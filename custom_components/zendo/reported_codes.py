"""The codes a managed device reports to the hub: its admin code and its Device
Lock PIN.

Plain logic with no Home Assistant import, so the tests can load this module on
its own. ``admin_code.py`` and ``app_lock_pin.py`` keep each in its own store.
See docs/managed-devices.md, "Admin Code" and "Device Lock PIN".

A code must never reach a log line, an error message, an entity, an attribute,
a state or an event. The book's messages and errors never carry one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

# The app and the Homey app know the same three, for the admin code and the
# Device Lock PIN alike, so keep all three in sync.
STATUS_SET = "set"
STATUS_NOT_SET = "not_set"
STATUS_UNKNOWN = "unknown"
CODE_STATUSES = (STATUS_SET, STATUS_NOT_SET, STATUS_UNKNOWN)

CodeStatus = Literal["set", "not_set", "unknown"]

_CODE_PATTERN = re.compile(r"[0-9]{4}|[0-9]{6}|[0-9]{8}")

REPORTED_AT = "reported_at"


def is_valid_code(value: Any) -> bool:
    """Return whether ``value`` is 4, 6 or 8 ASCII digits, as an admin code and
    a Device Lock PIN are."""
    return isinstance(value, str) and _CODE_PATTERN.fullmatch(value) is not None


class ReportedCodeBook:
    """The last code each managed device reported, by managed device ID.

    Each entry is ``{<field>: <the code, or None>, "reported_at": <ISO 8601,
    UTC>}``, where ``field`` is ``code`` for an admin code and ``pin`` for a
    Device Lock PIN. No entry for a record: its device hasn't reported (status
    unknown). An entry whose code is None: its device reported that it has none
    (status not_set).
    """

    def __init__(self, field: str) -> None:
        self._field = field
        self._entries: dict[str, dict[str, str | None]] = {}

    @classmethod
    def from_data(
        cls, field: str, data: Any, known_keys: Iterable[str]
    ) -> tuple[ReportedCodeBook, bool]:
        """Read a store's data, keeping only valid entries of records in ``known_keys``.

        Returns the book, and whether an entry was dropped. Data that isn't an
        object reads as no entries, with none dropped.
        """
        book = cls(field)
        if not isinstance(data, dict):
            return book, False

        known = set(known_keys)
        for key, value in data.items():
            entry = book._restored_entry(value)
            if key in known and entry is not None:
                book._entries[key] = entry
        return book, len(book._entries) != len(data)

    def _restored_entry(self, value: Any) -> dict[str, str | None] | None:
        if not isinstance(value, dict):
            return None
        code = value.get(self._field)
        reported_at = value.get(REPORTED_AT)
        if not (code is None or is_valid_code(code)):
            return None
        if not isinstance(reported_at, str):
            return None
        return {self._field: code, REPORTED_AT: reported_at}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> dict[str, str | None] | None:
        return self._entries.get(key)

    def status(self, key: str) -> CodeStatus:
        """Return the status the record's entry reads as: no entry is unknown."""
        entry = self._entries.get(key)
        if entry is None:
            return STATUS_UNKNOWN
        return STATUS_SET if entry[self._field] is not None else STATUS_NOT_SET

    def report(self, key: str, code: str | None, reported_at: str) -> None:
        """Keep a checked code, or None for none, as reported at ``reported_at``."""
        self._entries[key] = {self._field: code, REPORTED_AT: reported_at}

    def remove(self, key: str) -> bool:
        """Remove the record's entry. Returns whether it had one."""
        return self._entries.pop(key, None) is not None

    def to_data(self) -> dict[str, dict[str, str | None]]:
        return {key: dict(entry) for key, entry in self._entries.items()}
