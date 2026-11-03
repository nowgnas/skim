"""메인 화면: 관리 저장소 범위, 검토 현황 수치, PR별 먼저 볼 파일을 검증한다."""
from contextlib import closing
import json
import re
import unittest

from test_web import WebTests
from skim.db import connect
from skim.web import file_anchor, file_rows, file_card
from skim.web_settings import RepoLookup


class HomeTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def card(self, title, body=None):
        body = body or self.request('/')[2]
        return next(c.split('</article>')[0] for c in body.split('<article class="issue-row">')[1:] if title in c)

    def set_items(self, pr_id, verdicts):
        """PR의 최신 분석에서 변경 단위별 중요도를 바꾼다."""
        with closing(connect(self.config)) as conn, conn:
            row = conn.execute('SELECT a.id,a.output_json FROM analyses a JOIN snapshots s ON s.id=a.snapshot_id '
                               'WHERE s.pr_id=? ORDER BY a.id DESC', (pr_id,)).fetchone()
            result = json.loads(row['output_json'])
            result['items'] = [dict(change_id=cid, importance=imp, reason=f'{cid} 근거', risk_flags=[]) for cid, imp in verdicts.items()]
            conn.execute('UPDATE analyses SET output_json=? WHERE id=?', (json.dumps(result), row['id']))
            return result['units']

    def test_overview_focus_ranking(self):
        units = [dict(change_id=f'h{i}', file_path=f'{i}.py', candidates=['config'] if i == 1 else [])
                 for i in range(1, 5)]
        pr = dict(id=1, units=units, items={unit['change_id']: dict(importance='high', reason='변경', risk_flags=[])
                                         for unit in units}, allowed=True, stale=False,
                  result={'focus': [dict(change_id='h4', reason='PR 목적 구현'),
                                    dict(change_id='h3', reason='핵심 동작')]})
        self.assertEqual([row['path'] for row in file_rows(pr)], ['4.py', '3.py', '1.py', '2.py'])
        self.assertIn('목적 관련: PR 목적 구현', file_card(pr))
        previous = dict(pr, result={})
        self.assertEqual(file_rows(previous)[0]['path'], '1.py')
        hidden_candidate = dict(pr, result={'focus': [dict(change_id=f'h{i}', reason='핵심') for i in (4, 3, 2)]})
        self.assertIn('그 외 규칙 기반 후보 1개 확인', file_card(hidden_candidate))

    def test_only_prs_of_managed_repositories(self):
        with closing(connect(self.config)) as conn, conn:
            repo = conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('other','service',0)").lastrowid
            pr = conn.execute("INSERT INTO pull_requests(repo_id,number,title) VALUES(?,7,'남의 저장소 PR')", (repo,)).lastrowid
            conn.execute("INSERT INTO pr_sources VALUES(?,'review_request')", (pr,))
        self.assertNotIn('남의 저장소 PR', self.request('/')[2])
        self.assertEqual(self.request(f'/pr/{pr}')[0], 200)

    def test_tabs_show_counts_and_filter(self):
        body = self.request('/')[2]
        tabs = dict(re.findall(r'<span>([^<]+)</span><strong>(\d+)</strong>', body.split('class="pr-tabs"', 1)[1].split('</nav>', 1)[0]))
        self.assertEqual(int(tabs['전체']), body.count('<article class="issue-row">'))
        for key, name in [('review', '리뷰 요청'), ('high', '높음 파일 있음'), ('pending', '분석 전')]:
            with self.subTest(tab=key):
                filtered = self.request('/?tab=' + key)[2]
                self.assertEqual(filtered.count('<article class="issue-row">'), int(tabs[name]))
                self.assertIn('aria-current="page"><span>' + name, filtered)
        self.assertEqual(self.request('/?tab=bogus')[0], 200)
        self.assertNotIn('팀', self.request('/')[2].split('<main', 1)[1])

    def test_top_three_files_and_low_only_counted(self):
        base = self.set_items(2, {})[0]
        paths = [f'f{i}.py' for i in range(5)]
        new_units = [dict(base, change_id=f'u{i}', file_path=path) for i, path in enumerate(paths)]
        with closing(connect(self.config)) as conn, conn:
            row = conn.execute('SELECT a.id,a.output_json FROM analyses a JOIN snapshots s ON s.id=a.snapshot_id '
                               'WHERE s.pr_id=2 ORDER BY a.id DESC').fetchone()
            result = json.loads(row['output_json'])
            for unit in new_units:
                unit['candidates'] = []
            result['units'] = new_units
            result['items'] = [dict(change_id='u0', importance='low', reason='낮음 근거', risk_flags=[]),
                               dict(change_id='u1', importance='high', reason='인증 흐름 변경', risk_flags=['auth']),
                               dict(change_id='u2', importance='mid', reason='보통 근거', risk_flags=[]),
                               dict(change_id='u3', importance='high', reason='결제 경계', risk_flags=[]),
                               dict(change_id='u4', importance='mid', reason='보통 둘', risk_flags=[])]
            conn.execute('UPDATE analyses SET output_json=? WHERE id=?', (json.dumps(result), row['id']))
        card = self.card('일반 API 응답 정리')
        shown = re.findall(r'<span class="file-path">([^<]+)</span>', card)
        self.assertEqual(shown, ['f1.py', 'f3.py', 'f2.py'])
        self.assertNotIn('f0.py', shown)
        self.assertIn('인증 흐름 변경', card)
        self.assertIn('모델 판단: 인증', card)
        self.assertIn('파일 5개 (외 2개)', card)
        self.assertIn('<span>낮음 1</span>', card)
        self.assertIn(f'href="/pr/2?file=f1.py#{file_anchor("f1.py")}"', card)

    def test_file_link_lands_on_open_file_in_detail(self):
        card = self.card('결제 인증 경계')
        href = re.search(r'<a class="file-row" href="([^"]+)"', card).group(1).replace('&amp;', '&')
        path, anchor = href.split('#')
        body = self.request(path)[2]
        self.assertIn(f'id="{anchor}"', body)
        panel = re.search(r'<section class="detail-file" id="' + re.escape(anchor) + r'"[^>]*>', body)
        self.assertIsNotNone(panel)
        self.assertNotIn(' hidden', panel.group())

    def test_unanalyzed_card_shows_next_step(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE github_hosts SET analysis_scope='none'")
            conn.execute("UPDATE repositories SET analysis_policy='inherit'")
        card = self.card('결제 인증 경계')
        self.assertIn('분석 허용 안 됨', card)
        self.assertIn('#analysis-scope', card)

    def test_priority_first_then_review_requested(self):
        body = self.request('/')[2]
        cards = body.split('<article class="issue-row">')[1:]
        levels = {'높음': 3, '보통': 2, '낮음': 1}
        badges = [c.split('<p class="home-pr-badges">', 1)[1].split('</p>', 1)[0] for c in cards]
        priorities = [(levels.get(match.group(1), 0) if (match := re.search(r'중요도 (높음|보통|낮음)', c)) else 0,
                       '리뷰 요청 받음' in c) for c in badges]
        self.assertEqual(priorities, sorted(priorities, reverse=True))


if __name__ == '__main__':
    unittest.main()
