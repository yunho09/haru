"""haruapp.diary: 일기 내용 만들기, 커밋 모으기, 기록 파일, 노션 올리기(가짜 서버)."""
import itertools
import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from haruapp import diary

BOT, USER = "bot-1", "user-9"


class FakeNotion:
    """노션 API 중 하루가 쓰는 부분만 흉내 낸다 (페이지·블록 트리를 메모리에)."""

    def __init__(self):
        self.pages = {}  # 제목 → {"id", "by", "children": [블록]}
        self.ids = itertools.count()
        self.log = []
        self.fail_429 = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, code, obj, headers=()):
                body = json.dumps(obj).encode()
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                fake.log.append(("GET", self.path))
                if self.headers["Authorization"] != "Bearer tok":
                    return self.send(401, {"code": "unauthorized"})
                if self.path == "/v1/users/me":
                    return self.send(200, {"id": BOT})
                if self.path.startswith("/v1/databases/"):
                    return self.send(200, {"title": [{"plain_text": "일기"}]})
                kids = fake.children_of(self.path.split("/")[3])
                return self.send(200, {"results": [fake.public(b) for b in kids], "has_more": False})

            def do_POST(self):
                body = self.body()
                fake.log.append(("POST", self.path))
                if self.path.endswith("/query"):
                    page = fake.pages.get(body["filter"]["title"]["equals"])
                    return self.send(200, {"results": [{"id": page["id"], "url": "u/" + page["id"]}] if page else []})
                title = body["properties"][diary.TITLE_PROP]["title"][0]["text"]["content"]
                pid = f"p{next(fake.ids)}"
                fake.pages[title] = {"id": pid, "by": BOT, "children": [fake.block(c) for c in body["children"]]}
                return self.send(200, {"id": pid, "url": "u/" + pid})

            def do_PATCH(self):
                if fake.fail_429:
                    fake.fail_429 -= 1
                    fake.log.append(("429", self.path))
                    return self.send(429, {"code": "rate_limited"}, [("Retry-After", "0.05")])
                body = self.body()
                fake.log.append(("PATCH", self.path))
                new = [fake.block(c) for c in body["children"]]
                fake.children_of(self.path.split("/")[3]).extend(new)
                return self.send(200, {"results": [{"id": b["id"]} for b in new]})

            def do_DELETE(self):
                fake.log.append(("DELETE", self.path))
                fake.delete(self.path.split("/")[3])
                return self.send(200, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def block(self, b, by=BOT):
        t = b["type"]
        data = dict(b[t])
        data["rich_text"] = [dict(x, plain_text=x["text"]["content"]) for x in data.get("rich_text", [])]
        kids = [self.block(k, by) for k in data.pop("children", [])]
        return {"id": f"b{next(self.ids)}", "type": t, t: data, "_kids": kids,
                "created_by": {"id": by}, "last_edited_by": {"id": by}}

    @staticmethod
    def public(b):
        return {k: v for k, v in b.items() if k != "_kids"}

    def _walk(self, blocks):
        for b in blocks:
            yield b
            yield from self._walk(b["_kids"])

    def children_of(self, block_id):
        for page in self.pages.values():
            if page["id"] == block_id:
                return page["children"]
            for b in self._walk(page["children"]):
                if b["id"] == block_id:
                    return b["_kids"]
        raise KeyError(block_id)

    def delete(self, block_id):
        def drop(blocks):
            for i, b in enumerate(blocks):
                if b["id"] == block_id:
                    del blocks[i]
                    return True
                if drop(b["_kids"]):
                    return True
            return False

        for page in self.pages.values():
            drop(page["children"])

    def outline(self, title):
        def text(b):
            return "".join(x["plain_text"] for x in b[b["type"]].get("rich_text", []))

        return [f"{b['type']}:{text(b)}" for b in self.pages[title]["children"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def paragraph(text):
    return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


class Push(unittest.TestCase):
    DAY = date(2026, 10, 21)

    def setUp(self):
        self.fake = FakeNotion()
        patcher = mock.patch.object(diary, "API", self.fake.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.fake.close)
        diary._me_cache.clear()
        self.cfg = {"token": "tok", "database_id": "db"}
        self.blocks = diary.build_entry(self.DAY, [("수학", True)], ["하루 종일  영어 수행"], [], "메모")

    def test_new_page_gets_one_box(self):
        url = diary.push(self.cfg, self.DAY, self.blocks)
        self.assertTrue(url)
        self.assertEqual(self.fake.outline("2026-10-21"), ["callout:하루 기록"])
        box = self.fake.pages["2026-10-21"]["children"][0]
        self.assertEqual(len(box["_kids"]), len(self.blocks))

    def test_upload_again_replaces_only_the_box(self):
        diary.push(self.cfg, self.DAY, self.blocks)
        diary.push(self.cfg, self.DAY, diary.build_entry(self.DAY, [("국어", False)], [], [], ""))
        page = self.fake.pages["2026-10-21"]
        self.assertEqual(self.fake.outline("2026-10-21"), ["callout:하루 기록"])
        self.assertEqual(len(page["children"][0]["_kids"]), 2)  # 제목 + 할 일 하나

    def test_hand_written_page_is_kept(self):
        self.fake.pages["2026-10-21"] = {"id": "p-user", "by": USER,
                                         "children": [self.fake.block(paragraph("직접 쓴 일기"), USER)]}
        diary.push(self.cfg, self.DAY, self.blocks)
        diary.push(self.cfg, self.DAY, self.blocks)
        self.assertEqual(self.fake.outline("2026-10-21"), ["paragraph:직접 쓴 일기", "callout:하루 기록"])

    def test_legacy_page_keeps_blocks_the_user_edited(self):
        mine = self.fake.block(paragraph("하루가 쓴 메모"))
        edited = self.fake.block(paragraph("내가 고친 문단"))
        edited["last_edited_by"] = {"id": USER}
        self.fake.pages["2026-10-21"] = {"id": "p-old", "by": BOT, "children": [mine, edited]}
        diary.push(self.cfg, self.DAY, self.blocks)
        self.assertEqual(self.fake.outline("2026-10-21"), ["paragraph:내가 고친 문단", "callout:하루 기록"])

    def test_new_box_is_added_before_old_one_is_deleted(self):
        diary.push(self.cfg, self.DAY, self.blocks)
        self.fake.log.clear()
        diary.push(self.cfg, self.DAY, self.blocks)
        kinds = [k for k, *_ in self.fake.log if k in ("PATCH", "DELETE")]
        self.assertEqual(kinds, ["PATCH", "DELETE"])

    def test_more_than_100_blocks(self):
        big = diary.build_entry(self.DAY, [(f"할 일 {i}", False) for i in range(130)], [], [], "")
        diary.push(self.cfg, self.DAY, big)
        self.assertEqual(len(self.fake.pages["2026-10-21"]["children"][0]["_kids"]), 131)

    def test_rate_limit_is_retried(self):
        diary.push(self.cfg, self.DAY, self.blocks)
        self.fake.fail_429 = 2
        diary.push(self.cfg, self.DAY, self.blocks)
        self.assertEqual(sum(1 for k, *_ in self.fake.log if k == "429"), 2)
        self.assertEqual(self.fake.outline("2026-10-21"), ["callout:하루 기록"])

    def test_bad_token(self):
        with self.assertRaisesRegex(diary.NotionError, "토큰"):
            diary.check({"token": "nope", "database_id": "db"})

    def test_find_page_without_token(self):
        self.assertIsNone(diary.find_page({"database_id": "db"}, self.DAY))


class Entry(unittest.TestCase):
    def test_sections_and_order(self):
        blocks = diary.build_entry(date(2026, 10, 21), [("수학", True), ("국어", False)], ["09:00–10:00  회의"],
                                   [("haru", "커밋 1"), ("haru", "커밋 2"), ("other", "커밋 3")], "첫 줄\n둘째 줄")
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["heading_3", "to_do", "to_do", "heading_3", "bulleted_list_item",
                                 "heading_3", "bulleted_list_item", "bulleted_list_item", "heading_3",
                                 "paragraph", "paragraph"])
        self.assertTrue(blocks[1]["to_do"]["checked"])
        self.assertFalse(blocks[2]["to_do"]["checked"])
        haru = blocks[6]["bulleted_list_item"]
        self.assertEqual(len(haru["children"]), 2)  # 레포별로 묶임

    def test_empty(self):
        self.assertEqual(diary.build_entry(date(2026, 10, 21), [], [], [], "  \n"), [])

    def test_long_text_is_split(self):
        parts = diary.rich("가" * 4500)
        self.assertEqual([len(p["text"]["content"]) for p in parts], [2000, 2000, 500])

    def test_title(self):
        self.assertEqual(diary.title_for(date(2026, 10, 5)), "2026-10-05")

    def test_section_detection(self):
        box = diary._section([])
        self.assertTrue(diary.is_haru_section(box))
        self.assertFalse(diary.is_haru_section(paragraph(diary.HARU_TITLE)))

    def test_old_haru_blocks(self):
        mine = {"id": "a", "type": "paragraph", "created_by": {"id": BOT}, "last_edited_by": {"id": BOT}}
        edited = {"id": "b", "type": "paragraph", "created_by": {"id": BOT}, "last_edited_by": {"id": USER}}
        theirs = {"id": "c", "type": "paragraph", "created_by": {"id": USER}, "last_edited_by": {"id": USER}}
        self.assertEqual(diary.old_haru_blocks([mine, edited, theirs], BOT), ["a"])
        box = dict(diary._section([]), id="box")
        self.assertEqual(diary.old_haru_blocks([mine, box, theirs], BOT), ["box"])


class LocalFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, value in (("UPLOADED", self.tmp / "uploaded"), ("MEMO_DIR", self.tmp / "memo"),
                            ("CONFIG", self.tmp / "notion.json")):
            p = mock.patch.object(diary, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_uploaded(self):
        d = date(2026, 10, 21)
        self.assertFalse(diary.is_uploaded(d))
        diary.mark_uploaded(d)
        diary.mark_uploaded(d)
        self.assertTrue(diary.is_uploaded(d))
        self.assertEqual(diary.UPLOADED.read_text().split(), ["2026-10-21"])

    def test_memo(self):
        d = date(2026, 10, 21)
        self.assertEqual(diary.read_memo(d), "")
        diary.write_memo(d, "오늘은")
        self.assertEqual(diary.read_memo(d), "오늘은")
        diary.write_memo(d, "   ")  # 비우면 파일을 지운다
        self.assertFalse(diary.memo_path(d).exists())

    def test_broken_memo_file(self):
        d = date(2026, 10, 21)
        diary.memo_path(d).parent.mkdir(parents=True)
        diary.memo_path(d).write_bytes(b"\xff\xfe\x00broken")
        self.assertEqual(diary.read_memo(d), "")

    def test_config_is_private(self):
        diary.save_config({"token": "secret"})
        self.assertEqual(diary.CONFIG.stat().st_mode & 0o777, 0o600)
        self.assertEqual(diary.load_config()["token"], "secret")


class Commits(unittest.TestCase):
    def git(self, *args, date_=None):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM="1")
        if date_:
            env.update(GIT_AUTHOR_DATE=date_, GIT_COMMITTER_DATE=date_)
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, env=env)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / "proj"
        self.repo.mkdir()
        self.gitconfig = self.root / "gitconfig"
        self.gitconfig.write_text("[user]\n\tname = me\n\temail = me@example.com\n[init]\n\tdefaultBranch = main\n")
        p = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.gitconfig), "GIT_CONFIG_NOSYSTEM": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.git("init", "-q")
        for msg, when in (("어제 커밋", "2026-10-20T15:00:00"), ("오늘 커밋 1", "2026-10-21T10:00:00"),
                          ("오늘 커밋 2", "2026-10-21T18:00:00")):
            (self.repo / "f").write_text(msg)
            self.git("add", "f")
            self.git("commit", "-q", "-m", msg, date_=when)
        (self.repo / "f").write_text("작업 중")  # stash 는 빼야 한다
        self.git("stash", date_="2026-10-21T19:00:00")

    def test_only_that_day_and_no_stash(self):
        got = diary.commits(date(2026, 10, 21), self.root)
        self.assertEqual(got, [("proj", "오늘 커밋 1"), ("proj", "오늘 커밋 2")])

    def test_other_authors_are_skipped(self):
        (self.repo / "g").write_text("x")
        self.git("add", "g")
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=other@example.com", "commit", "-q", "-m",
                        "남의 커밋"], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE="2026-10-21T12:00:00",
                                GIT_COMMITTER_DATE="2026-10-21T12:00:00"))
        self.assertNotIn(("proj", "남의 커밋"), diary.commits(date(2026, 10, 21), self.root))


if __name__ == "__main__":
    unittest.main()
