"""Read-only projection of the independent IBKR candle laboratory into the desktop UI."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.fx.config import data_dir
from app.fx.store import FxStore


def discover_lab() -> Path:
    explicit = os.environ.get("TRADERINVEST_FX_PAPER_LAB")
    if explicit:
        return Path(explicit)
    executable = Path(sys.executable).parent
    config = executable / "paper-monitor.json"
    if config.is_file():
        try:
            return Path(json.loads(config.read_text(encoding="utf-8"))["lab_dir"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    candidates = (
        Path(__file__).resolve().parents[2] / "data/fx/candle-lab-funded-paper",
        Path.cwd() / "data/fx/candle-lab-funded-paper",
        executable / "data/fx/candle-lab-funded-paper",
        data_dir() / "candle-lab-funded-paper",
    )
    return next((p for p in candidates if (p / "status.json").is_file()), candidates[-1])


def read_monitor(
    root: Path, now: datetime | None = None, cache_dir: Path | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "root": str(root),
        "fresh": False,
        "events": [],
        "journal_ok": None,
        "orders": [],
        "tests": [],
    }
    try:
        status = json.loads((root / "status.json").read_text(encoding="utf-8"))
        observed = datetime.fromisoformat(status["observed_at"])
        age = ((now or datetime.now(UTC)) - observed).total_seconds()
        result.update(status=status, age=age, fresh=-5 <= age <= 180)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["error"] = f"Estado do laboratório indisponível: {exc}"
        return result
    try:
        # Recover only private copies; never open or mutate the source database.
        cache = cache_dir or Path(
            os.environ.get("TRADERINVEST_FX_MONITOR_CACHE", str(data_dir() / "paper-monitor-cache"))
        )
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="view-", dir=cache) as tmp:
            copy = Path(tmp) / "pilot.sqlite"
            for suffix in ("", "-wal"):
                source = root / ("pilot.sqlite" + suffix)
                if source.exists():
                    shutil.copyfile(source, Path(str(copy) + suffix))
            db = sqlite3.connect(copy, timeout=0.2)
            try:
                store = FxStore.__new__(FxStore)
                store.db = db
                result["journal_ok"] = store.verify()[0]
                if not result["journal_ok"]:
                    raise ValueError("integridade do diário não confirmada")
                result["events"] = store.recent(150)
                result["counts"] = dict(
                    db.execute("SELECT kind,COUNT(*) FROM journal GROUP BY kind")
                )
                test_events = store.recent(
                    1000,
                    ("user_demo_test_intent", "user_demo_test_filled", "user_demo_test_closed"),
                )
                fills = store.recent(500, ("pilot_order_filled", "pilot_close"))
                totals: dict[str, float] = {}
                result["results_complete"] = True
                for (body,) in db.execute("SELECT payload FROM journal WHERE kind='pilot_close'"):
                    trade = json.loads(body)
                    if not trade.get("model_id") or not isinstance(
                        trade.get("pnl_estimate"), (int, float)
                    ):
                        result["results_complete"] = False
                        continue
                    mid = trade["model_id"]
                    totals[mid] = totals.get(mid, 0.0) + float(trade["pnl_estimate"])
                result["agent_results"] = totals
            finally:
                db.close()
        tests: dict[str, dict[str, Any]] = {}
        orders = []
        closed_trades = []
        for event in test_events:
            p = event["payload"]
            ref = p.get("ref", p.get("client_order_id", ""))
            test = tests.setdefault(ref, {"ref": ref, "state": "Intenção registrada"})
            if event["kind"] == "user_demo_test_filled":
                test.update(
                    entry=p.get("avg_price"),
                    units=p.get("filled_units"),
                    symbol=p.get("pair"),
                    state="Entrada preenchida",
                )
                orders.append(
                    {
                        "origin": "Teste de integração",
                        "ref": ref,
                        "symbol": p.get("pair"),
                        "side": p.get("side"),
                        "units": p.get("filled_units"),
                        "price": p.get("avg_price"),
                        "state": "Preenchida",
                        "time": event["ts"],
                    }
                )
            elif event["kind"] == "user_demo_test_closed":
                close = p.get("close", {})
                test.update(
                    exit=close.get("avg_price"),
                    pnl=p.get("net_eur_estimate"),
                    fees=p.get("commissions_eur_estimate"),
                    state="Fechado",
                )
                orders.append(
                    {
                        "origin": "Teste de integração",
                        "ref": close.get("client_order_id", ref),
                        "symbol": close.get("pair"),
                        "side": close.get("side"),
                        "units": close.get("filled_units"),
                        "price": close.get("avg_price"),
                        "state": "Fechamento preenchido",
                        "time": event["ts"],
                    }
                )
        opened = {
            e["payload"]["coid"]: e["payload"] for e in fills if e["kind"] == "pilot_order_filled"
        }
        for event in fills:
            p = event["payload"]
            closed = event["kind"] == "pilot_close"
            entry = opened.get(p.get("entry_id"), {}) if closed else p
            units = entry.get("units")
            side = None if units is None else "sell" if units < 0 else "buy"
            if closed and side:
                side = "buy" if side == "sell" else "sell"
            if closed:
                closed_trades.append(
                    {
                        "symbol": p.get("symbol"),
                        "model_id": p.get("model_id"),
                        "entry": entry.get("price"),
                        "exit": p.get("exit_price"),
                        "units": abs(units) if units is not None else None,
                        "pnl": p.get("pnl_estimate"),
                        "reason": p.get("reason"),
                        "time": event["ts"],
                        "ref": p.get("entry_id"),
                    }
                )
            orders.append(
                {
                    "origin": "Agente",
                    "ref": p.get("coid", p.get("entry_id")),
                    "symbol": p.get("symbol"),
                    "side": side,
                    "units": abs(units) if units is not None else None,
                    "price": p.get("price", p.get("exit_price")),
                    "state": "Fechada" if event["kind"] == "pilot_close" else "Preenchida",
                    "time": event["ts"],
                }
            )
        result.update(
            closed_trades=closed_trades,
            tests=list(tests.values()),
            orders=sorted(orders, key=lambda x: x["time"], reverse=True),
        )
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        result.update(journal_ok=None, journal_error=f"Não foi possível conferir o diário: {exc}")
    return result


def pair_reason(context: dict[str, Any], funded: bool) -> str:
    if not context.get("tradable"):
        if context.get("volatility_rank", 0) >= 0.98:
            return "Analista vetou: volatilidade extrema (percentil >=98%)."
        return "Analista vetou: sessão, volatilidade ou dados fora dos critérios."
    patterns = context.get("patterns", [])
    if not patterns:
        return "Sem padrão direcional com contexto atendido."
    if funded and all(p.get("direction") == 1 for p in patterns):
        return "Padrão de compra: entradas BUY bloqueadas no modo financiado."
    return "Padrão observado; ainda depende do especialista, cotação e gestor de risco."
