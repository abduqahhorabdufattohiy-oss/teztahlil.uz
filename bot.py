import logging
import os
import time
import pytz
import threading
import sys
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time as dt_time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from finvizfinance.quote import finvizfinance

# 1. LOGGING
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# 2. HEALTH CHECK SERVER
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is operational")
    def log_message(self, format, *args): return

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP Server error: {e}")

# 3. KONSTANTALAR
UZB_TZ = pytz.timezone('Asia/Tashkent')
USER_FILE = "users.txt"

SECTOR_MAP = {
    "Technology": "TEXNOLOGIYA", "Financial": "MOLIYA", "Healthcare": "SOG‘LIQNI SAQLASH",
    "Consumer Cyclical": "ISTE’MOL TOVARLARI", "Consumer Defensive": "ISTE’MOL TOVARLARI (HIMOYA)",
    "Energy": "ENERGETIKA", "Communication Services": "ALOQA", "Industrials": "SANOAT",
    "Basic Materials": "XOMASHYO", "Real Estate": "KO‘CHMAS MULK", "Utilities": "KOMMUNAL"
}

# 4. YORDAMCHI FUNKSIYALAR
def save_user(user_id):
    try:
        u_id = str(user_id)
        if not os.path.exists(USER_FILE): open(USER_FILE, "w").close()
        with open(USER_FILE, "r") as f: users = f.read().splitlines()
        if u_id not in users:
            with open(USER_FILE, "a") as f: f.write(f"{u_id}\n")
    except: pass

def get_economic_calendar_data():
    try:
        url = "https://economic-calendar.tradingview.com/events"
        params = {
            "from": datetime.now().strftime('%Y-%m-%dT00:00:00.000Z'),
            "to": datetime.now().strftime('%Y-%m-%dT23:59:59.999Z'),
            "countries": "US"
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        events = []
        sorted_events = sorted(data['result'], key=lambda x: x['date'])
        for event in sorted_events:
            dt = datetime.strptime(event['date'], '%Y-%m-%dT%H:%M:%S.000Z')
            uzb_time = dt.replace(tzinfo=pytz.UTC).astimezone(UZB_TZ).strftime('%H:%M')
            title = event.get('title_id', event.get('indicator', 'Muhim voqea'))
            # Vaqtni bold qilish
            events.append(f"<b>{uzb_time}</b> — {title}")
        return "\n".join(events[:10]) if events else "bugun kutilayotgan muhim voqealar topilmadi."
    except: return "ma’lumotlarni yuklashda uzilish bo’ldi."

# 5. TAQVIM XABARI
async def send_economic_calendar(context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(USER_FILE): return
    try:
        with open(USER_FILE, "r") as f: user_ids = f.read().splitlines()
        today = datetime.now(UZB_TZ).strftime('%d.%m.%Y')
        calendar_text = get_economic_calendar_data()
        text = (
            f"<b>AQSh IQTISODIY TAQVIMI | {today}</b>\n—\n"
            f"bugun kutilayotgan muhim voqealar (UZB vaqti bilan):\n\n"
            f"{calendar_text}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("FINVIZ", url="https://finviz.com/calendar.ashx"),
            InlineKeyboardButton("TRADINGVIEW", url="https://www.tradingview.com/economic-calendar/")
        ]])
        for u_id in user_ids:
            try: await context.bot.send_message(chat_id=u_id, text=text, reply_markup=kb, parse_mode='HTML', disable_web_page_preview=True)
            except: continue
    except: pass

# 6. TICKER TAHLILI
def clean_val(val):
    return str(val).replace(',', '') if val not in ['-', 'N/A', None] else "N/A"

def perform_analysis(f):
    try:
        raw_debt = clean_val(f.get('Debt/Eq', '0'))
        try: debt_eq = float(raw_debt)
        except: debt_eq = 0.0
        shariah = "NOJOIZ" if any(x in f.get('Industry', '') for x in ['Banks', 'Insurance', 'Gambling', 'Tobacco']) else ("SHUBHALI" if debt_eq > 0.33 else "JOIZ")
        
        analysis = (
            f"—\n"
            f"■ FUNDAMENTAL (VALUATION & DEBT)\n"
            f"<b>M.CAP:</b> {f.get('Market Cap', 'N/A')} | <b>P/E:</b> {f.get('P/E', 'N/A')} | <b>Fwd P/E:</b> {f.get('Forward P/E', 'N/A')}\n"
            f"<b>P/B:</b> {f.get('P/B', 'N/A')} | <b>P/S:</b> {f.get('P/S', 'N/A')} | <b>Debt/Eq:</b> {raw_debt}\n"
            f"<b>DIVIDEND:</b> {f.get('Dividend %', 'N/A')} | <b>EPS (ttm):</b> {f.get('EPS (ttm)', 'N/A')}\n\n"
            f"■ TECHNICAL (TREND & MOMENTUM)\n"
            f"<b>RSI (14):</b> {clean_val(f.get('RSI (14)', 'N/A'))} | <b>ATR:</b> {f.get('ATR', 'N/A')}\n"
            f"<b>SMA20:</b> {f.get('SMA20', 'N/A')} | <b>SMA50:</b> {f.get('SMA50', 'N/A')} | <b>SMA200:</b> {f.get('SMA200', 'N/A')}\n"
            f"<b>52W Range:</b> {f.get('52W Range', 'N/A')}\n"
            f"—\n<b>SHARI’AT STATUSI:</b> {shariah}"
        )
        raw_sector = f.get('Sector', 'N/A')
        uzb_sector = SECTOR_MAP.get(raw_sector, raw_sector).upper()
        sector_display = f"<b>{raw_sector.upper()}</b> ({uzb_sector})"
        
        return analysis, sector_display, f.get('Price', '0'), f.get('Change', '0')
    except: return "Tahlil xatosi", "N/A", "0", "0%"

async def handle_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or not update.message.text.startswith('$'): return
    save_user(update.effective_user.id)
    ticker = "".join(update.message.text.strip()[1:].split()).upper()
    prog = await update.message.reply_text(f"QIDIRILMOQDA.. ${ticker}")
    try:
        f = finvizfinance(ticker).ticker_fundament()
        if not f:
            await prog.edit_text("Ticker topilmadi.")
            return
        now = datetime.now(UZB_TZ)
        txt, sec, pr, ch = perform_analysis(f)
        cap = f"<b>SANA:</b> {now.strftime('%d.%m.%Y')} | <b>VAQT:</b> {now.strftime('%H:%M')} (UZB)\n\n<b>TICKER:</b> ${ticker} | <b>PRICE:</b> {pr} ({ch})\n<b>SECTOR:</b> {sec}\n{txt}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("FINVIZ", url=f"https://finviz.com/quote.ashx?t={ticker}"), InlineKeyboardButton("ISLAMICLY", url="https://www.islamicly.com/home/stocks")]])
        chart = f"https://charts2.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d&rev={int(time.time())}"
        try:
            await update.message.reply_photo(photo=chart, caption=cap, parse_mode='HTML', reply_markup=kb)
            await prog.delete()
        except: await prog.edit_text(cap, parse_mode='HTML', reply_markup=kb)
    except: await prog.edit_text("$ticker noto‘g‘ri yoki tahlilda uzilish yuz berdi")

# 7. MAIN RUNNER
def main():
    threading.Thread(target=run_http_server, daemon=True).start()
    token = os.getenv("BOT_TOKEN")
    if not token: sys.exit(1)
    
    while True:
        try:
            app = Application.builder().token(token).build()
            if app.job_queue:
                app.job_queue.run_daily(send_economic_calendar, time=dt_time(hour=9, minute=0, second=0, tzinfo=UZB_TZ))
            app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("marhamat! $ticker yuborishingiz mumkin")))
            app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^\$'), handle_ticker))
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            logger.error(f"Restarting... {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
