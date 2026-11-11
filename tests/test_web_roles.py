"""핵심·연관·경로 힌트 구분: 홈 카드, 상세 목록, 평가가 같은 선정을 쓰는지 검증한다."""
from contextlib import closing
import json
import re
import unittest

import test_web
from skim.db import connect
from skim.web import file_card, file_rows, first_look, foldable, load_prs, priority_rank, top_file_rows
from skim.web_settings import RepoLookup


def unit(change_id, path, **signals):
    base = dict(change_id=change_id, file_path=path, candidates=[], location_hints=[], kind_signals=[],
                content_hits=[], hard_floors=[], mechanical_hints=[], test_gap=False)
    return dict(base, **signals)


def verdict(importance, behavior='changed', reason='근거'):
    return dict(importance=importance, behavior=behavior, reason=reason, risk_flags=[])


def pull(units, items, **result):
    return dict(id=7, units=units, items=items, allowed=True, stale=False, result=result)


def group(core, reason='주문 금액 계산을 바꾼다', **related):
    return dict(core_change_id=core, reason=reason,
                related=[dict(change_id=change_id, relation=relation) for change_id, relation in related.items()])


class RoleSelectionTests(unittest.TestCase):
    def grouped(self):
        units = [unit('h1', 'orders/total.py'), unit('h2', 'api/orders.py'), unit('h3', 'tests/test_total.py'),
                 unit('h4', 'auth/session.py', location_hints=['auth'], candidates=['auth']),
                 unit('h5', 'billing/refund.py'), unit('h6', 'orders/names.py')]
        items = {'h1': verdict('mid'), 'h2': verdict('low'), 'h3': verdict('low'), 'h4': verdict('low', 'unchanged'),
                 'h5': verdict('high', reason='환불 한도를 없앤다'), 'h6': verdict('low', 'unchanged')}
        return pull(units, items, focus=[dict(change_id='h1', reason='주문 금액 계산을 바꾼다')],
                    groups=[group('h1', h2='caller', h3='test', h99='other')])

    def test_location_hint_with_model_low_stays_out_of_first_look(self):
        pr = self.grouped()
        rows = file_rows(pr)
        hinted = next(row for row in rows if row['path'] == 'auth/session.py')
        self.assertTrue(hinted['hint'])
        self.assertNotIn('auth/session.py', [row['path'] for row in top_file_rows(rows)])
        card = file_card(pr)
        self.assertNotIn('auth/session.py', re.findall(r'<span class="file-path">([^<]+)</span>', card))
        self.assertIn('<a href="/pr/7?view=hints#file-changes">경로상 확인 1</a> · 기계적 변경 1', card)
        # 위험색은 모델이 높음으로 본 파일에만 붙는다.
        self.assertEqual(re.findall(r'<span class="badge high">[^<]*</span><span class="file-path">([^<]+)', card), ['billing/refund.py'])

    def test_core_group_lists_reason_and_related_summary(self):
        pr = self.grouped()
        core = next(row for row in file_rows(pr) if row['path'] == 'orders/total.py')
        self.assertEqual(core['role'], 'core')
        # 입력에 없는 변경 ID로 경로를 만들지 않는다.
        self.assertEqual(core['related'], [dict(path='api/orders.py', relation='caller'),
                                           dict(path='tests/test_total.py', relation='test')])
        roles = {row['path']: row['role'] for row in file_rows(pr)}
        self.assertEqual((roles['api/orders.py'], roles['tests/test_total.py']), ('supporting', 'supporting'))
        row = next(part for part in file_card(pr).split('<a class="file-row"') if 'orders/total.py' in part)
        self.assertIn('<span class="badge mid">보통</span>', row)
        self.assertIn('<span class="badge role">핵심</span><span>주문 금액 계산을 바꾼다</span>', row)
        self.assertIn('<span class="badge role">연관 1 · 테스트 1</span>', row)

    def test_high_outside_groups_is_still_listed(self):
        rows = file_rows(self.grouped())
        self.assertEqual([row['path'] for row in top_file_rows(rows)], ['orders/total.py', 'billing/refund.py'])
        self.assertEqual(next(row for row in rows if row['path'] == 'billing/refund.py')['role'], '')

    def test_review_order_and_mechanical_fold(self):
        pr = self.grouped()
        pr['units'].append(unit('h7', '.github/workflows/ci.yml', kind_signals=['ci_workflow']))
        pr['items']['h7'] = verdict('low', 'unchanged')
        rows = file_rows(pr)
        self.assertEqual([row['path'] for row in rows],
                         ['billing/refund.py', 'orders/total.py', '.github/workflows/ci.yml', 'api/orders.py',
                          'tests/test_total.py', 'auth/session.py', 'orders/names.py'])
        self.assertEqual([row['path'] for row in rows if foldable(row)], ['orders/names.py'])
        look = first_look(rows)
        self.assertEqual([row['path'] for row in look['safety']], ['.github/workflows/ci.yml'])
        self.assertIn('안전 확인', file_card(pr))
        self.assertIn('CI 워크플로', file_card(pr))

    def test_mechanical_label_never_folds_high_floor_uncertain_or_partial(self):
        units = [unit('h1', 'a.py'), unit('h2', 'db/migrate.sql', hard_floors=['db_schema_migration']),
                 unit('h3', 'c.py'), unit('h4', 'd.py'), unit('h5', 'd.py'), unit('h6', 'e.py')]
        items = {'h1': verdict('high', 'unchanged'), 'h2': verdict('low', 'unchanged'), 'h3': verdict('low', 'unchanged'),
                 'h4': verdict('low', 'unchanged'), 'h6': verdict('low', 'unchanged')}
        rows = file_rows(pull(units, items, uncertainties=[dict(change_id='h3', reason='호출부 미확인')]))
        self.assertEqual([row['path'] for row in rows if foldable(row)], ['e.py'])

    def test_test_gap_alone_and_unanalyzed_signals(self):
        units = [unit('h1', 'app/job.py', test_gap=True, candidates=['logic_without_tests']),
                 unit('h2', '.env', kind_signals=['secrets'], hard_floors=['secrets'], excluded=True),
                 unit('h3', 'payments/client.py', location_hints=['payments'])]
        pr = pull(units, {'h1': verdict('low')})
        look = first_look(file_rows(pr))
        self.assertEqual(look['top'], [])
        self.assertEqual([row['path'] for row in look['safety']], ['.env', 'payments/client.py'])
        card = file_card(pr)
        self.assertIn('높음, 보통 파일이 없습니다.', card)
        self.assertEqual(card.count('<span class="badge unanalyzed">확인</span>'), 2)
        self.assertNotIn('class="badge high"', card)

    def test_safety_line_is_capped(self):
        units = [unit(f'h{i}', f'.github/workflows/w{i}.yml', kind_signals=['ci_workflow']) for i in range(1, 6)]
        card = file_card(pull(units, {u['change_id']: verdict('low') for u in units}))
        self.assertEqual(card.count('class="safety-item"'), 3)
        self.assertIn('<span>외 2개</span>', card)

    def test_empty_groups_fall_back_to_importance_order(self):
        units = [unit('h1', 'a.py'), unit('h2', 'b.py'), unit('h3', 'auth/c.py', location_hints=['auth'])]
        pr = pull(units, {'h1': verdict('mid'), 'h2': verdict('high'), 'h3': verdict('low')}, focus=[], groups=[])
        self.assertEqual([row['path'] for row in top_file_rows(file_rows(pr))], ['b.py', 'a.py'])
        self.assertNotIn('높음, 보통 파일이 없습니다.', file_card(pr))

    def test_legacy_candidates_are_not_read_as_location_hints(self):
        units = [dict(change_id='h1', file_path='auth/a.py', candidates=['auth']),
                 dict(change_id='h2', file_path='b.py', candidates=[])]
        pr = pull(units, {'h1': verdict('low'), 'h2': verdict('low')})
        rows = file_rows(pr)
        self.assertTrue(all(row['legacy'] and not row['hint'] for row in rows))
        self.assertEqual([row['path'] for row in top_file_rows(rows)], ['auth/a.py'])
        card = file_card(pr)
        self.assertIn('규칙 후보: 인증', card)
        self.assertNotIn('경로상 확인', card)

    def test_stale_result_is_not_a_current_high_on_home(self):
        pr = self.grouped()
        for hidden in (dict(pr, stale=True), dict(pr, allowed=False), dict(pr, status='failed')):
            card = file_card(hidden)
            self.assertNotIn('class="badge high"', card)
            self.assertIn('최신 변경의 분석 결과가 없습니다.', card)
        self.assertIn('class="badge high"', file_card(dict(pr, status='failed', previous_result=True)))


class RoleWebTests(unittest.TestCase):
    setup_http = test_web.WebTests.setUp
    request = test_web.WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def card(self, title, body):
        return next(c.split('</article>')[0] for c in body.split('<article class="issue-row">')[1:] if title in c)

    def result(self, pr_id):
        with closing(connect(self.config)) as conn:
            row = conn.execute('SELECT a.id,a.output_json FROM analyses a JOIN snapshots s ON s.id=a.snapshot_id '
                               'WHERE s.pr_id=? ORDER BY a.id DESC', (pr_id,)).fetchone()
            return row['id'], json.loads(row['output_json'])

    def test_demo_seed_does_not_force_auth_path_high(self):
        _, result = self.result(1)
        paths = {u['change_id']: u['file_path'] for u in result['units']}
        levels = {paths[item['change_id']]: (item['importance'], item['behavior']) for item in result['items']}
        self.assertEqual(levels['auth/session.py'], ('low', 'unchanged'))
        self.assertEqual(levels['service.py'], ('high', 'changed'))
        self.assertEqual([paths[g['core_change_id']] for g in result['groups']], ['service.py'])
        self.assertEqual([f['change_id'] for f in result['focus']], [g['core_change_id'] for g in result['groups']])
        _, stale = self.result(5)
        self.assertEqual([item['importance'] for item in stale['items']], ['low'])

    def test_home_card_separates_core_from_path_hint(self):
        body = self.request('/')[2]
        card = self.card('결제 인증 경계', body)
        self.assertEqual(re.findall(r'<span class="badge high">높음</span><span class="file-path">([^<]+)', card), ['service.py'])
        self.assertIn('<span class="badge role">연관 1</span>', card)
        self.assertIn('href="/pr/1?view=hints#file-changes">경로상 확인 1</a>', card)
        self.assertRegex(card, r'class="safety-item"[^>]*><span class="file-path">\.env</span>')
        self.assertIn('중요도 높음', card)
        self.assertIn('결제 인증 경계', self.request('/?tab=high')[2])
        # 이전 SHA 결과는 홈에서도 현재 중요도로 보이지 않는다.
        stale = self.card('이전 SHA 분석 결과', body)
        self.assertNotIn('class="badge low"', stale)
        self.assertIn('최신 변경의 분석 결과가 없습니다.', stale)

    def test_hints_view_lists_location_hint_files(self):
        body = self.request('/pr/1?view=hints')[2]
        nav = body.split('class="file-nav-links"', 1)[1].split('</nav>', 1)[0]
        self.assertIn('title="auth/session.py"', nav)
        self.assertNotIn('title="service.py"', nav)
        self.assertIn('<span class="badge hint">경로 확인</span>', nav)
        self.assertNotIn('badge high', nav)
        self.assertIn('경로상 확인 파일 1개만 표시합니다.', body)
        self.assertRegex(nav, r'href="\?view=hints&amp;file=auth%2Fsession\.py#')
        self.assertIn('?order=original&amp;view=hints#file-changes', body)

    def test_detail_badges_pair_role_with_importance(self):
        body = self.request('/pr/1')[2]
        nav = body.split('class="file-nav-links"', 1)[1].split('</nav>', 1)[0]
        order = re.findall(r'<span class="file-label" title="([^"]+)"', nav)
        self.assertEqual(order, ['service.py', '.env', 'auth/session.py', 'README.md'])
        entries = dict(zip(order, nav.split('<a href=')[1:]))
        self.assertIn('<span class="badge role">핵심</span><span class="badge high">높음</span>', entries['service.py'])
        self.assertIn('<span class="badge role">연관</span><span class="badge hint">경로 확인</span><span class="badge low">낮음</span>',
                      entries['auth/session.py'])
        self.assertIn('<span class="badge unanalyzed">확인</span>', entries['.env'])
        self.assertEqual(body.count('class="badge high"'), body.count('<span class="badge high">높음</span>')
                         + body.count('<span class="badge high">중요도 높음</span>'))
        focus = body.split('class="detail-focus"', 1)[1].split('</section>', 1)[0]
        self.assertIn('결제 승인 흐름의 세션 확인 순서를 바꾸는 핵심 변경입니다.', focus)
        self.assertIn('<span class="badge role">연관 1</span>', focus)
        self.assertIn('<p>경로상 확인: 인증</p>', body)
        self.assertNotIn('규칙 후보:', body)

    def test_mechanical_files_fold_only_in_priority_order(self):
        analysis, result = self.result(3)
        result['items'][0].update(importance='low', behavior='unchanged')
        with closing(connect(self.config)) as conn, conn:
            conn.execute('UPDATE analyses SET output_json=? WHERE id=?', (json.dumps(result), analysis))
        body = self.request('/pr/3')[2]
        self.assertIn('<details class="file-nav-fold" open><summary>기계적 변경 1개</summary>', body)
        self.assertIn('<span class="badge role">기계적</span><span class="badge low">낮음</span>', body)
        self.assertNotIn('file-nav-fold', self.request('/pr/3?order=original')[2])
        self.assertIn('기계적 변경 1', self.card('사용 설명 보완', self.request('/')[2]))

    def test_supporting_high_still_counts_as_high_pr(self):
        analysis, result = self.result(1)
        ids = {u['file_path']: u['change_id'] for u in result['units']}
        for item in result['items']:
            item['importance'] = 'high' if item['change_id'] == ids['auth/session.py'] else 'low'
        with closing(connect(self.config)) as conn, conn:
            conn.execute('UPDATE analyses SET output_json=? WHERE id=?', (json.dumps(result), analysis))
        with closing(connect(self.config)) as conn:
            pr = next(p for p in load_prs(conn, self.config) if p['id'] == 1)
        row = next(r for r in file_rows(pr) if r['path'] == 'auth/session.py')
        self.assertEqual((row['role'], row['rank']), ('supporting', 3))
        self.assertEqual(priority_rank(pr), 3)
        body = self.request('/?tab=high')[2]
        card = self.card('결제 인증 경계', body)
        self.assertEqual(re.findall(r'<span class="file-path">([^<]+)</span><span class="file-note">', card),
                         ['service.py', 'auth/session.py'])
        self.assertIn('<span class="badge high">높음</span><span class="file-path">auth/session.py', card)


if __name__ == '__main__':
    unittest.main()
