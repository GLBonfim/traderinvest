"""M5 extension of the single paper lab. Rejected models stay NO TRADE."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.fx.accounting import Converter
from app.fx.agents.trained import LinearModel
from app.fx.ibkr.candle_lab import CandleLab
from app.fx.ibkr.m5_pipeline import (
    BAR,
    MAX_DELAY,
    assess,
    closed_native_m5,
    load_eligible,
    protocol_from_hash,
)
from app.fx.ibkr.pilot import PaperPilot
from app.fx.models import Quote, pair
from app.fx.research.m5_validation import fee_ratio


class CandleM5Lab(CandleLab):
    def __init__(self, *args: Any, m5_source: Path, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if self.state.open or self.state.pending or self.risk.state.reservations:
            raise ValueError("M5 transition requires reconciled flat state")
        self.m5_models, self.m5_roles = load_eligible(m5_source)
        registry = json.loads((m5_source / "selected.json").read_text())
        self.m5_protocol = protocol_from_hash(registry["protocol_sha256"])
        self.models.update(self.m5_models)
        self.m5_seen: dict[str, str] = {}
        self.m5_registry = json.loads((m5_source / "selected.json").read_text())
        if self.m5_models:
            if self.risk.state.kill_switch or self.risk.state.lock:
                raise ValueError("risk lock forbids M5 transition")
            chosen = [i for i in self.m5_models if i not in self.state.retired_ids]
            if self.state.active_ids != chosen:
                with self.store.transaction():
                    self.journal(
                        "timeframe_transition",
                        previous_ids=self.state.active_ids,
                        active_ids=chosen,
                        timeframe="M5",
                        risk_preserved=True,
                    )
                    self.state.active_ids = chosen
                    self.save()
        self.roles.update(self.m5_roles)

    def signal_duration(self, model: LinearModel) -> timedelta:
        if model.timeframe != "M5":
            raise ValueError("M5 model required")
        return timedelta(minutes=5)

    def signal_delay(self) -> timedelta:
        return timedelta(seconds=90)

    def entry_geometry_allowed(
        self, candidate: dict[str, Any], units: float, reference: float, quotes: dict[str, Quote]
    ) -> bool:
        symbol = candidate["symbol"]
        distance = float(candidate["row"]["stop_dist"])
        minimum_quote = 2 * Converter(quotes).rate("USD", pair(symbol).quote)
        commission = max(minimum_quote, 0.2e-4 * units * reference)
        return bool(
            units > 0
            and distance > 0
            and units <= 5000
            and fee_ratio(symbol, units, reference, distance) <= 0.45
            and 2 * commission / (units * distance) <= 0.45
        )

    async def _entry(self, now: datetime, quotes: dict[str, Quote]) -> None:
        if not self.m5_models:
            await self._signals(now)
            return
        await super()._entry(now, quotes)

    async def _signals(self, now: datetime) -> list[dict[str, Any]]:
        close = pd.Timestamp(now).floor("5min")
        opened = close - BAR
        if now >= self.entry_until or pd.Timestamp(now) - close > MAX_DELAY:
            return []
        proposals = []
        active = {i: m for i, m in self.m5_models.items() if i in self.state.active_ids}
        for symbol in self.config.symbols:
            processed = active and all(
                self.state.last_signal_bar.get(f"{mid}|{symbol}") == opened.isoformat()
                for mid in active
            )
            if processed or self.m5_seen.get(symbol) == opened.isoformat():
                continue
            bars = closed_native_m5(await self.fetch_bars(symbol, "M5"), symbol, now)
            if bars.empty:
                self.journal(
                    "m5_data_wait", symbol=symbol, reason="missing/future/stale/native bars"
                )
                continue
            context, local = assess(bars, symbol, active, self.m5_roles, self.m5_protocol)
            self.contexts[symbol] = context
            self.m5_seen[symbol] = opened.isoformat()
            for mid in active:
                self.state.last_signal_bar[f"{mid}|{symbol}"] = opened.isoformat()
            self.journal("m5_market_analysis", symbol=symbol, context=context)
            for role in ("reversao", "continuacao", "impulso"):
                self.journal(
                    "m5_specialist_decision",
                    symbol=symbol,
                    role=role,
                    eligible=any(self.m5_roles[i] == role for i in active),
                    proposals=sum(self.m5_roles[p["model"].model_id] == role for p in local),
                    decision="evaluate" if active else "NO TRADE: validation failed",
                )
            proposals.extend(local)
        return sorted(proposals, key=lambda p: -float(p["row"]["score"]))

    def _retirement(self, model_id: str) -> None:
        previous = self.registry
        try:
            self.registry = {
                **self.m5_registry,
                "ranking": [
                    r
                    for r in self.m5_registry["ranking"]
                    if r.get("role") == self.m5_roles[model_id]
                ],
            }
            PaperPilot._retirement(self, model_id)
        finally:
            self.registry = previous

    def status(self) -> dict[str, Any]:
        out = super().status()
        out.update(
            active_ids=[i for i in self.m5_models if i in self.state.active_ids],
            agents=[
                {
                    "id": next((i for i in self.m5_models if self.m5_roles[i] == r), None),
                    "role": r,
                    "active": any(self.m5_roles[i] == r for i in self.m5_models),
                    "status": "eligible"
                    if any(self.m5_roles[i] == r for i in self.m5_models)
                    else "NO TRADE: validation failed",
                }
                for r in ("reversao", "continuacao", "impulso")
            ],
            timeframe="M5",
            native_bar_minutes=5,
            m5_execution_eligible=bool(self.m5_models),
            m5_active_ids=[i for i in self.m5_models if i in self.state.active_ids],
            m5_roles={
                r: (
                    "eligible"
                    if any(self.m5_roles[i] == r for i in self.m5_models)
                    else "NO TRADE: validation failed"
                )
                for r in ("reversao", "continuacao", "impulso")
            },
        )
        return out
