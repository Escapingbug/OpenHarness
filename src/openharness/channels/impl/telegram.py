"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
import time
from collections.abc import Callable

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity as TGMessageEntity,
    ReplyParameters,
    Update,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from telegramify_markdown import convert, split_entities

from openharness.channels.bus.events import OutboundMessage
from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.base import BaseChannel
from openharness.config.schema import TelegramConfig

logger = logging.getLogger(__name__)

# Telegram limit is 4096 UTF-16 code units; leave some margin
TELEGRAM_MAX_UTF16_LEN = 4000
TELEGRAM_POLL_STALL_SECONDS = 90.0
TELEGRAM_SEND_CONCURRENCY = 4

# Regex to extract Markdown tables from raw content (before convert()).
# Matches a table block: header row(s), separator row (---), and data rows.
_TABLE_BLOCK_RE = re.compile(
    r"(?:^[ \t]*\|[^\n]+\|[ \t]*$\n)+"  # header rows
    r"^[ \t]*\|[-: \t|]+\|[ \t]*$\n"  # separator row
    r"(?:^[ \t]*\|[^\n]+\|[ \t]*$\n?)*",  # data rows (optional trailing newline)
    re.MULTILINE,
)


class _TrackingHTTPXRequest(HTTPXRequest):
    """HTTPXRequest variant that records successful getUpdates calls."""

    def __init__(self, *args, on_get_updates_success: Callable[[], None], **kwargs):
        super().__init__(*args, **kwargs)
        self._on_get_updates_success = on_get_updates_success

    async def do_request(self, url: str, method: str, *args, **kwargs):
        result = await super().do_request(url, method, *args, **kwargs)
        if "/getUpdates" in url:
            self._on_get_updates_success()
        return result


def _split_by_tables(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into ordered segments: text blocks and table blocks.

    Returns a list of (kind, content) tuples where kind is 'text' or 'table'.
    """
    segments: list[tuple[str, str]] = []
    cursor = 0
    for m in _TABLE_BLOCK_RE.finditer(markdown):
        start, end = m.span()
        if start > cursor:
            segments.append(("text", markdown[cursor:start]))
        segments.append(("table", m.group(0)))
        cursor = end
    if cursor < len(markdown):
        segments.append(("text", markdown[cursor:]))
    return segments


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

    name = "telegram"

    # Commands registered with Telegram's command menu
    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("compact", "Compact conversation history"),
        BotCommand("status", "Show session status"),
        BotCommand("summary", "Summarize conversation history"),
        BotCommand("cost", "Show token usage and estimated cost"),
        BotCommand("usage", "Show usage and token estimates"),
        BotCommand("model", "Show or update the default model"),
        BotCommand("provider", "Show or switch provider profiles"),
        BotCommand("config", "Show or update configuration"),
        BotCommand("memory", "Inspect and manage project memory"),
        BotCommand("agents", "List or inspect agent and teammate tasks"),
        BotCommand("tasks", "Manage background tasks"),
        BotCommand("permissions", "Show or update permission mode"),
        BotCommand("allow", "Approve a pending permission request"),
        BotCommand("deny", "Deny a pending permission request"),
        BotCommand("fast", "Show or update fast mode"),
        BotCommand("effort", "Show or update reasoning effort"),
        BotCommand("export", "Export the current transcript"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("tables", "Resend recent tables as copyable text"),
        BotCommand("help", "Show available commands"),
    ]

    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        groq_api_key: str = "",
    ):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.groq_api_key = groq_api_key
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        self._media_group_buffers: dict[str, dict] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._last_poll_activity: float = 0.0
        self._send_semaphore = asyncio.Semaphore(TELEGRAM_SEND_CONCURRENCY)
        self._pending_tables: dict[int, list[str]] = {}  # chat_id -> table markdown blocks

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True

        # Keep long-polling traffic separate from replies/progress so a stuck
        # getUpdates request cannot starve /help or other outbound sends.
        bot_request = HTTPXRequest(
            connection_pool_size=32,
            pool_timeout=10.0,
            connect_timeout=30.0,
            read_timeout=30.0,
        )
        updates_request = _TrackingHTTPXRequest(
            connection_pool_size=4,
            pool_timeout=10.0,
            connect_timeout=30.0,
            read_timeout=35.0,
            on_get_updates_success=self._record_poll_activity,
        )
        builder = Application.builder().token(self.config.token).request(bot_request).get_updates_request(updates_request)
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Add command handlers — /start and /help have dedicated local
        # handlers; all other slash commands are forwarded to the message
        # bus so the gateway runtime can dispatch them via CommandRegistry.
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("help", self._on_help))
        self._app.add_handler(CommandHandler("new", self._forward_command))
        self._app.add_handler(CommandHandler("compact", self._forward_command))
        self._app.add_handler(CommandHandler("status", self._forward_command))
        self._app.add_handler(CommandHandler("summary", self._forward_command))
        self._app.add_handler(CommandHandler("cost", self._forward_command))
        self._app.add_handler(CommandHandler("usage", self._forward_command))
        self._app.add_handler(CommandHandler("model", self._forward_command))
        self._app.add_handler(CommandHandler("provider", self._forward_command))
        self._app.add_handler(CommandHandler("config", self._forward_command))
        self._app.add_handler(CommandHandler("memory", self._forward_command))
        self._app.add_handler(CommandHandler("agents", self._forward_command))
        self._app.add_handler(CommandHandler("tasks", self._forward_command))
        self._app.add_handler(CommandHandler("permissions", self._forward_command))
        self._app.add_handler(CommandHandler("allow", self._forward_command))
        self._app.add_handler(CommandHandler("deny", self._forward_command))
        self._app.add_handler(CommandHandler("fast", self._forward_command))
        self._app.add_handler(CommandHandler("effort", self._forward_command))
        self._app.add_handler(CommandHandler("export", self._forward_command))
        self._app.add_handler(CommandHandler("stop", self._forward_command))
        self._app.add_handler(CommandHandler("tables", self._on_tables))
        self._app.add_handler(CallbackQueryHandler(self._on_callback_query, pattern=r"^ohmo_perm:"))

        # Catch-all for any other slash commands not explicitly registered
        # above (e.g. /clear, /stats, /hooks, /skills, /resume, etc.).
        # Without this handler, unrecognised /commands are silently dropped
        # by the Telegram bot framework.
        # NOTE: In python-telegram-bot v22+, CommandHandler requires a string
        # command name; filters.COMMAND is a filter object, not iterable.
        # Use MessageHandler with filters.COMMAND instead.
        self._app.add_handler(MessageHandler(filters.COMMAND, self._forward_command))

        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL)
                & ~filters.COMMAND,
                self._on_message
            )
        )

        logger.info("Starting Telegram bot (polling mode)...")

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()

        # Get bot info and register command menu
        bot_info = await self._app.bot.get_me()
        logger.info("Telegram bot @%s connected", bot_info.username)

        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            logger.debug("Telegram bot commands registered")
        except Exception as e:
            logger.warning("Failed to register bot commands: %s", e)

        # Start polling (this runs until stopped)
        await self._start_polling(drop_pending_updates=True)

        # Keep running until stopped, with stall detection
        while self._running:
            await asyncio.sleep(5)
            if not self._running or not self._app:
                break
            if self._needs_polling_restart():
                logger.warning("Polling is not running, restarting...")
                try:
                    await self._restart_polling()
                except Exception as e:
                    logger.error("Failed to restart polling: %s", e)

    async def _start_polling(self, *, drop_pending_updates: bool = False) -> None:
        """Start the updater's long-polling loop and record activity."""
        self._record_poll_activity()
        await self._app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=drop_pending_updates,
        )

    def _record_poll_activity(self) -> None:
        """Record a successful polling request, not just an inbound message."""
        self._last_poll_activity = time.monotonic()

    async def _restart_polling(self) -> None:
        """Restart the polling loop after it has stopped."""
        if not self._app:
            return
        try:
            if self._app.updater.running:
                await self._app.updater.stop()
        except Exception as e:
            logger.warning("Error stopping stalled updater: %s", e)
        await self._start_polling(drop_pending_updates=False)
        logger.info("Polling restarted")

    def _needs_polling_restart(self) -> bool:
        """Check if the polling task has stopped and should be restarted."""
        if not self._app:
            return False
        if not self._app.updater.running:
            return True
        polling_task = getattr(self._app.updater, "_Updater__polling_task", None)
        if polling_task is not None and polling_task.done():
            return True
        if self._last_poll_activity:
            idle_for = time.monotonic() - self._last_poll_activity
            if idle_for > TELEGRAM_POLL_STALL_SECONDS:
                logger.warning("Telegram polling appears stalled after %.1fs without getUpdates success", idle_for)
                return True
        return False

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False

        # Cancel all typing indicators
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        for task in self._media_group_tasks.values():
            task.cancel()
        self._media_group_tasks.clear()
        self._media_group_buffers.clear()

        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None

    @staticmethod
    def _get_media_type(path: str) -> str:
        """Guess media type from file extension."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "photo"
        if ext == "ogg":
            return "voice"
        if ext in ("mp3", "m4a", "wav", "aac"):
            return "audio"
        return "document"

    async def _call_telegram(self, func, *args, **kwargs):
        """Limit concurrent outbound Telegram API calls."""
        async with self._send_semaphore:
            return await func(*args, **kwargs)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        # Only stop typing indicator for final responses
        if not msg.metadata.get("_progress", False):
            self._stop_typing(msg.chat_id)

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            logger.error("Invalid chat_id: %s", msg.chat_id)
            return

        reply_params = None
        if self.config.reply_to_message:
            reply_to_message_id = msg.metadata.get("message_id")
            if reply_to_message_id:
                reply_params = ReplyParameters(
                    message_id=reply_to_message_id,
                    allow_sending_without_reply=True
                )

        # Topic (forum) thread ID — pass through so replies land in the same topic
        thread_id = msg.metadata.get("message_thread_id")

        # Send media files
        for media_path in (msg.media or []):
            try:
                media_type = self._get_media_type(media_path)
                sender = {
                    "photo": self._app.bot.send_photo,
                    "voice": self._app.bot.send_voice,
                    "audio": self._app.bot.send_audio,
                }.get(media_type, self._app.bot.send_document)
                param = "photo" if media_type == "photo" else media_type if media_type in ("voice", "audio") else "document"
                with open(media_path, 'rb') as f:
                    await self._call_telegram(
                        sender,
                        chat_id=chat_id,
                        **{param: f},
                        message_thread_id=thread_id,
                        reply_parameters=reply_params
                    )
            except Exception as e:
                filename = media_path.rsplit("/", 1)[-1]
                logger.error("Failed to send media %s: %s", media_path, e)
                await self._call_telegram(
                    self._app.bot.send_message,
                    chat_id=chat_id,
                    text=f"[Failed to send: {filename}]",
                    message_thread_id=thread_id,
                    reply_parameters=reply_params
                )

        # Send text content
        if msg.content and msg.content != "[empty message]":
            is_progress = msg.metadata.get("_progress", False)
            draft_id = msg.metadata.get("message_id")
            use_draft = is_progress and draft_id and chat_id > 0
            reply_markup = self._permission_reply_markup(msg.metadata)

            # Split content into text and table blocks so tables can be sent
            # as images in-place, preserving the original reading order.
            if not is_progress:
                segments = _split_by_tables(msg.content)
            else:
                segments = [("text", msg.content)]

            # Only the very first sub-message should reply-quote the user;
            # subsequent parts should flow naturally without quoting.
            is_first = True

            for seg_index, (seg_kind, seg_content) in enumerate(segments):
                if seg_kind == "table":
                    # Collect table markdown for /tables command
                    self._pending_tables.setdefault(chat_id, []).append(seg_content)
                    # Only reply-quote on the very first sub-message
                    table_reply = reply_params if is_first else None
                    is_first = False
                    try:
                        await self._send_table_images(
                            [seg_content], chat_id, thread_id, table_reply,
                        )
                    except Exception:
                        logger.warning("Table image send failed, falling back to text", exc_info=True)
                        # Fallback: send table as text with convert()
                        try:
                            text, entities = convert(seg_content)
                            tg_entities = [TGMessageEntity.de_json(e.to_dict(), None) for e in entities]
                            chunks = split_entities(text, tg_entities, max_utf16_len=TELEGRAM_MAX_UTF16_LEN) if tg_entities else [(text, [])]
                            for chunk_text, chunk_entities in chunks:
                                await self._call_telegram(
                                    self._app.bot.send_message,
                                    chat_id=chat_id,
                                    text=chunk_text,
                                    message_thread_id=thread_id,
                                    entities=chunk_entities or None,
                                )
                        except Exception:
                            logger.warning("Table text fallback also failed", exc_info=True)
                    continue

                # Text block — convert and send as usual
                try:
                    text, entities = convert(seg_content)
                    tg_entities = [TGMessageEntity.de_json(e.to_dict(), None) for e in entities]
                except Exception:
                    logger.warning("telegramify-markdown convert failed, sending raw text", exc_info=True)
                    text = seg_content
                    tg_entities = []

                chunks = split_entities(text, tg_entities, max_utf16_len=TELEGRAM_MAX_UTF16_LEN) if tg_entities else [(text, [])]

                for index, (chunk_text, chunk_entities) in enumerate(chunks):
                    # Only attach reply_markup, reply_parameters, and use draft
                    # for the very first sub-message so subsequent parts read
                    # as a continuous flow rather than separate replies.
                    chunk_reply_params = reply_params if is_first else None
                    chunk_reply_markup = reply_markup if is_first and not use_draft else None
                    if is_first:
                        is_first = False
                    try:
                        if use_draft and seg_index == 0 and index == 0:
                            await self._call_telegram(
                                self._app.bot.send_message_draft,
                                chat_id=chat_id,
                                draft_id=draft_id,
                                text=chunk_text,
                                message_thread_id=thread_id,
                                entities=chunk_entities or None,
                            )
                        else:
                            await self._call_telegram(
                                self._app.bot.send_message,
                                chat_id=chat_id,
                                text=chunk_text,
                                message_thread_id=thread_id,
                                entities=chunk_entities or None,
                                reply_parameters=chunk_reply_params,
                                reply_markup=chunk_reply_markup,
                            )
                    except Exception as e:
                        logger.warning("Entity send failed, falling back to plain text: %s", e)
                        try:
                            await self._call_telegram(
                                self._app.bot.send_message,
                                chat_id=chat_id,
                                text=chunk_text,
                                message_thread_id=thread_id,
                                reply_parameters=chunk_reply_params,
                            )
                        except Exception as e2:
                            logger.error("Error sending Telegram message: %s", e2)

    @staticmethod
    def _permission_reply_markup(metadata: dict) -> InlineKeyboardMarkup | None:
        """Return inline approval buttons for gateway permission prompts."""
        if not metadata.get("_permission_request"):
            return None
        request_id = metadata.get("permission_request_id")
        if not isinstance(request_id, str) or not request_id:
            return None
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"ohmo_perm:allow:{request_id}"),
                    InlineKeyboardButton("Deny", callback_data=f"ohmo_perm:deny:{request_id}"),
                ]
            ]
        )

    async def _send_table_images(
        self,
        table_blocks: list[str],
        chat_id: int,
        thread_id: int | None,
        reply_params: ReplyParameters | None,
    ) -> None:
        """Render Markdown tables as PNG images and send them via send_photo.

        Raises ImportError if md2png-lite is not installed.
        Raises Exception if image rendering or sending fails.
        """
        from md2png_lite import render_markdown_image

        for table_md in table_blocks:
            result = render_markdown_image(table_md, theme="github-dark")
            if not result.get("ok"):
                raise RuntimeError(f"md2png-lite rendering failed: {result}")
            img_bytes = base64.b64decode(result["base64"])
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            try:
                with open(tmp_path, "rb") as f:
                    await self._call_telegram(
                        self._app.bot.send_photo,
                        chat_id=chat_id,
                        photo=f,
                        message_thread_id=thread_id,
                        reply_parameters=reply_params,
                    )
            finally:
                import os
                os.unlink(tmp_path)

    async def _on_tables(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /tables command: resend recent table markdown as copyable text."""
        self._record_poll_activity()
        if not update.message or not self._app:
            return

        chat_id = update.message.chat_id
        thread_id = getattr(update.message, "message_thread_id", None)
        tables = self._pending_tables.pop(chat_id, None)

        if not tables:
            await self._call_telegram(
                update.message.reply_text,
                "No tables in recent messages.",
                do_quote=False,
                **({"message_thread_id": thread_id} if thread_id else {}),
            )
            return

        for table_md in tables:
            try:
                text, entities = convert(table_md)
                tg_entities = [TGMessageEntity.de_json(e.to_dict(), None) for e in entities]
                chunks = split_entities(text, tg_entities, max_utf16_len=TELEGRAM_MAX_UTF16_LEN) if tg_entities else [(text, [])]
                for chunk_text, chunk_entities in chunks:
                    await self._call_telegram(
                        self._app.bot.send_message,
                        chat_id=chat_id,
                        text=chunk_text,
                        message_thread_id=thread_id,
                        entities=chunk_entities or None,
                    )
            except Exception:
                logger.warning("Failed to send table markdown", exc_info=True)
                await self._call_telegram(
                    self._app.bot.send_message,
                    chat_id=chat_id,
                    text=table_md,
                    message_thread_id=thread_id,
                )

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        self._record_poll_activity()
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        thread_id = getattr(update.message, "message_thread_id", None)
        reply_kwargs = {"do_quote": False}
        if thread_id:
            reply_kwargs["message_thread_id"] = thread_id
        await self._call_telegram(
            update.message.reply_text,
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands.",
            **reply_kwargs,
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command, bypassing ACL so all users can access it."""
        self._record_poll_activity()
        if not update.message:
            return
        thread_id = getattr(update.message, "message_thread_id", None)
        reply_kwargs = {"do_quote": False}
        if thread_id:
            reply_kwargs["message_thread_id"] = thread_id
        await self._call_telegram(
            update.message.reply_text,
            "🐈 nanobot commands:\n"
            "/new — Start a new conversation\n"
            "/compact — Compact conversation history\n"
            "/status — Show session status\n"
            "/summary — Summarize conversation history\n"
            "/cost — Show token usage and estimated cost\n"
            "/usage — Show usage and token estimates\n"
            "/model — Show or update the default model\n"
            "/provider — Show or switch provider profiles\n"
            "/config — Show or update configuration\n"
            "/memory — Inspect and manage project memory\n"
            "/agents — List or inspect agent and teammate tasks\n"
            "/tasks — Manage background tasks\n"
            "/permissions — Show or update permission mode\n"
            "/allow — Approve a pending permission request\n"
            "/deny — Deny a pending permission request\n"
            "/fast — Show or update fast mode\n"
            "/effort — Show or update reasoning effort\n"
            "/export — Export the current transcript\n"
            "/stop — Stop the current task\n"
            "/tables — Resend recent tables as copyable text\n"
            "/help — Show available commands",
            **reply_kwargs,
        )

    @staticmethod
    def _sender_id(user) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus for unified handling in AgentLoop."""
        if not update.message or not update.effective_user:
            return
        message = update.message
        await self._handle_message(
            sender_id=self._sender_id(update.effective_user),
            chat_id=str(message.chat_id),
            content=message.text,
            metadata={
                "message_id": message.message_id,
                "message_thread_id": getattr(message, "message_thread_id", None),
                "user_id": update.effective_user.id,
                "username": update.effective_user.username,
                "first_name": update.effective_user.first_name,
                "is_group": message.chat.type != "private",
            },
        )

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Turn inline permission button clicks into gateway /allow or /deny messages."""
        query = update.callback_query
        if not query or not update.effective_user:
            return
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "ohmo_perm" or parts[1] not in {"allow", "deny"}:
            await query.answer("Unknown permission action.", show_alert=True)
            return

        message = query.message
        if message is None:
            await query.answer("Original permission request is unavailable.", show_alert=True)
            return

        action = parts[1]
        request_id = parts[2]
        command = f"/{action} {request_id}"
        sender_id = self._sender_id(update.effective_user)
        await query.answer("Permission response sent.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Failed to clear Telegram permission buttons", exc_info=True)
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(message.chat_id),
            content=command,
            metadata={
                "message_id": message.message_id,
                "message_thread_id": getattr(message, "message_thread_id", None),
                "user_id": update.effective_user.id,
                "username": update.effective_user.username,
                "first_name": update.effective_user.first_name,
                "is_group": message.chat.type != "private",
                "callback_query_id": query.id,
            },
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = self._sender_id(user)

        # New user message: clear pending tables for this chat
        self._pending_tables.pop(chat_id, None)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Build content from text and/or media
        content_parts = []
        media_paths = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Handle media files
        media_file = None
        media_type = None

        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"

        # Download media if present
        if media_file and self._app:
            try:
                file = await self._call_telegram(self._app.bot.get_file, media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))

                # Save to workspace/media/
                from openharness.channels.impl.base import resolve_channel_media_dir
                media_dir = resolve_channel_media_dir(self.name)

                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))

                media_paths.append(str(file_path))

                # Handle voice transcription
                if media_type == "voice" or media_type == "audio":
                    from openharness.providers.transcription import GroqTranscriptionProvider  # noqa: F401
                    transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                    transcription = await transcriber.transcribe(file_path)
                    if transcription:
                        logger.info("Transcribed %s: %s...", media_type, transcription[:50])
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    content_parts.append(f"[{media_type}: {file_path}]")

                logger.debug("Downloaded %s to %s", media_type, file_path)
            except Exception as e:
                logger.error("Failed to download media: %s", e)
                content_parts.append(f"[{media_type}: download failed]")

        content = "\n".join(content_parts) if content_parts else "[empty message]"

        logger.debug("Telegram message from %s: %s...", sender_id, content[:50])

        str_chat_id = str(chat_id)

        # Telegram media groups: buffer briefly, forward as one aggregated turn.
        if media_group_id := getattr(message, "media_group_id", None):
            key = f"{str_chat_id}:{media_group_id}"
            if key not in self._media_group_buffers:
                self._media_group_buffers[key] = {
                    "sender_id": sender_id, "chat_id": str_chat_id,
                    "contents": [], "media": [],
                    "metadata": {
                        "message_id": message.message_id,
                        "message_thread_id": getattr(message, "message_thread_id", None),
                        "user_id": user.id,
                        "username": user.username, "first_name": user.first_name,
                        "is_group": message.chat.type != "private",
                    },
                }
                self._start_typing(str_chat_id)
            buf = self._media_group_buffers[key]
            if content and content != "[empty message]":
                buf["contents"].append(content)
            buf["media"].extend(media_paths)
            if key not in self._media_group_tasks:
                self._media_group_tasks[key] = asyncio.create_task(self._flush_media_group(key))
            return

        # Start typing indicator before processing
        self._start_typing(str_chat_id)

        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata={
                "message_id": message.message_id,
                "message_thread_id": getattr(message, "message_thread_id", None),
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private"
            }
        )

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            if not (buf := self._media_group_buffers.pop(key, None)):
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            await self._handle_message(
                sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                content=content, media=list(dict.fromkeys(buf["media"])),
                metadata=buf["metadata"],
            )
        finally:
            self._media_group_tasks.pop(key, None)

    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            while self._app:
                await self._call_telegram(self._app.bot.send_chat_action, chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Typing indicator stopped for %s: %s", chat_id, e)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        logger.error("Telegram error: %s", context.error)

    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
