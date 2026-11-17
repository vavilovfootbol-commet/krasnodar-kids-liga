import os
import re
import io
import time
import requests
from bs4 import BeautifulSoup
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
}

RESULT_KEYWORDS = (
    "результат", "итог", "сыгран", "тур", "счет", "счёт",
    "побед", "матч", "игр", "таблиц", "обзор", "финал"
)


def _compress_image_for_vision(img_bytes, max_side=1600):
    """Уменьшает вес картинки перед отправкой в API, чтобы избежать 504 и ReadTimeout."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / float(max(w, h))
            img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return img_bytes, "image/jpeg"


def extract_matches_from_image_bytes(img_bytes, league_hint="Турнир"):
    """Распознаёт сыгранные матчи со счётом с карточки-изображения через Gemini Vision."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Не найден GEMINI_API_KEY в файле .env")

    compressed_bytes, mime_type = _compress_image_for_vision(img_bytes)

    prompt = (
        f"Ты — спортивный парсер результатов детского футбола. Турнир: «{league_hint}».\n"
        "Найди на изображении ВСЕ сыгранные матчи, у которых указан итоговый счёт (два числа через двоеточие или тире).\n"
        "Игнорируй анонсы без счёта, турнирные таблицы (очки/места) и списки бомбардиров.\n"
        "Внимательно сохраняй номера составов команд (-1, -2, -3, 2 и т.д.), если они указаны на карточке!\n"
        "Верни результат СТРОГО в формате строк (по одной на матч, без нумерации и лишнего текста):\n"
        "Лига | Хозяева - Гости | СчетХ:СчетГ\n"
        f"Если название лиги/возраста на картинке не указано, пиши «{league_hint}»."
    )

    models_chain = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    raw_text = ""

    for model_name in models_chain:
        try:
            print(f"[Vision] Отправляю фото в {model_name} ({league_hint})...")
            client = genai.Client(api_key=api_key, http_options={"timeout": 45})
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=compressed_bytes, mime_type=mime_type),
                    prompt
                ]
            )
            raw_text = (response.text or "").strip()
            if raw_text:
                break
        except Exception as e:
            print(f"[Vision] Ошибка на {model_name}: {e}")
            time.sleep(2)

    if not raw_text:
        return []

    matches = []
    for line in raw_text.splitlines():
        line = line.strip().lstrip("•-*0123456789. ")
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            continue
        m_teams = re.split(r"\s+[-–—]\s+", parts[1], maxsplit=1)
        m_score = re.match(r"^(\d{1,2})\s*[:\-]\s*(\d{1,2})$", parts[2])
        if len(m_teams) == 2 and m_score:
            matches.append({
                "league": parts[0] or league_hint,
                "home": m_teams[0].strip(),
                "away": m_teams[1].strip(),
                "sh": int(m_score.group(1)),
                "sg": int(m_score.group(2))
            })
    return matches


def extract_matches_from_text(text, league_name="Турнир"):
    """Извлекает матчи со счётом из обычного текста поста."""
    matches = []
    if not text:
        return matches

    for raw_line in text.splitlines():
        line = re.sub(r"^[⚽🏆📅🔹▪️•\-*\d.)\s]+", "", raw_line).strip()
        if len(line) < 7 or len(line) > 130:
            continue

        # Вариант 1: "Команда А - Команда Б 3:1" или "Команда А — Команда Б (3:1)"
        m = re.match(
            r"^([А-ЯЁA-Z0-9«»\"'\s.\-()]{2,45}?)\s+[-–—vsVS]+\s+([А-ЯЁA-Z0-9«»\"'\s.\-()]{2,45}?)\s*[\(\[]?\s*(\d{1,2})\s*[:\-]\s*(\d{1,2})\s*[\)\]]?$",
            line
        )
        if m:
            home, away = m.group(1).strip(), m.group(2).strip()
            if home and away and home.lower() != away.lower():
                matches.append({
                    "league": league_name,
                    "home": home,
                    "away": away,
                    "sh": int(m.group(3)),
                    "sg": int(m.group(4))
                })
                continue

        # Вариант 2: "Команда А 3:1 Команда Б"
        m2 = re.match(
            r"^([А-ЯЁA-Z0-9«»\"'\s.\-()]{2,45}?)\s+(\d{1,2})\s*[:\-]\s*(\d{1,2})\s+([А-ЯЁA-Z0-9«»\"'\s.\-()]{2,45})$",
            line
        )
        if m2:
            home, away = m2.group(1).strip(), m2.group(4).strip()
            if home and away and home.lower() != away.lower():
                matches.append({
                    "league": league_name,
                    "home": home,
                    "away": away,
                    "sh": int(m2.group(2)),
                    "sg": int(m2.group(3))
                })

    return matches


def _normalize_vk_mobile_url(url):
    u = url.strip()
    if not u.startswith("http"):
        u = "https://" + u
    u = u.replace("vk.com/", "m.vk.com/").replace("vk.ru/", "m.vk.com/")
    return u


def _extract_vk_post_images(post_tag):
    """Достаёт прямые ссылки на прикреплённые изображения из поста m.vk.com."""
    urls = []
    for el in post_tag.find_all(["a", "div", "img"]):
        for attr in ("data-src_big", "data-src", "src", "style"):
            val = el.get(attr)
            if not val:
                continue
            found = re.findall(r"https?://[^\s\"'()]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s\"'()]*)?", str(val), flags=re.IGNORECASE)
            for f_url in found:
                clean_u = f_url.split("|")[0].replace("&amp;", "&")
                if any(skip in clean_u for skip in ("emoji", "sticker", "camera_", "deactivated", "icon")):
                    continue
                if clean_u not in urls:
                    urls.append(clean_u)
    return urls


def parse_vk_group(url, league_name=None, count=15, scan_photos=True):
    """Сканирует стену группы ВК (тексты постов + прикреплённые карточки результатов)."""
    mobile_url = _normalize_vk_mobile_url(url)
    resp = requests.get(mobile_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    page_title = soup.title.get_text(strip=True) if soup.title else "Лига ВК"
    default_league = league_name or re.sub(r"\s*\|.*$", "", page_title).strip() or "Лига ВК"

    posts = soup.find_all("div", class_=re.compile(r"wall_item|wi_body"))
    all_matches = []
    seen_keys = set()

    for idx, post in enumerate(posts[:count], start=1):
        text_el = post.find("div", class_=re.compile(r"pi_text|wi_explain"))
        post_text = text_el.get_text("\n", strip=True) if text_el else post.get_text("\n", strip=True)

        text_matches = extract_matches_from_text(post_text, league_name=default_league)
        for m in text_matches:
            key = f"{m['home'].lower()}|{m['away'].lower()}|{m['sh']}:{m['sg']}"
            if key not in seen_keys:
                seen_keys.add(key)
                all_matches.append(m)

        if scan_photos:
            img_urls = _extract_vk_post_images(post)
            low_text = post_text.lower()
            looks_like_results = any(kw in low_text for kw in RESULT_KEYWORDS) or bool(re.search(r"\b\d{1,2}\s*:\s*\d{1,2}\b", low_text))
            if img_urls and (looks_like_results or len(text_matches) == 0):
                print(f"[VK] Пост #{idx} подходит под результаты, скачиваю фото ({len(img_urls)} шт.)...")
                for img_u in img_urls[:3]:
                    try:
                        img_resp = requests.get(img_u, headers=HEADERS, timeout=15)
                        img_resp.raise_for_status()
                        if len(img_resp.content) < 12000:
                            continue
                        vis_matches = extract_matches_from_image_bytes(img_resp.content, league_hint=default_league)
                        for m in vis_matches:
                            key = f"{m['home'].lower()}|{m['away'].lower()}|{m['sh']}:{m['sg']}"
                            if key not in seen_keys:
                                seen_keys.add(key)
                                all_matches.append(m)
                    except Exception as e:
                        print(f"[VK] Пропуск фото в посте #{idx}: {e}")
                        continue

    return all_matches


def parse_tg_channel(url, league_name=None):
    """Сканирует публичный Telegram-канал через веб-превью t.me/s/..."""
    u = url.strip()
    if not u.startswith("http"):
        u = "https://" + u
    if "t.me/" in u and "/s/" not in u:
        u = u.replace("t.me/", "t.me/s/")

    resp = requests.get(u, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    title_el = soup.find("div", class_="tgme_channel_info_header_title")
    default_league = league_name or (title_el.get_text(strip=True) if title_el else "Лига TG")

    messages = soup.find_all("div", class_="tgme_widget_message_wrap")
    all_matches = []
    seen_keys = set()

    for idx, msg in enumerate(messages[-15:], start=1):
        text_el = msg.find("div", class_="tgme_widget_message_text")
        post_text = text_el.get_text("\n", strip=True) if text_el else ""

        for m in extract_matches_from_text(post_text, league_name=default_league):
            key = f"{m['home'].lower()}|{m['away'].lower()}|{m['sh']}:{m['sg']}"
            if key not in seen_keys:
                seen_keys.add(key)
                all_matches.append(m)

        # Проверяем фото в посте ТГ, если текст похож на сводку результатов
        low_text = post_text.lower()
        if any(kw in low_text for kw in RESULT_KEYWORDS):
            for photo_a in msg.find_all("a", class_="tgme_widget_message_photo_wrap")[:2]:
                style = photo_a.get("style", "")
                m_bg = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", style)
                if m_bg:
                    try:
                        img_resp = requests.get(m_bg.group(1), headers=HEADERS, timeout=15)
                        img_resp.raise_for_status()
                        for m in extract_matches_from_image_bytes(img_resp.content, league_hint=default_league):
                            key = f"{m['home'].lower()}|{m['away'].lower()}|{m['sh']}:{m['sg']}"
                            if key not in seen_keys:
                                seen_keys.add(key)
                                all_matches.append(m)
                    except Exception as e:
                        print(f"[TG] Пропуск фото #{idx}: {e}")

    return all_matches


def inspect_vk_wall(url, count=5):
    """Диагностика первых постов стены ВК для проверки видимости текста и картинок."""
    mobile_url = _normalize_vk_mobile_url(url)
    resp = requests.get(mobile_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    posts = soup.find_all("div", class_=re.compile(r"wall_item|wi_body"))

    lines = [f"Найдено блоков постов: <b>{len(posts)}</b>"]
    for i, post in enumerate(posts[:count], start=1):
        text_el = post.find("div", class_=re.compile(r"pi_text|wi_explain"))
        txt = (text_el.get_text(" ", strip=True) if text_el else post.get_text(" ", strip=True))[:90]
        imgs = _extract_vk_post_images(post)
        lines.append(f"{i}. Фото: {len(imgs)} шт. | «{txt}...»")
    return "\n".join(lines)