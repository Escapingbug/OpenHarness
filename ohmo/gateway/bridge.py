"""Gateway bridge connecting channel bus traffic to ohmo runtimes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from openharness.channels.bus.events import InboundMessage, OutboundMessage
from openharness.channels.bus.queue import MessageBus

from ohmo.gateway.router import session_key_for_message
from ohmo.gateway.runtime import OhmoSessionRuntimePool

logger = logging.getLogger(__name__)
_INTERRUPT_WAIT_SECONDS = 3.0


def _content_snippet(text: str, *, limit: int = 160) -> str:
    """Return a single-line preview suitable for logs."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _format_gateway_error(exc: Exception) -> str:
    """Return a short, user-facing gateway error message."""
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    if "claude oauth refresh failed" in lowered:
        return (
            "[ohmo gateway error] Claude subscription auth refresh failed. "
            "Run `oh auth claude-login` again or switch the gateway profile."
        )
    if "claude oauth refresh token is invalid or expired" in lowered:
        return (
            "[ohmo gateway error] Claude subscription token is expired. "
            "Run `claude auth login`, then `oh auth claude-login`, or switch the gateway profile."
        )
    if "auth source not found" in lowered or "access token" in lowered:
        return (
            "[ohmo gateway error] Authentication is not configured for the current "
            "gateway profile. Run `oh setup` or `ohmo config`."
        )
    if "api key" in lowered or "auth" in lowered or "credential" in lowered:
        return (
            "[ohmo gateway error] Authentication failed for the current gateway "
            "profile. Check `oh auth status` and `ohmo config`."
        )
    return f"[ohmo gateway error] {message}"


class OhmoGatewayBridge:
    """Consume inbound messages and publish assistant replies.

    Supports queuing of cron-triggered agent messages: when a cron job fires
    while the session is busy processing a user message, the cron message is
    queued and processed after the current task finishes.  User-initiated
    messages (new messages or /stop) always clear the queue — a new user
    message replaces the queued cron message, while /stop clears it and then
    immediately processes it (since /stop means "change direction", not "go
    silent").
    """

    def __init__(
        self,
        *,
        bus: MessageBus,
        runtime_pool: OhmoSessionRuntimePool,
        restart_gateway: Callable[[object, str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._bus = bus
        self._runtime_pool = runtime_pool
        self._restart_gateway = restart_gateway
        self._running = False
        self._session_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_cancel_reasons: dict[str, str] = {}
        # Per-session pending cron messages (at most one per session).
        self._pending_cron: dict[str, InboundMessage] = {}

    # ------------------------------------------------------------------
    # Public API for cron scheduler
    # ------------------------------------------------------------------

    def enqueue_cron_message(self, message: InboundMessage, session_key: str) -> bool:
        """Enqueue a cron-triggered message for *session_key*.

        If the session is idle, the message is processed immediately.
        If the session is busy, the message is queued and will be processed
        after the current task finishes.  Returns True if the message was
        enqueued (or processed immediately), False if a cron message was
        already queued for this session (the new one replaces the old one).
        """
        self._pending_cron[session_key] = message
        logger.info(
            "ohmo cron message enqueued session_key=%s session_busy=%s",
            session_key,
            session_key in self._session_tasks and not self._session_tasks[session_key].done(),
        )
        # If session is idle, process immediately
        task = self._session_tasks.get(session_key)
        if task is None or task.done():
            self._drain_pending_cron(session_key)
        return True

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                message = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            session_key = session_key_for_message(message)
            logger.info(
                "ohmo inbound received channel=%s chat_id=%s sender_id=%s session_key=%s content=%r",
                message.channel,
                message.chat_id,
                message.sender_id,
                session_key,
                _content_snippet(message.content),
            )
            resolve_permission_response = getattr(self._runtime_pool, "resolve_permission_response", None)
            permission_ack = (
                resolve_permission_response(message, session_key)
                if resolve_permission_response is not None
                else None
            )
            if permission_ack is not None:
                await self._bus.publish_outbound(
                    OutboundMessage(
                        channel=message.channel,
                        chat_id=message.chat_id,
                        content=permission_ack,
                        metadata={"_session_key": session_key},
                    )
                )
                continue
            if message.content.strip() == "/stop":
                await self._handle_stop(message, session_key)
                continue
            if message.content.strip() == "/restart":
                await self._handle_restart(message, session_key)
                continue
            # User sent a new message — clear any pending cron for this session
            # (the user is actively talking, the cron reminder is no longer needed)
            self._pending_cron.pop(session_key, None)
            await self._interrupt_session(
                session_key,
                reason="replaced by a newer user message",
                notify=OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content="⏹️ 已停止上一条正在处理的任务，继续看你的最新消息。",
                    metadata={"_progress": True, "_session_key": session_key},
                ),
            )
            task = asyncio.create_task(
                self._process_message(message, session_key),
                name=f"ohmo-session:{session_key}",
            )
            self._session_tasks[session_key] = task
            task.add_done_callback(lambda finished, key=session_key: self._cleanup_task(key, finished))

    def stop(self) -> None:
        self._running = False
        for session_key, task in list(self._session_tasks.items()):
            self._session_cancel_reasons[session_key] = "gateway stopping"
            task.cancel()

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _handle_stop(self, message, session_key: str) -> None:
        stopped = await self._interrupt_session(
            session_key,
            reason="stopped by user command",
        )
        content = "⏹️ 已停止当前正在运行的任务。" if stopped else "当前没有正在运行的任务。"
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=content,
                metadata={"_session_key": session_key},
            )
        )
        # After /stop, drain any pending cron message — the user stopped the
        # current direction, so the queued reminder should fire immediately.
        self._drain_pending_cron(session_key)

    async def _handle_restart(self, message, session_key: str) -> None:
        # Clear pending cron on restart — the session is being torn down
        self._pending_cron.pop(session_key, None)
        await self._interrupt_session(
            session_key,
            reason="restarting gateway by user command",
        )
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content="🔄 正在重启 gateway，马上回来。\nRestarting the gateway now. I'll be back in a moment.",
                metadata={"_session_key": session_key},
            )
        )
        if self._restart_gateway is not None:
            result = self._restart_gateway(message, session_key)
            if asyncio.iscoroutine(result):
                await result

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _interrupt_session(
        self,
        session_key: str,
        *,
        reason: str,
        notify: OutboundMessage | None = None,
    ) -> bool:
        task = self._session_tasks.get(session_key)
        if task is None or task.done():
            return False
        self._session_cancel_reasons[session_key] = reason
        task.cancel()
        if notify is not None:
            await self._bus.publish_outbound(notify)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_INTERRUPT_WAIT_SECONDS)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        await self._close_interrupted_session(session_key)
        return True

    async def _process_message(self, message, session_key: str) -> None:
        # Preserve inbound message_id so channels can reply in-thread
        inbound_meta = {
            k: message.metadata[k] for k in ("message_id", "thread_id") if k in message.metadata
        }
        try:
            reply = ""
            final_media: list[str] = []
            async for update in self._runtime_pool.stream_message(message, session_key):
                if update.kind == "final":
                    reply = update.text
                    # Collect media from the final update (produced files from tools)
                    for path in getattr(update, "media", ()) or ():
                        if path not in final_media:
                            final_media.append(path)
                    continue
                if not update.text:
                    continue
                logger.info(
                    "ohmo outbound update channel=%s chat_id=%s session_key=%s kind=%s content=%r",
                    message.channel,
                    message.chat_id,
                    session_key,
                    update.kind,
                    _content_snippet(update.text),
                )
                if not self._is_current_session_task(session_key):
                    logger.info(
                        "ohmo ignoring stale session update channel=%s chat_id=%s session_key=%s kind=%s",
                        message.channel,
                        message.chat_id,
                        session_key,
                        update.kind,
                    )
                    return
                await self._bus.publish_outbound(
                    OutboundMessage(
                        channel=message.channel,
                        chat_id=message.chat_id,
                        content=update.text,
                        metadata={**inbound_meta, **(update.metadata or {})},
                    )
                )
        except asyncio.CancelledError:
            logger.info(
                "ohmo session interrupted channel=%s chat_id=%s session_key=%s reason=%s",
                message.channel,
                message.chat_id,
                session_key,
                self._session_cancel_reasons.get(session_key, "cancelled"),
            )
            raise
        except Exception as exc:  # pragma: no cover - gateway failure path
            logger.exception(
                "ohmo gateway failed to process inbound message channel=%s chat_id=%s sender_id=%s session_key=%s content=%r",
                message.channel,
                message.chat_id,
                message.sender_id,
                session_key,
                _content_snippet(message.content),
            )
            reply = _format_gateway_error(exc)
        finally:
            # After the current task finishes, drain any pending cron message
            self._drain_pending_cron(session_key)
        if not reply:
            logger.info(
                "ohmo inbound finished without final reply channel=%s chat_id=%s session_key=%s",
                message.channel,
                message.chat_id,
                session_key,
            )
            return
        logger.info(
            "ohmo outbound final channel=%s chat_id=%s session_key=%s content=%r",
            message.channel,
            message.chat_id,
            session_key,
            _content_snippet(reply),
        )
        if not self._is_current_session_task(session_key):
            logger.info(
                "ohmo ignoring stale session final channel=%s chat_id=%s session_key=%s",
                message.channel,
                message.chat_id,
                session_key,
            )
            return
        await self._bus.publish_outbound(
            OutboundMessage(
                channel=message.channel,
                chat_id=message.chat_id,
                content=reply,
                metadata={**inbound_meta, "_session_key": session_key},
                media=final_media,
            )
        )

    def _is_current_session_task(self, session_key: str) -> bool:
        return self._session_tasks.get(session_key) is asyncio.current_task()

    async def _close_interrupted_session(self, session_key: str) -> None:
        close_session = getattr(self._runtime_pool, "close_session", None)
        if close_session is None:
            return
        try:
            result = close_session(session_key)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("ohmo failed to close interrupted session session_key=%s", session_key)

    def _cleanup_task(self, session_key: str, task: asyncio.Task[None]) -> None:
        current = self._session_tasks.get(session_key)
        if current is task:
            self._session_tasks.pop(session_key, None)
            self._session_cancel_reasons.pop(session_key, None)

    # ------------------------------------------------------------------
    # Pending cron drain
    # ------------------------------------------------------------------

    def _drain_pending_cron(self, session_key: str) -> None:
        """If there is a pending cron message for *session_key*, start processing it."""
        message = self._pending_cron.pop(session_key, None)
        if message is None:
            return
        logger.info(
            "ohmo draining pending cron message session_key=%s content=%r",
            session_key,
            _content_snippet(message.content),
        )
        task = asyncio.create_task(
            self._process_message(message, session_key),
            name=f"ohmo-cron:{session_key}",
        )
        self._session_tasks[session_key] = task
        task.add_done_callback(lambda finished, key=session_key: self._cleanup_task(key, finished))
