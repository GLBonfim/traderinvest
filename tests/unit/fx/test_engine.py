"""Engine process protocol: loopback only, token authentication, commands, single instance."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.engine.instance import EngineAlreadyRunningError, InstanceLock
from app.engine.protocol import (
    CONFIRM_FLATTEN,
    CONFIRM_KILL_RELEASE,
    ProtocolError,
    command,
    decode,
    encode,
)
from app.engine.service import EngineService
from app.fx.config import save_config
from tests.unit.fx.helpers import small_config

TOKEN = "t" * 40


def test_protocol_encoding() -> None:
    msg = command("1", "pause")
    assert decode(encode(msg)) == msg
    for bad in (b"not json\n", b"[1,2]\n", b'{"no_type": 1}\n'):
        with pytest.raises(ProtocolError):
            decode(bad)
    with pytest.raises(ProtocolError):
        command("1", "send_order")  # there is no command to place an order by hand


def test_engine_refuses_non_loopback_and_short_tokens(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        EngineService(tmp_path, TOKEN, host="0.0.0.0")  # noqa: S104 - the point of the test
    with pytest.raises(ValueError, match="token"):
        EngineService(tmp_path, "short")


def test_single_instance_lock(tmp_path: Path) -> None:
    a = InstanceLock(tmp_path)
    a.acquire()
    with pytest.raises(EngineAlreadyRunningError):
        InstanceLock(tmp_path).acquire()
    a.release()
    b = InstanceLock(tmp_path)
    b.acquire()
    b.release()


async def _session(root: Path) -> dict[str, Any]:
    svc = EngineService(root, TOKEN)
    task = asyncio.create_task(svc.run())
    for _ in range(400):
        if svc.bound_port:
            break
        await asyncio.sleep(0.05)
    port = svc.bound_port
    assert port
    out: dict[str, Any] = {"engine_json": json.loads((root / "engine.json").read_text())}

    # wrong token: refused and disconnected
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(encode({"type": "hello", "protocol": 1, "token": "x" * 40}))
    await w.drain()
    out["bad"] = decode(await r.readline())
    out["bad_eof"] = await r.readline()
    w.close()

    r, w = await asyncio.open_connection("127.0.0.1", port, limit=8 * 1024 * 1024)
    w.write(encode({"type": "hello", "protocol": 1, "token": TOKEN}))
    await w.drain()
    out["welcome"] = decode(await r.readline())
    out["first"] = decode(await r.readline())

    async def call(name: str, **args: Any) -> dict[str, Any]:
        cid = f"c-{name}"
        w.write(encode(command(cid, name, **args)))
        await w.drain()
        while True:
            m = decode(await r.readline())
            if m["type"] == "ack" and m["id"] == cid:
                return m

    out["flatten_no_word"] = await call("flatten_all", confirmation="sim")
    out["flatten"] = await call("flatten_all", confirmation=CONFIRM_FLATTEN)
    out["kill"] = await call("kill_switch.engage", reason="teste")
    out["release_bad"] = await call("kill_switch.release", confirmation="ok")
    out["release"] = await call("kill_switch.release", confirmation=CONFIRM_KILL_RELEASE)
    out["frozen"] = await call("config.update", patch={"initial_capital": 5})
    out["update"] = await call("config.update", patch={"risk": {"max_entries_per_day": 3}})
    out["bad_update"] = await call("config.update", patch={"risk": {"max_leverage": 500}})
    out["verify"] = await call("journal.verify")
    out["pause"] = await call("pause")
    out["snap"] = (await call("snapshot"))["result"]["snapshot"]
    out["shutdown"] = await call("shutdown")
    await asyncio.wait_for(task, 20)
    w.close()
    return out


def test_engine_session_end_to_end(tmp_path: Path) -> None:
    save_config(small_config(), tmp_path)
    out = asyncio.run(_session(tmp_path))
    assert out["engine_json"]["port"] > 0
    assert out["bad"] == {"type": "error", "error": "authentication failed"}
    assert out["bad_eof"] == b""
    assert out["welcome"]["type"] == "welcome" and out["welcome"]["environment"] == "simulation"
    assert out["first"]["type"] == "snapshot"
    assert not out["flatten_no_word"]["ok"] and out["flatten"]["ok"]
    assert out["kill"]["ok"] and not out["release_bad"]["ok"] and out["release"]["ok"]
    assert not out["frozen"]["ok"] and "reset" in out["frozen"]["error"]
    assert out["update"]["ok"] and not out["bad_update"]["ok"]
    assert out["verify"]["result"]["ok"] is True
    assert out["snap"]["paused"] is True
    assert out["snap"]["config"]["risk"]["max_entries_per_day"] == 3
    assert out["shutdown"]["ok"]
    assert not (tmp_path / "engine.json").exists()  # removed on clean stop
    # the next start recovers the saved state, including the configuration change
    out2 = asyncio.run(_restart_snapshot(tmp_path))
    assert out2["recovered"] is True
    assert out2["config"]["risk"]["max_entries_per_day"] == 3


async def _restart_snapshot(root: Path) -> dict[str, Any]:
    svc = EngineService(root, TOKEN)
    task = asyncio.create_task(svc.run())
    while not svc.bound_port:
        await asyncio.sleep(0.05)
    r, w = await asyncio.open_connection("127.0.0.1", svc.bound_port, limit=8 * 1024 * 1024)
    w.write(encode({"type": "hello", "protocol": 1, "token": TOKEN}))
    await w.drain()
    await r.readline()
    snap: dict[str, Any] = decode(await r.readline())["data"]
    svc.stop()
    await asyncio.wait_for(task, 20)
    w.close()
    return snap
