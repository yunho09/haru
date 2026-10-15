#!/usr/bin/python3
"""하루 — GNOME 일정·할 일 관리 앱.

저장소는 Evolution Data Server(EDS)를 그대로 쓴다. 그래서 GNOME 온라인 계정에
등록한 구글 캘린더/할 일과 자동으로 동기화되고, 알림은 evolution-alarm-notify
데몬이 앱이 꺼져 있어도 띄워준다.
"""
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("EDataServer", "1.2")
gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import Adw, ECal, EDataServer, Gdk, Gio, GLib, GObject, Gtk, ICalGLib, Pango  # noqa: E402

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

    def covers(self, d):
        if self.all_day:
            return self.start <= d < max(self.end, self.start + timedelta(days=1))
        s, e = self.start.date(), self.end.date()
        if self.end.time() == time(0, 0) and e > s:
            e -= timedelta(days=1)  # 자정에 끝나면 그 날은 포함 안 함
        return s <= d <= e


@dataclass
class Task:
    uid: str
    source_uid: str
    summary: str
    due: object  # date | None
    done: bool
    icomp: object


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
                out.append(Task(
                    uid=comp.get_uid(), source_uid=uid,
                    summary=text_of(comp.get_summary()) or "(제목 없음)",
                    due=d, done=comp.get_status() == ICalGLib.PropertyStatus.COMPLETED,
                    icomp=comp.get_icalcomponent(),
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
    def __init__(self, win, day=None, event=None):
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
        for w in (self.all_day, r_start, r_end):
            g2.add(w)
        page.add(g2)

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
        self.set_focus(self.title)

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

        def fill(comp, cs, ce):
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
            self.store.create(src, fill(comp, s, e), done)
            return

        ev = self.event
        if not ev.recurring:
            comp = fill(ECal.Component.new_from_icalcomponent(ev.icomp.clone()), s, e)
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
            if shift.days != 0:
                # 반복 규칙(예: 매주 월요일)은 그대로라 날짜만 옮기면 일정이 깨진다
                done("모든 반복의 날짜는 바꿀 수 없어요. 시간·제목 등만 바꾸거나 '이 일정만'을 고르세요")
                return
            m_start = ical_to_py(master.get_dtstart().get_value())
            m_day = as_date(m_start)
            ms = m_day if not isinstance(s, datetime) else datetime.combine(m_day, s.time())
            self.store.modify(src, fill(master, ms, ms + (e - s)), mod, done)

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

        self._shortcuts()
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
        self.day_tasks_title = Gtk.Label(label="이날 마감인 할 일", xalign=0)
        self.day_tasks_title.add_css_class("heading")
        self.day_tasks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.day_tasks.add_css_class("boxed-list")
        self.day_tasks.connect("row-activated", lambda _l, row: TaskDialog(self, row.task).present(self))
        add = Gtk.Button(label="일정 추가", halign=Gtk.Align.CENTER)
        add.add_css_class("pill")
        add.connect("clicked", lambda *_: self.new_event())
        for w in (self.day_title, self.day_list, self.day_tasks_title, self.day_tasks, add):
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

    def _day_row_activated(self, _l, row):
        if getattr(row, "event", None):
            EventDialog(self, event=row.event).present(self)

    # ── 할 일 ──
    def add_task(self):
        text = self.task_entry.get_text().strip()
        lists = self.store.writable(ECal.ClientSourceType.TASKS)
        if not text or not lists:
            return
        src = lists[min(self.task_list_combo.get_selected(), len(lists) - 1)]
        comp = ECal.Component.new()
        comp.set_new_vtype(ECal.ComponentVType.TODO)
        comp.set_uid(str(uuid.uuid4()))
        comp.set_summary(ECal.ComponentText.new(text, None))
        comp.set_status(ICalGLib.PropertyStatus.NEEDSACTION)
        if self.task_due_on.get_active():
            comp.set_due(comp_dt(self.task_due.value))
        comp.commit_sequence()
        self.task_entry.set_text("")
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
        self.all_tasks = self.store.tasks()
        self.month_label.set_label(f"{first.year}년 {first.month}월")

        today = date.today()
        for i, btn in enumerate(self.cells):
            d = start + timedelta(days=i)
            cell = btn.cell
            cell["date"] = d
            num = cell["num"]
            num.set_label(str(d.day))
            day_events = [e for e in self.events if e.covers(d)]
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
            shown = day_events[:3]
            for e in shown:
                text = e.summary if e.all_day else f"{e.start:%H:%M} {e.summary}"
                lab = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1, hexpand=True)
                lab.add_css_class("chip")
                lab.add_css_class(self._color_class.get(e.source_uid, "c0"))
                chips.append(lab)
            if len(day_events) > 3:
                more = Gtk.Label(label=f"+{len(day_events) - 3}개", xalign=0)
                more.add_css_class("more")
                chips.append(more)
            btn.set_tooltip_text("\n".join(
                (e.summary if e.all_day else f"{fmt_time(e.start)} {e.summary}") for e in day_events) or None)
        self._render_selection()
        self._render_day()
        self._render_tasks()
        self._sync_header()

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
        self.day_tasks_title.set_visible(bool(due))
        self.day_tasks.set_visible(bool(due))
        for t in due:
            self.day_tasks.append(self._task_row(t))

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
.month-cell { padding: 4px; margin: 1px; border-radius: 10px; min-height: 64px; }
.month-cell.other-month { opacity: .45; }
.month-cell.selected-day { background-color: alpha(@accent_bg_color, .14); box-shadow: inset 0 0 0 2px alpha(@accent_bg_color, .55); }
.day-num { font-weight: 700; font-feature-settings: "tnum"; min-width: 24px; min-height: 24px; border-radius: 12px; padding: 0 2px; }
.day-num.today { background-color: @accent_bg_color; color: @accent_fg_color; }
.sun { color: #e5484d; }
.sat { color: #3584e4; }
.day-num.today.sun, .day-num.today.sat { color: @accent_fg_color; }
.chip { font-size: 0.82em; padding: 1px 5px; border-radius: 4px; }
.more { font-size: 0.78em; opacity: .7; padding-left: 4px; }
.bar { border-radius: 2px; min-width: 4px; }
row.overdue .subtitle { color: #e5484d; }
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
