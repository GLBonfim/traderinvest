"""Single supervised paper coordinator: reconcile BEFORE every restart, never resend orders."""

import argparse
import asyncio
import json
import msvcrt
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.fx.calendar import FxCalendar
from app.fx.ibkr.m5_pipeline import load_eligible
from app.fx.orders import ExecutionError
from app.fx.store import FxStore


def restart_allowed(pilot: dict[str, Any] | None, risk: dict[str, Any] | None, stop: bool) -> bool:
    if not isinstance(pilot, dict) or not isinstance(risk, dict):
        return False
    if (
        not {"open", "pending"} <= pilot.keys()
        or not {"kill_switch", "lock", "reservations"} <= risk.keys()
    ):
        return False
    return bool(
        not stop
        and not pilot["open"]
        and not pilot["pending"]
        and not risk["kill_switch"]
        and not risk["lock"]
        and not risk["reservations"]
    )


async def preflight(root: Path, source: Path, timeframe: str = "M5") -> dict[str, Any]:
    from ib_async import IB, Forex, MarketOrder

    from app.fx.ibkr.candle_lab import FundedPaperExecutor
    from app.fx.ibkr.cash_reconciliation import CashReconciler

    lock = (root / "runner.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    ib = IB()
    store = FxStore(root / "pilot.sqlite")
    try:
        if not restart_allowed(
            store.get_state("pilot"), store.get_state("risk"), (root / "STOP").exists()
        ):
            raise ValueError("state/STOP/ambiguity/risk lock requires intervention; no restart")
        if not store.verify()[0]:
            raise ValueError("journal integrity failed")
        funding = FxStore(root / "funding.sqlite")
        try:
            if not funding.verify()[0]:
                raise ValueError("funding journal integrity failed")
        finally:
            funding.close()
        await ib.connectAsync("127.0.0.1", 4002, clientId=78, readonly=True, timeout=15)
        a = ib.managedAccounts()
        if len(a) != 1 or not a[0].startswith("DU"):
            raise ValueError("single demo DU account required")
        guard = CashReconciler(ib, store, root / "cash-anchor.json", root / "funding.json")
        if await guard():
            raise ValueError("strategy positions not flat")
        if await ib.reqAllOpenOrdersAsync():
            raise ValueError("broker has orders; no restart")
        models, _ = load_eligible(source)
        opened = FxCalendar().is_open(datetime.now(UTC))
        result = {
            "checked_at": datetime.now(UTC).isoformat(),
            "cash_reconciled": True,
            "broker_orders": 0,
            "strategy_positions": {},
            "m5_eligible_models": list(models),
            "market_open": opened,
            "arm": bool(opened and (models or timeframe == "H1")),
            "runtime_timeframe": timeframe,
            "orders_sent": 0,
        }
        if opened:
            quotes = await FundedPaperExecutor(ib, currency="EUR").live_quotes(
                ["EURUSD", "EURGBP", "EURJPY"]
            )
            if len(quotes) != 3:
                raise ValueError("fresh LIVE quotes absent")
            result["live_pairs"] = list(quotes)
        if result["arm"]:
            previews = {}
            for symbol in ("EURUSD", "EURGBP", "EURJPY"):
                c = Forex(symbol)
                await ib.qualifyContractsAsync(c)
                response = await asyncio.wait_for(
                    ib.whatIfOrderAsync(c, MarketOrder("SELL", 1000, account=a[0], tif="DAY")), 20
                )
                if response is None or response.warningText:
                    raise ValueError("broker preview warning/rejection")
                previews[symbol + "_SELL"] = {"response": type(response).__name__, "errors": []}
            pre = {
                "observed_at": datetime.now(UTC).isoformat(),
                "paper_account": "DU*****" + a[0][-2:],
                "whatif_only": True,
                "previews": previews,
                "actual_orders": 0,
            }
            path = root.parent / "candle-lab-api-preflight.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(pre, indent=2))
            temp.replace(path)
        store.append("m5_supervisor_reconciliation", result)
        return result
    finally:
        ib.disconnect()
        store.close()
        lock.close()


def run(root: Path, source: Path, timeframe: str = "M5") -> int:
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "supervisor.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    child = None

    def status(state: str, **kwargs: Any) -> None:
        data = {
            "state": state,
            "supervisor_pid": os.getpid(),
            "observed_at": datetime.now(UTC).isoformat(),
            "timeframe": timeframe,
            **kwargs,
        }
        p = root / "supervisor-status.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(p)
        if state == "waiting_market_open":
            canonical = root / "status.json"
            current = json.loads(canonical.read_text(encoding="utf-8"))
            current.update(
                state=state,
                armed=False,
                pid=None,
                supervisor_pid=os.getpid(),
                observed_at=data["observed_at"],
                execution_mode="waiting_market_open_no_orders",
                timeframe=timeframe,
                runtime_supervision=data,
                contexts={},
            )
            temporary = canonical.with_suffix(".tmp")
            temporary.write_text(json.dumps(current, indent=2, default=str), encoding="utf-8")
            temporary.replace(canonical)
        print(json.dumps(data), flush=True)

    try:
        failures = 0
        while True:
            if (root / "STOP").exists():
                status("paused_STOP")
                time.sleep(30)
                continue
            try:
                proof = asyncio.run(preflight(root, source, timeframe))
                if not proof["market_open"]:
                    status("waiting_market_open", preflight=proof, orders_sent=0)
                    while not FxCalendar().is_open(datetime.now(UTC)):
                        if (root / "STOP").exists():
                            break
                        time.sleep(30)
                        status("waiting_market_open", preflight=proof, orders_sent=0)
                    # Always reconcile again with fresh quotes before launching.
                    continue
                args = [
                    sys.executable,
                    "-B",
                    "-m",
                    "app.fx.ibkr.candle_lab",
                    "--dir",
                    str(root),
                    "--port",
                    "4002",
                    "--client-id",
                    "62",
                    "--consent",
                    "EXPERIMENTO-SOMENTE-DEMO",
                    "--funded-eur-only",
                    "--cash-ledger",
                    "--timeframe",
                    timeframe,
                    "--m5-source",
                    str(source),
                ]
                if timeframe == "H1":
                    args.append("--lesson-h1")
                if proof["arm"]:
                    args.append("--arm")
                with (root / "runtime-m5.log").open("a", encoding="utf-8") as log:
                    child = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
                    status(
                        "running",
                        child_pid=child.pid,
                        preflight=proof,
                        mode="armed_demo"
                        if proof["arm"]
                        else "observation_only_no_eligible_models",
                    )
                    failures = 0
                    while child.poll() is None:
                        status(
                            "running",
                            child_pid=child.pid,
                            preflight=proof,
                            mode="armed_demo"
                            if proof["arm"]
                            else "observation_only_no_eligible_models",
                        )
                        time.sleep(30)
                status("child_stopped", exit_code=child.returncode)
                child = None
            except (ValueError, OSError, TimeoutError, ConnectionError, ExecutionError) as e:
                failures += 1
                status("waiting_reconciliation", error=str(e), failures=failures, orders_sent=0)
            # A fresh complete preflight is mandatory next iteration. Nothing resends an intent.
            time.sleep(min(300, 30 * max(1, failures)))
    finally:
        # Do not abandon a live broker session because the supervising terminal was closed.
        # Its runner lock still prevents a competing worker. No orders or STOP are modified.
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path("data/fx/candle-lab-funded-paper"))
    parser.add_argument("--source", type=Path, default=Path("data/fx/training/run-m5-20261018"))
    parser.add_argument("--timeframe", choices=("H1", "M5"), default="M5")
    a = parser.parse_args()
    raise SystemExit(run(a.dir.resolve(), a.source.resolve(), a.timeframe))
