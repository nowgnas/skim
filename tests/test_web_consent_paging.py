"""PR 상세의 분석 동의 진입, 목록 페이징, 분석 0건 사유를 검증한다."""
from contextlib import closing
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_web import WebTests
from skim import jobs
from skim.db import connect, analysis_allowed
from skim.jobs import Runner
from skim.paging import PAGE_SIZE, paginate
from skim.web_settings import RepoLookup


class ConsentTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE repositories SET analysis_policy='inherit' WHERE owner='demo'")

    def allowed(self):
        with closing(connect(self.config)) as conn:
            return analysis_allowed(conn, 1)

    def consent(self, **extra):
        data = dict(csrf=self.csrf, host='github.com', scope='repo', target='demo/checkout',
                    policy='allow', confirm='yes', next='/pr/1')
        data.update(extra)
        return self.request('/settings/analysis', 'POST', {k: v for k, v in data.items() if v is not None})

    def test_detail_offers_repo_consent_form_when_not_allowed(self):
        body = self.request('/pr/1')[2]
        form = re.search(r'<form method="post" action="/settings/analysis">.*?</form>', body).group(0)
        self.assertIn('name="next" value="/pr/1"', form)
        self.assertIn('name="target" value="demo/checkout"', form)
        self.assertIn('name="scope" value="repo"', form)
        self.assertIn('name="confirm"', form)
        # 조직 허용은 상세 화면에서 열지 않는다.
        self.assertNotIn('value="org"', form)
        self.assertNotIn('action="/jobs/analyze"', body)

    def test_explicit_deny_hides_consent_form(self):
        for repo_policy, org_policy in [('deny', None), ('inherit', 'deny')]:
            with self.subTest(repo=repo_policy, org=org_policy):
                with closing(connect(self.config)) as conn, conn:
                    conn.execute("UPDATE repositories SET owner='solo', analysis_policy=? WHERE id=1", (repo_policy,))
                    conn.execute('DELETE FROM organization_policies')
                    if org_policy:
                        conn.execute("INSERT INTO organization_policies VALUES('solo',?)", (org_policy,))
                body = self.request('/pr/1')[2]
                self.assertIn('분석이 거부되어 있습니다', body)
                self.assertNotIn('action="/settings/analysis"', body)

    def test_consent_form_keeps_enterprise_host(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET repo_id=2 WHERE id=1")
        body = self.request('/pr/1')[2]
        self.assertIn('name="host" value="github.corp.example"', body)
        self.assertIn('name="target" value="demo-org/intranet"', body)

    def test_consent_redirects_back_and_does_not_queue_analysis(self):
        code, headers, _ = self.consent()
        self.assertEqual((code, headers['Location']), (303, '/pr/1'))
        self.assertTrue(self.allowed())
        with closing(connect(self.config)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE type='analyze'").fetchone()[0], 0)
        self.assertIn('action="/jobs/analyze"', self.request('/pr/1')[2])

    def test_consent_requires_confirm(self):
        self.assertEqual(self.consent(confirm=None)[0], 400)
        self.assertFalse(self.allowed())

    def test_next_only_allows_pr_paths(self):
        for bad in ['//evil.test', 'http://evil.test/pr/1', '/pr/1/../x', '/pr/0', '/settings?x', '/pr/1\r\nX: y', '']:
            with self.subTest(next=bad):
                with closing(connect(self.config)) as conn, conn:
                    conn.execute("UPDATE repositories SET analysis_policy='inherit' WHERE owner='demo'")
                code, headers, _ = self.consent(next=bad)
                self.assertEqual((code, headers.get('Location')), (303, '/settings'))
        self.assertEqual(self.consent(next=None)[1]['Location'], '/settings')


class PagingTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def add_prs(self, count):
        with closing(connect(self.config)) as conn, conn:
            for number in range(100, 100 + count):
                conn.execute("INSERT INTO pull_requests(repo_id,number,title,base_sha,head_sha) VALUES(1,?,?,'b','h')",
                             (number, f'추가 PR {number}'))

    def add_repos(self, count):
        with closing(connect(self.config)) as conn, conn:
            for index in range(count):
                conn.execute("INSERT INTO repositories(owner,name) VALUES('zz-other',?)", (f'r{index:03d}',))

    def test_paginate_clamps_and_keeps_query(self):
        items = list(range(45))
        for raw, page, first in [(None, 1, 0), ('2', 2, 20), ('3', 3, 40), ('99', 3, 40), ('abc', 1, 0), ('0', 1, 0), ('-1', 1, 0)]:
            with self.subTest(raw=raw):
                query = {'repo': ['a b'], 'page': [raw]} if raw else {'repo': ['a b']}
                shown, nav = paginate(items, query, '/')
                self.assertEqual(shown[0], first)
                self.assertIn(f'{page} / 3', nav)
        shown, nav = paginate(items, {'repo': ['a b'], 'page': ['2']}, '/')
        self.assertEqual(len(shown), PAGE_SIZE)
        self.assertIn('href="/?repo=a+b&amp;page=1"', nav)
        self.assertIn('href="/?repo=a+b&amp;page=3"', nav)
        self.assertEqual(paginate(list(range(5)), {}, '/')[1], '')

    def test_pr_list_pages(self):
        self.add_prs(40)
        first = self.request('/')[2]
        self.assertEqual(first.count('<h2><a href="/pr/'), PAGE_SIZE)
        self.assertIn('page=2', first)
        last = self.request('/?page=3')[2]
        self.assertEqual(last.count('<h2><a href="/pr/'), 45 - 2 * PAGE_SIZE)
        filtered = self.request('/?importance=unanalyzed&page=2')[2]
        self.assertIn('importance=unanalyzed&amp;page=1', filtered)

    def test_settings_pages_prioritizes_relevant_and_searches(self):
        self.add_repos(60)
        body = self.request('/settings')[2]
        repos = re.findall(r'<article><h2>([^<]+)</h2>', body)
        self.assertEqual(len(repos), PAGE_SIZE)
        # 추적 중인 데모 저장소가 첫 페이지 앞쪽에 온다.
        self.assertEqual(repos[0], 'github.com/demo/checkout')
        self.assertTrue(any('/settings?page=2' in link for link in re.findall(r'href="([^"]+)"', body)))
        found = self.request('/settings?q=r05')[2]
        self.assertEqual(re.findall(r'<article><h2>([^<]+)</h2>', found),
                         [f'github.com/zz-other/r05{i}' for i in range(10)])
        self.assertIn('name="q" value="r05"', found)

    def test_settings_order_tracked_then_policy_then_open_pr(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute('UPDATE repositories SET tracked=0')
            conn.execute("UPDATE pull_requests SET state='closed'")
            for name, tracked, policy in [('a-plain', 0, 'inherit'), ('b-open', 0, 'inherit'),
                                          ('c-policy', 0, 'deny'), ('d-tracked', 1, 'inherit')]:
                conn.execute("INSERT INTO repositories(owner,name,tracked,analysis_policy) VALUES('aaa',?,?,?)", (name, tracked, policy))
            repo = conn.execute("SELECT id FROM repositories WHERE name='b-open'").fetchone()[0]
            conn.execute("INSERT INTO pull_requests(repo_id,number,state) VALUES(?,1,'open')", (repo,))
        names = [n for n in re.findall(r'<article><h2>github.com/aaa/([^<]+)</h2>', self.request('/settings?q=aaa/')[2])]
        self.assertEqual(names, ['d-tracked', 'c-policy', 'b-open', 'a-plain'])

    def test_search_value_is_escaped(self):
        body = self.request('/settings?q=%22%3E%3Cx%3E')[2]
        self.assertIn('value="&quot;&gt;&lt;x&gt;"', body)
        self.assertNotIn('"><x>', body)
        self.assertIn('검색 결과가 없습니다', body)
        self.assertNotIn('검색 결과가 없습니다', self.request('/settings')[2])

    def test_repos_lookup_results_page_with_separate_key(self):
        self.server.repo_lookup.snapshot = lambda with_host=False: ('github.com', [f'org/r{i:02d}' for i in range(30)])
        with closing(connect(self.config)) as conn, conn:
            for index in range(25):
                conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('t',?,1)", (f'n{index:02d}',))
        body = self.request('/repos?page=2&found=2')[2]
        self.assertEqual(body.count('<button>선택하여 연결</button>'), 10)
        self.assertIn('href="/repos?page=2&amp;found=1"', body)
        self.assertIn('href="/repos?found=2&amp;page=1"', body)

    def test_repos_tracked_list_pages(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute('UPDATE repositories SET tracked=0')
            for index in range(25):
                conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('t',?,1)", (f'n{index:02d}',))
        body = self.request('/repos')[2]
        self.assertEqual(body.count('<button>연결 해제</button>'), PAGE_SIZE)
        self.assertEqual(self.request('/repos?page=2')[2].count('<button>연결 해제</button>'), 5)


class SkipReasonTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def test_zero_targets_reports_skip_reasons(self):
        analyzer = Mock()
        analyzer.return_value.run.return_value = []
        analyzer.return_value.skipped = {'not_allowed': 2, 'draft': 1}
        conn, state = connect(self.config), connect(self.config)
        self.addCleanup(conn.close)
        self.addCleanup(state.close)
        with closing(connect(self.config)) as c:
            row, _ = jobs.enqueue(c, 'analyze')
        runner = Runner(self.config, conn, state, self.server.repo_lookup, analyzer=analyzer)
        runner.scheduler = SimpleNamespace(tick=lambda collector: False, last_ok=False)
        runner.step()
        data = json.loads(self.request('/jobs/' + str(row['id']))[2])
        self.assertIn('분석 허용 안 됨 2', data['result'])
        self.assertIn('draft 1', data['result'])
        self.assertIn('설정', data['result'])

    def run_batch(self, results, skipped):
        analyzer = Mock()
        analyzer.return_value.run.return_value = results
        analyzer.return_value.skipped = skipped
        conn, state = connect(self.config), connect(self.config)
        self.addCleanup(conn.close)
        self.addCleanup(state.close)
        with closing(connect(self.config)) as c:
            row, _ = jobs.enqueue(c, 'analyze')
        runner = Runner(self.config, conn, state, self.server.repo_lookup, analyzer=analyzer)
        runner.scheduler = SimpleNamespace(tick=lambda collector: False, last_ok=False)
        runner.step()
        return json.loads(self.request('/jobs/' + str(row['id']))[2])['result']

    def test_skip_note_variants(self):
        self.assertEqual(self.run_batch([], {}), '분석 대상 0개 허용된 미분석 스냅샷 없음')
        self.assertEqual(self.run_batch([], {'stale': 1}), '분석 대상 0개 건너뜀: 최신 수집 전 1')
        self.assertEqual(self.run_batch([{'status': 'completed'}], {'not_allowed': 1}),
                         '분석 대상 1개 (완료 1) 건너뜀: 분석 허용 안 됨 1 (PR 상세 또는 설정에서 저장소 분석을 허용하세요)')


if __name__ == '__main__':
    unittest.main()
