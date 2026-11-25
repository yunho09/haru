"""새벽 2시 일기 자동 업로드 (systemd 타이머 haru-diary-upload.timer 가 부른다).

막 끝난 하루(전날 새벽 2시 ~ 오늘 새벽 2시)의 메모가 있으면 '노션 일기에 올리기'와 같은 내용(할 일·일정·커밋·메모)을 올린다.
이미 올린 날도 다시 올려 하루의 마지막 모습으로 맞춘다 — 바뀌는 건 📒 하루 기록 상자뿐.
절전·꺼짐·실패로 놓친 날(지난 일주일 중 메모가 있는데 아직 안 올린 날)도 같이 올린다.
"""
import subprocess
import time
from datetime import datetime, timedelta

from gi.repository import GLib

from . import diary
from .core import diary_day
from .log import log
from .store import Store

CATCH_UP_DAYS = 7
RESTART_AFTER = 45  # 초: 그때도 안 붙은 캘린더가 있으면 캘린더 서비스를 한 번 다시 띄운다 (ECal 연결 대기 30초보다 길게)
CONNECT_WAIT = 120  # 초: 캘린더 연결을 기다리는 최대 시간
RETRY_WAITS = (30, 60, 120, 300)  # 오프라인일 때 다시 해 보기 전 쉬는 시간 — 절전에서 깬 직후엔 인터넷이 늦게 붙는다
NOTIFY_TRIES = 12  # 부팅 직후엔 알림 서버(gnome-shell)가 아직 없어서 5초 간격으로 다시


def target_day(now):
    """올릴 날 = 가장 최근에 끝난 일기 하루 (하루는 새벽 2시에 바뀜).
    새벽 2시에 돌면 전날 것. 절전·꺼짐으로 놓쳐 아침에 돌아도 같은 날 것을 올린다."""
    return diary_day(now) - timedelta(days=1)


def days_to_upload(target):
    """올릴 날들 (오래된 순): 놓친 날 + 메모가 있으면 target."""
    days = [target - timedelta(days=i) for i in range(CATCH_UP_DAYS, 0, -1)]
    out = [d for d in days if diary.read_memo(d).strip() and not diary.is_uploaded(d)]
    if diary.read_memo(target).strip():
        out.append(target)
    return out


def connect_store(make_store=Store):
    """캘린더 서비스(EDS)에 붙어 모든 캘린더·할 일 목록이 연결될 때까지 기다린다 (최대 CONNECT_WAIT 초).
    오래 안 붙으면 앱의 '다시 연결'처럼 서비스를 한 번 다시 띄운다."""
    loop = GLib.MainLoop()
    store = make_store(lambda: None)

    def restarted(_err):
        store.on_all_connected = loop.quit
        if not store.pending():
            loop.quit()

    def restart():
        if store.pending():
            log("자동 업로드: 캘린더 연결이 늦어 서비스를 다시 띄움:", ", ".join(store.pending()))
            # 재시작으로 끊긴 연결도 '연결 끝'으로 세어진다 → 다시 연결을 시작할 때까지는 끝을 기다리지 않는다
            store.on_all_connected = None
            store.restart_services(restarted)
        return False

    store.on_all_connected = loop.quit
    if store.pending():
        timers = [GLib.timeout_add(int(RESTART_AFTER * 1000), restart),
                  GLib.timeout_add(int(CONNECT_WAIT * 1000), loop.quit)]
        loop.run()
        for t in timers:
            if src := GLib.MainContext.default().find_source_by_id(t):
                src.destroy()
    return store


def collect(store, day):
    return diary.day_items(day, store.tasks(), store.events(day, day + timedelta(days=1)), store.skip_in_diary)


def notify(title, body):
    for _ in range(NOTIFY_TRIES):
        try:
            res = subprocess.run(["notify-send", "-a", "하루", "-i", "x-office-calendar", title, body],
                                 capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            log("알림 실패:", e)
            return
        if res.returncode == 0:
            return
        time.sleep(5)
    log("알림 실패: 알림 서버가 없음 —", title)


def push_with_retry(cfg, day, blocks):
    for wait in (*RETRY_WAITS, None):
        try:
            return diary.push(cfg, day, blocks)
        except diary.Offline as e:
            if wait is None:
                raise
            log(f"자동 업로드: {e} — {wait}초 뒤 다시")
            time.sleep(wait)


def titles(days):
    return ", ".join(diary.title_for(d) for d in days)


def failed(days, reason):
    log(f"자동 업로드 실패 ({titles(days)}):", reason)
    notify("일기를 자동으로 못 올렸어요",
           f"{titles(days)}: {reason}\n앱에서 '노션 일기에 올리기'를 누르거나, 내일 밤 자동 업로드 때 다시 올라가요")
    return 1


def run(now=None):
    target = target_day(now or datetime.now())
    days = days_to_upload(target)
    if not days:
        log(f"자동 업로드: {target} 메모가 없어 건너뜀")
        return 0
    cfg = diary.load_config()
    if not cfg.get("token"):
        return failed(days, "노션 연결이 안 돼 있어요")
    done, errors = [], []  # 올린 날, (못 올린 날, 오류)
    try:
        store = connect_store()
        # 캘린더가 덜 붙은 채 올리면 할 일·일정이 빠진 상자로 덮어쓰게 된다 (공휴일·생일은 어차피 안 올림)
        missing = [store.name(u) for u in store.not_connected() if not store.skip_in_diary(u)]
        if missing:
            return failed(days, f"캘린더가 연결되지 않았어요 ({', '.join(missing)})")
        for day in days:
            todos, events = collect(store, day)
            blocks = diary.build_entry(day, todos, events, diary.commits(day, cfg["project_root"]), diary.read_memo(day))
            try:
                push_with_retry(cfg, day, blocks)
            except diary.Offline:
                raise  # 다시 해 봐도 안 됐으니 나머지 날도 안 된다
            except diary.NotionError as e:  # 그날만의 문제일 수 있다 → 나머지 날(특히 오늘)은 계속
                errors.append((day, e))
                continue
            done.append(day)
            try:
                diary.mark_uploaded(day)
            except OSError as e:  # 올리기는 성공했으니 기록 실패는 로그만
                log("업로드 기록 실패:", e)
    except Exception as e:  # Offline 과 예상 못 한 오류도 알림으로 알린다
        return failed([d for d in days if d not in done], str(e))
    if done:
        log(f"자동 업로드: {titles(done)} 올림")
    if errors:
        return failed([d for d, _ in errors], " / ".join(dict.fromkeys(str(e) for _, e in errors)))
    notify(f"{titles(done)} 일기를 올렸어요", "메모를 노션 일기에 자동으로 올렸어요")
    return 0
