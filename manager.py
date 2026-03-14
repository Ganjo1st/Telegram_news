import os
import json
import subprocess
import sys
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import io
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
BOT_SCRIPT = os.getenv('BOT_SCRIPT', 'bot.py')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS')
FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID')

# Имена файлов состояния
STATE_FILES = [
    'sent_links.json',
    'sent_hashes.json',
    'sent_titles.json',
    'posts_log.json'
]

# УВЕЛИЧИВАЕМ ТАЙМАУТ ДО 20 МИНУТ
BOT_TIMEOUT = 1200  # 20 минут

def get_drive_service():
    """Создает сервис для работы с Google Drive из JSON-ключа."""
    if not GOOGLE_CREDENTIALS_JSON:
        logger.error("❌ Переменная окружения GOOGLE_CREDENTIALS не найдена!")
        sys.exit(1)
    try:
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_info, scopes=['https://www.googleapis.com/auth/drive']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"❌ Ошибка создания сервиса Google Drive: {e}")
        sys.exit(1)

def download_files(service):
    """Скачивает все файлы состояния из указанной папки на Google Drive."""
    logger.info("📥 Скачивание файлов состояния с Google Drive...")
    for file_name in STATE_FILES:
        try:
            query = f"name='{file_name}' and '{FOLDER_ID}' in parents and trashed=false"
            results = service.files().list(q=query, fields="files(id, name)").execute()
            files = results.get('files', [])

            if files:
                file_id = files[0]['id']
                request = service.files().get_media(fileId=file_id)
                fh = io.FileIO(file_name, 'wb')
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while done is False:
                    status, done = downloader.next_chunk()
                logger.info(f"  ✅ {file_name} скачан.")
            else:
                logger.info(f"  ⚠️ {file_name} не найден на Диске. Создаю пустой.")
                with open(file_name, 'w') as f:
                    if file_name == 'posts_log.json':
                        json.dump([], f)
                    else:
                        json.dump([], f)
        except Exception as e:
            logger.error(f"  ❌ Ошибка скачивания {file_name}: {e}")
            with open(file_name, 'w') as f:
                if file_name == 'posts_log.json':
                    json.dump([], f)
                else:
                    json.dump([], f)

def upload_files(service):
    """Загружает обновленные файлы состояния обратно на Google Drive."""
    logger.info("📤 Загрузка файлов состояния на Google Drive...")
    for file_name in STATE_FILES:
        try:
            if not os.path.exists(file_name):
                logger.warning(f"  ⚠️ {file_name} не найден локально, пропуск загрузки.")
                continue

            query = f"name='{file_name}' and '{FOLDER_ID}' in parents and trashed=false"
            results = service.files().list(q=query, fields="files(id, name)").execute()
            files = results.get('files', [])

            media = MediaFileUpload(file_name, mimetype='application/json')

            if files:
                file_id = files[0]['id']
                service.files().update(fileId=file_id, media_body=media).execute()
                logger.info(f"  ✅ {file_name} обновлен на Диске.")
            else:
                file_metadata = {'name': file_name, 'parents': [FOLDER_ID]}
                service.files().create(body=file_metadata, media_body=media).execute()
                logger.info(f"  ✅ {file_name} создан на Диске.")
        except Exception as e:
            logger.error(f"  ❌ Ошибка загрузки {file_name}: {e}")

def run_bot():
    """Запускает скрипт бота с таймаутом."""
    logger.info(f"🚀 Запуск бота ({BOT_SCRIPT}) с таймаутом {BOT_TIMEOUT//60} минут...")
    try:
        bot_env = os.environ.copy()
        
        for f in STATE_FILES:
            if f == 'sent_links.json':
                bot_env['SENT_LINKS_FILE'] = f
            elif f == 'sent_hashes.json':
                bot_env['SENT_HASHES_FILE'] = f
            elif f == 'sent_titles.json':
                bot_env['SENT_TITLES_FILE'] = f
            elif f == 'posts_log.json':
                bot_env['POSTS_LOG_FILE'] = f
        
        bot_env['TELEGRAM_TOKEN'] = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        bot_env['CHANNEL_ID'] = os.environ.get('TELEGRAM_CHANNEL_ID', '@Novikon_news')
        
        if not bot_env['TELEGRAM_TOKEN']:
            logger.error("❌ TELEGRAM_TOKEN не передан в менеджер!")
            return False

        result = subprocess.run(
            [sys.executable, BOT_SCRIPT], 
            env=bot_env, 
            capture_output=True, 
            text=True, 
            timeout=BOT_TIMEOUT
        )

        if result.stdout:
            last_lines = result.stdout.strip().split('\n')[-20:]
            logger.info(f"📢 ПОСЛЕДНИЕ 20 СТРОК ВЫВОДА БОТА:\n" + "\n".join(last_lines))
        if result.stderr:
            logger.error(f"⚠️ ОШИБКИ БОТА:\n{result.stderr}")

        if result.returncode != 0:
            logger.error(f"❌ Бот завершился с ошибкой (код {result.returncode})")
            return False
        else:
            logger.info("✅ Бот успешно завершил работу.")
            return True
            
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Бот выполнялся слишком долго (>{BOT_TIMEOUT//60} минут). Прерывание.")
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске бота: {e}")
        return False

if __name__ == "__main__":
    logger.info("="*50)
    logger.info("🚀 ЗАПУСК МЕНЕДЖЕРА")
    logger.info("="*50)

    if not FOLDER_ID:
        logger.error("❌ Переменная окружения GOOGLE_DRIVE_FOLDER_ID не задана!")
        sys.exit(1)

    drive_service = get_drive_service()
    download_files(drive_service)
    
    success = run_bot()
    
    if success:
        upload_files(drive_service)
        logger.info("✅ Все операции завершены, файлы синхронизированы.")
    else:
        logger.warning("⚠️ Бот завершился с ошибкой, файлы НЕ загружены на Disk, чтобы не повредить данные.")

    logger.info("🏁 РАБОТА МЕНЕДЖЕРА ЗАВЕРШЕНА")
