#!/usr/bin/python3
"""하루 실행 파일. 앱 코드는 haruapp/ 에 있다.

    /usr/bin/python3 haru.py            앱 열기
    /usr/bin/python3 haru.py --diary    오늘 날짜 메모칸으로 열기
    /usr/bin/python3 haru.py --remind   (타이머용) 오늘 일기를 안 올렸으면 알림
"""
import sys

from haruapp.app import main

if __name__ == "__main__":
    sys.exit(main())
