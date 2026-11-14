"""Free data acquisition: Yahoo Finance daily bars (via yfinance) and ECB statistical series.

Every file is written once under ``<root>/<snapshot>/`` and described by a `FileRecord` with its
SHA-256. Files are never rewritten: re-fetching into an existing snapshot is refused, so a
manifest can always be checked against the exact bytes it was built from.

Yahoo note: the stored CSV is yfinance's parsed output (one row per provider session, split-
adjusted Open/High/Low/Close, the provider's Adj Close, Volume, Dividends and Stock Splits),
not the raw HTTP payload. The yfinance version is recorded with each file.
"""

import hashlib
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.integrity import atomic_write
from app.trend.universe import Universe

ECB_BASE = "https://data-api.ecb.europa.eu/service/data"
YAHOO_COLUMNS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
    "Dividends": "dividends",
    "Stock Splits": "splits",
}


@dataclass(frozen=True)
class FileRecord:
    role: str  # e.g. "price:SPY", "fx:USD", "rate:estr"
    path: str  # relative to the data root, POSIX separators
    sha256: str
    bytes: int
    retrieved_at_utc: str
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _record(root: Path, path: Path, role: str, origin: str, when: str) -> FileRecord:
    return FileRecord(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=sha256_file(path),
        bytes=path.stat().st_size,
        retrieved_at_utc=when,
        origin=origin,
    )


def yahoo_frame_to_csv(frame: pd.DataFrame) -> str:
    """Normalise a yfinance ``history`` frame to the stored CSV text (pure, testable)."""
    if frame.empty:
        raise ValueError("empty price history")
    missing = [c for c in ("Close", "Adj Close", "Dividends") if c not in frame.columns]
    if missing:
        raise ValueError(f"price history lacks columns {missing}")
    out = pd.DataFrame(
        {new: frame[old] if old in frame.columns else 0.0 for old, new in YAHOO_COLUMNS.items()}
    )
    # The provider stamps each session at local midnight; the session date is that local date.
    index = pd.DatetimeIndex(frame.index)
    out.index = pd.Index([ts.date().isoformat() for ts in index], name="date")
    return str(out.to_csv(lineterminator="\n", float_format="%.10g"))


def fetch_yahoo(symbol: str, root: Path, snapshot: str) -> FileRecord:
    import yfinance as yf

    path = root / snapshot / "yahoo" / f"{symbol}.csv"
    if path.exists():
        raise FileExistsError(f"refusing to overwrite raw file {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    when = datetime.now(UTC).isoformat(timespec="seconds")
    frame = yf.Ticker(symbol).history(period="max", auto_adjust=False, actions=True)
    atomic_write(path, yahoo_frame_to_csv(frame))
    origin = (
        f"yfinance {yf.__version__}: Ticker({symbol!r}).history(period='max', "
        "auto_adjust=False, actions=True)"
    )
    return _record(root, path, f"price:{symbol}", origin, when)


def ecb_url(flow: str, key: str) -> str:
    return f"{ECB_BASE}/{flow}/{urllib.parse.quote(key)}?format=csvdata"


def fetch_ecb(flow: str, key: str, role: str, root: Path, snapshot: str) -> FileRecord:
    path = root / snapshot / "ecb" / f"{flow}_{key}.csv"
    if path.exists():
        raise FileExistsError(f"refusing to overwrite raw file {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    url = ecb_url(flow, key)
    when = datetime.now(UTC).isoformat(timespec="seconds")
    if not url.startswith(ECB_BASE + "/"):
        raise ValueError(f"refusing non-ECB URL {url}")
    headers = {"User-Agent": "quant-platform-research/1"}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https host
    with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310 - fixed https host
        body: bytes = resp.read()
    if not body.startswith(b"KEY,"):
        raise ValueError(f"unexpected ECB response for {flow}/{key}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
    return _record(root, path, role, url, when)


def needed_roles(universe: Universe) -> list[str]:
    """Every file role the universe needs, in a fixed order."""
    roles = [f"price:{a.research.symbol}" for a in universe.all_series()]
    return [*roles, "fx:USD", "rate:eonia", "rate:estr"]


def fetch_missing(
    universe: Universe, root: Path, snapshot: str, existing: list[FileRecord]
) -> list[FileRecord]:
    """Download only the price series the universe needs and the snapshot lacks.

    Existing files are never touched (`fetch_yahoo` refuses to overwrite).
    """
    have = {r.role for r in existing}
    return [
        fetch_yahoo(a.research.symbol, root, snapshot)
        for a in universe.all_series()
        if f"price:{a.research.symbol}" not in have
    ]


def fetch_all(universe: Universe, root: Path, snapshot: str) -> list[FileRecord]:
    """Download every series the universe needs into a new snapshot directory."""
    target = root / snapshot
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"snapshot {target} already holds files; choose a new snapshot id")
    records = [fetch_yahoo(a.research.symbol, root, snapshot) for a in universe.all_series()]
    fx = universe.raw["eur_conversion"]
    records.append(fetch_ecb(fx["flow"], fx["key"], "fx:USD", root, snapshot))
    cash = universe.raw["cash_rate"]
    for name in ("eonia", "estr"):
        records.append(
            fetch_ecb(cash[name]["flow"], cash[name]["key"], f"rate:{name}", root, snapshot)
        )
    return records
