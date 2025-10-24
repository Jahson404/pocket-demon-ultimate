import asyncio
import logging
import os
import json
from datetime import datetime
import matplotlib.pyplot as plt
import io
import polars as pd
import pandas_ta as ta
from pytz import timezone
from pocketoptionapi import PocketOptionAPI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ASSETS = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'BTCUSD', 'ETHUSD']
TIMEFRAME, CANDLE_COUNT, EXPIRY = 60, 50, 60
USER_DATA_FILE = 'user_data.json'
logging.basicConfig(level=logging.INFO)

# === USER DATABASE ===
class UserDB:
    def __init__(self): self.users = self.load()
    def load(self): return json.load(open(USER_DATA_FILE)) if os.path.exists(USER_DATA_FILE) else {}
    def save(self): json.dump(self.users, open(USER_DATA_FILE, 'w'), indent=2)
    def get(self, uid): 
        uid = str(uid)
        if uid not in self.users:
            self.users[uid] = {'demo_email': None, 'demo_pass': None, 'live_email': None, 'live_pass': None, 'mode': 'demo',
                               'amount': 5, 'use_percent': False, 'percent': 1.0, 'martingale': False, 'martingale_step': 0,
                               'wins': 0, 'losses': 0, 'profit': 0.0, 'trades': [], 'assets': ASSETS.copy(), 'pending_live': False}
            self.save()
        return self.users[uid]
    def update(self, uid, data): self.users[str(uid)].update(data); self.save()

db = UserDB()
user_apis, user_trading, user_prices, user_candles = {}, {}, {}, {}

# === CONNECT USER ===
async def connect_user(user_id):
    user = db.get(user_id)
    email, password = (user['demo_email'], user['demo_pass']) if user['mode'] == 'demo' else (user['live_email'], user['live_pass'])
    if not email or not password: return None
    api = PocketOptionAPI(email=email, password=password, is_demo=user['mode'] == 'demo')
    if await api.connect(): user_apis[user_id] = api; return api
    return None

# === /start ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user, api = db.get(user_id), user_apis.get(user_id)
    balance = await api.get_balance() if api else 0
    keyboard = [[InlineKeyboardButton("START TRADING", callback_data='start_trading')], [InlineKeyboardButton("DEMO", callback_data='demo')], [InlineKeyboardButton("LIVE", callback_data='live')]]
    await update.message.reply_text(f"POCKET DEMON vFINAL\nMode: <b>{user['mode'].upper()}</b>\nBalance: <b>${balance:.2f}</b>\n/start_trading", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

# === BUTTON HANDLER ===
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if query.data == 'start_trading': await start_trading(query, context)
    elif query.data == 'demo': await switch_mode(query, context, 'demo')
    elif query.data == 'live': db.update(query.from_user.id, {'pending_live': True}); await query.edit_message_text("LIVE? /confirm")

async def switch_mode(query, context, mode): 
    user_id = query.from_user.id; db.update(user_id, {'mode': mode})
    if user_id in user_apis: del user_apis[user_id]
    await query.edit_message_text(f"Switched to {mode.upper()}")

# === /price ===
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: /price EURUSD"); return
    asset = context.args[0].upper()
    if asset in user_prices: await update.message.reply_text(f"{asset}\nBid: {user_prices[asset]['bid']:.5f}\nAsk: {user_prices[asset]['ask']:.5f}", parse_mode='HTML')

# === TRADE LOOP (Simplified) ===
async def trade_loop(user_id):
    while user_trading.get(user_id):
        api = user_apis.get(user_id)
        if not api or not await api.is_connected(): api = await connect_user(user_id) or break
        user = db.get(user_id); asset = user['assets'][0]
        candles = await api.get_candles(asset, TIMEFRAME, CANDLE_COUNT)
        if not candles or len(candles) < 20: await asyncio.sleep(10); continue
        df = pd.DataFrame(candles); df['time'] = pd.to_datetime(df['time'], unit='s')
        if (datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(timezone('Africa/Lagos')) - df['time'].max()).total_seconds() > 60: await asyncio.sleep(10); continue
        df_pd = df.to_pandas(); df_pd['rsi'] = df_pd.ta.rsi(14); df = pd.from_pandas(df_pd); latest = df[-1]
        amount = user['amount'] if not user['use_percent'] else max(1, round((await api.get_balance()) * user['percent'] / 100, 2))
        direction = 1 if latest['rsi'] < 30 else 0 if latest['rsi'] > 70 else None
        if direction is not None:
            trade_id = await api.buy_binary(asset, amount, direction, EXPIRY)
            await asyncio.sleep(EXPIRY + 5)
            result = await api.check_win(trade_id)
            user['wins' if result > 0 else 'losses'] += 1; user['profit'] += result if result > 0 else 0
            db.update(user_id, user)
        await asyncio.sleep(5)

async def start_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if hasattr(update, 'effective_user') else update.from_user.id
    if user_trading.get(user_id): await update.message.reply_text("Already trading."); return
    api = await connect_user(user_id)
    if not api: await update.message.reply_text("Connect failed."); return
    user_trading[user_id] = True
    asyncio.create_task(trade_loop(user_id))
    await update.message.reply_text("TRADING STARTED")

# === MAIN ===
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("start_trading", start_trading))
    app.add_handler(CallbackQueryHandler(button))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("POCKET DEMON ONLINE")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
