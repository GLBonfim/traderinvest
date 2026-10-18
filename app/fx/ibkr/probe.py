"""`python -m app.fx.ibkr.probe` — read-only diagnosis of the IBKR PAPER connection.

Prerequisite (done by the owner, never by this tool): IB Gateway or TWS running on this
machine, logged in with the Live/Paper switch on PAPER, API socket enabled, trusted IP
127.0.0.1 and "Read-Only API" ticked.

The probe connects with `readonly=True`, refuses non-paper sessions, then prints: masked account
id, account values, forex positions, bid/ask for the configured pairs and the last M15 bars of
one pair. It sends no order and writes nothing. Exit codes: 0 ok, 2 cannot connect, 3 refused
(not a paper session), 4 connected but no usable data.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable
from typing import Any

from app.fx.config import load_config
from app.fx.ibkr.adapter import IbkrClient, IbkrReadOnlyAdapter
from app.fx.ibkr.paper import NotPaperAccountError
from app.fx.ibkr.settings import IbkrSettings
from app.fx.models import pair

OK, CANNOT_CONNECT, NOT_PAPER, NO_DATA = 0, 2, 3, 4


def default_client() -> IbkrClient:
    from ib_async import IB

    client: IbkrClient = IB()
    return client


def price(sym: str, v: float) -> str:
    return f"{v:.3f}" if pair(sym).pip_size >= 0.01 else f"{v:.5f}"


async def run(
    settings: IbkrSettings,
    pairs: list[str],
    bars_pair: str,
    client_factory: Callable[[], IbkrClient] = default_client,
    out: Any = sys.stdout,
) -> int:
    def say(*a: object) -> None:
        print(*a, file=out)

    adapter = IbkrReadOnlyAdapter(settings, client_factory())
    say(f"IBKR (só leitura) -> {settings.host}:{settings.port} client_id={settings.client_id}")
    try:
        await adapter.connect()
    except NotPaperAccountError as exc:
        say(f"RECUSADO: {exc}")
        return NOT_PAPER
    except (OSError, TimeoutError, ConnectionError) as exc:
        say(f"SEM LIGAÇÃO: {type(exc).__name__}: {exc}")
        say("Confirme que o IB Gateway está aberto em modo Paper e a porta da API.")
        return CANNOT_CONNECT
    try:
        say(f"Conta(s) paper: {', '.join(adapter.masked_accounts)}")
        acct = await adapter.account()
        say(
            f"Conta: capital próprio {acct.equity:,.2f} {acct.currency} · saldo "
            f"{acct.balance:,.2f} · não realizado {acct.unrealized:,.2f} · margem usada "
            f"{acct.margin_used:,.2f} · margem livre {acct.free_margin:,.2f}"
        )
        pos = adapter.positions()
        say(f"Posições Forex: {len(pos)}")
        for p in pos:
            say(f"  {p.pair} {p.units:+,.0f} @ {price(p.pair, p.avg_price)}")
        rep = await adapter.quotes(pairs)
        say(f"Cotações ({len(rep.quotes)}/{len(pairs)}):")
        for sym, q in rep.quotes.items():
            say(
                f"  {sym} bid {price(sym, q.bid)} ask {price(sym, q.ask)} spread "
                f"{pair(sym).pips(q.spread):.1f} pips · {q.origin.value} · "
                f"{q.ts:%Y-%m-%d %H:%M:%S}Z"
            )
        if rep.missing:
            say(f"  sem bid/ask: {', '.join(rep.missing)} (permissões ou dados de mercado?)")
        bars = await adapter.bars(bars_pair, "M15", 8)
        say(f"Últimas velas M15 de {bars_pair} (MIDPOINT): {len(bars)}")
        for b in bars[-4:]:
            say(f"  {b.ts:%Y-%m-%d %H:%M}Z O {b.open} H {b.high} L {b.low} C {b.close}")
        return OK if rep.quotes or bars else NO_DATA
    finally:
        adapter.disconnect()


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252: never crash on an accented character
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="traderinvest-ibkr-probe")
    ap.add_argument("--port", type=int, default=cfg.ibkr.port)
    ap.add_argument("--client-id", type=int, default=cfg.ibkr.client_id)
    ap.add_argument("--bars-pair", default="EURUSD")
    args = ap.parse_args(argv)
    settings = cfg.ibkr.model_copy(update={"port": args.port, "client_id": args.client_id})
    return asyncio.run(
        run(IbkrSettings.model_validate(settings.model_dump()), cfg.pairs, args.bars_pair)
    )


if __name__ == "__main__":
    raise SystemExit(main())
