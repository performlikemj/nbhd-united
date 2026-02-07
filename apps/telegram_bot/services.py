"""Telegram bot setup and message sending."""
import logging

from django.conf import settings
from telegram import Bot

logger = logging.getLogger(__name__)


def get_bot() -> Bot:
    """Get a configured Telegram Bot instance."""
    return Bot(token=settings.TELEGRAM_BOT_TOKEN)


async def send_message(chat_id: int, text: str, **kwargs):
    """Send a message to a Telegram chat."""
    bot = get_bot()
    await bot.send_message(chat_id=chat_id, text=text, **kwargs)


async def set_webhook(url: str):
    """Set the Telegram webhook URL."""
    bot = get_bot()
    await bot.set_webhook(
        url=url,
        secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
    )
    logger.info("Telegram webhook set to %s", url)
