"""일정·할 일 편집 창, 노션 연결 창."""
import threading
import uuid
from datetime import date, datetime, time, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import Adw, ECal, GLib, Gtk, ICalGLib  # noqa: E402

from . import diary  # noqa: E402
from .core import (  # noqa: E402
    REMINDERS, DEFAULT_REMINDER, REPEATS, CUSTOM_REPEAT, KEEP, ical_to_py, comp_dt, as_date,
    text_of, parse_rule, repeat_index, rule_until, build_rule, reminder_index, set_reminder, rule_start_problem,
)
from .widgets import DateButton, TimeEntry, combo_row  # noqa: E402


class EventDialog(Adw.Dialog):
    def __init__(self, win, day=None, event=None, span=None, times=None):
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
            if times:  # 주간 보기에서 시간대를 끌어 고른 경우
                start, end, all_day = times[0], times[1], False

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
        rule, series_start = {}, sd
        if event is not None and event.recurring:
            master = self.store.master(event.source_uid, event.uid)
            rrules = master.get_rrules() if master else None
            rule = parse_rule(rrules[0].to_string()) if rrules else {"X-UNKNOWN": "1"}
            if master is not None:
                series_start = as_date(ical_to_py(master.get_dtstart().get_value()))
        idx = repeat_index(rule, series_start)
        self._all_day0 = all_day
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
        if event is None and span is None and times is None:
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
        if not self.repeat_end.get_active():  # 아직 안 고른 반복 종료일 기본값도 따라간다
            self.until.set_date(self.until.value + (d - self._start_prev))
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
        if r_idx is not None and self._until0 and self.all_day.get_active() != self._all_day0:
            rule_changed = True  # UNTIL 형식(날짜/시각)이 DTSTART 와 같아야 해서 규칙을 다시 만든다
        if r_idx and r_until is not None and r_until < as_date(s):
            self.win.toast("반복 종료일이 시작일보다 빨라요")
            return
        if rule_changed and (problem := rule_start_problem(r_idx, s)):
            self.win.toast(problem)
            return

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
                if r_idx == 0:  # 반복을 끄면 지금 연 회차 자리에 일정 하나만 남긴다
                    # 따로 떼어냈던 회차들도 지운다 — 안 그러면 다른 앱(폰·상단바)에 떨어진 일정으로 남는다
                    self.store.turn_off_repeat(src, fill(master, s, e, ""), done)
                    return
                # 규칙을 새로 만들면 요일·날짜도 새 시작일 기준이라 날짜 이동도 가능
                m_day = as_date(ical_to_py(master.get_dtstart().get_value())) + shift
                ms = m_day if not isinstance(s, datetime) else datetime.combine(m_day, s.time())
                if problem := rule_start_problem(r_idx, ms):
                    done(problem)
                    return
                self.store.modify(src, fill(master, ms, ms + (e - s), build_rule(r_idx, ms, r_until)), mod, done)
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
            self.store.remove(ev.source_uid, ev.uid, ev.rid if mod == ECal.ObjModType.THIS else None, mod, done,
                              label="삭제했어요")

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
                "2. 노션 일기 데이터베이스(🎒 일기) ⋯ 메뉴 → 연결 → 방금 만든 통합 추가\n"
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
        self._closed = False
        self.connect("closed", lambda *_: setattr(self, "_closed", True))

    def connect_check(self):
        token = self.token.get_text().strip()
        if not token:
            return
        if not token.isascii() or any(c.isspace() for c in token):
            self.status.set_label("토큰에 들어갈 수 없는 글자가 있어요. 다시 복사해 붙여넣어 주세요")
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
            except Exception as e:  # 예상 못 한 오류에도 버튼이 멈추지 않게
                name, err = None, f"확인 실패: {e}"
            GLib.idle_add(done, name, err)

        def done(name, err):
            if self._closed:  # 취소했으면 저장도, 이어서 올리기도 하지 않는다
                return False
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
