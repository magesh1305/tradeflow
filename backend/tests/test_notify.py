from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.notify.base import Level, LogNotifier
from app.notify.telegram import MAX_LEN, CompositeNotifier, TelegramNotifier

TOKEN = "123456:SECRET-bot-token"


def make(handler: httpx.MockTransport, min_level: Level = "info") -> TelegramNotifier:
    return TelegramNotifier(
        TOKEN,
        "42",
        client=httpx.AsyncClient(transport=handler),
        min_interval=0,
        min_level=min_level,
    )


async def test_sends_message_to_chat() -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    t = make(httpx.MockTransport(handle))
    await t.send("KILL SWITCH", "critical")
    (req,) = seen
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "42"
    assert body["text"].endswith("KILL SWITCH") and body["text"].startswith("🚨")


async def test_failures_are_swallowed_and_token_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    t = make(httpx.MockTransport(boom))
    with caplog.at_level(logging.DEBUG):
        await t.send("hello")
        t2 = make(httpx.MockTransport(lambda r: httpx.Response(500)))
        await t2.send("hello")
    assert t.failures == 1 and t2.failures == 1
    assert TOKEN not in caplog.text
    assert "SECRET" not in caplog.text


async def test_min_level_and_truncation() -> None:
    texts: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        texts.append(json.loads(req.content)["text"])
        return httpx.Response(200)

    t = make(httpx.MockTransport(handle), min_level="warning")
    await t.send("fill", "info")
    await t.send("x" * 10_000, "warning")
    assert len(texts) == 1 and len(texts[0]) == MAX_LEN


async def test_composite_prefix_and_isolation() -> None:
    class Broken:
        async def send(self, text: str, level: Level = "info") -> None:
            raise RuntimeError("sink down")

    log_sink = LogNotifier()
    c = CompositeNotifier(Broken(), log_sink, prefix="[LIVE] ")
    await c.send("hello", "warning")
    assert log_sink.sent == [("warning", "[LIVE] hello")]
