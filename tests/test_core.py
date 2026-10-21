"""haruapp.core: 날짜 변환, 일정 기간, 반복 규칙, 드래그·배치 계산."""
import unittest
from datetime import date, datetime, timedelta

from haruapp.core import (
    WEEKDAY_CODES, Event, Task, assign_lanes, build_rule, drag_range, fmt_day, ical_to_py, overlap_columns,
    parse_rule, py_to_ical, reminder_index, repeat_index, rule_start_problem, rule_until, set_reminder,
    stretched_times, task_label,
)


def event(start, end, all_day=None):
    if all_day is None:
        all_day = not isinstance(start, datetime)
    return Event(uid="u", rid="", source_uid="s", summary="t", location="", start=start, end=end,
                 all_day=all_day, icomp=None, recurring=False)


class TimeConversion(unittest.TestCase):
    def test_date_round_trip(self):
        t = py_to_ical(date(2026, 10, 21))
        self.assertTrue(t.is_date())
        self.assertEqual(ical_to_py(t), date(2026, 10, 21))

    def test_datetime_round_trip(self):
        dt = datetime(2026, 10, 21, 14, 30)
        self.assertEqual(ical_to_py(py_to_ical(dt)), dt)

    def test_null(self):
        self.assertIsNone(ical_to_py(None))

    def test_fmt_day_shows_year_only_when_different(self):
        this_year = date.today().year
        self.assertNotIn("년", fmt_day(date(this_year, 3, 1)))
        self.assertIn(f"{this_year - 1}년", fmt_day(date(this_year - 1, 3, 1)))


class EventDays(unittest.TestCase):
    def test_all_day_end_is_exclusive(self):
        e = event(date(2026, 10, 20), date(2026, 10, 23))
        self.assertEqual(e.days(), (date(2026, 10, 20), date(2026, 10, 22)))
        self.assertTrue(e.covers(date(2026, 10, 22)))
        self.assertFalse(e.covers(date(2026, 10, 23)))

    def test_ending_at_midnight_is_one_day(self):
        e = event(datetime(2026, 10, 20, 22), datetime(2026, 10, 21, 0))
        self.assertEqual(e.days(), (date(2026, 10, 20), date(2026, 10, 20)))

    def test_past_midnight_is_two_days(self):
        e = event(datetime(2026, 10, 20, 23), datetime(2026, 10, 21, 1))
        self.assertEqual(e.days(), (date(2026, 10, 20), date(2026, 10, 21)))

    def test_zero_length_all_day(self):
        e = event(date(2026, 10, 20), date(2026, 10, 20))
        self.assertEqual(e.days(), (date(2026, 10, 20), date(2026, 10, 20)))


class Rules(unittest.TestCase):
    WED = date(2026, 10, 21)

    def test_parse(self):
        self.assertEqual(parse_rule("FREQ=WEEKLY;BYDAY=MO,WE;INTERVAL=2"),
                         {"FREQ": "WEEKLY", "BYDAY": "MO,WE", "INTERVAL": "2"})
        self.assertEqual(parse_rule(""), {})

    def test_presets_round_trip(self):
        for idx in range(1, 7):
            with self.subTest(idx=idx):
                self.assertEqual(repeat_index(parse_rule(build_rule(idx, self.WED, None)), self.WED), idx)

    def test_no_repeat(self):
        self.assertEqual(build_rule(0, self.WED, None), "")
        self.assertEqual(repeat_index({}, self.WED), 0)

    def test_rules_the_presets_cannot_express(self):
        cases = {
            "매월 마지막 날": ("FREQ=MONTHLY;BYMONTHDAY=-1", date(2026, 10, 31)),
            "매월 1일·15일": ("FREQ=MONTHLY;BYMONTHDAY=1,15", date(2026, 10, 1)),
            "시작과 다른 요일": ("FREQ=WEEKLY;BYDAY=TU", self.WED),
            "횟수 제한": ("FREQ=DAILY;COUNT=5", self.WED),
            "둘째 화요일": ("FREQ=MONTHLY;BYDAY=2TU", self.WED),
            "3주마다": ("FREQ=WEEKLY;INTERVAL=3", self.WED),
        }
        for name, (rule, start) in cases.items():
            with self.subTest(name):
                self.assertIsNone(repeat_index(parse_rule(rule), start))

    def test_weekly_uses_start_weekday(self):
        rule = build_rule(3, self.WED, None)
        self.assertEqual(parse_rule(rule)["BYDAY"], WEEKDAY_CODES[self.WED.weekday()])

    def test_until_for_timed_is_utc(self):
        rule = build_rule(1, datetime(2026, 10, 21, 9), date(2026, 10, 31))
        until = parse_rule(rule)["UNTIL"]
        self.assertTrue(until.endswith("Z"))
        self.assertEqual(rule_until(parse_rule(rule)), date(2026, 10, 31))

    def test_weekday_rule_must_start_on_a_weekday(self):
        self.assertIsNotNone(rule_start_problem(2, date(2026, 10, 24)))  # 토요일
        self.assertIsNotNone(rule_start_problem(2, datetime(2026, 10, 25, 9)))  # 일요일
        self.assertIsNone(rule_start_problem(2, self.WED))
        self.assertIsNone(rule_start_problem(3, date(2026, 10, 24)))  # 매주는 토요일 시작도 괜찮다

    def test_until_for_all_day_is_date(self):
        rule = build_rule(1, date(2026, 10, 21), date(2026, 10, 31))
        self.assertEqual(parse_rule(rule)["UNTIL"], "20261031")
        self.assertEqual(rule_until(parse_rule(rule)), date(2026, 10, 31))


class TaskLabel(unittest.TestCase):
    def test_marks(self):
        t = Task(uid="u", source_uid="s", summary="수학", due=date(2026, 10, 21), done=False, icomp=None)
        self.assertEqual(task_label(t), "○ 수학")
        self.assertEqual(task_label(Task(**{**t.__dict__, "done": True})), "✓ 수학")


class Reminders(unittest.TestCase):
    def test_round_trip(self):
        from gi.repository import ECal
        for idx in (0, 1, 3, 6):
            with self.subTest(idx=idx):
                comp = ECal.Component.new()
                comp.set_new_vtype(ECal.ComponentVType.EVENT)
                set_reminder(comp, idx)
                self.assertEqual(reminder_index(comp), idx)


class DragRange(unittest.TestCase):
    D = date(2026, 10, 1)

    def d(self, n):
        return self.D + timedelta(days=n)

    def test_move_keeps_length(self):
        self.assertEqual(drag_range("move", (self.d(20), self.d(22)), self.d(21), self.d(25)), (self.d(24), self.d(26)))

    def test_end_edge_shrinks_and_grows(self):
        span = (self.d(20), self.d(23))
        self.assertEqual(drag_range("end", span, self.d(23), self.d(21)), (self.d(20), self.d(21)))
        self.assertEqual(drag_range("end", span, self.d(23), self.d(27)), (self.d(20), self.d(27)))

    def test_end_edge_past_start_flips(self):
        self.assertEqual(drag_range("end", (self.d(20), self.d(23)), self.d(23), self.d(18)), (self.d(18), self.d(20)))

    def test_start_edge(self):
        span = (self.d(20), self.d(23))
        self.assertEqual(drag_range("start", span, self.d(20), self.d(22)), (self.d(22), self.d(23)))
        self.assertEqual(drag_range("start", span, self.d(20), self.d(25)), (self.d(23), self.d(25)))

    def test_new_event_from_empty_cells(self):
        self.assertEqual(drag_range(None, None, self.d(8), self.d(6)), (self.d(6), self.d(8)))


class StretchedTimes(unittest.TestCase):
    def test_all_day(self):
        e = event(date(2026, 10, 20), date(2026, 10, 21))
        self.assertEqual(stretched_times(e, date(2026, 10, 20), date(2026, 10, 22)),
                         (date(2026, 10, 20), date(2026, 10, 23)))

    def test_timed_keeps_times(self):
        e = event(datetime(2026, 10, 20, 10), datetime(2026, 10, 20, 11, 30))
        self.assertEqual(stretched_times(e, date(2026, 10, 22), date(2026, 10, 23)),
                         (datetime(2026, 10, 22, 10), datetime(2026, 10, 23, 11, 30)))

    def test_midnight_end_stays_at_midnight(self):
        e = event(datetime(2026, 10, 20, 22), datetime(2026, 10, 21, 0))
        self.assertEqual(stretched_times(e, date(2026, 10, 20), date(2026, 10, 22)),
                         (datetime(2026, 10, 20, 22), datetime(2026, 10, 23, 0)))


class Layout(unittest.TestCase):
    def t(self, h, m=0):
        return datetime(2026, 10, 21, h, m)

    def test_overlapping_side_by_side(self):
        out = overlap_columns([(self.t(10), self.t(11, 30), "a"), (self.t(10, 30), self.t(12), "b")])
        self.assertEqual({v: (col, n) for _, _, v, col, n in out}, {"a": (0, 2), "b": (1, 2)})

    def test_separate_events_use_full_width(self):
        out = overlap_columns([(self.t(9), self.t(10), "a"), (self.t(10), self.t(11), "b")])
        self.assertEqual({v: (col, n) for _, _, v, col, n in out}, {"a": (0, 1), "b": (0, 1)})

    def test_column_is_reused(self):
        out = overlap_columns([(self.t(9), self.t(12), "long"), (self.t(9), self.t(10), "x"),
                               (self.t(10), self.t(11), "y")])
        cols = {v: (col, n) for _, _, v, col, n in out}
        self.assertEqual(cols["long"], (0, 2))
        self.assertEqual(cols["x"][0], cols["y"][0])

    def test_lanes(self):
        sun = date(2026, 10, 18)
        lanes = assign_lanes([(date(2026, 10, 19), date(2026, 10, 21)), (date(2026, 10, 20), date(2026, 10, 20)),
                              (date(2026, 10, 22), date(2026, 10, 30))], sun)
        self.assertEqual([lane for lane, _ in lanes], [0, 1, 0])
        self.assertEqual(list(lanes[2][1]), [4, 5, 6])  # 다음 주로 넘어가는 부분은 잘린다


if __name__ == "__main__":
    unittest.main()
