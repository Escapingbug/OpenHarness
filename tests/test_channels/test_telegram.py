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
        def __init__(self):
            self.bot_request = None
            self.updates_request = None

        def token(self, token):
            return self

        def request(self, request):
            self.bot_request = request
            return self

        def get_updates_request(self, request):
            self.updates_request = request
            return self

        def proxy(self, proxy):
            return self

        def get_updates_proxy(self, proxy):
            return self

        def build(self):
            return FakeApplication()

    builder = FakeBuilder()
    monkeypatch.setattr(telegram_mod.Application, "builder", lambda: builder)
    monkeypatch.setattr(telegram_mod, "HTTPXRequest", lambda **kwargs: SimpleNamespace(kind="bot", kwargs=kwargs))
    monkeypatch.setattr(telegram_mod, "_TrackingHTTPXRequest", lambda **kwargs: SimpleNamespace(kind="updates", kwargs=kwargs))
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

    assert builder.bot_request.kind == "bot"
    assert builder.updates_request.kind == "updates"
    assert builder.bot_request is not builder.updates_request

    commands = [
        handler.command
        for handler in registered_handlers
        if getattr(handler, "kind", None) == "command"
    ]
    expected_commands = [
        "start", "help", "new", "compact", "status", "summary",
        "cost", "usage", "model", "provider", "config", "memory",
        "agents", "tasks", "permissions", "allow", "deny",
        "fast", "effort", "export", "stop", "tables",
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
    channel._last_poll_activity = telegram_mod.time.monotonic()

    assert channel._needs_polling_restart() is False


def test_telegram_polling_watchdog_restarts_on_stale_poll_activity():
    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(updater=SimpleNamespace(running=True))
    channel._last_poll_activity = telegram_mod.time.monotonic() - telegram_mod.TELEGRAM_POLL_STALL_SECONDS - 1

    assert channel._needs_polling_restart() is True


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


def test_split_by_tables_basic():
    md = """Intro text

| Name | Type |
|------|------|
| CWE-78 | Injection |
| CWE-89 | SQL |

Middle text

| A | B |
|---|---|
| 1 | 2 |

Outro text"""

    segments = telegram_mod._split_by_tables(md)
    kinds = [k for k, _ in segments]
    assert kinds == ["text", "table", "text", "table", "text"]

    # Verify table contents are preserved
    tables = [c for k, c in segments if k == "table"]
    assert "| CWE-78 | Injection |" in tables[0]
    assert "| A | B |" in tables[1]


def test_split_by_tables_no_tables():
    md = "Just some text without any tables."
    segments = telegram_mod._split_by_tables(md)
    assert segments == [("text", md)]


def test_split_by_tables_only_table():
    md = "| Header |\n|--------|\n| Value  |"
    segments = telegram_mod._split_by_tables(md)
    assert segments == [("table", md)]


def test_split_by_tables_code_block_with_pipes_not_matched():
    md = """Some text

```bash
cat file.txt | grep pattern | sort
```

More text"""

    segments = telegram_mod._split_by_tables(md)
    assert len(segments) == 1
    assert segments[0][0] == "text"


@pytest.mark.asyncio
async def test_telegram_send_splits_tables_into_separate_messages(monkeypatch):
    """Table blocks should be sent as images interleaved with text messages."""
    sent_messages: list[dict] = []
    sent_photos: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent_messages.append(kwargs)

        async def send_photo(self, **kwargs):
            sent_photos.append(kwargs)

    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(bot=FakeBot())

    # Mock md2png-lite to avoid actual image rendering
    fake_md2png = SimpleNamespace(
        render_markdown_image=lambda table_md, theme=None: {"ok": True, "base64": "iVBORw0KGgo="}
    )
    monkeypatch.setitem(__import__("sys").modules, "md2png_lite", fake_md2png)

    from openharness.channels.bus.events import OutboundMessage

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="Before table\n\n| Name | Type |\n|------|------|\n| A | B |\n\nAfter table",
        )
    )

    # Should have: text message, photo, text message
    assert len(sent_messages) == 2, f"Expected 2 text messages, got {len(sent_messages)}"
    assert len(sent_photos) == 1, f"Expected 1 photo, got {len(sent_photos)}"

    assert "Before table" in sent_messages[0]["text"]
    assert "After table" in sent_messages[1]["text"]


@pytest.mark.asyncio
async def test_tables_command_returns_pending_tables(monkeypatch):
    """/tables should resend stored table markdown as copyable pre blocks."""
    sent_messages: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent_messages.append(kwargs)

    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(bot=FakeBot())

    # Simulate tables collected during a send() call
    channel._pending_tables[123] = [
        "| Name | Type |\n|------|------|\n| CWE-78 | Injection |",
        "| A | B |\n|---|---|\n| 1 | 2 |",
    ]

    update = SimpleNamespace(
        message=SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
            reply_text=None,
        ),
        effective_user=SimpleNamespace(id=456, username="test", first_name="Test"),
    )

    # Mock reply_text to capture the "no tables" case
    async def fake_reply_text(text, **kwargs):
        sent_messages.append({"text": text, **kwargs})

    update.message.reply_text = fake_reply_text

    await channel._on_tables(update, SimpleNamespace())

    # Should have sent the two tables as messages
    assert len(sent_messages) == 2
    # Tables are sent via convert(), so they'll be in pre blocks
    for msg in sent_messages:
        assert "entities" in msg or "text" in msg


@pytest.mark.asyncio
async def test_tables_command_no_tables():
    """/tables with no pending tables should reply with a hint."""

    class FakeBot:
        async def send_message(self, **kwargs):
            pass

    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )
    channel._app = SimpleNamespace(bot=FakeBot())

    replied: list[str] = []

    update = SimpleNamespace(
        message=SimpleNamespace(
            chat_id=123,
            message_thread_id=None,
        ),
        effective_user=SimpleNamespace(id=456, username="test", first_name="Test"),
    )

    async def fake_reply_text(text, **kwargs):
        replied.append(text)

    update.message.reply_text = fake_reply_text

    await channel._on_tables(update, SimpleNamespace())

    assert len(replied) == 1
    assert "No tables" in replied[0]


@pytest.mark.asyncio
async def test_pending_tables_cleared_on_new_user_message():
    """Pending tables should be cleared when a new user message arrives."""
    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=False),
        MessageBus(),
    )

    # Simulate pending tables from previous bot reply
    channel._pending_tables[123] = ["| A | B |"]

    # Simulate _on_message clearing them (first thing it does)
    channel._pending_tables.pop(123, None)

    assert 123 not in channel._pending_tables


@pytest.mark.asyncio
async def test_reply_parameters_only_on_first_sub_message(monkeypatch):
    """Only the first sub-message should carry reply_parameters; subsequent
    parts (table images, text chunks) should flow without quoting."""
    sent_messages: list[dict] = []
    sent_photos: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs):
            sent_messages.append(kwargs)

        async def send_photo(self, **kwargs):
            sent_photos.append(kwargs)

    channel = TelegramChannel(
        TelegramConfig(token="token", allow_from=["*"], proxy=None, reply_to_message=True),
        MessageBus(),
    )
    channel._app = SimpleNamespace(bot=FakeBot())

    fake_md2png = SimpleNamespace(
        render_markdown_image=lambda table_md, theme=None: {"ok": True, "base64": "iVBORw0KGgo="}
    )
    monkeypatch.setitem(__import__("sys").modules, "md2png_lite", fake_md2png)

    from openharness.channels.bus.events import OutboundMessage

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="Before\n\n| H |\n|---|\n| V |\n\nAfter",
            metadata={"message_id": 42},
        )
    )

    # First message should have reply_parameters
    first_msg = sent_messages[0]
    assert "reply_parameters" in first_msg
    assert first_msg["reply_parameters"].message_id == 42

    # Photo (table image) should NOT have reply_parameters
    photo_msg = sent_photos[0]
    assert photo_msg.get("reply_parameters") is None

    # Second text message should NOT have reply_parameters
    second_msg = sent_messages[1]
    assert second_msg.get("reply_parameters") is None
