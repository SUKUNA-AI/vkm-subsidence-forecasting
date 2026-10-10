import asyncio
import os
import threading

import pytest

from vkm_corpus.update.barrier import AdmissionBarrierMiddleware, BarrierUnavailable, ReceiverBarrier


def test_pause_prevents_new_admission_but_waits_for_existing_response():
    barrier = ReceiverBarrier("receiver")
    lease = barrier.acquire()
    barrier.pause("switch")
    with pytest.raises(BarrierUnavailable, match="paused"):
        barrier.acquire()
    with pytest.raises(BarrierUnavailable, match="did not drain"):
        barrier.drain("switch", .001)
    assert barrier.status()["active_requests"] == 1
    lease.release()
    lease.release()
    barrier.drain("switch", .1)
    barrier.resume("switch")
    with barrier.acquire():
        assert barrier.status()["active_requests"] == 1


def test_drain_wakes_when_last_request_finishes():
    barrier = ReceiverBarrier("receiver")
    lease = barrier.acquire()
    barrier.pause("switch")
    thread = threading.Thread(target=lease.release)
    thread.start()
    barrier.drain("switch", 1)
    thread.join()
    assert barrier.status()["active_requests"] == 0


@pytest.mark.parametrize("operation", ["pause", "drain", "resume"])
def test_other_maintenance_owner_cannot_take_receiver(operation):
    barrier = ReceiverBarrier("receiver")
    barrier.pause("first")
    with pytest.raises(BarrierUnavailable):
        getattr(barrier, operation)("second", .1) if operation == "drain" else getattr(barrier, operation)("second")
    assert barrier.status()["paused"]


def test_inherited_process_barrier_is_not_a_cross_process_proof(monkeypatch):
    barrier = ReceiverBarrier("receiver")
    monkeypatch.setattr("vkm_corpus.update.barrier.os.getpid", lambda: barrier.pid + 1)
    with pytest.raises(BarrierUnavailable, match="another process"):
        barrier.acquire()
    with pytest.raises(BarrierUnavailable):
        barrier.status()


def test_pure_asgi_lease_covers_all_streamed_bytes():
    async def run():
        barrier = ReceiverBarrier("receiver")
        bodies = []
        async def application(scope, receive, send):
            assert barrier.status()["active_requests"] == 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
            barrier.pause("switch")
            with pytest.raises(BarrierUnavailable):
                barrier.drain("switch", .001)
            await send({"type": "http.response.body", "body": b"last", "more_body": False})
        async def receive():
            return {"type": "http.request", "body": b""}
        async def send(message):
            bodies.append(message)
            assert barrier.status()["active_requests"] == 1
        await AdmissionBarrierMiddleware(application, provider=lambda: barrier)(
            {"type": "http", "path": "/v1/evidence"}, receive, send)
        assert len(bodies) == 3 and barrier.status()["active_requests"] == 0
        barrier.drain("switch", .1)
    asyncio.run(run())


def test_paused_receiver_returns_api_error_without_calling_content_handler():
    async def run():
        barrier = ReceiverBarrier("receiver")
        barrier.pause("switch")
        messages = []
        async def application(*_):
            pytest.fail("content handler entered after admission closed")
        async def receive():
            return {"type": "http.request", "body": b""}
        async def send(message):
            messages.append(message)
        await AdmissionBarrierMiddleware(application, provider=lambda: barrier)(
            {"type": "http", "path": "/v1/pages"}, receive, send)
        assert messages[0]["status"] == 503
        assert b"DEPENDENCY_UNAVAILABLE" in messages[-1]["body"]
    asyncio.run(run())


def test_cancelled_asgi_response_releases_lease():
    async def run():
        barrier = ReceiverBarrier("receiver")
        async def application(*_):
            raise asyncio.CancelledError()
        async def receive():
            return {"type": "http.request"}
        async def send(_):
            pass
        with pytest.raises(asyncio.CancelledError):
            await AdmissionBarrierMiddleware(application, provider=lambda: barrier)(
                {"type": "http", "path": "/v1/pages"}, receive, send)
        assert barrier.status()["active_requests"] == 0
    asyncio.run(run())


def test_async_native_generation_guard_is_awaited_before_content():
    from fastapi.testclient import TestClient
    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.api.fixtures import synthetic_service
    from tempfile import TemporaryDirectory
    from pathlib import Path
    calls = []
    async def guard():
        calls.append("native observation")
        return {"status": "UNAVAILABLE"}
    with TemporaryDirectory() as folder:
        service, _, _ = synthetic_service(Path(folder))
        service.deps.generation_guard = guard
        with TestClient(create_app(service, ApiConfig(read_tokens={"x": "read"}))) as client:
            response = client.get("/v1/sources/VKM-SRC-001", headers={"Authorization": "Bearer x"})
            assert response.status_code == 503 and calls == ["native observation"]
            assert "title" not in response.json().get("item", {}) if response.json().get("item") else True


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
def test_shared_gate_lease_covers_streamed_response_and_releases_after_cancellation(tmp_path):
    import fcntl
    gate = tmp_path / "admission.lock"
    barrier = ReceiverBarrier("receiver", gate_path=gate)
    async def run():
        async def application(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"stream", "more_body": True})
            raise asyncio.CancelledError()
        async def receive():
            return {"type": "http.request"}
        async def send(message):
            fd = os.open(gate, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        with pytest.raises(asyncio.CancelledError):
            await AdmissionBarrierMiddleware(application, provider=lambda: barrier)(
                {"type": "http", "path": "/v1/pages"}, receive, send)
    asyncio.run(run())
    assert barrier.status()["active_requests"] == 0
    fd = os.open(gate, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
@pytest.mark.parametrize("operation", ["acquire", "rebind"])
def test_replaced_gate_inode_cannot_silently_rebind_existing_receiver(tmp_path, operation):
    gate = tmp_path / "admission.lock"
    barrier = ReceiverBarrier("receiver", gate_path=gate)
    with barrier.acquire():
        pass
    gate.rename(tmp_path / "original-admission.lock")
    gate.write_bytes(b"")
    with pytest.raises(BarrierUnavailable, match="inode|replac|gate"):
        if operation == "acquire":
            with barrier.acquire():
                pytest.fail("existing receiver followed an unqualified gate inode")
        else:
            barrier.bind_gate(gate)
    assert barrier.status()["active_requests"] == 0


@pytest.mark.skipif(os.name != "posix", reason="NOT_RUN: POSIX shared admission gate")
def test_receiver_cannot_change_gate_while_a_request_is_active(tmp_path):
    gate = tmp_path / "admission.lock"
    barrier = ReceiverBarrier("receiver", gate_path=gate)
    with barrier.acquire():
        with pytest.raises(BarrierUnavailable, match="active"):
            barrier.bind_gate(tmp_path / "another.lock")
    assert barrier.gate_path == gate
