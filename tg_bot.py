import os
import io
import re
from datetime import datetime
import pandas as pd
import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(BASE_DIR, "credentials.json")
SPREADSHEET_NAME = "Krasnodar Kids Liga Master"

DB_SCHEMA = {
    "Лиги": ["ID_Лиги", "Название", "Возраст", "Коэффициент_Престижа", "Статус"],
    "Команды": ["ID_Команды", "Название", "Школа", "Возраст", "Текущий_Рейтинг", "Игры", "Победы", "Ничьи", "Поражения", "ЗМ", "ПМ"],
    "Матчи": ["ID_Матча", "Дата", "ID_Лиги", "Тур", "ID_Хозяева", "ID_Гости", "Счет_Х", "Счет_Г", "Статус_Обработки"],
    "История_Рейтинга": ["Дата", "ID_Команды", "ID_Матча", "Рейтинг_После"]
}

def get_google_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME)

def parse_float(val, default=0.0):
    if val is None or str(val).strip() == "":
        return default
    clean = str(val).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return default

def calculate_elo(rating_a, rating_b, score_a, score_b, league_coef=1.0, k_factor=32):
    expected_a = 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    expected_b = 1 / (1 + 10 ** ((rating_a - rating_b) / 400))

    if score_a > score_b:
        actual_a, actual_b = 1.0, 0.0
    elif score_a < score_b:
        actual_a, actual_b = 0.0, 1.0
    else:
        actual_a, actual_b = 0.5, 0.5

    goal_diff = abs(score_a - score_b)
    diff_mult = 1.0 if goal_diff <= 1 else (1.1 if goal_diff == 2 else 1.2)

    k = k_factor * league_coef * diff_mult
    new_rating_a = round(rating_a + k * (actual_a - expected_a))
    new_rating_b = round(rating_b + k * (actual_b - expected_b))
    return int(new_rating_a), int(new_rating_b)

def parse_match_line(line):
    """Разбирает строку вида 'Лига | Хозяева - Гости | 3:1' или 'Хозяева - Гости 3:1'."""
    line = line.strip()
    if not line:
        return None

    try:
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3 and "-" in parts[1] and ":" in parts[2]:
                league = parts[0]
                home, away = [t.strip() for t in parts[1].rsplit("-", 1)]
                score_match = re.search(r"(\d+)\s*[:\-]\s*(\d+)", parts[2])
                if score_match:
                    sh, sg = int(score_match.group(1)), int(score_match.group(2))
                    return {"league": league, "home": home, "away": away, "sh": sh, "sg": sg}

        m = re.match(r"^(.+?)\s*[-–—]\s*(.+?)\s+(\d+)\s*[:\-]\s*(\d+)$", line)
        if m:
            return {
                "league": "Общая лига",
                "home": m.group(1).strip(),
                "away": m.group(2).strip(),
                "sh": int(m.group(3)),
                "sg": int(m.group(4))
            }
    except Exception:
        return None
    return None

def process_matches_batch(matches_list):
    """Пакетная обработка списка матчей с записью в Google Таблицу."""
    sheet = get_google_sheet()
    ws_leagues = sheet.worksheet("Лиги")
    ws_teams = sheet.worksheet("Команды")
    ws_matches = sheet.worksheet("Матчи")
    ws_history = sheet.worksheet("История_Рейтинга")

    leagues_data = ws_leagues.get_all_records()
    teams_data = ws_teams.get_all_records()
    existing_matches_count = max(0, len(ws_matches.get_all_values()) - 1)

    leagues_map = {str(r["Название"]).strip().lower(): r for r in leagues_data}
    teams_map = {str(r["Название"]).strip().lower(): r for r in teams_data}

    new_leagues_rows = []
    new_matches_rows = []
    new_history_rows = []
    summary_lines = []

    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for idx, m in enumerate(matches_list, start=1):
        l_name = m["league"]
        l_key = l_name.lower()
        if l_key not in leagues_map:
            new_l_id = f"L_{len(leagues_map) + 1:02d}"
            l_rec = {"ID_Лиги": new_l_id, "Название": l_name, "Возраст": "Общий", "Коэффициент_Престижа": 1.0, "Статус": "Активна"}
            leagues_map[l_key] = l_rec
            new_leagues_rows.append([new_l_id, l_name, "Общий", 1, "Активна"])
        league_rec = leagues_map[l_key]
        league_coef = parse_float(league_rec.get("Коэффициент_Престижа"), 1.0)

        for t_name in (m["home"], m["away"]):
            t_key = t_name.lower()
            if t_key not in teams_map:
                new_t_id = f"T_{len(teams_map) + 1:03d}"
                teams_map[t_key] = {
                    "ID_Команды": new_t_id, "Название": t_name, "Школа": "Не указана", "Возраст": "Общий",
                    "Текущий_Рейтинг": 1000, "Игры": 0, "Победы": 0, "Ничьи": 0, "Поражения": 0, "ЗМ": 0, "ПМ": 0
                }

        th = teams_map[m["home"].lower()]
        tg = teams_map[m["away"].lower()]

        old_elo_h = int(round(parse_float(th["Текущий_Рейтинг"], 1000)))
        old_elo_g = int(round(parse_float(tg["Текущий_Рейтинг"], 1000)))
        new_elo_h, new_elo_g = calculate_elo(old_elo_h, old_elo_g, m["sh"], m["sg"], league_coef)

        th["Текущий_Рейтинг"] = new_elo_h
        th["Игры"] = int(parse_float(th["Игры"])) + 1
        th["Победы"] = int(parse_float(th["Победы"])) + (1 if m["sh"] > m["sg"] else 0)
        th["Ничьи"] = int(parse_float(th["Ничьи"])) + (1 if m["sh"] == m["sg"] else 0)
        th["Поражения"] = int(parse_float(th["Поражения"])) + (1 if m["sh"] < m["sg"] else 0)
        th["ЗМ"] = int(parse_float(th["ЗМ"])) + m["sh"]
        th["ПМ"] = int(parse_float(th["ПМ"])) + m["sg"]

        tg["Текущий_Рейтинг"] = new_elo_g
        tg["Игры"] = int(parse_float(tg["Игры"])) + 1
        tg["Победы"] = int(parse_float(tg["Победы"])) + (1 if m["sg"] > m["sh"] else 0)
        tg["Ничьи"] = int(parse_float(tg["Ничьи"])) + (1 if m["sg"] == m["sh"] else 0)
        tg["Поражения"] = int(parse_float(tg["Поражения"])) + (1 if m["sg"] < m["sh"] else 0)
        tg["ЗМ"] = int(parse_float(tg["ЗМ"])) + m["sg"]
        tg["ПМ"] = int(parse_float(tg["ПМ"])) + m["sh"]

        match_id = f"M_{existing_matches_count + idx:04d}"
        new_matches_rows.append([match_id, today_str, league_rec["ID_Лиги"], "-", th["ID_Команды"], tg["ID_Команды"], m["sh"], m["sg"], "Учтен"])
        new_history_rows.append([today_str, th["ID_Команды"], match_id, new_elo_h])
        new_history_rows.append([today_str, tg["ID_Команды"], match_id, new_elo_g])

        dh = new_elo_h - old_elo_h
        dg = new_elo_g - old_elo_g
        summary_lines.append(
            f"• <b>{m['home']} {m['sh']}:{m['sg']} {m['away']}</b> "
            f"({m['home']}: {new_elo_h} [{'+' if dh>=0 else ''}{dh}], {m['away']}: {new_elo_g} [{'+' if dg>=0 else ''}{dg}])"
        )

    if new_leagues_rows:
        ws_leagues.append_rows(new_leagues_rows)

    all_teams_rows = [DB_SCHEMA["Команды"]]
    for t in teams_map.values():
        all_teams_rows.append([
            t["ID_Команды"], t["Название"], t["Школа"], t["Возраст"],
            int(round(parse_float(t["Текущий_Рейтинг"], 1000))),
            int(parse_float(t["Игры"])), int(parse_float(t["Победы"])),
            int(parse_float(t["Ничьи"])), int(parse_float(t["Поражения"])),
            int(parse_float(t["ЗМ"])), int(parse_float(t["ПМ"]))
        ])
    ws_teams.clear()
    ws_teams.append_rows(all_teams_rows)
    ws_teams.format("A1:K1", {"textFormat": {"bold": True}})

    if new_matches_rows:
        ws_matches.append_rows(new_matches_rows)
    if new_history_rows:
        ws_history.append_rows(new_history_rows)

    return summary_lines

# ==================== КОМАНДЫ БОТА ====================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "⚽ <b>Krasnodar Kids Liga — Аналитический штаб</b>\n\n"
        "<b>Ввод результатов:</b>\n"
        "• /match <code>Лига | Хозяева - Гости | Счет</code> — один матч\n"
        "• /batch — пакетная загрузка списком (каждый матч с новой строки)\n"
        "• /template — скачать пустой Excel-шаблон для заполнения\n"
        "• Или просто <b>отправь готовый Excel-файл (.xlsx)</b> в этот чат!\n\n"
        "<b>Аналитика и графики:</b>\n"
        "• /top — единый городской рейтинг команд\n"
        "• /chart <code>Название команды</code> — кривая рейтинга\n"
        "• /stats — сводка по базе данных\n"
        "• /clear_data — очистить тестовые записи\n"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['clear_data'])
def clear_test_data(message):
    bot.send_message(message.chat.id, "🧹 Очищаю тестовые записи...")
    try:
        sheet = get_google_sheet()
        for sheet_name, headers in DB_SCHEMA.items():
            ws = sheet.worksheet(sheet_name)
            ws.clear()
            ws.append_row(headers)
            ws.format("A1:Z1", {"textFormat": {"bold": True}})
        bot.send_message(message.chat.id, "✅ Все 4 листа очищены и готовы к работе!")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['stats'])
def send_stats(message):
    bot.send_message(message.chat.id, "⏳ Проверяю базу...")
    try:
        sheet = get_google_sheet()
        report = [f"📊 <b>База «{SPREADSHEET_NAME}»</b>\n"]
        for ws in sheet.worksheets():
            rows = ws.get_all_values()
            non_empty = [r for r in rows if any(cell.strip() for cell in r)]
            count = max(0, len(non_empty) - 1)
            report.append(f"• <b>{ws.title}</b>: {count} записей")
        bot.send_message(message.chat.id, "\n".join(report), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['template'])
def send_excel_template(message):
    """Генерирует и отправляет чистый Excel-шаблон для пакетной загрузки матчей."""
    sample_data = [
        {"Лига": "КДФЛ 2019", "Хозяева": "Академия Краснодар", "Гости": "Спартак КРД", "Счет": "2:1"},
        {"Лига": "КДФЛ 2019", "Хозяева": "Екатеринодар", "Гости": "Олимп", "Счет": "0:3"},
        {"Лига": "Лига А 2018", "Хозяева": "Чемпион", "Гости": "Барс", "Счет": "1:1"},
    ]
    df = pd.DataFrame(sample_data)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Матчи')
    buf.seek(0)

    bot.send_document(
        message.chat.id,
        buf,
        visible_file_name="Shablon_Matchey.xlsx",
        caption=(
            "📄 <b>Готовый шаблон для загрузки матчей!</b>\n\n"
            "Заполни его своими играми (колонки <code>Лига</code>, <code>Хозяева</code>, <code>Гости</code>, <code>Счет</code>) "
            "и просто перетащи файл обратно в этот чат."
        ),
        parse_mode="HTML"
    )

@bot.message_handler(commands=['match'])
def add_single_match(message):
    raw_text = message.text.replace("/match", "", 1).strip()
    parsed = parse_match_line(raw_text)
    if not parsed:
        bot.reply_to(
            message,
            "⚠️ Неверный формат. Пример:\n<code>/match КДФЛ 2019 | Спартак КРД - Краснодар Юг | 3:1</code>",
            parse_mode="HTML"
        )
        return

    bot.send_message(message.chat.id, "⏳ Записываю матч и пересчитываю рейтинг...")
    try:
        summary = process_matches_batch([parsed])
        bot.send_message(message.chat.id, f"✅ <b>Матч учтён!</b>\n\n{summary[0]}", parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['batch'])
def add_batch_matches(message):
    raw_text = message.text.replace("/batch", "", 1).strip()
    if not raw_text:
        example = (
            "📋 <b>Пакетная загрузка матчей списком</b>\n\n"
            "Отправь команду <code>/batch</code> и с новой строки перечисли матчи:\n\n"
            "<code>/batch\n"
            "КДФЛ 2019 | Спартак КРД - Барс | 2:1\n"
            "КДФЛ 2019 | Краснодар Юг - Екатеринодар | 0:0\n"
            "Лига А | Олимп - Чемпион | 4:2</code>"
        )
        bot.reply_to(message, example, parse_mode="HTML")
        return

    lines = raw_text.splitlines()
    parsed_matches = []
    errors = []

    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        m = parse_match_line(line)
        if m:
            parsed_matches.append(m)
        else:
            errors.append(f"Стр. {i}: <code>{line}</code>")

    if not parsed_matches:
        bot.reply_to(message, "❌ Не удалось распознать ни одной строки. Нажми /batch для примера.", parse_mode="HTML")
        return

    bot.send_message(message.chat.id, f"⏳ Обрабатываю пачку из <b>{len(parsed_matches)}</b> матчей...", parse_mode="HTML")
    try:
        summary = process_matches_batch(parsed_matches)
        reply = [f"✅ <b>Загружено матчей: {len(parsed_matches)}</b>\n"] + summary[:25]
        if len(summary) > 25:
            reply.append(f"\n<i>...и ещё {len(summary) - 25} матчей.</i>")
        if errors:
            reply.append("\n⚠️ <b>Не распознаны строки:</b>\n" + "\n".join(errors))
        bot.send_message(message.chat.id, "\n".join(reply), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка пакетной обработки:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['top'])
def show_top(message):
    bot.send_message(message.chat.id, "⏳ Считаю единый городской рейтинг...")
    try:
        sheet = get_google_sheet()
        ws_teams = sheet.worksheet("Команды")
        records = ws_teams.get_all_records()
        if not records:
            bot.send_message(message.chat.id, "📭 В базе пока нет команд. Загрузи матчи через /batch или Excel!")
            return

        sorted_teams = sorted(records, key=lambda x: parse_float(x["Текущий_Рейтинг"]), reverse=True)
        lines = ["🏆 <b>ЕДИНЫЙ РЕЙТИНГ КОМАНД КРАСНОДАРА</b>\n"]
        for i, t in enumerate(sorted_teams[:20], 1):
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
            rating_val = int(round(parse_float(t["Текущий_Рейтинг"], 1000)))
            lines.append(
                f"{medal} <b>{t['Название']}</b> — <b>{rating_val}</b> "
                f"(И:{t['Игры']}, В:{t['Победы']}, Н:{t['Ничьи']}, П:{t['Поражения']}, Мячи:{t['ЗМ']}-{t['ПМ']})"
            )
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['chart'])
def send_rating_chart(message):
    team_query = message.text.replace("/chart", "", 1).strip()
    if not team_query:
        bot.reply_to(message, "⚠️ Укажи название команды. Пример:\n<code>/chart Спартак КРД</code>", parse_mode="HTML")
        return

    bot.send_message(message.chat.id, f"📈 Строю кривую рейтинга для «{team_query}»...")
    try:
        sheet = get_google_sheet()
        ws_teams = sheet.worksheet("Команды")
        ws_history = sheet.worksheet("История_Рейтинга")

        teams = ws_teams.get_all_records()
        target_team = next((t for t in teams if team_query.lower() in str(t["Название"]).lower()), None)
        if not target_team:
            bot.send_message(message.chat.id, f"❌ Команда «{team_query}» не найдена в базе.")
            return

        team_id = target_team["ID_Команды"]
        team_name = target_team["Название"]
        history = ws_history.get_all_records()

        points = [1000]
        labels = ["Старт"]
        for h in history:
            if str(h["ID_Команды"]) == str(team_id):
                points.append(int(round(parse_float(h["Рейтинг_После"], 1000))))
                labels.append(f"Игра {len(points)-1}")

        if len(points) <= 1:
            bot.send_message(message.chat.id, f"ℹ️ У команды <b>{team_name}</b> пока нет сыгранных матчей.", parse_mode="HTML")
            return

        plt.figure(figsize=(8, 4.5))
        plt.plot(labels, points, marker='o', linewidth=2.5, color='#1f77b4', markersize=8)
        plt.axhline(y=1000, color='gray', linestyle='--', alpha=0.5, label='Базовый уровень (1000)')
        plt.title(f"Кривая рейтинга ELO: {team_name}", fontsize=13, fontweight='bold')
        plt.ylabel("Рейтинг ELO", fontsize=11)
        plt.grid(True, linestyle=':', alpha=0.6)

        for i, val in enumerate(points):
            plt.annotate(f"{val}", (labels[i], points[i]), textcoords="offset points", xytext=(0, 8), ha='center', fontweight='bold')

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        plt.close()

        bot.send_photo(
            message.chat.id,
            buf,
            caption=f"📊 <b>Динамика рейтинга: {team_name}</b>\nТекущий рейтинг: <b>{points[-1]}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка при построении графика:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(content_types=['document'])
def handle_excel_upload(message):
    doc = message.document
    if not doc.file_name.lower().endswith(('.xlsx', '.xls')):
        bot.reply_to(message, "⚠️ Пришли файл в формате Excel (<code>.xlsx</code>).", parse_mode="HTML")
        return

    bot.send_message(message.chat.id, f"📥 Скачиваю и разбираю файл <b>{doc.file_name}</b>...", parse_mode="HTML")
    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        df = pd.read_excel(io.BytesIO(downloaded))
        df.columns = [str(c).strip().lower() for c in df.columns]

        col_league = next((c for c in df.columns if 'лиг' in c or 'турнир' in c), None)
        col_home = next((c for c in df.columns if 'хозя' in c or 'команда 1' in c or 'команда1' in c), None)
        col_away = next((c for c in df.columns if 'гост' in c or 'команда 2' in c or 'команда2' in c), None)
        col_sh = next((c for c in df.columns if 'счет_х' in c or 'голы_х' in c or 'счет 1' in c), None)
        col_sg = next((c for c in df.columns if 'счет_г' in c or 'голы_г' in c or 'счет 2' in c), None)
        col_score = next((c for c in df.columns if c == 'счет' or c == 'результат'), None)

        if not (col_home and col_away and ((col_sh and col_sg) or col_score)):
            bot.send_message(
                message.chat.id,
                "❌ В Excel-файле должны быть колонки: <code>Хозяева</code>, <code>Гости</code> и <code>Счет</code> (или <code>Счет_Х</code> и <code>Счет_Г</code>). Опционально: <code>Лига</code>.",
                parse_mode="HTML"
            )
            return

        parsed_matches = []
        for _, row in df.iterrows():
            home = str(row[col_home]).strip()
            away = str(row[col_away]).strip()
            if not home or not away or home == 'nan' or away == 'nan':
                continue
            league = str(row[col_league]).strip() if col_league and str(row[col_league]).strip() != 'nan' else "Общая лига"

            if col_sh and col_sg:
                if pd.isna(row[col_sh]) or pd.isna(row[col_sg]):
                    continue
                sh, sg = int(float(row[col_sh])), int(float(row[col_sg]))
            else:
                sc_str = str(row[col_score]).strip()
                if ":" not in sc_str and "-" not in sc_str:
                    continue
                sep = ":" if ":" in sc_str else "-"
                sh, sg = [int(x.strip()) for x in sc_str.split(sep, 1)]

            parsed_matches.append({"league": league, "home": home, "away": away, "sh": sh, "sg": sg})

        if not parsed_matches:
            bot.send_message(message.chat.id, "⚠️ В файле не найдено заполненных матчей со счётом.")
            return

        summary = process_matches_batch(parsed_matches)
        reply = [f"✅ <b>Файл обработан! Загружено матчей: {len(parsed_matches)}</b>\n"] + summary[:25]
        if len(summary) > 25:
            reply.append(f"\n<i>...и ещё {len(summary) - 25} матчей записано в базу.</i>")
        bot.send_message(message.chat.id, "\n".join(reply), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка при чтении Excel:\n<code>{e}</code>", parse_mode="HTML")

# ==================== ПЕРЕХВАТЧИК ВСЕХ ОСТАЛЬНЫХ ТЕКСТОВ (ВСЕГДА В САМОМ КОНЦЕ) ====================

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    bot.reply_to(message, "Я на связи! Нажми /help, чтобы увидеть список команд, или скинь Excel-файл с матчами.")

if __name__ == "__main__":
    print("🤖 Аналитический бот с пакетной загрузкой и Excel запущен...")
    bot.infinity_polling()