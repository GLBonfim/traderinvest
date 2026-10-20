"""Explicit prospective PAPER experiment: market analyst -> three candle specialists.

Previous historical rejection is retained. This is a new, unvalidated combined strategy;
trained forecasts only rank pattern proposals, never manufacture candle detections.
"""

import argparse
import asyncio
import json
import math
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.candles.engine import CandlestickEngine
from app.candles.knowledge import VERSION as CANDLE_KNOWLEDGE_VERSION
from app.candles.knowledge import explain_pattern
from app.fx.agents.trained import LinearModel, feature_frame, model_signals, retirement_decision
from app.fx.ibkr.budget_settings import BudgetRiskManager, apply_settings, configure, load_settings
from app.fx.ibkr.execution import IbkrPaperExecutor
from app.fx.ibkr.paper import mask
from app.fx.ibkr.pilot import (
    LIVE_PORTS,
    MAX_BAR_SPREAD_PIPS,
    SIGNAL_MAX_DELAY,
    PaperPilot,
    PilotConfig,
    PilotStopError,
    closed_model_bars,
    ib_bars_fetcher,
    ib_positions_fetcher,
    mid_of,
)
from app.fx.models import Side, pair
from app.fx.orders import ExecutionError, OrderRequest, OrderStatus
from app.fx.risk import RiskConfig
from app.fx.store import FxStore

SCHEMA = "fx-candle-paper-experiment/1"
CONSENT = "EXPERIMENTO-SOMENTE-DEMO"
ROLES = {"reversao": "reversal", "continuacao": "continuation", "impulso": "momentum"}


def market_analysis(mid: pd.DataFrame) -> dict[str, Any]:
    """The analyst consumes closed bars only; context is passed verbatim to specialists."""
    f = feature_frame(mid, "H1").iloc[-1]
    analysis = CandlestickEngine().analyze(mid.assign(volume=0.0), timeframe="H1")
    obs = analysis.observations
    obs = obs[obs["ts"] == mid.index[-1]]
    patterns = [
        {
            "name": str(r.pattern),
            "family": str(r.family),
            "direction": 1 if r.orientation == "bullish" else -1,
            "strength": float(r.strength),
            "knowledge": explain_pattern(str(r.pattern)),
        }
        for r in obs.itertuples()
        if r.orientation in ("bullish", "bearish") and r.context_requirements_met
    ]
    finite = all(math.isfinite(float(f[n])) for n in ("atr", "er", "volatility_rank", "sma_gap"))
    return {
        "analyst": "analista-mercado-v1",
        "knowledge_version": CANDLE_KNOWLEDGE_VERSION,
        "candle_observations": [
            {
                "name": str(r.pattern),
                "orientation": str(r.orientation),
                "context_requirements_met": bool(r.context_requirements_met),
                "prior_trend_observed": str(r.prior_trend),
                "strength_is_probability": False,
                "knowledge": explain_pattern(str(r.pattern)),
            }
            for r in obs.itertuples()
        ],
        "bar_open": mid.index[-1].isoformat(),
        "bar_close": (mid.index[-1] + pd.Timedelta(hours=1)).isoformat(),
        "regime": "tendencia" if f["er"] >= 0.25 else "lateral",
        "trend": 1 if f["sma_gap"] > 0 else -1 if f["sma_gap"] < 0 else 0,
        "efficiency": float(f["er"]),
        "volatility_rank": float(f["volatility_rank"]),
        "atr": float(f["atr"]),
        "tradable": bool(finite and f["tradable"] and f["volatility_rank"] < 0.98),
        "patterns": patterns,
    }


def specialist(
    role: str, context: dict[str, Any], model: LinearModel, mid: pd.DataFrame
) -> tuple[pd.Series, str] | None:
    if not context["tradable"]:
        return None
    candidates = [
        p for p in context["patterns"] if p["family"] == ROLES[role] and p["strength"] >= 0.15
    ]
    if role in ("continuacao", "impulso"):
        candidates = [p for p in candidates if p["direction"] == context["trend"]]
    elif context["efficiency"] > 0.65:
        return None
    if not candidates:
        return None
    p = max(candidates, key=lambda item: item["strength"])
    forecast = model_signals(mid, model).iloc[-1]
    agreement = int(forecast["dir"]) == p["direction"]
    score = min(0.95, 0.56 + 0.24 * p["strength"] + (0.1 if agreement else 0.0))
    return pd.Series(
        {
            "dir": p["direction"],
            "stop_dist": 2 * context["atr"],
            "target_dist": 3 * context["atr"],
            "score": score,
            "max_hold": 8,
        }
    ), p["name"]


def create_cohort(source: Path, destination: Path) -> dict[str, Any]:
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("statistically_approved") is not False:
        raise ValueError("expected unapproved historical research")
    models = [LinearModel.from_dict(m) for m in data["models"]]
    chosen: list[dict[str, Any]] = []
    reserves: list[dict[str, Any]] = []
    for role, algorithm in zip(ROLES, ("ridge", "forest", "logistic"), strict=True):
        available = [m for m in models if m.algorithm == algorithm and m.timeframe == "H1"]
        # Lower original gate supplies a forecast score; candle rules define the new strategy.
        available.sort(key=lambda item: (item.threshold, item.model_id))
        m = available[0]
        alternate = next(
            (
                item
                for item in available
                if item.weights != m.weights
                or getattr(item, "trees", None) != getattr(m, "trees", None)
            ),
            None,
        )
        if alternate is not None:
            reserves.append(
                {
                    "role": role,
                    "source_model_id": alternate.model_id,
                    "model": replace(
                        alternate, model_id="LAB-" + role + "-" + alternate.model_id
                    ).to_dict(),
                }
            )
        chosen.append(
            {
                "role": role,
                "source_model_id": m.model_id,
                "model": replace(m, model_id="LAB-" + role + "-" + m.model_id).to_dict(),
            }
        )
    cohort = {
        "schema": SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "statistically_approved": False,
        "historically_eligible": False,
        "source": str(source.resolve()),
        "active_ids": [a["model"]["model_id"] for a in chosen],
        "agents": chosen + reserves,
        "entry_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "description": "Unapproved paper hypotheses; analyst required.",
    }
    destination.write_text(json.dumps(cohort, indent=2), encoding="utf-8")
    return cohort


class FundedPaperExecutor(IbkrPaperExecutor):
    """Only EUR-funded SELL entries; normal BUY reduce/close and broker protections remain.

    The account rejected opening BUY EURUSD when USD cash was absent. Never borrow
    quote currency to manufacture a long entry; never invert a bullish candle signal.
    """

    cash_guard: Any = None

    async def place_bracket(self, req: OrderRequest) -> OrderStatus:
        if self.cash_guard is not None:
            await self.cash_guard()
        self.verify_paper()
        if req.pair not in ("EURUSD", "EURGBP", "EURJPY") or req.side is not Side.SELL:
            raise ExecutionError("funded experiment admits EUR SELL entries only", ambiguous=False)
        if len(self.client.managedAccounts()) != 1:
            raise ExecutionError("funding requires exactly one paper account", ambiguous=False)
        req = replace(req, units=min(req.units, 5000.0))
        balances = {}
        for value in getattr(self.client, "accountValues", lambda: [])():
            if value.tag == "$LEDGER-CashBalance":
                try:
                    number = float(value.value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    balances[value.currency] = number
        cash = balances.get("EUR", 0.0)
        if req.units * 1.01 > cash * 0.1:
            raise ExecutionError(
                "EUR cash absent or entry exceeds funded cash limit", ambiguous=False
            )
        quote = self._last_quotes.get(req.pair)
        if quote is None or abs((self.clock() - quote.ts).total_seconds()) > self.quote_max_age_s:
            raise ExecutionError(
                "fresh LIVE quote required for funded stop budget", ambiguous=False
            )
        if req.stop_loss is None:
            raise ExecutionError("funded entry needs stop", ambiguous=False)
        reserve = (
            req.units * max(req.stop_loss - quote.bid, 0.0)
            + 0.02 * req.units * quote.bid
            + 20 * quote.bid
        )
        if balances.get(pair(req.pair).quote, 0.0) < reserve:
            raise ExecutionError(
                "quote cash cannot fund stop plus gap/cost buffer", ambiguous=False
            )
        return await super().place_bracket(req)


class CandleLab(PaperPilot):
    def __init__(
        self, *args: Any, cohort: dict[str, Any], funded_eur_only: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.risk = BudgetRiskManager(
            self.risk.config, self.risk.state, self.risk.calendar, self.risk.environment
        )
        self.budget_settings = load_settings(self.store)
        self.budget_status: dict[str, Any] = {"state": "current", "message": "Configuração atual."}
        configure(self, self.budget_settings)
        self.funded_eur_only = funded_eur_only
        self.cohort = cohort
        self.roles = {a["model"]["model_id"]: a["role"] for a in cohort["agents"]}
        self.contexts: dict[str, Any] = {}
        self.entry_until = datetime.fromisoformat(cohort["entry_until"])
        self.scan_attempts: dict[str, tuple[str, datetime, int]] = {}
        self.analysis_cache: dict[str, tuple[str, pd.DataFrame, dict[str, Any], float]] = {}

    async def _entry(self, now: datetime, quotes: dict[str, Any]) -> None:
        if self.budget_status.get("state") == "waiting":
            return  # finish existing trades before changing the budget; exits remain enabled
        await super()._entry(now, quotes)

    async def _signals(self, now: datetime) -> list[dict[str, Any]]:
        if now >= self.entry_until:
            return []
        proposals: list[dict[str, Any]] = []
        last_close = pd.Timestamp(now).floor("h")
        if now - last_close.to_pydatetime() > SIGNAL_MAX_DELAY:
            return []
        bar_open = last_close - pd.Timedelta(hours=1)
        for sym in self.config.symbols:
            if all(
                self.state.last_signal_bar.get(f"{mid}|{sym}") == bar_open.isoformat()
                for mid in self.state.active_ids
            ):
                continue
            cached = self.analysis_cache.get(sym)
            if cached is not None and cached[0] == bar_open.isoformat():
                _, mid, context, spread = cached
            else:
                attempt = self.scan_attempts.get(sym)
                if attempt and attempt[0] == bar_open.isoformat():
                    if attempt[2] >= 3 or now - attempt[1] < timedelta(minutes=5):
                        continue
                    count = attempt[2] + 1
                else:
                    count = 1
                self.scan_attempts[sym] = (bar_open.isoformat(), now, count)
                bars = closed_model_bars(await self.fetch_bars(sym, "H1"), sym, "H1", now)
                if len(bars) < 260 or bars.index[-1] != bar_open:
                    continue
                mid = mid_of(bars.iloc[-260:])
                context = market_analysis(mid)
                spread = (bars.iloc[-1]["ask_close"] - bars.iloc[-1]["bid_close"]) / pair(
                    sym
                ).pip_size
                self.analysis_cache[sym] = (bar_open.isoformat(), mid, context, spread)
                self.contexts[sym] = context
                self.journal("market_analysis", symbol=sym, context=context)
            if spread > MAX_BAR_SPREAD_PIPS:
                continue
            local: list[dict[str, Any]] = []
            for model_id in self.state.active_ids:
                if pd.Timestamp(now) < pd.Timestamp(self.models[model_id].training_end):
                    continue
                self.state.last_signal_bar[f"{model_id}|{sym}"] = bar_open.isoformat()
                result = specialist(self.roles[model_id], context, self.models[model_id], mid)
                if result is None:
                    continue
                row, pattern = result
                self.journal(
                    "specialist_proposal",
                    symbol=sym,
                    model_id=model_id,
                    pattern=pattern,
                    analyst_bar=context["bar_close"],
                    direction=int(row["dir"]),
                    score=float(row["score"]),
                )
                local.append(
                    {
                        "symbol": sym,
                        "model": self.models[model_id],
                        "row": row,
                        "bar_open": bar_open,
                    }
                )
            if len({int(p["row"]["dir"]) for p in local}) > 1:
                self.journal("conflict", symbol=sym, reason="especialistas discordam")
                continue
            for proposal in local:
                if self.funded_eur_only and int(proposal["row"]["dir"]) != -1:
                    self.journal(
                        "funded_direction_veto", symbol=sym, model_id=proposal["model"].model_id
                    )
                else:
                    proposals.append(proposal)
        # Fair observation; risk manager still permits at most five TOTAL daily entries.
        return sorted(
            proposals,
            key=lambda p: (
                len(self.state.returns.get(p["model"].model_id, []))
                + sum(t["model_id"] == p["model"].model_id for t in self.state.open.values()),
                -float(p["row"]["score"]),
            ),
        )

    def _retirement(self, model_id: str) -> None:
        decision = retirement_decision(
            self.registry,
            model_id,
            self.state.returns[model_id],
            self.state.retired_ids,
            self.state.active_ids,
        )
        if not decision["retire"] or model_id in self.state.retired_ids:
            return
        self.state.retired_ids.append(model_id)
        replacement = next(
            (
                mid
                for mid in self.models
                if self.roles[mid] == self.roles[model_id]
                and mid not in {*self.state.active_ids, *self.state.retired_ids}
            ),
            None,
        )
        if model_id in self.state.active_ids:
            self.state.active_ids.remove(model_id)
            if replacement is not None:
                self.state.active_ids.append(replacement)
        self.journal(
            "research_retirement",
            model_id=model_id,
            replacement=replacement,
            reason=decision["reason"],
            historically_approved=False,
        )

    def status(self) -> dict[str, Any]:
        result = super().status()
        result.update(
            budget_settings=self.budget_settings,
            budget_status=self.budget_status,
            funded_eur_only=self.funded_eur_only,
            experiment=SCHEMA,
            analyst="analista-mercado-v1",
            journal_intact=self.store.verify()[0],
            execution_counts={
                kind: count
                for kind, count in self.store.db.execute(
                    "SELECT kind,COUNT(*) FROM journal WHERE kind IN "
                    "('pilot_order_sent','pilot_order_filled','pilot_close') GROUP BY kind"
                )
            },
            market_context=self.contexts,
            observed_at=self.clock().isoformat(),
            entry_until=self.entry_until.isoformat(),
            agents=[
                {
                    "id": mid,
                    "role": self.roles[mid],
                    "active": mid in self.state.active_ids,
                    "retired": mid in self.state.retired_ids,
                    "closed_trades": len(v),
                    "total_r_estimate": sum(v),
                    "mean_r_estimate": sum(v) / len(v) if v else None,
                }
                for mid in self.roles
                for v in [self.state.returns.get(mid, [])]
            ],
        )
        return result


def positions_without_funding(fetcher: Any, baseline: dict[str, float]) -> Any:
    async def fetch() -> dict[str, float]:
        raw = await fetcher()
        if not set(baseline) <= set(raw):
            raise ExecutionError(
                "funding positions disappeared from broker position feed", ambiguous=True
            )
        return {symbol: units - baseline.get(symbol, 0.0) for symbol, units in raw.items()}

    return fetch


async def run(args: argparse.Namespace) -> int:
    import msvcrt

    from ib_async import IB

    root = args.dir.resolve()
    if args.cash_ledger and not args.funded_eur_only:
        raise ValueError("cash ledger requires funded EUR-only paper mode")
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "runner.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    if args.port in LIVE_PORTS or args.consent != CONSENT:
        raise ValueError("explicit demo experiment consent and paper port required")
    preflight_path = Path("data/fx/candle-lab-api-preflight.json")
    preflight = (
        json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.exists() else {}
    )
    if args.arm:
        required = (
            {"EURUSD_SELL", "EURGBP_SELL", "EURJPY_SELL"}
            if args.funded_eur_only
            else {"BUY", "SELL"}
        )
        previews = preflight.get("previews", {})
        failures = [e for key in required for e in previews.get(key, {}).get("errors", [])]
        stamp = datetime.fromisoformat(preflight.get("observed_at", "2000-01-01T00:00:00+00:00"))
        if (
            failures
            or not required <= set(previews)
            or datetime.now(UTC) - stamp > timedelta(hours=6)
        ):
            raise ValueError("paper execution blocked: broker preview rejected or stale")
    cohort_path = root / "cohort.json"
    cohort = (
        json.loads(cohort_path.read_text(encoding="utf-8"))
        if cohort_path.exists()
        else create_cohort(args.source, cohort_path)
    )
    if (
        cohort["schema"] != SCHEMA
        or cohort["statistically_approved"] is not False
        or len(cohort["active_ids"]) != 3
    ):
        raise ValueError("invalid paper experiment cohort")
    # No alteration of the historical eligibility field or the standard loader.
    registry = {
        "statistically_approved": False,
        "models": [a["model"] for a in cohort["agents"]],
        "active_model_ids": cohort["active_ids"],
        "ranking": [],
        "schema": "fx-training-run/1",
    }

    def say(message: str) -> None:
        print(message, flush=True)

    ib = IB()
    store = FxStore(root / "pilot.sqlite")
    pilot = None
    try:
        await ib.connectAsync(
            "127.0.0.1", args.port, clientId=args.client_id, timeout=15, readonly=not args.arm
        )
        ex = (FundedPaperExecutor if args.funded_eur_only else IbkrPaperExecutor)(
            ib, currency="EUR"
        )
        say("EXPERIMENTO PAPER, não aprovado; conta " + mask(ex.verify_paper()))
        config = PilotConfig(
            symbols=("EURUSD", "EURGBP", "EURJPY")
            if args.funded_eur_only
            else PilotConfig().symbols,
            risk=RiskConfig(
                max_leverage=3.0,
                risk_per_trade_pct=0.1,
                daily_loss_limit_pct=1.0,
                max_drawdown_pct=5.0,
                max_open_positions=3,
            ),
        )
        pilot = CandleLab(
            ex,
            store,
            registry,
            ib_bars_fetcher(ib),
            ib_positions_fetcher(ib),
            cohort=cohort,
            funded_eur_only=args.funded_eur_only,
            config=config,
            armed=args.arm and not args.funded_eur_only,
            stop_file=root / "STOP",
            say=say,
        )
        pilot.journal(
            "research_start",
            experiment=SCHEMA,
            agents=pilot.state.active_ids,
            analyst="analista-mercado-v1",
            historical_approval=False,
        )
        (root / "status.json").write_text(
            json.dumps(
                {**pilot.status(), "state": "initializing_analyst", "pid": os.getpid()}, default=str
            ),
            encoding="utf-8",
        )
        for symbol in pilot.config.symbols:
            bars = closed_model_bars(
                await pilot.fetch_bars(symbol, "H1"), symbol, "H1", pilot.clock()
            )
            if len(bars) >= 260:
                pilot.contexts[symbol] = market_analysis(mid_of(bars.iloc[-260:]))
                pilot.journal(
                    "analyst_bootstrap",
                    symbol=symbol,
                    context=pilot.contexts[symbol],
                    note="last closed historical bar; not an entry",
                )
        funding: dict[str, Any] = {"state": "not_requested"}
        while True:
            if (root / "STOP").exists():
                raise PilotStopError("STOP requested; paper lab disarmed")
            if args.arm and args.funded_eur_only and pilot.clock() < pilot.entry_until:
                if funding["state"] != "complete":
                    from app.fx.ibkr.funded_setup import ensure_funding

                    funding = await ensure_funding(ib, root)
                    if funding["state"] == "complete":
                        baseline = {k: float(v) for k, v in funding["baseline_positions"].items()}
                        if set(baseline) != set(pilot.config.symbols) or any(
                            v != -1000 for v in baseline.values()
                        ):
                            raise ValueError("invalid funding baseline; no strategy orders allowed")
                        if args.cash_ledger:
                            from app.fx.ibkr.cash_reconciliation import CashReconciler

                            reconciler = CashReconciler(
                                ib, store, root / "cash-anchor.json", root / "funding.json"
                            )
                            await reconciler()
                            pilot.fetch_positions = reconciler
                            if not isinstance(ex, FundedPaperExecutor):
                                raise ValueError("cash ledger requires funded executor")
                            ex.cash_guard = reconciler
                        else:
                            pilot.fetch_positions = positions_without_funding(
                                ib_positions_fetcher(ib), baseline
                            )
                pilot.armed = funding["state"] == "complete" and not pilot.risk.state.kill_switch
            if not ib.isConnected():
                raise ConnectionError(
                    "Gateway desconectado; supervisor deve rever antes de retomar"
                )
            request_path = root / "budget-request.json"
            if request_path.exists():
                try:
                    if request_path.stat().st_size > 4096:
                        raise ValueError("Pedido de configuração inválido.")
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    rid = request.get("request_id")
                    if not isinstance(rid, str) or len(rid) != 32:
                        raise ValueError("Identificador de configuração inválido.")
                    if rid != pilot.budget_settings.get("request_id"):
                        pilot.budget_status = {
                            "state": "waiting",
                            "request_id": rid,
                            "message": "Aguardando conta reconciliada e nenhuma operação aberta.",
                        }
                        if not pilot.state.open and not pilot.state.pending:
                            ex.verify_paper()
                            positions = await pilot.fetch_positions()
                            orders = await ib.reqAllOpenOrdersAsync()
                            if not positions and not orders:
                                pilot.budget_settings = apply_settings(pilot, request, rid)
                                pilot.budget_status = {
                                    "state": "applied",
                                    "request_id": rid,
                                    "message": "Aplicado. Resultados e proteções preservados.",
                                }
                    else:
                        pilot.budget_status = {
                            "state": "applied",
                            "request_id": rid,
                            "message": "Aplicado. Resultados e proteções preservados.",
                        }
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    pilot.budget_status = {"state": "rejected", "message": str(exc)}
            await pilot.cycle()
            status = pilot.status()
            status.update(
                execution_mode="armed_paper" if pilot.armed else "observation_only_no_orders",
                broker_preflight=preflight,
                funding=funding,
                requested_armed=bool(args.arm),
                reconciliation_mode="cash_and_confirmed_executions"
                if args.cash_ledger
                else "virtual_fx_positions",
                pid=os.getpid(),
                connection="paper",
                state=(
                    funding["state"]
                    if args.arm and args.funded_eur_only and funding["state"] != "complete"
                    else "observing"
                    if pilot.calendar.is_open(pilot.clock())
                    else "waiting_market_open"
                ),
                next_open=pilot.calendar.next_open(pilot.clock()).isoformat(),
            )
            temporary = root / "status.tmp"
            temporary.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
            temporary.replace(root / "status.json")
            if args.once:
                return 0
            if (
                pilot.clock() >= pilot.entry_until
                and not pilot.state.open
                and not pilot.state.pending
            ):
                return 0
            await asyncio.sleep(60)
    except Exception as error:
        status = pilot.status() if pilot is not None else {}
        status.update(
            state="stopped",
            armed=False,
            error=str(error),
            pid=os.getpid(),
            observed_at=datetime.now(UTC).isoformat(),
        )
        (root / "status.json").write_text(
            json.dumps(status, indent=2, default=str), encoding="utf-8"
        )
        raise
    finally:
        if pilot is not None:
            pilot.save()
        store.close()
        ib.disconnect()
        lock.close()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path("data/fx/training/run-20261018-candles/selected.json")
    )
    parser.add_argument("--dir", type=Path, default=Path("data/fx/candle-lab"))
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=62)
    parser.add_argument("--consent", required=True)
    parser.add_argument("--arm", action="store_true")
    parser.add_argument("--funded-eur-only", action="store_true")
    parser.add_argument("--cash-ledger", action="store_true")
    parser.add_argument("--once", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
