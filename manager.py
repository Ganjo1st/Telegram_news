import os
import json
import importlib.util
import sys
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import io

# --- НАСТРОЙКИ (их будет передавать GitHub Actions) ---
BOT_NAME = os.getenv('BOT_NAME') # Имя бота, например, "bot1"
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS') # Содержимое JSON-ключа
# ID папки на Google Диске (достаем из ссылки: https://drive.google.com/drive/folders/XXXXX)
FOLDER_ID = 'ВАШ_ID_ПАПКИ_ВСТАВЬТЕ_СЮДА'

# --- 1. Подключаемся к Google Drive ---
def get_drive_service():
    """Создает сервис для работы с Google Drive"""
    import json
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=['https://www.googleapis.com/auth/drive'])
    return build('drive', 'v3', credentials=creds)

# --- 2. Скачиваем файл состояния с Google Drive ---
def download_file_from_drive(service, file_name, destination):
    """Скачивает файл из Google Drive"""
    try:
        # Ищем файл в нашей папке
        query = f"name='{file_name}' and '{FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])

        if files:
            file_id = files[0]['id']
            request = service.files().get_media(fileId=file_id)
            fh = io.FileIO(destination, 'wb')
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            print(f"✅ Файл {file_name} скачан с Google Drive.")
        else:
            # Если файла нет, создаем пустой
            print(f"⚠️ Файл {file_name} не найден на Диске. Создаем пустой локально.")
            with open(destination, 'w') as f:
                json.dump({}, f)
    except Exception as e:
        print(f"❌ Ошибка скачивания: {e}")
        # В случае ошибки создаем пустой файл
        with open(destination, 'w') as f:
            json.dump({}, f)

# --- 3. Загружаем файл состояния обратно на Google Drive ---
def upload_file_to_drive(service, file_path, file_name):
    """Загружает файл на Google Drive, заменяя старый"""
    try:
        # Ищем старый файл, чтобы удалить или обновить
        query = f"name='{file_name}' and '{FOLDER_ID}' in parents and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])

        media = MediaFileUpload(file_path, mimetype='application/json')

        if files:
            file_id = files[0]['id']
            # Обновляем существующий файл
            updated_file = service.files().update(fileId=file_id, media_body=media).execute()
            print(f"✅ Файл {file_name} обновлен на Google Drive.")
        else:
            # Создаем новый файл в нашей папке
            file_metadata = {'name': file_name, 'parents': [FOLDER_ID]}
            new_file = service.files().create(body=file_metadata, media_body=media).execute()
            print(f"✅ Файл {file_name} создан на Google Drive.")
    except Exception as e:
        print(f"❌ Ошибка загрузки: {e}")

# --- 4. Запускаем нужного бота ---
def run_bot(bot_name):
    """Динамически импортирует и запускает функцию main() из файла бота"""
    bot_file = f"{bot_name}.py"
    state_file = f"sent_links_{bot_name}.json"

    # Проверяем, существует ли файл бота
    if not os.path.exists(bot_file):
        print(f"❌ Файл бота {bot_file} не найден!")
        return False

    try:
        # Динамически импортируем модуль бота
        spec = importlib.util.spec_from_file_location(bot_name, bot_file)
        bot_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot_module)

        # Проверяем, есть ли в боте функция main()
        if hasattr(bot_module, 'main'):
            # Передаем имя файла состояния, чтобы бот его использовал
            # ВАЖНО! Ваш бот должен принимать аргумент `state_file` и читать/писать этот файл.
            bot_module.main(state_file)
            print(f"✅ Бот {bot_name} успешно выполнен.")
            return True
        else:
            print(f"❌ В файле {bot_file} нет функции main()!")
            return False
    except Exception as e:
        print(f"❌ Ошибка при выполнении бота {bot_name}: {e}")
        return False

# --- ГЛАВНАЯ ФУНКЦИЯ ---
if __name__ == "__main__":
    print(f"🚀 Запуск менеджера для бота: {BOT_NAME}")

    # 1. Подключаемся к Drive
    service = get_drive_service()

    # 2. Определяем имена файлов
    local_state_file = f"sent_links_{BOT_NAME}.json"
    drive_state_file = f"sent_links_{BOT_NAME}.json"

    # 3. Скачиваем актуальное состояние с Google Drive
    download_file_from_drive(service, drive_state_file, local_state_file)

    # 4. Запускаем бота
    success = run_bot(BOT_NAME)

    if success:
        # 5. Если бот выполнился, загружаем обновленное состояние обратно
        upload_file_to_drive(service, local_state_file, drive_state_file)
    else:
        print("❌ Бот не выполнен, файл состояния не загружается на Drive.")

    print("🏁 Работа менеджера завершена.")
