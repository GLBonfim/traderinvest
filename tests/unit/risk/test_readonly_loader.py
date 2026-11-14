"""Verified read-only database access for the risk research loader.

A fake psycopg-like DBAPI connection stands in for PostgreSQL (no database is contacted): it
records the transaction start command psycopg would send (`BEGIN READ ONLY` when the DBAPI
connection's `read_only` is True), answers `SHOW transaction_read_only` from that state and
serves synthetic rows to the real `load_backtest_bars` queries through a real SQLAlchemy
engine, Session and the real `before_cursor_execute` guard."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

import app.database.readonly as ro
from app.backtest.data import load_backtest_bars
from app.risk import cli, research
from tests.unit.risk.test_research_cli import SELECT, FakeSuite, make_bars

TS = [datetime(2024, 1, d, 14, 30, tzinfo=UTC) for d in (2, 3, 4)]
UNSET = object()


class FakeDB:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.connections = 0
        self.honor_read_only = True  # False: the server ignores READ ONLY (answers 'off')
        self.show_value: Any = UNSET  # override the SHOW answer
        self.show_raises = False
        self.setter_ignored = False  # the DBAPI ignores `read_only = True`


class FakeConn:
    autocommit = False

    def __init__(self, db: FakeDB) -> None:
        self.db = db
        self._ro: bool | None = None
        self.in_tx = False
        self.tx_read_only = False
        self.closed = False
        self.broken = False
        self.info = SimpleNamespace(transaction_status=0)
        db.connections += 1

    @property
    def read_only(self) -> bool | None:
        return self._ro

    @read_only.setter
    def read_only(self, value: bool | None) -> None:
        if self.in_tx:  # psycopg refuses to change it inside a transaction
            raise psycopg.ProgrammingError("can't change 'read_only' now")
        if not self.db.setter_ignored:
            self._ro = value

    def add_notice_handler(self, fn: Any) -> None:
        pass

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)

    def commit(self) -> None:
        self.db.log.append("COMMIT")
        self.in_tx = False

    def rollback(self) -> None:
        self.db.log.append("ROLLBACK")
        self.in_tx = False

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self.connection = conn
        self.description: Any = None
        self.rowcount = -1
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
        c, db = self.connection, self.connection.db
        if not c.in_tx:  # what psycopg sends before the first statement of a transaction
            mode = {True: " READ ONLY", False: " READ WRITE", None: ""}[c.read_only]
            db.log.append("BEGIN" + mode)
            c.in_tx = True
            c.tx_read_only = bool(c.read_only) and db.honor_read_only
        db.log.append(sql)
        s = " ".join(sql.split()).upper()
        cols: list[str]
        rows: list[tuple[Any, ...]]
        if s.startswith("SHOW TRANSACTION_READ_ONLY"):
            if db.show_raises:
                raise psycopg.OperationalError("connection lost")
            value = db.show_value if db.show_value is not UNSET else (
                "on" if c.tx_read_only else "off"
            )  # fmt: skip
            cols, rows = ["transaction_read_only"], ([] if value is None else [(value,)])
        elif "FROM INSTRUMENTS" in s:
            cols, rows = ["id"], [(7,)]
        elif "PRICE_BARS.ADJ_CLOSE" in s:
            cols, rows = ["ts", "adj_close"], [(t, Decimal("100.5")) for t in TS]
        elif "PRICE_BARS.OPEN" in s:
            cols = ["ts", "open", "high", "low", "close", "volume"]
            rows = [(t, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100.25"),
                     Decimal("1000")) for t in TS]  # fmt: skip
        elif s.startswith("SELECT 1"):
            cols, rows = ["one"], [(1,)]
        else:
            if c.tx_read_only:
                raise psycopg.errors.ReadOnlySqlTransaction("read-only transaction")
            cols, rows = [], []
        oid = {
            "id": 23,
            "one": 23,
            "ts": 1184,
            "transaction_read_only": 25,
        }  # int4, timestamptz, text
        self.description = [
            (n, oid.get(n, 1700), None, None, None, None, None)  # other columns: numeric
            for n in cols
        ] or None
        self._rows = list(rows)
        self.rowcount = len(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        n = len(self._rows) if size is None else size
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows

    def close(self) -> None:
        pass


def fake_engine(db: FakeDB, *, read_only_option: bool = True) -> Engine:
    opts = {ro.READ_ONLY_OPTION: True} if read_only_option else {}
    return create_engine(
        "postgresql+psycopg://research@localhost/quant",
        creator=lambda: FakeConn(db),
        poolclass=NullPool,
        execution_options=opts,
        _initialize=False,
    )


def statements(db: FakeDB) -> list[str]:
    return [s for s in db.log if s not in ("COMMIT", "ROLLBACK")]


def assert_every_statement_verified(db: FakeDB) -> None:
    """Each data statement directly follows a SHOW in a transaction that began READ ONLY."""
    tx_mode = None
    prev = None
    for s in statements(db):
        if s.startswith("BEGIN"):
            tx_mode, prev = s, None
            continue
        assert tx_mode == "BEGIN READ ONLY", (s, tx_mode)
        if not s.upper().startswith("SHOW TRANSACTION_READ_ONLY"):
            assert prev is not None and prev.upper().startswith("SHOW TRANSACTION_READ_ONLY"), s
        prev = s


# ── the read-only transaction starts before the first SELECT, every statement verified ──


def test_loader_reads_only_inside_verified_read_only_transactions() -> None:
    db = FakeDB()
    engine = fake_engine(db)
    with ro.read_only_session(engine) as (session, ev):
        instrument_id, bars = load_backtest_bars(session, "SPY")
    assert instrument_id == 7 and len(bars) == 3 and list(bars.columns)[-1] == "adj_close"
    log = statements(db)
    assert log[0] == "BEGIN READ ONLY"  # before any query, data or check
    data = [s for s in log if "price_bars" in s or "instruments" in s]
    assert len(data) == 3
    assert_every_statement_verified(db)
    assert ev.confirmed and ev.statements == ev.checks == 4  # initial SHOW + 3 SELECTs
    assert ev.to_dict()["transaction_read_only_values"] == ["on"]
    assert db.log[-1] == "ROLLBACK"  # the session never commits


@pytest.mark.parametrize("failure", ["off", "null", "raises"])
def test_unconfirmed_read_only_aborts_before_any_data(failure: str) -> None:
    db = FakeDB()
    if failure == "off":
        db.honor_read_only = False
    elif failure == "null":
        db.show_value = None
    else:
        db.show_raises = True
    with pytest.raises(ro.ReadOnlyViolationError), ro.read_only_session(fake_engine(db)) as (s, _):
        load_backtest_bars(s, "SPY")  # pragma: no cover - never reached
    assert not any("price_bars" in x or "instruments" in x for x in db.log)


def test_unexpected_value_is_refused() -> None:
    db = FakeDB()
    db.show_value = "true"
    ev = ro.ReadOnlyEvidence()
    with pytest.raises(ro.ReadOnlyViolationError), ro.read_only_session(fake_engine(db), ev):
        pass  # pragma: no cover
    assert not ev.confirmed and ev.violation and "'true'" in ev.violation


def test_engine_without_the_option_is_refused_before_connecting() -> None:
    db = FakeDB()
    with (
        pytest.raises(ro.ReadOnlyViolationError),
        ro.read_only_session(fake_engine(db, read_only_option=False)),
    ):
        pass  # pragma: no cover
    assert db.connections == 0 and db.log == []


def test_dbapi_that_ignores_read_only_is_refused_before_any_statement() -> None:
    db = FakeDB()
    db.setter_ignored = True
    with pytest.raises(ro.ReadOnlyViolationError), ro.read_only_session(fake_engine(db)):
        pass  # pragma: no cover
    assert db.log == [] or db.log == ["ROLLBACK"]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE price_bars SET close = 0",
        "INSERT INTO instruments (symbol) VALUES ('X')",
        "SET TRANSACTION READ WRITE",
        "WITH d AS (DELETE FROM price_bars RETURNING id) SELECT * FROM d",
        "COMMIT",
    ],
)
def test_non_read_statements_are_never_sent(sql: str) -> None:
    db = FakeDB()
    with (
        ro.read_only_session(fake_engine(db)) as (session, ev),
        pytest.raises(ro.ReadOnlyViolationError),
    ):
        session.execute(text(sql))
    assert sql not in db.log and not ev.confirmed


# ── protection survives commit, rollback, new sessions and extra connections ──


def test_new_transactions_after_commit_and_rollback_are_read_only_and_verified() -> None:
    db = FakeDB()
    with ro.read_only_session(fake_engine(db)) as (session, ev):
        load_backtest_bars(session, "SPY")
        session.commit()
        load_backtest_bars(session, "SPY")
        session.rollback()
        load_backtest_bars(session, "SPY")
    assert statements(db).count("BEGIN READ ONLY") == 3
    assert "BEGIN" not in statements(db) and "BEGIN READ WRITE" not in statements(db)
    assert_every_statement_verified(db)
    assert ev.confirmed and ev.statements == 10


def test_additional_connections_of_the_engine_are_protected() -> None:
    db = FakeDB()
    engine = fake_engine(db)
    # a loader opening its own connection while the read-only session is active
    with ro.read_only_session(engine) as (_, ev), engine.connect() as other:
        other.execute(text("SELECT 1"))
        with pytest.raises(ro.ReadOnlyViolationError):
            other.execute(text("DELETE FROM price_bars"))
    assert db.connections == 2
    assert_every_statement_verified(db)
    assert "DELETE FROM price_bars" not in db.log and not ev.confirmed


def test_guard_is_removed_after_the_block() -> None:
    engine = fake_engine(FakeDB())
    with ro.read_only_session(engine) as (_, ev):
        (guard,) = [g for g in engine.dispatch.before_cursor_execute]
    assert not event.contains(engine, "before_cursor_execute", guard)
    assert ev.confirmed


def test_evidence_is_not_confirmed_without_statements() -> None:
    assert not ro.ReadOnlyEvidence().confirmed
    ev = ro.ReadOnlyEvidence(statements=2, checks=2, values=["on", "off"])
    assert not ev.confirmed


def test_read_only_engine_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        db_connect_timeout_s=3,
        database_url=lambda: URL.create("postgresql+psycopg", "u", "pw", "h", 5432, "d"),
    )
    monkeypatch.setattr(ro, "get_settings", lambda: settings)
    engine = ro.read_only_engine()  # create_engine never connects
    try:
        assert isinstance(engine.pool, NullPool)
        assert engine.get_execution_options()[ro.READ_ONLY_OPTION] is True
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()


# ── the CLI path: the real DB loader, the manifest records only confirmed evidence ──


def manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((path / research.MANIFEST).read_text(encoding="utf-8"))
    return data


@pytest.fixture
def _fixed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    code = {"app_version": "0", "git_commit": "c" * 40, "git_branch": "b", "git_dirty": False}
    monkeypatch.setattr(research, "code_info", lambda: dict(code))


@pytest.mark.usefixtures("_fixed_code")
def test_cli_db_loader_records_confirmed_read_only_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = FakeDB()
    monkeypatch.setattr(ro, "read_only_engine", lambda: fake_engine(db))
    out, suite = tmp_path / "r", FakeSuite()
    code = cli.main(["run", "--symbol", "SPY", "--out", str(out), *SELECT],
                    run_suite=suite, configure=False)  # fmt: skip
    assert code == cli.EXIT_OK and len(suite.calls) == 1
    m = manifest(out)
    assert m["status"] == research.COMPLETE
    acc = m["db_access"]
    assert acc["read_only_confirmed"] is True and acc["mechanism"] == ro.MECHANISM
    assert acc["statements_checked"] == acc["checks"] == 4
    assert acc["transaction_read_only_values"] == ["on"] and acc["violation"] is None
    assert m["input"]["rows"] == 3
    assert_every_statement_verified(db)


@pytest.mark.usefixtures("_fixed_code")
def test_cli_aborts_and_records_failure_when_read_only_is_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = FakeDB()
    db.honor_read_only = False
    monkeypatch.setattr(ro, "read_only_engine", lambda: fake_engine(db))
    out, suite = tmp_path / "r", FakeSuite()
    code = cli.main(["run", "--symbol", "SPY", "--out", str(out), *SELECT],
                    run_suite=suite, configure=False)  # fmt: skip
    assert code == cli.EXIT_FAILED and suite.calls == []
    m = manifest(out)
    assert m["status"] == research.FAILED and m["input"] is None and m["artifacts"] == []
    acc = m["db_access"]
    assert acc["read_only_confirmed"] is False and "'off'" in acc["violation"]
    assert not any("price_bars" in x for x in db.log)
    assert not (out / research.TABLE).exists()


@pytest.mark.usefixtures("_fixed_code")
def test_unconfirmed_load_result_is_never_used(tmp_path: Path) -> None:
    out, suite = tmp_path / "r", FakeSuite()
    unconfirmed = research.LoadResult(make_bars(), {"read_only_confirmed": False})
    code = cli.main(["run", "--symbol", "SPY", "--out", str(out), *SELECT],
                    load_bars=lambda: unconfirmed, run_suite=suite, configure=False)  # fmt: skip
    assert code == cli.EXIT_FAILED and suite.calls == []
    m = manifest(out)
    assert m["status"] == research.FAILED and m["db_access"]["read_only_confirmed"] is False


@pytest.mark.usefixtures("_fixed_code")
def test_in_memory_loader_never_claims_read_only(tmp_path: Path) -> None:
    out = tmp_path / "r"
    code = cli.main(["run", "--symbol", "SPY", "--out", str(out), *SELECT],
                    load_bars=make_bars, run_suite=FakeSuite(), configure=False)  # fmt: skip
    assert code == cli.EXIT_OK
    assert manifest(out)["db_access"]["read_only_confirmed"] is False
