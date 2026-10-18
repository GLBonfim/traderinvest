"""Engine service: owns the orchestrator, ticks the simulation clock and serves the protocol.

Lifecycle (ADR-0037):
- start: take the instance lock, load config and state (recovery), listen on 127.0.0.1, print
  `ENGINE_READY port=<n>` on stdout for the supervisor;
- run: tick at `simulation.steps_per_second`, push snapshots and events to authenticated
  clients, answer commands;
- stop (command `shutdown`, SIGINT/SIGTERM, or orphaned): save all state, journal
  `engine_stopped`, release the lock. Open positions stay in the (simulated) account with their
  stops and are recovered on the next start.
- orphan policy: when launched by the desktop (`orphan_timeout`), the engine stops itself if no
  authenticated client is connected for that long, so a crashed UI never leaves a hidden engine
  trading.
- refused start: an untrustworthy configuration or state raises a classified `FxStartupError`
  before the server listens; nothing is saved over the existing data (`__main__` exits with
  `STARTUP_REFUSED_EXIT`, which the desktop never restarts automatically).
- safety pause: an internal error during a tick pauses the engine with reason `safety`; the
  pause is persisted, so a restart does not resume trading. Only the `resume` command does.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.engine.instance import InstanceLock
from app.engine.protocol import (
    CONFIRM_FLATTEN,
    CONFIRM_RESET,
    HEARTBEAT_SECONDS,
    HELLO_TIMEOUT,
    MAX_LINE,
    PROTOCOL_VERSION,
    SNAPSHOT_SECONDS,
    ProtocolError,
    decode,
    encode,
)
from app.fx.config import FxConfig, load_config, merge, save_config
from app.fx.errors import ConfigInvalidError, JournalIntegrityError
from app.fx.orchestrator import ENGINE_VERSION, Orchestrator
from app.fx.store import FxStore

log = logging.getLogger("traderinvest.engine")


class _Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)

    def send(self, msg: dict[str, Any]) -> None:
        data = encode(msg)
        if self.queue.full():  # a slow client loses old messages, never blocks the engine
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait(data)


class EngineService:
    def __init__(
        self,
        root: Path,
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
        orphan_timeout: float | None = None,
    ) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the engine only listens on the loopback interface")
        if len(token) < 16:
            raise ValueError("the session token is too short")
        self.root = root
        self.token = token
        self.host = host
        self.port = port
        self.orphan_timeout = orphan_timeout
        self.lock = InstanceLock(root)
        self.clients: set[_Client] = set()
        self.stopping = asyncio.Event()
        self.bound_port: int | None = None
        self._last_client_at = time.monotonic()
        self.store: FxStore | None = None

    # --- lifecycle --------------------------------------------------------------------------

    def _open(self) -> None:
        self.lock.acquire()
        try:
            self.config: FxConfig = load_config(self.root)
        except (ValueError, OSError) as exc:  # pydantic ValidationError is a ValueError
            raise ConfigInvalidError(
                f"config.json inválido ou ilegível ({type(exc).__name__})"
            ) from exc
        self.store = FxStore(self.root / "fx.db")
        self.orch = Orchestrator(self.config, self.store)
        self.orch.listeners.append(self._on_event)

    async def run(self) -> None:
        try:
            self._open()
            server = await asyncio.start_server(self._handle, self.host, self.port, limit=MAX_LINE)
            self.bound_port = int(server.sockets[0].getsockname()[1])
            (self.root / "engine.json").write_text(
                json.dumps(
                    {
                        "port": self.bound_port,
                        "pid": os.getpid(),
                        "started_at": datetime.now(UTC).isoformat(),
                        "version": ENGINE_VERSION,
                    }
                ),
                encoding="utf-8",
            )
            self._last_client_at = time.monotonic()
            print(f"ENGINE_READY port={self.bound_port}", flush=True)
            log.info("engine ready on %s:%s", self.host, self.bound_port)
            async with server:
                tasks = [asyncio.create_task(c()) for c in (self._ticker, self._pusher)]
                await self.stopping.wait()
                for t in tasks:
                    t.cancel()
                server.close()
                for c in list(self.clients):  # deliver pending replies (e.g. the shutdown ack)
                    with contextlib.suppress(Exception):
                        while not c.queue.empty():
                            c.writer.write(c.queue.get_nowait())
                        await asyncio.wait_for(c.writer.drain(), 2.0)
                    c.writer.close()
        finally:
            self._close()

    def _close(self) -> None:
        try:
            if hasattr(self, "orch") and self.store is not None:
                self.orch.save()
                self.store.append("engine_stopped", {"reason": "shutdown"}, self.orch.market.now)
            if self.store is not None:  # a refused start saves nothing over the existing data
                self.store.close()
            with contextlib.suppress(OSError):
                (self.root / "engine.json").unlink()
        finally:
            self.lock.release()
        log.info("engine stopped")

    def stop(self) -> None:
        self.stopping.set()

    # --- loops ------------------------------------------------------------------------------

    def safe_tick(self) -> None:
        """One tick; an internal error pauses the engine with a persisted `safety` reason."""
        if self.orch.paused:
            return
        try:
            self.orch.tick()
        except Exception as exc:  # a failing tick pauses trading; it never crashes silently
            log.exception("tick failed; engine paused")
            try:
                self.orch.pause("safety", f"erro interno ({type(exc).__name__})")
            except Exception:  # the pause itself could not be persisted: still pause here
                log.exception("could not persist the safety pause")
                self.orch.paused, self.orch.pause_reason = True, "safety"
            self._broadcast(
                {
                    "type": "event",
                    "event": {
                        "kind": "engine_error",
                        "ts": datetime.now(UTC).isoformat(),
                        "payload": {"message": "erro interno: motor em pausa (ver registo)"},
                    },
                }
            )

    async def _ticker(self) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            self.safe_tick()
            interval = 1.0 / self.orch.config.simulation.steps_per_second
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    async def _pusher(self) -> None:
        last_beat = 0.0
        while not self.stopping.is_set():
            if self.clients:
                self._last_client_at = time.monotonic()
                self._broadcast({"type": "snapshot", "data": self.orch.snapshot()})
                if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                    last_beat = time.monotonic()
                    self._broadcast({"type": "heartbeat", "ts": datetime.now(UTC).isoformat()})
            elif (
                self.orphan_timeout is not None
                and time.monotonic() - self._last_client_at > self.orphan_timeout
            ):
                log.warning("no client for %.0fs: stopping (orphan policy)", self.orphan_timeout)
                self.stop()
            await asyncio.sleep(SNAPSHOT_SECONDS)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        for c in list(self.clients):
            c.send(msg)

    def _on_event(self, event: dict[str, Any]) -> None:
        from app.fx.orchestrator import compact_event

        self._broadcast({"type": "event", "event": compact_event(event)})

    # --- connections ------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(writer)
        sender: asyncio.Task[None] | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            hello = decode(line)
            token = hello.get("token")
            if (
                hello["type"] != "hello"
                or hello.get("protocol") != PROTOCOL_VERSION
                or not isinstance(token, str)
                or not hmac.compare_digest(token.encode(), self.token.encode())
            ):
                writer.write(encode({"type": "error", "error": "authentication failed"}))
                await writer.drain()
                return
            client.send(
                {
                    "type": "welcome",
                    "protocol": PROTOCOL_VERSION,
                    "engine_version": ENGINE_VERSION,
                    "environment": self.orch.environment.value,
                }
            )
            client.send({"type": "snapshot", "data": self.orch.snapshot()})
            self.clients.add(client)
            sender = asyncio.create_task(self._sender(client))
            while not self.stopping.is_set():
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = decode(line)
                except ProtocolError as exc:
                    client.send({"type": "error", "error": str(exc)})
                    continue
                if msg["type"] == "command":
                    client.send(self._command(msg))
        except (TimeoutError, ProtocolError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(client)
            self._last_client_at = time.monotonic()
            if sender is not None:
                sender.cancel()
            with contextlib.suppress(Exception):  # deliver pending replies (e.g. shutdown ack)
                while not client.queue.empty():
                    writer.write(client.queue.get_nowait())
                await asyncio.wait_for(writer.drain(), 2.0)
            writer.close()

    @staticmethod
    async def _sender(client: _Client) -> None:
        while True:
            data = await client.queue.get()
            client.writer.write(data)
            await client.writer.drain()

    # --- commands ---------------------------------------------------------------------------

    def _command(self, msg: dict[str, Any]) -> dict[str, Any]:
        cid = str(msg.get("id", ""))
        name = msg.get("name")
        args = msg.get("args") or {}
        try:
            result = self._run_command(str(name), args if isinstance(args, dict) else {})
        except Exception as exc:  # every failure is reported to the client, typed by message
            log.warning("command %s failed: %s", name, exc)
            return {"type": "ack", "id": cid, "ok": False, "error": str(exc)}
        return {"type": "ack", "id": cid, "ok": True, "error": None, "result": result}

    def _run_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        o = self.orch
        store = o.store
        if name == "snapshot":
            return {"snapshot": o.snapshot()}
        if name == "pause":
            o.pause("manual")
            return {}
        if name == "resume":
            o.resume()
            return {}
        if name == "set_speed":
            cfg = merge(o.config, {"simulation": {"steps_per_second": float(args["value"])}})
            save_config(cfg, self.root)
            o.config = cfg
            return {"speed": cfg.simulation.steps_per_second}
        if name == "kill_switch.engage":
            o.engage_kill_switch(str(args.get("reason") or "manual"))
            if args.get("flatten"):
                o.flatten_all("kill switch")
            return {}
        if name == "kill_switch.release":
            o.release_kill_switch(str(args.get("confirmation", "")))
            return {}
        if name == "flatten_all":
            if args.get("confirmation") != CONFIRM_FLATTEN:
                raise ValueError(f"closing every position requires the word {CONFIRM_FLATTEN}")
            return {"orders": len(o.flatten_all("pedido manual"))}
        if name == "config.update":
            patch = args.get("patch")
            if not isinstance(patch, dict):
                raise ValueError("config.update needs a 'patch' object")
            for frozen in ("account_currency", "initial_capital", "pairs", "environment"):
                if frozen in patch and patch[frozen] != getattr(o.config, frozen):
                    raise ValueError(f"{frozen} can only change with an account reset")
            cfg = merge(o.config, patch)
            save_config(cfg, self.root)
            o.apply_config(cfg)
            return {}
        if name == "account.reset":
            if args.get("confirmation") != CONFIRM_RESET:
                raise ValueError(f"an account reset requires the word {CONFIRM_RESET}")
            patch = {
                k: args[k] for k in ("account_currency", "initial_capital", "pairs") if k in args
            }
            cfg = merge(o.config, patch)
            ok, bad = store.verify()
            if not ok:  # never start a new account on top of a broken journal
                raise JournalIntegrityError(f"registo de eventos inválido no evento {bad}")
            save_config(cfg, self.root)
            store.append("account_reset", {"config": cfg.model_dump(mode="json")}, o.market.now)
            store.clear_state()
            self.orch = Orchestrator(cfg, store)
            self.orch.listeners.append(self._on_event)
            return {}
        if name == "journal.verify":
            ok, bad = store.verify()
            return {"ok": ok, "first_bad_seq": bad, "events": store.count()}
        if name == "shutdown":
            self.stop()
            return {}
        raise ValueError(f"unknown command {name!r}")
