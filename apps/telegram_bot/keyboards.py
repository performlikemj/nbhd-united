"""Inline keyboard layouts for Telegram bot."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📝 New Chat", callback_data="new_chat"),
            InlineKeyboardButton("📋 My Chats", callback_data="my_chats"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Reset Session", callback_data="reset")],
        [InlineKeyboardButton("🔒 Privacy Policy", callback_data="privacy")],
        [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")],
    ])


def confirm_reset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, reset", callback_data="confirm_reset"),
            InlineKeyboardButton("❌ Cancel", callback_data="main_menu"),
        ],
    ])
