import os
import telebot
from dotenv import load_dotenv
from tg_bot import run_sources_sync

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()


def main():
    print("[AutoSync] апуск проверки всех активных лиг...")
    report = run_sources_sync()
    print("[AutoSync] езультат:\n", report)

    if BOT_TOKEN and ADMIN_CHAT_ID and report:
        try:
            bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
            bot.send_message(ADMIN_CHAT_ID, f"🤖 <b>вто-мониторинг лиг (GitHub Actions):</b>\n\n{report}")
            print("[AutoSync] тчёт отправлен в Telegram.")
        except Exception as e:
            print(f"[AutoSync] е удалось отправить отчёт в Telegram: {e}")


if __name__ == "__main__":
    main()
