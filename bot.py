import logging
import os
import time
import pytz
import asyncio
import sqlite3
import httpx
from datetime import datetime, time as dt_time, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from finvizfinance.quote import finvizfinance
from googletrans import Translator

# 1. LOGLAR VA SOZLAMALAR
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
translator = Translator()
DB_FILE = "bot_pro.db"
UZB_TZ = pytz.timezone('Asia/Tashkent')

# Render monitori uchun PORT (8080)
PORT = int(os.environ.get('PORT', 8080))

# 2. MA’LUMOTLAR BAZASI (Xavfsiz ulanish)
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS cache 
                        (key TEXT PRIMARY KEY, value TEXT, expires DATETIME)''')
        conn.commit()

def get_cache(key):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            res = conn.execute("SELECT value FROM cache WHERE key = ? AND expires > ?", 
                               (key, datetime.now())).fetchone()
            return res[0] if res else None
    except Exception as e:
        logger.error(f"Cache get xatosi: {e}")
        return None

def set_cache(key, value, minutes=15):
    try:
        expires = datetime.now() + timedelta(minutes=minutes)
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, value, expires))
            conn.commit()
    except Exception as e:
        logger.error(f"Cache set xatosi: {e}")

# 3. TARJIMA VA TAQVIM
async def translate_safe(text):
    dictionary = {
        "Trade Balance": "Tashqi savdo balansi", "Factory Orders": "Zavod buyurtmalari",
        "Unemployment Claims": "Ishsizlik nafaqasi so‘rovlari", "CPI": "Iste’mol narxlari indeksi (CPI)"
    }
    if text in dictionary: return dictionary[text]
    try:
        # Googletrans asinxron emas, shuning uchun thread’da ishlatamiz
        result = await asyncio.to_thread(translator.translate, text, src='en', dest='uz')
        return result.text
    except:
        return text

async def get_economic_calendar_data():
    cached_data = get_cache("calendar_uz")
    if cached_data: return cached_data.split("||")

    url = "https://economic-calendar.tradingview.com/events"
    params = {
        "from": datetime.now().strftime('%Y-%m-%dT00:00:00.000Z'),
        "to": datetime.now().strftime('%Y-%m-%dT23:59:59.999Z'),
        "countries": "US"
    }

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            events = []
            for event in sorted(data.get('result', []), key=lambda x: x.get('date', '')):
                dt = datetime.strptime(event['date'], '%Y-%m-%dT%H:%M:%S.000Z')
                uzb_time = dt.replace(tzinfo=pytz.UTC).astimezone(UZB_TZ).strftime('%H:%M')
                title_uz = await translate_safe(event.get('indicator', 'Muhim voqea'))
                events.append(f"<b>{uzb_time}</b> — {title_uz}")
            
            if events:
                set_cache("calendar_uz", "||".join(events), minutes=60)
                return events
    except Exception as e:
        logger.error(f"Taqvim xatosi: {e}")
    return None

# 4. TAHLIL MANTIQI (Shari’at statusi qo‘shildi)
def perform_analysis(f):
    try:
        raw_debt = str(f.get('Debt/Eq', '0')).replace(',', '')
        debt_eq = float(raw_debt) if raw_debt not in ['-', 'N/A', ''] else 0.0
        industry = f.get('Industry', '')
        
        # Sektorlarni tekshirish
        haram_sectors = ['Banks', 'Insurance', 'Gambling', 'Tobacco', 'Alcohol', 'Entertainment']
        shariah = "NOJOIZ" if any(x in industry for x in haram_sectors) else ("SHUBHALI" if debt_eq > 0.33 else "JOIZ")
        
        analysis = (
            f"—\n■ FUNDAMENTAL (VALUATION & DEBT)\n"
            f"<b>M.CAP:</b> {f.get('Market Cap', 'N/A')} | <b>P/E:</b> {f.get('P/E', 'N/A')} | <b>Fwd P/E:</b> {f.get('Forward P/E', 'N/A')}\n"
            f"<b>P/B:</b> {f.get('P/B', 'N/A')} | <b>P/S:</b> {f.get('P/S', 'N/A')} | <b>Debt/Eq:</b> {raw_debt}\n"
            f"<b>DIVIDEND:</b> {f.get('Dividend %', 'N/A')} | <b>EPS (ttm):</b> {f.get('EPS (ttm)', 'N/A')}\n\n"
            f"■ TECHNICAL (TREND & MOMENTUM)\n"
            f"<b>RSI (14):</b> {f.get('RSI (14)', 'N/A')} | <b>ATR:</b> {f.get('ATR', 'N/A')}\n"
            f"<b>SMA20:</b> {f.get('SMA20', 'N/A')} | <b>SMA50:</b> {f.get('SMA50', 'N/A')} | <b>SMA200:</b> {f.get('SMA200', 'N/A')}\n"
            f"<b>52W Range:</b> {f.get('52W Range', 'N/A')}\n—\n<b>SHARI’AT STATUSI:</b> {shariah}"
        )
        return analysis, f.get('Sector', 'N/A').upper(), f.get('Price', '0'), f.get('Change', '0')
    except:
        return "Tahlil xatosi", "N/A", "0", "0%"

# 5. BOT ISHLASHI
async def send_economic_calendar(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(UZB_TZ).strftime('%d.%m.%Y')
    events = await get_economic_calendar_data()
    
    with sqlite3.connect(DB_FILE) as conn:
        user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]

    if events is None:
        text = f"<b>AQSh IQTISODIY TAQVIMI | {today}</b>\n—\nma’lumotlarni yuklashda texnik uzilish yuz berdi. marhamat, quyida havolalar orqali tanishib ko‘rishingiz mumkin. "
    elif events:
        text = f"<b>AQSh IQTISODIY TAQVIMI | {today}</b>\n—\nbugun (UZB vaqti bilan):\n\n" + "\n".join(events[:12])
    else:
        text = f"<b>AQSh IQTISODIY TAQVIMI | {today}</b>\n—\nbugun voqealar kutilmayapti."

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("FINVIZ", url="https://finviz.com/calendar.ashx"),
        InlineKeyboardButton("TRADINGVIEW", url="https://www.tradingview.com/economic-calendar/")
    ]])

    for u_id in user_ids:
        try:
            await context.bot.send_message(chat_id=u_id, text=text, reply_markup=kb, parse_mode='HTML')
            await asyncio.sleep(0.05)
        except: continue

async def handle_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    ticker = "".join(update.message.text.strip()[1:].split()).upper()
    user_id = str(update.effective_user.id)
    
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()

    cached_res = get_cache(f"stock_{ticker}")
    kb_links = InlineKeyboardMarkup([[
        InlineKeyboardButton("FINVIZ", url=f"https://finviz.com/quote.ashx?t={ticker}"),
        InlineKeyboardButton("ISLAMICLY", url="https://www.islamicly.com/home/stocks")
    ]])

    if cached_res:
        await update.message.reply_text(cached_res, parse_mode='HTML', reply_markup=kb_links)
        return

    prog = await update.message.reply_text(f"QIDIRILMOQDA.. ${ticker}")
    try:
        # Sinxron funksiyani thread'da chaqiramiz
        f_obj = finvizfinance(ticker)
        f = await asyncio.to_thread(f_obj.ticker_fundament)
        
        if not f:
            await prog.edit_text(f"${ticker} topilmadi."); return
        
        txt, sec, pr, ch = perform_analysis(f)
        cap = (f"<b>SANA:</b> {datetime.now(UZB_TZ).strftime('%d.%m.%Y | %H:%M')} (UZB)\n\n"
               f"<b>TICKER:</b> ${ticker} | <b>PRICE:</b> {pr} ({ch})\n<b>SECTOR:</b> {sec}\n{txt}")
        
        set_cache(f"stock_{ticker}", cap, minutes=15)
        chart = f"https://charts2.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d&rev={int(time.time())}"
        
        try:
            await update.message.reply_photo(photo=chart, caption=cap, parse_mode='HTML', reply_markup=kb_links)
            await prog.delete()
        except:
            await prog.edit_text(cap, parse_mode='HTML', reply_markup=kb_links)
    except Exception as e:
        logger.error(f"Ticker xatosi: {e}")
        await prog.edit_text(f"${ticker} noto‘g‘ri yoki uzilish yuz berdi")

def main():
    init_db()
    token = os.getenv("BOT_TOKEN")
    app = Application.builder().token(token).build()
    
    if app.job_queue:
        app.job_queue.run_daily(send_economic_calendar, time=dt_time(hour=9, minute=0, tzinfo=UZB_TZ))
    
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("<b>marhamat! $ticker yuborishingiz mumkin", parse_mode='HTML')))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^\$'), handle_ticker))
    
    logger.info(f"Bot {PORT}-portda ishga tushdi...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
