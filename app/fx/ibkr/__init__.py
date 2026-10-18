"""Interactive Brokers (IBKR) connection — PHASE A: READ-ONLY, PAPER ACCOUNT ONLY.

The TraderInvest process never sees IBKR credentials: the owner logs into IB Gateway (or TWS)
on this machine in PAPER mode, and the adapter connects to it on 127.0.0.1 through the TWS API
(`ib_async`). In this phase the adapter reads quotes, historical bars, account values and
positions; it has NO order method at all, connects with `readonly=True`, and refuses (then
disconnects from) any session whose accounts are not paper accounts. The engine's enabled
environments are unchanged (SIMULATION only): nothing here is wired into order execution.
"""
