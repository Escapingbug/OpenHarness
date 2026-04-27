from __future__ import annotations

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
            kind="message",
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
    assert commands == ["start", "new", "stop", "help"]
