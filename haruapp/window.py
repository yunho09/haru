"""메인 창: 월 보기, 사이드바(일정·할 일·메모·노션), 할 일 탭, 드래그, 되돌리기."""
import threading
import uuid
from dataclasses import replace
from datetime import date, time, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, ECal, Gdk, Gio, GLib, GObject, Graphene, Gtk, ICalGLib, Pango  # noqa: E402

from . import diary  # noqa: E402
from .core import WEEKDAYS, Event, assign_lanes, comp_dt, drag_range, fmt_day, fmt_time, stretched_times  # noqa: E402
from .log import log  # noqa: E402
from .store import Store  # noqa: E402
from .widgets import DateButton  # noqa: E402
from .dialogs import EventDialog, TaskDialog, NotionSetupDialog  # noqa: E402
from .weekview import WeekView  # noqa: E402


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

        # 캘린더 연결이 오래 안 될 때 위에 뜨는 띠
        self.banner = Adw.Banner(button_label="다시 연결", revealed=False)
        self.banner.connect("button-clicked", lambda *_: self._restart_calendars())

        tv = Adw.ToolbarView(content=self.stack)
        tv.add_top_bar(header)
        tv.add_top_bar(self.banner)
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
        self.store.on_undoable = self._push_undo
        self.store.on_slow = self._on_slow_connect
        self.store.on_all_connected = lambda: self.banner.set_revealed(False)
        self._undo = []  # [(제목, 되돌릴 목록)]
        self._undo_toast = None
        self._restoring = False
        undo_keys = Gtk.EventControllerKey()  # 버블 단계: 입력칸이 자기 Ctrl+Z 를 먼저 쓴다
        undo_keys.connect("key-pressed", self._on_undo_key)
        self.add_controller(undo_keys)
        # 자정이 지나면 '오늘' 표시를 갱신
        GLib.timeout_add_seconds(60, self._tick)
        self._today = date.today()

    # ── 구성 ──
    def _build_calendar(self):
        # 월 이동 바
        nav = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=8, margin_bottom=4)
        self.month_label = Gtk.Label(xalign=0, hexpand=True)
        self.month_label.add_css_class("title-1")
        prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="이전 (Page Up)")
        nxt = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="다음 (Page Down)")
        today = Gtk.Button(label="오늘", tooltip_text="오늘로 (Ctrl+T)")
        prev.connect("clicked", lambda *_: self.shift_period(-1))
        nxt.connect("clicked", lambda *_: self.shift_period(1))
        self.view_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.view_toggle.add(Adw.Toggle(name="month", label="월"))
        self.view_toggle.add(Adw.Toggle(name="week", label="주"))
        self.view_toggle.set_active_name("month")
        self.view_toggle.connect("notify::active-name", lambda *_: self.set_view(self.view_toggle.get_active_name()))
        today.connect("clicked", lambda *_: self.go_today())
        for b in (prev, nxt, today):
            b.add_css_class("flat")
        nav.append(self.month_label)
        nav.append(self.view_toggle)
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

        self.week = WeekView(self)
        # 끄는 동안 마우스를 따라다니는 막대
        self.float_chip = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, halign=Gtk.Align.START,
                                    valign=Gtk.Align.START, can_target=False, visible=False)
        self.float_chip.add_css_class("chip")
        self.float_chip.add_css_class("floating")
        self.month_overlay = Gtk.Overlay(child=grid)
        self.month_overlay.add_overlay(self.float_chip)
        self.view_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.view_stack.add_named(self.month_overlay, "month")
        self.view_stack.add_named(self.week, "week")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(nav)
        content.append(self.view_stack)

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
        self._memo_dirty = False

        self.notion_btn = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=4,
                                     tooltip_text="할 일·일정·커밋·메모를 노션 일기에 올려요")
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
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", fn)
            self.add_action(act)
            app.set_accels_for_action(f"win.{name}", accels)

    # ── 동작 ──
    # ── 되돌리기 ──
    def _push_undo(self, label, summary, entries):
        entry = (summary, entries)
        self._undo.append(entry)
        del self._undo[:-30]
        if self._undo_toast is not None:  # 다른 변경이 생기면 이전 알림의 되돌리기는 거둔다
            self._undo_toast.dismiss()
        if label:  # 실수하기 쉬운 동작은 바로 되돌릴 수 있게 알림에 버튼
            t = Adw.Toast(title=f"‘{summary}’ {label}" if summary else label, button_label="되돌리기", timeout=6)
            t.set_use_markup(False)
            t.connect("button-clicked", lambda *_: self.undo(entry))
            t.connect("dismissed", lambda t: setattr(self, "_undo_toast", None) if self._undo_toast is t else None)
            self._undo_toast = t
            self.toasts.add_toast(t)

    def _on_undo_key(self, _c, keyval, _code, state):
        if isinstance(self.get_focus(), (Gtk.Text, Gtk.TextView)):
            return False  # 입력칸에서는 글자 되돌리기만 (되돌릴 글자가 없어도 일정은 건드리지 않는다)
        if keyval in (Gdk.KEY_z, Gdk.KEY_Z) and state & Gdk.ModifierType.CONTROL_MASK \
                and not state & Gdk.ModifierType.SHIFT_MASK and self.get_visible_dialog() is None:
            self.undo()
            return True
        return False

    def undo(self, entry=None):
        """맨 위 변경을 되돌린다. entry 가 주어지면(알림 버튼) 그게 맨 위일 때만."""
        if self._undo_toast is not None:
            self._undo_toast.dismiss()
        if self._restoring or self.store.busy:
            self.toast("아직 저장하는 중이에요. 잠시 후 다시 해주세요")
            return
        if not self._undo:
            self.toast("되돌릴 게 없어요")
            return
        if entry is not None and self._undo[-1] is not entry:
            self.toast("그 뒤에 바뀐 게 있어요. Ctrl+Z로 차례로 되돌려 주세요")
            return
        summary, entries = item = self._undo.pop()
        self._restoring = True

        def done(err):
            self._restoring = False
            if err:
                self._undo.append(item)  # 실패하면 다시 시도할 수 있게 남겨 둔다
                self.toast(f"되돌리기 실패: {err}")
            else:
                self.toast(f"‘{summary}’ 되돌렸어요" if summary else "되돌렸어요")

        self.store.restore(entries, done)

    # ── 캘린더 연결 지연 ──
    def _on_slow_connect(self, names):
        self.banner.set_title(f"{', '.join(names)} 연결이 늦어요. 캘린더 서비스가 멈췄을 수 있어요")
        self.banner.set_button_label("다시 연결")
        self.banner.set_revealed(True)

    def _restart_calendars(self):
        self.banner.set_title("캘린더 서비스를 다시 띄우는 중…")
        self.banner.set_button_label(None)

        def done(err):
            if err:
                self.banner.set_title(f"다시 띄우지 못했어요: {err}")
                self.banner.set_button_label("다시 연결")

        self.store.restart_services(done)

    def toast(self, msg):
        t = Adw.Toast(title=msg, timeout=4)
        t.set_use_markup(False)  # 제목에 & < 가 있어도 그대로
        self.toasts.add_toast(t)

    def _sync_header(self):
        self.add_btn.set_visible(self.stack.get_visible_child_name() == "calendar")
        self.sidebar_btn.set_visible(self.stack.get_visible_child_name() == "calendar" and self.split.get_collapsed())

    def _tick(self):
        self.week.body.queue_draw()
        if date.today() != self._today:
            self._today = date.today()
            self.refresh()
        return True

    def shift_month(self, n):
        y, m = self.month.year, self.month.month + n
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        self.month = date(y, m, 1)
        self.refresh()
        self._fade_in()

    def _fade_in(self):
        """달·주가 바뀔 때 달력이 살짝 비쳤다가 또렷해진다."""
        anim = Adw.TimedAnimation.new(self.view_stack, 0.3, 1, 220,
                                      Adw.PropertyAnimationTarget.new(self.view_stack, "opacity"))
        anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        anim.play()
        self._fade_anim = anim  # 끝날 때까지 참조 유지

    def shift_period(self, n):
        if self.view_stack.get_visible_child_name() == "week":
            self.select_day(self.selected + timedelta(days=7 * n))
        else:
            self.shift_month(n)

    def set_view(self, name):
        if name and name != self.view_stack.get_visible_child_name():
            self.view_stack.set_visible_child_name(name)
            self.refresh()

    def on_selection_changed(self):
        """선택한 날만 바뀌었을 때 (같은 달 안)."""
        if (self.selected.year, self.selected.month) != (self.month.year, self.month.month):
            self.select_day(self.selected)
            return
        if self.view_stack.get_visible_child_name() == "week" and self.week.week_start != getattr(self, "_shown_week", None):
            self._fade_in()
        self._render_selection()
        self._render_day()
        self._update_title()
        if self.split.get_collapsed():
            self.split.set_show_sidebar(True)

    def _update_title(self):
        if self.view_stack.get_visible_child_name() == "week":
            a, b = self.week.days()[0], self.week.days()[-1]
            end = f"{b.day}일" if a.month == b.month else f"{b.month}월 {b.day}일"
            self.month_label.set_label(f"{a.year}년 {a.month}월 {a.day}일 – {end}")
        else:
            self.month_label.set_label(f"{self.month.year}년 {self.month.month}월")

    def go_today(self):
        self.select_day(date.today())

    def select_day(self, d):
        before = (self.month, getattr(self, "_shown_week", None))
        self.selected = d
        self.month = d.replace(day=1)
        self.refresh()
        week = self.view_stack.get_visible_child_name() == "week"
        if (self.week.week_start if week else self.month) != (before[1] if week else before[0]):
            self._fade_in()

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
    PAGES = {Gdk.KEY_Page_Up: -1, Gdk.KEY_Page_Down: 1}

    def _on_key(self, _ctrl, keyval, _code, state):
        if keyval not in self.ARROWS and keyval not in self.PAGES:
            return False
        if self.stack.get_visible_child_name() != "calendar" or self.get_visible_dialog() is not None:
            return False
        if isinstance(self.get_focus(), (Gtk.Text, Gtk.Entry, Gtk.TextView)):
            return False  # 글 쓰는 중이면 커서 이동
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
            return False
        if keyval in self.PAGES:
            self.shift_period(self.PAGES[keyval])
            return True
        self.selected += timedelta(days=self.ARROWS[keyval])
        self.on_selection_changed()
        return True

    # ── 드래그: 일정 막대를 끌어 늘리기 / 빈 칸을 끌어 여러 날 일정 만들기 ──
    def _cell_at(self, x, y):
        """그리드 좌표 → (칸 버튼, 그 위의 일정 | None, 일정 막대 위젯 | None)"""
        w = self.grid.pick(x, y, Gtk.PickFlags.DEFAULT)
        event = chip = None
        while w is not None and w is not self.grid:
            if event is None and getattr(w, "event", None) is not None:
                event, chip = w.event, w
            if hasattr(w, "cell"):
                return w, event, chip
            w = w.get_parent()
        return None, None, None

    EDGE = 10  # 막대 끝에서 이만큼(px) 안쪽을 잡으면 늘리기·줄이기

    def _chip_edge(self, chip, lx):
        """막대 위 x 좌표 → 'start' | 'end' | None (첫날 왼쪽 끝 / 마지막 날 오른쪽 끝만)"""
        if lx < self.EDGE and chip.day == chip.event.days()[0]:
            return "start"
        if lx > chip.get_width() - self.EDGE and chip.day == chip.event.days()[1]:
            return "end"
        return None

    @staticmethod
    def _drag_range(d):
        return drag_range(d["mode"], d["event"].days() if d["event"] is not None else None, d["origin"], d["target"])

    def _drag_begin(self, _g, x, y):
        btn, event, chip = self._cell_at(x, y)
        self._drag = None
        if btn is not None and btn.cell["date"] is not None:
            mode = None
            grab = (0, 0)
            if chip is not None:
                ok, p = self.grid.compute_point(chip, Graphene.Point().init(x, y))
                mode = (self._chip_edge(chip, p.x) if ok else None) or "move"
                grab = (p.x, p.y) if ok else (0, 0)
            self._drag = {"x": x, "y": y, "origin": btn.cell["date"], "target": btn.cell["date"],
                          "event": event, "mode": mode, "active": False, "grab": grab,
                          "chip_w": btn.get_width() - 8}

    def _drag_update(self, g, dx, dy):
        d = self._drag
        if d is None:
            return
        if not d["active"]:
            if dx * dx + dy * dy < 64:  # 8px 넘게 움직여야 드래그로 본다
                return
            d["active"] = True
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            if d["mode"] == "move":
                ev = d["event"]
                self.float_chip.set_label(ev.summary if ev.all_day else f"{ev.start:%H:%M} {ev.summary}")
                for cls in list(self.float_chip.get_css_classes()):
                    if cls.startswith("c") and cls[1:].isdigit():
                        self.float_chip.remove_css_class(cls)
                self.float_chip.add_css_class(self._color_class.get(ev.source_uid, "c0"))
                self.float_chip.set_size_request(max(d["chip_w"], 60), -1)
                self.float_chip.set_visible(True)
        if d["mode"] == "move":  # 막대가 칸에 맞춰지기 전에도 마우스를 부드럽게 따라온다
            ok, p = self.grid.compute_point(self.month_overlay,
                                            Graphene.Point().init(d["x"] + dx - d["grab"][0], d["y"] + dy - d["grab"][1]))
            if ok:
                self.float_chip.set_margin_start(max(int(p.x), 0))
                self.float_chip.set_margin_top(max(int(p.y), 0))
        btn, _, _ = self._cell_at(d["x"] + dx, d["y"] + dy)
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
            s, e = stretched_times(ev, a, b)
            self._preview = replace(ev, start=s, end=e)
            key = (ev.source_uid, ev.uid, ev.rid)
            events = [self._preview if (x.source_uid, x.uid, x.rid) == key else x for x in self.events]
        else:
            cals = self.store.writable(ECal.ClientSourceType.EVENTS)
            self._preview = Event(uid="", rid="", source_uid=cals[0] if cals else "", summary="새 일정",
                                  location="", start=a, end=b + timedelta(days=1), all_day=True,
                                  icomp=None, recurring=False)
            events = self.events + [self._preview]
        self._render_grid(events)

    def _drag_end(self, _g, _dx, _dy):
        d, self._drag = self._drag, None
        self.float_chip.set_visible(False)
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
            self.select_day(a)
            EventDialog(self, day=a, span=(a, b)).present(self)

    def stretch(self, ev, first, last):
        """일정을 first~last 날짜에 걸치도록 바꾼다 (시각은 그대로)."""
        if (first, last) == ev.days():
            return
        self.apply_times(ev, *stretched_times(ev, first, last))

    def apply_times(self, ev, s, e):
        """일정의 시작·끝을 바꿔 저장한다 (드래그용)."""
        if (s, e) == (ev.start, ev.end):
            return
        if ev.source_uid not in self.store.writable(ECal.ClientSourceType.EVENTS):
            self.toast("읽기 전용 캘린더라 바꿀 수 없어요")
            self.refresh()
            return
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
        key, token = (ev.source_uid, ev.uid, ev.rid), object()
        self._overrides[key] = (s, e, token)
        self.refresh()

        def done(err):
            if self._overrides.get(key, (None, None, None))[2] is token:
                del self._overrides[key]
            if err:
                self.toast(f"변경 실패: {err}")
                self.refresh()  # 원래 길이로 되돌린다

        self.store.modify(ev.source_uid, comp, mod, done,
                          label="이 회차만 바꿨어요" if ev.recurring else "바꿨어요")

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
        self.store.remove(task.source_uid, task.uid, None, ECal.ObjModType.ALL, done, label="삭제했어요")

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
            css.append(f".chip.floating.c{i} {{ background-color: mix(@window_bg_color, {color}, .35); }}")
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
        self._update_title()
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
            for (e, _, _), (lane, _) in zip(week, assign_lanes([(ef, el) for _, ef, el in week], wf)):
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
                lab.event, lab.day = e, d
                if e is self._preview:
                    lab.add_css_class("dragging")
                lab.set_cursor_from_name("grabbing" if e is self._preview else "grab")
                if d in (ef, el):  # 끝을 잡을 수 있는 칸: 가장자리에서 커서를 ↔ 로
                    motion = Gtk.EventControllerMotion()
                    motion.connect("motion", lambda _c, x, _y, lab=lab: lab.set_cursor_from_name(
                        "ew-resize" if self._chip_edge(lab, x) else "grab"))
                    lab.add_controller(motion)
                lab.set_tooltip_text(f"{e.summary}\n끌어서 옮기기 · 끝을 잡고 끌면 기간 조절")
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
        self.week.update()
        self._shown_week = self.week.week_start

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
        try:
            self.memo_view.get_buffer().set_text(diary.read_memo(d))
        finally:
            self._memo_loading = False
            self._memo_dirty = False

    def _memo_changed(self, _buf):
        if self._memo_loading:
            return
        self._memo_dirty = True
        if self._memo_timer:
            GLib.source_remove(self._memo_timer)
        self._memo_timer = GLib.timeout_add(600, self._flush_memo)

    def _flush_memo(self):
        if self._memo_timer:
            GLib.source_remove(self._memo_timer)
            self._memo_timer = None
        if self._memo_day is not None and self._memo_dirty:  # 바뀐 것만 (밖에서 고친 파일을 덮지 않게)
            buf = self.memo_view.get_buffer()
            try:
                diary.write_memo(self._memo_day, buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
                self._memo_dirty = False
            except OSError as e:
                log("메모 저장 실패:", e)
                self.toast(f"메모 저장 실패: {e}")
        return False

    # ── 노션 일기 ──
    def open_diary(self):
        """오늘 날짜 메모칸으로 (밤 일기 알림을 눌렀을 때)."""
        self.stack.set_visible_child_name("calendar")
        self.select_day(date.today())
        if self.split.get_collapsed():
            self.split.set_show_sidebar(True)
        self.memo_view.grab_focus()

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
                try:
                    diary.mark_uploaded(d)
                except OSError as e:  # 올리기는 성공했으니 기록 실패는 로그만
                    log("업로드 기록 실패:", e)
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
            t = Adw.Toast(title=f"{diary.title_for(d)} 일기를 올렸어요", timeout=6)
            if url:
                t.set_button_label("열기")
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
.month-cell { padding: 4px 0; margin: 0; border-radius: 10px; min-height: 64px;
              transition: background-color 180ms ease-out, box-shadow 180ms ease-out; }
.chip.floating { margin: 0; font-weight: 700; box-shadow: 0 8px 18px alpha(black, .45); }
.month-cell .day-num { margin-left: 4px; }
.month-cell.drag-range { background-color: alpha(@accent_bg_color, .20); }
.month-cell .chip.dragging { font-weight: 700; box-shadow: inset 0 2px alpha(@accent_bg_color, .95), inset 0 -2px alpha(@accent_bg_color, .95); }
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
