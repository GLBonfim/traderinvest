"""Phase 15 broker sandbox integration — Alpaca PAPER account only, no real money.

    linked local paper account (RiskManager-approved decisions, Phase 12 ledger)
        -> SandboxExecutor (gates: kill switch, armed, TRADING_MODE=paper, credentials, limits)
        -> BrokerGateway (AlpacaPaperGateway: fixed https://paper-api.alpaca.markets)

The local PaperBroker remains the project's execution simulation; this package only mirrors
its risk-approved instructions into an external sandbox when explicitly armed. Default state:
unarmed, nothing linked, no credentials.
"""
