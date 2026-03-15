#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Универсальный менеджер для запуска ботов с синхронизацией Google Drive
Версия 2.0 - Поддержка единого файла состояния, улучшенная обработка ошибок
"""

import os
import sys
import json
import time
import logging
import subprocess
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
import io

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# Создаём папку для логов, если её нет
LOG_DIR = 'logs'
os.makedirs(LOG_DIR, exist_ok=True)

# Лог-файл с датой в имени
log_filename = f"{LOG_DIR}/manager_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('manager')

# ========== КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
BOT_NAME = os.getenv('BOT_NAME', 'news_bot')  # Имя бота: news_bot, ninth_poster и т.д.
BOT_SCRIPT = os.getenv('BOT_SCRIPT', f'bots/{BOT_NAME}.py')  # Путь к скрипту бота
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS')
FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID')

# Таймауты и настройки
BOT_TIMEOUT = int(os.getenv('BOT_TIMEOUT', '1800'))  # 30 минут на выполнение бота
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))     # Количество попыток при ошибках Drive
RETRY_DELAY = int(os.getenv('RETRY_DELAY', '5'))     # Задержка между попытками (сек)

# Имя файла состояния (один файл для всего)
STATE_FILE = f"state_{BOT_NAME}.json"

# ========== КЛАСС ДЛЯ РАБОТЫ С GOOGLE DRIVE ==========
class GoogleDriveManager:
    """Менеджер для работы с Google Drive с поддержкой повторных попыток"""
    
    def __init__(self, credentials_json: str, folder_id: str):
        self.credentials_json = credentials_json
        self.folder_id = folder_id
        self.service = None
        self._connect()
    
    def _connect(self):
        """Устанавливает соединение с Google Drive"""
        try:
            creds_info = json.loads(self.credentials_json)
            creds = service_account.Credentials.from_service_account_info(
                creds_info, 
                scopes=['https://www.googleapis.com/auth/drive']
            )
            self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
            logger.info("✅ Подключение к Google Drive установлено")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Google Drive: {e}")
            raise
    
    def _execute_with_retry(self, func, *args, **kwargs):
        """Выполняет функцию с повторными попытками при ошибках"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except HttpError as e:
                if attempt == MAX_RETRIES:
                    logger.error(f"❌ Ошибка Google Drive после {MAX_RETRIES} попыток: {e}")
                    raise
                wait_time = RETRY_DELAY * attempt
                logger.warning(f"⚠️ Ошибка Google Drive (попытка {attempt}/{MAX_RETRIES}), жду {wait_time}с...")
                time.sleep(wait_time)
            except Exception as e:
                logger.error(f"❌ Неожиданная ошибка: {e}")
                raise
    
    def download_file(self, file_name: str, destination: str) -> bool:
        """Скачивает файл из Google Drive"""
        try:
            # Ищем файл в указанной папке
            query = f"name='{file_name}' and '{self.folder_id}' in parents and trashed=false"
            
            def _list_files():
                return self.service.files().list(q=query, fields="files(id, name)").execute()
            
            results = self._execute_with_retry(_list_files)
            files = results.get('files', [])
            
            if files:
                file_id = files[0]['id']
                
                def _download():
                    request = self.service.files().get_media(fileId=file_id)
                    fh = io.FileIO(destination, 'wb')
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while done is False:
                        status, done = downloader.next_chunk()
                    return True
                
                self._execute_with_retry(_download)
                logger.info(f"  ✅ {file_name} скачан с Google Drive")
                return True
            else:
                logger.info(f"  ⚠️ {file_name} не найден на Google Drive. Создаю пустой.")
                # Создаём пустой файл состояния
                default_state = {
                    'sent_links': [],
                    'sent_hashes': [],
                    'sent_titles': [],
                    'posts_log': []
                }
                with open(destination, 'w', encoding='utf-8') as f:
                    json.dump(default_state, f, ensure_ascii=False, indent=2)
                return False
                
        except Exception as e:
            logger.error(f"  ❌ Ошибка скачивания {file_name}: {e}")
            # В случае ошибки создаём пустой файл
            default_state = {
                'sent_links': [],
                'sent_hashes': [],
                'sent_titles': [],
                'posts_log': []
            }
            with open(destination, 'w', encoding='utf-8') as f:
                json.dump(default_state, f, ensure_ascii=False, indent=2)
            return False
    
    def upload_file(self, file_path: str, file_name: str) -> bool:
        """Загружает файл на Google Drive, заменяя старый"""
        try:
            if not os.path.exists(file_path):
                logger.warning(f"  ⚠️ {file_path} не найден локально, пропуск загрузки")
                return False
            
            # Ищем старый файл
            query = f"name='{file_name}' and '{self.folder_id}' in parents and trashed=false"
            
            def _list_files():
                return self.service.files().list(q=query, fields="files(id, name)").execute()
            
            results = self._execute_with_retry(_list_files)
            files = results.get('files', [])
            
            media = MediaFileUpload(file_path, mimetype='application/json', resumable=True)
            
            if files:
                # Обновляем существующий файл
                file_id = files[0]['id']
                
                def _update():
                    return self.service.files().update(fileId=file_id, media_body=media).execute()
                
                self._execute_with_retry(_update)
                logger.info(f"  ✅ {file_name} обновлен на Google Drive")
            else:
                # Создаём новый файл
                file_metadata = {'name': file_name, 'parents': [self.folder_id]}
                
                def _create():
                    return self.service.files().create(body=file_metadata, media_body=media).execute()
                
                self._execute_with_retry(_create)
                logger.info(f"  ✅ {file_name} создан на Google Drive")
            
            return True
            
        except Exception as e:
            logger.error(f"  ❌ Ошибка загрузки {file_name}: {e}")
            return False

# ========== ФУНКЦИИ ДЛЯ ЗАПУСКА БОТА ==========
def validate_environment() -> Tuple[bool, List[str]]:
    """Проверяет наличие всех необходимых переменных окружения"""
    missing_vars = []
    
    # Обязательные переменные
    if not GOOGLE_CREDENTIALS_JSON:
        missing_vars.append('GOOGLE_CREDENTIALS')
    if not FOLDER_ID:
        missing_vars.append('GOOGLE_DRIVE_FOLDER_ID')
    if not os.getenv('TELEGRAM_TOKEN'):
        missing_vars.append('TELEGRAM_TOKEN')
    
    return len(missing_vars) == 0, missing_vars

def prepare_environment() -> Dict[str, str]:
    """Подготавливает окружение для запуска бота"""
    bot_env = os.environ.copy()
    
    # Добавляем специфичные для бота переменные
    bot_env['STATE_FILE'] = STATE_FILE  # Единый файл состояния
    bot_env['BOT_NAME'] = BOT_NAME
    
    # Убеждаемся, что токены передаются
    if 'TELEGRAM_TOKEN' not in bot_env:
        bot_env['TELEGRAM_TOKEN'] = os.getenv('TELEGRAM_BOT_TOKEN', '')
    
    return bot_env

def run_bot() -> Tuple[bool, str, float]:
    """
    Запускает скрипт бота с таймаутом.
    Возвращает: (успех, сообщение, время выполнения в секундах)
    """
    start_time = time.time()
    
    # Проверяем существование файла бота
    if not os.path.exists(BOT_SCRIPT):
        error_msg = f"❌ Файл бота {BOT_SCRIPT} не найден!"
        logger.error(error_msg)
        return False, error_msg, 0
    
    logger.info(f"🚀 Запуск бота {BOT_NAME} ({BOT_SCRIPT})")
    logger.info(f"⏱️  Таймаут: {BOT_TIMEOUT//60} минут")
    
    try:
        # Подготавливаем окружение
        bot_env = prepare_environment()
        
        # Запускаем бота как подпроцесс
        result = subprocess.run(
            [sys.executable, BOT_SCRIPT],
            env=bot_env,
            capture_output=True,
            text=True,
            timeout=BOT_TIMEOUT
        )
        
        execution_time = time.time() - start_time
        logger.info(f"⏱️  Время выполнения: {execution_time:.1f} секунд")
        
        # Логируем вывод бота (последние 30 строк)
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            last_lines = lines[-30:] if len(lines) > 30 else lines
            logger.info(f"📢 ПОСЛЕДНИЕ {len(last_lines)} СТРОК ВЫВОДА БОТА:")
            for line in last_lines:
                if line.strip():
                    logger.info(f"  {line}")
        
        if result.stderr:
            logger.error(f"⚠️ STDERR БОТА:\n{result.stderr}")
        
        if result.returncode == 0:
            logger.info("✅ Бот успешно завершил работу")
            return True, "Бот выполнен успешно", execution_time
        else:
            error_msg = f"❌ Бот завершился с ошибкой (код {result.returncode})"
            logger.error(error_msg)
            return False, error_msg, execution_time
            
    except subprocess.TimeoutExpired:
        execution_time = time.time() - start_time
        error_msg = f"❌ Бот выполнялся слишком долго (>{BOT_TIMEOUT//60} минут)"
        logger.error(error_msg)
        return False, error_msg, execution_time
    except Exception as e:
        execution_time = time.time() - start_time
        error_msg = f"❌ Ошибка при запуске бота: {e}"
        logger.error(error_msg)
        return False, error_msg, execution_time

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========
def main():
    """Главная функция менеджера"""
    start_time = time.time()
    
    # Выводим красивый заголовок
    logger.info("="*60)
    logger.info(f"🚀 МЕНЕДЖЕР ЗАПУЩЕН")
    logger.info(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"🤖 Бот: {BOT_NAME}")
    logger.info(f"📁 Файл состояния: {STATE_FILE}")
    logger.info(f"📂 Папка Google Drive ID: {FOLDER_ID}")
    logger.info("="*60)
    
    # Проверяем переменные окружения
    env_ok, missing_vars = validate_environment()
    if not env_ok:
        logger.error(f"❌ Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")
        sys.exit(1)
    
    drive_manager = None
    try:
        # Подключаемся к Google Drive
        logger.info("📡 Подключение к Google Drive...")
        drive_manager = GoogleDriveManager(GOOGLE_CREDENTIALS_JSON, FOLDER_ID)
        
        # Скачиваем файл состояния
        logger.info("📥 Скачивание файла состояния...")
        drive_manager.download_file(STATE_FILE, STATE_FILE)
        
        # Запускаем бота
        logger.info("-"*50)
        success, message, exec_time = run_bot()
        logger.info("-"*50)
        
        if success:
            # Загружаем обновлённый файл состояния
            logger.info("📤 Загрузка обновлённого файла состояния...")
            upload_success = drive_manager.upload_file(STATE_FILE, STATE_FILE)
            
            if upload_success:
                logger.info("✅ Файл состояния успешно синхронизирован с Google Drive")
            else:
                logger.warning("⚠️ Не удалось загрузить файл состояния на Google Drive")
        else:
            logger.warning("⚠️ Бот завершился с ошибкой, файл состояния НЕ загружен на Drive")
        
        # Итоговая статистика
        total_time = time.time() - start_time
        logger.info("="*60)
        logger.info(f"📊 СТАТИСТИКА ВЫПОЛНЕНИЯ:")
        logger.info(f"   • Время работы бота: {exec_time:.1f} сек")
        logger.info(f"   • Общее время: {total_time:.1f} сек")
        logger.info(f"   • Статус: {'✅ УСПЕХ' if success else '❌ ОШИБКА'}")
        logger.info(f"   • Сообщение: {message}")
        logger.info("="*60)
        
        # Выходим с соответствующим кодом
        sys.exit(0 if success else 1)
        
    except KeyboardInterrupt:
        logger.warning("🛑 Менеджер остановлен пользователем")
        sys.exit(130)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка менеджера: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
