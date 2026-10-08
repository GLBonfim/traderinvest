"""Phase 14.5 daily operations: a local, paper-only orchestration of existing subsystems.

    PRECHECK -> per completed XNYS session: INGEST -> VALIDATE -> PAPER -> ALERTS
             -> HEALTH -> COMPLETE

Orchestration only: no indicator, strategy, risk, accounting or alert-rule logic lives here.
Paper trading remains a local simulation; nothing here can send an order or move money.
"""
