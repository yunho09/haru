"""Evolution Data Server 연결·조회·쓰기·되돌리기."""
import uuid
from datetime import date, datetime, time, timedelta, timezone

import gi

gi.require_version("EDataServer", "1.2")
gi.require_version("ECal", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import ECal, EDataServer, Gio, GLib, GObject, ICalGLib  # noqa: E402

from .core import TZ, ical_to_py, as_date, text_of, Event, Task  # noqa: E402
from .log import log  # noqa: E402


class Store:
    """EDS 캘린더/할 일 목록 연결과 실시간 갱신을 관리한다."""

    def __init__(self, on_changed):
        self.on_changed = on_changed
        self.registry = EDataServer.SourceRegistry.new_sync(None)
        self.clients = {}  # source uid → ECal.Client
        self.views = []
        self.colors = {}
        self._pending = None
        self._gen = 0
        self.on_error = log
        self.on_undoable = None
        self.busy = 0  # 진행 중인 쓰기 수
        self.on_slow = None  # (이름들) — 연결이 20초 넘게 안 될 때
        self.on_all_connected = None
        self._connecting = {}  # 연결 중인 source uid → 이름
        self._revive = None
        self._watch_registry()
        self.reconnect()

    def _watch_registry(self):
        for sig in ("source-added", "source-removed", "source-enabled", "source-disabled"):
            self.registry.connect(sig, lambda *_: self.reconnect())

    # ── 연결 ──
    def _sources(self, ext):
        return [s for s in self.registry.list_sources(ext) if s.get_enabled()]

    def reconnect(self):
        # 이전 세대에서 아직 진행 중인 연결은 도착해도 버린다
        self._gen += 1
        for v in self.views:
            try:
                v.stop()
            except GLib.Error:
                pass
        self.views = []
        self.clients = {}
        self.colors = {}
        for ext, kind in (
            (EDataServer.SOURCE_EXTENSION_CALENDAR, ECal.ClientSourceType.EVENTS),
            (EDataServer.SOURCE_EXTENSION_TASK_LIST, ECal.ClientSourceType.TASKS),
        ):
            for src in self._sources(ext):
                self.colors[src.get_uid()] = src.get_extension(ext).get_color() or "#62a0ea"
                self._connecting[src.get_uid()] = src.get_display_name()
                ECal.Client.connect(src, kind, 30, None, self._on_connected, (self._gen, src.get_uid()))
        GLib.timeout_add_seconds(20, self._check_slow, self._gen)
        self.changed()

    def _check_slow(self, gen):
        # 캘린더 서비스(EDS)가 멈추면 연결이 끝나지도 실패하지도 않는다 → 알려서 다시 띄울 수 있게
        if gen == self._gen and self._connecting and self.on_slow:
            log("연결이 늦음:", ", ".join(self._connecting.values()))
            self.on_slow(sorted(set(self._connecting.values())))
        return False

    def _done_connecting(self, gen, uid):
        if gen != self._gen:
            return
        self._connecting.pop(uid, None)
        if not self._connecting and self.on_all_connected:
            self.on_all_connected()

    def _on_connected(self, _src, res, data):
        gen, src_uid = data
        self._done_connecting(gen, src_uid)
        try:
            client = ECal.Client.connect_finish(res)
        except GLib.Error as e:
            log("연결 실패:", e.message)
            return
        uid = client.get_source().get_uid()
        src = self.source(uid)
        if gen != self._gen or src is None or not src.get_enabled():
            return
        client.set_default_timezone(TZ)
        # 캘린더 서비스가 재시작되면 이 연결은 죽는다 → 잠시 뒤 전부 다시 연결
        # ECal.Client.connect 는 '캘린더에 연결'하는 정적 함수라 신호 연결은 GObject 쪽으로 부른다
        GObject.Object.connect(client, "backend-died", lambda *_: self._backend_died())
        self.clients[uid] = client
        client.get_view("#t", None, self._on_view, gen)
        self.changed()

    def _on_view(self, client, res, gen):
        try:
            ok, view = client.get_view_finish(res)
        except GLib.Error:
            return
        if gen != self._gen:
            return
        for sig in ("objects-added", "objects-modified", "objects-removed"):
            view.connect(sig, lambda *_: self.changed())
        view.start()
        self.views.append(view)

    def _backend_died(self):
        if self._revive:
            return
        log("캘린더 서비스 연결이 끊겨 다시 연결")

        def revive():
            self._revive = None
            self.reconnect()
            return False

        self._revive = GLib.timeout_add_seconds(2, revive)

    def restart_services(self, done):
        """멈춘 캘린더 서비스(EDS)를 다시 띄우고 다시 연결한다. done(err|None)"""
        cmd = ["sh", "-c", "systemctl --user restart evolution-source-registry && sleep 1"
               " && systemctl --user restart evolution-calendar-factory"]
        try:
            proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            done(e.message)
            return

        def finished(p, res, _data):
            try:
                p.wait_check_finish(res)
            except GLib.Error as e:
                log("캘린더 서비스 재시작 실패:", e.message)
                done(e.message)
                return
            log("캘린더 서비스를 다시 띄움")
            self.registry = EDataServer.SourceRegistry.new_sync(None)  # 새로 뜬 레지스트리에 다시 붙는다
            self._watch_registry()
            self.reconnect()
            done(None)

        proc.wait_check_async(None, finished, None)

    def changed(self):
        """여러 신호가 몰려와도 한 번만 다시 그리도록 묶는다."""
        if self._pending:
            return

        def fire():
            self._pending = None
            self.on_changed()
            return False

        self._pending = GLib.timeout_add(120, fire)

    # ── 조회 ──
    def source(self, uid):
        return self.registry.ref_source(uid)

    def name(self, uid):
        s = self.source(uid)
        return s.get_display_name() if s else uid

    def is_holiday_source(self, uid):
        return "휴일" in self.name(uid) or "holiday" in self.name(uid).lower()

    def _clients(self, kind):
        return [
            (uid, c) for uid, c in list(self.clients.items())
            if c.get_source_type() == kind
        ]

    def writable(self, kind):
        """쓰기 가능한 캘린더 목록. 구글(이메일 이름) 캘린더를 맨 앞에."""
        items = [(uid, c) for uid, c in self._clients(kind)
                 if not c.is_readonly() and uid != "birthdays" and self.source(uid) is not None]
        items.sort(key=lambda x: (0 if self.source(x[0]).get_parent() not in ("local-stub", None) else 1, self.name(x[0])))
        return [uid for uid, _ in items]

    def events(self, start: date, end: date):
        out = []
        t0 = int(datetime.combine(start, time()).timestamp())
        t1 = int(datetime.combine(end, time()).timestamp())
        utc = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sexp = f'(occur-in-time-range? (make-time "{utc(t0)}") (make-time "{utc(t1)}"))'
        for uid, client in self._clients(ECal.ClientSourceType.EVENTS):
            holiday_src = self.is_holiday_source(uid)

            def cb(icomp, s, e, *_a, uid=uid, holiday_src=holiday_src):
                comp = ECal.Component.new_from_icalcomponent(icomp.clone())
                if comp is None:
                    return True
                st, en = ical_to_py(s), ical_to_py(e)
                all_day = not isinstance(st, datetime)
                if en is None:
                    en = st + (timedelta(days=1) if all_day else timedelta(0))
                if all_day and isinstance(en, datetime):
                    en = en.date()
                rid = comp.get_recurid_as_string() or ""
                desc = comp.get_descriptions()
                # 구글 '대한민국의 휴일'은 설명이 "공휴일"/"기념일" 로 시작한다. 기념일은 쉬는 날이 아님.
                day_off = holiday_src and not (desc and text_of(desc[0]).startswith("기념일"))
                out.append(Event(
                    uid=comp.get_uid(), rid=rid, source_uid=uid,
                    summary=text_of(comp.get_summary()) or "(제목 없음)",
                    location=comp.get_location() or "",
                    start=st, end=en, all_day=all_day, icomp=comp.get_icalcomponent(),
                    recurring=bool(rid) or comp.has_recurrences(), day_off=day_off,
                ))
                return True

            try:
                ok, comps = client.get_object_list_as_comps_sync(sexp, None)
            except GLib.Error as e:
                log("일정 읽기 실패:", e.message)
                continue
            # 반복 일정에서 떼어낸 회차는 원본을 펼칠 때 같이 나온다.
            # (generate_instances_sync 는 떼어낸 회차를 무시해서 쓰지 않는다)
            masters = {c.get_uid() for c in comps if not c.get_recurid_as_string()}
            for comp in comps:
                if comp.get_recurid_as_string() and comp.get_uid() in masters:
                    continue
                try:
                    client.generate_instances_for_object_sync(comp.get_icalcomponent(), t0, t1, None, cb)
                except GLib.Error as e:
                    log("일정 펼치기 실패:", e.message)
        out.sort(key=lambda ev: (as_date(ev.start), 0 if ev.all_day else 1, time() if ev.all_day else ev.start.time(), ev.summary))
        return out

    def tasks(self):
        out = []
        for uid, client in self._clients(ECal.ClientSourceType.TASKS):
            try:
                ok, comps = client.get_object_list_as_comps_sync("#t", None)
            except GLib.Error:
                continue
            for comp in comps:
                due = comp.get_due()
                d = ical_to_py(due.get_value()) if due else None
                if isinstance(d, datetime):
                    d = d.date()
                done_at = ical_to_py(comp.get_completed()) if comp.get_completed() else None
                out.append(Task(
                    uid=comp.get_uid(), source_uid=uid,
                    summary=text_of(comp.get_summary()) or "(제목 없음)",
                    due=d, done=comp.get_status() == ICalGLib.PropertyStatus.COMPLETED,
                    icomp=comp.get_icalcomponent(), completed=as_date(done_at) if done_at else None,
                ))
        out.sort(key=lambda t: (t.due is None, t.due or date.max, t.summary))
        return out

    # ── 쓰기 (모두 비동기, 결과는 done(err|None)) ──
    # 쓰기 직전 모습을 찍어 두었다가 on_undoable(알림 문구, 제목, 되돌릴 목록) 으로 알린다.
    # 되돌릴 목록 = [(캘린더, uid, 바꾸기 전 iCal 컴포넌트들)] — 빈 목록이면 "원래 없던 것".
    def snapshot(self, source_uid, uid):
        client = self.clients.get(source_uid)
        if client is None or not uid:
            return []
        try:
            ok, comps = client.get_objects_for_uid_sync(uid, None)
        except GLib.Error:
            return []
        return [c.get_icalcomponent().clone() for c in comps or []]

    def _record(self, label, summary, entries):
        if self.on_undoable:
            self.on_undoable(label, summary, entries)

    def _client(self, source_uid, done):
        client = self.clients.get(source_uid)
        if client is None:
            done("캘린더에 다시 연결하는 중이에요. 잠시 후 다시 해주세요")
        return client

    def create(self, source_uid, comp, done, label=None, record=True):
        if not (client := self._client(source_uid, done)):
            return

        def recorded(out):
            uid = out[-1] if isinstance(out, tuple) else out if isinstance(out, str) else None
            if record:
                self._record(label, text_of(comp.get_summary()), [(source_uid, uid or comp.get_uid(), [])])

        client.create_object(comp.get_icalcomponent(), ECal.OperationFlags.NONE, None,
                             self._finish("create", done, recorded), None)

    def modify(self, source_uid, comp, mod, done, label=None, record=True):
        if not (client := self._client(source_uid, done)):
            return
        before = self.snapshot(source_uid, comp.get_uid()) if record else None
        recorded = (lambda _out: self._record(label, text_of(comp.get_summary()),
                                              [(source_uid, comp.get_uid(), before)])) if record else None
        client.modify_object(comp.get_icalcomponent(), mod, ECal.OperationFlags.NONE, None,
                             self._finish("modify", done, recorded), None)

    def remove(self, source_uid, uid, rid, mod, done, label=None, record=True):
        if not (client := self._client(source_uid, done)):
            return
        before = self.snapshot(source_uid, uid) if record else None
        summary = text_of(ECal.Component.new_from_icalcomponent(before[0].clone()).get_summary()) if before else ""
        recorded = (lambda _out: self._record(label, summary, [(source_uid, uid, before)])) if record else None
        client.remove_object(uid, rid or None, mod, ECal.OperationFlags.NONE, None,
                             self._finish("remove", done, recorded), None)

    def move(self, old_src, new_src, comp, done, label=None):
        """다른 캘린더로 옮기기 = 새 캘린더에 만들고 원래 것을 지운다."""
        uid = comp.get_uid()
        before = self.snapshot(old_src, uid)
        new = comp.clone()
        new.set_uid(str(uuid.uuid4()))
        # 원래 캘린더에 묶인 정보(초대 조직자/참석자, 서버 전용 X- 속성)는 가져가지 않는다
        new.set_organizer(None)
        new.set_attendees(None)
        ic = new.get_icalcomponent()
        xs, p = [], ic.get_first_property(ICalGLib.PropertyKind.X_PROPERTY)
        while p is not None:
            xs.append(p)
            p = ic.get_next_property(ICalGLib.PropertyKind.X_PROPERTY)
        for p in xs:
            ic.remove_property(p)

        def after_remove(err):
            if err:
                # 새 캘린더에는 이미 만들어졌으니 저장은 성공으로 처리 (다시 누르면 중복됨)
                self.on_error(f"옮긴 뒤 원래 일정 삭제 실패: {err}")
            else:
                self._record(label, text_of(comp.get_summary()), [(new_src, new.get_uid(), []), (old_src, uid, before)])
            done(None)

        def after_create(err):
            if err:
                done(err)
            else:
                self.remove(old_src, uid, None, ECal.ObjModType.ALL, after_remove, record=False)

        self.create(new_src, new, after_create, record=False)

    def restore(self, entries, done):
        """찍어 둔 모습으로 되돌린다."""
        def rid_of(ic):
            return ECal.Component.new_from_icalcomponent(ic.clone()).get_recurid_as_string() or ""

        steps, late = [], []
        for src, uid, snap in entries:
            client = self.clients.get(src)
            if client is None:
                done("캘린더에 다시 연결하는 중이에요. 잠시 후 다시 해주세요")
                return
            cur = {rid_of(i): i for i in self.snapshot(src, uid)}
            old = {rid_of(i): i for i in snap}
            if not old:  # 원래 없던 것 → 지운다. 단 되살리기가 다 끝난 뒤에 (옮기기 되돌리다 둘 다 잃지 않게)
                if cur:
                    late.append(("remove", client, uid, None, ECal.ObjModType.ALL))
                continue
            # 나중에 떼어낸 회차부터 지운다 (지우면 원본에 '제외'가 붙으니, 원본 복원은 그 다음에)
            for rid in cur:
                if rid and rid not in old:
                    steps.append(("remove", client, uid, rid, ECal.ObjModType.THIS))
            for rid in sorted(old):  # "" (원본) 먼저
                comp = ECal.Component.new_from_icalcomponent(old[rid].clone())
                mod = ECal.ObjModType.ALL if not rid else ECal.ObjModType.THIS
                if rid in cur:
                    # 서버가 옛 버전으로 보고 거절하지 않게 순번은 지금 것보다 올린다
                    seq = ECal.Component.new_from_icalcomponent(cur[rid].clone()).get_sequence()
                    comp.set_sequence(max(seq, comp.get_sequence()) + 1)
                    steps.append(("modify", client, comp.get_icalcomponent(), None, mod))
                elif not rid:
                    steps.append(("create", client, comp.get_icalcomponent(), None, None))
                else:  # 떼어냈던 회차 다시 떼어내기
                    steps.append(("modify", client, comp.get_icalcomponent(), None, mod))

        steps += late
        remap = {}  # 새 uid 로 다시 만든 원본 → 뒤따르는 회차 단계도 새 uid 로

        def run(i=0, err=None):
            if err or i == len(steps):
                self.changed()
                done(err)
                return
            kind, client, a, b, mod = steps[i]
            nxt = lambda e: run(i + 1, e)  # noqa: E731
            flags = ECal.OperationFlags.NONE
            if kind == "remove":
                a = remap.get(a, a)
            elif a.get_uid() in remap:
                a = a.clone()
                a.set_uid(remap[a.get_uid()])
            if kind == "create":
                def retry(e, a=a, client=client):
                    if not e:
                        return nxt(None)
                    # 같은 uid 로 다시 못 만드는 서버(구글 등)면 새 uid 로
                    ic = a.clone()
                    ic.set_uid(str(uuid.uuid4()))
                    remap[a.get_uid()] = ic.get_uid()
                    client.create_object(ic, flags, None, self._finish("create", nxt), None)
                client.create_object(a, flags, None, self._finish("create", retry), None)
            elif kind == "modify":
                client.modify_object(a, mod, flags, None, self._finish("modify", nxt), None)
            else:
                client.remove_object(a, b, mod, flags, None, self._finish("remove", nxt), None)

        run()

    def master(self, source_uid, uid):
        client = self.clients.get(source_uid)
        if client is None:
            return None
        try:
            ok, icomp = client.get_object_sync(uid, None, None)
        except GLib.Error:
            return None
        return ECal.Component.new_from_icalcomponent(icomp)

    def _finish(self, op, done, on_success=None):
        self.busy += 1  # 비동기 호출 바로 전에 만들어진다

        def cb(client, res, _data):
            self.busy = max(self.busy - 1, 0)
            out = None
            try:
                out = getattr(client, f"{op}_object_finish")(res)
                err = None
            except GLib.Error as e:
                err = e.message
            self.changed()
            if err is None and on_success is not None:
                on_success(out)
            done(err)

        return cb
