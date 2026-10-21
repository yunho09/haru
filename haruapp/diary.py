"""하루 → 노션 일기.

선택한 날의 할 일 · 일정 · git 커밋 · 메모를 모아 노션 일기 데이터베이스에
"2026-10-15" 페이지로 올린다. 올리는 내용은 "📒 하루 기록" 상자(콜아웃) 하나에 담고,
다시 올리면 그 상자만 바꾼다 — 노션에서 직접 쓴 글은 건드리지 않는다.

설정: ~/.config/haru/notion.json
    {"token": "ntn_...", "database_id": "...", "project_root": "/data/project"}
"""
import http.client
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "haru" / "notion.json"
UPLOADED = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "haru" / "uploaded"
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
    # 토큰이 들어 있으니 처음부터 나만 읽을 수 있게 만든다
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
    CONFIG.chmod(0o600)


def memo_path(day):
    return MEMO_DIR / f"{day.isoformat()}.txt"


def read_memo(day):
    try:
        return memo_path(day).read_text(encoding="utf-8")
    except (OSError, ValueError):  # 없거나 깨진 파일
        return ""


def write_memo(day, text):
    p = memo_path(day)
    if text.strip():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    elif p.exists():
        p.unlink()


def mark_uploaded(day):
    """올린 날짜를 기록한다 (밤 알림이 이미 올린 날은 건너뛰게). 최근 90일만 남긴다."""
    days = sorted(set(_uploaded_days()) | {day.isoformat()})[-90:]
    UPLOADED.parent.mkdir(parents=True, exist_ok=True)
    UPLOADED.write_text("\n".join(days) + "\n")


def is_uploaded(day):
    return day.isoformat() in _uploaded_days()


def _uploaded_days():
    try:
        return [x for x in UPLOADED.read_text().split() if x]
    except OSError:
        return []


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
        cmd = ["git", "-C", str(repo), "log", "--exclude=refs/stash", "--all", "--no-merges", "--reverse",
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
        blocks.append({"type": "heading_3", "heading_3": {"rich_text": rich(text)}})

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

def _call(token, method, path, body=None, tries=4):
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
        with e:  # 오류 응답도 연결을 닫는다
            return _http_error(e, token, method, path, body, tries)
    except urllib.error.URLError as e:
        raise NotionError(f"인터넷 연결을 확인해 주세요 ({e.reason})") from None
    except (OSError, http.client.HTTPException) as e:  # 읽다가 끊김·시간 초과
        raise NotionError(f"노션과 통신하다 끊겼어요 ({e})") from None
    except ValueError:
        raise NotionError("노션 응답을 읽지 못했어요") from None


def _http_error(e, token, method, path, body, tries):
    """HTTP 오류 응답 → 재시도하거나 알아듣기 쉬운 NotionError."""
    if e.code in (429, 502, 503) and tries > 1:  # 요청 제한·일시 오류는 잠깐 쉬고 다시
        try:
            wait = float(e.headers.get("Retry-After") or 1)
        except ValueError:
            wait = 1
        time.sleep(min(wait, 10))
        return _call(token, method, path, body, tries - 1)
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


def check(cfg):
    """토큰과 데이터베이스 접근이 되는지 확인 (데이터베이스 제목 반환)."""
    db = _call(cfg["token"], "GET", f"/databases/{cfg['database_id']}")
    return "".join(t.get("plain_text", "") for t in db.get("title", [])) or "일기"


def find_page(cfg, day):
    """그날 일기 페이지 (없으면 None)."""
    if not cfg.get("token"):
        return None
    found = _call(cfg["token"], "POST", f"/databases/{cfg['database_id']}/query", {
        "filter": {"property": TITLE_PROP, "title": {"equals": title_for(day)}},
        "page_size": 1,
    }).get("results", [])
    return found[0] if found else None


HARU_TITLE = "하루 기록"
HARU_ICON = "📒"
_me_cache = {}


def _section(blocks):
    """하루가 올리는 내용을 담는 상자. 한 번에 자식 100개까지라 나머지는 따로 붙인다."""
    return {"type": "callout", "callout": {
        "rich_text": rich(HARU_TITLE), "icon": {"type": "emoji", "emoji": HARU_ICON},
        "color": "gray_background", "children": blocks[:100]}}


def is_haru_section(block):
    if block.get("type") != "callout":
        return False
    c = block["callout"]
    text = "".join(t.get("plain_text") or (t.get("text") or {}).get("content", "") for t in c.get("rich_text", []))
    return text == HARU_TITLE and (c.get("icon") or {}).get("emoji") == HARU_ICON


def _children(token, block_id):
    out, cursor = [], None
    while True:
        q = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        res = _call(token, "GET", f"/blocks/{block_id}/children{q}")
        out += res.get("results", [])
        if not res.get("has_more"):
            return out
        cursor = res.get("next_cursor")


def _me(token):
    if token not in _me_cache:
        _me_cache[token] = _call(token, "GET", "/users/me").get("id")
    return _me_cache[token]


def _append(token, parent_id, blocks):
    for i in range(0, len(blocks), 100):
        _call(token, "PATCH", f"/blocks/{parent_id}/children", {"children": blocks[i:i + 100]})


def old_haru_blocks(children, me):
    """이번에 바꿀 하루 몫의 블록: 하루 기록 상자들, 그리고 상자 없던 시절 하루가 만들고 아무도 안 고친 블록."""
    boxes = [b["id"] for b in children if is_haru_section(b)]
    if boxes:
        return boxes
    return [b["id"] for b in children
            if (b.get("created_by") or {}).get("id") == me and (b.get("last_edited_by") or {}).get("id") == me]


def push(cfg, day, blocks):
    """그날 페이지에 하루 기록 상자를 만들거나 바꾸고 페이지 URL 을 돌려준다.
    새 상자를 먼저 붙이고 옛 상자를 지운다 — 중간에 끊겨도 내용이 비지 않게."""
    token, db = cfg["token"], cfg["database_id"]
    page = find_page(cfg, day)
    if page is None:
        page = _call(token, "POST", "/pages", {
            "parent": {"database_id": db},
            "properties": {TITLE_PROP: {"title": [{"type": "text", "text": {"content": title_for(day)}}]}},
            "children": [_section(blocks)],
        })
        if len(blocks) > 100:
            box = next(b for b in _children(token, page["id"]) if is_haru_section(b))
            _append(token, box["id"], blocks[100:])
        return page.get("url", "")
    old = old_haru_blocks(_children(token, page["id"]), _me(token))
    res = _call(token, "PATCH", f"/blocks/{page['id']}/children", {"children": [_section(blocks)]})
    if len(blocks) > 100:
        _append(token, res["results"][0]["id"], blocks[100:])
    for block_id in old:
        _call(token, "DELETE", f"/blocks/{block_id}")
    return page.get("url", "")


if __name__ == "__main__":
    # 확인용: 오늘 커밋 목록 출력
    for repo, subject in commits(date.today(), load_config()["project_root"]):
        print(f"{repo}: {subject}")
