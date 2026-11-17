import os
import re
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
VK_TOKEN = os.getenv("VK_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def detect_age_group(text, fallback="ДФЛ Атлант"):
    """Определяет год рождения / категорию турнира из текста поста."""
    m = re.search(r"\b(201[0-9]|202[0-2])\s*(?:г\.?\s*р\.?|год)?", text, re.IGNORECASE)
    if m:
        return f"ДФЛ Атлант ({m.group(1)})"
    return fallback

def extract_matches_from_text(text, default_league=None):
    """Ищет в тексте строки с результатами матчей вида 'Команда 1 - Команда 2 3:1'."""
    matches = []
    current_league = default_league or detect_age_group(text)

    for line in text.splitlines():
        clean_line = re.sub(r"\s+", " ", line).strip()
        line_age = re.search(r"\b(201[0-9]|202[0-2])\s*(?:г\.?\s*р\.?|год)", clean_line, re.IGNORECASE)
        if line_age and not default_league:
            current_league = f"ДФЛ Атлант ({line_age.group(1)})"

        m = re.search(
            r"([А-Яа-яA-Za-z0-9«»\"'\.\(\)\-\s]{2,38}?)\s+[-–—]\s+([А-Яа-яA-Za-z0-9«»\"'\.\(\)\-\s]{2,38}?)\s+(\d{1,2})\s*[:\-]\s*(\d{1,2})(?!\d)",
            clean_line
        )
        if m:
            home = m.group(1).strip(" .,-–—•✅⚽🏆")
            away = m.group(2).strip(" .,-–—•✅⚽🏆")
            if len(home) >= 2 and len(away) >= 2 and not home.isdigit() and not away.isdigit():
                matches.append({
                    "league": current_league,
                    "home": home,
                    "away": away,
                    "sh": int(m.group(3)),
                    "sg": int(m.group(4))
                })
    return matches

def extract_matches_from_image_bytes(image_bytes, mime_type="image/jpeg", league_hint="Турнир"):
    """Отправляет картинку в Gemini Vision и извлекает сыгранные матчи со счётом."""
    if not GEMINI_API_KEY:
        raise ValueError("Не задан GEMINI_API_KEY в файле .env")

    client = genai.Client(api_key=GEMINI_API_KEY, http_options={"timeout": 20000})
    prompt = (
        f"На этой картинке результаты детских футбольных матчей (контекст/лига: {league_hint}).\n"
        "Извлеки ВСЕ сыгранные матчи, где указан конкретный счёт (например, 3:1 или 2-0).\n"
        "Игнорируй расписание будущих игр без счёта и итоговые турнирные таблицы без счёта конкретных матчей.\n"
        "Выведи строго по одному матчу на строку в формате:\n"
        "Название Лиги | Команда Хозяев - Команда Гостей | ГолыХозяев:ГолыГостей\n"
        "Без нумерации, без маркдауна, без лишних пояснений. Если на картинке нет счетов матчей, напиши: НЕТ_МАТЧЕЙ"
    )

    models_to_try = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    raw_text = ""
    last_error = None

    for model_name in models_to_try:
        try:
            print(f"[Vision] Отправляю фото в {model_name} ({league_hint})...")
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt
                ]
            )
            raw_text = (response.text or "").strip()
            print(f"[Vision] Ответ получен от {model_name}.")
            last_error = None
            break
        except Exception as e:
            print(f"[Vision] Ошибка на {model_name}: {e}")
            last_error = e
            time.sleep(1.5)

    if last_error is not None:
        raise last_error

    if "НЕТ_МАТЧЕЙ" in raw_text:
        return []

    found = []
    for line in raw_text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3:
            league, teams_part, score_part = parts
            m_teams = re.split(r"\s+[-–—]\s+", teams_part, maxsplit=1)
            m_score = re.match(r"^(\d{1,2})\s*[:\-]\s*(\d{1,2})$", score_part)
            if len(m_teams) == 2 and m_score:
                found.append({
                    "league": league,
                    "home": m_teams[0].strip(),
                    "away": m_teams[1].strip(),
                    "sh": int(m_score.group(1)),
                    "sg": int(m_score.group(2))
                })
    return found

def parse_vk_group(domain_or_url, league_name=None, count=15, scan_photos=True):
    """Забирает посты со стены ВК: ищет счёт в тексте и читает фото только из постов с результатами."""
    if not VK_TOKEN:
        raise ValueError("Не задан VK_TOKEN в файле .env")

    clean_url = domain_or_url.split("?")[0].rstrip("/")
    domain = clean_url.split("/")[-1]

    url = "https://api.vk.com/method/wall.get"
    params = {
        "domain": domain,
        "count": count,
        "access_token": VK_TOKEN,
        "v": "5.199"
    }
    resp = requests.get(url, params=params, timeout=10).json()
    if "error" in resp:
        raise RuntimeError(f"Ошибка VK API: {resp['error'].get('error_msg')}")

    items = resp.get("response", {}).get("items", [])
    found_matches = []

    for idx, post in enumerate(items, start=1):
        text = post.get("text", "")
        text_low = text.lower()
        post_league = league_name or detect_age_group(text, fallback=f"ВК: {domain}")

        # 1. Сначала ищем счёт в тексте поста
        text_matches = extract_matches_from_text(text, default_league=post_league)
        found_matches.extend(text_matches)

        # 2. Если в тексте счёта нет, проверяем фото ТОЛЬКО в постах про результаты/итоги (и пропускаем прямые трансляции)
        is_result_post = ("результат" in text_low or "итог" in text_low) and ("трансляци" not in text_low)
        if not text_matches and scan_photos and GEMINI_API_KEY and is_result_post:
            print(f"[VK] Пост #{idx} подходит под результаты, скачиваю фото...")
            for att in post.get("attachments", []):
                if att.get("type") == "photo":
                    sizes = att["photo"].get("sizes", [])
                    if sizes:
                        best_photo = max(sizes, key=lambda s: s.get("width", 0))
                        img_url = best_photo.get("url")
                        if img_url:
                            img_bytes = requests.get(img_url, timeout=10).content
                            found_matches.extend(
                                extract_matches_from_image_bytes(img_bytes, league_hint=post_league)
                            )

    return found_matches

def parse_tg_channel(channel_or_url, league_name=None):
    """Читает последние посты из публичного Telegram-канала через веб-версию t.me/s/."""
    username = channel_or_url.split("?")[0].rstrip("/").split("/")[-1].replace("@", "")
    url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.find_all("div", class_="tgme_widget_message_text")
    found_matches = []

    for msg in messages[-20:]:
        text = msg.get_text(separator="\n")
        found_matches.extend(extract_matches_from_text(text, default_league=league_name))

    return found_matches

def inspect_vk_wall(domain_or_url, count=5):
    """Показывает структуру последних постов группы ВК."""
    if not VK_TOKEN:
        raise ValueError("Не задан VK_TOKEN в файле .env")

    clean_url = domain_or_url.split("?")[0].rstrip("/")
    domain = clean_url.split("/")[-1]

    url = "https://api.vk.com/method/wall.get"
    params = {
        "domain": domain,
        "count": count,
        "access_token": VK_TOKEN,
        "v": "5.199"
    }
    resp = requests.get(url, params=params, timeout=10).json()
    if "error" in resp:
        raise RuntimeError(f"Ошибка VK API: {resp['error'].get('error_msg')}")

    items = resp.get("response", {}).get("items", [])
    summary = []
    for i, post in enumerate(items, start=1):
        text = post.get("text", "").strip()
        short_text = (text[:220] + "...") if len(text) > 220 else (text or "[Без текста]")
        attachments = post.get("attachments", [])
        att_info = []
        for att in attachments:
            atype = att.get("type")
            if atype == "link":
                att_info.append(f"🔗 Ссылка: {att['link'].get('url')}")
            elif atype == "photo":
                att_info.append("🖼 Фото")
            else:
                att_info.append(f"📎 {atype}")
        att_str = ", ".join(att_info) if att_info else "нет вложений"
        summary.append(f"<b>Пост #{i}</b> ({att_str}):\n<code>{short_text}</code>")

    return "\n\n".join(summary)