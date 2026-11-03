"""저장소 화면: 호스트별 저장소 목록과 분석 범위를 한 카드에서 다룬다."""
from contextlib import closing
import re
import unittest

from test_web import WebTests
from skim.db import connect, analysis_allowed
from skim.web_settings import RepoLookup


class ReposPageTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def card(self, query=''):
        return self.request('/repos' + query)[2].split('id="analysis-scope"', 1)[1].split('</section>', 1)[0]

    def test_one_list_per_host_with_scope_and_actions(self):
        body = self.request('/repos')[2]
        self.assertEqual(body.count('type="checkbox" name="repository" value="demo/checkout"'), 1)  # 저장소가 두 목록에 중복되지 않음
        card = self.card()
        self.assertIn('demo/checkout', card)
        self.assertIn('열린 PR', card)
        self.assertIn('class="badge ok">분석 허용', card)
        self.assertNotIn('demo-org/intranet', card)
        self.assertIn('href="/repos?scope_host=github.corp.example#analysis-scope"', card)
        self.assertNotIn('id="connected-repositories" class="repo-section"', body)

    def test_remove_button_submits_separate_form(self):
        body = self.request('/repos')[2]
        form_id = re.search(r'<button class="btn danger sm" form="(remove-\d+)"', body)[1]
        removal = re.search(rf'<form class="action-form" id="{form_id}"[^>]*>(.*?)</form>', body)[1]
        self.assertIn('name="repository" value="demo/checkout"', removal)
        self.assertIn('name="csrf"', removal)
        code = self.request('/repos/remove', 'POST', {'csrf': self.csrf, 'host': 'github.com', 'repository': 'demo/checkout'})[0]
        self.assertEqual(code, 303)
        # 연결을 끊어도 분석 선택은 남으므로 목록에 '연결 해제됨'으로 남는다.
        card = self.card()
        self.assertIn('연결 해제됨', card)
        with closing(connect(self.config)) as conn:
            self.assertTrue(analysis_allowed(conn, 1))

    def test_add_lives_inside_card_and_uses_current_host(self):
        body = self.request('/repos')[2]
        card = self.card()
        self.assertIn('<details class="repo-add" id="add-repository"><summary class="btn secondary sm">+ 저장소 추가</summary>', card)
        self.assertNotIn('class="repo-section repo-discovery"', body)
        add = card.split('id="add-repository"', 1)[1]
        self.assertIn('action="/repos/load"', add)
        self.assertNotIn('<select name="host"', add)  # 호스트는 탭에서 고른 값을 쓴다
        self.assertIn('<input type="hidden" name="host" value="github.com">', add)
        self.assertIn('<details class="direct-connect"><summary>목록에 없는 저장소를 주소로 연결</summary>', add)
        other = self.card('?scope_host=github.corp.example').split('id="add-repository"', 1)[1]
        self.assertIn('<input type="hidden" name="host" value="github.corp.example">', other)
        self.assertNotIn('name="available_q"', body)

    def test_pending_add_shows_at_top_and_reloads_on_success(self):
        from skim import jobs
        with closing(connect(self.config)) as conn:
            jobs.enqueue(conn, 'repos', 0, 'github.com/new/repo', params={'repository': 'new/repo', 'source': 'responsible', 'host': 'github.com'})
        card = self.card()
        repos = card.split('class="scope-repos repo-list"', 1)[1]
        self.assertLess(repos.index('new/repo'), repos.index('demo/checkout'))
        self.assertIn('연결 확인 중', repos)
        self.assertIn('data-job-reload="1"', repos)
        self.assertIn('<details class="repo-add" id="add-repository" open>', card)  # 진행 중이면 패널을 펼친다
        self.assertNotIn('new/repo', self.card('?scope_host=github.corp.example').split('class="scope-repos repo-list"', 1)[1])

    def test_search_only_when_needed(self):
        self.assertNotIn('name="tracked_q"', self.card())
        with closing(connect(self.config)) as conn, conn:
            for index in range(25):
                conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('many',?,1)", (f'r{index:02d}',))
        self.assertIn('name="tracked_q"', self.card())

if __name__ == '__main__':
    unittest.main()
