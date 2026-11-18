import os
import telebot
from dotenv import load_dotenv
from tg_bot import run_sources_sync

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()


def main():
    print("[AutoSync] апуск проверки всех активных лиг...")
    result = run_sources_sync()

    # run_sources_sync() возвращает (список_новых_матчей, кол-во_проверенных_источников)
    if isinstance(result, tuple) and len(result) == 2:
        new_matches, checked_count = result
    else:
        new_matches, checked_count = [], 0

    print(f"[AutoSync] роверено источников: {checked_count}, найдено новых матчей: {len(new_matches)}")

    if not new_matches:
        print("[AutoSync] овых матчей нет, уведомление в Telegram не требуется.")
        return

    lines = [
        f"🤖 <b>вто-мониторинг лиг (GitHub Actions)</b>",
        f"роверено источников: <b>{checked_count}</b> | обавлено матчей: <b>{len(new_matches)}</b>\n"
    ]
    for m in new_matches[:25]:
        if isinstance(m, dict):
            lines.append(f"⚽ {m.get('league', '')}: <b>{m.get('home')} — {m.get('away')}</b> ({m.get('sh')}:{m.get('sg')})")
        else:
            lines.append(f"⚽ {m}")

    if len(new_matches) > 25:
        lines.append(f"\n...и ещё {len(new_matches) - 25} матчей.")

    report_text = "\n".join(lines)

    if BOT_TOKEN and ADMIN_CHAT_ID:
        try:
            bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
            bot.send_message(ADMIN_CHAT_ID, report_text)
            print("[AutoSync] тчёт отправлен в Telegram.")
        except Exception as e:
            print(f"[AutoSync] шибка отправки в Telegram: {e}")


if __name__ == "__main__":
    main()
