"""Alpaca PAPER Trading API adapter (official REST API, stdlib HTTP; no vendor SDK).

Verified against Alpaca's documentation (2026-10-08): paper base URL
https://paper-api.alpaca.markets (the only endpoint this project uses), key headers
APCA-API-KEY-ID / APCA-API-SECRET-KEY, POST /v2/orders (client_order_id <= 128 chars;
time_in_force opg = opening auction; fractional/notional orders only with `day`),
GET /v2/orders:by_client_order_id, GET /v2/account, GET /v2/positions/{symbol}.

Safety: the endpoint is the constant SANDBOX_BASE_URL (no parameter, setting or environment
variable can change it) and is asserted again on every request; https://api.alpaca.markets is
explicitly rejected;
redirects are never followed (a redirect could forward the key headers elsewhere); credentials
come only from the environment and never appear in repr, logs, errors or stored records. The
transport is injectable, so tests never touch the network.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.broker.config import KEY_ID_ENV, SANDBOX_BASE_URL, SECRET_ENV
from app.broker.gateway import (
    BrokerAccount,
    BrokerOrder,
    BrokerPosition,
    BrokerRequestError,
    BrokerSafetyError,
    OrderRequest,
    assert_sandbox_url,
)

# (method, url, headers, body, timeout) -> (status, response body)
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a 3xx becomes an HTTPError instead of being followed


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    assert_sandbox_url(url)
    req = Request(url, data=body, method=method, headers=headers)  # noqa: S310 - https only
    opener = build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()


def _f(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


class AlpacaPaperGateway:
    name = "alpaca"
    environment = "sandbox"

    def __init__(
        self,
        key_id: str,
        secret: str,
        *,
        transport: Transport = urllib_transport,
        timeout: float = 10.0,
    ) -> None:
        # No endpoint parameter exists: the sandbox URL is a constant, so no caller, setting or
        # environment variable can point this adapter anywhere else.
        if not key_id or not secret:
            raise BrokerSafetyError("sandbox credentials are missing")
        self._base = assert_sandbox_url(SANDBOX_BASE_URL).rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        }
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_env(cls, env: Mapping[str, str], **kw: Any) -> "AlpacaPaperGateway":
        return cls(env.get(KEY_ID_ENV, "").strip(), env.get(SECRET_ENV, "").strip(), **kw)

    def __repr__(self) -> str:  # never reveal credentials
        return f"AlpacaPaperGateway(environment='sandbox', base='{self._base}')"

    # ── HTTP ──

    def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        url = assert_sandbox_url(self._base + path + (f"?{urlencode(query)}" if query else ""))
        headers = dict(self._headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._transport(method, url, headers, body, self._timeout)
        except (URLError, TimeoutError, OSError) as exc:
            raise BrokerRequestError(
                f"{type(exc).__name__} reaching the sandbox", ambiguous=method == "POST"
            ) from None
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return status, data

    def _ok(self, status: int, data: Any, what: str) -> Any:
        if not 200 <= status < 300:
            msg = data.get("message") if isinstance(data, dict) else None
            raise BrokerRequestError(f"{what}: HTTP {status} {msg or ''}".strip(), status=status)
        return data

    # ── gateway ──

    def account(self) -> BrokerAccount:
        d = self._ok(*self._call("GET", "/v2/account"), "account")
        return BrokerAccount(
            account_id=str(d.get("account_number") or d.get("id")),
            status=str(d.get("status")),
            currency=str(d.get("currency")),
            cash=float(d["cash"]),
            equity=float(d["equity"]),
            buying_power=float(d["buying_power"]),
            trading_blocked=bool(d.get("trading_blocked")),
            account_blocked=bool(d.get("account_blocked")),
        )

    def position(self, symbol: str) -> BrokerPosition | None:
        status, d = self._call("GET", f"/v2/positions/{quote(symbol)}")
        if status == 404:
            return None
        d = self._ok(status, d, "position")
        return BrokerPosition(
            symbol=str(d["symbol"]),
            qty=float(d["qty"]),
            avg_entry_price=_f(d.get("avg_entry_price")),
            market_value=_f(d.get("market_value")),
        )

    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        status, d = self._call(
            "GET", "/v2/orders:by_client_order_id", query={"client_order_id": client_order_id}
        )
        if status in (404, 422):
            return None
        return self._order(self._ok(status, d, "order lookup"))

    def submit(self, request: OrderRequest) -> BrokerOrder:
        if len(request.client_order_id) > 128:
            raise BrokerSafetyError("client_order_id longer than 128 characters")
        payload = {
            "symbol": request.symbol,
            "qty": f"{request.qty:.9f}".rstrip("0").rstrip("."),
            "side": request.side,
            "type": request.order_type,
            "time_in_force": request.time_in_force,
            "client_order_id": request.client_order_id,
        }
        status, d = self._call("POST", "/v2/orders", payload)
        return self._order(self._ok(status, d, "order submission"))

    @staticmethod
    def _order(d: dict[str, Any]) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=str(d["id"]),
            client_order_id=str(d["client_order_id"]),
            symbol=str(d["symbol"]),
            side=str(d["side"]),
            qty=float(d.get("qty") or 0),
            status=str(d["status"]),
            filled_qty=float(d.get("filled_qty") or 0),
            filled_avg_price=_f(d.get("filled_avg_price")),
            submitted_at=d.get("submitted_at"),
            filled_at=d.get("filled_at"),
        )
