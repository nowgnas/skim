"""상태 화면: 점검 결과를 항목별로 요약하고 작업 기록을 한 목록으로 보여준다."""
from contextlib import closing
import json
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
        self.assertIn('전용 홈 재설정', health)
        self.assertIn('<details id="codex-setup" class="status-section codex-setup" open>', body)
        self.assertIn('GitHub 인증 (github.com)', health)
        self.assertEqual(health.count('name="action" value="reset"'), 1)
        self.assertIn('<span>점검 방금</span>', health)

    def test_all_ok_keeps_codex_setup_collapsed(self):
        self.finish_doctor('모든 점검 항목 정상\n정상: gh github.com — 인증 확인', status='completed')
        body = self.request('/status')[2]
        self.assertIn('모두 정상', self.health(body))
        self.assertIn('<details id="codex-setup" class="status-section codex-setup">', body)

    def test_failed_doctor_result_is_visible_in_history_and_payload(self):
        with closing(connect(self.config)) as conn:
            row, _ = jobs.enqueue(conn, 'doctor')
            jobs.claim(conn)
            jobs.finish(conn, row['id'], 'failed', RESULT, None)
        card = self.record(self.request('/status')[2], row['id'])
        self.assertIn('<article class="job-row job-failed">', card)
        error = '<p class="job-error">' + RESULT.splitlines()[0] + '</p>'
        self.assertIn(error, card)
        self.assertLess(card.index(error), card.index('<details'))
        payload = json.loads(self.request('/jobs/' + str(row['id']))[2])
        self.assertIsNone(payload['error'])
        self.assertEqual(payload['record']['error_line'], RESULT.splitlines()[0])

    def test_failed_and_blocked_history_style_without_error_text(self):
        for status in ('failed', 'blocked'):
            with self.subTest(status=status), closing(connect(self.config)) as conn:
                row, _ = jobs.enqueue(conn, 'doctor')
                jobs.claim(conn)
                jobs.finish(conn, row['id'], status, None, None)
                card = self.record(self.request('/status')[2], row['id'])
                self.assertIn('<article class="job-row job-failed">', card)
                self.assertIn('<p class="job-error" hidden></p>', card)

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

    def record(self, body, job_id):
        records = body.split('<h2>작업 기록</h2>', 1)[1]
        return '<article' + records.split(f'data-job-id="{job_id}"', 1)[0].rsplit('<article', 1)[1] + records.split(f'data-job-id="{job_id}"', 1)[1].split('</article>', 1)[0]

    def test_history_card_shows_badge_target_kst_times_duration_and_outcome(self):
        with closing(connect(self.config)) as conn, conn:
            done, _ = jobs.enqueue(conn, 'repos', 0, 'github.com/demo-org')
            jobs.claim(conn)
            jobs.finish(conn, done['id'], 'completed', '저장소 3개를 찾았습니다.\n둘째 줄')
            conn.execute("UPDATE jobs SET started_at='2026-11-01T12:40:00+00:00',finished_at='2026-11-01T12:42:04+00:00' WHERE id=?", (done['id'],))
        card = self.record(self.request('/status')[2], done['id'])
        self.assertIn('<article class="job-row">', card)
        self.assertIn('<span class="badge job-status ok" aria-live="polite" data-job-status>완료</span>', card)
        self.assertIn(f'<h3 class="job-title">저장소 조회 #{done["id"]}</h3><span class="job-target">github.com/demo-org</span>', card)
        self.assertRegex(card, r'<span class="job-fact-label">시작</span><span>[^<]*21:40 KST</span>')
        self.assertRegex(card, r'<span class="job-fact-label">종료</span><span>[^<]*21:42 KST</span>')
        self.assertIn('<span class="job-fact-label">소요</span><span>2분 4초</span>', card)
        # The one-line outcome is readable without opening the details.
        self.assertIn('<p class="job-note">저장소 3개를 찾았습니다.</p>', card)
        self.assertIn('<p class="job-error" hidden></p>', card)
        self.assertIn('둘째 줄</pre>', card)

    def test_running_job_is_prominent_and_waiting_job_hides_duration(self):
        with closing(connect(self.config)) as conn:
            waiting, _ = jobs.enqueue(conn, 'doctor')
            body = self.request('/status')[2]
            card = self.record(body, waiting['id'])
            self.assertIn('<article class="job-row">', card)
            self.assertIn('data-job-status>대기 중</span>', card)
            self.assertIn('<span class="job-fact-label">요청</span>', card)
            self.assertNotIn('소요', card)
            self.assertNotIn('경과', card)
            self.assertNotIn('job-target', card)
            jobs.claim(conn)
            running = conn.execute("SELECT * FROM jobs WHERE status='running'").fetchone()
            jobs.progress(conn, running['id'], '저장소 수집 중', 2, 5)
        body = self.request('/status')[2]
        card = self.record(body, running['id'])
        self.assertIn('<article class="job-row job-running">', card)
        self.assertIn('<span class="badge job-status running" aria-live="polite" data-job-status>실행 중</span>', card)
        self.assertIn(f'data-job-id="{running["id"]}" data-job-active="1" data-job-record="1"', body)
        self.assertIn('<span class="job-fact-label">경과</span>', card)
        self.assertIn('KST</span>', card)
        self.assertIn('<p class="job-note">저장소 수집 중 2/5</p>', card)
        self.assertIn('aria-label="작업 진행" max="5" value="2"', card)

    def test_poll_payload_carries_the_card_fields(self):
        with closing(connect(self.config)) as conn:
            row, _ = jobs.enqueue(conn, 'repos', 0, 'github.com/org/x')
            jobs.claim(conn)
            jobs.finish(conn, row['id'], 'blocked', None, '차단 사유 첫 줄\n자세한 안내')
        payload = json.loads(self.request('/jobs/' + str(row['id']))[2])
        view = payload['record']
        self.assertEqual((view['status_label'], view['tone'], view['error_line'], view['note']), ('차단', 'high', '차단 사유 첫 줄', ''))
        self.assertEqual([fact[0] for fact in view['facts']], ['시작', '종료', '소요'])
        self.assertTrue(all(len(fact) == 3 for fact in view['facts']))
        self.assertTrue(view['facts'][0][1].endswith(' KST'))
        # The summary contract used by the other job boxes is unchanged.
        self.assertEqual(payload['summary'], ', '.join(payload['summary_parts']))
        self.assertIn('<article class="job-row job-failed">', self.record(self.request('/status')[2], row['id']))


if __name__ == '__main__':
    unittest.main()
