"""화면용 시각 표시: 현지 시각, 상대 표현, 읽을 수 없는 값 보존."""
from datetime import datetime, timedelta, timezone
import os
import time
import unittest
from unittest.mock import patch

from skim.timefmt import human_time

KST = timezone(timedelta(hours=9))


class HumanTimeTests(unittest.TestCase):
    def setUp(self):
        # 현지 시간대를 고정해 실행 환경에 상관없이 같은 결과를 확인한다.
        env = patch.dict(os.environ, TZ='Asia/Seoul')
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(time.tzset)
        time.tzset()
        self.now = datetime(2026, 11, 4, 10, 0, tzinfo=KST)

    def test_relative_and_calendar_forms(self):
        cases = [(self.now - timedelta(seconds=20), '방금'),
                 (self.now - timedelta(minutes=5), '5분 전'),
                 (self.now - timedelta(hours=2), '오늘 08:00'),
                 (self.now - timedelta(days=1), '어제 10:00'),
                 (self.now - timedelta(days=10), '10월 25일 10:00'),
                 (self.now - timedelta(days=400), '2025년 9월 30일')]
        for moment, expected in cases:
            with self.subTest(expected=expected):
                stored = moment.astimezone(timezone.utc).isoformat()
                self.assertEqual(human_time(stored, now=self.now), expected)

    def test_storage_formats_are_utc_and_converted_to_local(self):
        # SQLite CURRENT_TIMESTAMP(시간대 없음)와 Z 표기 모두 UTC로 읽는다.
        self.assertEqual(human_time('2026-11-03 15:09:07', now=self.now), '오늘 00:09')
        self.assertEqual(human_time('2026-11-03T15:09:07.462542+00:00', now=self.now), '오늘 00:09')
        self.assertEqual(human_time('2026-11-03T15:09:07Z', now=self.now), '오늘 00:09')

    def test_unreadable_values_are_kept(self):
        for value in ('shared-time', '대기', ''):
            self.assertEqual(human_time(value, now=self.now), value)
        self.assertEqual(human_time(None, now=self.now), '')
        # 미래 시각(재시도 예정 등)은 상대 표현 없이 날짜와 시각으로 보여준다.
        self.assertEqual(human_time((self.now + timedelta(minutes=30)).isoformat(), now=self.now), '오늘 10:30')


if __name__ == '__main__':
    unittest.main()
