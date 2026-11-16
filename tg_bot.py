import os
import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE = os.path.join(BASE_DIR, "credentials.json")
SPREADSHEET_NAME = "Krasnodar Kids Liga Master"

def get_google_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    return gspread.authorize(creds)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "⚽ <b>Krasnodar Kids Liga — Личный ассистент</b>\n\n"
        "База данных готова к работе. Доступные команды:\n"
        "• /stats — проверить состояние базы данных\n"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['stats'])
def send_stats(message):
    bot.send_message(message.chat.id, "⏳ Проверяю базу...")
    try:
        client = get_google_client()
        sheet = client.open(SPREADSHEET_NAME)
        worksheets = sheet.worksheets()
        
        report = [f"📊 <b>База «{SPREADSHEET_NAME}»</b>\n"]
        for ws in worksheets:
            rows = ws.get_all_values()
            non_empty = [r for r in rows if any(cell.strip() for cell in r)]
            count = max(0, len(non_empty) - 1)
            report.append(f"• <b>{ws.title}</b>: {count} записей")
            
        bot.send_message(message.chat.id, "\n".join(report), parse_mode="HTML")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    bot.reply_to(message, "Я на связи! Используй /stats для проверки базы.")

if __name__ == "__main__":
    print("🤖 Бот запущен...")
    bot.infinity_polling()