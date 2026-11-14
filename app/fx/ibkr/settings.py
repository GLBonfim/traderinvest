"""Settings of the IBKR read-only connection (part of `FxConfig`, no credentials).

- host: loopback only (the Gateway runs on this machine);
- port: the Gateway/TWS API port of the PAPER session (IB Gateway paper default 4002, TWS
  paper default 7497 — whatever the Gateway shows in its API settings);
- read_only and require_paper are fixed to True in this build (phase A).
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

LOOPBACK = ("127.0.0.1", "localhost", "::1")

# IBKR market data types: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
MarketDataType = Literal[1, 2, 3, 4]


class IbkrSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(4002, ge=1, le=65535)
    client_id: int = Field(17, ge=0, le=2_147_483_647)
    connect_timeout_s: float = Field(10.0, gt=0, le=120)
    market_data_type: MarketDataType = 1
    read_only: Literal[True] = True  # phase A: no order can be sent
    require_paper: Literal[True] = True  # live accounts are refused

    @field_validator("host")
    @classmethod
    def _loopback(cls, v: str) -> str:
        if v not in LOOPBACK:
            raise ValueError("the IBKR Gateway must be reached on this machine (127.0.0.1)")
        return v
