#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Менеджер для запуска бота с синхронизацией Google Drive
"""

import os
import sys
import json
import time
import logging
import subprocess
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import io

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('manager')

# Настройки
BOT_NAME = os.getenv('BOT_NAME', 'news_bot')
BOT_SCRIPT = os.getenv('BOT_SCRIPT', f'bots/{BOT_NAME}.py')
GOOGLE_CREDENTIALS = os.getenv('GOOGLE_CREDENTIALS')
FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID')

STATE_FILE = f"state_{BOT_NAME}.json"
BOT_TIMEOUT = int(os.getenv('BOT_TIMEOUT', '1800'))

def get_drive_service():
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDENTIALS),
            scopes=['https://www.googleapis.com/auth/drive']
        )
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"❌ Ошибка Drive: {e}")
        sys.exit(1)

def download_state(service):
    logger.info("📥 Загрузка состояния...")
    try:
        query = f"name='{STATE_FILE}' and '{FOLDER_ID}' in parents"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])
        
        if files:
            request = service.files().get_media(fileId=files[0]['id'])
            with open(STATE_FILE, 'wb') as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            logger.info(f"✅ {STATE_FILE} загружен")
        else:
            logger.info(f"⚠️ {STATE_FILE} не найден, создаю пустой")
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'sent_links': [],
                    'sent_hashes': [],
                    'sent_titles': [],
                    'posts_log': []
                }, f)
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки: {e}")
        with open(STATE_FILE, 'w') as f:
            json.dump({
                'sent_links': [],
                'sent_hashes': [],
                'sent_titles': [],
                'posts_log': []
            }, f)

def upload_state(service):
    logger.info("📤 Загрузка состояния...")
    try:
        query = f"name='{STATE_FILE}' and '{FOLDER_ID}' in parents"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])
        
        media = MediaFileUpload(STATE_FILE, mimetype='application/json')
        
        if files:
            service.files().update(fileId=files[0]['id'], media_body=media).execute()
            logger.info(f"✅ {STATE_FILE} обновлен")
        else:
            file_metadata = {'name': STATE_FILE, 'parents': [FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media).execute()
            logger.info(f"✅ {STATE_FILE} создан")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки: {e}")

def run_bot():
    logger.info(f"🚀 Запуск {BOT_SCRIPT}")
    env = os.environ.copy()
    env['STATE_FILE'] = STATE_FILE
    
    try:
        result = subprocess.run(
            [sys.executable, BOT_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=BOT_TIMEOUT
        )
        
        if result.stdout:
            lines = result.stdout.strip().split('\n')[-20:]
            logger.info("📢 Последние строки вывода:")
            for line in lines:
                if line.strip():
                    logger.info(f"  {line}")
        
        if result.stderr:
            logger.error(f"⚠️ Ошибки:\n{result.stderr}")
        
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Таймаут {BOT_TIMEOUT//60} мин")
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False

def main():
    logger.info("="*50)
    logger.info(f"🚀 Менеджер запущен")
    logger.info(f"🤖 Бот: {BOT_NAME}")
    logger.info(f"📁 Файл: {STATE_FILE}")
    logger.info("="*50)

    if not GOOGLE_CREDENTIALS or not FOLDER_ID:
        logger.error("❌ Нет GOOGLE_CREDENTIALS или FOLDER_ID")
        sys.exit(1)

    service = get_drive_service()
    download_state(service)
    
    success = run_bot()
    
    if success:
        upload_state(service)
        logger.info("✅ Готово")
    else:
        logger.warning("⚠️ Ошибка, состояние не загружено")
    
    logger.info("="*50)

if __name__ == "__main__":
    main()
