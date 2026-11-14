"""Descriptive candle knowledge audit on 2023 development data. No training or orders."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.candles.knowledge import CATALOG, VERSION, recognize


def development_h1(
    root: Path, symbol: str, chunks: list[dict[str, Any]]
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """Use genuine BID M5 -> complete H1, with hashes and no interpolated bars."""
    frames, provenance = [], []
    selected = [
        c
        for c in chunks
        if c["pair"] == symbol
        and c["side"] == "BID"
        and pd.Timestamp(c["first"]).year == 2023
        and pd.Timestamp(c["last"]).year == 2023
    ]
    for chunk in selected:
        path = root / symbol / Path(chunk["file"]).name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != chunk["sha256"]:
            raise ValueError(f"historical file hash mismatch: {path.name}")
        frames.append(pd.read_parquet(path))
        provenance.append({"file": str(path), "sha256": digest})
    if not frames:
        raise ValueError(f"no 2023 BID development data for {symbol}")
    raw = pd.concat(frames, ignore_index=True)
    raw["ts"] = pd.to_datetime(raw.ts, utc=True)
    raw = raw.sort_values("ts").drop_duplicates("ts").set_index("ts")
    index = pd.DatetimeIndex(raw.index)
    raw = raw.loc[index.year == 2023]
    index = pd.DatetimeIndex(raw.index)
    if not ((index.minute % 5 == 0) & (index.second == 0)).all():
        raise ValueError("data is not M5 aligned")
    h1 = raw.resample("1h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    counts = raw.close.resample("1h").count()
    h1 = h1.loc[counts == 12].dropna()
    h1["volume"] = 0.0  # unknown centralized FX volume, never a confirmation signal
    h1["is_closed"] = True
    return h1, provenance


def run(root: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("bar_size") != "5 mins" or manifest.get("upsampled") is not False:
        raise ValueError("require genuine IBKR M5 data, not upsampled history")
    report: dict[str, Any] = {
        "schema": VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "recognition and explanations, not profitability or model approval",
        "development_year": 2023,
        "holdout_evaluated": False,
        "training_weights_changed": False,
        "orders_sent": 0,
        "statistically_approved": False,
        "patterns": len(CATALOG),
        "live_detectors": 20,
        "additional_research_detectors": 10,
        "symbols": {},
        "source_manifest_sha256": hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest(),
    }
    (output / "catalogo-padroes.json").write_text(
        json.dumps(
            {"schema": VERSION, "patterns": list(CATALOG.values())}, indent=2, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    for symbol in ("EURUSD", "EURGBP", "EURJPY"):
        bars, provenance = development_h1(root, symbol, manifest["chunks"])
        observations = recognize(bars, timeframe="H1", as_of=datetime(2024, 1, 1, tzinfo=UTC))
        serial = observations.copy()
        serial["explanation"] = serial.explanation.map(lambda x: json.dumps(x, ensure_ascii=False))
        path = output / f"observacoes-{symbol}-H1-2023.csv"
        serial.to_csv(path, index=False)
        counts = []
        for name in CATALOG:
            group = observations.loc[observations.pattern == name]
            counts.append(
                {
                    "pattern": name,
                    "shape_count": len(group),
                    "context_met_count": int(group.context_requirements_met.sum()),
                    "next_bar_states": {
                        str(k): int(v) for k, v in group.confirmation.value_counts().items()
                    },
                }
            )
        report["symbols"][symbol] = {
            "complete_h1_bars": len(bars),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "observations": len(observations),
            "counts": counts,
            "input_files": provenance,
            "observations_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "price_basis": "IBKR historical BID; no mid reconstruction or volume invention",
        }
    (output / "auditoria-reconhecimento.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("data/fx/ibkr-m5"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.data_root, args.output)
    print(
        json.dumps(
            {
                "patterns": report["patterns"],
                "orders_sent": 0,
                "bars": {s: x["complete_h1_bars"] for s, x in report["symbols"].items()},
            }
        )
    )


if __name__ == "__main__":
    main()
