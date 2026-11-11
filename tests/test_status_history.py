"""Tests for custom_components/zendo/status_history.py.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Needs nothing from Home Assistant: the module is loaded from its file, not as
part of the integration's package.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "custom_components" / "zendo" / "status_history.py"

_spec = importlib.util.spec_from_file_location("bngnt_status_history", MODULE_PATH)
status_history = importlib.util.module_from_spec(_spec)
sys.modules["bngnt_status_history"] = status_history
_spec.loader.exec_module(status_history)

TZ = ZoneInfo("Europe/Rome")
RESTART_WINDOW = 180
MINUTE = 60
SAVE_DELAY = status_history.SAVE_DELAY
KEY = "md_front_desk"
OTHER = "md_meeting_room"


def at(month, day, hour, minute=0, second=0, year=2026):
    """Unix seconds of a wall-clock time in the hub's time zone."""
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ).timestamp()


def ms(timestamp):
    return round(timestamp * 1000)


class Changes:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


def make(*keys, now=None, data=None, time_zone=TZ):
    """A history loaded at ``now``, by default before every event a test records,
    with nothing left to save."""
    changes = Changes()
    history = status_history.BNGntStatusHistory(lambda: time_zone, RESTART_WINDOW, changes)
    history.load(data, keys or (KEY,), now if now is not None else at(11, 8, 0))
    saved(history, changes)
    return history, changes


def saved(history, changes):
    """Take what the store would write, as it writes it."""
    history.data_to_save()
    changes.count = 0


def stored(recorded_since, *keys, last_seen=None):
    """What the store holds for records with no entries."""
    return {
        "hub_started": [],
        "records": {
            key: {
                "recorded_since": ms(recorded_since),
                "last_seen": ms(last_seen) if last_seen is not None else None,
                "entries": [],
            }
            for key in keys
        },
    }


def day_entries(answer, index):
    return answer["days"][index]["entries"]


def kinds(entries):
    return [entry["kind"] for entry in entries]


def offline(history, when, key=KEY, reason="timeout", now=None):
    history.disconnected(
        key, was_online=True, reason=reason, at=when, now=now if now is not None else when
    )


def online(history, when, key=KEY, cause=None, was_online=False, online_since=None):
    return history.connected(
        key, was_online=was_online, online_since=online_since, cause=cause, now=when
    )


class ConnectCauseTest(unittest.TestCase):
    def test_the_three_causes_the_app_sends(self):
        for cause in ("device_restarted", "zendo_restarted", "reconnected"):
            self.assertEqual(status_history.connect_cause(cause), cause)

    def test_a_cause_the_hub_doesnt_know_is_still_a_cause(self):
        self.assertEqual(status_history.connect_cause("power_cut"), "power_cut")
        self.assertEqual(status_history.connect_cause("a"), "a")

    def test_up_to_32_characters(self):
        self.assertEqual(status_history.connect_cause("a" * 32), "a" * 32)
        self.assertEqual(status_history.connect_cause("a" + "_" * 31), "a" + "_" * 31)
        self.assertIsNone(status_history.connect_cause("a" * 33))

    def test_anything_else_is_no_cause(self):
        for value in (
            None,
            "",
            "Reconnected",
            "RECONNECTED",
            "_reconnected",
            "1reconnected",
            "re-connected",
            "re connected",
            "reconnected\n",
            " reconnected",
            "recon2nected",
            "é",
            0,
            1,
            True,
            False,
            1.5,
            [],
            ["reconnected"],
            {},
            {"cause": "reconnected"},
            b"reconnected",
        ):
            with self.subTest(value=value):
                self.assertIsNone(status_history.connect_cause(value))


class MergingTest(unittest.TestCase):
    def test_events_within_ten_minutes_merge(self):
        history, _ = make()
        offline(history, at(11, 10, 9))
        offline(history, at(11, 10, 9, 4))
        offline(history, at(11, 10, 9, 9))
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        record = [entry for entry in entries if entry["kind"] == "went_offline"]
        self.assertEqual(
            record,
            [
                {
                    "kind": "went_offline",
                    "reason": "timeout",
                    "cause": None,
                    "first_at": ms(at(11, 10, 9)),
                    "last_at": ms(at(11, 10, 9, 9)),
                    "count": 3,
                }
            ],
        )

    def test_the_window_runs_from_the_entrys_latest_event(self):
        history, _ = make()
        for minute in (0, 9, 18, 27, 36):
            online(history, at(11, 10, 9, minute))
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["count"], 5)
        self.assertEqual(entries[0]["first_at"], ms(at(11, 10, 9)))
        self.assertEqual(entries[0]["last_at"], ms(at(11, 10, 9, 36)))

    def test_exactly_ten_minutes_merges_and_a_millisecond_more_doesnt(self):
        history, _ = make()
        start = at(11, 10, 9)
        offline(history, start)
        offline(history, start + 10 * MINUTE)
        offline(history, start + 20 * MINUTE + 0.001)
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual([entry["count"] for entry in entries], [1, 2])

    def test_another_reason_or_cause_is_another_entry(self):
        history, _ = make()
        offline(history, at(11, 10, 9), reason="timeout")
        offline(history, at(11, 10, 9, 1), reason="unregistered")
        online(history, at(11, 10, 9, 2))
        online(history, at(11, 10, 9, 6), cause="reconnected")
        online(history, at(11, 10, 9, 7), cause="device_restarted")
        online(history, at(11, 10, 9, 8), cause="reconnected")
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(
            [(entry["kind"], entry["reason"], entry["cause"], entry["count"]) for entry in entries],
            [
                ("came_online", None, "reconnected", 2),
                ("came_online", None, "device_restarted", 1),
                ("came_online", None, None, 1),
                ("went_offline", "unregistered", None, 1),
                ("went_offline", "timeout", None, 1),
            ],
        )

    def test_it_merges_into_the_latest_like_entry_not_the_newest_overall(self):
        history, _ = make()
        offline(history, at(11, 10, 9))
        online(history, at(11, 10, 9, 2), cause="device_restarted")
        offline(history, at(11, 10, 9, 5))
        online(history, at(11, 10, 9, 7), cause="device_restarted")
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(
            [(entry["kind"], entry["count"]) for entry in entries],
            [("came_online", 2), ("went_offline", 2)],
        )

    def test_it_merges_into_the_most_recent_of_several_like_entries(self):
        history, _ = make()
        offline(history, at(11, 10, 9))
        offline(history, at(11, 10, 10))
        offline(history, at(11, 10, 10, 5))
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(
            [(entry["first_at"], entry["count"]) for entry in entries],
            [(ms(at(11, 10, 10)), 2), (ms(at(11, 10, 9)), 1)],
        )

    def test_an_earlier_event_never_moves_last_at_back(self):
        history, _ = make()
        offline(history, at(11, 10, 9, 5))
        offline(history, at(11, 10, 9, 1), now=at(11, 10, 9, 6))
        (entry,) = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(entry["first_at"], ms(at(11, 10, 9, 1)))
        self.assertEqual(entry["last_at"], ms(at(11, 10, 9, 5)))
        self.assertEqual(entry["count"], 2)

    def test_merging_never_crosses_midnight(self):
        history, _ = make()
        offline(history, at(11, 9, 23, 58))
        offline(history, at(11, 10, 0, 3))
        answer = history.answer(KEY, at(11, 10, 12))
        self.assertEqual([entry["count"] for entry in day_entries(answer, 0)], [1])
        self.assertEqual([entry["count"] for entry in day_entries(answer, 1)], [1])

    def test_each_record_merges_its_own_entries(self):
        history, _ = make(KEY, OTHER)
        offline(history, at(11, 10, 9), key=KEY)
        offline(history, at(11, 10, 9, 1), key=OTHER)
        self.assertEqual(day_entries(history.answer(KEY, at(11, 10, 12)), 0)[0]["count"], 1)
        self.assertEqual(day_entries(history.answer(OTHER, at(11, 10, 12)), 0)[0]["count"], 1)

    def test_hub_starts_merge_among_themselves(self):
        history, _ = make()
        history.hub_started(at(11, 10, 9))
        offline(history, at(11, 10, 9, 1))
        history.hub_started(at(11, 10, 9, 4))
        history.hub_started(at(11, 10, 9, 30))
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(
            [(entry["kind"], entry["count"]) for entry in entries],
            [("hub_started", 1), ("hub_started", 2), ("went_offline", 1)],
        )

    def test_the_first_change_asks_for_a_save_and_the_rest_wait_for_it(self):
        history, changes = make()
        self.assertFalse(history.has_unsaved_changes)
        offline(history, at(11, 10, 9))
        offline(history, at(11, 10, 9, 1))
        history.hub_started(at(11, 10, 9, 2))
        self.assertEqual(changes.count, 1)
        self.assertTrue(history.has_unsaved_changes)
        history.data_to_save()
        self.assertFalse(history.has_unsaved_changes)
        offline(history, at(11, 10, 9, 3))
        self.assertEqual(changes.count, 2)


class DaysTest(unittest.TestCase):
    def test_exactly_three_days_today_first(self):
        history, _ = make()
        answer = history.answer(KEY, at(11, 10, 12))
        self.assertEqual(
            answer["days"],
            [
                {"date": "2026-11-10", "entries": []},
                {"date": "2026-11-09", "entries": []},
                {"date": "2026-11-08", "entries": []},
            ],
        )

    def test_entries_go_to_the_local_day_of_their_first_event(self):
        history, _ = make()
        # 23:30 UTC on the 8th is 00:30 on the 9th in Rome.
        late = datetime(2026, 11, 8, 23, 30, tzinfo=ZoneInfo("UTC")).timestamp()
        offline(history, late)
        offline(history, at(11, 10, 8))
        offline(history, at(11, 8, 22))
        answer = history.answer(KEY, at(11, 10, 12))
        self.assertEqual(
            [[entry["first_at"] for entry in day["entries"]] for day in answer["days"]],
            [[ms(at(11, 10, 8))], [ms(late)], [ms(at(11, 8, 22))]],
        )

    def test_the_days_follow_the_time_zone(self):
        history, _ = make(time_zone=ZoneInfo("America/New_York"))
        # 03:00 on the 10th in Rome is 21:00 on the 9th in New York.
        offline(history, at(11, 10, 3))
        answer = history.answer(KEY, at(11, 10, 3, 5))
        self.assertEqual(answer["days"][0]["date"], "2026-11-09")
        self.assertEqual(len(day_entries(answer, 0)), 1)

    def test_entries_newest_first_by_last_at(self):
        history, _ = make()
        offline(history, at(11, 10, 8))
        online(history, at(11, 10, 8, 5), cause="device_restarted")
        offline(history, at(11, 10, 8, 30))
        # Merges into the 08:00 entry and moves its last_at past the others.
        offline(history, at(11, 10, 8, 39), now=at(11, 10, 8, 40))
        history.hub_started(at(11, 10, 8, 20))
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(
            [entry["last_at"] for entry in entries],
            sorted((entry["last_at"] for entry in entries), reverse=True),
        )
        self.assertEqual(kinds(entries), ["went_offline", "hub_started", "came_online", "went_offline"])

    def test_hub_starts_are_in_every_records_answer(self):
        history, _ = make(KEY, OTHER)
        history.hub_started(at(11, 10, 9))
        for key in (KEY, OTHER):
            entries = day_entries(history.answer(key, at(11, 10, 12)), 0)
            self.assertEqual(
                entries,
                [
                    {
                        "kind": "hub_started",
                        "reason": None,
                        "cause": None,
                        "first_at": ms(at(11, 10, 9)),
                        "last_at": ms(at(11, 10, 9)),
                        "count": 1,
                    }
                ],
            )

    def test_an_entry_has_exactly_the_six_fields(self):
        history, _ = make()
        online(history, at(11, 10, 9), cause="zendo_restarted")
        (entry,) = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(
            entry,
            {
                "kind": "came_online",
                "reason": None,
                "cause": "zendo_restarted",
                "first_at": ms(at(11, 10, 9)),
                "last_at": ms(at(11, 10, 9)),
                "count": 1,
            },
        )

    def test_the_answer_is_a_copy(self):
        history, _ = make()
        offline(history, at(11, 10, 9))
        answer = history.answer(KEY, at(11, 10, 12))
        day_entries(answer, 0)[0]["count"] = 99
        self.assertEqual(day_entries(history.answer(KEY, at(11, 10, 12)), 0)[0]["count"], 1)

    def test_the_answer_across_a_daylight_saving_change(self):
        history, _ = make(now=at(10, 25, 0))
        offline(history, at(10, 25, 2, 30))
        answer = history.answer(KEY, at(10, 26, 9))
        self.assertEqual(
            [day["date"] for day in answer["days"]], ["2026-10-26", "2026-10-25", "2026-10-24"]
        )
        self.assertEqual(len(day_entries(answer, 1)), 1)


class PruningTest(unittest.TestCase):
    def test_a_write_drops_the_days_before_the_three_kept(self):
        history, _ = make(now=at(11, 7, 9))
        offline(history, at(11, 7, 23))
        offline(history, at(11, 8, 0, 30))
        history.hub_started(at(11, 7, 22))
        offline(history, at(11, 10, 9))
        stored = history.as_data()
        self.assertEqual(
            [entry["first_at"] for entry in stored["records"][KEY]["entries"]],
            [ms(at(11, 8, 0, 30)), ms(at(11, 10, 9))],
        )
        self.assertEqual(stored["hub_started"], [])

    def test_a_read_drops_them_and_saves(self):
        history, changes = make(now=at(11, 8, 9))
        offline(history, at(11, 8, 9))
        history.hub_started(at(11, 8, 10))
        saved(history, changes)
        answer = history.answer(KEY, at(11, 11, 0, 1))
        self.assertEqual([day["entries"] for day in answer["days"]], [[], [], []])
        self.assertEqual(history.as_data()["records"][KEY]["entries"], [])
        self.assertEqual(history.as_data()["hub_started"], [])
        self.assertEqual(changes.count, 1)

    def test_a_read_with_nothing_to_drop_doesnt_save(self):
        history, changes = make()
        offline(history, at(11, 10, 9))
        saved(history, changes)
        history.answer(KEY, at(11, 10, 12))
        self.assertEqual(changes.count, 0)
        self.assertFalse(history.has_unsaved_changes)

    def test_two_days_ago_is_kept_until_midnight(self):
        history, _ = make(now=at(11, 8, 0))
        offline(history, at(11, 8, 0, 0, 1))
        self.assertEqual(len(day_entries(history.answer(KEY, at(11, 10, 23, 59, 59)), 2)), 1)
        self.assertEqual(
            [day["entries"] for day in history.answer(KEY, at(11, 11, 0))["days"]], [[], [], []]
        )

    def test_a_load_drops_them(self):
        history, _ = make(now=at(11, 7, 9))
        offline(history, at(11, 7, 9))
        offline(history, at(11, 9, 9))
        reloaded, _ = make(data=history.as_data(), now=at(11, 10, 9))
        self.assertEqual(
            [entry["first_at"] for entry in reloaded.as_data()["records"][KEY]["entries"]],
            [ms(at(11, 9, 9))],
        )


class BackstopTest(unittest.TestCase):
    def test_at_most_fifty_entries_a_day_the_oldest_going(self):
        history, _ = make(now=at(11, 10, 0))
        start = at(11, 10, 0, 1)
        for index in range(55):
            offline(history, start + index * 11 * MINUTE)
        entries = day_entries(history.answer(KEY, at(11, 10, 23)), 0)
        self.assertEqual(len(entries), 50)
        self.assertEqual(entries[-1]["first_at"], ms(start + 5 * 11 * MINUTE))
        self.assertEqual(entries[0]["first_at"], ms(start + 54 * 11 * MINUTE))
        self.assertEqual(len(history.as_data()["records"][KEY]["entries"]), 50)

    def test_the_backstop_is_per_day(self):
        history, _ = make(now=at(11, 9, 0))
        for day in (9, 10):
            start = at(11, day, 0, 1)
            for index in range(50):
                offline(history, start + index * 11 * MINUTE)
        answer = history.answer(KEY, at(11, 10, 23))
        self.assertEqual(len(day_entries(answer, 0)), 50)
        self.assertEqual(len(day_entries(answer, 1)), 50)

    def test_hub_starts_have_their_own_fifty(self):
        history, _ = make(now=at(11, 10, 0))
        start = at(11, 10, 0, 1)
        for index in range(55):
            history.hub_started(start + index * 11 * MINUTE)
        self.assertEqual(len(history.as_data()["hub_started"]), 50)

    def test_an_answer_never_has_more_than_fifty_a_day(self):
        history, _ = make(now=at(11, 10, 0))
        start = at(11, 10, 0, 1)
        for index in range(50):
            offline(history, start + index * 11 * MINUTE)
        history.hub_started(start + 5 * MINUTE)
        history.hub_started(start + 50 * 11 * MINUTE)
        entries = day_entries(history.answer(KEY, at(11, 10, 23)), 0)
        self.assertEqual(len(entries), 50)
        self.assertEqual(entries[0]["kind"], "hub_started")
        self.assertNotIn(ms(start), [entry["first_at"] for entry in entries])

    def test_merging_keeps_a_restart_loop_far_from_it(self):
        history, _ = make(now=at(11, 10, 0))
        start = at(11, 10, 0, 1)
        for index in range(500):
            when = start + index * 2 * MINUTE
            offline(history, when)
            online(history, when + 30, cause="device_restarted")
        entries = day_entries(history.answer(KEY, at(11, 10, 23)), 0)
        self.assertEqual(len(entries), 2)
        self.assertEqual([entry["count"] for entry in entries], [500, 500])


class OnlineSinceTest(unittest.TestCase):
    def test_a_registration_without_a_cause_on_an_online_record_leaves_it_alone(self):
        history, _ = make()
        since = at(11, 10, 8)
        result = online(history, at(11, 10, 9), was_online=True, online_since=since)
        self.assertEqual(result, since)
        self.assertEqual(day_entries(history.answer(KEY, at(11, 10, 9)), 0), [])

    def test_a_state_update_on_an_online_record_leaves_it_alone(self):
        history, _ = make()
        since = at(11, 10, 8)
        self.assertEqual(online(history, at(11, 10, 9), was_online=True, online_since=since), since)

    def test_a_registration_with_a_cause_on_an_online_record_moves_it(self):
        history, _ = make()
        now = at(11, 10, 9)
        result = online(
            history, now, was_online=True, online_since=at(11, 10, 8), cause="zendo_restarted"
        )
        self.assertEqual(result, now)
        entries = day_entries(history.answer(KEY, at(11, 10, 9)), 0)
        self.assertEqual([(entry["kind"], entry["cause"]) for entry in entries], [("came_online", "zendo_restarted")])

    def test_any_cause_moves_it_one_the_hub_doesnt_know_included(self):
        history, _ = make()
        now = at(11, 10, 9)
        self.assertEqual(
            online(history, now, was_online=True, online_since=at(11, 10, 8), cause="power_cut"),
            now,
        )
        self.assertEqual(day_entries(history.answer(KEY, now), 0)[0]["cause"], "power_cut")

    def test_coming_back_from_offline_moves_it_and_records_it(self):
        for cause in (None, "reconnected", "device_restarted"):
            with self.subTest(cause=cause):
                history, _ = make()
                now = at(11, 10, 9)
                self.assertEqual(online(history, now, cause=cause), now)
                entries = day_entries(history.answer(KEY, now), 0)
                self.assertEqual(
                    [(entry["kind"], entry["cause"]) for entry in entries],
                    [("came_online", cause)],
                )

    def test_an_online_record_without_one_gets_now(self):
        history, _ = make()
        now = at(11, 10, 9)
        self.assertEqual(online(history, now, was_online=True, online_since=None), now)

    def test_going_offline_twice_records_once(self):
        history, _ = make()
        offline(history, at(11, 10, 9))
        history.disconnected(
            KEY, was_online=False, reason="unregistered", at=at(11, 10, 9, 5), now=at(11, 10, 9, 5)
        )
        entries = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual([(entry["kind"], entry["reason"]) for entry in entries], [("went_offline", "timeout")])


# When the history held by ``restarted`` began, unless a test gives its own.
HISTORY_BEGAN = at(11, 10, 6)


def restarted(online_since, last_seen, stop_and_start, *, running=True, data=None):
    """The history after a hub restart, with KEY online when it stopped.

    ``last_seen`` is the one the config entry has.
    """
    if data is None:
        data = stored(HISTORY_BEGAN, KEY, OTHER)
    history, changes = make(KEY, OTHER, data=data, now=stop_and_start)
    history.await_reconnect(KEY, online_since, last_seen)
    history.hub_started(stop_and_start)
    if running:
        history.hub_running(stop_and_start)
    saved(history, changes)
    return history, changes


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.online_since = at(11, 10, 7)
        self.last_seen = at(11, 10, 8, 59, 30)
        self.start = at(11, 10, 9)

    def record_entries(self, history, when):
        return [
            entry
            for entry in day_entries(history.answer(KEY, when), 0)
            if entry["kind"] != "hub_started"
        ]

    def test_a_device_that_rides_out_the_restart_records_nothing(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        result = online(history, self.start + 45)
        self.assertEqual(result, self.online_since)
        history.settle(self.start + 10 * MINUTE)
        self.assertEqual(self.record_entries(history, self.start + 10 * MINUTE), [])

    def test_a_registration_without_a_cause_keeps_online_since_too(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        self.assertEqual(online(history, self.start + 179, cause=None), self.online_since)
        self.assertEqual(self.record_entries(history, self.start + 179), [])

    def test_without_an_online_since_from_before_it_gets_now(self):
        history, _ = restarted(None, self.last_seen, self.start)
        self.assertEqual(online(history, self.start + 30), self.start + 30)
        self.assertEqual(self.record_entries(history, self.start + 30), [])

    def test_reconnected_in_the_window_isnt_recorded_but_moves_online_since(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        when = self.start + 20
        self.assertEqual(online(history, when, cause="reconnected"), when)
        self.assertEqual(self.record_entries(history, when), [])

    def test_other_causes_in_the_window_are_recorded(self):
        for cause in ("device_restarted", "zendo_restarted", "power_cut"):
            with self.subTest(cause=cause):
                history, _ = restarted(self.online_since, self.last_seen, self.start)
                when = self.start + 20
                self.assertEqual(online(history, when, cause=cause), when)
                self.assertEqual(
                    [(entry["kind"], entry["cause"]) for entry in self.record_entries(history, when)],
                    [("came_online", cause)],
                )

    def test_a_device_that_doesnt_come_back_went_offline_at_its_last_heartbeat(self):
        history, changes = restarted(self.online_since, self.last_seen, self.start)
        self.assertFalse(history.settle(self.start + RESTART_WINDOW - 1))
        self.assertTrue(history.is_awaiting(KEY))
        self.assertTrue(history.settle(self.start + RESTART_WINDOW))
        self.assertFalse(history.is_awaiting(KEY))
        self.assertEqual(changes.count, 1)
        self.assertEqual(
            self.record_entries(history, self.start + RESTART_WINDOW),
            [
                {
                    "kind": "went_offline",
                    "reason": "timeout",
                    "cause": None,
                    "first_at": ms(self.last_seen),
                    "last_at": ms(self.last_seen),
                    "count": 1,
                }
            ],
        )

    def test_once_the_window_has_closed_reconnected_is_recorded(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        when = self.start + RESTART_WINDOW + 30
        self.assertEqual(online(history, when, cause="reconnected"), when)
        self.assertEqual(
            [(entry["kind"], entry["cause"]) for entry in self.record_entries(history, when)],
            [("came_online", "reconnected"), ("went_offline", None)],
        )

    def test_a_state_update_after_the_window_records_both(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        when = self.start + RESTART_WINDOW + 30
        self.assertEqual(online(history, when), when)
        self.assertEqual(
            kinds(self.record_entries(history, when)), ["came_online", "went_offline"]
        )

    def test_the_window_stays_open_until_home_assistant_has_started(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start, running=False)
        self.assertFalse(history.settle(self.start + 10 * MINUTE))
        history.hub_running(self.start + 10 * MINUTE)
        self.assertEqual(online(history, self.start + 12 * MINUTE), self.online_since)
        self.assertEqual(self.record_entries(history, self.start + 12 * MINUTE), [])

    def test_the_window_counts_from_when_home_assistant_has_started(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start, running=False)
        history.hub_running(self.start + 5 * MINUTE)
        history.hub_running(self.start + 9 * MINUTE)
        self.assertFalse(history.settle(self.start + 5 * MINUTE + RESTART_WINDOW - 1))
        self.assertTrue(history.settle(self.start + 5 * MINUTE + RESTART_WINDOW))

    def test_an_unregister_in_the_window_is_recorded(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        when = self.start + 30
        history.disconnected(KEY, was_online=False, reason="unregistered", at=when, now=when)
        self.assertEqual(
            [(entry["kind"], entry["reason"], entry["first_at"]) for entry in self.record_entries(history, when)],
            [("went_offline", "unregistered", ms(when))],
        )
        self.assertFalse(history.settle(self.start + RESTART_WINDOW))

    def test_an_unregister_after_the_window_records_only_the_timeout(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        when = self.start + RESTART_WINDOW + 30
        history.disconnected(KEY, was_online=False, reason="unregistered", at=when, now=when)
        self.assertEqual(
            [(entry["reason"], entry["first_at"]) for entry in self.record_entries(history, when)],
            [("timeout", ms(self.last_seen))],
        )

    def test_a_record_offline_before_the_restart_records_its_return_in_the_window(self):
        for cause in (None, "reconnected"):
            with self.subTest(cause=cause):
                history, _ = restarted(self.online_since, self.last_seen, self.start)
                when = self.start + 30
                self.assertEqual(online(history, when, key=OTHER, cause=cause), when)
                entries = day_entries(history.answer(OTHER, self.start + 40), 0)
                self.assertEqual(
                    [(entry["kind"], entry["cause"]) for entry in entries],
                    [("came_online", cause), ("hub_started", None)],
                )

    def test_reconnected_for_a_record_online_before_the_restart_is_suppressed(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        when = self.start + RESTART_WINDOW - 1
        self.assertEqual(online(history, when, cause="reconnected"), when)
        self.assertEqual(self.record_entries(history, when), [])
        history.settle(self.start + 10 * MINUTE)
        self.assertEqual(self.record_entries(history, self.start + 10 * MINUTE), [])

    def test_a_record_offline_before_the_restart_is_recorded_whatever_its_cause(self):
        for cause in ("reconnected", None, "device_restarted", "zendo_restarted"):
            with self.subTest(cause=cause):
                history, _ = restarted(self.online_since, self.last_seen, self.start)
                when = self.start + 20
                self.assertEqual(online(history, when, key=OTHER, cause=cause), when)
                entries = [
                    entry
                    for entry in day_entries(history.answer(OTHER, when), 0)
                    if entry["kind"] != "hub_started"
                ]
                self.assertEqual(
                    [(entry["kind"], entry["cause"], entry["first_at"]) for entry in entries],
                    [("came_online", cause, ms(when))],
                )

    def test_a_restart_records_a_hub_start_and_nothing_else(self):
        history, _ = make(KEY, now=at(11, 10, 8))
        online(history, at(11, 10, 8, 30), cause="device_restarted")
        restart, _ = restarted(at(11, 10, 8, 30), self.last_seen, self.start, data=history.as_data())
        online(restart, self.start + 50)
        restart.settle(self.start + 30 * MINUTE)
        self.assertEqual(
            kinds(day_entries(restart.answer(KEY, self.start + 30 * MINUTE), 0)),
            ["hub_started", "came_online"],
        )

    def test_a_last_heartbeat_from_before_the_history_began_never_moves_recorded_since(self):
        # The first start with the history: it begins at the restart.
        history, _ = restarted(
            self.online_since, self.last_seen, self.start, data=stored(self.start, KEY, OTHER)
        )
        closed = self.start + RESTART_WINDOW
        history.settle(closed)
        self.assertEqual(history.answer(KEY, closed)["recorded_since"], ms(self.start))
        self.assertEqual(
            [(entry["kind"], entry["first_at"]) for entry in self.record_entries(history, closed)],
            [("went_offline", ms(self.start))],
        )

    def test_without_a_last_heartbeat_it_went_offline_at_the_hubs_start(self):
        history, _ = restarted(None, None, self.start)
        history.settle(self.start + RESTART_WINDOW)
        (entry,) = self.record_entries(history, self.start + RESTART_WINDOW)
        self.assertEqual(entry["first_at"], ms(self.start))

    def test_the_online_since_from_before_is_readable_while_awaiting(self):
        history, _ = restarted(self.online_since, self.last_seen, self.start)
        self.assertEqual(history.awaited_online_since(KEY), self.online_since)
        self.assertIsNone(history.awaited_online_since(OTHER))
        online(history, self.start + 10)
        self.assertFalse(history.is_awaiting(KEY))


class CrashTest(unittest.TestCase):
    """The hub stopped without writing the config entry, which holds the
    last_seen of the record's last transition, days before the history's."""

    def setUp(self):
        self.began = at(11, 8, 9)
        self.config_entry_last_seen = at(11, 8, 14)
        self.saved_last_seen = at(11, 10, 8, 58)
        self.start = at(11, 10, 9)

    def test_it_went_offline_at_the_last_heartbeat_the_history_saved(self):
        data = stored(self.began, KEY, OTHER, last_seen=self.saved_last_seen)
        history, _ = restarted(at(11, 8, 13), self.config_entry_last_seen, self.start, data=data)
        closed = self.start + RESTART_WINDOW
        self.assertTrue(history.settle(closed))
        answer = history.answer(KEY, closed)
        self.assertEqual(answer["recorded_since"], ms(self.began))
        self.assertEqual(
            [
                [(entry["kind"], entry["first_at"]) for entry in day["entries"]]
                for day in answer["days"]
            ],
            [
                [("hub_started", ms(self.start)), ("went_offline", ms(self.saved_last_seen))],
                [],
                [],
            ],
        )

    def test_a_restored_record_takes_the_later_last_seen(self):
        history, _ = make(KEY, OTHER, data=stored(self.began, KEY, last_seen=self.saved_last_seen))
        self.assertEqual(
            history.restored_last_seen(KEY, self.config_entry_last_seen), self.saved_last_seen
        )
        self.assertEqual(history.restored_last_seen(KEY, None), self.saved_last_seen)
        later = self.saved_last_seen + 30
        self.assertEqual(history.restored_last_seen(KEY, later), later)
        self.assertEqual(
            history.restored_last_seen(OTHER, self.config_entry_last_seen),
            self.config_entry_last_seen,
        )
        self.assertIsNone(history.restored_last_seen(OTHER, None))

    def test_without_a_saved_heartbeat_a_stale_one_never_moves_recorded_since(self):
        began = at(11, 10, 6)
        history, _ = restarted(
            None, self.config_entry_last_seen, self.start, data=stored(began, KEY, OTHER)
        )
        closed = self.start + RESTART_WINDOW
        history.settle(closed)
        answer = history.answer(KEY, closed)
        self.assertEqual(answer["recorded_since"], ms(began))
        self.assertEqual(
            [entry["first_at"] for entry in day_entries(answer, 0) if entry["kind"] == "went_offline"],
            [ms(began)],
        )
        self.assertEqual([day_entries(answer, 1), day_entries(answer, 2)], [[], []])


class LastSeenTest(unittest.TestCase):
    def test_a_heartbeat_moves_it_and_asks_for_a_save(self):
        history, changes = make()
        now = at(11, 10, 9)
        online(history, now, was_online=True, online_since=at(11, 10, 8))
        self.assertEqual(history.as_data()["records"][KEY]["last_seen"], ms(now))
        self.assertEqual(history.as_data()["records"][KEY]["entries"], [])
        self.assertEqual(changes.count, 1)

    def test_a_registration_or_a_return_moves_it(self):
        history, _ = make()
        online(history, at(11, 10, 9), cause="device_restarted")
        self.assertEqual(history.as_data()["records"][KEY]["last_seen"], ms(at(11, 10, 9)))

    def test_going_offline_leaves_it(self):
        history, _ = make()
        online(history, at(11, 10, 9), was_online=True)
        offline(history, at(11, 10, 9, 5), reason="unregistered")
        self.assertEqual(history.as_data()["records"][KEY]["last_seen"], ms(at(11, 10, 9)))

    def test_a_new_record_is_seen_at_its_creation(self):
        history, _ = make()
        history.add_record(OTHER, at(11, 10, 9, 30))
        self.assertEqual(history.as_data()["records"][OTHER]["last_seen"], ms(at(11, 10, 9, 30)))

    def test_it_survives_a_reload(self):
        history, _ = make()
        online(history, at(11, 10, 9), was_online=True)
        reloaded, _ = make(data=history.as_data(), now=at(11, 10, 10))
        self.assertEqual(reloaded.restored_last_seen(KEY, at(11, 10, 8)), at(11, 10, 9))

    def test_a_replace_gives_the_target_the_replacements(self):
        history, _ = make(KEY, OTHER)
        online(history, at(11, 10, 9), key=KEY, was_online=True)
        online(history, at(11, 10, 8), key=OTHER, was_online=True)
        history.replace(KEY, OTHER)
        self.assertEqual(history.as_data()["records"][KEY]["last_seen"], ms(at(11, 10, 8)))


class RecordsTest(unittest.TestCase):
    def test_a_new_record_is_recorded_from_its_creation(self):
        history, changes = make()
        history.add_record(OTHER, at(11, 10, 9, 30))
        answer = history.answer(OTHER, at(11, 10, 10))
        self.assertEqual(answer["recorded_since"], ms(at(11, 10, 9, 30)))
        self.assertEqual([day["entries"] for day in answer["days"]], [[], [], []])
        self.assertEqual(changes.count, 1)

    def test_a_new_record_starts_afresh(self):
        history, _ = make()
        offline(history, at(11, 10, 8))
        history.add_record(KEY, at(11, 10, 9))
        self.assertEqual(day_entries(history.answer(KEY, at(11, 10, 10)), 0), [])

    def test_removing_a_record_removes_its_history(self):
        history, changes = make(KEY, OTHER)
        offline(history, at(11, 10, 9))
        saved(history, changes)
        history.remove_record(KEY)
        self.assertNotIn(KEY, history.as_data()["records"])
        self.assertIn(OTHER, history.as_data()["records"])
        self.assertEqual(changes.count, 1)
        saved(history, changes)
        history.remove_record(KEY)
        self.assertEqual(changes.count, 0)

    def test_a_replace_keeps_the_targets_history_and_drops_the_replacements(self):
        history, _ = make(KEY, OTHER, now=at(11, 9, 9))
        offline(history, at(11, 10, 8), key=KEY)
        offline(history, at(11, 10, 8, 30), key=OTHER, reason="unregistered")
        history.replace(KEY, OTHER)
        stored = history.as_data()["records"]
        self.assertEqual(list(stored), [KEY])
        self.assertEqual(stored[KEY]["recorded_since"], ms(at(11, 9, 9)))
        self.assertEqual(
            [entry["reason"] for entry in stored[KEY]["entries"]], ["timeout"]
        )

    def test_a_replace_moves_the_replacements_awaiting_to_the_target(self):
        history, _ = make(KEY, OTHER)
        history.await_reconnect(KEY, at(11, 10, 6), at(11, 10, 6, 30))
        history.await_reconnect(OTHER, at(11, 10, 7), at(11, 10, 8, 59))
        history.hub_started(at(11, 10, 9))
        history.hub_running(at(11, 10, 9))
        history.replace(KEY, OTHER)
        self.assertTrue(history.is_awaiting(KEY))
        self.assertFalse(history.is_awaiting(OTHER))
        self.assertEqual(history.awaited_online_since(KEY), at(11, 10, 7))

    def test_a_replace_drops_the_targets_awaiting(self):
        history, _ = make(KEY, OTHER)
        history.await_reconnect(KEY, at(11, 10, 6), at(11, 10, 6, 30))
        history.replace(KEY, OTHER)
        self.assertFalse(history.is_awaiting(KEY))

    def test_a_record_without_a_history_answers_from_now(self):
        history, _ = make()
        history.hub_started(at(11, 10, 9))
        answer = history.answer("md_unknown", at(11, 10, 10))
        self.assertEqual(answer["recorded_since"], ms(at(11, 10, 10)))
        self.assertEqual([day["entries"] for day in answer["days"]], [[], [], []])


class HubStartsBeforeTheHistoryTest(unittest.TestCase):
    def test_a_records_answer_leaves_out_the_hub_starts_from_before_its_history(self):
        history, _ = make(KEY)
        history.hub_started(at(11, 10, 9))
        history.add_record(OTHER, at(11, 10, 9, 30))
        history.hub_started(at(11, 10, 10))
        online(history, at(11, 10, 10, 1), key=OTHER, cause="device_restarted")
        self.assertEqual(
            [
                (entry["kind"], entry["first_at"])
                for entry in day_entries(history.answer(OTHER, at(11, 10, 12)), 0)
            ],
            [("came_online", ms(at(11, 10, 10, 1))), ("hub_started", ms(at(11, 10, 10)))],
        )
        self.assertEqual(
            [entry["first_at"] for entry in day_entries(history.answer(KEY, at(11, 10, 12)), 0)],
            [ms(at(11, 10, 10)), ms(at(11, 10, 9))],
        )

    def test_one_that_began_before_it_is_left_out_whole(self):
        history, _ = make(KEY)
        history.hub_started(at(11, 10, 9))
        history.add_record(OTHER, at(11, 10, 9, 2))
        history.hub_started(at(11, 10, 9, 5))
        self.assertEqual(day_entries(history.answer(OTHER, at(11, 10, 12)), 0), [])
        (entry,) = day_entries(history.answer(KEY, at(11, 10, 12)), 0)
        self.assertEqual(entry["count"], 2)

    def test_one_at_the_moment_the_history_began_is_in_it(self):
        history, _ = make(KEY, now=at(11, 10, 9))
        history.hub_started(at(11, 10, 9))
        self.assertEqual(kinds(day_entries(history.answer(KEY, at(11, 10, 12)), 0)), ["hub_started"])


class LoadTest(unittest.TestCase):
    def test_the_first_load_starts_every_records_history_now(self):
        changes = Changes()
        history = status_history.BNGntStatusHistory(lambda: TZ, RESTART_WINDOW, changes)
        history.load(None, [KEY, OTHER], at(11, 10, 9))
        self.assertEqual(
            history.as_data(),
            {
                "hub_started": [],
                "records": {
                    KEY: {"recorded_since": ms(at(11, 10, 9)), "last_seen": None, "entries": []},
                    OTHER: {"recorded_since": ms(at(11, 10, 9)), "last_seen": None, "entries": []},
                },
            },
        )
        self.assertEqual(changes.count, 1)

    def test_a_round_trip_changes_nothing(self):
        history, _ = make(KEY, OTHER, now=at(11, 9, 9))
        offline(history, at(11, 10, 8))
        online(history, at(11, 10, 8, 5), cause="device_restarted")
        history.hub_started(at(11, 10, 8, 1))
        changes = Changes()
        reloaded = status_history.BNGntStatusHistory(lambda: TZ, RESTART_WINDOW, changes)
        reloaded.load(history.as_data(), [KEY, OTHER], at(11, 10, 9))
        self.assertEqual(reloaded.as_data(), history.as_data())
        self.assertEqual(changes.count, 0)

    def test_a_record_saved_without_a_last_seen_loads_without_one(self):
        data = {"hub_started": [], "records": {KEY: {"recorded_since": ms(at(11, 9, 9)), "entries": []}}}
        changes = Changes()
        history = status_history.BNGntStatusHistory(lambda: TZ, RESTART_WINDOW, changes)
        history.load(data, [KEY], at(11, 10, 9))
        self.assertEqual(
            history.as_data()["records"][KEY],
            {"recorded_since": ms(at(11, 9, 9)), "last_seen": None, "entries": []},
        )
        self.assertEqual(changes.count, 1)

    def test_a_record_thats_gone_is_dropped_and_a_new_one_starts_now(self):
        history, _ = make(KEY, OTHER, now=at(11, 9, 9))
        reloaded, _ = make(KEY, "md_new", data=history.as_data(), now=at(11, 10, 9))
        stored = reloaded.as_data()["records"]
        self.assertEqual(set(stored), {KEY, "md_new"})
        self.assertEqual(stored[KEY]["recorded_since"], ms(at(11, 9, 9)))
        self.assertEqual(stored["md_new"]["recorded_since"], ms(at(11, 10, 9)))

    def test_what_isnt_valid_is_dropped(self):
        good = {
            "kind": "went_offline",
            "reason": "timeout",
            "cause": None,
            "first_at": ms(at(11, 10, 8)),
            "last_at": ms(at(11, 10, 8, 5)),
            "count": 2,
        }
        bad_entries = [
            "nonsense",
            None,
            {**good, "kind": "hub_started"},
            {**good, "kind": "rebooted"},
            {**good, "first_at": ms(at(11, 10, 9))},
            {**good, "count": 0},
            {**good, "count": True},
            {**good, "count": 1.5},
            {**good, "first_at": "yesterday"},
            {**good, "last_at": None},
            {**good, "first_at": -1},
            {**good, "last_at": 10**30},
            {**good, "reason": 3},
        ]
        data = {
            "hub_started": [good, {**good, "kind": "hub_started", "reason": "timeout"}],
            "records": {
                KEY: {
                    "recorded_since": ms(at(11, 9, 9)),
                    "last_seen": "a minute ago",
                    "entries": [good, *bad_entries],
                },
                OTHER: {"recorded_since": "long ago", "last_seen": ms(at(11, 10, 8)), "entries": [good]},
            },
        }
        changes = Changes()
        history = status_history.BNGntStatusHistory(lambda: TZ, RESTART_WINDOW, changes)
        history.load(data, [KEY, OTHER], at(11, 10, 9))
        stored = history.as_data()
        self.assertEqual(stored["records"][KEY]["entries"], [good])
        self.assertIsNone(stored["records"][KEY]["last_seen"])
        self.assertEqual(
            stored["records"][OTHER],
            {"recorded_since": ms(at(11, 10, 9)), "last_seen": None, "entries": []},
        )
        self.assertEqual(
            stored["hub_started"], [{**good, "kind": "hub_started", "reason": None}]
        )
        self.assertEqual(changes.count, 1)

    def test_anything_that_isnt_the_stores_shape_starts_afresh(self):
        for data in ("nonsense", [], 3, {"records": []}, {"hub_started": {}}):
            with self.subTest(data=data):
                history, _ = make(data=data, now=at(11, 10, 9))
                self.assertEqual(
                    history.as_data(),
                    {
                        "hub_started": [],
                        "records": {
                            KEY: {"recorded_since": ms(at(11, 10, 9)), "last_seen": None, "entries": []}
                        },
                    },
                )


class TimedStore:
    """Writes as status_history_store.py does: ``SAVE_DELAY`` after the history
    asks, on a timer that nothing moves."""

    def __init__(self, now):
        self.now = now
        self.due = None
        self.writes = []
        self.history = status_history.BNGntStatusHistory(lambda: TZ, RESTART_WINDOW, self._unsaved)
        self.history.load(None, [KEY], now)
        self.run_until(now + SAVE_DELAY)
        self.writes.clear()

    def _unsaved(self):
        if self.due is not None:
            raise AssertionError("asked for a save while one was due")
        self.due = self.now + SAVE_DELAY

    def run_until(self, when):
        while self.due is not None and self.due <= when:
            self.now, self.due = self.due, None
            self.writes.append((self.now, self.history.data_to_save()))
        self.now = when


class SavingTest(unittest.TestCase):
    def test_a_steady_stream_of_changes_is_saved_within_the_minute(self):
        start = at(11, 10, 9)
        store = TimedStore(start - SAVE_DELAY)
        # A device dropping off and coming back every 20 seconds for 5 minutes.
        # Each change, and the index of the write that holds it.
        changes = []
        for index in range(15):
            when = start + index * 20
            store.run_until(when)
            if index % 2 == 0:
                offline(store.history, when)
            else:
                online(store.history, when)
            changes.append((when, len(store.writes)))
        store.run_until(start + 15 * MINUTE)

        self.assertEqual(
            [when for when, _ in store.writes],
            [start + minutes * MINUTE for minutes in range(1, 6)],
        )
        for when, write in changes:
            self.assertLessEqual(store.writes[write][0] - when, SAVE_DELAY)
        self.assertEqual(store.writes[-1][1], store.history.as_data())
        self.assertFalse(store.history.has_unsaved_changes)

    def test_heartbeats_are_saved_once_a_minute_and_nothing_else_writes(self):
        start = at(11, 10, 9)
        store = TimedStore(start - SAVE_DELAY)
        for minute in range(3):
            when = start + minute * MINUTE + 5
            store.run_until(when)
            online(store.history, when, was_online=True, online_since=start)
        store.run_until(start + 30 * MINUTE)
        self.assertEqual(
            [(when, data["records"][KEY]["last_seen"]) for when, data in store.writes],
            [
                (start + 65, ms(start + 5)),
                (start + 125, ms(start + 65)),
                (start + 185, ms(start + 125)),
            ],
        )


class LateCauseTest(unittest.TestCase):
    """A state update, or a registration without a cause, brings the record
    back, and the connection's registration with its cause follows."""

    def came_online(self, history, when=None):
        return [
            (entry["cause"], entry["first_at"], entry["last_at"], entry["count"])
            for entry in day_entries(history.answer(KEY, when or at(11, 10, 12)), 0)
            if entry["kind"] == "came_online"
        ]

    def test_the_cause_goes_on_the_entry_just_recorded(self):
        # The history gets the same call from a state update as from a
        # registration without a cause.
        for first in ("state update", "registration without a cause"):
            with self.subTest(first=first):
                history, _ = make()
                back = at(11, 10, 9)
                offline(history, back - 5 * MINUTE)
                self.assertEqual(online(history, back), back)
                registered = back + 5
                result = online(
                    history, registered, cause="device_restarted", was_online=True, online_since=back
                )
                self.assertEqual(result, registered)
                self.assertEqual(self.came_online(history), [("device_restarted", ms(back), ms(back), 1)])

    def test_up_to_180_seconds_either_way(self):
        for gap, expected in (
            (180, [("zendo_restarted", 0, 0, 1)]),
            (-180, [("zendo_restarted", 0, 0, 1)]),
            (180.001, [("zendo_restarted", 180.001, 180.001, 1), (None, 0, 0, 1)]),
        ):
            with self.subTest(gap=gap):
                history, _ = make()
                back = at(11, 10, 9)
                online(history, back)
                online(history, back + gap, cause="zendo_restarted", was_online=True, online_since=back)
                self.assertEqual(
                    self.came_online(history),
                    [
                        (cause, ms(back + first), ms(back + last), count)
                        for cause, first, last, count in expected
                    ],
                )

    def test_a_merged_entry_without_a_cause_is_left_alone(self):
        history, _ = make()
        back = at(11, 10, 9)
        online(history, back - 2 * MINUTE)
        online(history, back)
        online(history, back + 5, cause="reconnected", was_online=True, online_since=back)
        self.assertEqual(
            self.came_online(history),
            [("reconnected", ms(back + 5), ms(back + 5), 1), (None, ms(back - 2 * MINUTE), ms(back), 2)],
        )

    def test_one_that_isnt_the_newest_entry_is_left_alone(self):
        history, _ = make()
        back = at(11, 10, 9)
        online(history, back)
        offline(history, back + 30)
        online(history, back + 60, cause="device_restarted")
        self.assertEqual(
            self.came_online(history),
            [("device_restarted", ms(back + 60), ms(back + 60), 1), (None, ms(back), ms(back), 1)],
        )

    def test_one_with_a_cause_keeps_it(self):
        history, _ = make()
        back = at(11, 10, 9)
        online(history, back, cause="device_restarted")
        online(history, back + 5, cause="reconnected", was_online=True, online_since=back)
        self.assertEqual(
            self.came_online(history),
            [("reconnected", ms(back + 5), ms(back + 5), 1), ("device_restarted", ms(back), ms(back), 1)],
        )

    def test_it_asks_for_a_save(self):
        history, changes = make()
        back = at(11, 10, 9)
        online(history, back)
        saved(history, changes)
        online(history, back + 5, cause="reconnected", was_online=True, online_since=back)
        self.assertEqual(changes.count, 1)
        self.assertEqual(history.as_data()["records"][KEY]["entries"][0]["cause"], "reconnected")


if __name__ == "__main__":
    unittest.main()
