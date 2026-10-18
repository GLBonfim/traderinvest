"""Paper-account guard: the only accounts this build may talk to.

IBKR paper (simulated) account ids start with "DU" (individual paper accounts; live ones start
with "U"). This is the documented convention to confirm on the first real connection; anything
else is refused, so a Gateway logged into LIVE can never be used, even read-only.
"""

from collections.abc import Sequence

from app.fx.errors import EnvironmentMismatchError

PAPER_PREFIXES: tuple[str, ...] = ("DU",)


class NotPaperAccountError(EnvironmentMismatchError):
    code = "not_paper_account"


def mask(account: str) -> str:
    """An account id safe to print: prefix and last two characters only."""
    if len(account) <= 4:
        return "*" * len(account)
    return f"{account[:2]}{'*' * (len(account) - 4)}{account[-2:]}"


def assert_paper_accounts(accounts: Sequence[str]) -> list[str]:
    ids = [a.strip() for a in accounts if a and a.strip()]
    if not ids:
        raise NotPaperAccountError("a sessão IBKR não indicou nenhuma conta")
    live = [a for a in ids if not a.startswith(PAPER_PREFIXES)]
    if live:
        raise NotPaperAccountError(
            "a sessão IBKR inclui conta(s) que não são paper ("
            + ", ".join(mask(a) for a in live)
            + "); entre no Gateway em modo Paper"
        )
    return ids
