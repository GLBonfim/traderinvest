"""Engine <-> UI protocol, version 1 (ADR-0037).

Transport: TCP on 127.0.0.1 only, one JSON object per line (UTF-8), max 8 MiB per line.

Client -> engine
    {"type": "hello", "protocol": 1, "token": "<session token>", "client": "desktop/0.1.0"}
    {"type": "command", "id": "<client id>", "name": "<command>", "args": {...}}
Engine -> client
    {"type": "welcome", "protocol": 1, "engine_version": "...", "environment": "simulation"}
    {"type": "snapshot", "data": {...}}         full state, pushed periodically
    {"type": "event", "event": {...}}           journal events as they happen
    {"type": "ack", "id": "...", "ok": true|false, "error": "...", "result": {...}}
    {"type": "heartbeat", "ts": "..."}
    {"type": "error", "error": "..."}           then the connection closes

The first client message must be a valid `hello` within `HELLO_TIMEOUT` seconds; the token is
compared in constant time. Destructive commands carry a typed confirmation word.
"""

import json
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE = 8 * 1024 * 1024
HELLO_TIMEOUT = 5.0
HEARTBEAT_SECONDS = 1.0
SNAPSHOT_SECONDS = 0.5
TOKEN_ENV = "TRADERINVEST_ENGINE_TOKEN"  # noqa: S105 - a variable name, not a secret

CONFIRM_FLATTEN = "FECHAR"
CONFIRM_RESET = "REINICIAR"
CONFIRM_KILL_RELEASE = "LIBERTAR"

COMMANDS = frozenset(
    {
        "snapshot",
        "pause",
        "resume",
        "set_speed",
        "kill_switch.engage",
        "kill_switch.release",
        "flatten_all",
        "config.update",
        "account.reset",
        "journal.verify",
        "shutdown",
    }
)


class ProtocolError(ValueError):
    pass


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode()


def decode(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE:
        raise ProtocolError("message too large")
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("a message must be an object with a string 'type'")
    return msg


def command(cid: str, name: str, **args: Any) -> dict[str, Any]:
    if name not in COMMANDS:
        raise ProtocolError(f"unknown command {name!r}")
    return {"type": "command", "id": cid, "name": name, "args": args}
