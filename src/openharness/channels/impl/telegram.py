"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio

import logging
from telegram import BotCommand, MessageEntity as TGMessageEntity, ReplyParameters, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest
from telegramify_markdown import convert, split_entities

from openharness.channels.bus.events import OutboundMessage
from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.base import BaseChannel
from openharness.config.schema import TelegramConfig

logger = logging.getLogger(__name__)

# Telegram limit is 4096 UTF-16 code units; leave some margin
TELEGRAM_MAX_UTF16_LEN = 4000


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
        BotCommand("fast", "Show or update fast mode"),
        BotCommand("effort", "Show or update reasoning effort"),
        BotCommand("export", "Export the current transcript"),
        BotCommand("stop", "Stop the current task"),
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

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True

        # Build the application with larger connection pool to avoid pool-timeout on long runs
        req = HTTPXRequest(connection_pool_size=16, pool_timeout=5.0, connect_timeout=30.0, read_timeout=30.0)
        builder = Application.builder().token(self.config.token).request(req).get_updates_request(req)
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
        self._app.add_handler(CommandHandler("fast", self._forward_command))
        self._app.add_handler(CommandHandler("effort", self._forward_command))
        self._app.add_handler(CommandHandler("export", self._forward_command))
        self._app.add_handler(CommandHandler("stop", self._forward_command))

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
        self._last_poll_activity = asyncio.get_event_loop().time()
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=drop_pending_updates,
        )

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
                    await sender(
                        chat_id=chat_id,
                        **{param: f},
                        message_thread_id=thread_id,
                        reply_parameters=reply_params
                    )
            except Exception as e:
                filename = media_path.rsplit("/", 1)[-1]
                logger.error("Failed to send media %s: %s", media_path, e)
                await self._app.bot.send_message(
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

            try:
                text, entities = convert(msg.content)
                tg_entities = [TGMessageEntity.de_json(e.to_dict(), None) for e in entities]
            except Exception:
                logger.warning("telegramify-markdown convert failed, sending raw text", exc_info=True)
                text = msg.content
                tg_entities = []

            chunks = split_entities(text, tg_entities, max_utf16_len=TELEGRAM_MAX_UTF16_LEN) if tg_entities else [(text, [])]

            for chunk_text, chunk_entities in chunks:
                try:
                    if use_draft:
                        await self._app.bot.send_message_draft(
                            chat_id=chat_id,
                            draft_id=draft_id,
                            text=chunk_text,
                            message_thread_id=thread_id,
                            entities=chunk_entities or None,
                        )
                    else:
                        await self._app.bot.send_message(
                            chat_id=chat_id,
                            text=chunk_text,
                            message_thread_id=thread_id,
                            entities=chunk_entities or None,
                            reply_parameters=reply_params
                        )
                except Exception as e:
                    logger.warning("Entity send failed, falling back to plain text: %s", e)
                    try:
                        if use_draft:
                            await self._app.bot.send_message_draft(
                                chat_id=chat_id,
                                draft_id=draft_id,
                                message_thread_id=thread_id,
                                text=chunk_text,
                            )
                        else:
                            await self._app.bot.send_message(
                                chat_id=chat_id,
                                text=chunk_text,
                                message_thread_id=thread_id,
                                reply_parameters=reply_params
                            )
                    except Exception as e2:
                        logger.error("Error sending Telegram message: %s", e2)

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        self._last_poll_activity = asyncio.get_event_loop().time()
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        thread_id = getattr(update.message, "message_thread_id", None)
        reply_kwargs = {"do_quote": False}
        if thread_id:
            reply_kwargs["message_thread_id"] = thread_id
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands.",
            **reply_kwargs,
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command, bypassing ACL so all users can access it."""
        self._last_poll_activity = asyncio.get_event_loop().time()
        if not update.message:
            return
        thread_id = getattr(update.message, "message_thread_id", None)
        reply_kwargs = {"do_quote": False}
        if thread_id:
            reply_kwargs["message_thread_id"] = thread_id
        await update.message.reply_text(
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
            "/fast — Show or update fast mode\n"
            "/effort — Show or update reasoning effort\n"
            "/export — Export the current transcript\n"
            "/stop — Stop the current task\n"
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

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = self._sender_id(user)

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
                file = await self._app.bot.get_file(media_file.file_id)
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
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
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
