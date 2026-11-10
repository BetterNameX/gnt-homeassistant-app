"""Tests for custom_components/zendo/reported_codes.py: the book the admin code
store and the Device Lock PIN store keep their entries in, and the rule a code
and a PIN follow.

Run from the repository root:

    python3 -m unittest discover -s tests -v

Needs nothing from Home Assistant: the module is loaded from its file, not as
part of the integration's package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "custom_components" / "zendo" / "reported_codes.py"

_spec = importlib.util.spec_from_file_location("bngnt_reported_codes", MODULE_PATH)
reported_codes = importlib.util.module_from_spec(_spec)
sys.modules["bngnt_reported_codes"] = reported_codes
_spec.loader.exec_module(reported_codes)

# The field each store names its code with: the admin code store's and the
# Device Lock PIN store's.
FIELDS = ("code", "pin")

AT = "2026-11-10T09:30:00.123456+00:00"
LATER = "2026-11-10T09:45:00.000000+00:00"


class IsValidCodeTest(unittest.TestCase):
    def test_four_six_or_eight_ascii_digits(self):
        for value in ("0000", "1234", "012345", "999999", "00000000", "98765432"):
            with self.subTest(value):
                self.assertTrue(reported_codes.is_valid_code(value))

    def test_any_other_length(self):
        for value in ("", "1", "123", "12345", "1234567", "123456789", "1234567890"):
            with self.subTest(value):
                self.assertFalse(reported_codes.is_valid_code(value))

    def test_digits_that_arent_ascii(self):
        # Arabic-Indic, superscript and fullwidth digits all pass str.isdigit().
        for value in ("١٢٣٤", "²³⁴⁵", "１２３４", "12３4"):
            with self.subTest(value):
                self.assertTrue(value.isdigit())
                self.assertFalse(reported_codes.is_valid_code(value))

    def test_anything_around_the_digits(self):
        for value in (" 1234", "1234 ", "12 34", "1234\n", "+1234", "-123", "12.4", "abcd"):
            with self.subTest(repr(value)):
                self.assertFalse(reported_codes.is_valid_code(value))

    def test_values_that_arent_text(self):
        for value in (None, 1234, 123456, 0, True, 12.34, ["1234"], {"pin": "1234"}, b"1234"):
            with self.subTest(repr(value)):
                self.assertFalse(reported_codes.is_valid_code(value))


class StatusesTest(unittest.TestCase):
    def test_the_three_statuses(self):
        self.assertEqual(reported_codes.CODE_STATUSES, ("set", "not_set", "unknown"))
        self.assertEqual(reported_codes.STATUS_SET, "set")
        self.assertEqual(reported_codes.STATUS_NOT_SET, "not_set")
        self.assertEqual(reported_codes.STATUS_UNKNOWN, "unknown")


class ReportedCodeBookTest(unittest.TestCase):
    """Each case runs for both stores' fields."""

    def _book(self, field):
        return reported_codes.ReportedCodeBook(field)

    def test_no_entry_is_unknown(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                self.assertIsNone(book.get("md_1"))
                self.assertEqual(book.status("md_1"), "unknown")
                self.assertEqual(len(book), 0)

    def test_a_report_keeps_the_code_and_when(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                book.report("md_1", "246810", AT)
                self.assertEqual(book.get("md_1"), {field: "246810", "reported_at": AT})
                self.assertEqual(book.status("md_1"), "set")

    def test_a_report_of_none_is_not_set(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                book.report("md_1", None, AT)
                self.assertEqual(book.get("md_1"), {field: None, "reported_at": AT})
                self.assertEqual(book.status("md_1"), "not_set")

    def test_a_later_report_replaces_the_entry(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                book.report("md_1", "2468", AT)
                book.report("md_1", "2468", LATER)
                self.assertEqual(book.get("md_1"), {field: "2468", "reported_at": LATER})
                book.report("md_1", None, LATER)
                self.assertEqual(book.status("md_1"), "not_set")
                book.report("md_1", "13579135", LATER)
                self.assertEqual(book.get("md_1")[field], "13579135")

    def test_each_record_has_its_own_entry(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                book.report("md_1", "2468", AT)
                book.report("md_2", None, AT)
                self.assertEqual(book.status("md_1"), "set")
                self.assertEqual(book.status("md_2"), "not_set")
                self.assertEqual(book.status("md_3"), "unknown")
                self.assertEqual(len(book), 2)

    def test_remove(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                book.report("md_1", "2468", AT)
                self.assertTrue(book.remove("md_1"))
                self.assertEqual(book.status("md_1"), "unknown")
                self.assertFalse(book.remove("md_1"))
                self.assertFalse(book.remove("md_never"))

    def test_round_trip(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                book.report("md_1", "246810", AT)
                book.report("md_2", None, LATER)
                data = json.loads(json.dumps(book.to_data()))
                self.assertEqual(
                    data,
                    {
                        "md_1": {field: "246810", "reported_at": AT},
                        "md_2": {field: None, "reported_at": LATER},
                    },
                )
                restored, dropped = reported_codes.ReportedCodeBook.from_data(
                    field, data, ["md_1", "md_2"]
                )
                self.assertFalse(dropped)
                self.assertEqual(restored.to_data(), data)

    def test_the_data_is_a_copy(self):
        for field in FIELDS:
            with self.subTest(field):
                book = self._book(field)
                book.report("md_1", "2468", AT)
                data = book.to_data()
                data["md_1"][field] = "1357"
                data["md_2"] = {field: "1357", "reported_at": AT}
                self.assertEqual(book.get("md_1")[field], "2468")
                self.assertIsNone(book.get("md_2"))

    def test_loading_drops_the_entries_of_records_that_are_gone(self):
        for field in FIELDS:
            with self.subTest(field):
                data = {
                    "md_1": {field: "2468", "reported_at": AT},
                    "md_gone": {field: "1357", "reported_at": AT},
                }
                book, dropped = reported_codes.ReportedCodeBook.from_data(field, data, ["md_1"])
                self.assertTrue(dropped)
                self.assertEqual(book.to_data(), {"md_1": {field: "2468", "reported_at": AT}})

    def test_loading_drops_entries_that_arent_valid(self):
        for field in FIELDS:
            with self.subTest(field):
                invalid = {
                    "not_an_object": "2468",
                    "list": [field, "2468"],
                    "bad_code": {field: "12345", "reported_at": AT},
                    "code_not_text": {field: 2468, "reported_at": AT},
                    "digits_not_ascii": {field: "١٢٣٤", "reported_at": AT},
                    "no_reported_at": {field: "2468"},
                    "reported_at_not_text": {field: "2468", "reported_at": 1793232000},
                }
                data = {"md_ok": {field: None, "reported_at": AT}, **invalid}
                book, dropped = reported_codes.ReportedCodeBook.from_data(
                    field, data, list(data)
                )
                self.assertTrue(dropped)
                self.assertEqual(book.to_data(), {"md_ok": {field: None, "reported_at": AT}})

    def test_an_entry_without_the_books_field_reads_as_none(self):
        # An entry is read by its own store's field: the other store's field
        # isn't a code, and a missing field reads as none.
        for field, other in (("code", "pin"), ("pin", "code")):
            with self.subTest(field):
                data = {"md_1": {other: "2468", "reported_at": AT}}
                book, dropped = reported_codes.ReportedCodeBook.from_data(field, data, ["md_1"])
                self.assertFalse(dropped)
                self.assertEqual(book.get("md_1"), {field: None, "reported_at": AT})
                self.assertEqual(book.status("md_1"), "not_set")

    def test_extra_keys_in_an_entry_arent_kept(self):
        for field in FIELDS:
            with self.subTest(field):
                data = {"md_1": {field: "2468", "reported_at": AT, "extra": "x"}}
                book, dropped = reported_codes.ReportedCodeBook.from_data(field, data, ["md_1"])
                self.assertFalse(dropped)
                self.assertEqual(book.to_data(), {"md_1": {field: "2468", "reported_at": AT}})

    def test_data_that_isnt_an_object_reads_as_no_entries(self):
        for field in FIELDS:
            for data in (None, [], "text", 12):
                with self.subTest(field=field, data=data):
                    book, dropped = reported_codes.ReportedCodeBook.from_data(
                        field, data, ["md_1"]
                    )
                    self.assertFalse(dropped)
                    self.assertEqual(len(book), 0)

    def test_no_change_on_load_needs_no_save(self):
        for field in FIELDS:
            with self.subTest(field):
                _, dropped = reported_codes.ReportedCodeBook.from_data(field, {}, ["md_1"])
                self.assertFalse(dropped)


if __name__ == "__main__":
    unittest.main()
