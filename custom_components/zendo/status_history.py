"""The status history: what happened to each managed device record's connection,
over the past days, by day.

Plain logic with no Home Assistant import, so the tests load this module on its
own. ``status_history_store.py`` keeps it in a store, and the registry in
``managed_devices.py`` tells it each record's transitions. See
docs/managed-devices.md, "Status History".

Times passed in are Unix seconds, as the registry keeps them. Entries hold epoch
ms, as the answer gives them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, TypedDict

KIND_WENT_OFFLINE = "went_offline"
KIND_CAME_ONLINE = "came_online"
KIND_HUB_STARTED = "hub_started"

# One of the registry's OFFLINE_REASONS, in managed_devices.py. Defined here
# because the history records it when the restart window closes.
OFFLINE_REASON_TIMEOUT = "timeout"

CAUSE_RECONNECTED = "reconnected"

# The Homey app keeps its history with the same rules, so keep the two in sync.
HISTORY_DAYS = 3
MAX_ENTRIES_PER_DAY = 50
MERGE_WINDOW_MS = 10 * 60 * 1000

_CONNECT_CAUSE = re.compile(r"[a-z][a-z_]{0,31}")


def connect_cause(value: Any) -> str | None:
    """Return a registration's ``connect_cause``, or None for anything that isn't one.

    Never an error: a registration with a cause this can't read counts as one
    without a cause. A cause it doesn't know is still a cause.
    """
    if isinstance(value, str) and _CONNECT_CAUSE.fullmatch(value):
        return value
    return None


class StatusEntry(TypedDict):
    """One or more events of the same kind, merged."""

    kind: str
    # went_offline only: the record's offline reason.
    reason: str | None
    # came_online only: the registration's connect_cause, if it had one.
    cause: str | None
    first_at: int
    last_at: int
    count: int


class _RecordHistory(TypedDict):
    recorded_since: int
    entries: list[StatusEntry]


def _ms(timestamp: float) -> int:
    return int(timestamp * 1000)


def _age(entry: StatusEntry) -> tuple[int, int]:
    return (entry["last_at"], entry["first_at"])


def _is_epoch_ms(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class BNGntStatusHistory:
    """Each record's status history, the hub's starts, and the restart window.

    Kept in memory. ``on_change`` is called after every change to what's
    stored, for the store to save it. Until the restart window closes, a
    record restored *awaiting* its device counts as still online: see
    docs/managed-devices.md, "After a Restart".
    """

    def __init__(
        self,
        time_zone: Callable[[], tzinfo],
        restart_window: float,
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        self._time_zone = time_zone
        self._restart_window = restart_window
        self._on_change = on_change
        self._records: dict[str, _RecordHistory] = {}
        self._hub_starts: list[StatusEntry] = []
        # Each awaiting record's online_since and last_seen from before the stop.
        self._awaiting: dict[str, tuple[float | None, float | None]] = {}
        self._started_at: float | None = None
        # None until Home Assistant has started.
        self._window_from: float | None = None

    # -- storage ---------------------------------------------------------------

    def load(self, data: Any, managed_device_ids: Iterable[str], now: float) -> None:
        """Take what the store held, for the records that exist.

        A record with no history yet starts one now: at the first start of a
        version with the history, or for a record made while an older version
        ran. Anything that isn't valid is dropped.
        """
        stored = data if isinstance(data, dict) else {}
        stored_records = stored.get("records")
        if not isinstance(stored_records, dict):
            stored_records = {}

        self._records = {
            key: self._restored_record(stored_records.get(key), now)
            for key in managed_device_ids
        }
        self._hub_starts = self._restored_entries(
            stored.get("hub_started"), (KIND_HUB_STARTED,)
        )
        self._prune(now)
        if self.as_data() != data:
            self._on_change()

    def as_data(self) -> dict[str, Any]:
        """Return what the store saves."""
        return {
            "hub_started": [dict(entry) for entry in self._hub_starts],
            "records": {
                key: {
                    "recorded_since": record["recorded_since"],
                    "entries": [dict(entry) for entry in record["entries"]],
                }
                for key, record in self._records.items()
            },
        }

    # -- the hub ---------------------------------------------------------------

    def hub_started(self, now: float) -> None:
        """Record the hub's start. The restart window stays open until ``hub_running``."""
        self._started_at = now
        self._window_from = None
        self._add(self._hub_starts, KIND_HUB_STARTED, None, None, now, now)

    def hub_running(self, now: float) -> None:
        """Home Assistant has started, so devices can reconnect: the window closes in ``restart_window``."""
        if self._window_from is None:
            self._window_from = now

    def await_reconnect(
        self, key: str, online_since: float | None, last_seen: float | None
    ) -> None:
        """Hold a record restored offline whose device was online when the hub stopped."""
        self._awaiting[key] = (online_since, last_seen)

    def is_awaiting(self, key: str) -> bool:
        return key in self._awaiting

    def awaited_online_since(self, key: str) -> float | None:
        """Return the ``online_since`` an awaiting record had before the stop."""
        awaiting = self._awaiting.get(key)
        return awaiting[0] if awaiting is not None else None

    def settle(self, now: float) -> bool:
        """Once the restart window has closed, record each record still awaiting its device as gone offline.

        At its last heartbeat, or at the hub's start for a record that has
        none. Returns whether it recorded any. The registry calls this on its
        offline check as well as on each transition, so the entry doesn't wait
        for one.
        """
        if not self._awaiting or self._window_open(now):
            return False
        awaiting, self._awaiting = self._awaiting, {}
        for key, (_, last_seen) in awaiting.items():
            if last_seen is not None:
                at = last_seen
            elif self._started_at is not None:
                at = self._started_at
            else:
                at = now
            self._record(key, KIND_WENT_OFFLINE, OFFLINE_REASON_TIMEOUT, None, at, now)
        return True

    # -- records ---------------------------------------------------------------

    def add_record(self, key: str, now: float) -> None:
        """Start a new record's history, recorded from now."""
        self._awaiting.pop(key, None)
        self._records[key] = {"recorded_since": _ms(now), "entries": []}
        self._on_change()

    def remove_record(self, key: str) -> None:
        self._awaiting.pop(key, None)
        if self._records.pop(key, None) is not None:
            self._on_change()

    def replace(self, target: str, replacement: str) -> None:
        """The replacement takes the target over, and the record it leaves behind goes.

        The target keeps its own history. If the replacement's record was
        awaiting its device, the target is now, as that device is attached to it.
        """
        awaiting = self._awaiting.pop(replacement, None)
        self._awaiting.pop(target, None)
        if awaiting is not None:
            self._awaiting[target] = awaiting
        self.remove_record(replacement)

    def connected(
        self,
        key: str,
        *,
        was_online: bool,
        online_since: float | None,
        cause: str | None,
        now: float,
    ) -> float:
        """Record a registration, or a state update, which has no cause, for an existing record.

        Returns the record's ``online_since`` after it: now when it comes back
        from offline or the registration has a cause, else the one it has. A
        record awaiting its device gets back the one it had before the stop.
        """
        self.settle(now)
        awaiting = self._awaiting.pop(key, None)
        if awaiting is not None:
            kept_online_since, _ = awaiting
            if cause is None:
                return kept_online_since if kept_online_since is not None else now
            # The hub's own restart dropped the connection: its hub_started
            # entry already says so.
            if cause != CAUSE_RECONNECTED:
                self._record(key, KIND_CAME_ONLINE, None, cause, now, now)
            return now

        if was_online and cause is None:
            return online_since if online_since is not None else now
        self._record(key, KIND_CAME_ONLINE, None, cause, now, now)
        return now

    def disconnected(
        self, key: str, *, was_online: bool, reason: str, at: float, now: float
    ) -> None:
        """Record a record going offline. Nothing for one that was already offline."""
        self.settle(now)
        awaiting = self._awaiting.pop(key, None)
        if was_online or awaiting is not None:
            self._record(key, KIND_WENT_OFFLINE, reason, None, at, now)

    # -- reading ---------------------------------------------------------------

    def answer(self, key: str, now: float) -> dict[str, Any]:
        """Return the record's history as ``zendo/v2/get_managed_device_status_history`` answers it.

        Exactly ``HISTORY_DAYS`` days, today first, each with its entries
        newest first, the hub's starts among them.
        """
        self.settle(now)
        if self._prune(now):
            self._on_change()

        record = self._records.get(key)
        record_entries = record["entries"] if record is not None else []
        today = self._day(_ms(now))
        days: list[dict[str, Any]] = []
        for offset in range(HISTORY_DAYS):
            day = today - timedelta(days=offset)
            entries = [
                entry
                for entry in (*record_entries, *self._hub_starts)
                if self._day(entry["first_at"]) == day
            ]
            # The sort is stable, so of two entries at the same time the one
            # added later comes first.
            entries.reverse()
            entries.sort(key=_age, reverse=True)
            days.append(
                {
                    "date": day.isoformat(),
                    "entries": [dict(entry) for entry in entries[:MAX_ENTRIES_PER_DAY]],
                }
            )
        return {
            "recorded_since": (
                record["recorded_since"] if record is not None else _ms(now)
            ),
            "days": days,
        }

    # -- internal --------------------------------------------------------------

    def _window_open(self, now: float) -> bool:
        return self._window_from is None or now - self._window_from < self._restart_window

    def _day(self, epoch_ms: int) -> date:
        return datetime.fromtimestamp(epoch_ms / 1000, self._time_zone()).date()

    def _record(
        self,
        key: str,
        kind: str,
        reason: str | None,
        cause: str | None,
        at: float,
        now: float,
    ) -> None:
        record = self._records.get(key)
        if record is None:
            record = self._records[key] = {"recorded_since": _ms(at), "entries": []}
        # A timeout is recorded at the last heartbeat, which can come before
        # the history started.
        record["recorded_since"] = min(record["recorded_since"], _ms(at))
        self._add(record["entries"], kind, reason, cause, at, now)

    def _add(
        self,
        entries: list[StatusEntry],
        kind: str,
        reason: str | None,
        cause: str | None,
        at: float,
        now: float,
    ) -> None:
        """Merge the event into the latest like entry on its day, or add one."""
        at_ms = _ms(at)
        day = self._day(at_ms)
        latest: StatusEntry | None = None
        for entry in entries:
            if (
                entry["kind"] == kind
                and entry["reason"] == reason
                and entry["cause"] == cause
                and self._day(entry["first_at"]) == day
                and (latest is None or entry["last_at"] > latest["last_at"])
            ):
                latest = entry

        if latest is not None and abs(at_ms - latest["last_at"]) <= MERGE_WINDOW_MS:
            latest["first_at"] = min(latest["first_at"], at_ms)
            latest["last_at"] = max(latest["last_at"], at_ms)
            latest["count"] += 1
        else:
            entries.append(
                {
                    "kind": kind,
                    "reason": reason,
                    "cause": cause,
                    "first_at": at_ms,
                    "last_at": at_ms,
                    "count": 1,
                }
            )
            self._cap_day(entries, day)
        self._prune(now)
        self._on_change()

    def _cap_day(self, entries: list[StatusEntry], day: date) -> None:
        on_day = [entry for entry in entries if self._day(entry["first_at"]) == day]
        excess = len(on_day) - MAX_ENTRIES_PER_DAY
        if excess <= 0:
            return
        dropped = {id(entry) for entry in sorted(on_day, key=_age)[:excess]}
        entries[:] = [entry for entry in entries if id(entry) not in dropped]

    def _prune(self, now: float) -> bool:
        """Drop the entries from before the days kept. Returns whether it dropped any."""
        oldest_kept = self._day(_ms(now)) - timedelta(days=HISTORY_DAYS - 1)
        dropped = False
        lists = [self._hub_starts, *(record["entries"] for record in self._records.values())]
        for entries in lists:
            kept = [entry for entry in entries if self._day(entry["first_at"]) >= oldest_kept]
            if len(kept) != len(entries):
                entries[:] = kept
                dropped = True
        return dropped

    def _restored_record(self, value: Any, now: float) -> _RecordHistory:
        if not isinstance(value, dict) or not self._is_day_time(value.get("recorded_since")):
            return {"recorded_since": _ms(now), "entries": []}
        return {
            "recorded_since": value["recorded_since"],
            "entries": self._restored_entries(
                value.get("entries"), (KIND_WENT_OFFLINE, KIND_CAME_ONLINE)
            ),
        }

    def _restored_entries(self, value: Any, kinds: tuple[str, ...]) -> list[StatusEntry]:
        if not isinstance(value, list):
            return []
        entries: list[StatusEntry] = []
        for item in value:
            entry = self._restored_entry(item, kinds)
            if entry is not None:
                entries.append(entry)
        return entries

    def _restored_entry(self, value: Any, kinds: tuple[str, ...]) -> StatusEntry | None:
        if not isinstance(value, dict) or value.get("kind") not in kinds:
            return None
        kind = value["kind"]
        first_at = value.get("first_at")
        last_at = value.get("last_at")
        count = value.get("count")
        if not (self._is_day_time(first_at) and self._is_day_time(last_at)):
            return None
        if first_at > last_at or not _is_epoch_ms(count) or count < 1:
            return None
        reason = value.get("reason") if kind == KIND_WENT_OFFLINE else None
        cause = value.get("cause") if kind == KIND_CAME_ONLINE else None
        if not (reason is None or isinstance(reason, str)):
            return None
        if not (cause is None or isinstance(cause, str)):
            return None
        return {
            "kind": kind,
            "reason": reason,
            "cause": cause,
            "first_at": first_at,
            "last_at": last_at,
            "count": count,
        }

    def _is_day_time(self, value: Any) -> bool:
        """Return whether ``value`` is epoch ms that has a date in the hub's time zone."""
        if not _is_epoch_ms(value):
            return False
        try:
            self._day(value)
        except (OverflowError, OSError, ValueError):
            return False
        return True
