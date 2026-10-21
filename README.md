# 하루

GNOME용 가벼운 일정·할 일 관리 앱. GTK4 + libadwaita (Python).

데이터는 직접 저장하지 않고 GNOME의 Evolution Data Server를 그대로 쓴다.
그래서 **설정 → 온라인 계정**에 구글 계정을 추가하면 구글 캘린더 / 구글 할 일과
자동으로 동기화되고, 폰(삼성 캘린더에 구글 계정 연결)과도 같은 일정을 본다.
알림은 `evolution-alarm-notify`가 띄워주므로 앱을 꺼둬도 온다.

## 기능

- 월 달력 + 선택한 날의 일정·마감 할 일, 여러 날 일정은 이어진 막대로 표시
- 월 보기: 일정 막대 가운데를 끌면 다른 날로 옮기기, 막대 왼쪽·오른쪽 끝을 끌면 기간 늘리기·줄이기
  (끄는 동안 미리보기, 놓으면 저장을 기다리지 않고 바로 반영), 빈 칸을 끌어 여러 날 일정 만들기
- 주 보기(시간표): 일정을 끌어 시간·요일 옮기기, 아래 끝을 끌어 길이 조절, 빈 시간대를 끌어 새 일정,
  겹치는 일정은 나란히, 하루 종일 일정은 위쪽 막대, 지금 시각 빨간 줄 (15분 단위)
- 새 일정은 하루 종일이 기본
- 일정 추가/수정/삭제, 하루 종일·여러 날 일정, 알림(정시~1일 전), 캘린더 간 이동
- 반복 일정 만들기: 매일·평일·매주·2주마다·매월·매년, 종료일 지정
- 반복 일정은 "이 일정만 / 모든 반복" 으로 수정·삭제 (반복 방식을 바꾸면 전체에 적용)
- 공휴일(구글 "대한민국의 휴일")은 빨간 날로 표시, 기념일은 제외
- 할 일 목록: 추가, 마감일, 완료 체크, 완료됨 접기
- 일일 투두: 사이드바에서 그 날짜 마감 할 일을 바로 추가·체크 (구글 할 일과 동기화)
- 날짜별 메모 (`~/.local/share/haru/memo/`)
- **노션 일기에 올리기**: 그날의 할 일(체크 상태)·일정·git 커밋(`/data/project` 아래 레포)·메모를
  노션 🎒 일기 데이터베이스의 `2026-10-15` 페이지에 "📒 하루 기록" 상자로 올림. 다시 올리면 그 상자만
  바뀌고, 노션에서 직접 쓴 글은 그대로 둠
- **밤 일기 알림**: 밤 10시에 그날 일기를 아직 안 올렸으면 알림, 누르면 오늘 메모칸으로 열림 (자동 업로드는 안 함)
- Ctrl+Z 되돌리기, 오류 로그 `~/.local/state/haru/haru.log`

## 실행

Ubuntu 기준:

```sh
sudo apt install gir1.2-adw-1 gir1.2-ecal-2.0 gir1.2-edataserver-1.2 gir1.2-ical-3.0
/usr/bin/python3 haru.py
```

앱 메뉴에 등록하려면 `io.github.yunho09.Haru.desktop`을 `~/.local/share/applications/`에 복사한다
(`Exec` 경로를 클론한 위치로 바꿀 것).

libadwaita 1.7 이상(GNOME 48+)이 필요하다 (월/주 전환 토글).

## 구조

| 파일 | 내용 |
| --- | --- |
| `haru.py` | 실행 파일 (`--diary`: 오늘 메모칸으로, `--remind`: 밤 일기 알림) |
| `haruapp/core.py` | 날짜·시간 변환, 일정·할 일 데이터, 반복 규칙, 드래그·겹침 계산 — GTK 없이 테스트 가능 |
| `haruapp/store.py` | Evolution Data Server 연결·조회·쓰기·되돌리기, 연결 지연 감지·서비스 재시작 |
| `haruapp/window.py` | 메인 창: 월 보기, 사이드바(일정·할 일·메모), 할 일 탭, 드래그, 되돌리기 |
| `haruapp/weekview.py` | 주 보기 (cairo 로 직접 그리는 시간표) |
| `haruapp/dialogs.py`, `widgets.py` | 일정·할 일·노션 연결 창, 날짜·시간 입력 |
| `haruapp/diary.py` | 노션 일기 올리기, 메모·업로드 기록 파일 |
| `haruapp/app.py`, `log.py` | 앱 시작·명령줄·알림, 오류 로그 |

## 테스트

```sh
/usr/bin/python3 -m unittest discover -s tests -t .          # 핵심 로직 + 노션(가짜 서버)
HARU_EDS_TESTS=1 /usr/bin/python3 -m unittest tests.test_store_eds   # 실제 로컬 캘린더로 쓰기·되돌리기
```

마지막 것은 데스크톱 세션의 로컬 '개인' 캘린더에 `haru-test-` 일정을 만들었다 지운다 (구글 캘린더는 안 건드림).
GitHub Actions 는 앞의 것만 돌린다.

## 캘린더가 안 뜰 때

구글 일정이 안 보이고 위쪽에 "연결이 늦어요" 띠가 뜨면, GNOME 캘린더 서비스(evolution-source-registry)가
멈춘 것이다. 띠의 "다시 연결"을 누르면 서비스를 다시 띄운다. 수동으로는:

```sh
systemctl --user restart evolution-source-registry evolution-calendar-factory
```

## 노션 연결

1. <https://www.notion.so/profile/integrations> 에서 내부 통합을 만들고 토큰 복사
2. 일기 데이터베이스 `⋯` → 연결 → 그 통합 추가
3. 앱에서 "노션 일기에 올리기"를 누르면 뜨는 창에 토큰 붙여넣기

설정은 `~/.config/haru/notion.json` (권한 600)에 저장된다. 데이터베이스나 레포 위치를 바꾸려면
이 파일의 `database_id`, `project_root` 를 고친다.

## 밤 일기 알림

```sh
cp systemd/haru-diary-remind.* ~/.config/systemd/user/   # ExecStart 경로를 클론한 위치로
systemctl --user daemon-reload
systemctl --user enable --now haru-diary-remind.timer
```

시각을 바꾸려면 `haru-diary-remind.timer` 의 `OnCalendar` 를 고친다. 그날 일기가 이미 있으면 알리지 않고,
절전 중에 지나간 알림이 다음 날 아침에 뜨지 않게 21시 이후에만 알린다.

## 단축키

| 키 | 동작 |
| --- | --- |
| Ctrl+N | 새 일정 (할 일 탭에서는 입력칸으로) |
| Ctrl+T | 오늘로 |
| Ctrl+Z | 되돌리기 (추가·수정·드래그·삭제·할 일 체크, 최근 30개) |
| ← → / ↑ ↓ | 하루 / 일주일 이동 (달 경계 넘으면 자동으로 다음 달) |
| Page Up / Page Down | 이전 / 다음 달 (주 보기에서는 주) |
| 날짜 두 번 클릭 | 그 날짜에 새 일정 |
| Ctrl+Q | 종료 |
