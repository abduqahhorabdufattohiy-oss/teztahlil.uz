import logging
import os
import time
import pytz
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone, time as dt_time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from finvizfinance.quote import finvizfinance

# 1. Muhit o‘zgaruvchilarini yuklash
load_dotenv()

# 2. Render uchun Port tinglovchi (Bepul rejimda o‘chib qolmasligi uchun)
def run_http_server():
    class HealthCheckHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive and running!")

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"Port {port} band qilindi. Render tekshiruvi tayyor.")
    server.serve_forever()

# 3. Logging sozlamalari
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

UZB_TZ = pytz.timezone('Asia/Tashkent')
USER_FILE = "users.txt"

SECTOR_MAP = {
    "Technology": "Texnologiya",
    "Financial": "Moliya",
    "Healthcare": "Sog‘liqni saqlash",
    "Consumer Cyclical": "Iste’mol tovarlari (siklik)",
    "Consumer Defensive": "Iste’mol tovarlari (himoyalangan)",
    "Energy": "Energetika",
    "Communication Services": "Aloqa xizmatlari",
    "Industrials": "Sanoat",
    "Basic Materials": "Xomashyo",
    "Real Estate": "Ko‘chmas mulk",
    "Utilities": "Kommunal xizmatlar"
}

# 4. Foydalanuvchilarni saqlash
def save_user(user_id):
    if not os.path.exists(USER_FILE):
        with open(USER_FILE, "w") as f: pass
    
    with open(USER_FILE, "r+") as f:
        users = f.read().splitlines()
        if str(user_id) not in users:
            f.write(f"{user_id}\n")

# 5. Iqtisodiy taqvim funksiyasi
async def send_economic_calendar(context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(USER_FILE):
        return

    with open(USER_FILE, "r") as f:
        user_ids = f.read().splitlines()

    today_date = datetime.now(UZB_TZ).strftime('%d.%m.%Y')
    
    text = (
        f"AQSh IQTISODIY TAQVIMI | {today_date}\n\n"
        f"bugun kutilayotgan muhim voqealar (UZB vaqti bilan):\n\n"
        f"17:30 — YaIM (GDP) o‘sishi\n"
        f"18:30 — Inflyatsiya darajasi (CPI)\n"
        f"19:00 — Ishsizlik nafaqasi arizalari"
    )
    
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Investing.com", url="https://www.investing.com/economic-calendar/"),
            InlineKeyboardButton("TradingView", url="https://www.tradingview.com/economic-calendar/")
        ]
    ])
    
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Xabar yuborishda xato ({user_id}): {e}")

# 6. Analiz funksiyalari
def clean_value(val):
    if val in ['-', 'N/A', None]: 
        return "0"
    return str(val).replace(',', '').replace('%', '')

def get_full_analysis(f):
    try:
        price = f.get('Price', '0')
        change = f.get('Change', '0')
        raw_sector = f.get('Sector', 'N/A')
        uzb_sector = SECTOR_MAP.get(raw_sector, raw_sector)
        formatted_sector = f"<b>{raw_sector.upper()}</b> ({uzb_sector.upper()})"

        mcap = f.get('Market Cap', 'N/A')
        pe = f.get('P/E', 'N/A')
        div = f.get('Dividend %', 'N/A')
        eps = f.get('EPS (ttm)', 'N/A')
        
        debt_eq_raw = clean_value(f.get('Debt/Eq', '0'))
        try:
            debt_eq = float(debt_eq_raw)
        except ValueError:
            debt_eq = 0.0

        industry = f.get('Industry', '')
        non_compliant_industries = ['Banks', 'Insurance', 'Gambling', 'Tobacco']
        
        if any(x in industry for x in non_compliant_industries):
            shariah = "NOJOIZ"
        elif debt_eq > 0.33:
            shariah = "SHUBHALI"
        else:
            shariah = "JOIZ"

        rsi = clean_value(f.get('RSI (14)', '0'))
        sma200 = clean_value(f.get('SMA200', '0'))
        sma50 = clean_value(f.get('SMA50', '0'))
        sma20 = clean_value(f.get('SMA20', '0'))

        analysis = (
            f"<b>FUNDAMENTAL</b>\n"
            f"<b>M.CAP:</b> {mcap} | <b>P/E:</b> {pe}\n"
            f"<b>DIVIDEND:</b> {div} | <b>EPS:</b> {eps}\n\n"
            f"<b>TECHNICAL</b>\n"
            f"<b>RSI:</b> {rsi} | <b>SMA200:</b> {sma200}\n"
            f"<b>SMA50:</b> {sma50} | <b>SMA20:</b> {sma20}\n"
            f"—\n"
            f"<b>SHARI’AT STATUSI:</b> {shariah}"
        )
        return analysis, formatted_sector, price, change
    except Exception as e:
        logger.error(f"Error in analysis: {e}")
        return "Tahlil jarayonida xatolik.", "N/A", "0", "0%"

# 7. Bot buyruqlari
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user.id)
    await update.message.reply_text("marhamat! $ticker yuborishingiz mumkin)")

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    save_user(update.effective_user.id)

    text = update.message.text.strip()
    if not text.startswith('$'):
        return

    ticker = text[1:].upper()
    if not ticker.isalnum():
        return

    status_msg = await update.message.reply_text(f"Qidirilmoqda: {ticker}...")

    try:
        stock = finvizfinance(ticker)
        fundament = stock.ticker_fundament()

        if not fundament:
            await status_msg.edit_text("ma’lumot topilmadi.")
            return

        now = datetime.now(timezone.utc) + timedelta(hours=5)
        dt_str = now.strftime('%d.%m.%Y | %H:%M')

        chart_url = f"https://charts2.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d&rev={int(time.time())}"
        analysis_text, sector_info, price, change = get_full_analysis(fundament)

        caption = (
            f"<b>SANA:</b> {dt_str} (UZB)\n\n"
            f"<b>TICKER:</b> ${ticker} | <b>PRICE:</b> {price} ({change})\n"
            f"<b>SECTOR:</b> {sector_info}\n"
            f"—\n"
            f"{analysis_text}"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("FINVIZ", url=f"https://finviz.com/quote.ashx?t={ticker}"),
            InlineKeyboardButton("ISLAMICLY", url="https://www.islamicly.com/home/stocks")
        ]])

        try:
            await update.message.reply_photo(photo=chart_url, caption=caption, parse_mode='HTML', reply_markup=kb)
        except Exception:
            await update.message.reply_text(caption, parse_mode='HTML', reply_markup=kb)

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Request error ({ticker}): {e}")
        await status_msg.edit_text("ma’lumot topilmadi yoki xatolik yuz berdi.")

# 8. Main funksiyasi
def main():
    # Port tinglovchini alohida oqimda ishga tushirish
    threading.Thread(target=run_http_server, daemon=True).start()

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN topilmadi!")
        return

    app = Application.builder().token(token).build()
    
    # Har kuni soat 09:00 da taqvim yuborish
    job_queue = app.job_queue
    job_queue.run_daily(
        send_economic_calendar, 
        time=dt_time(hour=9, minute=0, second=0, tzinfo=UZB_TZ)
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^\$'), handle_request))

    logger.info("Bot polling rejimida ishga tushdi...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
