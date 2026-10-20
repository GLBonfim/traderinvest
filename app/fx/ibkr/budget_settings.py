"""Audited, bounded settings for the existing PAPER experiment; no broker orders."""

import hashlib
import json
import math
from dataclasses import replace
from typing import Any

from app.fx.risk import Check, RiskDecision, RiskManager

KEY = "paper_budget_settings"
DEFAULT = {"capital_eur": 10000.0, "order_eur": 5000.0, "fixed_order": False}


def validate(value: dict[str, Any]) -> dict[str, Any]:
    capital, order = float(value["capital_eur"]), float(value["order_eur"])
    if not math.isfinite(capital) or not 100 <= capital <= 10000:
        raise ValueError("Banca deve estar entre EUR 100 e EUR 10.000.")
    if not math.isfinite(order) or not 1000 <= order <= 5000 or order % 1000:
        raise ValueError("Ordem deve ser de EUR 1.000 a EUR 5.000, em passos de EUR 1.000.")
    if not isinstance(value.get("fixed_order"), bool):
        raise ValueError("Modo de valor da ordem inválido.")
    return {"capital_eur": capital, "order_eur": order, "fixed_order": value["fixed_order"]}


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_settings(store: Any) -> dict[str, Any]:
    saved = store.get_state(KEY)
    if saved is None:
        return dict(DEFAULT)
    validate(saved)
    last = store.recent(1, ("paper_budget_applied",))
    if not last or last[0]["payload"].get("sha256") != digest(saved):
        raise ValueError("Configuração da banca difere do diário; rever antes de operar.")
    return saved


class BudgetRiskManager(RiskManager):
    desired_units: float | None = None

    def evaluate(self, *args: Any, **kwargs: Any) -> RiskDecision:
        decision = super().evaluate(*args, **kwargs)
        desired = self.desired_units
        if not decision.approved or not decision.is_entry or desired is None:
            return decision
        if desired > decision.units:
            return replace(
                decision,
                approved=False,
                units=0.0,
                checks=(
                    *decision.checks,
                    Check(
                        "chosen_order_budget",
                        False,
                        "Valor escolhido excede o tamanho autorizado pelo risco; entrada recusada.",
                    ),
                ),
                summary="Valor da ordem incompatível com banca/stop e limites de risco.",
            )
        return replace(decision, units=desired)


def configure(pilot: Any, value: dict[str, Any]) -> None:
    pilot.config = replace(pilot.config, capital=value["capital_eur"])
    pilot.risk.desired_units = value["order_eur"] if value["fixed_order"] else None


def apply_settings(pilot: Any, value: dict[str, Any], request_id: str) -> dict[str, Any]:
    value = validate(value)
    if pilot.state.open or pilot.state.pending or pilot.risk.state.reservations:
        raise ValueError("Aguardando todas as operações e pendências encerrarem.")
    if pilot.risk.state.kill_switch or pilot.risk.state.lock:
        raise ValueError("Bloqueio de risco ativo; configuração não remove bloqueios.")
    old = load_settings(pilot.store)
    if old.get("request_id") == request_id:
        if validate(old) != value:
            raise ValueError("Identificador já aplicado com outros valores.")
        return old
    delta = value["capital_eur"] - old["capital_eur"]
    if value["capital_eur"] + pilot.state.realized <= 0:
        raise ValueError("Banca insuficiente para os resultados acumulados.")
    risk = pilot.risk.state
    # A budget deposit/withdrawal is not trading P&L. Preserve euro losses, daily
    # usage, historical returns and breakers; never reset a high-water mark.
    for field in ("day_start_equity", "last_equity", "peak_equity"):
        previous = getattr(risk, field)
        if previous is not None and previous + delta <= 0:
            raise ValueError("Banca insuficiente para preservar as referências de risco.")
    updated = {**value, "request_id": request_id}
    with pilot.store.transaction():
        for field in ("day_start_equity", "last_equity", "peak_equity"):
            previous = getattr(risk, field)
            if previous is not None:
                setattr(risk, field, previous + delta)
        configure(pilot, value)
        pilot.store.append(
            "paper_budget_applied",
            {
                "before": old,
                "after": updated,
                "capital_delta": delta,
                "sha256": digest(updated),
                "orders_sent": 0,
                "note": "Capital reference translation only; returns and risk locks retained.",
            },
        )
        pilot.store.put_state(KEY, updated)
        pilot.save()
    return updated
