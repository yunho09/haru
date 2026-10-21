"""오류 로그 (~/.local/state/haru/haru.log)."""
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "haru"


def log(*parts):
    print(f"[{datetime.now():%m-%d %H:%M:%S}]", *parts, file=sys.stderr)


def setup_log():
    """오류를 ~/.local/state/haru/haru.log 에 남긴다. 앱 메뉴로 켰을 때는 GTK 경고까지."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "haru.log"
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(path.with_suffix(".log.1"))
    f = open(path, "a", buffering=1, encoding="utf-8")
    f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} 시작 (pid {os.getpid()}) ===\n")
    if sys.stderr is None or not sys.stderr.isatty():
        os.dup2(f.fileno(), 2)  # C 쪽(GTK·GLib) 경고도 같은 파일로
        sys.stderr = f
    else:  # 터미널에서 켰으면 화면과 파일 둘 다
        term = sys.stderr

        class Tee:
            def write(self, data):
                term.write(data)
                f.write(data)

            def flush(self):
                term.flush()
                f.flush()

        sys.stderr = Tee()

    def hook(kind, value, tb):  # 콜백 안에서 난 처리 안 된 오류도 여기로 온다
        log("처리 안 된 오류")
        traceback.print_exception(kind, value, tb, file=sys.stderr)

    sys.excepthook = hook
