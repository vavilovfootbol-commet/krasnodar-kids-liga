import os
import re
import io
import math
import time
import threading
from datetime import datetime
import telebot
import gspread
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from rapidfuzz import process, fuzz
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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

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
    sheets = {
        "leagues": ss.worksheet("Лиги"),
        "teams": ss.worksheet("Команды"),
        "matches": ss.worksheet("Матчи"),
        "history": ss.worksheet("История_Рейтинга")
    }
    try:
        sheets["aliases"] = ss.worksheet("Алиасы_Команд")
    except gspread.WorksheetNotFound:
        ws_a = ss.add_worksheet(title="Алиасы_Команд", rows=500, cols=3)
        ws_a.append_row(["Синоним", "ID_Команды", "Эталонное_Название"])
        sheets["aliases"] = ws_a

    try:
        sheets["sources"] = ss.worksheet("Источники")
    except gspread.WorksheetNotFound:
        ws_s = ss.add_worksheet(title="Источники", rows=200, cols=7)
        ws_s.append_row(["ID_Источника", "Название_Лиги", "Возраст", "Тип", "URL", "Последняя_Проверка", "Статус"])
        sheets["sources"] = ws_s

    return sheets

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ID, ВОЗРАСТА И ОЧИСТКИ ИМЁН ---
def extract_age_from_text(text, default="Общий"):
    m = re.search(r"\b(201[0-9]|202[0-2])\b", str(text))
    return m.group(1) if m else default

def clean_raw_team_name(raw_name):
    name = str(raw_name).strip()
    name = re.sub(r'[«»"\'“”]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def ensure_team_age_suffix(team_name, league_age):
    cleaned = clean_raw_team_name(team_name)
    if extract_age_from_text(cleaned, default=None) is None and league_age != "Общий":
        return f"{cleaned} ({league_age})"
    return cleaned

def extract_school_name(team_name):
    return re.sub(r"\s*\(201[0-9]|202[0-2]\)\s*$", "", team_name).strip()

def normalize_for_fuzzy(name):
    s = extract_school_name(name).lower()
    s = re.sub(r"\b(фк|дфк|дфш|фш|сш|сшор|мбу|афк)\b", "", s)
    s = re.sub(r"[^a-zа-яё0-9\s\-]", "", s)
    return re.sub(r"\s+", " ", s).strip()

def generate_next_id(existing_ids, prefix, pad=2):
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
        row = {str(k).strip(): v for k, v in row.items()}
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

def load_teams_and_aliases(ws_teams, ws_aliases):
    raw_teams = ws_teams.get_all_records()
    teams_by_key = {}
    teams_by_id = {}
    existing_ids = []

    for idx, row in enumerate(raw_teams, start=2):
        row = {str(k).strip(): v for k, v in row.items()}
        tid = str(row.get("ID_Команды", "")).strip()
        name = str(row.get("Название", "")).strip()
        if tid:
            existing_ids.append(tid)
        if name:
            obj = {
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
            teams_by_key[name.lower()] = obj
            teams_by_id[tid] = obj

    raw_aliases = ws_aliases.get_all_records()
    aliases_map = {}
    for row in raw_aliases:
        row = {str(k).strip(): v for k, v in row.items()}
        syn = str(row.get("Синоним", "")).strip().lower()
        tid = str(row.get("ID_Команды", "")).strip()
        if syn and tid in teams_by_id:
            aliases_map[syn] = tid

    return teams_by_key, teams_by_id, existing_ids, aliases_map

def extract_squad_number(name):
    """Вытаскивает номер состава (-1, -2, 2, 3), если он есть, чтобы не склеивать первый и второй составы."""
    clean = extract_school_name(name).strip()
    m = re.search(r"(?:[-–—\s]|№)(\d)$", clean)
    return m.group(1) if m else ""

def find_team_readonly(raw_name, league_age, teams_by_key, teams_by_id, aliases_map):
    cleaned_raw = clean_raw_team_name(raw_name)
    with_age = ensure_team_age_suffix(cleaned_raw, league_age)
    target_age = extract_age_from_text(with_age, default=league_age)
    target_squad = extract_squad_number(with_age)

    for candidate in (with_age.lower(), cleaned_raw.lower()):
        if candidate in teams_by_key:
            return teams_by_key[candidate]
    for candidate in (with_age.lower(), cleaned_raw.lower()):
        if candidate in aliases_map:
            return teams_by_id[aliases_map[candidate]]

    same_age_teams = [
        t for t in teams_by_id.values()
        if (t["age"] == target_age or target_age == "Общий" or t["age"] == "Общий")
        and extract_squad_number(t["name"]) == target_squad
    ]
    if same_age_teams:
        query_norm = normalize_for_fuzzy(with_age)
        choices = {t["id"]: normalize_for_fuzzy(t["name"]) for t in same_age_teams if normalize_for_fuzzy(t["name"])}
        if query_norm and choices:
            best = process.extractOne(query_norm, choices, scorer=fuzz.WRatio)
            if best and best[1] >= 84.0:
                return teams_by_id[best[2]]
    return None

def resolve_or_create_team(raw_name, league_age, ws_teams, ws_aliases, teams_by_key, teams_by_id, existing_ids, aliases_map, new_alias_rows, merge_notes):
    cleaned_raw = clean_raw_team_name(raw_name)
    with_age = ensure_team_age_suffix(cleaned_raw, league_age)
    target_age = extract_age_from_text(with_age, default=league_age)
    target_squad = extract_squad_number(with_age)

    for candidate in (with_age.lower(), cleaned_raw.lower()):
        if candidate in teams_by_key:
            return teams_by_key[candidate]

    for candidate in (with_age.lower(), cleaned_raw.lower()):
        if candidate in aliases_map:
            tid = aliases_map[candidate]
            return teams_by_id[tid]

    same_age_teams = [
        t for t in teams_by_id.values()
        if (t["age"] == target_age or target_age == "Общий" or t["age"] == "Общий")
        and extract_squad_number(t["name"]) == target_squad
    ]
    if same_age_teams:
        query_norm = normalize_for_fuzzy(with_age)
        choices = {t["id"]: normalize_for_fuzzy(t["name"]) for t in same_age_teams if normalize_for_fuzzy(t["name"])}
        if query_norm and choices:
            best = process.extractOne(query_norm, choices, scorer=fuzz.WRatio)
            if best and best[1] >= 84.0:
                matched_tid = best[2]
                matched_team = teams_by_id[matched_tid]
                aliases_map[cleaned_raw.lower()] = matched_tid
                aliases_map[with_age.lower()] = matched_tid
                new_alias_rows.append([cleaned_raw, matched_tid, matched_team["name"]])
                merge_notes.append(f"🔗 Склейка: «{cleaned_raw}» → <b>{matched_team['name']}</b> ({int(best[1])}%)")
                return matched_team

    new_tid = generate_next_id(existing_ids, "T", pad=2)
    existing_ids.append(new_tid)
    new_row_idx = len(teams_by_id) + 2
    school_name = extract_school_name(with_age)

    ws_teams.append_row([new_tid, with_age, school_name, target_age, 1000, 0, 0, 0, 0, 0, 0])
    new_obj = {
        "row": new_row_idx,
        "id": new_tid,
        "name": with_age,
        "school": school_name,
        "age": target_age,
        "rating": 1000,
        "games": 0, "wins": 0, "draws": 0, "losses": 0,
        "gf": 0, "ga": 0
    }
    teams_by_key[with_age.lower()] = new_obj
    teams_by_id[new_tid] = new_obj
    return new_obj

def load_matches_state(ws_matches):
    records = ws_matches.get_all_records()
    played_fingerprints = set()
    open_announcements = {}
    existing_ids = []

    for idx, row in enumerate(records, start=2):
        row = {str(k).strip(): v for k, v in row.items()}
        mid = str(row.get("ID_Матча", "")).strip()
        if mid:
            existing_ids.append(mid)
        lid = str(row.get("ID_Лиги", "")).strip()
        h_id = str(row.get("ID_Хозяева", "")).strip()
        g_id = str(row.get("ID_Гости", "")).strip()
        sh = str(row.get("Счет_Х", "")).strip()
        sg = str(row.get("Счет_Г", "")).strip()
        status = str(row.get("Статус_Обработки", "")).strip().lower()

        if not (lid and h_id and g_id):
            continue

        if status == "анонс" or (sh == "" and sg == ""):
            pair_key = f"{lid}|{'|'.join(sorted([h_id, g_id]))}"
            open_announcements[pair_key] = {
                "row": idx,
                "mid": mid,
                "home_id": h_id,
                "away_id": g_id
            }
        elif sh != "" and sg != "":
            fp = f"{lid}|{h_id}|{g_id}|{sh}:{sg}"
            played_fingerprints.add(fp)

    return played_fingerprints, open_announcements, existing_ids

def process_matches_batch(matches_list):
    sheets = get_sheets()
    ws_leagues = sheets["leagues"]
    ws_teams = sheets["teams"]
    ws_matches = sheets["matches"]
    ws_history = sheets["history"]
    ws_aliases = sheets["aliases"]

    leagues_map, league_ids = load_leagues_map(ws_leagues)
    teams_by_key, teams_by_id, team_ids, aliases_map = load_teams_and_aliases(ws_teams, ws_aliases)
    played_fingerprints, open_announcements, existing_match_ids = load_matches_state(ws_matches)

    today_str = datetime.now().strftime("%Y-%m-%d")

    new_match_rows = []
    new_history_rows = []
    new_alias_rows = []
    report_lines = []
    merge_notes = []
    skipped_dups = []
    processed_count = 0

    for m in matches_list:
        league_name = m["league"].strip()
        is_announcement = bool(m.get("is_announcement", False))
        match_date = m.get("date") or today_str
        tour = m.get("tour", "Групповой этап")

        league_info = get_or_create_league(ws_leagues, leagues_map, league_ids, league_name)
        coef = league_info["coef"]

        th = resolve_or_create_team(
            m["home"], league_info["age"], ws_teams, ws_aliases,
            teams_by_key, teams_by_id, team_ids, aliases_map, new_alias_rows, merge_notes
        )
        tg = resolve_or_create_team(
            m["away"], league_info["age"], ws_teams, ws_aliases,
            teams_by_key, teams_by_id, team_ids, aliases_map, new_alias_rows, merge_notes
        )

        pair_key = f"{league_info['id']}|{'|'.join(sorted([th['id'], tg['id']]))}"

        if is_announcement:
            if pair_key in open_announcements:
                skipped_dups.append(f"⏭ Дубль анонса: {th['name']} vs {tg['name']} ({match_date})")
                continue

            match_id = generate_next_id(existing_match_ids, "M", pad=3)
            existing_match_ids.append(match_id)

            new_match_rows.append([
                match_id,
                match_date,
                league_info["id"],
                tour,
                th["id"],
                tg["id"],
                "",
                "",
                "Анонс"
            ])
            open_announcements[pair_key] = {
                "row": None,
                "mid": match_id,
                "home_id": th["id"],
                "away_id": tg["id"]
            }
            processed_count += 1
            report_lines.append(
                f"📅 <b>Анонс:</b> {th['name']} vs {tg['name']} ({league_info['name']}, {match_date})"
            )
            continue

        sh = int(m["sh"])
        sg = int(m["sg"])

        fp = f"{league_info['id']}|{th['id']}|{tg['id']}|{sh}:{sg}"
        if fp in played_fingerprints:
            skipped_dups.append(f"⏭ Дубль: {th['name']} {sh}:{sg} {tg['name']}")
            continue
        played_fingerprints.add(fp)

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

        closed_announcement = False
        if pair_key in open_announcements and open_announcements[pair_key]["row"] is not None:
            ann = open_announcements.pop(pair_key)
            match_id = ann["mid"]
            if ann["home_id"] == th["id"]:
                cell_sh, cell_sg = sh, sg
            else:
                cell_sh, cell_sg = sg, sh

            ws_matches.update(
                range_name=f"B{ann['row']}:B{ann['row']}",
                values=[[match_date]]
            )
            ws_matches.update(
                range_name=f"G{ann['row']}:I{ann['row']}",
                values=[[cell_sh, cell_sg, "Обработан"]]
            )
            closed_announcement = True
        else:
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
        processed_count += 1

        sign_h = f"+{delta_h}" if delta_h > 0 else str(delta_h)
        sign_g = f"+{delta_g}" if delta_g > 0 else str(delta_g)
        tag = " ✅ <i>(закрыт анонс)</i>" if closed_announcement else ""
        report_lines.append(
            f"• <b>{th['name']}</b> {sh}:{sg} <b>{tg['name']}</b> ({league_info['name']}){tag}\n"
            f"  ELO: {old_rh}→<b>{th['rating']}</b> ({sign_h}) | {old_rg}→<b>{tg['rating']}</b> ({sign_g})"
        )

    if new_match_rows:
        ws_matches.append_rows(new_match_rows)
    if new_history_rows:
        ws_history.append_rows(new_history_rows)
    if new_alias_rows:
        ws_aliases.append_rows(new_alias_rows)

    for t in teams_by_id.values():
        ws_teams.update(
            range_name=f"E{t['row']}:K{t['row']}",
            values=[[t["rating"], t["games"], t["wins"], t["draws"], t["losses"], t["gf"], t["ga"]]]
        )

    full_report = report_lines[:]
    if merge_notes:
        full_report.append("\n<b>Авто-склейка названий:</b>\n" + "\n".join(dict.fromkeys(merge_notes)))
    if skipped_dups:
        full_report.append("\n<b>Антидубль (пропущено):</b>\n" + "\n".join(skipped_dups))

    return full_report, processed_count

# --- ОБХОД РЕЕСТРА ИСТОЧНИКОВ (/sync И ФОНОВЫЙ МОНИТОРИНГ) ---
def run_sources_sync():
    """Обходит все активные источники из листа 'Источники' и возвращает только новые недублирующиеся матчи."""
    sheets = get_sheets()
    ws_sources = sheets["sources"]
    raw_sources = ws_sources.get_all_records()

    leagues_map, _ = load_leagues_map(sheets["leagues"])
    teams_by_key, teams_by_id, _, aliases_map = load_teams_and_aliases(sheets["teams"], sheets["aliases"])
    played_fingerprints, _, _ = load_matches_state(sheets["matches"])

    fresh_matches = []
    checked_count = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    for idx, row in enumerate(raw_sources, start=2):
        row = {str(k).strip(): v for k, v in row.items()}
        status = str(row.get("Статус", "")).strip().lower()
        url = str(row.get("URL", "")).strip()
        league_name = str(row.get("Название_Лиги", "")).strip()

        if status != "активен" or not url:
            continue

        checked_count += 1
        try:
            if "vk.com" in url.lower() or "vk.ru" in url.lower():
                found = parse_vk_group(url, league_name=league_name or None, count=10, scan_photos=True)
            else:
                found = parse_tg_channel(url, league_name=league_name or None)
        except Exception as e:
            print(f"[SYNC] Ошибка при обходе {url}: {e}")
            continue

        ws_sources.update(range_name=f"F{idx}:F{idx}", values=[[now_str]])

        league_info = leagues_map.get(league_name.lower())
        lid = league_info["id"] if league_info else None
        lage = league_info["age"] if league_info else extract_age_from_text(league_name, default="Общий")

        for m in found:
            th = find_team_readonly(m["home"], lage, teams_by_key, teams_by_id, aliases_map)
            tg = find_team_readonly(m["away"], lage, teams_by_key, teams_by_id, aliases_map)
            if lid and th and tg:
                fp = f"{lid}|{th['id']}|{tg['id']}|{m['sh']}:{m['sg']}"
                if fp in played_fingerprints:
                    continue
                played_fingerprints.add(fp)

            m_copy = dict(m)
            if league_name:
                m_copy["league"] = league_name
            if th:
                m_copy["home"] = th["name"]
            else:
                m_copy["home"] = ensure_team_age_suffix(m["home"], lage)
            if tg:
                m_copy["away"] = tg["name"]
            else:
                m_copy["away"] = ensure_team_age_suffix(m["away"], lage)

            fresh_matches.append(m_copy)

    return fresh_matches, checked_count

def background_monitor_worker():
    """Каждые 60 минут проверяет активные источники и присылает новые матчи админу."""
    time.sleep(30)
    while True:
        try:
            if ADMIN_CHAT_ID:
                fresh, checked = run_sources_sync()
                if fresh:
                    lines = [f"🔔 <b>Авто-мониторинг лиг ({checked} истомн.): найдено новых матчей: {len(fresh)}</b>\n\n<code>/batch"]
                    for m in fresh[:35]:
                        lines.append(f"{m['league']} | {m['home']} - {m['away']} | {m['sh']}:{m['sg']}")
                    lines.append("</code>\n\n💡 <i>Скопируй и отправь в чат, чтобы записать в базу!</i>")
                    bot.send_message(int(ADMIN_CHAT_ID), "\n".join(lines), parse_mode="HTML")
        except Exception as e:
            print(f"[MONITOR] Ошибка фонового цикла: {e}")
        time.sleep(3600)

# --- КОМАНДЫ БОТА ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "⚽ <b>Krasnodar Kids Liga — Центр управления рейтингом</b>\n\n"
        "<b>Ввод матчей и расписания:</b>\n"
        "• <code>/match Лига | Хозяева - Гости | 3:1</code> — внести сыгранный матч\n"
        "• <code>/match Лига | Хозяева - Гости | vs | 2026-11-22</code> — добавить анонс\n"
        "• <code>/batch</code> + список матчей/анонсов — пакетная загрузка\n"
        "• <code>/calendar</code> — расписание ближайших несыгранных матчей\n"
        "• <code>/template</code> — скачать Excel-шаблон\n\n"
        "<b>Мониторинг лиг и парсинг:</b>\n"
        "• <code>/add_source Лига | ссылка_ВК_или_ТГ</code> — добавить лигу в авто-мониторинг\n"
        "• <code>/sources</code> — список отслеживаемых лиг и источников\n"
        "• <code>/sync</code> — проверить все активные лиги и собрать свежие матчи\n"
        "• <code>/scan ссылка</code> — разово просканировать группу ВК или ТГ-канал\n"
        "• <b>Фото/скриншот</b> со счётом в чат — распознавание с картинки\n\n"
        "<b>Аналитика и база:</b>\n"
        "• <code>/alias Синоним | Эталонная команда</code> — привязать сокращение\n"
        "• <code>/top</code> — текущий топ-15 команд по рейтингу ELO\n"
        "• <code>/chart Импульс</code> — график прогресса команды"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['add_source'])
def add_source_command(message):
    """Добавляет источник в лист 'Источники': /add_source ДФЛ Атлант (2016) | https://vk.ru/dfl_atlant"""
    raw = message.text.replace("/add_source", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        bot.reply_to(
            message,
            "❌ Формат: <code>/add_source Название Лиги | https://vk.ru/...</code>\n"
            "Пример: <code>/add_source ДФЛ Атлант (2016) | https://vk.ru/dfl_atlant</code>",
            parse_mode="HTML"
        )
        return

    league_name, url = parts
    src_type = "VK" if ("vk.com" in url.lower() or "vk.ru" in url.lower()) else "TG"
    age = extract_age_from_text(league_name, default="Общий")

    try:
        sheets = get_sheets()
        ws_sources = sheets["sources"]
        existing_ids = ws_sources.col_values(1)[1:]
        new_id = generate_next_id(existing_ids, "S", pad=2)
        ws_sources.append_row([new_id, league_name, age, src_type, url, "Никогда", "Активен"])
        bot.reply_to(
            message,
            f"✅ Источник добавлен в реестр (<b>{new_id}</b>):\n"
            f"• Лига: <b>{league_name}</b> (возраст: {age})\n"
            f"• Тип: {src_type} | URL: <code>{url}</code>\n\n"
            f"Запусти <code>/sync</code>, чтобы собрать свежие результаты по всем лигам!",
            parse_mode="HTML"
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка добавления источника: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['sources'])
def list_sources_command(message):
    try:
        sheets = get_sheets()
        records = sheets["sources"].get_all_records()
        if not records:
            bot.reply_to(
                message,
                "📡 Реестр источников пока пуст.\nДобавь первую лигу: <code>/add_source ДФЛ Атлант (2016) | https://vk.ru/dfl_atlant</code>",
                parse_mode="HTML"
            )
            return

        lines = [f"📡 <b>Отслеживаемые источники ({len(records)}):</b>\n"]
        for r in records:
            r = {str(k).strip(): v for k, v in r.items()}
            sid = r.get("ID_Источника", "")
            lname = r.get("Название_Лиги", "")
            stype = r.get("Тип", "")
            url = r.get("URL", "")
            status = r.get("Статус", "")
            last_chk = r.get("Последняя_Проверка", "")
            icon = "🟢" if str(status).strip().lower() == "активен" else "⏸"
            lines.append(f"{icon} <b>{sid}</b> | <b>{lname}</b> [{stype}]\n   <code>{url}</code> (проверено: {last_chk})")

        bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка чтения источников: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['sync'])
def sync_all_sources_command(message):
    bot.send_message(message.chat.id, "🔄 Запускаю обход всех активных лиг и источников...")
    try:
        fresh, checked = run_sources_sync()
        if checked == 0:
            bot.send_message(
                message.chat.id,
                "⚠️ В листе «Источники» нет активных записей. Добавь через <code>/add_source Лига | ссылка</code>.",
                parse_mode="HTML"
            )
            return

        if not fresh:
            bot.send_message(
                message.chat.id,
                f"✅ Проверено источников: <b>{checked}</b>. Новых матчей (которых ещё нет в базе) не обнаружено.",
                parse_mode="HTML"
            )
            return

        lines = [f"🎯 <b>Проверено источников: {checked} | Найдено новых матчей: {len(fresh)}</b>\n\n<code>/batch"]
        for m in fresh[:35]:
            lines.append(f"{m['league']} | {m['home']} - {m['away']} | {m['sh']}:{m['sg']}")
        lines.append("</code>")

        if len(fresh) > 35:
            lines.append(f"\n<i>...и ещё {len(fresh) - 35} матчей.</i>")

        lines.append("\n💡 <i>Нажми на блок кода выше, чтобы скопировать и отправить в чат для записи!</i>")
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка синхронизации: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['calendar', 'schedule'])
def show_calendar(message):
    try:
        sheets = get_sheets()
        raw_matches = sheets["matches"].get_all_records()
        raw_teams = sheets["teams"].get_all_records()
        raw_leagues = sheets["leagues"].get_all_records()

        teams_names = {
            str(r.get("ID_Команды", "")).strip(): str(r.get("Название", "")).strip()
            for r in raw_teams
        }
        leagues_names = {
            str(r.get("ID_Лиги", "")).strip(): str(r.get("Название", "")).strip()
            for r in raw_leagues
        }

        upcoming = []
        for row in raw_matches:
            row = {str(k).strip(): v for k, v in row.items()}
            status = str(row.get("Статус_Обработки", "")).strip().lower()
            sh = str(row.get("Счет_Х", "")).strip()
            sg = str(row.get("Счет_Г", "")).strip()
            if status == "анонс" or (sh == "" and sg == "" and row.get("ID_Хозяева")):
                upcoming.append({
                    "date": str(row.get("Дата", "Без даты")).strip(),
                    "league": leagues_names.get(str(row.get("ID_Лиги", "")).strip(), "Лига"),
                    "home": teams_names.get(str(row.get("ID_Хозяева", "")).strip(), str(row.get("ID_Хозяева", ""))),
                    "away": teams_names.get(str(row.get("ID_Гости", "")).strip(), str(row.get("ID_Гости", "")))
                })

        if not upcoming:
            bot.reply_to(message, "📅 Открытых анонсов матчей пока нет. Добавить можно через <code>/match Лига | Команда А - Команда Б | vs | 2026-11-22</code>", parse_mode="HTML")
            return

        upcoming.sort(key=lambda x: x["date"])
        lines = [f"📅 <b>Расписание ближайших матчей ({len(upcoming)}):</b>\n"]
        for item in upcoming[:30]:
            lines.append(f"• <b>{item['date']}</b> | {item['league']}\n  {item['home']} — {item['away']}")

        bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка загрузки календаря: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['alias'])
def add_manual_alias(message):
    raw = message.text.replace("/alias", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        bot.reply_to(
            message,
            "❌ Формат: <code>/alias Кривое название | Эталонная команда</code>\n"
            "Пример: <code>/alias DINSKA AI | DINSKAYA (2016)</code>",
            parse_mode="HTML"
        )
        return
    synonym, canonical = parts
    try:
        sheets = get_sheets()
        teams_by_key, _, _, _ = load_teams_and_aliases(sheets["teams"], sheets["aliases"])
        target = teams_by_key.get(canonical.lower())
        if not target:
            bot.reply_to(
                message,
                f"❌ Эталонная команда «{canonical}» не найдена в листе «Команды». Проверь точное название через /top.",
                parse_mode="HTML"
            )
            return
        sheets["aliases"].append_row([synonym, target["id"], target["name"]])
        bot.reply_to(
            message,
            f"✅ Синоним сохранён: «{synonym}» → <b>{target['name']}</b> ({target['id']})",
            parse_mode="HTML"
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка сохранения синонима: <code>{e}</code>", parse_mode="HTML")

def parse_match_line(line):
    parts = [p.strip() for p in line.split("|")]
    if len(parts) not in (3, 4):
        return None
    league = parts[0]
    teams_part = parts[1]
    score_part = parts[2].strip().lower()
    custom_date = parts[3].strip() if len(parts) == 4 and parts[3].strip() else None

    m_teams = re.split(r"\s+[-–—]\s+", teams_part, maxsplit=1)
    if len(m_teams) != 2:
        return None

    if score_part in ("vs", "-:-", "?:?", "анонс", "-", "—"):
        return {
            "league": league,
            "home": m_teams[0].strip(),
            "away": m_teams[1].strip(),
            "is_announcement": True,
            "date": custom_date
        }

    m_score = re.match(r"^(\d{1,2})\s*[:\-]\s*(\d{1,2})$", score_part)
    if not m_score:
        return None
    return {
        "league": league,
        "home": m_teams[0].strip(),
        "away": m_teams[1].strip(),
        "sh": int(m_score.group(1)),
        "sg": int(m_score.group(2)),
        "is_announcement": False,
        "date": custom_date
    }

@bot.message_handler(commands=['match'])
def add_single_match(message):
    raw = message.text.replace("/match", "", 1).strip()
    parsed = parse_match_line(raw)
    if not parsed:
        bot.reply_to(
            message,
            "❌ Неверный формат. Примеры:\n"
            "• Матч: <code>/match ДФЛ Атлант (2016) | ФК Импульс (2016) - ДФК Изумруд (2016) | 1:2</code>\n"
            "• Анонс: <code>/match ДФЛ Атлант (2016) | ФК Импульс (2016) - ДФК Изумруд (2016) | vs | 2026-11-22</code>",
            parse_mode="HTML"
        )
        return
    try:
        report, added_count = process_matches_batch([parsed])
        header = "✅ <b>Запись выполнена!</b>" if added_count else "ℹ️ <b>Новых записей не добавлено</b>"
        bot.reply_to(message, f"{header}\n\n" + "\n".join(report), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка записи: <code>{e}</code>", parse_mode="HTML")

@bot.message_handler(commands=['batch'])
def add_batch_matches(message):
    raw_body = re.sub(r"^/batch(?:@\w+)?\s*", "", message.text.strip(), flags=re.IGNORECASE)
    if "\n" not in raw_body and raw_body.count("|") >= 4:
        raw_body = re.sub(r"\s+(\d{1,2}\s*[:\-]\s*\d{1,2}|vs|анонс)\s+", r" \1\n", raw_body, flags=re.IGNORECASE)

    lines = raw_body.splitlines()
    parsed_list = []
    errors = []
    for idx, line in enumerate(lines, start=1):
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
        report, added_count = process_matches_batch(parsed_list)
        msg = f"✅ <b>Обработано записей: {added_count} из {len(parsed_list)}</b>\n\n" + "\n".join(report[:30])
        if errors:
            msg += "\n\n⚠️ <b>Пропущены строки с ошибкой формата:</b>\n" + "\n".join(errors[:5])
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
        caption="📄 Заполни таблицу (для анонсов оставь голы пустыми) и отправь файл обратно в этот чат."
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
            has_score = not pd.isna(row.get("Голы_Хозяев")) and not pd.isna(row.get("Голы_Гостей"))
            item = {
                "date": str(row.get("Дата", datetime.now().strftime("%Y-%m-%d")))[:10],
                "league": str(row.get("Лига", "Общая лига")).strip(),
                "tour": str(row.get("Тур", "Групповой этап")).strip(),
                "home": str(row["Хозяева"]).strip(),
                "away": str(row["Гости"]).strip(),
                "is_announcement": not has_score
            }
            if has_score:
                item["sh"] = int(row["Голы_Хозяев"])
                item["sg"] = int(row["Голы_Гостей"])
            parsed_list.append(item)
        if not parsed_list:
            bot.reply_to(message, "❌ В файле не найдено заполненных строк с матчами.")
            return
        report, added_count = process_matches_batch(parsed_list)
        bot.reply_to(
            message,
            f"✅ <b>Загружено из Excel: {added_count} из {len(parsed_list)} записей</b>\n\n" + "\n".join(report[:30]),
            parse_mode="HTML"
        )
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
    t_mon = threading.Thread(target=background_monitor_worker, daemon=True)
    t_mon.start()
    bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)