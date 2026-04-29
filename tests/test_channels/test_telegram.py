from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl import telegram as telegram_mod
from openharness.channels.impl.telegram import TelegramChannel
from openharness.config.schema import TelegramConfig


@pytest.mark.asyncio
async def test_telegram_registers_stop_command(monkeypatch):
    registered_handlers = []

    class FakeBot:
        async def get_me(self):
            return SimpleNamespace(username="openharness_test_bot")

        async def set_my_commands(self, commands):
            return None

    class FakeUpdater:
        async def start_polling(self, **kwargs):
            return None

        async def stop(self):
            return None

    class FakeApplication:
        def __init__(self):
            self.bot = FakeBot()
            self.updater = FakeUpdater()

        def add_error_handler(self, handler):
            return None

        def add_handler(self, handler):
            registered_handlers.append(handler)

        async def initialize(self):
            return None

        async def start(self):
            return None

        async def stop(self):
            return None

        async def shutdown(self):
            return None

    class FakeBuilder:
        def token(self, token):
            return self

        def request(self, request):
            return self

        def get_updates_request(self, request):
            return self

        def proxy(self, proxy):
            return self

        def get_updates_proxy(self, proxy):
            return self

        def build(self):
            return FakeApplication()

    monkeypatch.setattr(telegram_mod.Application, "builder", lambda: FakeBuilder())
    monkeypatch.setattr(telegram_mod, "HTTPXRequest", lambda **kwargs: object())
    monkeypatch.setattr(
        telegram_mod,
        "CommandHandler",
        lambda command, callback: SimpleNamespace(
            kind="command",
            command=command,
            callback=callback,
        ),
    )
    monkeypatch.setattr(
        telegram_mod,
        "MessageHandler",
        lambda filters, callback: SimpleNamespace(
            kind="command" if isinstance(filters, telegram_mod.filters.Command) else "message",
            command=filters,
            callback=callback,
        ),
    )

    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )

    async def fake_sleep(delay):
        channel._running = False

    monkeypatch.setattr(telegram_mod.asyncio, "sleep", fake_sleep)

    await channel.start()

    commands = [
        handler.command
        for handler in registered_handlers
        if getattr(handler, "kind", None) == "command"
    ]
    expected_commands = [
        "start", "help", "new", "compact", "status", "summary",
        "cost", "usage", "model", "provider", "config", "memory",
        "agents", "tasks", "permissions", "allow", "deny",
        "fast", "effort", "export", "stop",
    ]
    for cmd in expected_commands:
        assert cmd in commands, f"Expected /{cmd} to be registered"

    # Catch-all handler should also be present
    assert any(
        getattr(h, "kind", None) == "command" and not isinstance(getattr(h, "command", None), str)
        for h in registered_handlers
    ), "Catch-all command handler (filters.COMMAND) should be registered"


def test_telegram_polling_watchdog_does_not_restart_on_idle_time():
    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(updater=SimpleNamespace(running=True))
    channel._last_poll_activity = 1.0

    assert channel._needs_polling_restart() is False


@pytest.mark.asyncio
async def test_telegram_permission_request_renders_inline_buttons():
    sent: dict[str, object] = {}

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.update(kwargs)

    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(bot=FakeBot())

    from openharness.channels.bus.events import OutboundMessage

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="Permission required for `bash`.",
            metadata={
                "_permission_request": True,
                "permission_request_id": "abc123",
            },
        )
    )

    markup = sent["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "Approve"
    assert markup.inline_keyboard[0][0].callback_data == "ohmo_perm:allow:abc123"
    assert markup.inline_keyboard[0][1].text == "Deny"
    assert markup.inline_keyboard[0][1].callback_data == "ohmo_perm:deny:abc123"


@pytest.mark.asyncio
async def test_telegram_permission_callback_forwards_allow_command():
    bus = MessageBus()
    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["123"], proxy=None, reply_to_message=False),
        bus,
    )
    answered: list[str] = []
    edited: list[object] = []

    class FakeQuery:
        id = "query1"
        data = "ohmo_perm:allow:abc123"
        message = SimpleNamespace(
            chat_id=456,
            message_id=789,
            message_thread_id=None,
            chat=SimpleNamespace(type="private"),
        )

        async def answer(self, text, show_alert=False):
            answered.append(text)

        async def edit_message_reply_markup(self, reply_markup=None):
            edited.append(reply_markup)

    update = SimpleNamespace(
        callback_query=FakeQuery(),
        effective_user=SimpleNamespace(id=123, username=None, first_name="Test"),
    )

    await channel._on_callback_query(update, SimpleNamespace())
    msg = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)

    assert answered == ["Permission response sent."]
    assert edited == [None]
    assert msg.content == "/allow abc123"
    assert msg.chat_id == "456"
    assert msg.sender_id == "123"


@pytest.mark.asyncio
async def test_telegram_polling_restart_failure_remains_retryable():
    class FakeUpdater:
        def __init__(self):
            self.running = False
            self.start_calls = 0
            self.start_kwargs = None

        async def start_polling(self, **kwargs):
            self.start_calls += 1
            self.start_kwargs = kwargs
            raise RuntimeError("network failure")

        async def stop(self):
            self.running = False

    updater = FakeUpdater()
    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(updater=updater)

    with pytest.raises(RuntimeError, match="network failure"):
        await channel._restart_polling()

    assert updater.start_calls == 1
    assert updater.start_kwargs["drop_pending_updates"] is False
    assert channel._needs_polling_restart() is True


@pytest.mark.asyncio
async def test_telegram_polling_watchdog_restarts_finished_polling_task():
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    updater = SimpleNamespace(running=True)
    setattr(updater, "_Updater__polling_task", finished)
    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(updater=updater)

    assert channel._needs_polling_restart() is True
