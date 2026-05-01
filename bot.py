import logging
import os
import time
import pytz
import threading
import sys
import asyncio
import sqlite3
import httpx
# PYTHON 3.13+ UCHUN MAXSUS TUZATISH (MUHIM!)
import types
mock_module = types.ModuleType('cgi')
mock_module.escape = lambda x: x 
sys.modules['cgi'] = mock_module
sys.modules['imghdr'] = types.ModuleType('imghdr')

from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time as dt_time, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from finvizfinance.quote import finvizfinance
from googletrans import Translator

# Logging sozlamalari
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

DB_FILE = "bot_users.db"
translator = Translator()

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires DATETIME)''')
        conn.commit()

def save_user(user_id):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (str(user_id),))
            conn.commit()
    except Exception as e:
        logger.error(f"DB Error: {e}")

def get_cache(key):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            res = conn.execute("SELECT value FROM cache WHERE key = ? AND expires > ?", (key, datetime.now())).fetchone()
            return res[0] if res else None
    except:
        return None

def set_cache(key, value, minutes=15):
    try:
        expires = datetime.now() + timedelta(minutes=minutes)
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, value, expires))
            conn.commit()
    except:
        pass

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is operational")
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
    def log_message(self, format, *args): 
        return

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP Server error: {e}")

UZB_TZ = pytz.timezone('Asia/Tashkent')
SECTOR_MAP = {
    "Technology": "TEXNOLOGIYA", "Financial": "MOLIYA", "Healthcare": "SOG‘LIQNI SAQLASH",
    "Consumer Cyclical": "ISTE’MOL TOVARLARI", "Consumer Defensive": "ISTE’MOL TOVARLARI (HIMOYA)",
    "Energy": "ENERGETIKA", "Communication Services": "ALOQA", "Industrials": "SANOAT",
    "Basic Materials": "XOMASHYO", "Real Estate": "KO‘CHMAS MULK", "Utilities": "KOMMUNAL"
}

async def translate_safe(text):
    try:
        result = await asyncio.to_thread(translator.translate, text, src='en', dest='uz')
        return result.text
    except:
        return text

async def get_economic_calendar_data():
    cached = get_cache("calendar_uz")
    if cached: return cached
    try:
        url = "https://economic-calendar.tradingview.com/events"
        params = {
            "from": datetime.now().strftime('%Y-%m-%dT00:00:00.000Z'),
            "to": datetime.now().strftime('%Y-%m-%dT23:59:59.999Z'),
            "countries": "US"
        }
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=15)
            data = response.json()
            
        events = []
        for event in sorted(data['result'], key=lambda x: x['date']):
            dt = datetime.strptime(event['date'], '%Y-%m-%dT%H:%M:%S.000Z')
            uzb_time = dt.replace(tzinfo=pytz.UTC).astimezone(UZB_TZ).strftime('%H:%M')
            title_en = event.get('indicator', 'Muhim voqea')
            title_uz = await translate_safe(title_en)
            events.append(f"<b>{uzb_time}</b> — {title_uz}")
        
        res = "\n".join(events[:10]) if events else "bugun kutilayotgan muhim voqealar topilmadi."
        set_cache("calendar_uz", res, minutes=60)
        return res
    except Exception:
        return "ma’lumotlarni yuklashda uzilish bo‘ldi."

async def send_economic_calendar(context: ContextTypes.DEFAULT_TYPE):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]
        
        if not user_ids: return
        today = datetime.now(UZB_TZ).strftime('%d.%m.%Y')
        calendar_text = await get_economic_calendar_data()
        text = f"<b>AQSh IQTISODIY TAQVIMI | {today}</b>\n—\nbugun (UZB vaqti bilan):\n\n{calendar_text}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("FINVIZ", url="https://FINVIZ.com/calendar.ashx"),
            InlineKeyboardButton("TRADINGVIEW", url="https://www.TRADINGVIEW.com/economic-calendar/")
        ]])
        for u_id in user_ids:
            try:
                await context.bot.send_message(chat_id=u_id, text=text, reply_markup=kb, parse_mode='HTML', disable_web_page_preview=True)
                await asyncio.sleep(0.05)
            except: continue
    except: pass

def clean_val(val):
    return str(val).replace(',', '') if val not in ['-', 'N/A', None] else "N/A"

def perform_analysis(f):
    try:
        raw_debt = clean_val(f.get('Debt/Eq', '0'))
        try: debt_eq = float(raw_debt)
        except: debt_eq = 0.0
        
        industry = f.get('Industry', '')
        sector = f.get('Sector', '')
        haram_keywords = ['Banks', 'Insurance', 'Gambling', 'Tobacco', 'Alcohol', 'Entertainment', 'Financial Services']
        is_haram = any(kw.lower() in industry.lower() or kw.lower() in sector.lower() for kw in haram_keywords)
        
        if is_haram:
            shariah = "NOJOIZ"
        elif debt_eq > 0.33:
            shariah = "SHUBHALI"
        else:
            shariah = "JOIZ"

        analysis = (
            f"—\n■ FUNDAMENTAL (VALUATION & DEBT)\n"
            f"<b>M.CAP:</b> {f.get('Market Cap', 'N/A')} | <b>P/E:</b> {f.get('P/E', 'N/A')} | <b>Fwd P/E:</b> {f.get('Forward P/E', 'N/A')}\n"
            f"<b>P/B:</b> {f.get('P/B', 'N/A')} | <b>P/S:</b> {f.get('P/S', 'N/A')} | <b>Debt/Eq:</b> {raw_debt}\n"
            f"<b>DIVIDEND:</b> {f.get('Dividend %', 'N/A')} | <b>EPS (ttm):</b> {f.get('EPS (ttm)', 'N/A')}\n\n"
            f"■ TECHNICAL (TREND & MOMENTUM)\n"
            f"<b>RSI (14):</b> {clean_val(f.get('RSI (14)', 'N/A'))} | <b>ATR:</b> {f.get('ATR', 'N/A')}\n"
            f"<b>SMA20:</b> {f.get('SMA20', 'N/A')} | <b>SMA50:</b> {f.get('SMA50', 'N/A')} | <b>SMA200:</b> {f.get('SMA200', 'N/A')}\n"
            f"<b>52W Range:</b> {f.get('52W Range', 'N/A')}\n—\n<b>SHARI’AT STATUSI:</b> {shariah}"
        )
        return analysis, f"{sector.upper()} ({SECTOR_MAP.get(sector, sector).upper()})", f.get('Price', '0'), f.get('Change', '0')
    except:
        return "Tahlil xatosi", "N/A", "0", "0%"

async def handle_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    save_user(update.effective_user.id)
    ticker = "".join(update.message.text.strip()[1:].split()).upper()
    
    cached = get_cache(f"st_{ticker}")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("FINVIZ", url=f"https://FINVIZ.com/quote.ashx?t={ticker}"),
        InlineKeyboardButton("ISLAMICLY", url=f"https://www.islamicly.com/home/stocks")
    ]])
    
    if cached:
        await update.message.reply_text(cached, parse_mode='HTML', reply_markup=kb)
        return

    prog = await update.message.reply_text(f"QIDIRILMOQDA.. ${ticker}")
    try:
        stock = finvizfinance(ticker)
        f = await asyncio.to_thread(stock.ticker_fundament)
        if not f:
            await prog.edit_text("Ticker topilmadi.")
            return
            
        now = datetime.now(UZB_TZ)
        txt, sec, pr, ch = perform_analysis(f)
        cap = (f"<b>SANA:</b> {now.strftime('%d.%m.%Y')} | <b>VAQT:</b> {now.strftime('%H:%M')} (UZB)\n\n"
               f"<b>TICKER:</b> ${ticker} | <b>PRICE:</b> {pr} ({ch})\n<b>SECTOR:</b> {sec}\n{txt}")
        
        set_cache(f"st_{ticker}", cap, minutes=15)
        chart = f"https://charts2.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d&rev={int(time.time())}"
        try:
            await update.message.reply_photo(photo=chart, caption=cap, parse_mode='HTML', reply_markup=kb)
            await prog.delete()
        except:
            await prog.edit_text(cap, parse_mode='HTML', reply_markup=kb)
    except:
        await prog.edit_text(f"${ticker} noto‘g‘ri yoki uzilish yuz berdi")

async def handle_invalid_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>XATOLIK: noto‘g‘ri format.</b>\n\n"
        "<b>ogohlantirish. faqat aksiya tickerini $ticker formatida yuborishingizni so‘raymiz.</b>\n"
        "<b>audio, video, rasm, boshqalar va har qanday fayllar qabul qilinmaydi.</b>",
        parse_mode='HTML'
    )

def main():
    init_db()
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
            app.add_handler(MessageHandler(~filters.COMMAND, handle_invalid_content))
            app.run_polling(drop_pending_updates=True)
        except:
            time.sleep(10)

if __name__ == "__main__":
    main()
