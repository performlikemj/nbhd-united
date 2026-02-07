"""Telegram message handlers — button-first UX."""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from apps.agents.models import AgentSession
from apps.agents.services import AgentRunner
from apps.tenants.services import provision_tenant
from .keyboards import confirm_reset_keyboard, main_menu_keyboard, settings_keyboard
from .models import TelegramBinding

logger = logging.getLogger(__name__)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — onboarding flow, bind to tenant."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Check if already bound
    binding = TelegramBinding.objects.filter(chat_id=chat_id).first()
    if binding:
        await update.message.reply_text(
            f"Welcome back, {user.first_name}! 👋\n\nHow can I help you today?",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Provision new tenant
    tenant, db_user = provision_tenant(
        display_name=user.first_name or user.username or "Friend",
        telegram_chat_id=chat_id,
        telegram_user_id=user.id,
        telegram_username=user.username,
        language=user.language_code or "en",
    )

    TelegramBinding.objects.create(
        tenant=tenant,
        chat_id=chat_id,
        username=user.username or "",
    )

    # Create first session
    AgentSession.objects.create(tenant=tenant, title="First Chat")

    await update.message.reply_text(
        f"Hey {user.first_name}! 🎉\n\n"
        "I'm your personal AI assistant from Neighborhood United.\n\n"
        "Just send me a message and I'll do my best to help. "
        "Use the buttons below to navigate.",
        reply_markup=main_menu_keyboard(),
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route text messages to the agent."""
    chat_id = update.effective_chat.id
    binding = TelegramBinding.objects.filter(chat_id=chat_id, is_active=True).first()
    if not binding:
        await update.message.reply_text("Please send /start to get started!")
        return

    # Get or create active session
    session = AgentSession.objects.filter(
        tenant=binding.tenant, is_active=True
    ).order_by("-updated_at").first()

    if not session:
        session = AgentSession.objects.create(tenant=binding.tenant, title="New Chat")

    # Run agent
    runner = AgentRunner()
    response_msg = runner.run(session=session, user_message=update.message.text)

    await update.message.reply_text(response_msg.content)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help."""
    await update.message.reply_text(
        "🤖 **Available Commands**\n\n"
        "/start — Start or restart the bot\n"
        "/help — Show this help message\n"
        "/reset — Clear current session\n"
        "/privacy — View privacy policy\n\n"
        "Or just send me a message!",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reset — confirm session clear."""
    await update.message.reply_text(
        "⚠️ This will start a fresh conversation. Your memory items are preserved.\n\n"
        "Are you sure?",
        reply_markup=confirm_reset_keyboard(),
    )


async def privacy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /privacy."""
    await update.message.reply_text(
        "🔒 **Your Privacy**\n\n"
        "• Your conversations are private and isolated\n"
        "• We never share your data with other users\n"
        "• You can delete your data at any time\n"
        "• We use AI models to respond to your messages\n"
        "• Messages are stored to provide conversation context\n\n"
        "Questions? Reach out to the Neighborhood United team.",
        parse_mode="Markdown",
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    binding = TelegramBinding.objects.filter(chat_id=chat_id).first()

    if query.data == "main_menu":
        await query.edit_message_text(
            "How can I help you?", reply_markup=main_menu_keyboard()
        )
    elif query.data == "new_chat":
        if binding:
            AgentSession.objects.filter(tenant=binding.tenant, is_active=True).update(is_active=False)
            AgentSession.objects.create(tenant=binding.tenant, title="New Chat")
        await query.edit_message_text(
            "✨ Fresh conversation started! Send me a message.",
            reply_markup=main_menu_keyboard(),
        )
    elif query.data == "settings":
        await query.edit_message_text("⚙️ Settings", reply_markup=settings_keyboard())
    elif query.data == "help":
        await query.edit_message_text(
            "🤖 Just send me a message and I'll help!\n\n"
            "Commands: /start /help /reset /privacy",
            reply_markup=main_menu_keyboard(),
        )
    elif query.data == "confirm_reset":
        if binding:
            AgentSession.objects.filter(tenant=binding.tenant, is_active=True).update(is_active=False)
            AgentSession.objects.create(tenant=binding.tenant, title="New Chat")
        await query.edit_message_text(
            "🔄 Session reset! Send me a message to start.",
            reply_markup=main_menu_keyboard(),
        )
    elif query.data == "privacy":
        await query.edit_message_text(
            "🔒 Your conversations are private and isolated. "
            "We never share data between users. You can delete your data anytime.",
            reply_markup=settings_keyboard(),
        )
