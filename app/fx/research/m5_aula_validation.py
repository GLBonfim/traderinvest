"""A NEW frozen Aula02 M5 pipeline, preserving the earlier rejected six candidates."""

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.candles.lesson_m5 import VERSION
from app.fx.research.m5_validation import M5Protocol, run


@dataclass(frozen=True)
class AulaM5Protocol(M5Protocol):
    lesson_filter_version: str = VERSION

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out["new_strategy_note"] = (
            "New CRT/MSS/OTE filter. Earlier six M5 failures preserved. No profitability claim."
        )
        out["filter_conventions"] = {
            "h4": "previous complete UTC H4 bearish",
            "sweep": "upper sweep returns inside",
            "mss": "later close below pre-sweep 3 M5 lows",
            "ote": [0.62, 0.79],
            "expiry_bars": 12,
            "first_retest_only": True,
            "future_bars": False,
        }
        return out


PROTOCOL_AULA = AulaM5Protocol()
if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    p.add_argument("--out", type=Path, default=Path("data/fx/training/run-m5-aula02-20261024"))
    a = p.parse_args()
    if (a.out / "protocol.json").exists():
        raise SystemExit("Frozen run already exists: review, do not rerun")
    run(a.data, a.out, protocol=PROTOCOL_AULA)
