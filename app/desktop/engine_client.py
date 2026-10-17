"""Engine supervision and connection for the desktop UI (ADR-0037).

`EngineSupervisor` starts the engine as a CHILD process (QProcess) with a fresh random session
token in its environment, waits for `ENGINE_READY port=<n>`, then connects an `EngineClient`.
In a frozen build the executable re-runs itself with `--engine`; from source it runs
`python -m app.engine`. The engine is started with an orphan timeout, so it stops by itself if
the UI disappears without saying goodbye.
"""

import itertools
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from app.desktop import APP_VERSION
from app.engine.protocol import PROTOCOL_VERSION, TOKEN_ENV, command

ORPHAN_TIMEOUT_S = 20
ENGINE_BUSY_EXIT = 3  # the engine's exit code when another engine holds the data directory
REPO_ROOT = Path(__file__).resolve().parents[2]


def engine_command() -> tuple[str, list[str], str]:
    """(program, arguments, working directory) to start the engine."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, ["--engine", "--orphan-timeout", str(ORPHAN_TIMEOUT_S)], str(Path(exe).parent)
    return (
        sys.executable,
        ["-m", "app.engine", "--orphan-timeout", str(ORPHAN_TIMEOUT_S)],
        str(REPO_ROOT),
    )


class EngineClient(QObject):
    connected = Signal(dict)  # welcome message
    disconnected = Signal()
    snapshot = Signal(dict)
    engine_event = Signal(dict)
    ack = Signal(dict)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.sock = QTcpSocket(self)
        self.sock.connected.connect(self._on_connected)
        self.sock.readyRead.connect(self._on_ready)
        self.sock.disconnected.connect(self.disconnected.emit)
        self.sock.errorOccurred.connect(self._on_error)
        self._buf = bytearray()
        self._token = ""
        self._ids = itertools.count(1)
        self.pending: dict[str, str] = {}  # command id -> name
        self.online = False

    def connect_to(self, port: int, token: str) -> None:
        self._token = token
        self.sock.connectToHost("127.0.0.1", port)

    def _on_connected(self) -> None:
        hello = {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "token": self._token,
            "client": f"desktop/{APP_VERSION}",
        }
        self._write(hello)

    def _on_error(self, _err: QAbstractSocket.SocketError) -> None:
        self.online = False
        self.failed.emit(self.sock.errorString())

    def _write(self, msg: dict[str, Any]) -> None:
        self.sock.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())

    def send(self, name: str, **args: Any) -> str:
        cid = f"ui-{next(self._ids)}"
        self.pending[cid] = name
        self._write(command(cid, name, **args))
        return cid

    def _on_ready(self) -> None:
        self._buf.extend(bytes(self.sock.readAll().data()))
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                break
            line = bytes(self._buf[:i])
            del self._buf[: i + 1]
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            kind = msg.get("type")
            if kind == "welcome":
                self.online = True
                self.connected.emit(msg)
            elif kind == "snapshot":
                self.snapshot.emit(msg["data"])
            elif kind == "event":
                self.engine_event.emit(msg["event"])
            elif kind == "ack":
                msg["name"] = self.pending.pop(str(msg.get("id")), None)
                self.ack.emit(msg)
            elif kind == "error":
                self.failed.emit(str(msg.get("error")))

    def close(self) -> None:
        self.sock.disconnectFromHost()


class EngineSupervisor(QObject):
    status = Signal(str, str)  # (code, detail): starting | ready | stopped | error
    log_line = Signal(str)

    def __init__(self, client: EngineClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_proc_error)
        self.token = ""
        self.port: int | None = None
        self.stopping = False
        self.restarts = 0
        self.busy_retries = 0
        self.last_error = ""

    def start(self) -> None:
        self.stopping = False
        self.token = secrets.token_urlsafe(32)
        env = QProcessEnvironment.systemEnvironment()
        env.insert(TOKEN_ENV, self.token)
        env.insert("PYTHONUNBUFFERED", "1")
        program, args, cwd = engine_command()
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(cwd)
        self.status.emit("starting", "")
        self.proc.start(program, args)

    def _on_stdout(self) -> None:
        text = bytes(self.proc.readAllStandardOutput().data()).decode("utf-8", "replace")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ENGINE_READY port="):
                self.port = int(line.split("=", 1)[1])
                self.client.connect_to(self.port, self.token)
                self.status.emit("ready", str(self.port))
            elif line.startswith("ENGINE_ERROR"):
                self.last_error = line.removeprefix("ENGINE_ERROR").strip()
                self.log_line.emit(line)
            elif line:
                self.log_line.emit(line)

    def _on_stderr(self) -> None:
        text = bytes(self.proc.readAllStandardError().data()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                self.log_line.emit(line.strip())

    def _on_proc_error(self, err: QProcess.ProcessError) -> None:
        if not self.stopping:
            self.status.emit("error", f"processo do motor: {err.name}")

    def _on_finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        if self.stopping:
            self.status.emit("stopped", str(code))
            return
        if code == ENGINE_BUSY_EXIT and self.busy_retries < 10:
            # a previous engine still holds the data directory (e.g. after a UI crash); it
            # stops by itself within ORPHAN_TIMEOUT_S, so wait for it instead of failing
            self.busy_retries += 1
            self.status.emit("waiting", "a aguardar que o motor anterior termine")
            QTimer.singleShot(3000, self.start)
            return
        if code == ENGINE_BUSY_EXIT:
            self.status.emit("error", self.last_error or "outro motor está a usar os dados")
            return
        self.status.emit("stopped", str(code))
        if self.restarts < 2:
            self.restarts += 1  # an unexpected exit: restart; recovery restores the state
            QTimer.singleShot(1500, self.start)

    def stop(self, wait_ms: int = 15000) -> bool:
        """Asks the engine to shut down cleanly; kills it only if it does not exit in time."""
        self.stopping = True
        if self.proc.state() == QProcess.ProcessState.NotRunning:
            return True
        if self.client.online:
            self.client.send("shutdown")
            self.client.sock.flush()
        if self.proc.waitForFinished(wait_ms):
            return True
        self.proc.kill()
        self.proc.waitForFinished(3000)
        return False
