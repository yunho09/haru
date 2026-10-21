"""앱 시작, 명령줄(--diary), 밤 일기 알림(--remind)."""
import os
import subprocess
import sys
from datetime import date, datetime

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from . import diary  # noqa: E402
from .core import APP_ID  # noqa: E402
from .log import log, setup_log  # noqa: E402
from .window import Window  # noqa: E402


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("diary", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, "오늘 날짜 메모칸으로 열기", None)
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()

    def do_shutdown(self):
        for win in self.get_windows():  # Ctrl+Q·로그아웃도 메모를 남기고 끈다
            if hasattr(win, "_flush_memo"):
                win._flush_memo()
        Adw.Application.do_shutdown(self)

    def do_command_line(self, cmdline):
        # 이미 떠 있으면 두 번째 실행의 인자가 여기로 전달된다
        self.activate()
        if cmdline.get_options_dict().contains("diary"):
            self.get_active_window().open_diary()
        return 0


def remind():
    """밤 일기 알림 (systemd 타이머가 부른다). 이미 올렸으면 조용히 끝."""
    today = date.today()
    if datetime.now().hour < 21 and not os.environ.get("HARU_REMIND_NOW"):
        return 0  # 절전 중에 지나간 알림이 다음 날 아침에 뜨지 않게
    if diary.is_uploaded(today):
        return 0
    try:
        if diary.find_page(diary.load_config(), today):  # 노션에서 직접 쓴 경우
            return 0
    except Exception as e:  # 토큰 없음·오프라인이면 그냥 알린다
        log("일기 확인 실패:", e)
    res = subprocess.run(
        ["notify-send", "-a", "하루", "-i", "x-office-calendar", "-A", "default=열기", "-A", "open=일기 쓰기",
         "오늘 일기 올릴까요?", "할 일 체크하고 메모 쓴 다음 '노션 일기에 올리기'를 눌러요"],
        capture_output=True, text=True)
    if res.stdout.strip() in ("default", "open"):
        subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0]), "--diary"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def main():
    setup_log()
    if "--remind" in sys.argv[1:]:
        return remind()
    return App().run(sys.argv)
