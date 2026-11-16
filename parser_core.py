import os
import time
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INCOMING_FOLDER = os.path.join(BASE_DIR, "Incoming_Data")
PROCESSED_FOLDER = os.path.join(BASE_DIR, "Processed_Data")
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")

os.makedirs(INCOMING_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

GOOGLE_SHEET_NAME = "Krasnodar Kids Liga Master"

def sync_to_google_sheets(dataframe, sheet_name):
    """Синхронизация с Google Таблицей"""
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open(GOOGLE_SHEET_NAME)

        try:
            worksheet = spreadsheet.worksheet(sheet_name)
            worksheet.clear()
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")

        # Подготовка данных и очистка от NaN (чтобы не было ошибок пустых ячеек)
        data_to_write = [dataframe.columns.tolist()] + dataframe.values.tolist()
        data_to_write = [[("" if pd.isna(val) else val) for val in row] for row in data_to_write]

        # Передаем данные напрямую без привязки к фиксированному диапазону
        worksheet.update(data_to_write)
        print(f"   ☁️ Успешно выгружено в Google Таблицу (лист: {sheet_name})!")

    except Exception as e:
        print(f"   ❌ Ошибка при выгрузке в Google Таблицу: {e}")

def process_files():
    files = [f for f in os.listdir(INCOMING_FOLDER) if f.endswith(('.xlsx', '.xls', '.csv'))]
    
    if not files:
        print("⏳ Ожидание файлов в папке Incoming_Data...")
        return

    print(f"⚡ Найдено новых файлов: {len(files)}. Начинаю обработку и синхронизацию...")

    for filename in files:
        file_path = os.path.join(INCOMING_FOLDER, filename)
        print(f"   🔄 Обработка файла: {filename}...")
        
        try:
            # Безопасное чтение файла с автоматическим закрытием дескриптора (защита от WinError 32)
            if filename.endswith('.csv'):
                df_dict = {"Sheet1": pd.read_csv(file_path)}
            else:
                with pd.ExcelFile(file_path) as excel:
                    df_dict = {sheet: pd.read_excel(excel, sheet_name=sheet) for sheet in excel.sheet_names}

            for sheet, df in df_dict.items():
                cloud_sheet_title = f"{filename[:10]}_{sheet}"[:30]
                sync_to_google_sheets(df, cloud_sheet_title)

            # Небольшая пауза и безопасное перемещение в архив
            processed_path = os.path.join(PROCESSED_FOLDER, filename)
            if os.path.exists(processed_path):
                os.remove(processed_path)
            
            time.sleep(0.5)
            os.rename(file_path, processed_path)
            print(f"   ✅ Файл {filename} успешно обработан и перемещен в архив.")

        except Exception as e:
            print(f"   ❌ Ошибка с файлом {filename}: {e}")

if __name__ == "__main__":
    print("🚀 Бот-автоматизатор запущен и подключен к облаку Google Sheets!")
    process_files()