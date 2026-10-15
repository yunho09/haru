#!/usr/bin/python3
"""하루 — GNOME 일정·할 일 관리 앱.

저장소는 Evolution Data Server(EDS)를 그대로 쓴다. 그래서 GNOME 온라인 계정에
등록한 구글 캘린더/할 일과 자동으로 동기화되고, 알림은 evolution-alarm-notify
데몬이 앱이 꺼져 있어도 띄워준다.
"""
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("EDataServer", "1.2")
gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import Adw, ECal, EDataServer, Gdk, Gio, GLib, GObject, Gtk, ICalGLib, Pango  # noqa: E402

import diary  # noqa: E402

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
    try:
        return open("/etc/timezone").read().strip() or "Asia/Seoul"
    except OSError:
        return "Asia/Seoul"


TZ_NAME = local_tz_name()
TZ = ICalGLib.Timezone.get_builtin_timezone(TZ_NAME) or ICalGLib.Timezone.get_utc_timezone()


# ───────────────────────── 시간 변환 ─────────────────────────

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


# ───────────────────────── 데이터 ─────────────────────────

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


class Store:
    """EDS 캘린더/할 일 목록 연결과 실시간 갱신을 관리한다."""

    def __init__(self, on_changed):
        self.on_changed = on_changed
        self.registry = EDataServer.SourceRegistry.new_sync(None)
        self.clients = {}  # source uid → ECal.Client
        self.views = []
        self.colors = {}
        self._pending = None
        self._gen = 0
        self.on_error = lambda msg: print(msg, file=sys.stderr)
        for sig in ("source-added", "source-removed", "source-enabled", "source-disabled"):
            self.registry.connect(sig, lambda *_: self.reconnect())
        self.reconnect()

    # ── 연결 ──
    def _sources(self, ext):
        return [s for s in self.registry.list_sources(ext) if s.get_enabled()]

    def reconnect(self):
        # 이전 세대에서 아직 진행 중인 연결은 도착해도 버린다
        self._gen += 1
        for v in self.views:
            try:
                v.stop()
            except GLib.Error:
                pass
        self.views = []
        self.clients = {}
        self.colors = {}
        for ext, kind in (
            (EDataServer.SOURCE_EXTENSION_CALENDAR, ECal.ClientSourceType.EVENTS),
            (EDataServer.SOURCE_EXTENSION_TASK_LIST, ECal.ClientSourceType.TASKS),
        ):
            for src in self._sources(ext):
                self.colors[src.get_uid()] = src.get_extension(ext).get_color() or "#62a0ea"
                ECal.Client.connect(src, kind, 30, None, self._on_connected, self._gen)
        self.changed()

    def _on_connected(self, _src, res, gen):
        try:
            client = ECal.Client.connect_finish(res)
        except GLib.Error as e:
            print("연결 실패:", e.message, file=sys.stderr)
            return
        uid = client.get_source().get_uid()
        src = self.source(uid)
        if gen != self._gen or src is None or not src.get_enabled():
            return
        client.set_default_timezone(TZ)
        self.clients[uid] = client
        client.get_view("#t", None, self._on_view, gen)
        self.changed()

    def _on_view(self, client, res, gen):
        try:
            ok, view = client.get_view_finish(res)
        except GLib.Error:
            return
        if gen != self._gen:
            return
        for sig in ("objects-added", "objects-modified", "objects-removed"):
            view.connect(sig, lambda *_: self.changed())
        view.start()
        self.views.append(view)

    def changed(self):
        """여러 신호가 몰려와도 한 번만 다시 그리도록 묶는다."""
        if self._pending:
            return

        def fire():
            self._pending = None
            self.on_changed()
            return False

        self._pending = GLib.timeout_add(120, fire)

    # ── 조회 ──
    def source(self, uid):
        return self.registry.ref_source(uid)

    def name(self, uid):
        s = self.source(uid)
        return s.get_display_name() if s else uid

    def is_holiday_source(self, uid):
        return "휴일" in self.name(uid) or "holiday" in self.name(uid).lower()

    def _clients(self, kind):
        return [
            (uid, c) for uid, c in list(self.clients.items())
            if c.get_source_type() == kind
        ]

    def writable(self, kind):
        """쓰기 가능한 캘린더 목록. 구글(이메일 이름) 캘린더를 맨 앞에."""
        items = [(uid, c) for uid, c in self._clients(kind)
                 if not c.is_readonly() and uid != "birthdays" and self.source(uid) is not None]
        items.sort(key=lambda x: (0 if self.source(x[0]).get_parent() not in ("local-stub", None) else 1, self.name(x[0])))
        return [uid for uid, _ in items]

    def events(self, start: date, end: date):
        out = []
        t0 = int(datetime.combine(start, time()).timestamp())
        t1 = int(datetime.combine(end, time()).timestamp())
        utc = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sexp = f'(occur-in-time-range? (make-time "{utc(t0)}") (make-time "{utc(t1)}"))'
        for uid, client in self._clients(ECal.ClientSourceType.EVENTS):
            holiday_src = self.is_holiday_source(uid)

            def cb(icomp, s, e, *_a, uid=uid, holiday_src=holiday_src):
                comp = ECal.Component.new_from_icalcomponent(icomp.clone())
                if comp is None:
                    return True
                st, en = ical_to_py(s), ical_to_py(e)
                all_day = not isinstance(st, datetime)
                if en is None:
                    en = st + (timedelta(days=1) if all_day else timedelta(0))
                if all_day and isinstance(en, datetime):
                    en = en.date()
                rid = comp.get_recurid_as_string() or ""
                desc = comp.get_descriptions()
                # 구글 '대한민국의 휴일'은 설명이 "공휴일"/"기념일" 로 시작한다. 기념일은 쉬는 날이 아님.
                day_off = holiday_src and not (desc and text_of(desc[0]).startswith("기념일"))
                out.append(Event(
                    uid=comp.get_uid(), rid=rid, source_uid=uid,
                    summary=text_of(comp.get_summary()) or "(제목 없음)",
                    location=comp.get_location() or "",
                    start=st, end=en, all_day=all_day, icomp=comp.get_icalcomponent(),
                    recurring=bool(rid) or comp.has_recurrences(), day_off=day_off,
                ))
                return True

            try:
                ok, comps = client.get_object_list_as_comps_sync(sexp, None)
            except GLib.Error as e:
                print("일정 읽기 실패:", e.message, file=sys.stderr)
                continue
            # 반복 일정에서 떼어낸 회차는 원본을 펼칠 때 같이 나온다.
            # (generate_instances_sync 는 떼어낸 회차를 무시해서 쓰지 않는다)
            masters = {c.get_uid() for c in comps if not c.get_recurid_as_string()}
            for comp in comps:
                if comp.get_recurid_as_string() and comp.get_uid() in masters:
                    continue
                try:
                    client.generate_instances_for_object_sync(comp.get_icalcomponent(), t0, t1, None, cb)
                except GLib.Error as e:
                    print("일정 펼치기 실패:", e.message, file=sys.stderr)
        out.sort(key=lambda ev: (as_date(ev.start), 0 if ev.all_day else 1, time() if ev.all_day else ev.start.time(), ev.summary))
        return out

    def tasks(self):
        out = []
        for uid, client in self._clients(ECal.ClientSourceType.TASKS):
            try:
                ok, comps = client.get_object_list_as_comps_sync("#t", None)
            except GLib.Error:
                continue
            for comp in comps:
                due = comp.get_due()
                d = ical_to_py(due.get_value()) if due else None
                if isinstance(d, datetime):
                    d = d.date()
                done_at = ical_to_py(comp.get_completed()) if comp.get_completed() else None
                out.append(Task(
                    uid=comp.get_uid(), source_uid=uid,
                    summary=text_of(comp.get_summary()) or "(제목 없음)",
                    due=d, done=comp.get_status() == ICalGLib.PropertyStatus.COMPLETED,
                    icomp=comp.get_icalcomponent(), completed=as_date(done_at) if done_at else None,
                ))
        out.sort(key=lambda t: (t.due is None, t.due or date.max, t.summary))
        return out

    # ── 쓰기 (모두 비동기, 결과는 done(err|None)) ──
    def _client(self, source_uid, done):
        client = self.clients.get(source_uid)
        if client is None:
            done("캘린더에 다시 연결하는 중이에요. 잠시 후 다시 해주세요")
        return client

    def create(self, source_uid, comp, done):
        if not (client := self._client(source_uid, done)):
            return
        client.create_object(comp.get_icalcomponent(), ECal.OperationFlags.NONE, None, self._finish("create", done), None)

    def modify(self, source_uid, comp, mod, done):
        if not (client := self._client(source_uid, done)):
            return
        client.modify_object(comp.get_icalcomponent(), mod, ECal.OperationFlags.NONE, None, self._finish("modify", done), None)

    def remove(self, source_uid, uid, rid, mod, done):
        if not (client := self._client(source_uid, done)):
            return
        client.remove_object(uid, rid or None, mod, ECal.OperationFlags.NONE, None, self._finish("remove", done), None)

    def move(self, old_src, new_src, comp, done):
        """다른 캘린더로 옮기기 = 새 캘린더에 만들고 원래 것을 지운다."""
        uid = comp.get_uid()
        new = comp.clone()
        new.set_uid(str(uuid.uuid4()))
        # 원래 캘린더에 묶인 정보(초대 조직자/참석자, 서버 전용 X- 속성)는 가져가지 않는다
        new.set_organizer(None)
        new.set_attendees(None)
        ic = new.get_icalcomponent()
        xs, p = [], ic.get_first_property(ICalGLib.PropertyKind.X_PROPERTY)
        while p is not None:
            xs.append(p)
            p = ic.get_next_property(ICalGLib.PropertyKind.X_PROPERTY)
        for p in xs:
            ic.remove_property(p)

        def after_remove(err):
            if err:
                # 새 캘린더에는 이미 만들어졌으니 저장은 성공으로 처리 (다시 누르면 중복됨)
                self.on_error(f"옮긴 뒤 원래 일정 삭제 실패: {err}")
            done(None)

        def after_create(err):
            if err:
                done(err)
            else:
                self.remove(old_src, uid, None, ECal.ObjModType.ALL, after_remove)

        self.create(new_src, new, after_create)

    def master(self, source_uid, uid):
        client = self.clients.get(source_uid)
        if client is None:
            return None
        try:
            ok, icomp = client.get_object_sync(uid, None, None)
        except GLib.Error:
            return None
        return ECal.Component.new_from_icalcomponent(icomp)

    def _finish(self, op, done):
        def cb(client, res, _data):
            try:
                getattr(client, f"{op}_object_finish")(res)
                err = None
            except GLib.Error as e:
                err = e.message
            self.changed()
            done(err)

        return cb


# ───────────────────────── 위젯 ─────────────────────────

class DateButton(Gtk.MenuButton):
    """누르면 달력 팝오버가 열리는 날짜 선택 버튼."""

    def __init__(self, d, on_change=None):
        super().__init__(valign=Gtk.Align.CENTER)
        self.add_css_class("flat")
        self.on_change = on_change
        self.cal = Gtk.Calendar()
        self.cal.connect("day-selected", self._picked)
        # 화살표로 달을 넘기면 내부 날짜가 신호 없이 바뀌어서(3/5→4/5), 같은 날짜 숫자를
        # 누르면 day-selected 가 안 온다. 클릭이 끝난 뒤 직접 확인한다.
        self._navigated = False
        for sig in ("prev-month", "next-month", "prev-year", "next-year"):
            self.cal.connect(sig, self._on_nav)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: GLib.idle_add(self._after_click))
        self.cal.add_controller(click)
        pop = Gtk.Popover(child=self.cal)
        self.set_popover(pop)
        self.set_date(d)

    def set_date(self, d):
        self.value = d
        self.set_label(fmt_day(d))
        self.cal.select_day(GLib.DateTime.new_local(d.year, d.month, d.day, 0, 0, 0))

    def _on_nav(self, *_):
        self._navigated = True

    def _after_click(self):
        if self._navigated:
            self._navigated = False
        else:
            self._picked(self.cal)
        return False

    def _picked(self, cal):
        g = cal.get_date()
        d = date(g.get_year(), g.get_month(), g.get_day_of_month())
        if d != self.value:
            self.value = d
            self.set_label(fmt_day(d))
            self.get_popover().popdown()
            if self.on_change:
                self.on_change(d)


class TimeEntry(Gtk.Box):
    """시:분 입력 (분 버튼은 5분씩)."""

    def __init__(self, t, on_change=None):
        super().__init__(spacing=4, valign=Gtk.Align.CENTER)
        self.on_change = on_change
        self.h = self._spin(23, 1)
        self.m = self._spin(59, 5)
        self.append(self.h)
        self.append(Gtk.Label(label=":"))
        self.append(self.m)
        self.set_time(t)
        for s in (self.h, self.m):
            s.connect("value-changed", lambda *_: self.on_change and self.on_change(self.value))

    @staticmethod
    def _spin(maxv, step):
        s = Gtk.SpinButton.new_with_range(0, maxv, step)
        s.set_wrap(True)
        s.set_numeric(True)
        s.set_width_chars(2)
        s.set_orientation(Gtk.Orientation.VERTICAL)
        s.connect("output", lambda sp: (sp.set_text(f"{int(sp.get_value()):02d}"), True)[1])
        return s

    def set_time(self, t):
        self.h.set_value(t.hour)
        self.m.set_value(t.minute)

    @property
    def value(self):
        return time(int(self.h.get_value()), int(self.m.get_value()))


def parse_rule(text):
    """'FREQ=WEEKLY;BYDAY=MO' → {'FREQ': 'WEEKLY', 'BYDAY': 'MO'}"""
    out = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def repeat_index(r):
    """반복 규칙 → REPEATS 인덱스. 앱에서 표현 못 하는 규칙이면 None(사용자 지정)."""
    if not r:
        return 0
    if set(r) - {"FREQ", "INTERVAL", "BYDAY", "UNTIL", "BYMONTHDAY", "BYMONTH", "WKST"}:
        return None  # COUNT, BYSETPOS 등
    freq, interval, byday = r.get("FREQ"), int(r.get("INTERVAL", "1")), r.get("BYDAY")
    days = byday.split(",") if byday else []
    if any(not d.isalpha() for d in days):
        return None  # 매월 둘째 화요일(2TU) 같은 것
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


def combo_row(title, labels, selected=0):
    row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
    row.set_selected(selected)
    return row


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


class EventDialog(Adw.Dialog):
    def __init__(self, win, day=None, event=None, span=None):
        super().__init__(title="일정 편집" if event else "새 일정", content_width=440)
        self.win, self.store, self.event = win, win.store, event
        cal_uids = self.store.writable(ECal.ClientSourceType.EVENTS)
        readonly = event is not None and event.source_uid not in cal_uids
        if event is not None and readonly:
            self.set_title("일정")

        comp = ECal.Component.new_from_icalcomponent(event.icomp.clone()) if event else None
        if event:
            start, end, all_day = event.start, event.end, event.all_day
        else:
            now = datetime.now()
            day = day or now.date()
            hour = min(now.hour + 1, 23) if day == now.date() else 9
            base = datetime.combine(day, time(hour))
            start, end, all_day = base, base + timedelta(hours=1), False
            if span:  # 달력에서 여러 날을 끌어 고른 경우: 하루 종일 일정
                start, end, all_day = span[0], span[1] + timedelta(days=1), True

        page = Adw.PreferencesPage()
        g1 = Adw.PreferencesGroup()
        self.title = Adw.EntryRow(title="제목", text=event.summary if event and event.summary != "(제목 없음)" else "")
        self.title.connect("entry-activated", lambda *_: self.save())
        self.location = Adw.EntryRow(title="장소", text=event.location if event else "")
        g1.add(self.title)
        g1.add(self.location)
        page.add(g1)

        g2 = Adw.PreferencesGroup()
        self.all_day = Adw.SwitchRow(title="하루 종일", active=all_day)
        sd = start if all_day else start.date()
        ed = (end - timedelta(days=1)) if all_day else end.date()
        ed = max(ed, sd)
        self._start_prev = sd
        self.start_date = DateButton(sd, self._start_date_changed)
        self.end_date = DateButton(ed)
        t_s = time(9) if all_day else start.time()
        t_e = time(10) if all_day else end.time()
        self.start_time = TimeEntry(t_s, self._start_time_changed)
        self.end_time = TimeEntry(t_e)
        r_start = Adw.ActionRow(title="시작")
        r_start.add_suffix(self.start_date)
        r_start.add_suffix(self.start_time)
        r_end = Adw.ActionRow(title="종료")
        r_end.add_suffix(self.end_date)
        r_end.add_suffix(self.end_time)
        self.all_day.connect("notify::active", lambda *_: self._all_day_toggled())

        # 반복: 반복 일정이면 원본(첫 회차)의 규칙을 읽는다
        rule = {}
        if event is not None and event.recurring:
            master = self.store.master(event.source_uid, event.uid)
            rrules = master.get_rrules() if master else None
            rule = parse_rule(rrules[0].to_string()) if rrules else {"X-UNKNOWN": "1"}
        idx = repeat_index(rule)
        labels = REPEATS + ([CUSTOM_REPEAT] if idx is None else [])
        self._custom_repeat = len(REPEATS) if idx is None else -1
        self._repeat0 = len(REPEATS) if idx is None else idx
        self._until0 = rule_until(rule) if idx else None
        self.repeat = combo_row("반복", labels, self._repeat0)
        self.repeat_end = Adw.SwitchRow(title="반복 종료일", active=self._until0 is not None)
        self.until = DateButton(self._until0 or (sd + timedelta(days=30)))
        self.until.set_visible(self._until0 is not None)
        self.repeat_end.add_suffix(self.until)
        self.repeat_end.connect("notify::active", lambda *_: self.until.set_visible(self.repeat_end.get_active()))
        self.repeat.connect("notify::selected", lambda *_: self._sync_repeat())
        for w in (self.all_day, r_start, r_end, self.repeat, self.repeat_end):
            g2.add(w)
        page.add(g2)
        self._sync_repeat()

        g3 = Adw.PreferencesGroup()
        if readonly:
            self.cal_uids = [event.source_uid]
        else:
            self.cal_uids = cal_uids
        sel = self.cal_uids.index(event.source_uid) if event and event.source_uid in self.cal_uids else 0
        self.calendar = combo_row("캘린더", [self.store.name(u) for u in self.cal_uids], sel)
        self._reminder0 = reminder_index(comp) if comp else DEFAULT_REMINDER
        self.reminder = combo_row("알림", [r[0] for r in REMINDERS], self._reminder0)
        desc = comp.get_descriptions() if comp else None
        self.memo = Adw.EntryRow(title="메모", text=text_of(desc[0]) if desc else "")
        for w in (self.calendar, self.reminder, self.memo):
            g3.add(w)
        page.add(g3)

        if event and not readonly:
            g4 = Adw.PreferencesGroup()
            delete = Gtk.Button(label="일정 삭제", halign=Gtk.Align.CENTER)
            delete.add_css_class("destructive-action")
            delete.add_css_class("pill")
            delete.connect("clicked", lambda *_: self.delete())
            g4.add(delete)
            page.add(g4)

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="닫기" if readonly else "취소")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        if not readonly:
            self.save_btn = Gtk.Button(label="저장")
            self.save_btn.add_css_class("suggested-action")
            self.save_btn.connect("clicked", lambda *_: self.save())
            header.pack_end(self.save_btn)
        else:
            for w in (g1, g2, g3):
                w.set_sensitive(False)

        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(header)
        self.set_child(tv)
        self._sync_time_visibility()
        if event is None and span is None:
            # 새 일정은 하루 종일이 기본. 끄면 다음 정각부터 1시간으로 채워져 있다
            self.all_day.set_active(True)
        self.set_focus(self.title)

    def _sync_repeat(self):
        i = self.repeat.get_selected()
        self.repeat_end.set_visible(i not in (0, self._custom_repeat))

    def _repeat_choice(self):
        """(인덱스, 종료일) — 사용자 지정이면 인덱스 None."""
        i = self.repeat.get_selected()
        if i == self._custom_repeat:
            return None, self._until0
        if i == 0:
            return 0, None
        return i, self.until.value if self.repeat_end.get_active() else None

    def _sync_time_visibility(self):
        on = self.all_day.get_active()
        self.start_time.set_visible(not on)
        self.end_time.set_visible(not on)

    def _all_day_toggled(self):
        # 23:00~다음날 00:00 같은 일정은 하루 종일로 바꾸면 하루짜리여야 한다
        if (self.all_day.get_active() and self.end_time.value == time(0)
                and self.end_date.value > self.start_date.value):
            self.end_date.set_date(self.end_date.value - timedelta(days=1))
        self._sync_time_visibility()

    def _start_date_changed(self, d):
        # 기간은 유지한 채 종료 날짜도 같이 옮긴다
        self.end_date.set_date(self.end_date.value + (d - self._start_prev))
        self._start_prev = d

    def _start_time_changed(self, t):
        if self.end_date.value == self.start_date.value:
            later = (datetime.combine(date.today(), t) + timedelta(hours=1)).time()
            if self.end_time.value <= t:
                self.end_time.set_time(later if later > t else time(23, 55))

    def _values(self):
        if self.all_day.get_active():
            s = self.start_date.value
            e = max(self.end_date.value, s) + timedelta(days=1)
        else:
            s = datetime.combine(self.start_date.value, self.start_time.value)
            e = datetime.combine(self.end_date.value, self.end_time.value)
            if e <= s:
                e = s + timedelta(hours=1)
        return s, e

    def save(self):
        if not hasattr(self, "save_btn") or not self.save_btn.get_sensitive():
            return
        summary = self.title.get_text().strip() or "(제목 없음)"
        s, e = self._values()
        src = self.cal_uids[self.calendar.get_selected()] if self.cal_uids else None
        if src is None:
            self.win.toast("쓸 수 있는 캘린더가 없어요")
            return

        memo = self.memo.get_text().strip()
        reminder = self.reminder.get_selected()
        location = self.location.get_text().strip() or None

        r_idx, r_until = self._repeat_choice()
        rule_changed = (self._custom_repeat if r_idx is None else r_idx, r_until) != (self._repeat0, self._until0)

        def fill(comp, cs, ce, rule=KEEP):
            if rule is not KEEP:
                comp.set_rrules([ICalGLib.Recurrence.new_from_string(rule)] if rule else None)
                if not rule:
                    comp.set_rdates(None)
                    comp.set_exrules(None)
                    comp.set_exdates(None)
            comp.set_summary(ECal.ComponentText.new(summary, None))
            comp.set_location(location)
            comp.set_descriptions([ECal.ComponentText.new(memo, None)] if memo else [])
            comp.set_dtstart(comp_dt(cs))
            comp.set_dtend(comp_dt(ce))
            if self.event is None or reminder != self._reminder0:
                set_reminder(comp, reminder)
            comp.commit_sequence()
            return comp

        self.save_btn.set_sensitive(False)

        def done(err):
            if err:
                self.save_btn.set_sensitive(True)
                self.win.toast(f"저장 실패: {err}")
            else:
                self.win.select_day(s if not isinstance(s, datetime) else s.date())
                self.close()

        if not self.event:
            comp = ECal.Component.new()
            comp.set_new_vtype(ECal.ComponentVType.EVENT)
            comp.set_uid(str(uuid.uuid4()))
            self.store.create(src, fill(comp, s, e, build_rule(r_idx, s, r_until) if r_idx else KEEP), done)
            return

        ev = self.event
        if not ev.recurring:
            rule = build_rule(r_idx, s, r_until) if r_idx else KEEP
            comp = fill(ECal.Component.new_from_icalcomponent(ev.icomp.clone()), s, e, rule)
            if src != ev.source_uid:
                self.store.move(ev.source_uid, src, comp, done)
            else:
                self.store.modify(src, comp, ECal.ObjModType.ALL, done)
            return

        if src != ev.source_uid:
            self.win.toast("반복 일정은 다른 캘린더로 옮길 수 없어요")
            self.save_btn.set_sensitive(True)
            return

        def run(mod):
            if mod == ECal.ObjModType.THIS:
                # 떼어낸 인스턴스에는 반복 규칙이 남아 있으면 안 된다
                comp = ECal.Component.new_from_icalcomponent(ev.icomp.clone())
                comp.set_rrules(None)
                comp.set_rdates(None)
                comp.set_exrules(None)
                comp.set_exdates(None)
                self.store.modify(src, fill(comp, s, e), mod, done)
                return
            # 전체 수정: 원본(첫 회차) 기준으로, 이 회차에서 옮긴 만큼만 옮긴다
            master = self.store.master(src, ev.uid)
            if master is None:
                done("원본 일정을 찾을 수 없어요")
                return
            shift = as_date(s) - as_date(ev.start)
            if rule_changed:
                # 규칙을 새로 만들면 요일·날짜도 새 시작일 기준으로 다시 정해지니 날짜 이동도 가능
                m_day = as_date(ical_to_py(master.get_dtstart().get_value())) + shift
                ms = m_day if not isinstance(s, datetime) else datetime.combine(m_day, s.time())
                rule = build_rule(r_idx, ms, r_until)
                self.store.modify(src, fill(master, ms, ms + (e - s), rule), mod, done)
                return
            if shift.days != 0:
                # 반복 규칙(예: 매주 월요일)은 그대로라 날짜만 옮기면 일정이 깨진다
                done("모든 반복의 날짜는 바꿀 수 없어요. 시간·제목 등만 바꾸거나 '이 일정만'을 고르세요")
                return
            m_start = ical_to_py(master.get_dtstart().get_value())
            m_day = as_date(m_start)
            ms = m_day if not isinstance(s, datetime) else datetime.combine(m_day, s.time())
            self.store.modify(src, fill(master, ms, ms + (e - s)), mod, done)

        if rule_changed:
            # 반복 방식 자체를 바꾸는 건 전체에만 의미가 있다
            run(ECal.ObjModType.ALL)
            return
        self._ask_scope("이 반복 일정을 어떻게 바꿀까요?", "바꾸기", run,
                        lambda: self.save_btn.set_sensitive(True))

    def delete(self):
        ev = self.event

        def run(mod):
            def done(err):
                if err:
                    self.win.toast(f"삭제 실패: {err}")
                else:
                    self.close()
            self.store.remove(ev.source_uid, ev.uid, ev.rid if mod == ECal.ObjModType.THIS else None, mod, done)

        if ev.recurring:
            self._ask_scope("이 반복 일정을 어떻게 삭제할까요?", "삭제", run, None, destructive=True)
        else:
            d = Adw.AlertDialog(heading="일정을 삭제할까요?", body=ev.summary)
            d.add_response("cancel", "취소")
            d.add_response("ok", "삭제")
            d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
            d.connect("response", lambda _d, r: r == "ok" and run(ECal.ObjModType.ALL))
            d.present(self)

    def _ask_scope(self, heading, verb, run, cancelled, destructive=False):
        d = Adw.AlertDialog(heading=heading)
        d.add_response("cancel", "취소")
        d.add_response("this", f"이 일정만 {verb}")
        d.add_response("all", f"모든 반복 {verb}")
        if destructive:
            d.set_response_appearance("all", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("cancel")

        def resp(_d, r):
            if r == "this":
                run(ECal.ObjModType.THIS)
            elif r == "all":
                run(ECal.ObjModType.ALL)
            elif cancelled:
                cancelled()

        d.connect("response", resp)
        d.present(self)


class TaskDialog(Adw.Dialog):
    def __init__(self, win, task):
        super().__init__(title="할 일 편집", content_width=400)
        self.win, self.task = win, task
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        self.title = Adw.EntryRow(title="제목", text=task.summary)
        self.title.connect("entry-activated", lambda *_: self.save())
        self.has_due = Adw.SwitchRow(title="마감일", active=task.due is not None)
        self.due = DateButton(task.due or date.today())
        self.has_due.add_suffix(self.due)
        self.has_due.connect("notify::active", lambda *_: self.due.set_visible(self.has_due.get_active()))
        self.due.set_visible(task.due is not None)
        comp = ECal.Component.new_from_icalcomponent(task.icomp.clone())
        desc = comp.get_descriptions()
        self.memo = Adw.EntryRow(title="메모", text=text_of(desc[0]) if desc else "")
        for w in (self.title, self.has_due, self.memo):
            g.add(w)
        page.add(g)

        g2 = Adw.PreferencesGroup()
        delete = Gtk.Button(label="할 일 삭제", halign=Gtk.Align.CENTER)
        delete.add_css_class("destructive-action")
        delete.add_css_class("pill")
        delete.connect("clicked", lambda *_: win.delete_task(task, self.close))
        g2.add(delete)
        page.add(g2)

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="취소")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="저장")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self.save())
        header.pack_start(cancel)
        header.pack_end(save)
        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(header)
        self.set_child(tv)

    def save(self):
        comp = ECal.Component.new_from_icalcomponent(self.task.icomp.clone())
        comp.set_summary(ECal.ComponentText.new(self.title.get_text().strip() or "(제목 없음)", None))
        comp.set_due(comp_dt(self.due.value) if self.has_due.get_active() else None)
        memo = self.memo.get_text().strip()
        comp.set_descriptions([ECal.ComponentText.new(memo, None)] if memo else [])
        comp.commit_sequence()

        def done(err):
            if err:
                self.win.toast(f"저장 실패: {err}")
            else:
                self.close()

        self.win.store.modify(self.task.source_uid, comp, ECal.ObjModType.ALL, done)


class NotionSetupDialog(Adw.Dialog):
    """노션 통합 토큰 입력 + 연결 확인."""

    def __init__(self, win, then=None):
        super().__init__(title="노션 연결", content_width=460)
        self.win, self.then = win, then
        cfg = diary.load_config()
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(
            description=(
                "1. notion.so/profile/integrations 에서 새 통합(내부)을 만들고 토큰을 복사해요\n"
                "2. 노션 📕 일기 데이터베이스 ⋯ 메뉴 → 연결 → 방금 만든 통합 추가\n"
                "3. 아래에 토큰을 붙여넣고 연결 확인"
            )
        )
        self.token = Adw.PasswordEntryRow(title="통합 토큰", text=cfg.get("token", ""))
        self.token.connect("entry-activated", lambda *_: self.connect_check())
        g.add(self.token)
        page.add(g)
        self.status = Gtk.Label(wrap=True, margin_start=12, margin_end=12, margin_bottom=12)
        self.status.add_css_class("dim-label")
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="취소")
        cancel.connect("clicked", lambda *_: self.close())
        self.ok = Gtk.Button(label="연결 확인")
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", lambda *_: self.connect_check())
        header.pack_start(cancel)
        header.pack_end(self.ok)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        box.append(self.status)
        tv = Adw.ToolbarView(content=box)
        tv.add_top_bar(header)
        self.set_child(tv)

    def connect_check(self):
        token = self.token.get_text().strip()
        if not token:
            return
        cfg = diary.load_config()
        cfg["token"] = token
        self.ok.set_sensitive(False)
        self.status.set_label("확인하는 중…")

        def work():
            try:
                name, err = diary.check(cfg), None
            except diary.NotionError as e:
                name, err = None, str(e)
            GLib.idle_add(done, name, err)

        def done(name, err):
            self.ok.set_sensitive(True)
            if err:
                self.status.set_label(err)
                return False
            diary.save_config(cfg)
            self.win.toast(f"노션 '{name}' 에 연결했어요")
            self.close()
            if self.then:
                self.then()
            return False

        threading.Thread(target=work, daemon=True).start()


# ───────────────────────── 메인 창 ─────────────────────────

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="하루", default_width=1180, default_height=760)
        self.selected = date.today()
        self.month = date.today().replace(day=1)
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._color_key = None

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._build_calendar(), "calendar", "캘린더", "x-office-calendar-symbolic")
        self.stack.add_titled_with_icon(self._build_tasks(), "tasks", "할 일", "object-select-symbolic")
        self.stack.connect("notify::visible-child-name", lambda *_: self._sync_header())

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        self.add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="새 일정 (Ctrl+N)")
        self.add_btn.connect("clicked", lambda *_: self.new_event())
        header.pack_start(self.add_btn)
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-right-symbolic", tooltip_text="하루 일정 보기")
        header.pack_end(self.sidebar_btn)

        tv = Adw.ToolbarView(content=self.stack)
        tv.add_top_bar(header)
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 820sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)
        self.split.bind_property("show-sidebar", self.sidebar_btn, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.split.connect("notify::collapsed", lambda *_: self._sync_header())

        self._overrides = {}  # (캘린더, uid, rid) → 저장 중인 (시작, 끝)
        self._shortcuts()
        self.connect("close-request", lambda *_: self._flush_memo() and False)
        keys = Gtk.EventControllerKey()
        # 칸 버튼의 포커스 이동보다 먼저 받아서 방향키로 바로 날짜를 옮긴다
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.store = Store(self.refresh)
        self.store.on_error = self.toast
        # 자정이 지나면 '오늘' 표시를 갱신
        GLib.timeout_add_seconds(60, self._tick)
        self._today = date.today()

    # ── 구성 ──
    def _build_calendar(self):
        # 월 이동 바
        nav = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=8, margin_bottom=4)
        self.month_label = Gtk.Label(xalign=0, hexpand=True)
        self.month_label.add_css_class("title-1")
        prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="이전 달 (Page Up)")
        nxt = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="다음 달 (Page Down)")
        today = Gtk.Button(label="오늘", tooltip_text="오늘로 (Ctrl+T)")
        prev.connect("clicked", lambda *_: self.shift_month(-1))
        nxt.connect("clicked", lambda *_: self.shift_month(1))
        today.connect("clicked", lambda *_: self.go_today())
        for b in (prev, nxt, today):
            b.add_css_class("flat")
        nav.append(self.month_label)
        nav.append(today)
        nav.append(prev)
        nav.append(nxt)

        grid = Gtk.Grid(row_homogeneous=False, column_homogeneous=True, hexpand=True, vexpand=True,
                        margin_start=8, margin_end=8, margin_bottom=8)
        self.grid = grid
        self._drag = None
        drag = Gtk.GestureDrag()
        # 칸 버튼보다 먼저 받아서, 실제로 끌었을 때만 이벤트를 가져간다(그냥 클릭은 버튼으로)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        grid.add_controller(drag)
        for i, name in enumerate(["일", "월", "화", "수", "목", "금", "토"]):
            lab = Gtk.Label(label=name, margin_bottom=4)
            lab.add_css_class("caption-heading")
            if i == 0:
                lab.add_css_class("sun")
            elif i == 6:
                lab.add_css_class("sat")
            grid.attach(lab, i, 0, 1, 1)
        self.cells = []
        for r in range(6):
            for c in range(7):
                btn = Gtk.Button(hexpand=True, vexpand=True)
                btn.add_css_class("flat")
                btn.add_css_class("month-cell")
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.START)
                num = Gtk.Label(halign=Gtk.Align.START)
                num.add_css_class("day-num")
                chips = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                box.append(num)
                box.append(chips)
                btn.set_child(box)
                btn.connect("clicked", self._cell_clicked)
                dbl = Gtk.GestureClick()
                # 버튼 자체 클릭 제스처보다 먼저 받아야 두 번 클릭이 잡힌다
                dbl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                dbl.connect("pressed", self._cell_pressed, btn)
                btn.add_controller(dbl)
                btn.cell = {"num": num, "chips": chips, "date": None}
                grid.attach(btn, c, r + 1, 1, 1)
                self.cells.append(btn)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(nav)
        content.append(grid)

        # 오른쪽: 선택한 날 일정
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_start=16, margin_end=16, margin_top=16, margin_bottom=16)
        self.day_title = Gtk.Label(xalign=0)
        self.day_title.add_css_class("title-3")
        self.day_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.day_list.add_css_class("boxed-list")
        self.day_list.connect("row-activated", self._day_row_activated)
        add = Gtk.Button(label="일정 추가", halign=Gtk.Align.CENTER)
        add.add_css_class("pill")
        add.connect("clicked", lambda *_: self.new_event())

        # 일일 투두: 그 날짜가 마감인 할 일 (구글 할 일과 동기화)
        self.day_tasks_title = Gtk.Label(label="할 일", xalign=0, margin_top=8)
        self.day_tasks_title.add_css_class("heading")
        self.day_task_entry = Gtk.Entry(placeholder_text="이날 할 일 추가 후 Enter")
        self.day_task_entry.connect("activate", lambda e: self.add_task(e.get_text(), self.selected, e))
        self.day_tasks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.day_tasks.add_css_class("boxed-list")
        self.day_tasks.connect("row-activated", lambda _l, row: TaskDialog(self, row.task).present(self))

        # 메모: 그날 기록 (로컬 저장, 노션 일기에 같이 올라감)
        memo_title = Gtk.Label(label="메모", xalign=0, margin_top=8)
        memo_title.add_css_class("heading")
        self.memo_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
                                      left_margin=10, right_margin=10)
        self.memo_view.add_css_class("memo")
        self.memo_view.get_buffer().connect("changed", self._memo_changed)
        memo_frame = Gtk.Frame(child=self.memo_view, height_request=110)
        self._memo_day = None
        self._memo_timer = None
        self._memo_loading = False

        self.notion_btn = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=4,
                                     tooltip_text="할 일·일정·커밋·메모를 노션 📕 일기에 올려요")
        self.notion_btn.set_child(Adw.ButtonContent(icon_name="document-send-symbolic", label="노션 일기에 올리기"))
        self.notion_btn.add_css_class("pill")
        self.notion_btn.add_css_class("suggested-action")
        self.notion_btn.connect("clicked", lambda *_: self.upload_diary())

        for w in (self.day_title, self.day_list, add, self.day_tasks_title, self.day_task_entry,
                  self.day_tasks, memo_title, memo_frame, self.notion_btn):
            side.append(w)
        scroller = Gtk.ScrolledWindow(child=side, hscrollbar_policy=Gtk.PolicyType.NEVER)

        self.split = Adw.OverlaySplitView(content=content, sidebar=scroller, sidebar_position=Gtk.PackType.END,
                                          min_sidebar_width=300, max_sidebar_width=360)
        return self.split

    def _build_tasks(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=24, margin_bottom=24,
                      margin_start=12, margin_end=12)
        entry_box = Gtk.Box(spacing=6)
        self.task_entry = Gtk.Entry(placeholder_text="할 일 추가 후 Enter", hexpand=True)
        self.task_entry.connect("activate", lambda *_: self.add_task())
        self.task_due = DateButton(date.today())
        self.task_due_on = Gtk.CheckButton(label="마감", tooltip_text="마감일 지정")
        self.task_due_on.connect("toggled", lambda b: self.task_due.set_sensitive(b.get_active()))
        self.task_due.set_sensitive(False)
        self.task_list_combo = Gtk.DropDown(model=Gtk.StringList.new([]), tooltip_text="저장할 목록")
        for w in (self.task_entry, self.task_due_on, self.task_due, self.task_list_combo):
            entry_box.append(w)
        box.append(entry_box)

        self.task_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.task_listbox.add_css_class("boxed-list")
        self.task_listbox.connect("row-activated", lambda _l, row: TaskDialog(self, row.task).present(self))
        self.task_empty = Adw.StatusPage(icon_name="object-select-symbolic", title="할 일을 다 끝냈어요")
        self.task_empty.add_css_class("compact")
        box.append(self.task_listbox)
        box.append(self.task_empty)

        self.done_expander = Gtk.Expander()
        self.done_listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=8)
        self.done_listbox.add_css_class("boxed-list")
        self.done_listbox.connect("row-activated", lambda _l, row: TaskDialog(self, row.task).present(self))
        self.done_expander.set_child(self.done_listbox)
        box.append(self.done_expander)

        clamp = Adw.Clamp(child=box, maximum_size=720)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _shortcuts(self):
        app = self.get_application()
        for name, accels, fn in (
            ("new", ["<Control>n"], lambda *_: self.new_event() if self.stack.get_visible_child_name() == "calendar" else self.task_entry.grab_focus()),
            ("today", ["<Control>t"], lambda *_: self.go_today()),
            ("prev", ["Page_Up"], lambda *_: self.shift_month(-1)),
            ("next", ["Page_Down"], lambda *_: self.shift_month(1)),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", fn)
            self.add_action(act)
            app.set_accels_for_action(f"win.{name}", accels)

    # ── 동작 ──
    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=4))

    def _sync_header(self):
        self.add_btn.set_visible(self.stack.get_visible_child_name() == "calendar")
        self.sidebar_btn.set_visible(self.stack.get_visible_child_name() == "calendar" and self.split.get_collapsed())

    def _tick(self):
        if date.today() != self._today:
            self._today = date.today()
            self.refresh()
        return True

    def shift_month(self, n):
        y, m = self.month.year, self.month.month + n
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        self.month = date(y, m, 1)
        self.refresh()

    def go_today(self):
        self.select_day(date.today())

    def select_day(self, d):
        self.selected = d
        self.month = d.replace(day=1)
        self.refresh()

    def new_event(self):
        if not self.store.writable(ECal.ClientSourceType.EVENTS):
            self.toast("캘린더에 아직 연결 중이에요")
            return
        EventDialog(self, day=self.selected).present(self)

    def _cell_clicked(self, btn):
        d = btn.cell["date"]
        if d is None:  # 첫 그리기 전
            return
        if d.month != self.month.month:
            self.select_day(d)
        else:
            self.selected = d
            self._render_selection()
            self._render_day()
            if self.split.get_collapsed():
                self.split.set_show_sidebar(True)

    def _cell_pressed(self, gesture, n_press, _x, _y, btn):
        # 첫 클릭에서 다른 달 칸이면 달력이 다시 그려져 칸의 날짜가 바뀌므로, 첫 클릭 때 날짜를 기억한다
        if n_press == 1:
            self._pressed_date = btn.cell["date"]
        elif n_press == 2 and getattr(self, "_pressed_date", None):
            self.selected = self._pressed_date
            self.new_event()

    ARROWS = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -7, Gdk.KEY_Down: 7}

    def _on_key(self, _ctrl, keyval, _code, state):
        if keyval not in self.ARROWS or self.stack.get_visible_child_name() != "calendar":
            return False
        if self.get_visible_dialog() is not None or isinstance(self.get_focus(), (Gtk.Text, Gtk.Entry)):
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        d = self.selected + timedelta(days=self.ARROWS[keyval])
        if (d.year, d.month) != (self.month.year, self.month.month):
            self.select_day(d)
        else:
            self.selected = d
            self._render_selection()
            self._render_day()
        return True

    # ── 드래그: 일정 막대를 끌어 늘리기 / 빈 칸을 끌어 여러 날 일정 만들기 ──
    def _cell_at(self, x, y):
        """그리드 좌표 → (칸 버튼, 그 위의 일정 | None)"""
        w = self.grid.pick(x, y, Gtk.PickFlags.DEFAULT)
        event = None
        while w is not None and w is not self.grid:
            if event is None and getattr(w, "event", None) is not None:
                event = w.event
            if hasattr(w, "cell"):
                return w, event
            w = w.get_parent()
        return None, None

    @staticmethod
    def _drag_range(d):
        if d["event"] is not None:
            first, last = d["event"].days()
            t = d["target"]
            if d["origin"] == first and last > first:
                # 여러 날 일정의 첫날을 잡았으면 시작일을 움직인다 (오른쪽으로 끌면 줄어듦)
                return (t, last) if t <= last else (last, t)
            # 그 외(마지막 날·중간·하루짜리)는 끝나는 날을 움직인다 (왼쪽으로 끌면 줄어듦)
            return (first, t) if t >= first else (t, last)
        a, b = d["origin"], d["target"]
        return (a, b) if a <= b else (b, a)

    def _drag_begin(self, _g, x, y):
        btn, event = self._cell_at(x, y)
        self._drag = None
        if btn is not None and btn.cell["date"] is not None:
            self._drag = {"x": x, "y": y, "origin": btn.cell["date"], "target": btn.cell["date"],
                          "event": event, "active": False}

    def _drag_update(self, g, dx, dy):
        d = self._drag
        if d is None:
            return
        if not d["active"]:
            if dx * dx + dy * dy < 64:  # 8px 넘게 움직여야 드래그로 본다
                return
            d["active"] = True
            g.set_state(Gtk.EventSequenceState.CLAIMED)
        btn, _ = self._cell_at(d["x"] + dx, d["y"] + dy)
        if btn is None or (d.get("shown") == btn.cell["date"]):
            return
        d["target"] = d["shown"] = btn.cell["date"]
        a, b = self._drag_range(d)
        for cell in self.cells:
            on = a <= cell.cell["date"] <= b
            (cell.add_css_class if on else cell.remove_css_class)("drag-range")
        # 놓기 전에도 막대가 늘어난 모습을 보여준다
        ev = d["event"]
        if ev is not None:
            s, e = self._stretched(ev, a, b)
            self._preview = replace(ev, start=s, end=e)
            events = [self._preview if x is ev else x for x in self.events]
        else:
            cals = self.store.writable(ECal.ClientSourceType.EVENTS)
            self._preview = Event(uid="", rid="", source_uid=cals[0] if cals else "", summary="새 일정",
                                  location="", start=a, end=b + timedelta(days=1), all_day=True,
                                  icomp=None, recurring=False)
            events = self.events + [self._preview]
        self._render_grid(events)

    def _drag_end(self, _g, _dx, _dy):
        d, self._drag = self._drag, None
        for cell in self.cells:
            cell.remove_css_class("drag-range")
        if d is None or not d["active"]:
            return
        a, b = self._drag_range(d)
        if d["event"] is None or (a, b) == d["event"].days():
            # 새 일정은 창에서 저장할 때 생기고, 안 바뀐 일정은 원래대로 되돌린다
            self._preview = None
            self._render_grid(self.events)
        if d["event"] is not None:
            self.stretch(d["event"], a, b)
        elif not self.store.writable(ECal.ClientSourceType.EVENTS):
            self.toast("캘린더에 아직 연결 중이에요")
        else:
            self.selected = a
            EventDialog(self, day=a, span=(a, b)).present(self)

    @staticmethod
    def _stretched(ev, first, last):
        """first~last 날짜에 걸치도록 바꾼 (시작, 끝) — 시각은 그대로."""
        if ev.all_day:
            return first, last + timedelta(days=1)
        s = datetime.combine(first, ev.start.time())
        if ev.end.time() == time(0) and ev.end.date() > ev.start.date():
            e = datetime.combine(last + timedelta(days=1), time(0))
        else:
            e = datetime.combine(last, ev.end.time())
        if e <= s:
            e = s + timedelta(hours=1)
        return s, e

    def stretch(self, ev, first, last):
        """일정을 first~last 날짜에 걸치도록 바꾼다 (시각은 그대로)."""
        if (first, last) == ev.days():
            return
        if ev.source_uid not in self.store.writable(ECal.ClientSourceType.EVENTS):
            self.toast("읽기 전용 캘린더라 바꿀 수 없어요")
            self.refresh()
            return
        s, e = self._stretched(ev, first, last)
        comp = ECal.Component.new_from_icalcomponent(ev.icomp.clone())
        mod = ECal.ObjModType.ALL
        if ev.recurring:
            # 반복 일정은 끌어 놓은 그 회차만 바꾼다
            mod = ECal.ObjModType.THIS
            comp.set_rrules(None)
            comp.set_rdates(None)
            comp.set_exrules(None)
            comp.set_exdates(None)
        comp.set_dtstart(comp_dt(s))
        comp.set_dtend(comp_dt(e))
        comp.commit_sequence()

        # 구글 저장은 서버까지 다녀와서 1초 남짓 걸린다. 그동안 바뀐 길이를 먼저 보여준다
        key = (ev.source_uid, ev.uid, ev.rid)
        self._overrides[key] = (s, e)
        self.refresh()

        def done(err):
            self._overrides.pop(key, None)
            if err:
                self.toast(f"변경 실패: {err}")
                self.refresh()  # 원래 길이로 되돌린다
            elif ev.recurring:
                self.toast("반복 일정은 이 회차만 바꿨어요")

        self.store.modify(ev.source_uid, comp, mod, done)

    def _day_row_activated(self, _l, row):
        if getattr(row, "event", None):
            EventDialog(self, event=row.event).present(self)

    # ── 할 일 ──
    def add_task(self, text=None, due=None, entry=None):
        """할 일 추가. 인자 없이 부르면 할 일 탭의 입력칸을 쓴다."""
        if text is None:
            entry = self.task_entry
            text = entry.get_text()
            due = self.task_due.value if self.task_due_on.get_active() else None
            lists = self.store.writable(ECal.ClientSourceType.TASKS)
            src = lists[min(self.task_list_combo.get_selected(), len(lists) - 1)] if lists else None
        else:
            lists = self.store.writable(ECal.ClientSourceType.TASKS)
            src = lists[0] if lists else None
        text = text.strip()
        if not text:
            return
        if src is None:
            self.toast("할 일 목록에 아직 연결 중이에요")
            return
        comp = ECal.Component.new()
        comp.set_new_vtype(ECal.ComponentVType.TODO)
        comp.set_uid(str(uuid.uuid4()))
        comp.set_summary(ECal.ComponentText.new(text, None))
        comp.set_status(ICalGLib.PropertyStatus.NEEDSACTION)
        if due:
            comp.set_due(comp_dt(due))
        comp.commit_sequence()
        entry.set_text("")
        self.store.create(src, comp, lambda err: err and self.toast(f"추가 실패: {err}"))

    def toggle_task(self, task, done):
        comp = ECal.Component.new_from_icalcomponent(task.icomp.clone())
        if done:
            comp.set_status(ICalGLib.PropertyStatus.COMPLETED)
            comp.set_percent_complete(100)
            comp.set_completed(ICalGLib.Time.new_current_with_zone(ICalGLib.Timezone.get_utc_timezone()))
        else:
            comp.set_status(ICalGLib.PropertyStatus.NEEDSACTION)
            comp.set_percent_complete(0)
            comp.set_completed(None)
        comp.commit_sequence()
        self.store.modify(task.source_uid, comp, ECal.ObjModType.ALL,
                          lambda err: err and self.toast(f"변경 실패: {err}"))

    def delete_task(self, task, after=None):
        def done(err):
            if err:
                self.toast(f"삭제 실패: {err}")
            elif after:
                after()
        self.store.remove(task.source_uid, task.uid, None, ECal.ObjModType.ALL, done)

    def _task_row(self, task):
        row = Adw.ActionRow(title=GLib.markup_escape_text(task.summary), activatable=True)
        row.task = task
        check = Gtk.CheckButton(active=task.done, valign=Gtk.Align.CENTER)
        check.add_css_class("selection-mode")
        check.connect("toggled", lambda b: self.toggle_task(task, b.get_active()))
        row.add_prefix(check)
        if task.done:
            row.add_css_class("dim-label")
        subtitle = []
        if task.due:
            delta = (task.due - date.today()).days
            label = {0: "오늘", 1: "내일", -1: "어제"}.get(delta, fmt_day(task.due))
            subtitle.append(label)
            if delta < 0 and not task.done:
                row.add_css_class("overdue")
        if len(self.store.writable(ECal.ClientSourceType.TASKS)) > 1:
            subtitle.append(self.store.name(task.source_uid))
        row.set_subtitle(GLib.markup_escape_text(" · ".join(subtitle)))
        rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="삭제")
        rm.add_css_class("flat")
        rm.connect("clicked", lambda *_: self.delete_task(task))
        row.add_suffix(rm)
        return row

    # ── 그리기 ──
    def _update_colors(self):
        key = tuple(sorted(self.store.colors.items()))
        if key == self._color_key:
            return
        self._color_key = key
        self._color_class = {}
        css = []
        for i, (uid, color) in enumerate(sorted(self.store.colors.items())):
            self._color_class[uid] = f"c{i}"
            css.append(f".chip.c{i} {{ background-color: alpha({color}, .22); border-left: 3px solid {color}; }}")
            css.append(f".bar.c{i} {{ background-color: {color}; }}")
        self.css.load_from_string(BASE_CSS + "\n".join(css))

    def refresh(self):
        self._update_colors()
        first = self.month
        start = first - timedelta(days=(first.weekday() + 1) % 7)  # 일요일 시작
        end = start + timedelta(days=42)
        self.events = self.store.events(start, end)
        if self._overrides:  # 저장 중인 드래그 결과를 먼저 반영
            self.events = [
                replace(e, start=self._overrides[k][0], end=self._overrides[k][1])
                if (k := (e.source_uid, e.uid, e.rid)) in self._overrides else e
                for e in self.events
            ]
        self.all_tasks = self.store.tasks()
        self.month_label.set_label(f"{first.year}년 {first.month}월")
        self._grid_start = start
        self._preview = None
        self._render_grid(self.events)
        self._render_selection()
        self._render_day()
        self._render_tasks()
        self._sync_header()

    def _render_grid(self, events):
        """달력 칸을 그린다. 드래그 중에는 미리보기 일정이 섞인 목록으로 다시 그린다."""
        start, first = self._grid_start, self.month
        today = date.today()
        lanes = {}  # (일정 id, 주 번호) → 줄 번호
        for w in range(6):
            wf = start + timedelta(days=7 * w)
            wl = wf + timedelta(days=6)
            week = []
            for e in events:
                ef, el = e.days()
                if ef <= wl and el >= wf:
                    week.append((e, ef, el))
            # 여러 날·하루 종일 → 긴 것 → 이른 시각 순으로 위에서부터 줄 배정
            week.sort(key=lambda x: (0 if (x[0].all_day or x[2] > x[1]) else 1, x[1], -(x[2] - x[1]).days,
                                     time() if x[0].all_day else x[0].start.time(), x[0].summary))
            used = {}
            for e, ef, el in week:
                cols = range((max(ef, wf) - wf).days, (min(el, wl) - wf).days + 1)
                lane = 0
                while any((lane, c) in used for c in cols):
                    lane += 1
                for c in cols:
                    used[(lane, c)] = True
                lanes[(id(e), w)] = lane

        MAX_LANES = 3
        for i, btn in enumerate(self.cells):
            d = start + timedelta(days=i)
            w = i // 7
            cell = btn.cell
            cell["date"] = d
            num = cell["num"]
            num.set_label(str(d.day))
            day_events = [e for e in events if e.covers(d)]
            holiday = any(e.day_off for e in day_events)
            for cls, on in (
                ("today", d == today),
                ("sun", d.weekday() == 6 or holiday),
                ("sat", d.weekday() == 5 and not holiday),
            ):
                (num.add_css_class if on else num.remove_css_class)(cls)
            (btn.add_css_class if d.month != first.month else btn.remove_css_class)("other-month")

            chips = cell["chips"]
            while (c := chips.get_first_child()) is not None:
                chips.remove(c)
            by_lane = {lanes[(id(e), w)]: e for e in day_events}
            visible = [ln for ln in by_lane if ln < MAX_LANES]
            hidden = len(by_lane) - len(visible)
            for ln in range(max(visible) + 1 if visible else 0):
                e = by_lane.get(ln)
                if e is None:  # 다른 날의 막대 줄을 맞추기 위한 빈 자리
                    lab = Gtk.Label(label=" ", hexpand=True)
                    lab.add_css_class("chip")
                    lab.add_css_class("gap")
                    chips.append(lab)
                    continue
                ef, el = e.days()
                multi = el > ef
                if multi:
                    # 이어진 막대: 첫날이나 주의 첫 칸에만 제목
                    text = e.summary if (d == ef or d.weekday() == 6) else " "
                else:
                    text = e.summary if e.all_day else f"{e.start:%H:%M} {e.summary}"
                lab = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1, hexpand=True)
                lab.add_css_class("chip")
                lab.add_css_class(self._color_class.get(e.source_uid, "c0"))
                if d > ef:
                    lab.add_css_class("cont-left")
                if d < el:
                    lab.add_css_class("cont-right")
                lab.event = e
                if e is self._preview:
                    lab.add_css_class("dragging")
                lab.set_cursor_from_name("grabbing" if e is self._preview else "grab")
                lab.set_tooltip_text(f"{e.summary}\n끌어서 기간 늘리기")
                chips.append(lab)
            if hidden:
                more = Gtk.Label(label=f"+{hidden}개", xalign=0)
                more.add_css_class("more")
                chips.append(more)
            btn.set_tooltip_text("\n".join(
                (e.summary if e.all_day else f"{fmt_time(e.start)} {e.summary}") for e in day_events) or None)

    def _render_selection(self):
        for btn in self.cells:
            (btn.add_css_class if btn.cell["date"] == self.selected else btn.remove_css_class)("selected-day")

    def _render_day(self):
        d = self.selected
        rel = {0: " · 오늘", 1: " · 내일", -1: " · 어제"}.get((d - date.today()).days, "")
        self.day_title.set_label(f"{d.month}월 {d.day}일 {WEEKDAYS[d.weekday()]}요일{rel}")
        self.day_list.remove_all()
        evs = [e for e in getattr(self, "events", []) if e.covers(d)]
        if not evs:
            row = Adw.ActionRow(title="일정 없음", subtitle="두 번 클릭하거나 Ctrl+N 으로 추가")
            row.add_css_class("dim-label")
            self.day_list.append(row)
        for e in evs:
            if e.all_day:
                when = "하루 종일"
            else:
                when = f"{fmt_time(e.start)} – {fmt_time(e.end)}"
                if e.end.date() != e.start.date():
                    when = f"{fmt_day(e.start.date())} {fmt_time(e.start)} – {fmt_day(e.end.date())} {fmt_time(e.end)}"
            sub = " · ".join(x for x in (when, e.location) if x)
            row = Adw.ActionRow(title=GLib.markup_escape_text(e.summary), subtitle=GLib.markup_escape_text(sub),
                                activatable=True)
            bar = Gtk.Box(width_request=4, valign=Gtk.Align.FILL, margin_top=6, margin_bottom=6)
            bar.add_css_class("bar")
            bar.add_css_class(self._color_class.get(e.source_uid, "c0"))
            row.add_prefix(bar)
            if e.recurring:
                row.add_suffix(Gtk.Image(icon_name="view-refresh-symbolic", tooltip_text="반복 일정"))
            row.event = e
            self.day_list.append(row)

        self.day_tasks.remove_all()
        due = [t for t in getattr(self, "all_tasks", []) if t.due == d]
        self.day_tasks.set_visible(bool(due))
        for t in due:
            self.day_tasks.append(self._task_row(t))
        self._load_memo(d)

    # ── 메모 ──
    def _load_memo(self, d):
        if d == self._memo_day:
            return
        self._flush_memo()
        self._memo_day = d
        self._memo_loading = True
        self.memo_view.get_buffer().set_text(diary.read_memo(d))
        self._memo_loading = False

    def _memo_changed(self, _buf):
        if self._memo_loading:
            return
        if self._memo_timer:
            GLib.source_remove(self._memo_timer)
        self._memo_timer = GLib.timeout_add(600, self._flush_memo)

    def _flush_memo(self):
        if self._memo_timer:
            GLib.source_remove(self._memo_timer)
            self._memo_timer = None
        if self._memo_day is not None:
            buf = self.memo_view.get_buffer()
            diary.write_memo(self._memo_day, buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        return False

    # ── 노션 일기 ──
    def upload_diary(self):
        cfg = diary.load_config()
        if not cfg.get("token"):
            NotionSetupDialog(self, then=self.upload_diary).present(self)
            return
        self._flush_memo()
        d = self.selected
        # GI 객체는 메인 스레드에서만 다룬다: 할 일·일정은 여기서 문자열로 만들어 넘긴다
        todos = [(t.summary, t.done) for t in self.all_tasks if t.due == d]
        todos += [(t.summary, True) for t in self.all_tasks if t.done and t.completed == d and t.due != d]
        events = []
        for e in self.store.events(d, d + timedelta(days=1)):
            if not e.covers(d) or self.store.is_holiday_source(e.source_uid) or e.source_uid == "birthdays":
                continue
            when = "하루 종일" if e.all_day else f"{e.start:%H:%M}–{e.end:%H:%M}"
            events.append(f"{when}  {e.summary}" + (f"  @{e.location}" if e.location else ""))
        memo = diary.read_memo(d)
        self.notion_btn.set_sensitive(False)

        def work():
            try:
                blocks = diary.build_entry(d, todos, events, diary.commits(d, cfg["project_root"]), memo)
                if not blocks:
                    raise diary.NotionError("올릴 내용이 없어요")
                url, err = diary.push(cfg, d, blocks), None
            except diary.NotionError as e:
                url, err = None, str(e)
            except Exception as e:  # 예상 못 한 오류도 버튼이 멈추지 않게
                url, err = None, f"오류: {e}"
            GLib.idle_add(finish, url, err)

        def finish(url, err):
            self.notion_btn.set_sensitive(True)
            if err:
                self.toast(err)
                return False
            t = Adw.Toast(title=f"{diary.title_for(d)} 일기를 올렸어요", button_label="열기", timeout=6)
            t.connect("button-clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(url, None))
            self.toasts.add_toast(t)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _render_tasks(self):
        lists = self.store.writable(ECal.ClientSourceType.TASKS)
        names = [self.store.name(u) for u in lists]
        model = self.task_list_combo.get_model()
        if [model.get_string(i) for i in range(model.get_n_items())] != names:
            cur = self.task_list_combo.get_selected()
            self.task_list_combo.set_model(Gtk.StringList.new(names))
            self.task_list_combo.set_selected(cur if cur < len(names) else 0)
        self.task_list_combo.set_visible(len(names) > 1)

        self.task_listbox.remove_all()
        self.done_listbox.remove_all()
        todo = [t for t in self.all_tasks if not t.done]
        done = [t for t in self.all_tasks if t.done]
        for t in todo:
            self.task_listbox.append(self._task_row(t))
        for t in done:
            self.done_listbox.append(self._task_row(t))
        self.task_listbox.set_visible(bool(todo))
        self.task_empty.set_visible(not todo)
        self.done_expander.set_label(f"완료됨 ({len(done)})")
        self.done_expander.set_visible(bool(done))


BASE_CSS = """
.month-cell { padding: 4px 0; margin: 0; border-radius: 10px; min-height: 64px; }
.month-cell .day-num { margin-left: 4px; }
.month-cell.drag-range { background-color: alpha(@accent_bg_color, .10); }
.month-cell .chip.dragging { box-shadow: inset 0 1px alpha(@accent_bg_color, .9), inset 0 -1px alpha(@accent_bg_color, .9); font-weight: 700; }
.month-cell.other-month { opacity: .45; }
.month-cell.selected-day { background-color: alpha(@accent_bg_color, .14); box-shadow: inset 0 0 0 2px alpha(@accent_bg_color, .55); }
.day-num { font-weight: 700; font-feature-settings: "tnum"; min-width: 24px; min-height: 24px; border-radius: 12px; padding: 0 2px; }
.day-num.today { background-color: @accent_bg_color; color: @accent_fg_color; }
.sun { color: #e5484d; }
.sat { color: #3584e4; }
.day-num.today.sun, .day-num.today.sat { color: @accent_fg_color; }
.chip { font-size: 0.82em; padding: 1px 5px; border-radius: 4px; margin: 0 4px; }
.month-cell .chip.gap { background: none; border: none; }
.month-cell .chip.cont-left { margin-left: 0; border-left: none; border-top-left-radius: 0; border-bottom-left-radius: 0; }
.month-cell .chip.cont-right { margin-right: 0; border-top-right-radius: 0; border-bottom-right-radius: 0; }
.more { font-size: 0.78em; opacity: .7; padding-left: 4px; }
.bar { border-radius: 2px; min-width: 4px; }
row.overdue .subtitle { color: #e5484d; }
textview.memo, textview.memo text { background: transparent; }
"""


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
