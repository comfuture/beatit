"""One bounded, owner-scoped change sampler shared by all workspace streams.

Connections receive a fresh snapshot on every reconnect. Event IDs identify this
process only; missed updates are reconciled from MySQL rather than replayed from
an in-memory history that could disappear during a deployment.
"""

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

SAMPLE_SECONDS = 3
SYSTEM_SECONDS = 60
AUTH_SECONDS = 15
KEEPALIVE_SECONDS = 15
MAX_CONNECTIONS = 64
MAX_OWNER_CONNECTIONS = 8
MAX_PENDING_EVENTS = 16

_SUMMARY = """id,project_id,owner_id,filename,status,created,updated,error,progress,attempt,
             (result IS NOT NULL) AS has_result,
             JSON_UNQUOTE(JSON_EXTRACT(result,'$.generation')) AS result_generation"""


@dataclass(eq=False)
class Subscription:
    owner: str
    job_id: str | None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(MAX_PENDING_EVENTS))
    closed: bool = False


@dataclass
class OwnerState:
    jobs: dict = field(default_factory=dict)
    subscribers: set = field(default_factory=set)


class WorkspaceEvents:
    def __init__(self, store, system_factory):
        self.store = store
        self.system_factory = system_factory
        self.owners = {}
        self.lock = asyncio.Lock()
        self.system_lock = asyncio.Lock()
        self.system = None
        self.system_checked = 0
        self.task = None
        self.epoch = uuid4().hex
        self.sequence = 0

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="workspace-events")

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        for state in self.owners.values():
            for subscriber in state.subscribers:
                subscriber.closed = True
        self.owners.clear()

    async def system_snapshot(self):
        async with self.system_lock:
            if self.system is None or time.monotonic() - self.system_checked >= SYSTEM_SECONDS:
                self.system = await run_in_threadpool(self.system_factory)
                self.system_checked = time.monotonic()
            return self.system

    def frame(self, name, data):
        self.sequence += 1
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.epoch}:{self.sequence}\nevent: {name}\ndata: {payload}\n\n"

    def send(self, subscription, frame):
        if subscription.closed:
            return
        if subscription.queue.full():
            while not subscription.queue.empty():
                subscription.queue.get_nowait()
            subscription.closed = True
            subscription.queue.put_nowait(self.frame("resync", {}))
        else:
            subscription.queue.put_nowait(frame)

    def broadcast(self, state, name, data):
        frame = self.frame(name, data)
        for subscription in state.subscribers:
            self.send(subscription, frame)

    @staticmethod
    def public_summary(job):
        return {key: value for key, value in job.items() if key != "result_generation"}

    @staticmethod
    def signature(job):
        # Leases update `updated` every heartbeat, even when no visible state changes.
        return {key: value for key, value in job.items() if key != "updated"}

    def _read(self, owner, pinned):
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT {_SUMMARY} FROM beatit.jobs WHERE owner_id=%s "
                "ORDER BY created DESC LIMIT 100",
                (owner,),
            ).fetchall()
            missing = pinned - {row["id"] for row in rows}
            if missing:
                placeholders = ",".join(["%s"] * len(missing))
                rows += connection.execute(
                    f"SELECT {_SUMMARY} FROM beatit.jobs WHERE owner_id=%s "
                    f"AND id IN ({placeholders})",
                    (owner, *sorted(missing)),
                ).fetchall()
        for row in rows:
            row["has_result"] = bool(row["has_result"])
        return {row["id"]: row for row in rows}

    async def _refresh_owner(self, owner, state, extra_pin=None):
        pinned = {s.job_id for s in state.subscribers if s.job_id and not s.closed}
        if extra_pin:
            pinned.add(extra_pin)
        jobs = await run_in_threadpool(self._read, owner, pinned)
        changed = [
            identifier
            for identifier, job in jobs.items()
            if identifier not in state.jobs
            or self.signature(job) != self.signature(state.jobs[identifier])
        ]
        removed = state.jobs.keys() - jobs.keys()
        membership_changed = state.jobs.keys() != jobs.keys()
        for identifier in changed:
            if not state.subscribers:
                continue  # Initial history is a summary; no client needs full result rows yet.
            try:
                job = await run_in_threadpool(self.store.get, identifier, owner_id=owner)
            except KeyError:
                continue  # A concurrent delete will be reflected in the next snapshot.
            self.broadcast(state, "job", {"job": job})
        for identifier in removed:
            try:
                job = await run_in_threadpool(self.store.get, identifier, owner_id=owner)
            except KeyError:
                self.broadcast(state, "job-deleted", {"id": identifier})
            else:
                # A job that left the latest-100 window still gets its final transition.
                self.broadcast(state, "job", {"job": job})
        state.jobs = jobs
        if membership_changed:
            self.broadcast(state, "snapshot", self.snapshot(state))

    def snapshot(self, state):
        jobs = sorted(state.jobs.values(), key=lambda job: job["created"], reverse=True)
        return {"jobs": [self.public_summary(job) for job in jobs], "system": self.system}

    async def subscribe(self, owner, job_id=None):
        async with self.lock:
            total = sum(len(state.subscribers) for state in self.owners.values())
            state = self.owners.get(owner)
            if total >= MAX_CONNECTIONS or (
                state and len(state.subscribers) >= MAX_OWNER_CONNECTIONS
            ):
                raise HTTPException(429, "열린 연결이 많습니다. 다른 창을 닫고 다시 시도하세요.")
            await self.system_snapshot()
            if state is None:
                state = OwnerState()
                self.owners[owner] = state
            try:
                await self._refresh_owner(owner, state, job_id)
            except BaseException:
                if not state.subscribers:
                    self.owners.pop(owner, None)
                raise
            subscription = Subscription(owner, job_id)
            state.subscribers.add(subscription)
            self.send(subscription, self.frame("snapshot", self.snapshot(state)))
            self.start()
            return subscription

    async def unsubscribe(self, subscription):
        async with self.lock:
            subscription.closed = True
            state = self.owners.get(subscription.owner)
            if state:
                state.subscribers.discard(subscription)
                if not state.subscribers:
                    self.owners.pop(subscription.owner, None)

    async def sample(self):
        async with self.lock:
            for owner, state in list(self.owners.items()):
                state.subscribers = {s for s in state.subscribers if not s.closed}
                if not state.subscribers:
                    self.owners.pop(owner, None)
                    continue
                await self._refresh_owner(owner, state)
            if self.owners:
                previous = self.system
                current = await self.system_snapshot()
                if current != previous:
                    for state in self.owners.values():
                        self.broadcast(state, "system", current)

    async def _run(self):
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            try:
                await self.sample()
            except Exception:
                logging.exception(
                    "Workspace event snapshot unavailable; clients will resynchronize"
                )
                # Reconnect through ordinary API error handling rather than retaining
                # a live-looking stream with an indefinitely stale snapshot.
                for state in self.owners.values():
                    for subscription in state.subscribers:
                        self.send(subscription, self.frame("resync", {}))
                        subscription.closed = True

    async def stream(self, subscription, request):
        last_auth = last_keepalive = time.monotonic()
        try:
            yield "retry: 5000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                now = time.monotonic()
                if now - last_auth >= AUTH_SECONDS:
                    try:
                        user = await run_in_threadpool(request.app.state.auth.current_user, request)
                    except Exception:
                        logging.warning("Workspace stream authentication unavailable; reconnecting")
                        break
                    if not user or user["id"] != subscription.owner:
                        yield self.frame("session-expired", {})
                        break
                    last_auth = now
                try:
                    frame = await asyncio.wait_for(subscription.queue.get(), timeout=1)
                except TimeoutError:
                    if subscription.closed:
                        break
                    if now - last_keepalive >= KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        last_keepalive = now
                else:
                    yield frame
                    if subscription.closed:
                        break
        finally:
            await self.unsubscribe(subscription)
