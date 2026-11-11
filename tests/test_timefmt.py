"""화면용 시각 표시: 현지 시각, 상대 표현, 읽을 수 없는 값 보존."""
from datetime import datetime, timedelta, timezone
import os
import time
import unittest
from unittest.mock import patch

from skim.timefmt import duration, human_time, kst_clock

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


class KstClockTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 0, 50, tzinfo=KST)

    def test_clock_is_labelled_kst_with_calendar_prefix(self):
        cases = [('2026-11-11T15:37:10+00:00', '오늘 00:37 KST'),
                 ('2026-11-11T14:20:10+00:00', '어제 23:20 KST'),
                 ('2026-11-09 12:40:00', '11월 9일 21:40 KST'),
                 ('2025-11-03T12:40:00Z', '2025년 11월 3일 21:40 KST')]
        for stored, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(kst_clock(stored, now=self.now), expected)

    def test_clock_ignores_server_timezone(self):
        env = patch.dict(os.environ, TZ='America/New_York')
        env.start()
        self.addCleanup(time.tzset)
        self.addCleanup(env.stop)
        time.tzset()
        self.assertEqual(kst_clock('2026-11-11T15:37:10+00:00', now=self.now), '오늘 00:37 KST')

    def test_unreadable_clock_values_are_kept(self):
        self.assertEqual(kst_clock('대기', now=self.now), '대기')
        self.assertEqual(kst_clock(None, now=self.now), '')

    def test_duration_forms(self):
        start = '2026-11-11T15:00:00+00:00'
        cases = [(0, '0초'), (42, '42초'), (60, '1분'), (124, '2분 4초'), (3600, '1시간'), (3900, '1시간 5분'), (-5, '0초')]
        for seconds, expected in cases:
            with self.subTest(expected=expected):
                end = (datetime(2026, 11, 11, 15, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()
                self.assertEqual(duration(start, end), expected)
        # An open interval runs until now.
        self.assertEqual(duration(start, now=datetime(2026, 11, 11, 15, 3, 1, tzinfo=timezone.utc)), '3분 1초')
        self.assertEqual(duration('대기', start), '')
        self.assertEqual(duration(None), '')


if __name__ == '__main__':
    unittest.main()
