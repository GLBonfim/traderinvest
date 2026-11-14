"""M5 causality, native feed, validation gates, final-size costs and paper protection."""

import asyncio
import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from app.candles.lesson_m5 import allowed
from app.fx.ibkr.candle_m5_lab import CandleM5Lab
from app.fx.ibkr.m5_pipeline import BAR, assess, closed_native_m5, load_eligible
from app.fx.ibkr.paper_supervisor import restart_allowed
from app.fx.research.m5_validation import PROTOCOL, digest
from tests.unit.fx.ibkr_fakes import NOW
from tests.unit.fx.test_pilot import make, model, registry


def native(now=NOW, n=300):
    idx = pd.date_range(end=pd.Timestamp(now).floor("5min"), periods=n, freq="5min")
    c = 1.1 + 0.002 * np.sin(np.arange(n) / 25)
    return pd.DataFrame(
        {
            f"{side}_{k}": c + off + delta
            for side, off in [("bid", -0.00003), ("ask", 0.00003)]
            for k, delta in [("open", 0), ("close", 0), ("high", 0.0004), ("low", -0.0004)]
        },
        index=idx,
    )


def test_native_excludes_forming_and_future_bars():
    raw = native()
    a = closed_native_m5(raw, "EURUSD", NOW)
    raw.iloc[-1] *= 2
    pd.testing.assert_frame_equal(a, closed_native_m5(raw, "EURUSD", NOW))
    assert len(a) == 260 and a.index[-1] + BAR <= pd.Timestamp(NOW)


def test_m15_cannot_be_disguised_as_m5():
    raw = native(n=900).iloc[::3]
    assert closed_native_m5(raw, "EURUSD", NOW).empty


def test_hole_and_stale_m5_fail_closed():
    raw = native()
    assert closed_native_m5(raw.drop(raw.index[-10]), "EURUSD", NOW).empty
    assert closed_native_m5(raw.iloc[:-3], "EURUSD", NOW).empty


def test_bad_quote_and_timezone_rejected():
    raw = native()
    raw.iloc[-5, 0] = float("nan")
    with pytest.raises(ValueError):
        closed_native_m5(raw, "EURUSD", NOW)
    raw = native()
    raw.index = raw.index.tz_localize(None)
    with pytest.raises(ValueError):
        closed_native_m5(raw, "EURUSD", NOW)


def empty_evidence(path):
    proto = PROTOCOL.to_dict()
    sha = digest(proto)
    selection = {
        "protocol_sha256": sha,
        "selection": {r: None for r in ("reversao", "continuacao", "impulso")},
    }
    values = {
        "protocol.json": proto,
        "selection.json": selection,
        "candidates.json": [],
        "holdout.json": {"selection_sha256": digest(selection), "roles": {}},
        "selected.json": {
            "timeframe": "M5",
            "statistically_approved": False,
            "protocol_sha256": sha,
            "selection_sha256": digest(selection),
            "models": [],
            "active_model_ids": [],
        },
    }
    for name, data in values.items():
        (path / name).write_text(json.dumps(data))


def test_failed_candidates_cannot_become_active_by_editing_id(tmp_path):
    empty_evidence(tmp_path)
    assert load_eligible(tmp_path) == ({}, {})
    p = tmp_path / "selected.json"
    s = json.loads(p.read_text())
    s["active_model_ids"] = ["rejected"]
    p.write_text(json.dumps(s))
    with pytest.raises((ValueError, FileNotFoundError)):
        load_eligible(tmp_path)


def test_protocol_tamper_is_rejected(tmp_path):
    empty_evidence(tmp_path)
    p = tmp_path / "protocol.json"
    s = json.loads(p.read_text())
    s["max_entries_per_day"] = 50
    p.write_text(json.dumps(s))
    with pytest.raises(ValueError):
        load_eligible(tmp_path)


def test_three_agents_explain_no_trade_without_models():
    bars = closed_native_m5(native(), "EURUSD", NOW)
    c, p = assess(bars, "EURUSD", {}, {})
    assert p == [] and c["no_trade_reason"] == "no_candidate_passed_frozen_validation"
    assert c["timeframe"] == "M5" and "lesson_research" in c


@pytest.mark.parametrize(
    "key,value", [("kill_switch", True), ("lock", "daily_loss"), ("reservations", {"x": "EURUSD"})]
)
def test_recovery_never_clears_risk_or_reservations(key, value):
    risk = {"kill_switch": False, "lock": None, "reservations": {}}
    risk[key] = value
    assert not restart_allowed({"open": {}, "pending": {}}, risk, False)


def test_recovery_rejects_missing_state_positions_pending_and_stop():
    risk = {"kill_switch": False, "lock": None, "reservations": {}}
    assert not restart_allowed(None, risk, False)
    assert not restart_allowed({"open": {}, "pending": {}}, {}, False)
    assert not restart_allowed({"open": {"x": {}}, "pending": {}}, risk, False)
    assert not restart_allowed({"open": {}, "pending": {"x": {}}}, risk, False)
    assert not restart_allowed({"open": {}, "pending": {}}, risk, True)
    assert restart_allowed({"open": {}, "pending": {}}, risk, False)


def test_aula_mask_is_causal_on_full_frame():
    bars = native(n=400)
    mid = pd.DataFrame(
        {k: (bars["bid_" + k] + bars["ask_" + k]) / 2 for k in ("open", "high", "low", "close")},
        index=bars.index,
    )
    a = allowed(mid.iloc[:300])
    mid.iloc[300:] *= 2
    pd.testing.assert_series_equal(a, allowed(mid).iloc[:300])


def test_m5_final_size_fee_veto_and_eight_hour_exit(tmp_path):
    # Fake paper executor only; never authorizes a rejected production model.
    m = replace(model("m5-fixture", intercept=-1), timeframe="M5")
    p, ib, _, _clock = make(tmp_path, reg=registry(m), armed=True)

    class Probe(CandleM5Lab):
        async def _signals(self, now):
            return [
                {
                    "symbol": "EURUSD",
                    "model": m,
                    "row": pd.Series(
                        {
                            "dir": -1,
                            "stop_dist": 0.003,
                            "target_dist": 0.006,
                            "score": 0.9,
                            "max_hold": 96,
                        }
                    ),
                    "bar_open": pd.Timestamp(now).floor("5min") - BAR,
                }
            ]

    # Create the test fixture through the existing initialized paper pilot.
    p.risk.config = replace(p.risk.config, risk_per_trade_pct=0.1)
    p.__class__ = Probe
    p.m5_models = {m.model_id: m}
    p.budget_status = {}
    p.m5_seen = {}
    asyncio.run(p.cycle())
    assert len(p.state.open) == 1, list(p.store.db.execute("SELECT kind,payload FROM journal"))
    trade = next(iter(p.state.open.values()))
    expected = pd.Timestamp(NOW).floor("5min") + pd.Timedelta(hours=8)
    assert pd.Timestamp(trade["time_stop_at"]) == expected
    assert trade["units"] < 0 and abs(trade["units"]) <= 5000
    assert len(ib.placed) >= 3


def test_aula_requires_ordered_sweep_break_and_first_retest():
    idx = pd.date_range("2026-10-21", periods=52, freq="5min", tz="UTC")
    mid = pd.DataFrame({"open": 1.09, "high": 1.10, "low": 1.00, "close": 1.05}, index=idx)
    mid.iloc[45:48] = [1.06, 1.07, 1.04, 1.05]
    mid.iloc[48] = [1.09, 1.11, 1.06, 1.08]
    mid.iloc[49] = [1.08, 1.09, 1.02, 1.03]
    mid.iloc[50] = [1.075, 1.08, 1.04, 1.06]
    mid.iloc[51] = [1.075, 1.08, 1.04, 1.06]
    mask = allowed(mid)
    assert mask.iloc[50] and not mask.iloc[51]
    mid.iloc[49] = [1.08, 1.09, 1.05, 1.07]
    assert not allowed(mid).any()


def passing_evidence(path, envelope):
    empty_evidence(path)
    selected = json.loads((path / "selected.json").read_text())
    sha = selected["protocol_sha256"]
    candidate_id = "synthetic-reversao"
    portable = replace(model("synthetic-m5"), timeframe="M5").to_dict()
    selection = {"protocol_sha256": sha, "selection": {"reversao": candidate_id}}
    selection_sha = digest(selection)
    selected.update(
        selection_sha256=selection_sha,
        models=[portable],
        active_model_ids=[portable["model_id"]],
    )
    holdout = {
        "selection_sha256": selection_sha,
        "roles": {
            "reversao": {
                "candidate": candidate_id,
                "model_id": portable["model_id"],
                "model_sha256": digest(portable),
                "eligible_demo": True,
            }
        },
    }
    candidates = [{"candidate": candidate_id, "passed": True, "checks": {"frozen_gate": True}}]
    payload = {"protocol_sha256": sha, "candidates": candidates} if envelope else candidates
    values = {
        "selection.json": selection,
        "selected.json": selected,
        "holdout.json": holdout,
        "candidates.json": payload,
        "joint-paper-gate.json": {"registry_sha256": digest(selected), "passed": True},
    }
    for name, value in values.items():
        (path / name).write_text(json.dumps(value))
    return portable["model_id"]


@pytest.mark.parametrize("envelope", [False, True])
def test_eligible_loader_accepts_frozen_candidate_list_or_envelope(tmp_path, envelope):
    mid = passing_evidence(tmp_path, envelope)
    models, roles = load_eligible(tmp_path)
    assert list(models) == [mid] and models[mid].timeframe == "M5"
    assert roles == {mid: "reversao"}


def test_candidate_envelope_protocol_must_match_frozen_registry(tmp_path):
    passing_evidence(tmp_path, True)
    path = tmp_path / "candidates.json"
    payload = json.loads(path.read_text())
    payload["protocol_sha256"] = "wrong-protocol"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="candidate protocol"):
        load_eligible(tmp_path)


@pytest.mark.parametrize("envelope", [False, True])
def test_failed_development_gate_still_blocks_candidate_container(tmp_path, envelope):
    passing_evidence(tmp_path, envelope)
    path = tmp_path / "candidates.json"
    payload = json.loads(path.read_text())
    candidates = payload["candidates"] if envelope else payload
    candidates[0]["checks"]["frozen_gate"] = False
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="development validation"):
        load_eligible(tmp_path)
