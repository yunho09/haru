"""주 보기 (시간표, cairo 로 직접 그림)."""
from datetime import date, datetime, time, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("ECal", "2.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, ECal, Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from .core import WEEKDAYS, assign_lanes, overlap_columns  # noqa: E402
from .dialogs import EventDialog  # noqa: E402


def rgba(hex_or_rgba, alpha=1.0):
    c = Gdk.RGBA()
    c.parse(hex_or_rgba)
    c.alpha = alpha
    return c


def set_src(cr, c, alpha=None):
    cr.set_source_rgba(c.red, c.green, c.blue, c.alpha if alpha is None else alpha)


def round_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()


def text(cr, widget, s, x, y, width, size=9, bold=False, color=None, lines=1, align=Pango.Alignment.LEFT):
    layout = widget.create_pango_layout(None)  # 위젯 글꼴·힌팅 설정을 그대로 쓴다
    fd = widget.get_pango_context().get_font_description().copy()
    fd.set_size(size * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.BOLD)
    layout.set_font_description(fd)
    layout.set_width(max(int(width * Pango.SCALE), 1))
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_height(-lines)
    layout.set_alignment(align)
    layout.set_text(s, -1)
    if color is not None:
        set_src(cr, color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return layout.get_pixel_size()[1]


class WeekView(Gtk.Box):
    """일요일부터 7일, 시간표 형태. 일정을 끌어 시간·요일 옮기기, 아래 끝을 끌어 길이 바꾸기,
    빈 칸을 끌어 새 일정."""

    HOUR = 48     # 한 시간 높이(px)
    GUTTER = 56   # 왼쪽 시각 칸
    SNAP = 15     # 분 단위 맞춤
    LANE = 22     # 하루 종일 줄 높이
    HEAD = 46     # 요일·날짜 줄 높이

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.win = win
        self.head = Gtk.DrawingArea(content_height=self.HEAD)
        self.head.set_draw_func(self._draw_head)
        self.body = Gtk.DrawingArea(content_height=24 * self.HOUR)
        self.body.set_draw_func(self._draw_body)
        self.scroller = Gtk.ScrolledWindow(child=self.body, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(self.head)
        self.append(Gtk.Separator())
        self.append(self.scroller)
        self._rects = []       # 본문에 그린 일정 (x, y, w, h, 일정)
        self._head_rects = []  # 하루 종일 막대
        self._drag = None
        self._scrolled = False

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", self._end)
        self.body.add_controller(drag)
        dbl = Gtk.GestureClick()
        dbl.connect("pressed", self._body_pressed)
        self.body.add_controller(dbl)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.body.add_controller(motion)
        head_click = Gtk.GestureClick()
        head_click.connect("released", self._head_released)
        self.head.add_controller(head_click)
        self.connect("map", lambda *_: GLib.idle_add(self._scroll_to_morning))

    # ── 데이터 ──
    @property
    def week_start(self):
        d = self.win.selected
        return d - timedelta(days=(d.weekday() + 1) % 7)

    def days(self):
        ws = self.week_start
        return [ws + timedelta(days=i) for i in range(7)]

    def update(self):
        """하루 종일 줄 수에 맞춰 머리 높이를 정하고 다시 그린다."""
        days = self.days()
        lanes = self._allday_lanes(days)
        n = max((lane for _, lane in lanes), default=-1) + 1
        self.head.set_content_height(self.HEAD + n * self.LANE + (6 if n else 0))
        self.head.queue_draw()
        self.body.queue_draw()

    def _allday_lanes(self, days):
        ws, we = days[0], days[-1]
        items = []
        for e in getattr(self.win, "events", []):
            if not e.all_day:
                continue
            f, l = e.days()
            if f <= we and l >= ws:
                items.append((e, f, l))
        items.sort(key=lambda x: (x[1], -(x[2] - x[1]).days, x[0].summary))
        lanes = assign_lanes([(f, l) for _, f, l in items], ws)
        return [((e, cols), lane) for (e, _, _), (lane, cols) in zip(items, lanes)]

    # ── 좌표 ──
    def _colw(self, width):
        return (width - self.GUTTER) / 7

    def _at(self, x, y, end=False):
        """본문 좌표 → (날짜, 그날 0시부터 분) — 분은 SNAP 단위로 맞춤.
        end=False 면 23:45 까지만 (24:00 은 다음 날 0시라 클릭·시작점으로 쓰면 다음 날이 된다)."""
        colw = self._colw(self.body.get_width())
        i = min(max(int((x - self.GUTTER) // colw), 0), 6)
        minutes = min(max(y / self.HOUR * 60, 0), 24 * 60)
        minutes = int(round(minutes / self.SNAP) * self.SNAP)
        if not end:
            minutes = min(minutes, 24 * 60 - self.SNAP)
        return self.days()[i], minutes

    def _dt(self, day, minutes):
        return datetime.combine(day, time()) + timedelta(minutes=minutes)

    def _hit(self, x, y):
        for rx, ry, rw, rh, ev, ends in reversed(self._rects):
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                # 아래 끝 7px 는 길이 조절 — 단 일정이 실제로 끝나는 조각에서만 (여러 날 일정의 자정 끝 제외)
                return ev, ends and (y > ry + rh - 7)
        return None, False

    def _scroll_to_morning(self):
        if not self._scrolled:
            self._scrolled = True
            self.scroller.get_vadjustment().set_value(8 * self.HOUR - 8)
        return False

    # ── 그리기 ──
    def _colors(self):
        fg = self.get_color()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        return fg, accent

    def _draw_head(self, area, cr, width, height):
        fg, accent = self._colors()
        colw = self._colw(width)
        today = date.today()
        red, blue = rgba("#e5484d"), rgba("#3584e4")
        off_days = {d for e in getattr(self.win, "events", []) if e.day_off for d in self.days() if e.covers(d)}
        for i, d in enumerate(self.days()):
            x = self.GUTTER + i * colw
            if d == self.win.selected:
                set_src(cr, accent, 0.10)
                cr.rectangle(x, 0, colw, height)
                cr.fill()
            color = red if (d.weekday() == 6 or d in off_days) else blue if d.weekday() == 5 else fg
            text(cr, self, WEEKDAYS[d.weekday()], x, 4, colw, size=8, color=color, align=Pango.Alignment.CENTER)
            if d == today:
                set_src(cr, accent)
                cr.arc(x + colw / 2, 31, 12, 0, 6.2832)
                cr.fill()
                color = rgba("white")
            text(cr, self, str(d.day), x, 22, colw, size=11, bold=True, color=color, align=Pango.Alignment.CENTER)
        # 하루 종일 막대
        self._head_rects = []
        for (e, cols), lane in self._allday_lanes(self.days()):
            x = self.GUTTER + cols[0] * colw + 2
            w = len(cols) * colw - 4
            y = self.HEAD + lane * self.LANE + 2
            c = rgba(self.win.store.colors.get(e.source_uid, "#62a0ea"))
            set_src(cr, c, 0.28)
            round_rect(cr, x, y, w, self.LANE - 4, 4)
            cr.fill()
            set_src(cr, c)
            cr.rectangle(x, y, 3, self.LANE - 4)
            cr.fill()
            text(cr, self, e.summary, x + 7, y + 2, w - 10, size=8.5, bold=True, color=fg)
            self._head_rects.append((x, y, w, self.LANE - 4, e))

    def _draw_body(self, area, cr, width, height):
        fg, accent = self._colors()
        colw = self._colw(width)
        days = self.days()
        today = date.today()
        # 오늘·선택한 날 배경
        for i, d in enumerate(days):
            if d == self.win.selected:
                set_src(cr, accent, 0.06)
                cr.rectangle(self.GUTTER + i * colw, 0, colw, height)
                cr.fill()
        # 시간 줄
        for h in range(24):
            y = h * self.HOUR
            set_src(cr, fg, 0.10)
            cr.rectangle(self.GUTTER, y, width - self.GUTTER, 1)
            cr.fill()
            if h:
                text(cr, self, f"{h:02d}:00", 4, y - 7, self.GUTTER - 10, size=8, color=rgba(fg.to_string(), 0.55),
                     align=Pango.Alignment.RIGHT)
        for i in range(8):
            set_src(cr, fg, 0.08)
            cr.rectangle(self.GUTTER + i * colw, 0, 1, height)
            cr.fill()

        # 일정
        self._rects = []
        dragging = self._drag["event"] if self._drag and self._drag.get("active") else None
        for i, d in enumerate(days):
            d0 = datetime.combine(d, time())
            d1 = d0 + timedelta(days=1)
            segs = []
            for e in getattr(self.win, "events", []):
                if e.all_day:
                    continue
                s, en = max(e.start, d0), min(max(e.end, e.start + timedelta(minutes=15)), d1)
                if en > s:
                    segs.append((s, en, e))
            for s, en, e, col, n in overlap_columns(segs):
                x = self.GUTTER + i * colw + 2 + col * (colw - 4) / n
                w = (colw - 4) / n - 2
                y = (s - d0).total_seconds() / 3600 * self.HOUR + 1
                h = max((en - s).total_seconds() / 3600 * self.HOUR - 2, 18)
                c = rgba(self.win.store.colors.get(e.source_uid, "#62a0ea"))
                ghost = dragging is not None and (e.source_uid, e.uid, e.rid) == \
                    (dragging.source_uid, dragging.uid, dragging.rid)
                if ghost:  # 끄는 중인 일정의 원래 자리: 점선 테두리만
                    set_src(cr, fg, 0.45)
                    cr.set_line_width(1.2)
                    cr.set_dash([4, 3])
                    round_rect(cr, x + 0.6, y + 0.6, w - 1.2, h - 1.2, 5)
                    cr.stroke()
                    cr.set_dash([])
                    continue
                set_src(cr, c, 0.25)
                round_rect(cr, x, y, w, h, 5)
                cr.fill()
                set_src(cr, c)
                cr.rectangle(x, y, 3, h)
                cr.fill()
                lines = max(int((h - 4) // 14), 1)
                used = text(cr, self, e.summary, x + 7, y + 2, w - 9, size=8.5, bold=True, color=fg,
                            lines=min(lines, 2))
                if h - used > 18:
                    text(cr, self, f"{e.start:%H:%M}–{e.end:%H:%M}", x + 7, y + 2 + used, w - 9, size=8,
                         color=rgba(fg.to_string(), 0.7))
                self._rects.append((x, y, w, h, e, e.end <= d1))

        # 끄는 중인 블록: 마우스를 그대로 따라오고(시각 글자만 15분 단위), 놓으면 제자리로 미끄러진다
        d = self._drag
        if d and d.get("active") and d.get("new"):
            ns, ne = d["new"]
            rect = self._live_rect(d, colw)
            if rect is None:  # 여러 날에 걸친 경우: 맞춰진 자리에 그린다
                for i, day in enumerate(days):
                    d0 = datetime.combine(day, time())
                    s0, e0 = max(ns, d0), min(ne, d0 + timedelta(days=1))
                    if e0 > s0:
                        self._draw_block(cr, d, (self.GUTTER + i * colw + 2,
                                                 (s0 - d0).total_seconds() / 3600 * self.HOUR + 1, colw - 4,
                                                 max((e0 - s0).total_seconds() / 3600 * self.HOUR - 2, 22)), accent)
            else:
                self._draw_block(cr, d, rect, accent)

        # 지금 시각
        if today in days:
            now = datetime.now()
            i = days.index(today)
            y = (now.hour + now.minute / 60) * self.HOUR
            red = rgba("#e5484d")
            set_src(cr, red)
            cr.rectangle(self.GUTTER + i * colw, y - 1, colw, 2)
            cr.fill()
            cr.arc(self.GUTTER + i * colw, y, 4, 0, 6.2832)
            cr.fill()

    def _snapped_rect(self, d, colw):
        """맞춰진 시각의 블록 위치 (한 날 안일 때만)."""
        ns, ne = d["new"]
        days = self.days()
        if ns.date() not in days or (ne - datetime.combine(ns.date(), time())) > timedelta(days=1):
            return None
        i = days.index(ns.date())
        d0 = datetime.combine(ns.date(), time())
        y = (ns - d0).total_seconds() / 3600 * self.HOUR + 1
        h = max((ne - ns).total_seconds() / 3600 * self.HOUR - 2, 22)
        return self.GUTTER + i * colw + 2, y, colw - 4, h

    def _live_rect(self, d, colw):
        """마우스를 그대로 따라가는 블록 위치. 놓은 뒤에는 맞춰진 자리로 보간한다."""
        snapped = self._snapped_rect(d, colw)
        if snapped is None:
            return None
        px, py = d.get("px", (d["x"], d["y"]))
        sx, sy, sw, sh = snapped
        if d["mode"] == "move":
            raw = (min(max(px - d.get("grab_px", sw / 2), self.GUTTER), self.GUTTER + 7 * colw - sw),
                   py - d.get("grab_py", 10), sw, sh)
        elif d["mode"] == "resize":
            raw = (sx, sy, sw, max(py - sy, 22))
        else:
            ay = d["y"]
            raw = (sx, min(ay, py), sw, max(abs(py - ay), 22))
        t = d.get("settle")
        if t is None:
            return raw
        return tuple(r + (q - r) * t for r, q in zip(raw, snapped))

    def _draw_block(self, cr, d, rect, accent):
        x, y, w, h = rect
        ns, ne = d["new"]
        ev = d["event"]
        white = rgba("white")
        if ev is None:  # 새 일정 범위
            set_src(cr, accent, 0.40)
            round_rect(cr, x, y, w, h, 6)
            cr.fill()
            text(cr, self, f"{ns:%H:%M}–{ne:%H:%M}", x + 8, y + 3, w - 12, size=9, bold=True, color=white)
            return
        c = rgba(self.win.store.colors.get(ev.source_uid, "#62a0ea"))
        set_src(cr, rgba("black"), 0.35)  # 그림자
        round_rect(cr, x + 1, y + 4, w, h, 6)
        cr.fill()
        set_src(cr, c, 0.95)
        round_rect(cr, x, y, w, h, 6)
        cr.fill()
        set_src(cr, accent)
        cr.set_line_width(2)
        round_rect(cr, x + 1, y + 1, w - 2, h - 2, 5)
        cr.stroke()
        used = text(cr, self, f"{ns:%H:%M}–{ne:%H:%M}", x + 8, y + 3, w - 12, size=9.5, bold=True, color=white)
        if h - used > 16:
            text(cr, self, ev.summary, x + 8, y + 3 + used, w - 12, size=8.5, color=white)

    # ── 마우스 ──
    def _motion(self, _c, x, y):
        ev, edge = self._hit(x, y)
        self.body.set_cursor_from_name("ns-resize" if edge else "grab" if ev else "default")

    def _begin(self, _g, x, y):
        if self._drag and self._drag.get("settle") is not None:
            self._settle_anim.skip()  # 이전 드래그가 자리 잡는 중이면 바로 끝낸다
        ev, edge = self._hit(x, y)
        day, minutes = self._at(x, y)
        self._drag = {"x": x, "y": y, "px": (x, y), "event": ev, "active": False, "new": None,
                      "mode": "resize" if edge else "move" if ev else "create",
                      "anchor": self._dt(day, minutes)}
        if ev is not None:
            self._drag["grab"] = self._dt(day, minutes) - ev.start  # 잡은 곳과 시작의 차이
            for rx, ry, rw, rh, e, _ends in self._rects:  # 블록 안에서 잡은 위치(px)
                if e is ev and rx <= x <= rx + rw and ry <= y <= ry + rh:
                    self._drag["grab_px"], self._drag["grab_py"] = x - rx, y - ry

    def _update(self, g, dx, dy):
        d = self._drag
        if d is None:
            return
        if not d["active"]:
            if dx * dx + dy * dy < 25:
                return
            d["active"] = True
            g.set_state(Gtk.EventSequenceState.CLAIMED)
        d["px"] = (d["x"] + dx, d["y"] + dy)
        day, minutes = self._at(d["x"] + dx, d["y"] + dy, end=d["mode"] in ("resize", "create"))
        at = self._dt(day, minutes)
        ev = d["event"]
        if d["mode"] == "move":
            s = at - d["grab"]
            d["new"] = (s, s + (ev.end - ev.start))
        elif d["mode"] == "resize":
            d["new"] = (ev.start, max(at, ev.start + timedelta(minutes=self.SNAP)))
        else:
            a = d["anchor"]
            day0 = datetime.combine(a.date(), time())
            at = min(max(at, day0), day0 + timedelta(days=1))  # 새 일정은 한 날 안에서만
            lo, hi = min(a, at), max(a, at)
            if hi - lo < timedelta(minutes=self.SNAP):
                hi = lo + timedelta(minutes=self.SNAP)
            d["new"] = (lo, hi)
        self.body.queue_draw()

    def _end(self, _g, dx, dy):
        d = self._drag
        if d is None or d.get("settle") is not None:
            return
        if d["active"] and d["new"] is not None and self._snapped_rect(d, self._colw(self.body.get_width())):
            # 마우스 자리에서 맞춰진 시각 자리로 미끄러진 뒤 반영
            d["settle"] = 0.0

            def step(v):
                d["settle"] = v
                self.body.queue_draw()

            self._settle_anim = Adw.TimedAnimation.new(self.body, 0, 1, 160, Adw.CallbackAnimationTarget.new(step))
            self._settle_anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
            self._settle_anim.connect("done", lambda *_: self._finish(d))
            self._settle_anim.play()
            return
        self._finish(d)

    def _finish(self, d):
        if self._drag is d:
            self._drag = None
        win = self.win
        if not d["active"]:  # 그냥 클릭
            if win.get_visible_dialog() is not None:
                return
            if d["event"] is not None:
                EventDialog(win, event=d["event"]).present(win)
            else:
                win.selected = d["anchor"].date()
                win.on_selection_changed()
            return
        if d["new"] is None:
            self.body.queue_draw()
            return
        if d["event"] is not None:
            win.apply_times(d["event"], *d["new"])
            self.body.queue_draw()
        elif not win.store.writable(ECal.ClientSourceType.EVENTS):
            win.toast("캘린더에 아직 연결 중이에요")
            self.body.queue_draw()
        else:
            win.selected = d["new"][0].date()
            win.on_selection_changed()
            EventDialog(win, times=d["new"]).present(win)

    def _body_pressed(self, _g, n_press, x, y):
        if n_press == 2 and self._hit(x, y)[0] is None and self.win.get_visible_dialog() is None:
            day, minutes = self._at(x, y)
            s = self._dt(day, minutes - minutes % 60)
            EventDialog(self.win, times=(s, s + timedelta(hours=1))).present(self.win)

    def _head_released(self, _g, _n, x, y):
        for rx, ry, rw, rh, e in self._head_rects:
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                EventDialog(self.win, event=e).present(self.win)
                return
        if x > self.GUTTER:
            i = min(int((x - self.GUTTER) // self._colw(self.head.get_width())), 6)
            self.win.selected = self.days()[i]
            self.win.on_selection_changed()
