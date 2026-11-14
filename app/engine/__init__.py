"""The trading engine process (ADR-0037): runs the orchestrator and serves a local, token-
authenticated JSON-lines protocol on 127.0.0.1. The desktop UI is only a client: it renders
snapshots and sends commands, and it never trades by itself."""
