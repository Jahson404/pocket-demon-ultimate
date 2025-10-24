import asyncio
import logging
import numpy as np
from pocketoptionapi import PocketOptionAPI  # ChipaDev import
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
import threading
import http.server
import socketserver
import os
import json
from datetime import datetime
import matplotlib.pyplot as plt
import io
import polars as pd
import pandas_ta as ta
from pytz import timezone

# === CONVERSATION STATES ===
EMAIL, DEMO_PASS, LIVE_EMAIL, LIVE_PASS = range(4)

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ASSETS = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'BTCUSD', 'ETHUSD']
TIMEFRAME = 60
CANDLE_COUNT = 50
EXPIRY = 60
USER_DATA_FILE = 'user_data.json'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# === USER DATABASE ===
class UserDB:
    def __init__(self):
        self.users = self.load()
    
    def load(self):
        if os.path.exists(USER_DATA_FILE):
            with open(USER_DATA_FILE, 'r') as f:
                return json.load(f)
        return {}
    
    def save(self):
        with open(USER_DATA_FILE, 'w') as f:
            json.dump(self.users, f, indent=2)
    
    def get(self, uid):
        uid = str(uid)
        if uid not in self.users:
            self.users[uid] = {
                'demo_email': None, 'demo_pass': None,
                'live_email': None, 'live_pass': None,
                'mode': 'demo',
                'amount': 5, 'use_percent': False, 'percent': 1.0,
                'martingale': False, 'martingale_step': 0,
                'wins': 0, 'losses': 0, 'profit': 0.0,
                'trades': [], 'assets': ASSETS.copy(),
                'pending_live': False
            }
            self.save()
        return self.users[uid]
    
    def update(self, uid, data):
        uid = str(uid)
        self.users[uid].update(data)
        self.save()

db = UserDB()

# === GLOBALS ===
user_apis = {}
user_trading = {}
user_prices = {}
user_candles = {}

# === KEEP-ALIVE SERVER ($0 24/7) ===
def keep_alive():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DEMON AWAKE | USERS: {}\n".format(len(db.users)).encode())
    try:
        with socketserver.TCPServer(("0.0.0.0", 8080), Handler) as httpd:
            httpd.serve_forever()
    except: pass
threading.Thread(target=keep_alive, daemon=True).start()

# === CONNECT USER ===
async def connect_user(user_id):
    user = db.get(user_id)
    email = user['demo_email'] if user['mode'] == 'demo' else user['live_email']
    password = user['demo_pass'] if user['mode'] == 'demo' else user['live_pass']
    if not email or not password: return None
    api = PocketOptionAPI(email=email, password=password, is_demo=user['mode'] == 'demo')  # Adjusted for ChipaDev
    if await api.connect():  # ChipaDev method
        user_apis[user_id] = api
        return api
    return None

# === /start - DASHBOARD ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get(user_id)
    api = user_apis.get(user_id)
    balance = await api.get_balance() if api else 0
    keyboard = [
        [InlineKeyboardButton("START TRADING", callback_data='start_trading')],
        [InlineKeyboardButton("STATUS", callback_data='status')],
        [InlineKeyboardButton("DEMO", callback_data='demo')],
        [InlineKeyboardButton("LIVE", callback_data='live')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "POCKET DEMON vFINAL\n"
        "YOUR COMMANDS. FULL CONTROL.\n\n"
        f"Mode: <b>{user['mode'].upper()}</b>\n"
        f"Balance: <b>${balance:.2f}</b>\n\n"
        "COMMANDS:\n"
        "/demo /live /confirm\n"
        "/price EURUSD /chart BTCUSD\n"
        "/pnl /balance /logs\n"
        "/setamount 10 /setpercent 2\n"
        "/martingale on/off /assets",
        reply_markup=reply_markup, parse_mode='HTML'
    )

# === BUTTON HANDLER ===
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == 'start_trading':
        await start_trading(query, context)
    elif data == 'status':
        await status(query, context)
    elif data == 'demo':
        await switch_mode(query, context, 'demo')
    elif data == 'live':
        user_id = query.from_user.id
        db.update(user_id, {'pending_live': True})
        keyboard = [[InlineKeyboardButton("CONFIRM LIVE", callback_data='confirm_live')]]
        await query.edit_message_text("WARNING: LIVE TRADING\nType /confirm", reply_markup=InlineKeyboardMarkup(keyboard))

# === /confirm ===
async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get(user_id)
    if not user['pending_live']:
        await update.message.reply_text("No live request.")
        return
    await switch_mode(update, context, 'live')
    db.update(user_id, {'pending_live': False})

# === /demo /live ===
async def demo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await switch_mode(update, context, 'demo')

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.update(user_id, {'pending_live': True})
    keyboard = [[InlineKeyboardButton("CONFIRM", callback_data='confirm_live')]]
    await update.message.reply_text("LIVE MODE REQUESTED\nType /confirm", reply_markup=InlineKeyboardMarkup(keyboard))

# === SWITCH MODE ===
async def switch_mode(update_or_query, context, mode):
    user_id = update_or_query.from_user.id if hasattr(update_or_query, 'from_user') else update_or_query.message.from_user.id
    db.update(user_id, {'mode': mode})
    if user_id in user_apis:
        try: await user_apis[user_id].disconnect()
        except: pass
        del user_apis[user_id]
    await (update_or_query.message.reply_text if hasattr(update_or_query, 'message') else update_or_query.edit_message_text)(f"Switched to {mode.upper()}")

# === /price ===
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price EURUSD")
        return
    asset = context.args[0].upper()
    if asset not in user_prices:
        await update.message.reply_text("No data.")
        return
    p = user_prices[asset]
    await update.message.reply_text(
        f"<b>{asset}</b>\n"
        f"Bid: <code>{p['bid']:.5f}</code>\n"
        f"Ask: <code>{p['ask']:.5f}</code>\n"
        f"Time: {p['time']}",
        parse_mode='HTML'
    )

# === /chart ===
async def chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = context.args[0].upper() if context.args else 'EURUSD'
    buffer = generate_chart(asset)
    if buffer:
        await update.message.reply_photo(buffer, caption=f"Live {asset}")
    else:
        await update.message.reply_text("No data.")

def generate_chart(asset):
    if asset not in user_candles or len(user_candles[asset]) < 10: return None
    df = pd.DataFrame(user_candles[asset])
    plt.figure(figsize=(10,6))
    plt.plot(df['time'], df['close'], color='lime')
    plt.title(f"{asset} LIVE", color='white')
    plt.gca().set_facecolor('black')
    plt.gcf().set_facecolor('black')
    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', facecolor='black')
    buffer.seek(0)
    plt.close()
    return buffer

# === /pnl ===
async def pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get(user_id)
    rate = user['wins'] / (user['wins'] + user['losses']) * 100 if (user['wins'] + user['losses']) > 0 else 0
    await update.message.reply_text(
        f"<b>P&L</b>\n"
        f"Profit: <b>${user['profit']:.2f}</b>\n"
        f"Win Rate: <b>{rate:.1f}%</b>",
        parse_mode='HTML'
    )

# === /balance ===
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    api = user_apis.get(user_id)
    bal = await api.get_balance() if api else 0
    await update.message.reply_text(f"Balance: <b>${bal:.2f}</b>", parse_mode='HTML')

# === /setamount /setpercent ===
async def setamount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        amount = float(context.args[0])
        db.update(user_id, {'amount': amount, 'use_percent': False})
        await update.message.reply_text(f"Fixed ${amount}")
    except: await update.message.reply_text("Usage: /setamount 10")

async def setpercent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        p = float(context.args[0])
        if 0 < p <= 100:
            db.update(user_id, {'percent': p, 'use_percent': True})
            await update.message.reply_text(f"{p}% of balance")
    except: await update.message.reply_text("Usage: /setpercent 2")

# === /martingale ===
async def martingale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cmd = context.args[0].lower() if context.args else ""
    if cmd == "on":
        db.update(user_id, {'martingale': True, 'martingale_step': 0})
        await update.message.reply_text("MARTINGALE ON")
    elif cmd == "off":
        db.update(user_id, {'martingale': False, 'martingale_step': 0})
        await update.message.reply_text("MARTINGALE OFF")

# === /assets ===
async def assets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get(user_id)
    keyboard = []
    for a in ASSETS:
        status = "ON" if a in user['assets'] else "OFF"
        keyboard.append([InlineKeyboardButton(f"{a} [{status}]", callback_data=f'toggle_{a}')])
    await update.message.reply_text("Toggle assets:", reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    asset = query.data.split('_')[1]
    user_id = query.from_user.id
    user = db.get(user_id)
    if asset in user['assets']:
        user['assets'].remove(asset)
    else:
        user['assets'].append(asset)
    db.update(user_id, {'assets': user['assets']})
    await query.answer()
    await assets(query, context)

# === /logs ===
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get(user_id)
    if not user['trades']:
        await update.message.reply_text("No trades.")
        return
    text = "LAST 10 TRADES:\n" + "\n".join(user['trades'][-10:])
    await update.message.reply_text(text)

# === REGISTRATION ===
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send <b>DEMO EMAIL</b>", parse_mode='HTML')
    return EMAIL

async def get_demo_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['demo_email'] = update.message.text
    await update.message.reply_text("Send <b>DEMO PASSWORD</b>", parse_mode='HTML')
    return DEMO_PASS

async def get_demo_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['demo_pass'] = update.message.text
    await update.message.reply_text("Send <b>LIVE EMAIL</b>", parse_mode='HTML')
    return LIVE_EMAIL

async def get_live_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['live_email'] = update.message.text
    await update.message.reply_text("Send <b>LIVE PASSWORD</b>", parse_mode='HTML')
    return LIVE_PASS

async def get_live_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = {
        'demo_email': context.user_data['demo_email'],
        'demo_pass': context.user_data['demo_pass'],
        'live_email': context.user_data['live_email'],
        'live_pass': update.message.text
    }
    db.update(user_id, data)
    await update.message.reply_text("ACCOUNT LINKED! Use /myaccount")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# === TRADE LOOP ===
async def trade_loop(user_id):
    while user_trading.get(user_id):
        try:
            api = user_apis.get(user_id)
            if not api or not await api.is_connected():  # ChipaDev check
                api = await connect_user(user_id)
                if not api: break
            user = db.get(user_id)
            asset = user['assets'][0]
            candles = await api.get_candles(asset, TIMEFRAME, CANDLE_COUNT)  # ChipaDev method
            if not candles or len(candles) < 20:
                await asyncio.sleep(10)
                continue

            df = pd.DataFrame(candles)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            latest_time = df['time'].max()
            now = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(timezone('Africa/Lagos'))
            if (now - latest_time).total_seconds() > 60:
                logging.warning(f"Stale data for {asset}: {latest_time} vs {now}")
                await asyncio.sleep(10)
                continue

            df_pd = df.to_pandas()
            df_pd['rsi'] = df_pd['close'].ta.rsi(length=14)  # pandas_ta
            df = pd.from_pandas(df_pd)
            latest = df[-1]

            amount = user['amount']
            if user['use_percent']:
                balance = await api.get_balance()
                amount = max(1, round(balance * user['percent'] / 100, 2))

            direction = None
            if latest['rsi'] < 30:
                direction = 1  # CALL
            elif latest['rsi'] > 70:
                direction = 0  # PUT

            if direction is not None:
                trade_id = await api.buy_binary(asset, amount, direction, EXPIRY)  # ChipaDev method
                await asyncio.sleep(EXPIRY + 5)
                result = await api.check_win(trade_id)  # ChipaDev method
                if result and result > 0:
                    user['wins'] += 1
                    user['profit'] += result
                else:
                    user['losses'] += 1
                db.update(user_id, user)
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Trade error: {e}")
            await asyncio.sleep(10)

async def start_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if hasattr(update, 'effective_user') else update.from_user.id
    if user_trading.get(user_id):
        await update.message.reply_text("Already trading.")
        return
    api = await connect_user(user_id)
    if not api:
        await update.message.reply_text("Connect failed. Check /myaccount")
        return
    user_trading[user_id] = True
    asyncio.create_task(trade_loop(user_id))
    await update.message.reply_text("TRADING STARTED")

# === MAIN ===
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler('register', register)],
        states={
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_demo_email)],
            DEMO_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_demo_pass)],
            LIVE_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_live_email)],
            LIVE_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_live_pass)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(conv)
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("demo", demo))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("chart", chart))
    app.add_handler(CommandHandler("pnl", pnl))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("setamount", setamount))
    app.add_handler(CommandHandler("setpercent", setpercent))
    app.add_handler(CommandHandler("martingale", martingale))
    app.add_handler(CommandHandler("assets", assets))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CallbackQueryHandler(button, pattern='^(start_trading|status|demo|live|confirm_live)$'))
    app.add_handler(CallbackQueryHandler(toggle_asset, pattern='^toggle_'))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    logging.info("POCKET DEMON ONLINE - $0 FOREVER")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
