"""Frozen offline balanced reevaluation after chronological opening-gap correction."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.fx.research import m5_balanced_training as balanced
from app.fx.research import m5_boosted_training as boosted
from app.fx.research import m5_daily_diagnostics as daily
from app.fx.research import m5_economic_training as economic
from app.fx.research import m5_lesson_training as lesson
from app.fx.research.m5_validation import write_json


def sources() -> dict[str, Path]:
    return {**balanced.frozen_sources(), "gapfix_wrapper.py": Path(__file__)}


def protocol(source: Path) -> dict[str, Any]:
    value = balanced.protocol(source)
    value.update(
        schema="m5-balanced-gapfix/1.0.0",
        correction=(
            "Known OPEN precedes unknown intrabar high/low. Favorable target gaps "
            "resolve before later intrabar stop touches. Adverse stop gaps and "
            "stop-first intrabar ties preserved. TRAIN labels and evaluator corrected "
            "together. Prior runs preserved; no change to acceptance or costs."
        ),
        frozen_source_sha256={
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources().items()
        },
        stopping_rule="One corrected-label/evaluator refit only; six fixed candidates.",
        qualification_priority="One role today if evidence permits; no gate or risk bypass.",
    )
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--source", type=Path, default=Path("C:/Users/guilo/Downloads/Aula 02 - semana vip.pdf")
    )
    args = parser.parse_args()
    lesson.run(
        args.data,
        args.out,
        args.source,
        fit_function=balanced.fit,
        score_function=boosted.score,
        protocol_function=protocol,
        feature_function=economic.augment,
        sample_weight_function=balanced.balanced_weights,
        source_files=sources(),
    )
    candidates = json.loads((args.out / "candidates.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {}
    for candidate in candidates:
        name = candidate["candidate"]
        result[name] = {}
        for scenario in ("base", "stress"):
            paths = sorted((args.out / "trades").glob(f"{name}-*-{scenario}.csv"))
            frames = [pd.read_csv(path) for path in paths if path.stat().st_size > 1]
            trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            result[name][scenario] = daily.daily_realized(trades)
    write_json(args.out / "daily-realized-diagnostics.json", result)
