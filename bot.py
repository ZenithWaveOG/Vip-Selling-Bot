import os
import logging
import random
import string
import asyncio
import threading
from datetime import datetime
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from supabase import create_client, Client

print("Imports OK", flush=True)

# ==================== CONFIG ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_IDS = [7522869983]  # Replace with your Telegram user ID

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- Initialize settings (bot_status, etc.) ----------
def init_settings():
    status = supabase.table('settings').select('*').eq('key', 'bot_status').execute()
    if not status.data:
        supabase.table('settings').insert({'key': 'bot_status', 'value': 'on'}).execute()
init_settings()

def init_prices():
    existing = supabase.table('prices').select('*').eq('coupon_type', 'S01').execute()
    if not existing.data:
        supabase.table('prices').insert({
            'coupon_type': 'S01',
            'price_1': 10,
            'price_5': 45,
            'price_10': 85,
            'price_20': 160
        }).execute()
init_prices()

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== CONSTANTS ====================
COUPON_TYPES = ['S01']
MAX_QUANTITY = 5

# Conversation states
SELECTING_COUPON_TYPE, CUSTOM_QUANTITY = range(2)
WAITING_UTR, WAITING_PAYMENT_SCREENSHOT = range(2, 4)

# ==================== HELPER FUNCTIONS ====================
def get_main_menu(user_id=None):
    buttons = [
        [KeyboardButton("🛒 Buy Vouchers")],
        [KeyboardButton("📦 My Orders")],
        [KeyboardButton("📜 Disclaimer")],
        [KeyboardButton("🆘 Support"), KeyboardButton("📢 Our Channels")]
    ]
    if user_id and user_id in ADMIN_IDS:
        buttons.append([KeyboardButton("🛠 Admin Panel")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_admin_reply_keyboard():
    status = supabase.table('settings').select('value').eq('key', 'bot_status').execute()
    current = status.data[0]['value'] if status.data else 'on'
    toggle_text = "🔛 Turn Off" if current == 'on' else "🔴 Turn On"
    
    buttons = [
        ["➕ Add Coupon", "➖ Remove Coupon"],
        ["📊 Stock", "🎁 Get Free Code"],
        ["💰 Change Prices", "📢 Broadcast"],
        ["🕒 Last 10 Purchases", "🖼 Update QR"],
        [toggle_text]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_agree_decline_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Agree", callback_data="agree_terms")],
        [InlineKeyboardButton("❌ Decline", callback_data="decline_terms")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_coupon_type_keyboard():
    keyboard = [[InlineKeyboardButton(f"{ct} Off", callback_data=f"ctype_{ct}")] for ct in COUPON_TYPES]
    return InlineKeyboardMarkup(keyboard)

def generate_order_id():
    return 'ORD' + ''.join(random.choices(string.digits, k=14))

def get_coupon_type_admin_keyboard(action):
    keyboard = [[InlineKeyboardButton(f"{ct} Off", callback_data=f"admin_{action}_{ct}")] for ct in COUPON_TYPES]
    return InlineKeyboardMarkup(keyboard)

# ---------- Bot status check ----------
async def check_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user.id in ADMIN_IDS:
        return True
    status = supabase.table('settings').select('value').eq('key', 'bot_status').execute()
    if status.data and status.data[0]['value'] == 'off':
        if update.callback_query:
            await update.callback_query.answer("⚠️ Bot is offline for maintenance.", show_alert=True)
        else:
            await update.effective_message.reply_text("⚠️ Bot is currently offline for maintenance. Please try again later.")
        return False
    return True

# ==================== HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return
    user = update.effective_user
    supabase.table('users').upsert({
        'user_id': user.id,
        'username': user.username,
        'first_name': user.first_name
    }).execute()

    stock_msg = "✏️ VIP BOT SHOP\n━━━━━━━━━━━━━━\n📊 Current Stock\n\n"
    for ct in COUPON_TYPES:
        count = supabase.table('coupons').select('*', count='exact').eq('type', ct).eq('is_used', False).execute()
        stock = count.count if hasattr(count, 'count') else 0
        price = supabase.table('prices').select('price_1').eq('coupon_type', ct).execute()
        price_val = price.data[0]['price_1'] if price.data else 'N/A'
        stock_msg += f"▫️ {ct} Off: {stock} left (₹{price_val})\n"

    await update.message.reply_text(stock_msg, reply_markup=get_main_menu(user.id))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text("Admin Panel", reply_markup=get_admin_reply_keyboard())

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return
    user = update.effective_user
    text = update.message.text

    if user.id in ADMIN_IDS and (
        'admin_action' in context.user_data or
        context.user_data.get('broadcast') or
        context.user_data.get('awaiting_qr')
    ):
        await admin_message_handler(update, context)
        return

    if text == "🛒 Buy Vouchers":
        terms = (
            "1. Once coupon is delivered, no returns or refunds will be accepted.\n"
            "2. All coupons are fresh and valid.\n"
            "3. All sales are final. No refunds, no replacements.\n"
            "4. If coupon shows redeemed, try after some time (10-15 min).\n"
            "5. If there is a genuine issue and you recorded full screen from payment to applying, you can contact support."
        )
        await update.message.reply_text(terms, reply_markup=get_agree_decline_keyboard())
    elif text == "📦 My Orders":
        orders = supabase.table('orders').select('*').eq('user_id', user.id).order('created_at', desc=True).limit(10).execute()
        if not orders.data:
            await update.message.reply_text("You have no orders yet.")
        else:
            msg = "Your last orders:\n"
            for o in orders.data:
                msg += f"Order {o['order_id']}: {o['coupon_type']} x{o['quantity']} - {o['status']}\n"
            await update.message.reply_text(msg)
    elif text == "📜 Disclaimer":
        disclaimer = (
            "1. 🕒 IF CODE SHOW REDEEMED: Wait For 12–13 min Because All Codes Are Checked Before We Add.\n"
            "2. ⚡️ DELIVERY: codes are delivered immediately after payment confirmation.\n"
            "3. 🚫 NO REFUNDS: All sales final. No refunds/replacements for any codes.\n"
            "4. ❌ SUPPORT: For issues, a full screen-record from purchase to application is required."
        )
        await update.message.reply_text(disclaimer)
    elif text == "🆘 Support":
        await update.message.reply_text("🆘 Support Contact:\n━━━━━━━━━━━━━━\n@Raunso_shein_bot")
    elif text == "📢 Our Channels":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("@VIPAMMER", url="https://t.me/VIPAMMER")]])
        await update.message.reply_text("📢 Join our official channels for updates and deals:", reply_markup=keyboard)
    elif text == "🛠 Admin Panel" and user.id in ADMIN_IDS:
        await update.message.reply_text("Admin Panel", reply_markup=get_admin_reply_keyboard())
    elif user.id in ADMIN_IDS and text in ["➕ Add Coupon", "➖ Remove Coupon", "📊 Stock", "🎁 Get Free Code", "💰 Change Prices", "📢 Broadcast", "🕒 Last 10 Purchases", "🖼 Update QR", "🔛 Turn Off", "🔴 Turn On"]:
        await handle_admin_option(update, context, text)
    else:
        await update.message.reply_text("Use the menu buttons.")

async def handle_admin_option(update: Update, context: ContextTypes.DEFAULT_TYPE, option: str):
    if option == "➕ Add Coupon":
        context.user_data.clear()
        await update.message.reply_text("Select coupon type to add:", reply_markup=get_coupon_type_admin_keyboard('add'))
    elif option == "➖ Remove Coupon":
        context.user_data.clear()
        await update.message.reply_text("Select coupon type to remove:", reply_markup=get_coupon_type_admin_keyboard('remove'))
    elif option == "📊 Stock":
        msg = "Current Stock:\n"
        for ct in COUPON_TYPES:
            count = supabase.table('coupons').select('*', count='exact').eq('type', ct).eq('is_used', False).execute()
            stock = count.count if hasattr(count, 'count') else 0
            msg += f"{ct} Off: {stock}\n"
        await update.message.reply_text(msg, reply_markup=get_admin_reply_keyboard())
    elif option == "🎁 Get Free Code":
        context.user_data.clear()
        await update.message.reply_text("Select coupon type to get free codes:", reply_markup=get_coupon_type_admin_keyboard('free'))
    elif option == "💰 Change Prices":
        context.user_data.clear()
        await update.message.reply_text("Select coupon type to change prices:", reply_markup=get_coupon_type_admin_keyboard('prices'))
    elif option == "📢 Broadcast":
        context.user_data.clear()
        context.user_data['broadcast'] = True
        await update.message.reply_text("Send the message you want to broadcast to all users:")
    elif option == "🕒 Last 10 Purchases":
        orders = supabase.table('orders').select('*').order('created_at', desc=True).limit(10).execute()
        if not orders.data:
            await update.message.reply_text("No orders yet.")
        else:
            msg = "Last 10 purchases:\n"
            for o in orders.data:
                user = supabase.table('users').select('username').eq('user_id', o['user_id']).execute()
                username = user.data[0]['username'] if user.data else 'Unknown'
                msg += f"{o['order_id']}: {username} - {o['coupon_type']} x{o['quantity']} - {o['status']} - {o['created_at'][:19]}\n"
            await update.message.reply_text(msg, reply_markup=get_admin_reply_keyboard())
    elif option == "🖼 Update QR":
        context.user_data.clear()
        context.user_data['awaiting_qr'] = True
        await update.message.reply_text("Send the new QR code image.")
    elif option in ["🔛 Turn Off", "🔴 Turn On"]:
        status = supabase.table('settings').select('value').eq('key', 'bot_status').execute()
        current = status.data[0]['value'] if status.data else 'on'
        new_status = 'off' if current == 'on' else 'on'
        supabase.table('settings').upsert({'key': 'bot_status', 'value': new_status}).execute()
        await update.message.reply_text(f"Bot status changed to {new_status.upper()}.", reply_markup=get_admin_reply_keyboard())

async def terms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()
    if query.data == "agree_terms":
        await query.edit_message_text("🛒 Select a coupon type:", reply_markup=get_coupon_type_keyboard())
    else:
        await query.edit_message_text("Thanks for using the bot. Goodbye!")

async def coupon_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()
    ctype = query.data.split('_')[1]
    context.user_data['coupon_type'] = ctype

    count = supabase.table('coupons').select('*', count='exact').eq('type', ctype).eq('is_used', False).execute()
    stock = count.count if hasattr(count, 'count') else 0
    await query.edit_message_text(
        f"🏷️ {ctype} Off\n📦 Available stock: {stock}\n\n"
        f"Please enter the quantity (maximum {MAX_QUANTITY}):"
    )
    return CUSTOM_QUANTITY

async def custom_quantity_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return ConversationHandler.END
    try:
        qty = int(update.message.text)
        if qty <= 0:
            raise ValueError
        if qty > MAX_QUANTITY:
            await update.message.reply_text(f"❌ You can order at most {MAX_QUANTITY} coupons per transaction. Please enter a lower quantity (1-{MAX_QUANTITY}):")
            return CUSTOM_QUANTITY
        ctype = context.user_data.get('coupon_type')
        if not ctype:
            await update.message.reply_text("Error: coupon type not set. Please start over.")
            return ConversationHandler.END
        count = supabase.table('coupons').select('*', count='exact').eq('type', ctype).eq('is_used', False).execute()
        stock = count.count if hasattr(count, 'count') else 0
        if stock < qty:
            await update.message.reply_text(f"❌ Only {stock} codes available. Please enter a lower quantity (1-{MAX_QUANTITY}):")
            return CUSTOM_QUANTITY
        await process_quantity(update, context, qty)
    except:
        await update.message.reply_text("Invalid number. Please enter a valid quantity (1-5):")
        return CUSTOM_QUANTITY
    return ConversationHandler.END

async def process_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE, qty):
    ctype = context.user_data['coupon_type']
    count = supabase.table('coupons').select('*', count='exact').eq('type', ctype).eq('is_used', False).execute()
    stock = count.count if hasattr(count, 'count') else 0
    if stock < qty:
        await (update.message or update.callback_query.message).reply_text(f"❌ Only {stock} codes available for {ctype} Off.")
        return

    prices = supabase.table('prices').select('*').eq('coupon_type', ctype).execute()
    if not prices.data:
        await (update.message or update.callback_query.message).reply_text("Price error.")
        return
    p = prices.data[0]
    if qty <= 1:
        price_per = p['price_1']
    elif qty <= 5:
        price_per = p['price_5']
    elif qty <= 10:
        price_per = p['price_10']
    else:
        price_per = p['price_20']
    total = price_per * qty

    order_id = generate_order_id()
    context.user_data['order_id'] = order_id
    context.user_data['qty'] = qty
    context.user_data['price_per'] = price_per
    context.user_data['total'] = total

    supabase.table('orders').insert({
        'order_id': order_id,
        'user_id': update.effective_user.id,
        'coupon_type': ctype,
        'quantity': qty,
        'total_price': total,
        'status': 'pending'
    }).execute()

    qr_setting = supabase.table('settings').select('value').eq('key', 'qr_image').execute()
    qr_file_id = qr_setting.data[0]['value'] if qr_setting.data and qr_setting.data[0]['value'] else None

    invoice_text = (
        f"🧾 INVOICE\n━━━━━━━━━━━━━━\n"
        f"🆔 {order_id}\n"
        f"📦 {ctype} Off (x{qty})\n"
        f"💰 Pay Exactly: ₹{total}\n"
        f"⚠️ CRITICAL: You MUST pay exact amount. Do not ignore the paise (decimals), or the bot will NOT find your payment!\n\n"
        f"⏳ QR valid for 10 minutes."
    )

    if qr_file_id:
        await (update.message or update.callback_query.message).reply_photo(photo=qr_file_id, caption=invoice_text)
    else:
        await (update.message or update.callback_query.message).reply_text(invoice_text + "\n\n(QR not set by admin yet)")

    verify_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Verify Payment", callback_data=f"verify_{order_id}")]])
    await (update.message or update.callback_query.message).reply_text("After payment, click Verify.", reply_markup=verify_keyboard)

# --- Payment verification flow (UTR instead of payer name) ---
async def verify_payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    order_id = query.data.split('_')[1]
    context.user_data['verify_order_id'] = order_id
    await query.edit_message_text("Please enter the UTR number (Transaction ID) of your payment:")
    return WAITING_UTR

async def utr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return ConversationHandler.END
    context.user_data['utr_number'] = update.message.text
    await update.message.reply_text("Please send the screenshot of the payment:")
    return WAITING_PAYMENT_SCREENSHOT

async def payment_screenshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return ConversationHandler.END
    photo = update.message.photo[-1]
    file_id = photo.file_id
    context.user_data['screenshot_file_id'] = file_id
    order_id = context.user_data['verify_order_id']

    order = supabase.table('orders').select('*').eq('order_id', order_id).execute()
    if not order.data:
        await update.message.reply_text("Order not found.")
        return ConversationHandler.END
    o = order.data[0]

    admin_list = ADMIN_IDS
    user_mention = f"@{update.effective_user.username}" if update.effective_user.username else f"{update.effective_user.first_name}"
    utr_number = context.user_data['utr_number']

    admin_msg = (
        f"Payment verification requested:\n"
        f"User: {user_mention} (ID: {update.effective_user.id})\n"
        f"UTR Number: {utr_number}\n"
        f"Order: {o['order_id']}\n"
        f"Type: {o['coupon_type']} x{o['quantity']}\n"
        f"Total: ₹{o['total_price']}\n\n"
        f"Accept or Decline?"
    )
    accept_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept", callback_data=f"accept_{o['order_id']}"),
         InlineKeyboardButton("❌ Decline", callback_data=f"decline_{o['order_id']}")]
    ])

    for admin_id in admin_list:
        try:
            await context.bot.send_photo(admin_id, photo=file_id, caption=admin_msg, reply_markup=accept_keyboard)
        except Exception as e:
            logging.error(f"Failed to send to admin {admin_id}: {e}")

    await update.message.reply_text("Verification request sent to admin. Please wait for approval.")

    context.user_data.pop('verify_order_id', None)
    context.user_data.pop('utr_number', None)
    context.user_data.pop('screenshot_file_id', None)

    return ConversationHandler.END

# --- Admin accept/decline with confirmation messages ---
async def admin_accept_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_bot_status(update, context):
        return
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action = data[0]
    order_id = data[1]

    order = supabase.table('orders').select('*').eq('order_id', order_id).execute()
    if not order.data:
        await query.edit_message_text("Order not found.")
        return
    o = order.data[0]

    if o['status'] != 'pending':
        await query.edit_message_text(
            f"❌ This order ({order_id}) has already been processed (status: {o['status']}).\n"
            "No further action is possible."
        )
        return

    if action == "accept":
        coupons = supabase.table('coupons').select('*').eq('type', o['coupon_type']).eq('is_used', False).limit(o['quantity']).execute()
        if len(coupons.data) < o['quantity']:
            await query.edit_message_text("❌ Insufficient stock! Cannot accept payment.")
            return

        codes = [c['code'] for c in coupons.data]
        for c in coupons.data:
            supabase.table('coupons').update({
                'is_used': True,
                'used_by': o['user_id'],
                'used_at': datetime.utcnow().isoformat()
            }).eq('id', c['id']).execute()

        supabase.table('orders').update({'status': 'completed'}).eq('order_id', order_id).execute()

        codes_text = "\n".join(codes)
        await context.bot.send_message(
            o['user_id'],
            f"✅ Payment accepted! Here are your codes:\n{codes_text}\n\nThanks for purchasing!"
        )

        await query.edit_message_text(f"✅ Order {order_id} has been approved. Codes sent to user.")
        await query.message.reply_text(f"You approved the payment for order {order_id}.")
    else:  # decline
        supabase.table('orders').update({'status': 'declined'}).eq('order_id', order_id).execute()
        await context.bot.send_message(
            o['user_id'],
            "❌ Your payment has been declined by admin. If there is any issue, contact support."
        )
        await query.edit_message_text(f"❌ Order {order_id} has been declined.")
        await query.message.reply_text(f"You declined the payment for order {order_id}.")

# ==================== ADMIN MESSAGE HANDLER ====================
async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    text = update.message.text if update.message.text else None
    photo = update.message.photo[-1] if update.message.photo else None

    if context.user_data.get('broadcast'):
        users = supabase.table('users').select('user_id').execute()
        success = 0
        for u in users.data:
            try:
                await context.bot.send_message(u['user_id'], text)
                success += 1
            except:
                pass
        await update.message.reply_text(f"Broadcast sent to {success}/{len(users.data)} users.", reply_markup=get_admin_reply_keyboard())
        context.user_data.pop('broadcast', None)
        return

    if context.user_data.get('awaiting_qr'):
        if photo:
            file_id = photo.file_id
            supabase.table('settings').upsert({'key': 'qr_image', 'value': file_id}).execute()
            await update.message.reply_text("QR code updated.", reply_markup=get_admin_reply_keyboard())
            context.user_data.pop('awaiting_qr', None)
        else:
            await update.message.reply_text("Please send an image.")
        return

    if 'admin_action' in context.user_data:
        action = context.user_data['admin_action']
        if action[0] == 'add':
            ctype = action[1]
            if not text:
                await update.message.reply_text("Please send the coupon codes as text.")
                return
            codes = text.strip().split('\n')
            for code in codes:
                code = code.strip()
                if code:
                    supabase.table('coupons').insert({'code': code, 'type': ctype}).execute()
            await update.message.reply_text(f"Coupons added successfully to {ctype} Off.", reply_markup=get_admin_reply_keyboard())
            context.user_data.pop('admin_action', None)

        elif action[0] == 'remove':
            ctype = action[1]
            try:
                num = int(text)
                coupons = supabase.table('coupons').select('id').eq('type', ctype).eq('is_used', False).order('id').limit(num).execute()
                ids = [c['id'] for c in coupons.data]
                if ids:
                    supabase.table('coupons').delete().in_('id', ids).execute()
                await update.message.reply_text(f"Removed {len(ids)} coupons from {ctype} Off.", reply_markup=get_admin_reply_keyboard())
            except:
                await update.message.reply_text("Invalid number.", reply_markup=get_admin_reply_keyboard())
            context.user_data.pop('admin_action', None)

        elif action[0] == 'free':
            ctype = action[1]
            try:
                num = int(text)
                coupons = supabase.table('coupons').select('code').eq('type', ctype).eq('is_used', False).limit(num).execute()
                if len(coupons.data) < num:
                    await update.message.reply_text(f"Only {len(coupons.data)} available.", reply_markup=get_admin_reply_keyboard())
                codes = [c['code'] for c in coupons.data]
                for c in coupons.data:
                    supabase.table('coupons').update({
                        'is_used': True,
                        'used_by': update.effective_user.id,
                        'used_at': datetime.utcnow().isoformat()
                    }).eq('code', c['code']).execute()
                await update.message.reply_text(f"Here are your free codes:\n" + "\n".join(codes), reply_markup=get_admin_reply_keyboard())
            except:
                await update.message.reply_text("Invalid number.", reply_markup=get_admin_reply_keyboard())
            context.user_data.pop('admin_action', None)

        elif action[0] == 'price':
            ctype = action[1]
            qty = action[2]
            try:
                new_price = int(text)
                col = f"price_{qty}"
                supabase.table('prices').update({col: new_price}).eq('coupon_type', ctype).execute()
                await update.message.reply_text(f"Price updated for {ctype} Off, {qty} Qty: ₹{new_price}", reply_markup=get_admin_reply_keyboard())
            except:
                await update.message.reply_text("Invalid number.", reply_markup=get_admin_reply_keyboard())
            context.user_data.pop('admin_action', None)

# ==================== ADMIN CALLBACK ====================
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("Unauthorized.")
        return

    data = query.data

    context.user_data.pop('broadcast', None)
    context.user_data.pop('awaiting_qr', None)

    if data.startswith('admin_add_'):
        ctype = data.split('_')[2]
        context.user_data.clear()
        context.user_data['admin_action'] = ('add', ctype)
        await query.edit_message_text(f"Send the coupon codes for {ctype} Off (one per line):")
    elif data.startswith('admin_remove_'):
        ctype = data.split('_')[2]
        context.user_data.clear()
        context.user_data['admin_action'] = ('remove', ctype)
        await query.edit_message_text(f"How many codes to remove from {ctype} Off? (send a number)")
    elif data.startswith('admin_free_'):
        ctype = data.split('_')[2]
        context.user_data.clear()
        context.user_data['admin_action'] = ('free', ctype)
        await query.edit_message_text(f"How many free codes from {ctype} Off? (send a number)")
    elif data.startswith('admin_prices_'):
        ctype = data.split('_')[2]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 Qty", callback_data=f"admin_price_qty_{ctype}_1")],
            [InlineKeyboardButton("5 Qty", callback_data=f"admin_price_qty_{ctype}_5")],
            [InlineKeyboardButton("10 Qty", callback_data=f"admin_price_qty_{ctype}_10")],
            [InlineKeyboardButton("20 Qty", callback_data=f"admin_price_qty_{ctype}_20")]
        ])
        await query.edit_message_text(f"Select quantity for {ctype} Off price change:", reply_markup=keyboard)
    elif data.startswith('admin_price_qty_'):
        parts = data.split('_')
        ctype = parts[3]
        qty = parts[4]
        context.user_data.clear()
        context.user_data['admin_action'] = ('price', ctype, qty)
        await query.edit_message_text(f"Enter new price for {ctype} Off, {qty} Qty:")
    else:
        await query.edit_message_text("Unknown action.")

# ==================== CONVERSATION HANDLERS ====================
conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(coupon_type_callback, pattern="^ctype_")],
    states={
        CUSTOM_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_quantity_input)]
    },
    fallbacks=[],
    per_message=False
)

payment_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(verify_payment_start, pattern="^verify_")],
    states={
        WAITING_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, utr_handler)],
        WAITING_PAYMENT_SCREENSHOT: [MessageHandler(filters.PHOTO, payment_screenshot_handler)]
    },
    fallbacks=[],
    per_message=False
)

# ==================== BACKGROUND EVENT LOOP ====================
bot_loop = asyncio.new_event_loop()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=start_background_loop, args=(bot_loop,), daemon=True).start()

# ==================== TELEGRAM APPLICATION SETUP ====================
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("admin", admin_panel))

telegram_app.add_handler(conv_handler)
telegram_app.add_handler(payment_conv_handler)

telegram_app.add_handler(CallbackQueryHandler(terms_callback, pattern="^(agree|decline)_terms$"))
telegram_app.add_handler(CallbackQueryHandler(admin_accept_decline, pattern="^(accept|decline)_"))
telegram_app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in ADMIN_IDS and context.user_data.get('awaiting_qr'):
        await admin_message_handler(update, context)
telegram_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))

async def init_app():
    await telegram_app.initialize()

future = asyncio.run_coroutine_threadsafe(init_app(), bot_loop)
future.result()

# ==================== FLASK WEBHOOK ENDPOINT ====================
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), bot_loop)
    return 'ok', 200

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    url = request.url_root.rstrip('/') + '/webhook'
    asyncio.run_coroutine_threadsafe(telegram_app.bot.set_webhook(url=url), bot_loop)
    return f'Webhook set to {url}', 200

@app.route('/')
def home():
    return "Bot is running!", 200

# ==================== AUTOMATIC WEBHOOK SETUP ON STARTUP ====================
def set_webhook_automatically():
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if external_url:
        webhook_url = external_url.rstrip('/') + '/webhook'
        logging.info(f"Setting webhook to {webhook_url}")
        async def _set():
            await telegram_app.bot.set_webhook(url=webhook_url)
            logging.info("Webhook set successfully")
        asyncio.run_coroutine_threadsafe(_set(), bot_loop)
    else:
        logging.info("RENDER_EXTERNAL_URL not set, skipping automatic webhook setup")

set_webhook_automatically()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False, use_reloader=False)
