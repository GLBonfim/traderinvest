"""Classified start-up refusals of the FX engine.

The engine refuses to start (and therefore to process any order) when its persisted state or its
configuration cannot be trusted. Each refusal has a stable `code` that the engine prints
(`ENGINE_ERROR startup:<code> <message>`) and the desktop shows, so a failure is diagnosable
without reading logs. Messages name the failing document, sequence number or setting, never its
contents. Nothing is repaired, rewritten or deleted automatically: recovery is manual
(docs/fx-platform.md, "Arranque recusado").
"""


class FxStartupError(RuntimeError):
    """Base class: the engine must not start."""

    code = "startup"


class ConfigInvalidError(FxStartupError):
    code = "config_invalid"


class EnvironmentMismatchError(FxStartupError):
    """The configured environment, the adapter's declared environment and the environments
    enabled in this build do not agree (ADR-0036)."""

    code = "environment_mismatch"


class StateUnreadableError(FxStartupError):
    """The state database cannot be opened as a database at all."""

    code = "state_unreadable"


class StateCorruptError(FxStartupError):
    """A persisted state document is malformed, incomplete or inconsistent."""

    code = "state_corrupt"


class JournalIntegrityError(FxStartupError):
    """The hash-chained journal is altered, broken or shorter than the saved state expects."""

    code = "journal_integrity"


class AccountMismatchError(FxStartupError):
    """The configuration does not match the persisted account (e.g. its currency)."""

    code = "account_mismatch"
