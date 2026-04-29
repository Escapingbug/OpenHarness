from __future__ import annotations

import pytest

from openharness.channels.impl.manager import ChannelManager


@pytest.mark.asyncio
async def test_channel_manager_restarts_channel_after_crash(monkeypatch):
    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr("openharness.channels.impl.manager.asyncio.sleep", fake_sleep)

    class FlakyChannel:
        def __init__(self) -> None:
            self.start_calls = 0
            self.stop_calls = 0

        async def start(self) -> None:
            self.start_calls += 1
            if self.start_calls == 1:
                raise RuntimeError("boom")

        async def stop(self) -> None:
            self.stop_calls += 1

    channel = FlakyChannel()
    manager = ChannelManager.__new__(ChannelManager)

    await manager._start_channel("telegram", channel)

    assert channel.start_calls == 2
    assert channel.stop_calls == 1
    assert sleep_delays == [1.0]
