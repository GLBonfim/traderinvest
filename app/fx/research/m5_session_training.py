"""New offline Aula02 session setups: causal sweep -> MSS -> confirmed expansion -> OTE."""

import argparse
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.research import m5_boosted_training as boosted
from app.fx.research import m5_lesson_training as curriculum
from app.fx.research.backtest import _ns

VERSION = "m5-aula-session-setups/3.0.0"
# Fixed UTC research conventions, NOT a claim that the PDF supplied exact DST rules.
SESSIONS = (("tokyo", 22, 24, 0, 3), ("london", 0, 8, 8, 11), ("new_york", 8, 13, 13, 17))


def session_patterns(mid: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(mid.index)
    if idx.tz is None or not idx.is_unique or not idx.is_monotonic_increasing:
        raise ValueError("unique ordered timezone-aware M5 required")
    idx = idx.tz_convert("UTC")
    if not ((idx.minute % 5 == 0) & (idx.second == 0)).all():
        raise ValueError("native M5 grid required")
    out = pd.DataFrame(
        0.0,
        index=mid.index,
        columns=[
            f"{family}_{side}"
            for family in ("reversal", "continuation", "momentum")
            for side in ("bull", "bear")
        ],
    )
    high, low, op, cl = (mid[k].to_numpy(float) for k in ("high", "low", "open", "close"))
    median_body = (mid.close - mid.open).abs().rolling(20).median().shift(1).to_numpy(float)
    emitted = np.zeros((len(mid), 3))
    ns = _ns(idx)
    for day in idx.normalize().unique():
        for _name, r0, r1, t0, t1 in SESSIONS:
            reference_day = day - pd.Timedelta(days=1) if t0 == 0 else day
            start = reference_day + pd.Timedelta(hours=r0)
            end = reference_day + pd.Timedelta(hours=r1)
            ref = np.flatnonzero((idx >= start) & (idx < end))
            expected = (r1 - r0) * 12
            if (
                len(ref) != expected
                or idx[ref[0]] != start
                or idx[ref[-1]] != end - pd.Timedelta(minutes=5)
            ):
                continue
            if expected > 1 and not (np.diff(ns[ref]) == pd.Timedelta(minutes=5).value).all():
                continue
            rh, rl = float(high[ref].max()), float(low[ref].min())
            active = np.flatnonzero(
                (idx >= day + pd.Timedelta(hours=t0)) & (idx < day + pd.Timedelta(hours=t1))
            )
            stage = 0
            origin = base = level = 0.0
            sweep = base_i = -1
            locked = False
            displaced = fvg = aligned = False
            previous = -1
            for i in active.tolist():
                if previous >= 0 and idx[i] - idx[previous] != pd.Timedelta(minutes=5):
                    stage = 0
                previous = i
                if stage and (high[i] >= origin or low[i] <= rl or i - sweep > 36):
                    stage = 0
                if stage == 2:
                    if low[i] < base:
                        if locked:
                            stage = 0
                        else:
                            base = float(low[i])
                            base_i = int(i)
                    if stage:
                        if i - base_i >= 2:
                            locked = True
                        fvg = bool(fvg or (i >= 2 and high[i] < low[i - 2]))
                        zl, zh = base + 0.62 * (origin - base), base + 0.79 * (origin - base)
                        if locked and high[i] >= zl:
                            if high[i] <= zh and cl[i] < zl and cl[i] < op[i] and cl[i] > rl:
                                strength = min(
                                    0.95,
                                    0.5 + 0.4 * abs(cl[i] - op[i]) / max(high[i] - low[i], 1e-12),
                                )
                                emitted[i, 0] = max(emitted[i, 0], strength)
                                if aligned:
                                    emitted[i, 1] = max(emitted[i, 1], strength)
                                if displaced and fvg:
                                    emitted[i, 2] = max(emitted[i, 2], strength)
                            stage = 0
                elif stage == 1:
                    base = min(base, float(low[i]))
                    if cl[i] < level and cl[i] < op[i]:
                        stage = 2
                        base_i = int(i)
                        locked = False
                        displaced = bool(
                            np.isfinite(median_body[i]) and op[i] - cl[i] >= 1.25 * median_body[i]
                        )
                        fvg = bool(i >= 2 and high[i] < low[i - 2])
                if stage == 0 and i >= 20 and high[i] > rh and rl < cl[i] < rh and low[i] > rl:
                    if idx[i] - idx[i - 3] != pd.Timedelta(minutes=15):
                        continue
                    stage = 1
                    origin = float(high[i])
                    base = float(low[i])
                    sweep = int(i)
                    level = float(low[i - 3 : i].min())
                    aligned = bool(cl[i - 1] < np.mean(cl[i - 20 : i]))
    for j, family in enumerate(("reversal", "continuation", "momentum")):
        out[family + "_bear"] = emitted[:, j]
    return out


def fitted_model(
    role: str, variant: float, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None
) -> tuple[Any, dict[str, Any]]:
    fit, artifact = boosted.fitted_model(role, variant, x, y, sample_weight=sample_weight)
    artifact.update(
        schema=VERSION,
        pattern_semantics="Aula02 session setup; not the original20 candle detectors",
    )
    return fit, artifact


def protocol(source: Path) -> dict[str, Any]:
    p = boosted.protocol(source)
    p.update(
        schema=VERSION,
        model_trainer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        boosted_source_sha256=hashlib.sha256(Path(boosted.__file__).read_bytes()).hexdigest(),
        hypothesis=(
            "Session reference ranges from PDF pages17/19/21 generate independent "
            "research patterns, rather than requiring an unrelated old candle to "
            "coincide with the complete setup. H4 bearish context, learned agreement, "
            "costs and all acceptance gates remain required."
        ),
        pattern_conventions={
            "sessions_utc": SESSIONS,
            "dst": "fixed UTC convention; PDF timing conflicts are not resolved as author rules",
            "reference": "exact complete prior session M5 range; no future bars",
            "sweep": "upper sweep with close back inside, not both extremes",
            "mss": "later bearish close below three lows preceding sweep",
            "expansion": (
                "origin fixed at sweep; low may extend until two later bars confirm it, "
                "then locked; a later new extreme invalidates"
            ),
            "ote": (
                "first post-confirmation touch62-79%, bearish close below62%, target-side "
                "liquidity still unvisited"
            ),
            "reversao": "complete sequence",
            "continuacao": "sequence with bearish pre-sweep20bar trend",
            "impulso": (
                "sequence plus MSS body>=1.25 times prior20 median body and confirmed "
                "bearish three-bar FVG"
            ),
            "expiry": "36M5 bars or end of the session; gap resets state",
            "exit_geometry": (
                "unchanged cost floor,96bars,target2R; research risk geometry, not a "
                "claim of PDF exact exits"
            ),
        },
        numeric_conventions_are_local=True,
        original_candle_detectors_changed=False,
    )
    return p


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--source", type=Path, default=Path("C:/Users/guilo/Downloads/Aula 02 - semana vip.pdf")
    )
    a = p.parse_args()
    curriculum.run(
        a.data,
        a.out,
        a.source,
        fit_function=fitted_model,
        score_function=boosted.score,
        protocol_function=protocol,
        pattern_function=session_patterns,
    )
    (a.out / "model_trainer.py").write_bytes(Path(__file__).read_bytes())
    (a.out / "boosted_trainer.py").write_bytes(Path(boosted.__file__).read_bytes())
