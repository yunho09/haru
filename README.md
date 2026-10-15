# 하루

GNOME용 가벼운 일정·할 일 관리 앱. GTK4 + libadwaita (Python).

데이터는 직접 저장하지 않고 GNOME의 Evolution Data Server를 그대로 쓴다.
그래서 **설정 → 온라인 계정**에 구글 계정을 추가하면 구글 캘린더 / 구글 할 일과
자동으로 동기화되고, 폰(삼성 캘린더에 구글 계정 연결)과도 같은 일정을 본다.
알림은 `evolution-alarm-notify`가 띄워주므로 앱을 꺼둬도 온다.

## 기능

- 월 달력 + 선택한 날의 일정·마감 할 일
- 일정 추가/수정/삭제, 하루 종일·여러 날 일정, 알림(정시~1일 전), 캘린더 간 이동
- 반복 일정 만들기: 매일·평일·매주·2주마다·매월·매년, 종료일 지정
- 반복 일정은 "이 일정만 / 모든 반복" 으로 수정·삭제 (반복 방식을 바꾸면 전체에 적용)
- 공휴일(구글 "대한민국의 휴일")은 빨간 날로 표시, 기념일은 제외
- 할 일 목록: 추가, 마감일, 완료 체크, 완료됨 접기

## 실행

Ubuntu 기준:

```sh
sudo apt install gir1.2-adw-1 gir1.2-ecal-2.0 gir1.2-edataserver-1.2 gir1.2-ical-3.0
/usr/bin/python3 haru.py
```

앱 메뉴에 등록하려면 `io.github.yunho09.Haru.desktop`을 `~/.local/share/applications/`에 복사한다
(`Exec` 경로를 클론한 위치로 바꿀 것).

## 단축키

| 키 | 동작 |
| --- | --- |
| Ctrl+N | 새 일정 (할 일 탭에서는 입력칸으로) |
| Ctrl+T | 오늘로 |
| Page Up / Page Down | 이전 / 다음 달 |
| 날짜 두 번 클릭 | 그 날짜에 새 일정 |
| Ctrl+Q | 종료 |
