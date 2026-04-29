import logging
import os
import time
import pytz
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time as dt_time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from finvizfinance.quote import finvizfinance

# 1. PROFESSIONAL LOGGING
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# 2. RENDER HEALTH CHECK SERVER
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is operational")

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP Server error: {e}")

# 3. GLOBAL KONSTANTALAR
UZB_TZ = pytz.timezone('Asia/Tashkent')
USER_FILE = "users.txt"

SECTOR_MAP = {
    "Technology": "Texnologiya", "Financial": "Moliya", "Healthcare": "Sog‘liqni saqlash",
    "Consumer Cyclical": "Iste’mol tovarlari", "Consumer Defensive": "Iste’mol tovarlari (himoya)",
    "Energy": "Energetika", "Communication Services": "Aloqa", "Industrials": "Sanoat",
    "Basic Materials": "Xomashyo", "Real Estate": "Ko‘chmas mulk", "Utilities": "Kommunal"
}

# 4. YORDAMCHI FUNKSIYALAR
def save_user(user_id):
    u_id = str(user_id)
    if not os.path.exists(USER_FILE): open(USER_FILE, "w").close()
    with open(USER_FILE, "r") as f: users = f.read().splitlines()
    if u_id not in users:
        with open(USER_FILE, "a") as f: f.write(f"{u_id}\n")

def clean_val(val):
    if val in ['-', 'N/A', None]: return "N/A"
    return str(val).replace(',', '')

# 6. TICKER TAHLILI (NAMUNADAGI STRUKTURA)
def perform_analysis(f):
    try:
        raw_debt = clean_val(f.get('Debt/Eq', '0'))
        try: debt_eq = float(raw_debt)
        except: debt_eq = 0.0
            
        industry = f.get('Industry', '')
        if any(x in industry for x in ['Banks', 'Insurance', 'Gambling', 'Tobacco']):
            shariah = "NOJOIZ"
        elif debt_eq > 0.33:
            shariah = "SHUBHALI"
        else:
            shariah = "JOIZ"

        # NAMUNADAGI KABI BO‘SH QATORLAR BILAN SHAKLLANTIRISH
        analysis = (
            f"FUNDAMENTAL (VALUATION & DEBT)\n\n"
            f"<b>M.CAP:</b> {f.get('Market Cap', 'N/A')} | <b>P/E:</b> {f.get('P/E', 'N/A')} | <b>Fwd P/E:</b> {f.get('Forward P/E', 'N/A')}\n"
            f"<b>P/B:</b> {f.get('P/B', 'N/A')} | <b>P/S:</b> {f.get('P/S', 'N/A')} | <b>Debt/Eq:</b> {raw_debt}\n"
            f"<b>DIVIDEND:</b> {f.get('Dividend %', 'N/A')} | <b>EPS (ttm):</b> {f.get('EPS (ttm)', 'N/A')}\n\n\n"
            
            f"TECHNICAL (TREND & MOMENTUM)\n\n"
            f"<b>RSI (14):</b> {clean_val(f.get('RSI (14)', 'N/A'))} | <b>ATR:</b> {f.get('ATR', 'N/A')}\n"
            f"<b>SMA20:</b> {f.get('SMA20', 'N/A')} | <b>SMA50:</b> {f.get('SMA50', 'N/A')} | <b>SMA200:</b> {f.get('SMA200', 'N/A')}\n"
            f"<b>52W Range:</b> {f.get('52W Range', 'N/A')}\n"
            f"—\n<b>SHARI’AT STATUSI: {shariah}</b>"
        )
        raw_sector = f.get('Sector', 'N/A')
        uzb_sector = SECTOR_MAP.get(raw_sector, raw_sector)
        return analysis, f"<b>{raw_sector.upper()}</b> ({uzb_sector.upper()})", f.get('Price', '0'), f.get('Change', '0')
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return "Tahlil xatosi", "N/A", "0", "0%"

# 7. TELEGRAM HANDLERS
async def handle_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    save_user(update.effective_user.id)
    
    text = update.message.text.strip()
    if not text.startswith('$'): return
    
    # URL MUAMMOSINI HAL QILISH: Ticker ichidagi barcha bo‘shliqlarni butunlay o‘chirish
    ticker_input = "".join(text[1:].split()).upper()
    
    progress = await update.message.reply_text(f"QIDIRILMOQDA: {ticker_input}...")
    
    try:
        stock = finvizfinance(ticker_input)
        fundament = stock.ticker_fundament()
        if not fundament:
            await progress.edit_text("Ticker topilmadi.")
            return

        now = datetime.now(UZB_TZ)
        analysis_text, sector_info, price, change = perform_analysis(fundament)
        
        # SANA va VAQT oralig‘ida bo'sh qator tashlash
        caption = (
            f"<b>SANA:</b> {now.strftime('%d.%m.%Y')} | <b>VAQT:</b> {now.strftime('%H:%M')} (UZB)\n\n"
            f"<b>TICKER:</b> ${ticker_input} | <b>PRICE:</b> {price} ({change})\n"
            f"<b>SECTOR:</b> {sector_info}\n"
            f"—\n{analysis_text}"
        )

        # TOZA URL: ticker_input o‘zgaruvchisida bo‘shliq yo‘qligi sababli ? belgisi chiqmaydi
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("FINVIZ", url=f"https://finviz.com/quote.ashx?t={ticker_input}"),
            InlineKeyboardButton("ISLAMICLY", url="https://www.islamicly.com/")
        ]])

        chart_url = f"https://charts2.finviz.com/chart.ashx?t={ticker_input}&ty=c&ta=1&p=d&rev={int(time.time())}"
        
        try:
            await update.message.reply_photo(photo=chart_url, caption=caption, parse_mode='HTML', reply_markup=kb)
        except:
            await update.message.reply_text(caption, parse_mode='HTML', reply_markup=kb)
        
        await progress.delete()
    except Exception as e:
        logger.error(f"Request error: {e}")
        await progress.edit_text("$ticker noto‘g‘ri yoki tahlilda uzulish")

# 8. MAIN ENTRY POINT
def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    token = os.getenv("BOT_TOKEN")
    app = Application.builder().token(token).build()
    
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("marhamat! $ticker yuboring")))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^\$'), handle_ticker))
    
    logger.info("Bot polling rejimida ishga tushirildi...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
