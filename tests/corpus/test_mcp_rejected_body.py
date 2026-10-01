"""Rejected uploads are discarded only within a bound, without application work."""
import asyncio

from vkm_corpus.mcp.http import BearerMiddleware, REJECTED_BODY_LIMIT


def run(messages, *, headers=(), method="POST"):
    sent, received, called = [], [], []

    async def receive():
        if not messages:
            await asyncio.Future()
        value = messages.pop(0)
        received.append(value)
        return value

    async def send(value):
        sent.append(value)

    async def inner(*_):
        called.append(True)

    asyncio.run(BearerMiddleware(inner, {"allowed": "reader"})(
        {"type": "http", "path": "/mcp", "method": method, "headers": headers}, receive, send))
    assert not called
    assert sent[0]["status"] == 401
    assert (b"connection", b"close") in sent[0]["headers"]
    assert sent[-1]["type"] == "http.response.body"
    return received


def test_unauthorized_small_body_is_discarded_before_final_close():
    messages = [{"type": "http.request", "body": b"{", "more_body": True},
                {"type": "http.request", "body": b"}", "more_body": False}]
    assert len(run(messages)) == 2


def test_unauthorized_large_body_never_reads_unbounded_remainder():
    messages = [{"type": "http.request", "body": b"x" * REJECTED_BODY_LIMIT, "more_body": True},
                {"type": "http.request", "body": b"never read", "more_body": False}]
    assert len(run(messages)) == 1
    assert len(messages) == 1


def test_unauthorized_slow_body_has_deadline(monkeypatch):
    monkeypatch.setattr("vkm_corpus.mcp.http.REJECTED_BODY_TIMEOUT", 0.005)
    assert run([]) == []


def test_unauthorized_expect_continue_does_not_initiate_upload():
    messages = [{"type": "http.request", "body": b"never read", "more_body": False}]
    assert run(messages, headers=[(b"expect", b"100-continue")]) == []
    assert len(messages) == 1


def test_empty_get_does_not_wait_for_disconnect():
    assert run([], method="GET") == []
