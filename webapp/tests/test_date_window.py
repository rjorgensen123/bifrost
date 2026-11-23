# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for queries.date_window: two whole dates become a time window whose day boundaries
are local midnight in the installation's time zone. Needs no database.

The expected values are written as UTC instants, so they do not depend on the conversion under test.

  cd webapp && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import queries   # noqa: E402

OSLO = "Europe/Oslo"
HOUR = 3600


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


NOW = utc(2026, 11, 23, 12)     # a Monday at 13:00 in Oslo (UTC+1)


class WholeDays(unittest.TestCase):
    def test_one_day(self):
        self.assertEqual(queries.date_window("2026-11-10", "2026-11-10", OSLO, NOW),
                         (utc(2026, 11, 9, 23), utc(2026, 11, 10, 23)))

    def test_several_days_include_the_last_day(self):
        frm, to = queries.date_window("2026-06-01", "2026-06-07", OSLO, NOW)
        self.assertEqual((frm, to), (utc(2026, 5, 31, 22), utc(2026, 6, 7, 22)))
        self.assertEqual(to - frm, 7 * 24 * HOUR)

    def test_missing_end_date_means_the_single_day(self):
        one_day = (utc(2026, 11, 9, 23), utc(2026, 11, 10, 23))
        self.assertEqual(queries.date_window("2026-11-10", None, OSLO, NOW), one_day)
        self.assertEqual(queries.date_window("2026-11-10", "", OSLO, NOW), one_day)

    def test_window_is_a_pair_of_numbers(self):
        frm, to = queries.date_window("2026-11-10", None, OSLO, NOW)
        self.assertIsInstance(frm, float)
        self.assertIsInstance(to, float)


class DaylightSaving(unittest.TestCase):
    """The window follows local midnight, so the day the clocks change is not 24 hours long."""

    def test_day_the_clocks_go_forward_is_23_hours(self):
        frm, to = queries.date_window("2026-03-29", None, OSLO, NOW)
        self.assertEqual((frm, to), (utc(2026, 3, 28, 23), utc(2026, 3, 29, 22)))
        self.assertEqual(to - frm, 23 * HOUR)

    def test_day_the_clocks_go_back_is_25_hours(self):
        frm, to = queries.date_window("2026-10-25", None, OSLO, NOW)
        self.assertEqual((frm, to), (utc(2026, 10, 24, 22), utc(2026, 10, 25, 23)))
        self.assertEqual(to - frm, 25 * HOUR)

    def test_period_across_a_change_starts_and_ends_at_local_midnight(self):
        self.assertEqual(queries.date_window("2026-03-28", "2026-03-30", OSLO, NOW),
                         (utc(2026, 3, 27, 23), utc(2026, 3, 30, 22)))

    def test_day_without_a_local_midnight_starts_when_the_day_starts(self):
        # Sao Paulo, 2018-11-04: the clocks went from 00:00 straight to 01:00.
        frm, to = queries.date_window("2018-11-04", None, "America/Sao_Paulo", NOW)
        self.assertEqual((frm, to), (utc(2018, 11, 4, 3), utc(2018, 11, 5, 2)))
        self.assertEqual(to - frm, 23 * HOUR)


class EndInTheFuture(unittest.TestCase):
    def test_today_ends_now(self):
        self.assertEqual(queries.date_window("2026-11-23", None, OSLO, NOW),
                         (utc(2026, 11, 22, 23), NOW))

    def test_period_past_today_ends_now(self):
        self.assertEqual(queries.date_window("2026-11-20", "2026-11-30", OSLO, NOW),
                         (utc(2026, 11, 19, 23), NOW))

    def test_last_possible_date_as_end_ends_now(self):
        self.assertEqual(queries.date_window("2026-11-20", "9999-12-31", OSLO, NOW),
                         (utc(2026, 11, 19, 23), NOW))

    def test_period_that_ended_is_not_cut(self):
        self.assertEqual(queries.date_window("2026-11-22", None, OSLO, NOW),
                         (utc(2026, 11, 21, 23), utc(2026, 11, 22, 23)))


class BadPeriod(unittest.TestCase):
    def test_end_before_start(self):
        with self.assertRaises(queries.BadPeriod):
            queries.date_window("2026-11-10", "2026-11-09", OSLO, NOW)

    def test_period_entirely_in_the_future(self):
        for d_from, d_to in (("2026-11-24", None), ("2026-11-24", "2026-11-30"),
                             ("2027-01-01", "2027-12-31"), ("9999-12-31", None)):
            with self.subTest(d_from=d_from, d_to=d_to):
                with self.assertRaises(queries.BadPeriod):
                    queries.date_window(d_from, d_to, OSLO, NOW)

    def test_today_just_begun_gets_a_minute(self):
        midnight = utc(2026, 11, 22, 23)
        for seconds in (0, 1, 59, 60):
            with self.subTest(seconds=seconds):
                self.assertEqual(queries.date_window("2026-11-23", None, OSLO, midnight + seconds),
                                 (midnight, midnight + 60))
        self.assertEqual(queries.date_window("2026-11-23", None, OSLO, midnight + 61),
                         (midnight, midnight + 61))

    def test_is_not_a_bad_date(self):
        with self.assertRaises(queries.BadPeriod) as caught:
            queries.date_window("2026-11-10", "2026-11-09", OSLO, NOW)
        self.assertNotIsInstance(caught.exception, queries.BadDate)


class BadDate(unittest.TestCase):
    GARBAGE = ("abc", "2026-13-01", "2026-02-30", "2026-6-1", "20260610", "2026-W24-3",
               "10.06.2026", "06/10/2026", "2026-06-10T00:00", "2026-06-10 ", " 2026-06-10",
               "2026-06-10\n", "1781049600", "\u0662\u0660\u0662\u0666-\u0660\u0666-\u0661\u0660", "'; DROP TABLE host; --")

    def test_garbage_start_date(self):
        for text in self.GARBAGE + ("", None):
            with self.subTest(d_from=text):
                with self.assertRaises(queries.BadDate):
                    queries.date_window(text, "2026-06-10", OSLO, NOW)

    def test_garbage_end_date(self):
        for text in self.GARBAGE:
            with self.subTest(d_to=text):
                with self.assertRaises(queries.BadDate):
                    queries.date_window("2026-06-10", text, OSLO, NOW)

    def test_garbage_is_reported_before_the_period_is_judged(self):
        # A start date in the future and an unreadable end date: the date is what is wrong.
        with self.assertRaises(queries.BadDate):
            queries.date_window("2027-06-10", "soon", OSLO, NOW)

    def test_is_not_a_bad_period(self):
        with self.assertRaises(queries.BadDate) as caught:
            queries.date_window("abc", None, OSLO, NOW)
        self.assertNotIsInstance(caught.exception, queries.BadPeriod)


class OtherTimeZones(unittest.TestCase):
    def test_day_boundaries_follow_the_zone(self):
        self.assertEqual(queries.date_window("2026-06-10", None, "America/New_York", NOW),
                         (utc(2026, 6, 10, 4), utc(2026, 6, 11, 4)))
        self.assertEqual(queries.date_window("2026-06-10", None, "Asia/Tokyo", NOW),
                         (utc(2026, 6, 9, 15), utc(2026, 6, 10, 15)))
        self.assertEqual(queries.date_window("2026-06-10", None, "UTC", NOW),
                         (utc(2026, 6, 10), utc(2026, 6, 11)))

    def test_today_is_the_zone_s_today(self):
        # 01:00 UTC on 10 June: already the 10th in Tokyo and Oslo, still the 9th in New York.
        now = utc(2026, 6, 10, 1)
        self.assertEqual(queries.date_window("2026-06-10", None, "Asia/Tokyo", now),
                         (utc(2026, 6, 9, 15), now))
        self.assertEqual(queries.date_window("2026-06-10", None, OSLO, now),
                         (utc(2026, 6, 9, 22), now))
        with self.assertRaises(queries.BadPeriod):
            queries.date_window("2026-06-10", None, "America/New_York", now)
        self.assertEqual(queries.date_window("2026-06-09", "2026-06-10", "America/New_York", now),
                         (utc(2026, 6, 9, 4), now))


if __name__ == "__main__":
    unittest.main()
