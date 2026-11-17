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

VK_TOKEN = (os.getenv("VK_TOKEN") or os.getenv("VK_SERVICE_TOKEN") or "").strip()
VK_API_VER = "5.199"

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
    matches = []
    if not text:
        return matches

    for raw_line in text.splitlines():
        line = re.sub(r"^[⚽🏆📅🔹▪•\-*\d.)\s]+", "", raw_line).strip()
        if len(line) < 7 or len(line) > 130:
            continue

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


def _normalize_vk_url(url):
    u = url.strip()
    if not u.startswith("http"):
        u = "https://" + u
    return u.replace("vk.ru/", "vk.com/")


def _extract_vk_api_photos(post_obj):
    urls = []
    for att in post_obj.get("attachments", []):
        if att.get("type") == "photo":
            sizes = att.get("photo", {}).get("sizes", [])
            if sizes:
                best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                if best.get("url"):
                    urls.append(best["url"])
    return urls


def _fetch_vk_posts_via_api(url, count=15):
    clean_url = _normalize_vk_url(url)
    group_title = None
    target_post_id = None

    params = {"count": count, "filter": "owner", "access_token": VK_TOKEN, "v": VK_API_VER}

    # Если передана ссылка на конкретный пост (wall-227039869_3002), берём owner_id из неё и читаем стену через wall.get
    wall_match = re.search(r"wall(-?\d+)_(\d+)", clean_url)
    if wall_match:
        params["owner_id"] = int(wall_match.group(1))
        target_post_id = int(wall_match.group(2))
        params["count"] = 50
    else:
        slug_match = re.search(r"vk\.com/([^/?#]+)", clean_url)
        if not slug_match:
            return [], None
        slug = slug_match.group(1)
        if slug.startswith("club") or slug.startswith("public"):
            num = re.sub(r"\D", "", slug)
            params["owner_id"] = -int(num)
        else:
            params["domain"] = slug

    resp = requests.get("https://api.vk.com/method/wall.get", params=params, timeout=15).json()
    if "error" in resp:
        raise RuntimeError(f"VK API Error: {resp['error'].get('error_msg')}")

    items = resp.get("response", {}).get("items", [])

    if target_post_id is not None:
        matched = [p for p in items if p.get("id") == target_post_id]
        if matched:
            items = matched

    if items and items[0].get("owner_id", 0) < 0:
        gid = abs(items[0]["owner_id"])
        g_resp = requests.get(
            "https://api.vk.com/method/groups.getById",
            params={"group_id": gid, "access_token": VK_TOKEN, "v": VK_API_VER},
            timeout=10
        ).json()
        g_data = g_resp.get("response", {})
        groups = g_data.get("groups", []) if isinstance(g_data, dict) else g_data
        if groups:
            group_title = groups[0].get("name")

    parsed_posts = []
    for p in items:
        parsed_posts.append({
            "text": p.get("text", ""),
            "images": _extract_vk_api_photos(p)
        })
    return parsed_posts, group_title


def parse_vk_group(url, league_name=None, count=15, scan_photos=True):
    if not VK_TOKEN:
        raise ValueError("Не задан VK_TOKEN в файле .env")

    posts, detected_title = _fetch_vk_posts_via_api(url, count=count)
    default_league = league_name or detected_title or "Лига ВК"

    all_matches = []
    seen_keys = set()

    for idx, post in enumerate(posts, start=1):
        post_text = post.get("text", "")
        img_urls = post.get("images", [])

        text_matches = extract_matches_from_text(post_text, league_name=default_league)
        for m in text_matches:
            key = f"{m['home'].lower()}|{m['away'].lower()}|{m['sh']}:{m['sg']}"
            if key not in seen_keys:
                seen_keys.add(key)
                all_matches.append(m)

        if scan_photos and img_urls:
            low_text = post_text.lower()
            looks_like_results = (
                len(posts) == 1
                or any(kw in low_text for kw in RESULT_KEYWORDS)
                or bool(re.search(r"\b\d{1,2}\s*:\s*\d{1,2}\b", low_text))
                or len(text_matches) == 0
            )
            if looks_like_results:
                print(f"[VK] Пост #{idx}: скачиваю фото ({len(img_urls)} шт.) для Vision...")
                for img_u in img_urls[:5]:
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
    if not VK_TOKEN:
        return "⚠️ Не задан VK_TOKEN в .env"
    posts, title = _fetch_vk_posts_via_api(url, count=count)
    lines = [f"Группа: <b>{title or url}</b> | Найдено постов: <b>{len(posts)}</b>"]
    for i, post in enumerate(posts, start=1):
        txt = post.get("text", "").replace("\n", " ")[:90]
        imgs = post.get("images", [])
        lines.append(f"{i}. Фото: {len(imgs)} шт. | «{txt}...»")
    return "\n".join(lines)
