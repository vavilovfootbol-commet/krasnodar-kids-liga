import os
import time
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Krasnodar Kids Liga Master")

app = FastAPI(title="Krasnodar Kids Liga — Аналитический портал")

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# Кэш в памяти на 60 секунд, чтобы портал летал и не превышал квоты Google Sheets API
_CACHE = {
    "timestamp": 0,
    "data": None
}
CACHE_TTL = 60


def fetch_portal_data(force_refresh=False):
    now = time.time()
    if not force_refresh and _CACHE["data"] is not None and (now - _CACHE["timestamp"] < CACHE_TTL):
        return _CACHE["data"]

    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)
    client = gspread.authorize(creds)
    ss = client.open(SPREADSHEET_NAME)

    raw_leagues = ss.worksheet("Лиги").get_all_records()
    raw_teams = ss.worksheet("Команды").get_all_records()
    raw_matches = ss.worksheet("Матчи").get_all_records()
    raw_history = ss.worksheet("История_Рейтинга").get_all_records()

    leagues_by_id = {}
    leagues_list = []
    for r in raw_leagues:
        r = {str(k).strip(): v for k, v in r.items()}
        lid = str(r.get("ID_Лиги", "")).strip()
        name = str(r.get("Название", "")).strip()
        if lid and name:
            item = {
                "id": lid,
                "name": name,
                "age": str(r.get("Возраст", "Общий")).strip(),
                "coef": r.get("Коэффициент_Престижа", 1),
                "status": str(r.get("Статус", "Активна")).strip()
            }
            leagues_by_id[lid] = item
            leagues_list.append(item)

    teams_by_id = {}
    teams_list = []
    ages_set = set()
    for r in raw_teams:
        r = {str(k).strip(): v for k, v in r.items()}
        tid = str(r.get("ID_Команды", "")).strip()
        name = str(r.get("Название", "")).strip()
        if not (tid and name):
            continue
        age = str(r.get("Возраст", "Общий")).strip()
        if age and age != "Общий":
            ages_set.add(age)
        gf = int(r.get("ЗМ") or 0)
        ga = int(r.get("ПМ") or 0)
        team_obj = {
            "id": tid,
            "name": name,
            "school": str(r.get("Школа", "")).strip(),
            "age": age,
            "rating": int(r.get("Текущий_Рейтинг") or 1000),
            "games": int(r.get("Игры") or 0),
            "wins": int(r.get("Победы") or 0),
            "draws": int(r.get("Ничьи") or 0),
            "losses": int(r.get("Поражения") or 0),
            "gf": gf,
            "ga": ga,
            "gd": gf - ga
        }
        teams_by_id[tid] = team_obj
        teams_list.append(team_obj)

    teams_list.sort(key=lambda x: (x["rating"], x["gd"], x["gf"]), reverse=True)

    played_matches = []
    upcoming_matches = []
    for r in raw_matches:
        r = {str(k).strip(): v for k, v in r.items()}
        mid = str(r.get("ID_Матча", "")).strip()
        lid = str(r.get("ID_Лиги", "")).strip()
        hid = str(r.get("ID_Хозяева", "")).strip()
        gid = str(r.get("ID_Гости", "")).strip()
        if not (hid and gid):
            continue

        sh = str(r.get("Счет_Х", "")).strip()
        sg = str(r.get("Счет_Г", "")).strip()
        status = str(r.get("Статус_Обработки", "")).strip().lower()

        m_obj = {
            "id": mid,
            "date": str(r.get("Дата", "")).strip(),
            "league_id": lid,
            "league_name": leagues_by_id.get(lid, {}).get("name", lid),
            "league_age": leagues_by_id.get(lid, {}).get("age", "Общий"),
            "tour": str(r.get("Тур", "")).strip(),
            "home_id": hid,
            "home_name": teams_by_id.get(hid, {}).get("name", hid),
            "away_id": gid,
            "away_name": teams_by_id.get(gid, {}).get("name", gid),
            "sh": sh,
            "sg": sg
        }

        if status == "анонс" or (sh == "" and sg == ""):
            upcoming_matches.append(m_obj)
        else:
            played_matches.append(m_obj)

    played_matches.reverse()
    upcoming_matches.sort(key=lambda x: x["date"])

    # История рейтинга по каждой команде для графиков в карточке
    history_by_team = {tid: [{"date": "Старт", "rating": 1000}] for tid in teams_by_id}
    for r in raw_history:
        r = {str(k).strip(): v for k, v in r.items()}
        tid = str(r.get("ID_Команды", "")).strip()
        if tid in history_by_team:
            try:
                val = int(float(str(r.get("Рейтинг_После", 1000)).replace(",", ".")))
            except ValueError:
                val = 1000
            history_by_team[tid].append({
                "date": str(r.get("Дата", "")).strip(),
                "rating": val
            })

    payload = {
        "updated_at": time.strftime("%H:%M:%S"),
        "stats": {
            "teams_count": len(teams_list),
            "matches_count": len(played_matches),
            "upcoming_count": len(upcoming_matches),
            "leagues_count": len(leagues_list)
        },
        "ages": sorted(list(ages_set)),
        "leagues": leagues_list,
        "teams": teams_list,
        "played_matches": played_matches[:100],
        "upcoming_matches": upcoming_matches[:50],
        "history_by_team": history_by_team
    }

    _CACHE["timestamp"] = now
    _CACHE["data"] = payload
    return payload


@app.get("/")
async def index_page():
    return FileResponse("templates/index.html")


@app.get("/api/data")
async def get_api_data(refresh: bool = False):
    try:
        data = fetch_portal_data(force_refresh=refresh)
        return JSONResponse(content=data)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=True)