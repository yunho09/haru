"""haruapp.store: 실제 Evolution Data Server 로컬 캘린더에서 쓰기·되돌리기.

데스크톱 세션(EDS)이 필요해서 기본으로는 건너뛴다:
    HARU_EDS_TESTS=1 /usr/bin/python3 -m unittest tests.test_store_eds
로컬 '개인' 캘린더(system-calendar)에 haru-test- 로 시작하는 일정을 만들었다가 지운다. 구글 캘린더는 건드리지 않는다.
"""
import os
import unittest
import uuid
from datetime import date, datetime, timedelta

if os.environ.get("HARU_EDS_TESTS") != "1":
    raise unittest.SkipTest("HARU_EDS_TESTS=1 일 때만 (실제 EDS 필요)")

import warnings  # noqa: E402

from gi.repository import ECal, GLib, ICalGLib  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning, module="gi.events")  # PyGObject 내부 경고

from haruapp.core import comp_dt  # noqa: E402
from haruapp.store import Store  # noqa: E402

CAL = "system-calendar"
RANGE = (date(2026, 9, 1), date(2026, 12, 1))


class StoreUndo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(lambda: None)
        cls.records = []
        cls.store.on_undoable = lambda label, summary, entries: cls.records.append(entries)
        cls.wait_until(lambda: CAL in cls.store.clients)

    @staticmethod
    def wait_until(cond, timeout=10):
        loop = GLib.MainLoop()
        deadline = GLib.get_monotonic_time() + timeout * 1_000_000

        def check():
            if cond() or GLib.get_monotonic_time() > deadline:
                loop.quit()
                return False
            return True

        GLib.timeout_add(50, check)
        loop.run()
        if not cond():
            raise AssertionError("시간 초과")

    def run_op(self, fn, *args, **kw):
        """비동기 쓰기 하나를 끝날 때까지 기다리고 오류를 돌려준다."""
        box = []
        fn(*args, done=lambda err: box.append(err), **kw)
        self.wait_until(lambda: box)
        return box[0]

    def setUp(self):
        self.uid = f"haru-test-{uuid.uuid4()}"
        self.records.clear()

    def tearDown(self):
        for e in self.store.events(*RANGE):
            if e.uid.startswith("haru-test-") or e.summary.startswith("haru-test"):
                self.run_op(self.store.remove, CAL, e.uid, None, ECal.ObjModType.ALL, record=False)

    def make(self, start, end, rule=None, uid=None):
        comp = ECal.Component.new()
        comp.set_new_vtype(ECal.ComponentVType.EVENT)
        comp.set_uid(uid or self.uid)
        comp.set_summary(ECal.ComponentText.new("haru-test", None))
        comp.set_dtstart(comp_dt(start))
        comp.set_dtend(comp_dt(end))
        if rule:
            comp.set_rrules([ICalGLib.Recurrence.new_from_string(rule)])
        comp.commit_sequence()
        self.assertIsNone(self.run_op(self.store.create, CAL, comp, record=False))
        return comp

    def times(self, uid=None):
        return [(e.start, e.end) for e in self.store.events(*RANGE) if e.uid == (uid or self.uid)]

    def instance(self, start):
        return next(e for e in self.store.events(*RANGE) if e.uid == self.uid and e.start == start)

    def test_modify_then_restore(self):
        self.make(datetime(2026, 10, 21, 14), datetime(2026, 10, 21, 15))
        ev = self.instance(datetime(2026, 10, 21, 14))
        comp = ECal.Component.new_from_icalcomponent(ev.icomp.clone())
        comp.set_dtstart(comp_dt(datetime(2026, 10, 23, 16)))
        comp.set_dtend(comp_dt(datetime(2026, 10, 23, 17)))
        comp.commit_sequence()
        self.assertIsNone(self.run_op(self.store.modify, CAL, comp, ECal.ObjModType.ALL))
        self.assertEqual(self.times(), [(datetime(2026, 10, 23, 16), datetime(2026, 10, 23, 17))])
        self.assertIsNone(self.run_op(self.store.restore, self.records[-1]))
        self.assertEqual(self.times(), [(datetime(2026, 10, 21, 14), datetime(2026, 10, 21, 15))])

    def test_remove_then_restore(self):
        self.make(date(2026, 10, 20), date(2026, 10, 22))
        self.assertIsNone(self.run_op(self.store.remove, CAL, self.uid, None, ECal.ObjModType.ALL))
        self.assertEqual(self.times(), [])
        self.assertIsNone(self.run_op(self.store.restore, self.records[-1]))
        self.assertEqual(self.times(), [(date(2026, 10, 20), date(2026, 10, 22))])

    def test_create_then_restore_removes_it(self):
        comp = ECal.Component.new()
        comp.set_new_vtype(ECal.ComponentVType.EVENT)
        comp.set_uid(self.uid)
        comp.set_summary(ECal.ComponentText.new("haru-test", None))
        comp.set_dtstart(comp_dt(date(2026, 10, 25)))
        comp.set_dtend(comp_dt(date(2026, 10, 26)))
        comp.commit_sequence()
        self.assertIsNone(self.run_op(self.store.create, CAL, comp))
        self.assertIsNone(self.run_op(self.store.restore, self.records[-1]))
        self.assertEqual(self.times(), [])

    def test_detached_occurrence_restore_keeps_all_occurrences(self):
        self.make(datetime(2026, 10, 19, 9), datetime(2026, 10, 19, 10), rule="FREQ=DAILY;COUNT=3")
        ev = self.instance(datetime(2026, 10, 20, 9))
        comp = ECal.Component.new_from_icalcomponent(ev.icomp.clone())
        for clear in (comp.set_rrules, comp.set_rdates, comp.set_exrules, comp.set_exdates):
            clear(None)
        comp.set_dtstart(comp_dt(datetime(2026, 10, 20, 13)))
        comp.set_dtend(comp_dt(datetime(2026, 10, 20, 14)))
        comp.commit_sequence()
        self.assertIsNone(self.run_op(self.store.modify, CAL, comp, ECal.ObjModType.THIS))
        self.assertIn((datetime(2026, 10, 20, 13), datetime(2026, 10, 20, 14)), self.times())
        self.assertIsNone(self.run_op(self.store.restore, self.records[-1]))
        self.assertEqual(sorted(s for s, _ in self.times()),
                         [datetime(2026, 10, d, 9) for d in (19, 20, 21)])

    def test_move_then_restore(self):
        comp = self.make(date(2026, 10, 27), date(2026, 10, 28))
        self.assertIsNone(self.run_op(self.store.move, CAL, CAL, comp))  # 같은 캘린더로 옮기기 = uid 가 바뀐다
        moved = [e.uid for e in self.store.events(*RANGE) if e.summary == "haru-test"]
        self.assertEqual(len(moved), 1)
        self.assertNotEqual(moved[0], self.uid)
        self.assertIsNone(self.run_op(self.store.restore, self.records[-1]))
        self.assertEqual([e.uid for e in self.store.events(*RANGE) if e.summary == "haru-test"], [self.uid])

    def test_busy_counter(self):
        box = []
        comp = ECal.Component.new()
        comp.set_new_vtype(ECal.ComponentVType.EVENT)
        comp.set_uid(self.uid)
        comp.set_summary(ECal.ComponentText.new("haru-test", None))
        comp.set_dtstart(comp_dt(date(2026, 10, 29)))
        comp.set_dtend(comp_dt(date(2026, 10, 29) + timedelta(days=1)))
        comp.commit_sequence()
        self.store.create(CAL, comp, lambda err: box.append(err), record=False)
        self.assertEqual(self.store.busy, 1)
        self.wait_until(lambda: box)
        self.assertEqual(self.store.busy, 0)


if __name__ == "__main__":
    unittest.main()
