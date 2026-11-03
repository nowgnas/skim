"""페이지 사용성 보강: 복귀 경로, 확인 단계, 빈 상태, 보기 전환을 검증한다."""
from contextlib import closing
import re
import unittest

from test_web import WebTests
from skim.db import connect
from skim.web_settings import RepoLookup, validate_return_query


class UsabilityTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def test_repos_shows_return_link_only_for_valid_pr_number(self):
        body = self.request('/repos?scope_host=github.com&select_q=demo/checkout&from_pr=1')[2]
        self.assertIn('href="/pr/1"', body)
        self.assertIn('name="return_query" value="scope_host=github.com&amp;select_q=demo%2Fcheckout&amp;from_pr=1"', body)
        for bad in ['0', '-1', 'abc', '1e3', '9' * 10, '//evil']:
            with self.subTest(from_pr=bad):
                self.assertNotIn('class="notice"', self.request('/repos?from_pr=' + bad)[2])

    def test_return_query_rejects_non_numeric_pr(self):
        self.assertEqual(validate_return_query('/repos/scope', 'from_pr=3'), 'from_pr=3')
        for bad in ['from_pr=x', 'from_pr=0', 'from_pr=%2F%2Fevil']:
            with self.subTest(query=bad), self.assertRaises(ValueError):
                validate_return_query('/repos/scope', bad)

    def test_policy_save_returns_to_repos_with_pr_link(self):
        code, headers, _ = self.request('/repos/scope', 'POST', {
            'csrf': self.csrf, 'host': 'github.com', 'scope': 'selected', 'repository': 'demo/checkout',
            'shown': '1', 'confirm': 'yes', 'return_query': 'from_pr=1'})
        self.assertEqual((code, headers['Location']), (303, '/repos?from_pr=1'))

    def test_destructive_actions_need_expanding_first(self):
        body = self.request('/repos')[2]
        for action, label in [('/repos/remove', '연결 해제'), ('/hosts/remove', '호스트 삭제')]:
            with self.subTest(action=action):
                start = body.index(f'<summary class="btn danger sm">{label}</summary>')
                block = body[body.rindex('<details class="confirm-action">', 0, start):]
                block = block[:block.index('</details>')]
                self.assertIn(f'{label} 확인</button>', block)
                target = re.search(r'form="([^"]+)"', block)
                if target:  # 범위 저장 폼 안의 버튼은 바깥의 숨은 폼을 제출한다.
                    self.assertRegex(body, rf'<form class="action-form" id="{target[1]}" method="post" action="{action}" hidden>')
                else:
                    self.assertIn(f'action="{action}"', block)

    def test_empty_states_distinguish_filter_from_no_prs(self):
        body = self.request('/?importance=low&pr_q=없는검색어')[2]
        self.assertIn('조건에 맞는 PR이 없습니다', body)
        self.assertNotIn('아직 수집된 열린 PR이 없습니다', body)
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET state='closed'")
        body = self.request('/')[2]
        self.assertIn('아직 수집된 열린 PR이 없습니다', body)
        self.assertIn('href="/repos">저장소 연결하기', body)

    def test_full_diff_is_a_separate_view(self):
        body = self.request('/pr/1?diff=1')[2]
        self.assertIn('id="full-diff"', body)
        self.assertNotIn('name="unit_q"', body)
        self.assertIn('name="unit_q"', self.request('/pr/1')[2])

    def test_home_collect_explains_auto_analysis(self):
        self.assertIn('수집 후 자동 분석될 수 있습니다', self.request('/')[2])


if __name__ == '__main__':
    unittest.main()
