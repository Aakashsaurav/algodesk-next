"""Error types for AlgoDesk.

Every exception carries *what* went wrong, *where* it happened, and
a *fix* hint so the caller or operator can act.
"""

from __future__ import annotations


class AlgoDeskError(Exception):
    """Base for all AlgoDesk exceptions.

    Parameters
    ----------
    what : str
        What went wrong.
    where : str
        Where it happened (file, key path, component, …).
    fix : str
        What the user should do to resolve it.
    """

    def __init__(self, what: str, where: str, fix: str) -> None:
        self.what = what
        self.where = where
        self.fix = fix
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"{self.what} | {self.where} | {self.fix}"


class ConfigError(AlgoDeskError):
    """Configuration is invalid or unreadable."""


class ManifestError(AlgoDeskError):
    """A plugin manifest failed validation."""


class PluginError(AlgoDeskError):
    """A plugin could not be loaded or executed."""


class NotSupported(AlgoDeskError):
    """An undeclared capability was requested (CB-5)."""


class ParamError(AlgoDeskError):
    """A parameter operation failed validation or bounds."""


class SafetyViolation(AlgoDeskError):
    """A safety invariant was breached."""


class JournalError(AlgoDeskError):
    """The journal is corrupt, unwritable or failed verification."""


class CalendarCoverageError(AlgoDeskError):
    """Requested date or exchange is outside calendar coverage."""


class DataError(AlgoDeskError):
    """A data operation, selector, or catalog query failed."""
