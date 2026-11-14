"""Audit Aula02 facts on genuine 2023 development BID M5. No broker or training."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from app.candles.htf_ltf_lesson import SOURCE_SHA256, VERSION, curriculum, observe


def run(root: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("upsampled") is not False or manifest.get("bar_size") != "5 mins":
        raise ValueError("genuine M5 manifest required")
    report: dict[str, Any] = {
        "version": VERSION,
        "source_sha256": SOURCE_SHA256,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "development_year": 2023,
        "holdout_evaluated": False,
        "sampling": "every 48th observed M5 bar after initial 96; descriptive only",
        "orders_sent": 0,
        "weights_trained": False,
        "execution_enabled": False,
        "statistically_approved": False,
        "full_strategy_validated": False,
        "symbols": {},
    }
    for role in ("analista", "reversao", "continuacao", "impulso"):
        (output / f"curriculo-{role}.json").write_text(
            json.dumps(curriculum(role), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    observations = []
    for symbol in ("EURUSD", "EURGBP", "EURJPY"):
        frames, provenance = [], []
        for c in manifest["chunks"]:
            if c["pair"] != symbol or c["side"] != "BID":
                continue
            if pd.Timestamp(c["first"]).year != 2023 or pd.Timestamp(c["last"]).year != 2023:
                continue
            path = root / symbol / Path(c["file"]).name
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != c["sha256"]:
                raise ValueError("historical hash mismatch")
            frames.append(pd.read_parquet(path))
            provenance.append({"file": path.name, "sha256": digest})
        if not frames:
            raise ValueError("missing development data")
        raw = pd.concat(frames, ignore_index=True)
        raw["ts"] = pd.to_datetime(raw.ts, utc=True)
        bars = raw.sort_values("ts").drop_duplicates("ts").set_index("ts")
        bars = bars.loc[pd.DatetimeIndex(bars.index).year == 2023]
        counts: Counter[str] = Counter()
        missing: Counter[str] = Counter()
        samples = 0
        for end in range(96, len(bars) + 1, 48):
            window = bars.iloc[max(0, end - 144) : end]
            out = observe(window, as_of=window.index[-1] + pd.Timedelta(minutes=5))
            counts.update(f["kind"] for f in out["facts"])
            missing.update(out["missing"])
            observations.append({"symbol": symbol, **out})
            samples += 1
        report["symbols"][symbol] = {
            "bars": len(bars),
            "samples": samples,
            "facts": dict(counts),
            "missing": dict(missing),
            "input_files": provenance,
        }
    (output / "observacoes.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in observations), encoding="utf-8"
    )
    (output / "auditoria.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.data_root, args.output)
    print(
        json.dumps(
            {
                k: {key: value for key, value in v.items() if key != "input_files"}
                for k, v in result["symbols"].items()
            }
        )
    )
