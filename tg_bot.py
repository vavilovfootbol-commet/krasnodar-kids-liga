import os
import re
import io
import math
from datetime import datetime
import telebot
import gspread
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

from social_scraper import (
    parse_vk_group,
    parse_tg_channel,
    inspect_vk_wall,
    extract_matches_from_image_bytes
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Krasnodar Kids Liga Master")

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в файле .env")

bot = telebot.TeleBot(BOT_TOKEN)

# --- ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ---
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

def get_sheets():
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)
    client = gspread.authorize(creds)
    ss = client.open(SPREADSHEET_NAME)
    return {
        "leagues": ss.worksheet("Лиги"),
        "teams": ss.worksheet("Команды"),
        "matches": ss.worksheet("Матчи"),
        "history": ss.worksheet("История_Рейтинга")
    }

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ID И ВОЗРАСТА ---
def extract_age_from_text(text, default="Общий"):
    m = re.search(r"\b(201[0-9]|202[0-2])\b", str(text))
    return m.group(1) if m else default

def extract_school_name(team_name):
    """Убирает год в скобках в конце, чтобы получить чистое имя школы/клуба."""
    return re.sub(r"\s*\(201[0-9]|202[0-2]\)\s*$", "", team_name).strip()

def generate_next_id(existing_ids, prefix, pad=2):
    """Генерирует следующий ID вида L_03, T_05 или M_001."""
    max_num = 0
    for item in existing_ids:
        m = re.search(r"(\d+)$", str(item).strip())
        if m:
            num = int(m.group(1))
            if num > max_num:
                max_num = num
    return f"{prefix}_{str(max_num + 1).zfill(pad)}"

# --- МАТЕМАТИКА РЕЙТИНГА ELO ---
def calculate_elo_delta(rating_h, rating_g, score_h, score_g, coef=1.0):
    """Расчёт изменения рейтинга ELO в целых числах."""
    if score_h > score_g:
        actual_h = 1.0
    elif score_h < score_g:
        actual_h = 0.0
    else:
        actual_h = 0.5

    expected_h = 1.0 / (1.0 + math.pow(10, (rating_g - rating_h) / 400.0))

    goal_diff = abs(score_h - score_g)
    if goal_diff <= 1:
        diff_mult = 1.0
    elif goal_diff == 2:
        diff_mult = 1.5
    else:
        diff_mult = 1.75 + (goal_diff - 3) / 8.0

    k_factor = 32.0 * coef * diff_mult
    delta_h = round(k_factor * (actual_h - expected_h))
    if delta_h == 0 and score_h != score_g:
        delta_h = 1 if score_h > score_g else -1
    return int(delta_h), int(-delta_h)

def load_leagues_map(ws_leagues):
    records = ws_leagues.get_all_records()
    leagues = {}
    existing_ids = []
    for row in records:
        lid = str(row.get("ID_Лиги", "")).strip()
        name = str(row.get("Название", "")).strip()
        if lid:
            existing_ids.append(lid)
        if name:
            try:
                coef = float(str(row.get("Коэффициент_Престижа", 1.0)).replace(",", "."))
            except ValueError:
                coef = 1.0
            leagues[name.lower()] = {
                "id": lid,
                "name": name,
                "age": str(row.get("Возраст", "Общий")).strip(),
                "coef": coef
            }
    return leagues, existing_ids

def get_or_create_league(ws_leagues, leagues_map, existing_ids, league_name):
    key = league_name.strip().lower()
    if key in leagues_map:
        return leagues_map[key]

    new_id = generate_next_id(existing_ids, "L", pad=2)
    existing_ids.append(new_id)
    age = extract_age_from_text(league_name, default="Общий")
    ws_leagues.append_row([new_id, league_name.strip(), age, 1, "Активна"])

    info = {
        "id": new_id,
        "name": league_name.strip(),
        "age": age,
        "coef": 1.0
    }
    leagues_map[key] = info
    return info

def load_teams_map(ws_teams):
    records = ws_teams.get_all_records()
    teams = {}
    existing_ids = []
    for idx, row in enumerate(records, start=2):
        tid = str(row.get("ID_Команды", "")).strip()
        name = str(row.get("Название", "")).strip()
        if tid:
            existing_ids.append(tid)
        if name:
            teams[name.lower()] = {
                "row": idx,
                "id": tid,
                "name": name,
                "school": str(row.get("Школа", "")).strip(),
                "age": str(row.get("Возраст", "Общий")).strip(),
                "rating": int(row.get("Текущий_Рейтинг") or 1000),
                "games": int(row.get("Игры") or 0),
                "wins": int(row.get("Победы") or 0),
                "draws": int(row.get("Ничьи") or 0),
                "losses": int(row.get("Поражения") or 0),
                "gf": int(row.get("ЗМ") or 0),
                "ga": int(row.get("ПМ") or 0)
            }
    return teams, existing_ids

def process_matches_batch(matches_list):
    """Записывает пачку матчей строго по реляционной структуре 4 листов."""
    sheets = get_sheets()
    ws_leagues = sheets["leagues"]
    ws_teams = sheets["teams"]
    ws_matches = sheets["matches"]
    ws_history = sheets["history"]

    leagues_map, league_ids = load_leagues_map(ws_leagues)
    teams_map, team_ids = load_teams_map(ws_teams)

    existing_match_ids = ws_matches.col_values(1)[1:]
    today_str = datetime.now().strftime("%Y-%m-%d")

    new_match_rows = []
    new_history_rows = []
    report_lines = []

    for m in matches_list:
        league_name = m["league"].strip()
        home_raw = m["home"].strip()
        away_raw = m["away"].strip()
        sh = int(m["sh"])
        sg = int(m["sg"])
        match_date = m.get("date") or today_str
        tour = m.get("tour", "Групповой этап")

        league_info = get_or_create_league(ws_leagues, leagues_map, league_ids, league_name)
        coef = league_info["coef"]

        for t_raw in (home_raw, away_raw):
            key = t_raw.lower()
            if key not in teams_map:
                new_tid = generate_next_id(team_ids, "T", pad=2)
                team_ids.append(new_tid)
                new_row_idx = len(teams_map) + 2
                school_name = extract_school_name(t_raw)
                team_age = extract_age_from_text(t_raw, default=league_info["age"])

                ws_teams.append_row([new_tid, t_raw, school_name, team_age, 1000, 0, 0, 0, 0, 0, 0])
                teams_map[key] = {
                    "row": new_row_idx,
                    "id": new_tid,
                    "name": t_raw,
                    "school": school_name,
                    "age": team_age,
                    "rating": 1000,
                    "games": 0, "wins": 0, "draws": 0, "losses": 0,
                    "gf": 0, "ga": 0
                }

        th = teams_map[home_raw.lower()]
        tg = teams_map[away_raw.lower()]

        old_rh, old_rg = th["rating"], tg["rating"]
        delta_h, delta_g = calculate_elo_delta(old_rh, old_rg, sh, sg, coef)

        th["rating"] += delta_h
        tg["rating"] += delta_g
        th["games"] += 1
        tg["games"] += 1
        th["gf"] += sh
        th["ga"] += sg
        tg["gf"] += sg
        tg["ga"] += sh

        if sh > sg:
            th["wins"] += 1
            tg["losses"] += 1
        elif sh < sg:
            tg["wins"] += 1
            th["losses"] += 1
        else:
            th["draws"] += 1
            tg["draws"] += 1

        match_id = generate_next_id(existing_match_ids, "M", pad=3)
        existing_match_ids.append(match_id)

        new_match_rows.append([
            match_id,
            match_date,
            league_info["id"],
            tour,
            th["id"],
            tg["id"],
            sh,
            sg,
            "Обработан"
        ])

        new_history_rows.append([match_date, th["id"], match_id, th["rating"]])
        new_history_rows.append([match_date, tg["id"], match_id, tg["rating"]])

        sign_h = f"+{delta_h}" if delta_h > 0 else str(delta_h)
        sign_g = f"+{delta_g}" if delta_g > 0 else str(delta_g)
        report_lines.append(
            f"• <b>{th['name']}</b> {sh}:{sg} <b>{tg['name']}</b> ({league_info['name']})\n"
            f"  ELO: {old_rh}→<b>{th['rating']}</b> ({sign_h}) | {old_rg}→<b>{tg['rating']}</b> ({sign_g})"
        )

    if new_match_rows:
        ws_matches.append_rows(new_match_rows)
    if new_history_rows:
        ws_history.append_rows(new_history_rows)

    for t in teams_map.values():
        ws_teams.update(
            range_name=f"E{t['row']}:K{t['row']}",
            values=[[t["rating"], t["games"], t["wins"], t["draws"], t["losses"], t["gf"], t["ga"]]]
        )

    return report_lines

# --- КОМАНДЫ БОТА ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "⚽ <b>Krasnodar Kids Liga — Центр управления рейтингом</b>\n\n"
        "<b>Доступные команды:</b>\n"
        "• <code>/match Лига | Хозяева - Гости | 3:1</code> — внести один матч\n"
        "• <code>/batch</code> + список матчей с новой строки — пакетная загрузка\n"
        "• <code>/template</code> — скачать Excel-шаблон\n"
        "• <code>/scan ссылка</code> — автосбор матчей со стены ВК (текст + фото)\n"
        "• <b>Фото/скриншот</b> со счётом в чат — распознавание матчей с картинки\n"
        "• <code>/inspect ссылка</code> — посмотреть структуру постов в группе ВК\n"
        "• <code>/top</code> — текущий топ-15 команд по рейтингу ELO\n"
        "• <code>/chart Импульс</code> — график прогресса команды (можно часть названия)"
    )
    bot.reply_to(message, text, parse_mode="HTML")

def parse_match_line(line):
    parts = [p.strip() for p in line.split("|")]
    if len(parts) != 3:
        return None
    league, teams_part, score_part = parts
    m_teams = re.split(r"\s+[-–—]\s+", teams_part, maxsplit=1)
    if len(m_teams) != 2:
        return None
    m_score = re.match(r"^(\d{1,2})\s*[:\-]\s*(\d{1,2})$", score_part)
    if not m_score:
        return None
    return {
        "league": league,
        "home": m_teams[0].strip(),
        "away": m_teams[1].strip(),
        "sh": int(m_score.group(1)),
        "sg": int(m_score.group(2))
    }

@bot.message_handler(commands=['match'])
def add_single_match(message):
    raw = message.text.replace("/match", "", 1).strip()
    parsed = parse_match_line(raw)
    if not parsed:
        bot.reply_to(
            message,
            "❌ Неверный формат. Пример:\n<code>/match ДФЛ Атлант (2016) | ФК Импульс (2016) - ДФК Изумруд (2016) | 1:2</code>",
            parse_mode="HTML"
        )
        return
    try:
        report = process_matches_batch([parsed])
        bot.reply_to(message, "✅ <b>Матч записан!</b>\n\n" + "\n".join(report), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка записи: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['batch'])
def add_batch_matches(message):
    lines = message.text.splitlines()[1:]
    parsed_list = []
    errors = []
    for idx, line in enumerate(lines, start=2):
        if not line.strip():
            continue
        item = parse_match_line(line)
        if item:
            parsed_list.append(item)
        else:
            errors.append(f"Строка {idx}: {line}")

    if not parsed_list:
        bot.reply_to(
            message,
            "❌ Не найдено корректных строк. Формат:\n"
            "<code>/batch\nДФЛ Атлант (2016) | Команда А (2016) - Команда Б (2016) | 2:1</code>",
            parse_mode="HTML"
        )
        return

    try:
        report = process_matches_batch(parsed_list)
        msg = f"✅ <b>Обработано матчей: {len(parsed_list)}</b>\n\n" + "\n".join(report[:25])
        if errors:
            msg += "\n\n⚠️ <b>Пропущены строки:</b>\n" + "\n".join(errors[:5])
        bot.reply_to(message, msg, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка пакетной обработки: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['template'])
def send_excel_template(message):
    df = pd.DataFrame([
        {
            "Дата": datetime.now().strftime("%Y-%m-%d"),
            "Лига": "ДФЛ Атлант (2016)",
            "Тур": "Групповой этап",
            "Хозяева": "ФК Импульс (2016)",
            "Гости": "ДФК Изумруд (2016)",
            "Голы_Хозяев": 1,
            "Голы_Гостей": 2
        }
    ])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Матчи")
    buf.seek(0)
    bot.send_document(
        message.chat.id,
        buf,
        visible_file_name="template_matches.xlsx",
        caption="📄 Заполни таблицу и отправь файл обратно в этот чат."
    )

@bot.message_handler(content_types=['document'])
def handle_excel_upload(message):
    if not message.document.file_name.endswith(".xlsx"):
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        df = pd.read_excel(io.BytesIO(downloaded))
        parsed_list = []
        for _, row in df.iterrows():
            if pd.isna(row.get("Хозяева")) or pd.isna(row.get("Гости")):
                continue
            parsed_list.append({
                "date": str(row.get("Дата", datetime.now().strftime("%Y-%m-%d")))[:10],
                "league": str(row.get("Лига", "Общая лига")).strip(),
                "tour": str(row.get("Тур", "Групповой этап")).strip(),
                "home": str(row["Хозяева"]).strip(),
                "away": str(row["Гости"]).strip(),
                "sh": int(row["Голы_Хозяев"]),
                "sg": int(row["Голы_Гостей"])
            })
        if not parsed_list:
            bot.reply_to(message, "❌ В файле не найдено заполненных строк с матчами.")
            return
        report = process_matches_batch(parsed_list)
        bot.reply_to(message, f"✅ <b>Загружено из Excel: {len(parsed_list)} матчей</b>\n\n" + "\n".join(report[:25]), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка чтения Excel: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(content_types=['photo'])
def handle_photo_results(message):
    league_hint = (message.caption or "").strip() or "Турнир"
    bot.reply_to(message, "👁 Читаю результаты матчей с картинки...")
    try:
        photo_obj = message.photo[-1]
        file_info = bot.get_file(photo_obj.file_id)
        img_bytes = bot.download_file(file_info.file_path)

        matches = extract_matches_from_image_bytes(img_bytes, league_hint=league_hint)
        if not matches:
            bot.send_message(message.chat.id, "🤷‍♂️ Не удалось найти сыгранные матчи со счётом на этом изображении.")
            return

        lines = [f"🎯 <b>Распознано матчей: {len(matches)}</b>\n\n<code>/batch"]
        for m in matches:
            lines.append(f"{m['league']} | {m['home']} - {m['away']} | {m['sh']}:{m['sg']}")
        lines.append("</code>\n\n💡 <i>Нажми на блок выше, чтобы скопировать его, проверь названия команд и отправь обратно в чат!</i>")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка распознавания фото: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['top'])
def show_top_teams(message):
    try:
        sheets = get_sheets()
        records = sheets["teams"].get_all_records()
        valid_teams = [r for r in records if str(r.get("Название", "")).strip()]
        sorted_teams = sorted(valid_teams, key=lambda x: int(x.get("Текущий_Рейтинг") or 1000), reverse=True)[:15]
        if not sorted_teams:
            bot.reply_to(message, "База команд пока пуста.")
            return
        lines = ["🏆 <b>ТОП-15 команд (рейтинг ELO):</b>\n"]
        for i, t in enumerate(sorted_teams, start=1):
            lines.append(
                f"{i}. <b>{t['Название']}</b> — <b>{t['Текущий_Рейтинг']}</b> "
                f"(И:{t['Игры']}, В:{t['Победы']}, Н:{t['Ничьи']}, П:{t['Поражения']})"
            )
        bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка получения топа: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['chart'])
def send_team_chart(message):
    team_query = message.text.replace("/chart", "", 1).strip()
    if not team_query:
        bot.reply_to(message, "Укажи название команды (или его часть): <code>/chart Импульс</code>", parse_mode="HTML")
        return
    try:
        sheets = get_sheets()
        raw_teams = sheets["teams"].get_all_records()
        teams_records = [{str(k).strip(): v for k, v in row.items()} for row in raw_teams]

        q_low = team_query.lower()
        exact_match = None
        partial_matches = []

        for t in teams_records:
            name = str(t.get("Название", "")).strip()
            tid = str(t.get("ID_Команды", "")).strip()
            if not name:
                continue
            if name.lower() == q_low or tid.lower() == q_low:
                exact_match = t
                break
            if q_low in name.lower():
                partial_matches.append(t)

        target_team = exact_match
        if not target_team:
            if len(partial_matches) == 1:
                target_team = partial_matches[0]
            elif len(partial_matches) > 1:
                variants = "\n".join(f"• <code>/chart {t['Название']}</code>" for t in partial_matches[:10])
                bot.reply_to(
                    message,
                    f"🔍 Найдено несколько команд по запросу «{team_query}». Выбери точную:\n\n{variants}",
                    parse_mode="HTML"
                )
                return
            else:
                bot.reply_to(message, f"❌ Команда «{team_query}» не найдена в листе «Команды».")
                return

        tid = str(target_team.get("ID_Команды", "")).strip()
        tname = str(target_team.get("Название", "")).strip()

        raw_history = sheets["history"].get_all_records()
        history = [{str(k).strip(): v for k, v in row.items()} for row in raw_history]

        team_rows = [
            r for r in history
            if str(r.get("ID_Команды", "")).strip().lower() == tid.lower()
            or str(r.get("ID_Команды", "")).strip().lower() == tname.lower()
        ]
        if not team_rows:
            bot.reply_to(
                message,
                f"📉 Для команды <b>{tname}</b> ({tid}) пока нет записей в листе «История_Рейтинга».",
                parse_mode="HTML"
            )
            return

        ratings = [1000] + [int(float(str(r["Рейтинг_После"]).replace(",", "."))) for r in team_rows]
        labels = ["Старт"] + [f"И{i}" for i in range(1, len(team_rows) + 1)]

        plt.figure(figsize=(8, 4.5))
        plt.plot(labels, ratings, marker="o", linewidth=2.5, color="#1f77b4")
        plt.title(f"Динамика рейтинга ELO: {tname}", fontsize=13, fontweight="bold")
        plt.ylabel("Рейтинг ELO")
        plt.grid(True, linestyle="--", alpha=0.5)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150)
        plt.close()
        buf.seek(0)
        bot.send_photo(
            message.chat.id,
            buf,
            caption=f"📈 Текущий рейтинг <b>{tname}</b> ({tid}): <b>{ratings[-1]}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка построения графика: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['scan'])
def scan_social_source(message):
    args = message.text.replace("/scan", "", 1).strip()
    if not args:
        bot.reply_to(
            message,
            "🔎 <b>Автосбор результатов из ВК и Telegram</b>\n\n"
            "Отправь ссылку на группу ВК или публичный ТГ-канал:\n"
            "• <code>/scan https://vk.ru/dfl_atlant</code>\n"
            "• <code>/scan https://t.me/имя_канала</code>",
            parse_mode="HTML"
        )
        return

    parts = [p.strip() for p in args.split("|")]
    source_url = parts[0]
    custom_league = parts[1] if len(parts) > 1 else None

    bot.send_message(message.chat.id, f"🕵️‍♂️ Сканирую посты и карточки результатов: <code>{source_url}</code>...", parse_mode="HTML")
    try:
        if "vk.com" in source_url.lower() or "vk.ru" in source_url.lower():
            matches = parse_vk_group(source_url, league_name=custom_league, count=15, scan_photos=True)
        else:
            matches = parse_tg_channel(source_url, league_name=custom_league)

        if not matches:
            bot.send_message(
                message.chat.id,
                "🤷‍♂️ В последних 15 постах не найдено сыгранных матчей со счётом.",
                parse_mode="HTML"
            )
            return

        lines = [f"🎯 <b>Найдено матчей: {len(matches)}</b>\n\n<code>/batch"]
        for m in matches[:35]:
            lines.append(f"{m['league']} | {m['home']} - {m['away']} | {m['sh']}:{m['sg']}")
        lines.append("</code>")

        if len(matches) > 35:
            lines.append(f"\n<i>...и ещё {len(matches) - 35} матчей.</i>")

        lines.append("\n💡 <i>Нажми на блок кода выше, чтобы скопировать его одним тапом, и отправь в чат для записи в базу!</i>")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка сканирования:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['inspect'])
def inspect_social_source(message):
    source_url = message.text.replace("/inspect", "", 1).strip() or "https://vk.ru/dfl_atlant"
    try:
        report = inspect_vk_wall(source_url, count=5)
        bot.send_message(message.chat.id, f"🔬 <b>Разведка стены {source_url}:</b>\n\n{report}", parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(func=lambda message: True)
def fallback_message(message):
    bot.reply_to(message, "Используй /help, чтобы посмотреть список доступных команд.")

if __name__ == "__main__":
    print("Бот Krasnodar Kids Liga запущен...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)