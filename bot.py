import logging
import os
import time
import pytz
import threading
import sys
import asyncio
import sqlite3
import httpx
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, time as dt_time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from finvizfinance.quote import finvizfinance
from deep_translator import GoogleTranslator

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

DB_FILE = "bot_users.db"
UZB_TZ = pytz.timezone('Asia/Tashkent')

ECONOMIC_TRANSLATIONS = {
    "Unemployment Rate": "Ishsizlik darajasi",
    "Nonfarm Payrolls": "Qishloq xo‘jaligidan tashqari bandlik o‘zgarishi (NFP)",
    "ISM Manufacturing PMI": "Sanoat faolligi indeksi (ISM)",
    "FOMC Member Speech": "FOMC a’zosi nutqi",
    "CPI": "Inflyatsiya darajasi (CPI)",
    "GDP": "YaIM (Yalpi ichki mahsulot)",
    "Initial Jobless Claims": "Ishsizlik nafaqasi uchun dastlabki arizalar",
    "Fed Interest Rate Decision": "FED foiz stavkasi qarori",
    "Consumer Confidence": "Iste’molchilar ishonchi indeksi",
    "Retail Sales": "Chakana savdo hajmi"
}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
    conn.commit()
    conn.close()

def save_user(user_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (str(user_id),))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB Error: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
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

async def get_investing_news():
    try:
        url = "https://uz.investing.com/rss/news_285.rss"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=10)
            root = ET.fromstring(response.content)
            news_items = []
            for item in root.findall('./channel/item')[:3]:
                title = item.find('title').text
                news_items.append(f"• {title}")
            return "\n".join(news_items) if news_items else ""
    except: return ""

async def get_economic_calendar_data():
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
        translator = GoogleTranslator(source='en', target='uz')
        for event in sorted(data['result'], key=lambda x: x['date']):
            dt = datetime.strptime(event['date'], '%Y-%m-%dT%H:%M:%S.000Z')
            uzb_time = dt.replace(tzinfo=pytz.UTC).astimezone(UZB_TZ).strftime('%H:%M')
            orig_title = event.get('title_id', event.get('indicator', 'Muhim voqea'))
            title = ECONOMIC_TRANSLATIONS.get(orig_title)
            if not title:
                try: title = translator.translate(orig_title)
                except: title = orig_title
            events.append(f"<b>{uzb_time}</b> — {title}")
        return "\n".join(events[:10]) if events else "bugun iqtisodiy yangiliklar va hisobotlar kutilmayapti."
    except: return "ma’lumotlarni yuklashda uzilish bo‘ldi. marhamat, quyidagi havolalar orqali tanishib ko‘rishingiz mumkin."

async def send_economic_calendar(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE)
        user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]
        conn.close()
        if not user_ids: return

        today = datetime.now(UZB_TZ).strftime('%d.%m.%Y')
        calendar_text = await get_economic_calendar_data()
        news_text = await get_investing_news()
        
        text = f"<b>BOZOR TAQVIMI VA YANGILIKLARI | {today}</b>\n—\n<b>AQSh IQTISODIY TAQVIMI (UZB):</b>\n{calendar_text}"
        if news_text:
            text += f"\n\n<b>FOND BOZORI YANGILIKLARI (INVESTING):</b>\n{news_text}"
            
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("INVESTING", url="https://uz.investing.com/news/stock-market-news"),
            InlineKeyboardButton("TRADINGVIEW", url="https://www.tradingview.com/economic-calendar/")
        ]])

        for u_id in user_ids:
            try:
                await context.bot.send_message(chat_id=u_id, text=text, reply_markup=kb, parse_mode='HTML', disable_web_page_preview=True)
            except: continue
    except: pass

SECTOR_MAP = {
    "Technology": "TEXNOLOGIYA", "Financial": "MOLIYA", "Healthcare": "SOG‘LIQNI SAQLASH",
    "Consumer Cyclical": "ISTE’MOL TOVARLARI", "Consumer Defensive": "ISTE’MOL TOVARLARI (HIMOYA)",
    "Energy": "ENERGETIKA", "Communication Services": "ALOQA", "Industrials": "SANOAT",
    "Basic Materials": "XOMASHYO", "Real Estate": "KO‘CHMAS MULK", "Utilities": "KOMMUNAL"
}

def clean_val(val):
    return str(val).replace(',', '') if val not in ['-', 'N/A', None] else "N/A"

def perform_analysis(f):
    try:
        raw_debt = clean_val(f.get('Debt/Eq', '0'))
        try: debt_eq = float(raw_debt)
        except: debt_eq = 0.0
        industry = f.get('Industry', '')
        haram = ['Banks', 'Insurance', 'Gambling', 'Tobacco', 'Alcohol', 'Entertainment']
        shariah = "NOJOIZ" if any(x in industry for x in haram) else ("SHUBHALI" if debt_eq > 0.33 else "JOIZ")

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
        raw_sec = f.get('Sector', 'N/A')
        return analysis, f"{raw_sec.upper()} ({SECTOR_MAP.get(raw_sec, raw_sec).upper()})", f.get('Price', '0'), f.get('Change', '0')
    except: return "Tahlil xatosi", "N/A", "0", "0%"

async def handle_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    save_user(update.effective_user.id)
    ticker = "".join(update.message.text.strip()[1:].split()).upper()
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
               f"<b>TICKER:</b> ${ticker} | <b>PRICE:</b> {pr} ({ch})\n"
               f"<b>SECTOR:</b> {sec}\n{txt}")
        
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("FINVIZ", url=f"https://finviz.com/quote.ashx?t={ticker}"),
            InlineKeyboardButton("ISLAMICLY", url="https://www.islamicly.com/home/stocks")
        ]])
        
        chart = f"https://charts2.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d&rev={int(time.time())}"
        try:
            await update.message.reply_photo(photo=chart, caption=cap, parse_mode='HTML', reply_markup=kb)
            await prog.delete()
        except: await prog.edit_text(cap, parse_mode='HTML', reply_markup=kb)
    except: await prog.edit_text(f"${ticker} noto‘g‘ri yoki uzilish yuz berdi")

async def handle_invalid_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>XATOLIK: noto‘g‘ri format.</b>\n\n"
        "<b>ogohlantirish. faqat aksiya tickerini $ticker matn formatida yuborishingizni so‘raymiz.</b>",
        parse_mode='HTML'
    )

def main():
    init_db()
    threading.Thread(target=run_http_server, daemon=True).start()
    token = os.getenv("BOT_TOKEN")
    if not token: sys.exit(1)
    app = Application.builder().token(token).build()
    if app.job_queue:
        app.job_queue.run_daily(send_economic_calendar, time=dt_time(hour=9, minute=0, second=0, tzinfo=UZB_TZ))
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("marhamat! $ticker yuborishingiz mumkin")))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^\$'), handle_ticker))
    app.add_handler(MessageHandler(~filters.COMMAND, handle_invalid_content))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
