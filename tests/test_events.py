"""Shared event sampling, bounded connections and fresh session validation."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from transcription import events as event_module
from transcription.events import MAX_PENDING_EVENTS, WorkspaceEvents


def job(identifier="a", owner="owner-A", **changes):
    return {
        "id": identifier,
        "owner_id": owner,
        "project_id": "project",
        "filename": "private.wav",
        "status": "running",
        "progress": {"stage": "separating", "progress": 0.1},
        "created": 1,
        "updated": 1,
        "error": None,
        "attempt": 1,
        "has_result": False,
        "result_generation": None,
        **changes,
    }


def broker_fixture():
    rows = {"owner-A": {"a": job()}, "owner-B": {"b": job("b", "owner-B")}}
    reads, details, systems = [], [], []

    def get(identifier, owner_id):
        details.append((identifier, owner_id))
        if identifier not in rows[owner_id]:
            raise KeyError(identifier)
        return copy.deepcopy(rows[owner_id][identifier])

    def system():
        systems.append(True)
        return {"ffmpeg": True, "upload_mode": "direct"}

    broker = WorkspaceEvents(SimpleNamespace(get=get), system)

    def read(owner, pinned):
        reads.append((owner, pinned))
        return copy.deepcopy(rows[owner])

    broker._read = read
    return broker, rows, reads, details, systems


def drain(subscription):
    frames = []
    while not subscription.queue.empty():
        frames.append(subscription.queue.get_nowait())
    return frames


def test_one_owner_sample_is_shared_and_unchanged_leases_do_not_emit_events():
    async def scenario():
        broker, rows, reads, details, systems = broker_fixture()
        try:
            a1 = await broker.subscribe("owner-A")
            a2 = await broker.subscribe("owner-A")
            b = await broker.subscribe("owner-B")
            for subscriber in (a1, a2, b):
                drain(subscriber)
            assert not details  # Initial history does not load every full result.
            assert len(systems) == 1
            before = len(reads)
            rows["owner-A"]["a"]["updated"] += 3
            await broker.sample()
            assert len(reads) - before == 2  # Two owners, not three connections.
            assert not details
            assert all(sub.queue.empty() for sub in (a1, a2, b))
            rows["owner-A"]["a"]["progress"]["progress"] = 0.5
            await broker.sample()
            assert details == [("a", "owner-A")]
            assert "event: job\n" in a1.queue.get_nowait()
            assert "event: job\n" in a2.queue.get_nowait()
            assert b.queue.empty()
            del rows["owner-A"]["a"]
            await broker.sample()
            assert "event: job-deleted\n" in a1.queue.get_nowait()
            assert "event: snapshot\n" in a1.queue.get_nowait()
            assert b.queue.empty()
        finally:
            await broker.close()

    asyncio.run(scenario())


def test_slow_client_queue_is_bounded_and_reconnect_receives_a_current_snapshot():
    async def scenario():
        broker, rows, *_ = broker_fixture()
        try:
            subscriber = await broker.subscribe("owner-A", "a")
            for n in range(MAX_PENDING_EVENTS + 3):
                broker.send(subscriber, broker.frame("job", {"n": n}))
            assert subscriber.closed
            assert subscriber.queue.qsize() == 1
            assert "event: resync\n" in subscriber.queue.get_nowait()
            await broker.sample()
            assert "owner-A" not in broker.owners
            rows["owner-A"]["a"]["progress"]["progress"] = 0.9
            replacement = await broker.subscribe("owner-A", "a")
            frame = replacement.queue.get_nowait()
            payload = json.loads(frame.split("data: ", 1)[1])
            assert payload["jobs"][0]["progress"]["progress"] == 0.9
        finally:
            await broker.close()

    asyncio.run(scenario())


def test_stream_uses_fresh_auth_and_cleans_up_on_revocation(monkeypatch):
    monkeypatch.setattr(event_module, "AUTH_SECONDS", 0)

    async def scenario():
        broker, *_ = broker_fixture()
        current = {"user": {"id": "owner-A"}, "checks": 0}

        def validate(request):
            current["checks"] += 1
            return current["user"]

        async def connected():
            return False

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(auth=SimpleNamespace(current_user=validate))),
            state=SimpleNamespace(beatit_user={"id": "owner-A"}),
            is_disconnected=connected,
        )
        try:
            subscription = await broker.subscribe("owner-A")
            stream = broker.stream(subscription, request)
            assert (await anext(stream)).startswith("retry:")
            assert "event: snapshot\n" in await anext(stream)
            current["user"] = None
            assert "event: session-expired\n" in await anext(stream)
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert current["checks"] == 2
            assert not broker.owners
        finally:
            await broker.close()

    asyncio.run(scenario())


def test_connection_limit_is_enforced_and_disconnected_subscriptions_are_released(monkeypatch):
    monkeypatch.setattr(event_module, "MAX_OWNER_CONNECTIONS", 1)

    async def scenario():
        broker, *_ = broker_fixture()
        try:
            subscription = await broker.subscribe("owner-A")
            with pytest.raises(HTTPException) as error:
                await broker.subscribe("owner-A")
            assert error.value.status_code == 429
            await broker.unsubscribe(subscription)
            assert not broker.owners
            await broker.subscribe("owner-A")
        finally:
            await broker.close()

    asyncio.run(scenario())
