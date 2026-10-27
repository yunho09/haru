"""작은 입력 위젯: 날짜 버튼, 시:분 입력, 콤보 행."""
from datetime import date, time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from .core import fmt_day  # noqa: E402


class TaskRef(GObject.Object):
    """끌어 옮기는 할 일 표지. 문자열이 아닌 전용 타입이라 메모·입력칸이나 다른 앱은 받지 않는다."""
    __gtype_name__ = "HaruTaskRef"

    def __init__(self, source_uid, uid):
        super().__init__()
        self.key = (source_uid, uid)


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
        dt = GLib.DateTime.new_local(d.year, d.month, d.day, 0, 0, 0)
        if hasattr(self.cal, "set_date"):  # GTK 4.20+
            self.cal.set_date(dt)
        else:
            self.cal.select_day(dt)

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
