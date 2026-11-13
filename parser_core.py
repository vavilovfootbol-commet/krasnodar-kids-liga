import json
import re

# Имитация текста, который наш парсер в будущем будет автоматически скачивать из ВКонтакте
raw_vk_post = """
🏆 Результаты выходных! КДФЛ, 2013 г.р. (U-10)
Дата: 22.10.2023
Академия Краснодар - ФШМ Екатеринодар 3:1
Голы: Иванов (2), Смирнов (1) — Петров (1)
"""

def parse_match_text(text):
    print("🤖 Парсер запущен. Анализирую текст...")
    
    date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', text)
    match_date = date_match.group(1) if date_match else "Дата не найдена"
    
    score_match = re.search(r'(\d+):(\d+)', text)
    teams_line = re.search(r'(.+?)\s*-\s*(.+?)\s*\d+:\d+', text)
    
    if score_match and teams_line:
        team_home = teams_line.group(1).strip()
        team_away = teams_line.group(2).strip()
        score_home = int(score_match.group(1))
        score_away = int(score_match.group(2))
        
        match_data = {
            "tournament": "КДФЛ",
            "age_group": "U-10",
            "date": match_date,
            "team_home": team_home,
            "team_away": team_away,
            "score_home": score_home,
            "score_away": score_away
        }
        
        print("\n✅ Данные успешно структурированы:")
        print(json.dumps(match_data, ensure_ascii=False, indent=4))
        return match_data
    else:
        print("❌ Ошибка: Не удалось распознать команды или счет.")
        return None

structured_data = parse_match_text(raw_vk_post)