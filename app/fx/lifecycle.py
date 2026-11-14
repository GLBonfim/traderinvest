r"""Order lifecycle (ADR-0036): every stage between an agent's signal and a closed position is a
distinct, recorded state; nothing jumps from a signal straight to a fill.

    SIGNAL -> PROPOSAL -> AUTHORIZED -> SENT -> ACCEPTED -> PARTIALLY_FILLED -> FILLED
                      \-> REFUSED (risk)  \-> REJECTED (broker) / UNKNOWN (ambiguous send)
                                                       ACCEPTED -> CANCELLED / EXPIRED

Positions have their own states: OPEN -> CLOSED. An UNKNOWN order (timeout, lost reply) is
resolved only by looking it up at the venue by its client order id, never by resending.

Allowed transitions (`_ALLOWED`, the single source of truth; `transition()` is the only
validation and every state change of an order goes through it):

    SIGNAL           -> PROPOSAL
    PROPOSAL         -> AUTHORIZED | REFUSED
    AUTHORIZED       -> SENT | CANCELLED (never sent)
    SENT             -> ACCEPTED | REJECTED | UNKNOWN | PARTIALLY_FILLED | FILLED
    UNKNOWN          -> ACCEPTED | REJECTED | PARTIALLY_FILLED | FILLED | CANCELLED
    ACCEPTED         -> PARTIALLY_FILLED | FILLED | CANCELLED | EXPIRED
    PARTIALLY_FILLED -> PARTIALLY_FILLED (more fills) | FILLED | CANCELLED | EXPIRED
                        (CANCELLED/EXPIRED after a partial fill = remainder cancelled)

Terminal states (REFUSED, REJECTED, FILLED, CANCELLED, EXPIRED) have no way out.
"""

from enum import StrEnum


class OrderState(StrEnum):
    SIGNAL = "signal"
    PROPOSAL = "proposal"
    AUTHORIZED = "authorized"
    REFUSED = "refused"
    SENT = "sent"
    UNKNOWN = "unknown"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL: frozenset[OrderState] = frozenset(
    {
        OrderState.REFUSED,
        OrderState.REJECTED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    }
)

_ALLOWED: dict[OrderState, frozenset[OrderState]] = {
    OrderState.SIGNAL: frozenset({OrderState.PROPOSAL}),
    OrderState.PROPOSAL: frozenset({OrderState.AUTHORIZED, OrderState.REFUSED}),
    OrderState.AUTHORIZED: frozenset({OrderState.SENT, OrderState.CANCELLED}),
    OrderState.SENT: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.REJECTED,
            OrderState.UNKNOWN,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        }
    ),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.REJECTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
        }
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
}

LABELS: dict[OrderState, str] = {
    OrderState.SIGNAL: "Sinal",
    OrderState.PROPOSAL: "Proposta",
    OrderState.AUTHORIZED: "Intenção autorizada",
    OrderState.REFUSED: "Recusada pelo risco",
    OrderState.SENT: "Ordem enviada",
    OrderState.UNKNOWN: "Estado desconhecido (a verificar)",
    OrderState.ACCEPTED: "Aceite",
    OrderState.REJECTED: "Rejeitada",
    OrderState.PARTIALLY_FILLED: "Execução parcial",
    OrderState.FILLED: "Execução total",
    OrderState.CANCELLED: "Cancelada",
    OrderState.EXPIRED: "Expirada",
}


class IllegalTransitionError(ValueError):
    pass


def can_transition(src: OrderState, dst: OrderState) -> bool:
    return dst in _ALLOWED.get(src, frozenset())


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL


def transition(src: OrderState, dst: OrderState) -> OrderState:
    if not can_transition(src, dst):
        raise IllegalTransitionError(f"illegal order transition {src.value} -> {dst.value}")
    return dst


class PositionState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
