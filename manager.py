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

# --- НАСТРОЙКИ (передаются через переменные окружения GitHub Actions) ---
BOT_SCRIPT = os.getenv('BOT_SCRIPT', 'bot.py') # Имя вашего основного файла бота
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS') # Секретный ключ
FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID') # ID папки на Google Drive

# Имена файлов состояния (должны совпадать с теми, что использует bot.py)
STATE_FILES = [
    'sent_links.json',
    'sent_hashes.json',
    'sent_titles.json',
    'posts_log.json'
]

# --- 1. Подключение к Google Drive ---
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

# --- 2. Скачивание файлов с Google Drive ---
def download_files(service):
    """Скачивает все файлы состояния из указанной папки на Google Drive."""
    logger.info("📥 Скачивание файлов состояния с Google Drive...")
    for file_name in STATE_FILES:
        try:
            # Ищем файл в папке
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
                # Если файла нет, создаем пустой локально, чтобы бот мог работать
                logger.info(f"  ⚠️ {file_name} не найден на Диске. Создаю пустой.")
                with open(file_name, 'w') as f:
                    json.dump([] if file_name != 'posts_log.json' else {}, f) # posts_log - словарь
        except Exception as e:
            logger.error(f"  ❌ Ошибка скачивания {file_name}: {e}")
            # В случае ошибки тоже создаем пустой файл
            with open(file_name, 'w') as f:
                json.dump([] if file_name != 'posts_log.json' else {}, f)

# --- 3. Загрузка файлов на Google Drive ---
def upload_files(service):
    """Загружает обновленные файлы состояния обратно на Google Drive."""
    logger.info("📤 Загрузка файлов состояния на Google Drive...")
    for file_name in STATE_FILES:
        try:
            # Пропускаем, если файл не существует (например, бот его не создал)
            if not os.path.exists(file_name):
                logger.warning(f"  ⚠️ {file_name} не найден локально, пропуск загрузки.")
                continue

            # Ищем старый файл в папке, чтобы обновить его
            query = f"name='{file_name}' and '{FOLDER_ID}' in parents and trashed=false"
            results = service.files().list(q=query, fields="files(id, name)").execute()
            files = results.get('files', [])

            media = MediaFileUpload(file_name, mimetype='application/json')

            if files:
                file_id = files[0]['id']
                updated_file = service.files().update(fileId=file_id, media_body=media).execute()
                logger.info(f"  ✅ {file_name} обновлен на Диске.")
            else:
                # Если файла нет, создаем новый
                file_metadata = {'name': file_name, 'parents': [FOLDER_ID]}
                new_file = service.files().create(body=file_metadata, media_body=media).execute()
                logger.info(f"  ✅ {file_name} создан на Диске.")
        except Exception as e:
            logger.error(f"  ❌ Ошибка загрузки {file_name}: {e}")

# --- 4. Запуск бота ---
def run_bot():
    """Запускает скрипт бота, передавая ему пути к файлам через переменные окружения."""
    logger.info(f"🚀 Запуск бота ({BOT_SCRIPT})...")
    try:
        # Создаем окружение для дочернего процесса, добавляя пути к файлам
        bot_env = os.environ.copy()
        for f in STATE_FILES:
            # Устанавливаем переменные вроде SENT_LINKS_FILE=./sent_links.json
            env_var_name = f.replace('.', '_').upper() # Пример: SENT_LINKS_JSON -> SENT_LINKS_JSON (не совсем точно, лучше задать явно)
            # Зададим явно, как мы изменили в bot.py
            if f == 'sent_links.json':
                bot_env['SENT_LINKS_FILE'] = f
            elif f == 'sent_hashes.json':
                bot_env['SENT_HASHES_FILE'] = f
            elif f == 'sent_titles.json':
                bot_env['SENT_TITLES_FILE'] = f
            elif f == 'posts_log.json':
                bot_env['POSTS_LOG_FILE'] = f

        # Запускаем бота как отдельный процесс
        result = subprocess.run([sys.executable, BOT_SCRIPT], env=bot_env, capture_output=True, text=True, timeout=600) # Таймаут 10 минут

        # Выводим логи бота в лог менеджера
        if result.stdout:
            logger.info(f"📢 ВЫВОД БОТА:\n{result.stdout}")
        if result.stderr:
            logger.error(f"⚠️ ОШИБКИ БОТА:\n{result.stderr}")

        if result.returncode != 0:
            logger.error(f"❌ Бот завершился с ошибкой (код {result.returncode})")
            return False
        else:
            logger.info("✅ Бот успешно завершил работу.")
            return True
    except subprocess.TimeoutExpired:
        logger.error("❌ Бот выполнялся слишком долго (>10 минут). Прерывание.")
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске бота: {e}")
        return False

# --- ГЛАВНАЯ ФУНКЦИЯ ---
if __name__ == "__main__":
    logger.info("="*50)
    logger.info("🚀 ЗАПУСК МЕНЕДЖЕРА")
    logger.info("="*50)

    # 1. Проверяем обязательные параметры
    if not FOLDER_ID:
        logger.error("❌ Переменная окружения GOOGLE_DRIVE_FOLDER_ID не задана!")
        sys.exit(1)

    # 2. Подключаемся к Google Drive
    drive_service = get_drive_service()

    # 3. Скачиваем актуальные файлы состояния
    download_files(drive_service)

    # 4. Запускаем бота
    success = run_bot()

    # 5. Если бот выполнился (даже с ошибками? Лучше загружать, если файлы изменились), загружаем файлы обратно
    # Для простоты будем загружать всегда, если бот запустился.
    if success: # Загружаем только при успехе, чтобы не затереть данные плохим состоянием
        upload_files(drive_service)
        logger.info("✅ Все операции завершены, файлы синхронизированы.")
    else:
        logger.warning("⚠️ Бот завершился с ошибкой, файлы НЕ загружены на Disk, чтобы не повредить данные.")

    logger.info("🏁 РАБОТА МЕНЕДЖЕРА ЗАВЕРШЕНА")
