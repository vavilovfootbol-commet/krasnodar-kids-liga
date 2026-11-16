import os
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESSED_FOLDER = os.path.join(BASE_DIR, "Processed_Data")

def analyze_matches():
    if not os.path.exists(PROCESSED_FOLDER) or not os.listdir(PROCESSED_FOLDER):
        print("📂 Папка с обработанными данными пуста. Сначала запустите parser_core.py!")
        return

    files = [f for f in os.listdir(PROCESSED_FOLDER) if f.endswith(('.xlsx', '.xls', '.csv'))]
    all_data = []

    for filename in files:
        file_path = os.path.join(PROCESSED_FOLDER, filename)
        try:
            excel = pd.ExcelFile(file_path)
            for sheet in excel.sheet_names:
                df = pd.read_excel(excel, sheet_name=sheet)
                all_data.append(df)
        except Exception as e:
            print(f"❌ Ошибка чтения файла {filename}: {e}")

    if not all_data:
        print("⚠️ Нет данных для аналитики.")
        return

    master_df = pd.concat(all_data, ignore_index=True)
    master_df.columns = [str(col).strip() for col in master_df.columns]

    print("\n" + "="*50)
    print("🏆 АВТОМАТИЧЕСКИЙ СПОРТИВНЫЙ ОТЧЕТ И АНАЛИТИКА")
    print("="*50)

    # Ищем таблицу с матчами по наличию колонок 'ID_Матча' или 'Счет_Х'
    if 'ID_Матча' in master_df.columns or 'Счет_Х' in master_df.columns:
        # Фильтруем строки, где реально есть матчи (убираем пустые строки)
        matches_df = master_df.dropna(subset=['ID_Матча']) if 'ID_Матча' in master_df.columns else master_df.dropna(subset=['Счет_Х'])
        
        total_matches = len(matches_df)
        print(f"📊 Всего учтено матчей в базе: {total_matches}")

        # Если есть данные по голам игроков
        if 'Голы' in master_df.columns and 'ID_Игрока' in master_df.columns:
            players_df = master_df.dropna(subset=['ID_Игрока'])
            players_df['Голы'] = pd.to_numeric(players_df['Голы'], errors='fillna').fillna(0)
            
            top_scorers = players_df.groupby('ID_Игрока')['Голы'].sum().reset_index()
            top_scorers = top_scorers.sort_values(by='Голы', ascending=False).head(3)
            
            print(f"\n⚽ ТОП-3 БОМБАРДИРА ЛИГИ:")
            for idx, row in top_scorers.iterrows():
                print(f"   • Игрок (ID: {row['ID_Игрока']}) — {int(row['Голы'])} голов")
        else:
            print("\nℹ️ Колонки с игроками и голами не заполнены в этой таблице.")
            
    else:
        print("⚠️ Не удалось найти таблицу с матчами. Проверьте структуру листов в файле.")

    print("="*50)
    print("📊 Анализ завершен успешно!")

if __name__ == "__main__":
    analyze_matches()