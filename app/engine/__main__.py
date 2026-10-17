"""`python -m app.engine` — runs the trading engine.

The session token is read from the environment (`TRADERINVEST_ENGINE_TOKEN`), never from the
command line (visible to other processes). Without one, a random token is generated and written
to `<data>/engine.token` (readable by the current user) for manual clients.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.engine.instance import EngineAlreadyRunningError
from app.engine.protocol import TOKEN_ENV
from app.engine.service import EngineService
from app.fx.config import data_dir


def setup_logging(root: Path) -> None:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        root / "logs" / "engine.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="traderinvest-engine")
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--orphan-timeout", type=float, default=None)
    args = ap.parse_args(argv)
    root: Path = args.data_dir or data_dir()
    root.mkdir(parents=True, exist_ok=True)
    setup_logging(root)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        token = secrets.token_urlsafe(32)
        path = root / "engine.token"
        path.write_text(token, encoding="utf-8")
        print(f"token written to {path}", file=sys.stderr)
    service = EngineService(root, token, port=args.port, orphan_timeout=args.orphan_timeout)

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Windows has no loop signal handlers: Ctrl+C arrives as KeyboardInterrupt instead
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, service.stop)
        await service.run()

    try:
        asyncio.run(runner())
    except EngineAlreadyRunningError as exc:
        print(f"ENGINE_ERROR {exc}", flush=True)
        return 3
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
