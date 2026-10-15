"""하루 → 노션 일기.

선택한 날의 할 일 · 일정 · git 커밋 · 메모를 모아 노션 일기 데이터베이스에
"2026-10-15" 페이지로 올린다. 같은 날 페이지가 있으면 내용을 새로 덮어쓴다.

설정: ~/.config/haru/notion.json
    {"token": "ntn_...", "database_id": "...", "project_root": "/data/project"}
"""
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "haru" / "notion.json"
MEMO_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "haru" / "memo"
DEFAULT_DATABASE = "3977a4d61474805eb40ccea9b6d918ff"  # 🎒 일기 (기존 일기들이 있는 곳)
TITLE_PROP = "이름"
API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionError(Exception):
    pass


# ───────────────────────── 설정 · 메모 ─────────────────────────

def load_config():
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("database_id", DEFAULT_DATABASE)
    cfg.setdefault("project_root", "/data/project")
    return cfg


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    CONFIG.chmod(0o600)  # 토큰이 들어 있다


def memo_path(day):
    return MEMO_DIR / f"{day.isoformat()}.txt"


def read_memo(day):
    try:
        return memo_path(day).read_text()
    except OSError:
        return ""


def write_memo(day, text):
    p = memo_path(day)
    if text.strip():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    elif p.exists():
        p.unlink()


# ───────────────────────── 모으기 ─────────────────────────

def commits(day, root):
    """root 바로 아래 git 레포들에서 그날 내가 한 커밋 → [(레포, 제목)]"""
    try:
        email = subprocess.run(["git", "config", "--global", "user.email"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        email = ""
    since = datetime.combine(day, datetime.min.time())
    until = since + timedelta(days=1)
    out = []
    for repo in sorted(Path(root).iterdir()) if Path(root).is_dir() else []:
        if not (repo / ".git").exists():
            continue
        cmd = ["git", "-C", str(repo), "log", "--all", "--no-merges", "--reverse",
               f"--since={since:%Y-%m-%d %H:%M}", f"--until={until:%Y-%m-%d %H:%M}", "--format=%s"]
        if email:
            cmd.append(f"--author={email}")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        seen = set()
        for subject in res.stdout.splitlines():
            if subject and subject not in seen:  # --all 이라 같은 커밋 제목이 브랜치마다 나올 수 있다
                seen.add(subject)
                out.append((repo.name, subject))
    return out


def build_entry(day, todos, events, commit_list, memo):
    """노션 블록 목록. todos: [(제목, 완료?)], events: [문자열]"""
    blocks = []

    def heading(text):
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": rich(text)}})

    if todos:
        heading("✅ 할 일")
        for title, done in todos:
            blocks.append({"type": "to_do", "to_do": {"rich_text": rich(title), "checked": done}})
    if events:
        heading("📅 일정")
        for text in events:
            blocks.append(bullet(text))
    if commit_list:
        heading("💻 커밋")
        # 레포별로 묶는다
        by_repo = {}
        for repo, subject in commit_list:
            by_repo.setdefault(repo, []).append(subject)
        for repo, subjects in by_repo.items():
            item = bullet(repo)
            item["bulleted_list_item"]["children"] = [bullet(s) for s in subjects[:30]]
            blocks.append(item)
    if memo.strip():
        heading("📝 메모")
        for line in memo.strip().splitlines():
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": rich(line)}})
    return blocks


def rich(text):
    # 노션 rich_text 하나는 2000자 제한
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, max(len(text), 1), 2000)]


def bullet(text):
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich(text)}}


def title_for(day):
    # 기존 일기 제목 형식("2026-09-21")에 맞춘다
    return day.isoformat()


# ───────────────────────── 노션 ─────────────────────────

def _call(token, method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            info = json.loads(e.read())
        except ValueError:
            info = {}
        code = info.get("code", "")
        if code == "unauthorized":
            raise NotionError("노션 토큰이 올바르지 않아요") from None
        if code == "object_not_found":
            raise NotionError("일기 데이터베이스를 찾을 수 없어요. 데이터베이스에 통합을 연결했는지 확인해 주세요") from None
        raise NotionError(info.get("message") or f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise NotionError(f"인터넷 연결을 확인해 주세요 ({e.reason})") from None


def check(cfg):
    """토큰과 데이터베이스 접근이 되는지 확인 (데이터베이스 제목 반환)."""
    db = _call(cfg["token"], "GET", f"/databases/{cfg['database_id']}")
    return "".join(t.get("plain_text", "") for t in db.get("title", [])) or "일기"


def push(cfg, day, blocks):
    """그날 페이지를 만들거나 덮어쓰고 페이지 URL 을 돌려준다."""
    token, db = cfg["token"], cfg["database_id"]
    title = title_for(day)
    found = _call(token, "POST", f"/databases/{db}/query", {
        "filter": {"property": TITLE_PROP, "title": {"equals": title}},
        "page_size": 1,
    }).get("results", [])
    if found:
        page = found[0]
        # 기존 내용을 지우고 새로 채운다
        cursor = None
        old = []
        while True:
            q = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            res = _call(token, "GET", f"/blocks/{page['id']}/children{q}")
            old += [b["id"] for b in res.get("results", [])]
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        for block_id in old:
            _call(token, "DELETE", f"/blocks/{block_id}")
        for i in range(0, len(blocks), 100):
            _call(token, "PATCH", f"/blocks/{page['id']}/children", {"children": blocks[i:i + 100]})
        return page.get("url", "")
    page = _call(token, "POST", "/pages", {
        "parent": {"database_id": db},
        "properties": {TITLE_PROP: {"title": [{"type": "text", "text": {"content": title}}]}},
        "children": blocks[:100],
    })
    for i in range(100, len(blocks), 100):
        _call(token, "PATCH", f"/blocks/{page['id']}/children", {"children": blocks[i:i + 100]})
    return page.get("url", "")


if __name__ == "__main__":
    # 확인용: 오늘 커밋 목록 출력
    for repo, subject in commits(date.today(), load_config()["project_root"]):
        print(f"{repo}: {subject}")
