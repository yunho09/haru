"""haruapp.autoupload: 올릴 날 고르기, 메모 있을 때만 올리기, 놓친 날, 캘린더 연결 기다리기, 오프라인 재시도
(+ diary.day_items)."""
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from gi.repository import GLib

from haruapp import autoupload, diary
from haruapp.core import Event, Task
from tests.test_diary import FakeNotion

DAY = date(2026, 10, 23)


def task(summary, due, done=False, completed=None):
    return Task(uid=summary, source_uid="tasks", summary=summary, due=due, done=done, icomp=None, completed=completed)


def event(summary, start, end, source="cal", location=""):
    return Event(uid=summary, rid="", source_uid=source, summary=summary, location=location, start=start, end=end,
                 all_day=not isinstance(start, datetime), icomp=None, recurring=False)


class TargetDay(unittest.TestCase):
    def test_on_time(self):  # 새벽 2시에 돌면 전날(2시~2시) 것
        self.assertEqual(autoupload.target_day(datetime(2026, 10, 24, 2, 0, 0)), DAY)

    def test_late_run_uploads_the_missed_day(self):  # 늦게 돌거나, 아침에 절전에서 깨어 돌 때
        for now in (datetime(2026, 10, 24, 2, 0, 30), datetime(2026, 10, 24, 8, 0), datetime(2026, 10, 25, 1, 59)):
            self.assertEqual(autoupload.target_day(now), DAY)

    def test_before_time_is_the_day_before(self):  # 2시 전이면 그날은 아직 안 끝났다
        self.assertEqual(autoupload.target_day(datetime(2026, 10, 24, 1, 59, 59)), date(2026, 10, 22))


class DayItems(unittest.TestCase):
    def test_todos_and_events(self):
        tasks = [task("수학", DAY, True), task("국어", DAY), task("어제 마감", date(2026, 10, 22), True, DAY),
                 task("내일 마감", date(2026, 10, 24)), task("어제 끝냄", None, True, date(2026, 10, 22))]
        events = [event("수행평가", DAY, date(2026, 10, 24)),
                  event("회의", datetime(2026, 10, 23, 9), datetime(2026, 10, 23, 10), location="교실"),
                  event("공휴일", DAY, date(2026, 10, 24), source="holiday"),
                  event("어제 일정", date(2026, 10, 22), DAY)]
        todos, lines = diary.day_items(DAY, tasks, events, lambda uid: uid == "holiday")
        self.assertEqual(todos, [("수학", True), ("국어", False), ("어제 마감", True)])
        self.assertEqual(lines, ["하루 종일  수행평가", "09:00–10:00  회의  @교실"])


class FakeStore:
    """Store 대신: not_connected 만 흉내 (할 일·일정은 collect 를 바꿔치기)."""

    def __init__(self, missing=()):
        self.missing = list(missing)

    def not_connected(self):
        return self.missing

    def name(self, uid):
        return {"g": "구글 할 일", "h": "대한민국의 휴일"}.get(uid, uid)

    def skip_in_diary(self, uid):
        return uid == "h"


class Run(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 2, 0, 1)

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.store = FakeStore()
        self.mocks = {}
        for target, name, value in (
            (diary, "UPLOADED", tmp / "uploaded"), (diary, "MEMO_DIR", tmp / "memo"),
            (diary, "CONFIG", tmp / "notion.json"), (diary, "commits", mock.Mock(return_value=[])),
            (autoupload, "connect_store", mock.Mock(side_effect=lambda: self.store)),
            (autoupload, "collect", mock.Mock(return_value=([("수학", True)], []))),
            (autoupload, "RETRY_WAITS", (0, 0)), (autoupload, "notify", mock.Mock()), (autoupload, "log", mock.Mock()),
        ):
            p = mock.patch.object(target, name, value)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        diary.save_config({"token": "tok", "database_id": "db"})

    def push(self, **kw):
        p = mock.patch.object(diary, "push", mock.Mock(**kw))
        self.addCleanup(p.stop)
        return p.start()

    def pushed_days(self, push):
        return [c.args[1] for c in push.call_args_list]

    def test_no_memo_uploads_nothing(self):
        diary.write_memo(DAY, "  \n")  # 공백만 있는 메모도 없는 것
        push = self.push(return_value="u")
        self.assertEqual(autoupload.run(self.NOW), 0)
        push.assert_not_called()
        self.mocks["connect_store"].assert_not_called()  # 캘린더에도 안 붙는다
        self.mocks["notify"].assert_not_called()

    def test_memo_day_is_uploaded(self):
        fake = FakeNotion()
        self.addCleanup(fake.close)
        p = mock.patch.object(diary, "API", fake.url)
        p.start()
        self.addCleanup(p.stop)
        diary._me_cache.clear()
        diary.write_memo(DAY, "오늘은 좋았다")
        self.assertEqual(autoupload.run(self.NOW), 0)
        box = fake.pages["2026-10-23"]["children"][0]
        texts = [b[b["type"]]["rich_text"][0]["plain_text"] for b in box["_kids"]]
        self.assertIn("수학", texts)
        self.assertIn("오늘은 좋았다", texts)
        self.assertTrue(diary.is_uploaded(DAY))
        self.assertIn("2026-10-23", self.mocks["notify"].call_args.args[0])

    def test_already_uploaded_day_is_uploaded_again(self):  # 버튼으로 올린 뒤 고친 메모도 반영
        diary.write_memo(DAY, "메모")
        diary.mark_uploaded(DAY)
        push = self.push(return_value="u")
        self.assertEqual(autoupload.run(self.NOW), 0)
        push.assert_called_once()

    def test_missed_days_are_caught_up(self):
        for d in (date(2026, 10, 15), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)):
            diary.write_memo(d, "메모")
        diary.mark_uploaded(date(2026, 10, 21))
        push = self.push(return_value="u")
        self.assertEqual(autoupload.run(self.NOW), 0)  # 10-23 은 메모가 없다
        # 일주일 넘은 10-15, 이미 올린 10-21 은 빼고 오래된 순으로
        self.assertEqual(self.pushed_days(push), [date(2026, 10, 20), date(2026, 10, 22)])
        self.assertTrue(diary.is_uploaded(date(2026, 10, 20)))

    def test_calendar_not_connected_uploads_nothing(self):
        diary.write_memo(DAY, "메모")
        self.store.missing = ["g"]
        push = self.push(return_value="u")
        self.assertEqual(autoupload.run(self.NOW), 1)
        push.assert_not_called()
        self.assertIn("구글 할 일", self.mocks["notify"].call_args.args[1])
        self.assertFalse(diary.is_uploaded(DAY))  # 내일 밤에 놓친 날로 다시 올라간다

    def test_holiday_calendar_failure_is_ignored(self):
        diary.write_memo(DAY, "메모")
        self.store.missing = ["h"]
        push = self.push(return_value="u")
        self.assertEqual(autoupload.run(self.NOW), 0)
        push.assert_called_once()

    def test_offline_is_retried(self):
        diary.write_memo(DAY, "메모")
        push = self.push(side_effect=[diary.Offline("인터넷"), "u"])
        self.assertEqual(autoupload.run(self.NOW), 0)
        self.assertEqual(push.call_count, 2)
        self.assertTrue(diary.is_uploaded(DAY))

    def test_gives_up_after_retries(self):
        diary.write_memo(DAY, "메모")
        push = self.push(side_effect=diary.Offline("인터넷"))
        self.assertEqual(autoupload.run(self.NOW), 1)
        self.assertEqual(push.call_count, 3)
        self.assertFalse(diary.is_uploaded(DAY))
        self.assertIn("못 올렸어요", self.mocks["notify"].call_args.args[0])

    def test_failure_reports_only_days_not_uploaded(self):
        diary.write_memo(date(2026, 10, 22), "메모")
        diary.write_memo(DAY, "메모")
        self.push(side_effect=["u", diary.NotionError("오류")])
        self.assertEqual(autoupload.run(self.NOW), 1)
        self.assertTrue(diary.is_uploaded(date(2026, 10, 22)))
        body = self.mocks["notify"].call_args.args[1]
        self.assertIn("2026-10-23", body)
        self.assertNotIn("2026-10-22", body)

    def test_old_day_error_does_not_block_today(self):
        diary.write_memo(date(2026, 10, 22), "메모")
        diary.write_memo(DAY, "메모")
        self.push(side_effect=[diary.NotionError("오류"), "u"])
        self.assertEqual(autoupload.run(self.NOW), 1)
        self.assertTrue(diary.is_uploaded(DAY))
        body = self.mocks["notify"].call_args.args[1]
        self.assertIn("2026-10-22", body)
        self.assertNotIn("2026-10-23", body)

    def test_other_errors_are_not_retried(self):
        diary.write_memo(DAY, "메모")
        push = self.push(side_effect=diary.NotionError("노션 토큰이 올바르지 않아요"))
        self.assertEqual(autoupload.run(self.NOW), 1)
        self.assertEqual(push.call_count, 1)

    def test_no_token(self):
        diary.save_config({})
        diary.write_memo(DAY, "메모")
        push = self.push(return_value="u")
        self.assertEqual(autoupload.run(self.NOW), 1)
        push.assert_not_called()
        self.mocks["notify"].assert_called_once()


class SlowStore:
    """연결이 늦는 Store 흉내. 재시작하면 걸려 있던 연결이 오류로 끝나고(→ '다 끝남') 다시 연결한다."""

    def __init__(self, _on_changed, reconnects=True):
        self.on_all_connected = None
        self.connecting = ["구글"]
        self.restarts = 0
        self.reconnects = reconnects
        self.connected = False

    def pending(self):
        return list(self.connecting)

    def _finish(self):
        self.connecting = []
        if self.on_all_connected:
            self.on_all_connected()

    def restart_services(self, done):
        self.restarts += 1

        def killed():
            self._finish()  # 끊긴 연결도 '연결 끝'
            self.connecting = ["구글"]  # 새로 뜬 서비스에 다시 연결
            done(None)
            if self.reconnects:
                GLib.timeout_add(30, connected)
            return False

        def connected():
            self.connected = True
            self._finish()
            return False

        GLib.timeout_add(10, killed)


class ConnectStore(unittest.TestCase):
    def setUp(self):
        for name, value in (("RESTART_AFTER", 0.02), ("CONNECT_WAIT", 0.5), ("log", mock.Mock())):
            p = mock.patch.object(autoupload, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_waits_for_reconnect_after_restart(self):
        store = autoupload.connect_store(SlowStore)
        self.assertEqual(store.restarts, 1)
        self.assertTrue(store.connected)  # 재시작 중 '다 끝남'에 속아 일찍 끝나지 않는다

    def test_gives_up_after_wait(self):
        store = autoupload.connect_store(lambda cb: SlowStore(cb, reconnects=False))
        self.assertEqual(store.restarts, 1)
        self.assertEqual(store.pending(), ["구글"])

    def test_already_connected(self):
        def ready(cb):
            s = SlowStore(cb)
            s.connecting = []
            return s

        self.assertEqual(autoupload.connect_store(ready).restarts, 0)


class OfflineError(unittest.TestCase):
    def test_unreachable_server_is_offline(self):
        with mock.patch.object(diary, "API", "http://127.0.0.1:9/v1"):
            with self.assertRaises(diary.Offline):
                diary.find_page({"token": "tok", "database_id": "db"}, DAY)
