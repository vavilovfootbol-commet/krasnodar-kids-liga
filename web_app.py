import os
import time
from fastapi import FastAPI
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
        teams_by_id[tid] = {
            "id": tid,
            "name": name,
            "school": str(r.get("Школа", "")).strip(),
            "age": age,
            "rating": 1000,
            "games": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "gf": 0,
            "ga": 0,
            "gd": 0
        }

    # Строим историю рейтинга и берём финальный рейтинг из последней точки
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
            teams_by_id[tid]["rating"] = val

    # Пересчитываем игры, В/Н/П и мячи напрямую из реальных сыгранных матчей
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

        sh_raw = str(r.get("Счет_Х", "")).strip()
        sg_raw = str(r.get("Счет_Г", "")).strip()
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
            "sh": sh_raw,
            "sg": sg_raw
        }

        if status == "анонс" or (sh_raw == "" and sg_raw == ""):
            upcoming_matches.append(m_obj)
        else:
            played_matches.append(m_obj)
            try:
                sh_int, sg_int = int(sh_raw), int(sg_raw)
                if hid in teams_by_id:
                    teams_by_id[hid]["games"] += 1
                    teams_by_id[hid]["gf"] += sh_int
                    teams_by_id[hid]["ga"] += sg_int
                    if sh_int > sg_int:
                        teams_by_id[hid]["wins"] += 1
                    elif sh_int == sg_int:
                        teams_by_id[hid]["draws"] += 1
                    else:
                        teams_by_id[hid]["losses"] += 1
                if gid in teams_by_id:
                    teams_by_id[gid]["games"] += 1
                    teams_by_id[gid]["gf"] += sg_int
                    teams_by_id[gid]["ga"] += sh_int
                    if sg_int > sh_int:
                        teams_by_id[gid]["wins"] += 1
                    elif sg_int == sh_int:
                        teams_by_id[gid]["draws"] += 1
                    else:
                        teams_by_id[gid]["losses"] += 1
            except ValueError:
                pass

    teams_list = list(teams_by_id.values())
    for t in teams_list:
        t["gd"] = t["gf"] - t["ga"]

    teams_list.sort(key=lambda x: (x["rating"], x["gd"], x["gf"]), reverse=True)

    played_matches.reverse()
    upcoming_matches.sort(key=lambda x: x["date"])

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
