"""날짜·시간 변환, 일정·할 일 데이터, 반복 규칙, 드래그·겹침 계산. 화면(GTK) 없이 테스트할 수 있다."""
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import gi

gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import ECal, ICalGLib  # noqa: E402


APP_ID = "io.github.yunho09.Haru"


WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


# (라벨, 시작 기준 몇 초 전) — None 은 알림 없음
REMINDERS = [
    ("알림 없음", None),
    ("정시", 0),
    ("5분 전", 5 * 60),
    ("10분 전", 10 * 60),
    ("30분 전", 30 * 60),
    ("1시간 전", 60 * 60),
    ("1일 전", 24 * 60 * 60),
]


DEFAULT_REMINDER = 3  # 10분 전


REPEATS = ["반복 안 함", "매일", "평일 (월~금)", "매주", "2주마다", "매월", "매년"]


CUSTOM_REPEAT = "사용자 지정 (그대로 유지)"


WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


KEEP = object()  # 반복 규칙을 건드리지 않음


def local_tz_name():
    """시스템 시간대 이름 (예: Asia/Seoul)."""
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    real = os.path.realpath("/etc/localtime")  # 데비안 계열이 아니면 zoneinfo 링크로
    return real.split("zoneinfo/", 1)[1] if "zoneinfo/" in real else "Asia/Seoul"


TZ_NAME = local_tz_name()


TZ = ICalGLib.Timezone.get_builtin_timezone(TZ_NAME) or ICalGLib.Timezone.get_utc_timezone()


def ical_to_py(t):
    """ICalGLib.Time → (date | datetime). 날짜형이면 date, 아니면 로컬 시각 datetime."""
    if t is None or t.is_null_time():
        return None
    if t.is_date():
        return date(t.get_year(), t.get_month(), t.get_day())
    u = t.clone()
    if u.get_timezone() is not None:
        u.convert_to_zone_inplace(TZ)
    return datetime(u.get_year(), u.get_month(), u.get_day(), u.get_hour(), u.get_minute())


def py_to_ical(v):
    t = ICalGLib.Time.new_null_time()
    t.set_date(v.year, v.month, v.day)
    if isinstance(v, datetime):
        t.set_time(v.hour, v.minute, 0)
        t.set_timezone(TZ)
    else:
        t.set_is_date(True)
    return t


def comp_dt(v):
    return ECal.ComponentDateTime.new(py_to_ical(v), None if not isinstance(v, datetime) else TZ_NAME)


def as_date(v):
    return v.date() if isinstance(v, datetime) else v


def fmt_day(d):
    year = f"{d.year}년 " if d.year != date.today().year else ""
    return f"{year}{d.month}월 {d.day}일 ({WEEKDAYS[d.weekday()]})"


def fmt_time(dt):
    ampm = "오전" if dt.hour < 12 else "오후"
    h = dt.hour % 12 or 12
    return f"{ampm} {h}:{dt.minute:02d}"


def text_of(ct):
    return ct.get_value() if ct is not None else ""


@dataclass
class Event:
    uid: str
    rid: str  # 반복 일정 인스턴스 ID (없으면 "")
    source_uid: str
    summary: str
    location: str
    start: object  # date(하루 종일) | datetime
    end: object
    all_day: bool
    icomp: object  # 인스턴스 ICalGLib.Component 사본
    recurring: bool
    day_off: bool = False  # 공휴일(빨간 날)

    def days(self):
        """일정이 걸친 (첫날, 마지막날) — 둘 다 포함."""
        if self.all_day:
            return self.start, max(self.end - timedelta(days=1), self.start)
        s, e = self.start.date(), self.end.date()
        if self.end.time() == time(0, 0) and e > s:
            e -= timedelta(days=1)  # 자정에 끝나면 그 날은 포함 안 함
        return s, e

    def covers(self, d):
        first, last = self.days()
        return first <= d <= last


@dataclass
class Task:
    uid: str
    source_uid: str
    summary: str
    due: object  # date | None
    done: bool
    icomp: object
    completed: object = None  # 완료한 날 (date | None)


def parse_rule(text):
    """'FREQ=WEEKLY;BYDAY=MO' → {'FREQ': 'WEEKLY', 'BYDAY': 'MO'}"""
    out = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def repeat_index(r, start=None):
    """반복 규칙 → REPEATS 인덱스. 앱에서 표현 못 하는 규칙이면 None(사용자 지정).
    start(시리즈 첫날)와 어긋나는 요일·날짜 지정도 표현 못 하는 것으로 본다 — 프리셋으로 바꾸면 규칙이 달라지니까."""
    if not r:
        return 0
    if set(r) - {"FREQ", "INTERVAL", "BYDAY", "UNTIL", "BYMONTHDAY", "BYMONTH", "WKST"}:
        return None  # COUNT, BYSETPOS 등
    freq, interval, byday = r.get("FREQ"), int(r.get("INTERVAL", "1")), r.get("BYDAY")
    days = byday.split(",") if byday else []
    if any(not d.isalpha() for d in days):
        return None  # 매월 둘째 화요일(2TU) 같은 것
    if start is None:
        if "BYMONTHDAY" in r or "BYMONTH" in r:
            return None
    else:
        if r.get("BYMONTHDAY", str(start.day)) != str(start.day):
            return None  # 매월 마지막 날(-1), 1일·15일 등
        if r.get("BYMONTH", str(start.month)) != str(start.month):
            return None
        if freq == "WEEKLY" and len(days) == 1 and days[0] != WEEKDAY_CODES[start.weekday()]:
            return None
    if freq == "DAILY" and interval == 1 and not days:
        return 1
    if freq == "WEEKLY" and interval == 1 and sorted(days) == sorted(WEEKDAY_CODES[:5]):
        return 2
    if freq == "WEEKLY" and interval in (1, 2) and len(days) <= 1:
        return 3 if interval == 1 else 4
    if freq == "MONTHLY" and interval == 1 and not days:
        return 5
    if freq == "YEARLY" and interval == 1 and not days:
        return 6
    return None


def rule_until(r):
    u = r.get("UNTIL")
    if not u:
        return None
    if "T" not in u:
        return date(int(u[:4]), int(u[4:6]), int(u[6:8]))
    dt = datetime.strptime(u.rstrip("Z"), "%Y%m%dT%H%M%S")
    if u.endswith("Z"):
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt.date()


def rule_start_problem(idx, start):
    """규칙과 시작일이 안 맞으면 알릴 문구, 맞으면 None.
    평일 반복을 주말에 시작하면 그 주말 하루가 회차로 하나 더 생긴다 (RFC 5545: 시작일은 항상 첫 회차)."""
    if idx == 2 and as_date(start).weekday() >= 5:
        return "평일 반복은 월~금에 시작해야 해요. 시작일을 평일로 골라 주세요"
    return None


def build_rule(idx, start, until):
    """REPEATS 인덱스 → RRULE 문자열 ("" 이면 반복 없음)."""
    if idx == 0:
        return ""
    d = as_date(start)
    code = WEEKDAY_CODES[d.weekday()]
    rule = [
        None,
        "FREQ=DAILY",
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        f"FREQ=WEEKLY;BYDAY={code}",
        f"FREQ=WEEKLY;INTERVAL=2;BYDAY={code}",
        f"FREQ=MONTHLY;BYMONTHDAY={d.day}",
        f"FREQ=YEARLY;BYMONTH={d.month};BYMONTHDAY={d.day}",
    ][idx]
    if until:
        if isinstance(start, datetime):
            # 시각이 있는 일정의 UNTIL 은 UTC 로 (RFC 5545)
            u = datetime.combine(until, time(23, 59, 59)).astimezone(timezone.utc)
            rule += f";UNTIL={u:%Y%m%dT%H%M%SZ}"
        else:
            rule += f";UNTIL={until:%Y%m%d}"
    return rule


def reminder_index(comp):
    for alarm in comp.get_all_alarms() or []:
        trig = alarm.get_trigger()
        if trig is None or trig.get_kind() != ECal.ComponentAlarmTriggerKind.RELATIVE_START:
            continue
        secs = -trig.get_duration().as_int()
        best = min(range(1, len(REMINDERS)), key=lambda i: abs(REMINDERS[i][1] - secs))
        return best
    return 0


def set_reminder(comp, idx):
    comp.remove_all_alarms()
    secs = REMINDERS[idx][1]
    if secs is None:
        return
    alarm = ECal.ComponentAlarm.new()
    alarm.set_action(ECal.ComponentAlarmAction.DISPLAY)
    alarm.set_trigger(ECal.ComponentAlarmTrigger.new_relative(
        ECal.ComponentAlarmTriggerKind.RELATIVE_START, ICalGLib.Duration.new_from_int(-secs)))
    alarm.set_description(comp.get_summary() or ECal.ComponentText.new("일정", None))
    comp.add_alarm(alarm)


# ───────────────────────── 배치·드래그 계산 (화면과 무관한 부분) ─────────────────────────

def assign_lanes(spans, week_first):
    """한 주 안에서 막대가 겹치지 않게 줄을 배정한다.
    spans = [(첫날, 마지막날)] (위에 놓을 것부터) → [(줄 번호, 그 주에서 차지하는 칸 range)]"""
    week_last = week_first + timedelta(days=6)
    used, out = set(), []
    for first, last in spans:
        cols = range((max(first, week_first) - week_first).days, (min(last, week_last) - week_first).days + 1)
        lane = 0
        while any((lane, c) in used for c in cols):
            lane += 1
        used.update((lane, c) for c in cols)
        out.append((lane, cols))
    return out


def overlap_columns(segs):
    """시간이 겹치는 일정을 나란히 놓을 열. segs = [(시작, 끝, 값)] → [(시작, 끝, 값, 열, 열 개수)]"""
    segs = sorted(segs, key=lambda x: (x[0], -(x[1] - x[0]).total_seconds()))
    out, cluster, cols, end = [], [], [], None

    def flush():
        out.extend((s, e, v, col, len(cols)) for s, e, v, col in cluster)

    for s, e, v in segs:
        if cluster and s >= end:
            flush()
            cluster, cols, end = [], [], None
        for i, c_end in enumerate(cols):
            if c_end <= s:
                cols[i] = e
                col = i
                break
        else:
            cols.append(e)
            col = len(cols) - 1
        cluster.append((s, e, v, col))
        end = e if end is None else max(end, e)
    flush()
    return out


def drag_range(mode, event_days, origin, target):
    """월 보기 드래그 결과 (첫날, 마지막날).
    mode: 'start'(막대 왼쪽 끝) | 'end'(오른쪽 끝) | 'move'(가운데) | None(빈 칸에서 새로 끌기)"""
    if event_days is not None:
        first, last = event_days
        if mode == "start":
            return (target, last) if target <= last else (last, target)
        if mode == "end":
            return (first, target) if target >= first else (target, first)
        off = target - origin
        return first + off, last + off
    return (origin, target) if origin <= target else (target, origin)


def stretched_times(ev, first, last):
    """일정을 first~last 날짜에 걸치도록 바꾼 (시작, 끝) — 시각은 그대로."""
    if ev.all_day:
        return first, last + timedelta(days=1)
    s = datetime.combine(first, ev.start.time())
    if ev.end.time() == time(0) and ev.end.date() > ev.start.date():
        e = datetime.combine(last + timedelta(days=1), time(0))  # 자정에 끝나던 일정은 계속 자정에
    else:
        e = datetime.combine(last, ev.end.time())
    if e <= s:
        e = s + timedelta(hours=1)
    return s, e
