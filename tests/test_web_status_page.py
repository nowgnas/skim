"""상태 화면: 점검 결과를 항목별로 요약하고 작업 기록을 한 목록으로 보여준다."""
from contextlib import closing
import unittest

from test_web import WebTests
from skim import jobs
from skim.db import connect
from skim.web_settings import RepoLookup

RESULT = '\n'.join(['점검 실패 항목이 있습니다. 아래 안내를 따르세요.',
                    '정상: python — 3.11.14 (/usr/bin/python3), 최소 3.11',
                    '정상: gh github.com — 인증 확인',
                    '실패: codex-home — Codex 분석 차단: 초기화 때와 같은 CODEX_HOME 환경인지 확인하세요.',
                    '경고: model — Codex 모델을 알 수 없습니다.',
                    '안내: 조직 SSO는 gh 자격 증명의 SSO 승인을 확인하세요.'])


class StatusPageTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def finish_doctor(self, result, status='failed'):
        with closing(connect(self.config)) as conn:
            row, _ = jobs.enqueue(conn, 'doctor')
            jobs.claim(conn)
            jobs.finish(conn, row['id'], status, result if status == 'completed' else None, None if status == 'completed' else result)

    def health(self, body):
        return body.split('<h2>점검 결과</h2>', 1)[1].split('</section>', 1)[0]

    def test_empty_state_before_first_check(self):
        body = self.request('/status')[2]
        self.assertIn('아직 상태 점검을 하지 않았습니다', self.health(body))
        self.assertEqual(body.count('class="btn primary'), 1)

    def test_problems_first_with_recovery_and_codex_link(self):
        self.finish_doctor(RESULT)
        body = self.request('/status')[2]
        health = self.health(body)
        self.assertIn('확인 필요 2개', health)
        self.assertLess(health.index('Codex 전용 홈'), health.index('<details class="check-ok"><summary>정상 2개</summary>'))
        self.assertIn('분석 준비 초기화를 실행하세요', health)  # 복구 안내가 실패 항목 옆에 붙는다
        self.assertIn('href="#codex-setup"', health)
        self.assertIn('<details id="codex-setup" class="status-section codex-setup" open>', body)
        self.assertIn('GitHub 인증 (github.com)', health)
        self.assertEqual(health.count('href="#codex-setup"'), 1)
        self.assertIn('<span>점검 방금</span>', health)

    def test_all_ok_keeps_codex_setup_collapsed(self):
        self.finish_doctor('모든 점검 항목 정상\n정상: gh github.com — 인증 확인', status='completed')
        body = self.request('/status')[2]
        self.assertIn('모두 정상', self.health(body))
        self.assertIn('<details id="codex-setup" class="status-section codex-setup">', body)

    def test_failed_jobs_marked_in_single_list(self):
        with closing(connect(self.config)) as conn:
            row, _ = jobs.enqueue(conn, 'repos', 0, 'github.com/org/x')
            jobs.claim(conn)
            jobs.finish(conn, row['id'], 'failed', None, '저장소 조회 실패: GitHub 조직 SSO 승인 필요')
        body = self.request('/status')[2]
        records = body.split('<h2>작업 기록</h2>', 1)[1]
        self.assertIn('<article class="job-row job-failed">', records)
        self.assertIn('<p class="job-error">저장소 조회 실패: GitHub 조직 SSO 승인 필요</p>', records)
        self.assertIn('action="/jobs/collect"', body.split('<h2>작업 기록</h2>', 1)[0].rsplit('<section', 1)[1] + records[:2000])
        self.assertNotIn('활성 작업', body)


if __name__ == '__main__':
    unittest.main()
