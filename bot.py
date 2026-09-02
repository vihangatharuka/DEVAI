import json
import os
import time
import random
import logging
import asyncio
import nest_asyncio
from typing import Union, Dict, Any, List, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters
)

# Enable nested asyncio event loops if running in notebook environments
nest_asyncio.apply()

# Bot Token and Admin Authorization
BOT_TOKEN = ""
ADMIN_ID =   # Telegram Admin ID

# File Paths & Directory Configurations for Data Persistence
REPLIES_FILE = "autoreplies.json"
SETTINGS_FILE = "settings.json"
HISTORY_DIR = "chat_histories"

# Ensure chat histories directory exists
os.makedirs(HISTORY_DIR, exist_ok=True)

# Delay Configuration (Randomized human typing simulation)
MIN_DELAY_SECONDS = 3
MAX_DELAY_SECONDS = 12
ITEMS_PER_PAGE = 5  # Pagination limit for Admin Keyword Manager

# Default Settings Schema
DEFAULT_SETTINGS = {
    "offline_mode": False,
    "offline_message": "👋 I am currently offline. I will get back to you as soon as possible!",
    "cooldown_minutes": 10
}

# In-memory Tracker for Offline Reply Timestamps per chat_id
OFFLINE_TRACKER: Dict[int, float] = {}

# Footer signature appended to outgoing business text replies
RESPONSE_FOOTER = ""

# Configure System Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global thread lock for file updates
db_lock = asyncio.Lock()

async def load_chat_history(chat_id: int) -> List[Dict[str, Any]]:
    """Safely loads up to 20 past conversation messages for a specific chat from its JSON file."""
    filepath = os.path.join(HISTORY_DIR, f"{chat_id}.json")
    async with db_lock:
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading history JSON for chat {chat_id}: {e}")
            return []

async def save_chat_history(chat_id: int, history: List[Dict[str, Any]]):
    """Safely writes chat history capped at 20 messages into an individual JSON file."""
    filepath = os.path.join(HISTORY_DIR, f"{chat_id}.json")
    trimmed_history = history[-20:]
    async with db_lock:
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(trimmed_history, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Error saving history JSON for chat {chat_id}: {e}")

async def load_replies() -> Dict[str, Any]:
    """Safely loads auto-replies from JSON database with file locking."""
    async with db_lock:
        if not os.path.exists(REPLIES_FILE):
            with open(REPLIES_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=4)
            return {}
        try:
            with open(REPLIES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading replies JSON: {e}")
            return {}

async def save_replies(data: Dict[str, Any]):
    """Safely writes auto-replies to JSON database with file locking."""
    async with db_lock:
        try:
            with open(REPLIES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Error saving replies JSON: {e}")

async def add_auto_reply(keyword: str, content: Union[str, Dict[str, str]]):
    """Adds or updates a text or sticker auto-reply rule."""
    data = await load_replies()
    key = keyword.lower().strip()
    data[key] = content
    await save_replies(data)

async def delete_auto_reply(keyword: str) -> bool:
    """Deletes a specific keyword auto-reply safely."""
    data = await load_replies()
    key = keyword.lower().strip()
    if key in data:
        del data[key]
        await save_replies(data)
        return True
    return False

async def load_settings() -> Dict[str, Any]:
    """Loads system settings (Offline Mode status, Custom Message, etc.)."""
    async with db_lock:
        if not os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_SETTINGS, f, ensure_ascii=False, indent=4)
            return dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
                return {**DEFAULT_SETTINGS, **settings}
        except Exception as e:
            logger.error(f"Error reading settings JSON: {e}")
            return dict(DEFAULT_SETTINGS)

async def save_settings(settings: Dict[str, Any]):
    """Saves updated settings to JSON file."""
    async with db_lock:
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Error saving settings JSON: {e}")

async def safe_send_business_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, conn_id: str):
    """Delivers Telegram Business messages with automatic Markdown parsing fallback."""
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            business_connection_id=conn_id
        )
    except BadRequest as e:
        logger.warning(f"Markdown parse error caught: {e}. Retrying message delivery in plain text.")
        try:
            clean_text = text.replace("*", "").replace("_", "").replace("`", "")
            await context.bot.send_message(
                chat_id=chat_id,
                text=clean_text,
                business_connection_id=conn_id
            )
        except Exception as err:
            logger.error(f"Failed to deliver business text message: {err}")
    except Exception as e:
        logger.error(f"Error sending business message: {e}")

async def safe_send_business_sticker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, sticker_id: str, conn_id: str):
    """Delivers Telegram Business stickers with exception protection."""
    try:
        await context.bot.send_sticker(
            chat_id=chat_id,
            sticker=sticker_id,
            business_connection_id=conn_id
        )
    except Exception as e:
        logger.error(f"Error sending business sticker {sticker_id}: {e}")

def build_admin_main_menu(total_kw: int, settings: Dict[str, Any]):
    """Builds the main visual dashboard inline keyboard."""
    offline_status = "🔴 Offline Mode: ON" if settings.get("offline_mode") else "🟢 Offline Mode: OFF"
    cooldown_val = settings.get("cooldown_minutes", 10)
    
    keyboard = [
        [
            InlineKeyboardButton("📋 Manage Keywords", callback_data="view_keywords_0"),
            InlineKeyboardButton("➕ Add Keyword / Sticker", callback_data="wizard_start_add")
        ],
        [
            InlineKeyboardButton(offline_status, callback_data="toggle_offline")
        ],
        [
            InlineKeyboardButton("✏️ Set Offline Msg", callback_data="wizard_start_offline_msg"),
            InlineKeyboardButton(f"⏱️ Set Cooldown ({cooldown_val}m)", callback_data="wizard_start_cooldown")
        ],
        [
            InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def build_keywords_paginated_menu(page: int = 0):
    """Builds a paginated visual keyword manager with deletion buttons."""
    replies = await load_replies()
    keys = list(replies.keys())
    total_items = len(keys)
    
    if total_items == 0:
        keyboard = [
            [InlineKeyboardButton("➕ Add Keyword", callback_data="wizard_start_add")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ]
        return "📭 **No Auto-Replies Saved!**\n\nYou haven't added any keywords yet.", InlineKeyboardMarkup(keyboard)

    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_keys = keys[start_idx:end_idx]

    text = f"⚙️ **Auto-Reply Keyword Manager**\n"
    text += f"📊 Total Rules: `{total_items}` | Page `{page + 1}/{total_pages}`\n\n"

    keyboard = []
    for idx, key in enumerate(page_keys, start=start_idx + 1):
        item = replies[key]
        if isinstance(item, dict) and item.get("type") == "sticker":
            type_label = "🎭 [STICKER]"
            preview = f"File ID: {item.get('content')[:15]}..."
        elif isinstance(item, dict) and item.get("type") == "text":
            type_label = "💬 [TEXT]"
            preview = item.get('content')[:25] + "..." if len(item.get('content')) > 25 else item.get('content')
        else:
            type_label = "💬 [TEXT]"
            preview = str(item)[:25] + "..." if len(str(item)) > 25 else str(item)

        text += f"*{idx}. {key}* {type_label}\n└ _{preview}_\n\n"
        
        keyboard.append([
            InlineKeyboardButton(f"🔑 {key}", callback_data=f"info_{key}"),
            InlineKeyboardButton(f"🗑️ Delete", callback_data=f"confirm_del_{key}_{page}")
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"view_keywords_{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"view_keywords_{page + 1}"))
    
    keyboard.append(nav_buttons)
    keyboard.append([
        InlineKeyboardButton("➕ Add New", callback_data="wizard_start_add"),
        InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")
    ])

    return text, InlineKeyboardMarkup(keyboard)

async def cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the active visual creation wizard safely."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
    context.user_data.clear()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")]])
    if query:
        try:
            await query.edit_message_text("❌ Action cancelled.", reply_markup=kb)
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text("❌ Action cancelled.", reply_markup=kb)

async def admin_dm_wizard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles step-by-step interactive visual wizard for adding keywords or offline settings."""
    if update.effective_user.id != ADMIN_ID:
        return

    state = context.user_data.get("wizard_state")
    if not state or not update.message:
        return

    if state == "WAIT_OFFLINE_MSG":
        if not update.message.text:
            await update.message.reply_text("⚠️ Please send a valid text message for your offline reply.")
            return

        new_msg = update.message.text.strip()
        settings = await load_settings()
        settings["offline_message"] = new_msg
        await save_settings(settings)

        context.user_data.clear()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]])
        await update.message.reply_text(
            f"✅ *Offline Message Updated Successfully!*\n\n"
            f"**New Message:**\n_{new_msg}_",
            parse_mode="Markdown",
            reply_markup=kb
        )

    elif state == "WAIT_COOLDOWN_TIME":
        if not update.message.text or not update.message.text.strip().isdigit():
            await update.message.reply_text("⚠️ Please send a valid positive number for cooldown minutes (e.g. `5`, `10`, `30`).")
            return

        new_cooldown = int(update.message.text.strip())
        if new_cooldown < 1:
            await update.message.reply_text("⚠️ Cooldown time must be at least 1 minute.")
            return

        settings = await load_settings()
        settings["cooldown_minutes"] = new_cooldown
        await save_settings(settings)

        context.user_data.clear()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]])
        await update.message.reply_text(
            f"✅ *Offline Cooldown Updated Successfully!*\n\n"
            f"**New Cooldown Duration:** `{new_cooldown}` minutes per chat.",
            parse_mode="Markdown",
            reply_markup=kb
        )

    elif state == "WAIT_KEYWORD":
        if not update.message.text:
            await update.message.reply_text("⚠️ Please send a valid text keyword (e.g. `price`, `hello`, `address`).")
            return

        keyword = update.message.text.strip().lower()
        context.user_data["wizard_keyword"] = keyword
        context.user_data["wizard_state"] = "WAIT_CONTENT"

        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Wizard", callback_data="wizard_cancel")]])
        await update.message.reply_text(
            f"🎯 *Trigger Keyword Set:* `{keyword}`\n\n"
            "Now send the auto-reply content for this keyword:\n"
            "• Type a **Text Message** 💬\n"
            "• OR send any **Sticker** directly 🎭",
            parse_mode="Markdown",
            reply_markup=cancel_kb
        )

    elif state == "WAIT_CONTENT":
        keyword = context.user_data.get("wizard_keyword")
        if not keyword:
            context.user_data.clear()
            return

        if update.message.sticker:
            sticker_id = update.message.sticker.file_id
            await add_auto_reply(keyword, {"type": "sticker", "content": sticker_id})
            resp_desc = f"🎭 Sticker (`{sticker_id[:15]}...`)"
        elif update.message.text:
            text_resp = update.message.text.strip()
            await add_auto_reply(keyword, {"type": "text", "content": text_resp})
            resp_desc = f"💬 `{text_resp}`"
        else:
            await update.message.reply_text("⚠️ Please send either a text message or a sticker.")
            return

        context.user_data.clear()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View All Keywords", callback_data="view_keywords_0")],
            [InlineKeyboardButton("➕ Add Another", callback_data="wizard_start_add")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
        ])
        await update.message.reply_text(
            f"✅ *Auto-Reply Saved Successfully!*\n\n"
            f"• **Keyword:** `{keyword}`\n"
            f"• **Content:** {resp_desc}\n\n"
            "_This rule is now live and active for your Telegram Business account!_",
            parse_mode="Markdown",
            reply_markup=kb
        )

async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming Telegram Business customer messages with keyword matching & 10-min offline limit."""
    try:
        b_msg = update.business_message
        if not b_msg:
            return

        user_text = (b_msg.text or b_msg.caption or "").strip()
        if not user_text:
            return

        chat_id = b_msg.chat.id
        conn_id = b_msg.business_connection_id

        # 1. Non-blocking Randomized Typing Delay
        reply_delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        await asyncio.sleep(reply_delay)

        # 2. Load past chat history from individual JSON file
        chat_history = await load_chat_history(chat_id)

        # 3. Check JSON database for matching keyword
        replies = await load_replies()
        matched_item = replies.get(user_text.lower())

        if matched_item:
            # Handle Sticker Auto-Reply
            if isinstance(matched_item, dict) and matched_item.get("type") == "sticker":
                sticker_id = matched_item.get("content")
                await safe_send_business_sticker(context, chat_id, sticker_id, conn_id)

                chat_history.append({"role": "user", "parts": [user_text]})
                chat_history.append({"role": "model", "parts": ["[Sticker Sent]"]})
                await save_chat_history(chat_id, chat_history)
                return

            # Handle Text Auto-Reply
            if isinstance(matched_item, dict) and matched_item.get("type") == "text":
                response_text = matched_item.get("content")
            else:
                response_text = str(matched_item)

            full_response = f"{response_text}{RESPONSE_FOOTER}"
            await safe_send_business_message(context, chat_id, full_response, conn_id)

            chat_history.append({"role": "user", "parts": [user_text]})
            chat_history.append({"role": "model", "parts": [full_response]})
            await save_chat_history(chat_id, chat_history)
            return

        # 4. If NO keyword match -> Check if Offline Mode is Active
        settings = await load_settings()
        if settings.get("offline_mode", False):
            current_time = time.time()
            cooldown_seconds = settings.get("cooldown_minutes", 10) * 60
            last_sent_time = OFFLINE_TRACKER.get(chat_id, 0)

            # Only send offline message if 10 minutes have passed since the last offline response to this chat
            if current_time - last_sent_time >= cooldown_seconds:
                offline_msg = settings.get("offline_message", "I am currently offline.")
                full_response = f"{offline_msg}{RESPONSE_FOOTER}"
                
                await safe_send_business_message(context, chat_id, full_response, conn_id)

                # Update timestamp for this specific chat
                OFFLINE_TRACKER[chat_id] = current_time

                chat_history.append({"role": "user", "parts": [user_text]})
                chat_history.append({"role": "model", "parts": [full_response]})
                await save_chat_history(chat_id, chat_history)

    except Exception as e:
        logger.error(f"Error handling business message safely: {e}", exc_info=True)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opens the main interactive Admin Dashboard in Telegram DM."""
    if update.effective_user.id != ADMIN_ID:
        return
    
    total_kw = len(await load_replies())
    settings = await load_settings()
    
    status_str = "🔴 ON (Auto-Replying)" if settings.get("offline_mode") else "🟢 OFF (Manual Mode)"
    cooldown_val = settings.get("cooldown_minutes", 10)
    text = (
        "🤖 *Telegram Business Keyword & Offline Admin Panel*\n\n"
        f"• *Status:* 🟢 Connected & Active\n"
        f"• *Saved Auto-Replies:* `{total_kw}` Keyword Rules\n"
        f"• *Offline Mode:* {status_str}\n"
        f"• *Offline Cooldown:* `{cooldown_val}` Minutes per Chat\n"
        f"• *Current Offline Msg:* _{settings.get('offline_message')}_\n\n"
        "Manage keywords and offline settings using the dashboard below:"
    )
    await update.message.reply_text(
        text=text,
        parse_mode="Markdown",
        reply_markup=build_admin_main_menu(total_kw, settings)
    )

async def add_reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command fallback to add text auto-reply: /add <keyword> | <response>"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    try:
        raw_text = update.message.text.split("/add ", 1)[1]
        keyword, response = raw_text.split("|", 1)
        keyword = keyword.strip()
        response = response.strip()

        await add_auto_reply(keyword, {"type": "text", "content": response})
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 View Keywords", callback_data="view_keywords_0")],
            [InlineKeyboardButton("🔙 Admin Panel", callback_data="main_menu")]
        ])
        await update.message.reply_text(
            f"✅ *Text Auto-Reply Saved!*\n\n**Keyword:** `{keyword}`\n**Response:** {response}",
            parse_mode="Markdown",
            reply_markup=kb
        )
    except Exception:
        await update.message.reply_text(
            "❌ *Invalid Syntax!*\n\nUsage: `/add keyword | response text`\nExample: `/add price | Our packages start at $25.`",
            parse_mode="Markdown"
        )

async def del_reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to uniquely delete a keyword: /del <keyword>"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    try:
        keyword = update.message.text.split("/del ", 1)[1].strip()
        success = await delete_auto_reply(keyword)
        if success:
            await update.message.reply_text(f"🗑️ Deleted auto-reply for keyword `{keyword}`.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Keyword `{keyword}` not found.", parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("❌ Usage: `/del <keyword>`", parse_mode="Markdown")

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all visual inline button interactions safely."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        try:
            await query.answer("Unauthorized access.", show_alert=True)
        except Exception:
            pass
        return

    data = query.data
    try:
        await query.answer()
    except Exception:
        pass

    try:
        if data == "main_menu":
            context.user_data.clear()
            total_kw = len(await load_replies())
            settings = await load_settings()
            
            status_str = "🔴 ON (Auto-Replying)" if settings.get("offline_mode") else "🟢 OFF (Manual Mode)"
            cooldown_val = settings.get("cooldown_minutes", 10)
            text = (
                "🤖 *Telegram Business Keyword & Offline Admin Panel*\n\n"
                f"• *Status:* 🟢 Connected & Active\n"
                f"• *Saved Auto-Replies:* `{total_kw}` Keyword Rules\n"
                f"• *Offline Mode:* {status_str}\n"
                f"• *Offline Cooldown:* `{cooldown_val}` Minutes per Chat\n"
                f"• *Current Offline Msg:* _{settings.get('offline_message')}_\n\n"
                "Manage keywords and offline settings using the dashboard below:"
            )
            await query.edit_message_text(
                text=text,
                parse_mode="Markdown",
                reply_markup=build_admin_main_menu(total_kw, settings)
            )

        elif data == "toggle_offline":
            settings = await load_settings()
            settings["offline_mode"] = not settings.get("offline_mode", False)
            await save_settings(settings)
            
            new_status = "🔴 ON" if settings["offline_mode"] else "🟢 OFF"
            try:
                await query.answer(f"Offline Mode is now {new_status}", show_alert=True)
            except Exception:
                pass

            total_kw = len(await load_replies())
            status_str = "🔴 ON (Auto-Replying)" if settings["offline_mode"] else "🟢 OFF (Manual Mode)"
            cooldown_val = settings.get("cooldown_minutes", 10)
            text = (
                "🤖 *Telegram Business Keyword & Offline Admin Panel*\n\n"
                f"• *Status:* 🟢 Connected & Active\n"
                f"• *Saved Auto-Replies:* `{total_kw}` Keyword Rules\n"
                f"• *Offline Mode:* {status_str}\n"
                f"• *Offline Cooldown:* `{cooldown_val}` Minutes per Chat\n"
                f"• *Current Offline Msg:* _{settings.get('offline_message')}_\n\n"
                "Manage keywords and offline settings using the dashboard below:"
            )
            await query.edit_message_text(
                text=text,
                parse_mode="Markdown",
                reply_markup=build_admin_main_menu(total_kw, settings)
            )

        elif data == "wizard_start_offline_msg":
            context.user_data["wizard_state"] = "WAIT_OFFLINE_MSG"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Wizard", callback_data="wizard_cancel")]])
            await query.edit_message_text(
                "✏️ *Set Custom Offline Reply Message*\n\n"
                "Please type the custom message you want customers to receive when you are **Offline**:\n\n"
                "_Example: 'Hi there! I am currently away from my desk. I will reply as soon as I get back online.'_",
                parse_mode="Markdown",
                reply_markup=kb
            )

        elif data == "wizard_start_cooldown":
            context.user_data["wizard_state"] = "WAIT_COOLDOWN_TIME"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Wizard", callback_data="wizard_cancel")]])
            await query.edit_message_text(
                "⏱️ *Set Offline Response Cooldown*\n\n"
                "Please send the cooldown duration in **minutes** between automatic offline replies for a single chat:\n\n"
                "_Example: Send `10` for a 10-minute cooldown, or `5` for 5 minutes._",
                parse_mode="Markdown",
                reply_markup=kb
            )

        elif data == "wizard_start_add":
            context.user_data["wizard_state"] = "WAIT_KEYWORD"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Wizard", callback_data="wizard_cancel")]])
            await query.edit_message_text(
                "🧙‍♂️ *Interactive Auto-Reply Creator Wizard*\n\n"
                "Please send the **Trigger Keyword** you want to add (e.g., `price`, `hello`, `discount`):",
                parse_mode="Markdown",
                reply_markup=kb
            )

        elif data == "wizard_cancel":
            await cancel_wizard(update, context)

        elif data.startswith("view_keywords_"):
            page = int(data.split("_")[2])
            text, markup = await build_keywords_paginated_menu(page)
            await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=markup)

        elif data.startswith("confirm_del_"):
            parts = data.split("_")
            key_to_del = parts[2]
            page = int(parts[3])
            
            confirm_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ Delete '{key_to_del}'", callback_data=f"do_del_{key_to_del}_{page}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"view_keywords_{page}")
                ]
            ])
            await query.edit_message_text(
                text=f"⚠️ *Confirm Deletion*\n\nAre you sure you want to delete the rule for `'{key_to_del}'`?",
                parse_mode="Markdown",
                reply_markup=confirm_kb
            )

        elif data.startswith("do_del_"):
            parts = data.split("_")
            key_to_del = parts[2]
            page = int(parts[3])
            
            success = await delete_auto_reply(key_to_del)
            if success:
                try:
                    await query.answer(f"Deleted '{key_to_del}' successfully!", show_alert=True)
                except Exception:
                    pass
            
            text, markup = await build_keywords_paginated_menu(page)
            await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=markup)

        elif data.startswith("info_"):
            key = data.split("info_")[1]
            replies = await load_replies()
            item = replies.get(key, "Not found")
            if isinstance(item, dict) and item.get("type") == "sticker":
                info_str = f"Keyword: {key}\nType: Sticker\nFile ID: {item.get('content')}"
            else:
                resp = item.get("content") if isinstance(item, dict) else str(item)
                info_str = f"Keyword: {key}\nType: Text\nReply: {resp}"
            try:
                await query.answer(info_str, show_alert=True)
            except Exception:
                pass

    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Callback query BadRequest error: {e}")
    except Exception as e:
        logger.error(f"Error handling callback query: {e}")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catches all unhandled exceptions globally to maintain bot stability."""
    logger.error(f"Global Anti-Crash Handler caught exception: {context.error}", exc_info=context.error)

def main():
    """Initializes and runs the Telegram Business Auto-Reply Bot."""
    logger.info("Initializing Telegram Business Auto-Reply Bot...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Admin Dashboard Command Handlers
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("add", add_reply_cmd))
    app.add_handler(CommandHandler("del", del_reply_cmd))

    # Inline Keyboard Interaction Handler
    app.add_handler(CallbackQueryHandler(button_callback_handler))

    # Admin Wizard Message Handler (DM Text & Sticker inputs)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.ATTACHMENT) & ~filters.COMMAND,
            admin_dm_wizard_handler
        ),
        group=1
    )

    # Telegram Business Customer Messages Handler
    app.add_handler(
        MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE,
            handle_business_message
        ),
        group=2
    )

    # Global Anti-Crash Error Boundary
    app.add_error_handler(global_error_handler)

    logger.info("Bot successfully started and listening for Telegram Business updates!")
    app.run_polling()

if __name__ == "__main__":
    main()
