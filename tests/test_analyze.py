"""가짜 LLM으로만 분석·제외·예산·엄격한 결과 계약을 검증한다."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.analyze import Analyzer
from skim.analyze.codex import CodexAdapter, IsolationError
from skim.analyze.protocol import FLAGS, PROMPT_VERSION, chunk_units, validate_chunk, prompt, wire
from skim.analyze.units import parse_diff, sanitize
from skim.config import Config
from skim.db import connect

DIFF = '''diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
@@ -9 +9 @@
-old
+auth = True
'''


class Fake:
    provider, model, cli_version = 'codex', 'fake-model', 'fake-1'

    def __init__(self):
        self.calls = []
        self.invalid = False

    def verify(self):
        pass

    def generate(self, request, schema):
        self.calls.append(request)
        value = json.loads(request.split('\n', 1)[1])
        if self.invalid:
            return '{"items":[],"uncertainties":[]}'
        if value['stage'] == 'synthesis':
            return '{"intent":"인증 동작을 변경한다.","focus":[],"groups":[]}'
        if value['stage'] == 'overview':
            return '{"intent":"인증 동작을 변경한다.","focus":[]}'
        return json.dumps(dict(items=[dict(change_id=u['change_id'], importance='high' if u['change_id'] == 'h2' else 'low', reason='변경된 인증 동작 검토', behavior='changed', risk_flags=[]) for u in value['data']['units']], uncertainties=[]))


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, SKIM_HOME=self.temp.name)
        env.start()
        self.addCleanup(env.stop)
        self.config = Config.load()
        self.conn = connect(self.config)
        self.addCleanup(self.conn.close)
        with self.conn:
            self.conn.execute("INSERT INTO repositories(owner,name,analysis_policy) VALUES('org','repo','allow')")
            self.conn.execute("UPDATE github_hosts SET analysis_scope='selected'")
            self.conn.execute("INSERT INTO pull_requests(repo_id,number,title,body,base_sha,head_sha) VALUES(1,1,'변경','설명','base','head')")
        self.snapshot(DIFF)
        self.fake = Fake()
        self.analyzer = Analyzer(self.conn, self.config, self.fake, today=lambda: '2026-10-31')

    def test_structured_priority_ignores_candidate_union_and_test_gap(self):
        from skim.analyze.units import allocation_key
        from skim.analyze.context import overview_input
        base = parse_diff('', [{'filename': 'app.py'}])[0][0]
        units = [dict(base, change_id=f'h{i}', file_path=f'src/{i}.py', candidates=['auth']) for i in range(1, 8)]
        units[0]['mechanical_hints'] = ['pure_rename']
        units[1]['location_hints'] = ['auth']
        units[2]['test_gap'] = True
        units[3]['content_hits'] = ['auth']
        units[4]['kind_signals'] = ['ci_workflow']
        units[5]['hard_floors'] = ['secrets']
        units[6]['file_path'] = 'tests/test_app.py'
        expected = ['h6', 'h4', 'h5', 'h2', 'h3', 'h7', 'h1']
        self.assertEqual([u['change_id'] for u in sorted(units, key=allocation_key)], expected)
        chunk = chunk_units(units, {})[0]
        self.assertEqual([u['change_id'] for u in chunk['units']], expected)
        self.assertNotIn('candidates', chunk['units'][0])
        self.assertIn('hard_floors', chunk['units'][0])
        focused = chunk_units(units, {}, overview={'intent': '확인', 'focus': [{'change_id': 'h5', 'reason': '핵심'}]})
        self.assertEqual([u['change_id'] for u in focused[0]['units']][:3], ['h6', 'h5', 'h4'])
        samples = overview_input(units, {})['samples']
        self.assertEqual([u['change_id'] for u in samples], expected)

    def test_overview_round_robin_keeps_other_files(self):
        from skim.analyze.context import overview_input
        base = parse_diff('', [{'filename': 'app.py'}])[0][0]
        units = [dict(base, change_id=f'h{i}', content_hits=['auth']) for i in range(1, 4)]
        units.append(dict(base, change_id='h4', file_path='other.py'))
        self.assertEqual([u['change_id'] for u in overview_input(units, {})['samples']], ['h1', 'h4', 'h2', 'h3'])

    def test_raw_budget_prioritizes_floor_over_test_gap(self):
        base = parse_diff('', [{'filename': 'app.py'}])[0][0]
        units = [dict(base, change_id='h1', raw_bytes=700000, test_gap=True),
                 dict(base, change_id='h2', raw_bytes=700000, hard_floors=['db_schema_migration'])]
        with patch('skim.analyze.parse_diff', return_value=(units, [])):
            result = self.analyzer.run()[0]
        self.assertEqual([item['change_id'] for item in result['items']], ['h2'])
        self.assertEqual(result['units'][0]['omission_reason'], '정제 전 diff 1 MiB 초과')

    def test_groups_invalid_only_discard_groups(self):
        from skim.analyze.protocol import validate_synthesis
        focus = [{'change_id': 'h1', 'reason': '핵심'}]
        valid = {'core_change_id': 'h1', 'reason': '핵심', 'related': [{'change_id': 'h2', 'relation': 'caller'}]}
        good = dict(intent='동작 변경', focus=focus, groups=[valid])
        self.assertEqual(validate_synthesis(json.dumps(good), {'h1', 'h2'}), good)
        for groups in ([dict(valid, core_change_id='h2')], [valid, valid],
                       [dict(valid, related=[{'change_id': 'h1', 'relation': 'test'}])],
                       [dict(valid, related=[{'change_id': 'h9', 'relation': 'test'}])],
                       [dict(valid, related=[{'change_id': 'h2', 'relation': 'unknown'}])],
                       [dict(valid, reason='x' * 301)], [valid] * 21, None):
            with self.subTest(groups=groups):
                result = validate_synthesis(json.dumps(dict(good, groups=groups)), {'h1', 'h2'})
                self.assertEqual(result, dict(good, groups=[]))
        other = dict(core_change_id='h3', reason='다른 핵심', related=[{'change_id': 'h2', 'relation': 'test'}])
        result = validate_synthesis(json.dumps(dict(good, focus=focus + [{'change_id': 'h3', 'reason': '다른 핵심'}],
                                                   groups=[valid, other])), {'h1', 'h2', 'h3'})
        self.assertEqual(result['groups'], [])

    def test_synthesis_budget_preserves_high_floor_focus_and_only_included_ids(self):
        from skim.analyze.context import synthesis_input
        base = parse_diff('', [{'filename': 'app.py'}])[0][0]
        units = [dict(base, change_id=f'h{i}', file_path=f'{i}.py') for i in range(1, 41)]
        units[-1]['hard_floors'] = ['secrets']
        items = [dict(change_id=u['change_id'], importance='high' if u['change_id'] == 'h39' else 'low',
                      behavior='unchanged', reason='근거' * 200, risk_flags=[]) for u in units]
        overview = dict(intent='목적', focus=[dict(change_id='h38', reason='핵심')])
        data = synthesis_input(units, {}, items, {}, overview, [dict(change_id='h37', reason='미확인')], max_bytes=10000)
        ids = {i['change_id'] for i in data['items']}
        self.assertTrue({'h38', 'h39', 'h40'} <= ids)
        self.assertGreater(data['coverage']['synthesis_omitted'], 0)
        self.assertTrue(all(i['change_id'] in ids for i in data['uncertainties']))
        self.assertLessEqual(len(prompt('synthesis', data).encode()), 10000)
        self.assertEqual(data['items'][0]['behavior'], 'unchanged')

    def test_redacted_group_failure_keeps_synthesis_and_chunk_results(self):
        original = self.fake.generate

        def generate(request, schema):
            value = json.loads(original(request, schema))
            if '"stage":"synthesis"' in request:
                value['focus'] = [dict(change_id='h1', reason='핵심')]
                value['groups'] = [dict(core_change_id='h1', reason='ghp_a ' * 45, related=[])]
            return json.dumps(value)

        self.fake.generate = generate
        result = self.analyzer.run()[0]
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['groups'], [])
        self.assertTrue(result['intent'])
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(len(self.fake.calls), 2)

    def test_behavior_is_required_and_closed_enum(self):
        item = dict(change_id='h1', importance='low', reason='변경', risk_flags=[])
        for candidate in (item, dict(item, behavior='mechanical')):
            with self.assertRaises(ValueError):
                validate_chunk(json.dumps(dict(items=[candidate], uncertainties=[])), {'h1'})

    def test_prompt_defines_all_risk_flags(self):
        from skim.analyze.protocol import FLAGS
        request = prompt('chunk', {})
        for flag in FLAGS:
            self.assertIn(flag + ' = ', request)
        self.assertIn('추가·삭제된 변경 줄에 직접 근거', request)
        self.assertIn('설정을 읽거나 검증하는 코드 변경은 해당 안 됨', request)
        self.assertIn('DB 테이블·컬럼·인덱스', request)

    def test_first_analysis_records_verified_version_including_failure(self):
        self.fake.cli_version = 'unknown'

        def verify():
            self.fake.cli_version = 'verified-1'

        self.fake.verify = verify
        self.analyzer.run()
        self.assertEqual(self.conn.execute('SELECT cli_version FROM analyses').fetchone()[0], 'verified-1')
        self.fake.cli_version = 'unknown'

        def fail():
            self.fake.cli_version = 'verified-2'
            raise IsolationError('probe 필요')

        self.fake.verify = fail
        self.analyzer.run()
        self.assertEqual(self.conn.execute('SELECT cli_version FROM analyses ORDER BY id DESC').fetchone()[0], 'verified-2')

    def snapshot(self, diff, files=()):
        path = self.config.data_dir / 'cache' / 'diff'
        path.write_text(diff)
        with self.conn:
            self.conn.execute('INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path,coverage) VALUES(1,?,?,?,?,?,?)', ('base','head','raw','raw',str(path),json.dumps(dict(files=files))))

    def test_pipeline_cache_file_max_and_independent_candidates(self):
        value = self.analyzer.run()[0]
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(value['files'][0]['importance'], 'high')
        self.assertIn('logic_without_tests', value['files'][0]['candidates'])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.analyzer.run()[0], value)
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(self.conn.execute('SELECT calls FROM analysis_budget').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analysis_items').fetchone()[0], 3)

    def test_manual_cache_recovery_displays_success_after_failure(self):
        from skim.web import load_prs
        self.analyzer.run()
        original_items = self.conn.execute('SELECT count(*) FROM analysis_items').fetchone()[0]
        def fail():
            raise IsolationError('probe 만료')
        self.fake.verify = fail
        self.assertEqual(self.analyzer.run()[0]['status'], 'blocked')
        self.fake.verify = lambda: None
        self.assertEqual(self.analyzer.run(snapshot_id=1)[0]['status'], 'completed')
        displayed = load_prs(self.conn, self.config)[0]['analysis']
        self.assertEqual(displayed['status'], 'completed')
        self.assertTrue(displayed['intent'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM analysis_items WHERE analysis_id=?', (displayed['id'],)).fetchone()[0], original_items)
        count = self.conn.execute('SELECT count(*) FROM analyses').fetchone()[0]
        self.analyzer.run(snapshot_id=1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM analyses').fetchone()[0], count)

    def test_missing_intent_cache_is_retried(self):
        self.analyzer.run()
        with self.conn:
            row = self.conn.execute('SELECT id,output_json FROM analyses ORDER BY id DESC').fetchone()
            output = json.loads(row['output_json'])
            output['intent'] = None
            output['status'] = 'partial'
            self.conn.execute('UPDATE analyses SET status=?,intent=NULL,output_json=? WHERE id=?',
                              ('partial', json.dumps(output), row['id']))
        self.assertIsNone(self.analyzer.run()[0]['intent'])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertTrue(self.analyzer.run(snapshot_id=1)[0]['intent'])
        self.assertEqual(len(self.fake.calls), 4)

    def test_opt_in_draft_and_host_scope(self):
        # 선택 범위에서 빠진 저장소, 범위 '안 함'은 모델을 부르지 않는다. '전체'는 선택과 무관하게 허용한다.
        for scope, policy in [('selected', 'inherit'), ('none', 'allow')]:
            with self.conn:
                self.conn.execute('UPDATE github_hosts SET analysis_scope=?', (scope,))
                self.conn.execute('UPDATE repositories SET analysis_policy=?', (policy,))
            self.assertEqual(self.analyzer.run(), [])
        with self.conn:
            self.conn.execute("UPDATE github_hosts SET analysis_scope='all'")
            self.conn.execute('UPDATE pull_requests SET draft=1')
        self.assertEqual(self.analyzer.run(), [])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.analyzer.run(include_drafts=True)[0]['status'], 'completed')

    def test_strict_fail_retry_and_budget(self):
        self.fake.invalid = True
        value = self.analyzer.run()[0]
        self.assertEqual(value['status'], 'failed')
        self.assertEqual(value['items'], [])
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(value['coverage']['analyzed'], 0)
        with self.conn:
            self.conn.execute('UPDATE analysis_budget SET calls=19')
        self.assertEqual(self.analyzer.run()[0]['status'], 'deferred')
        self.assertEqual(self.conn.execute('SELECT calls FROM analysis_budget').fetchone()[0], 19)
        self.analyzer.today = lambda: '2026-11-01'
        self.fake.invalid = False
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')

    def test_manual_analysis_does_not_consume_automatic_budget(self):
        with self.conn:
            self.conn.execute("INSERT INTO analysis_budget(day,calls) VALUES('2026-10-31',20)")
        self.assertEqual(self.analyzer.run()[0]['status'], 'deferred')
        self.assertEqual(self.analyzer.run(snapshot_id=1, budgeted=False)[0]['status'], 'completed')
        self.assertEqual(self.conn.execute('SELECT calls FROM analysis_budget').fetchone()[0], 20)

    def test_overview_input_is_bounded_and_skips_excluded_units(self):
        from skim.analyze.context import overview_input
        units = [dict(parse_diff('', [{'filename': 'app.py'}])[0][0], change_id=f'h{i}', file_path=f'파일{i}.py', diff='+변경내용' * 100,
                      excluded=False, omission_reason=None) for i in range(1, 16)]
        units.extend([dict(change_id='h100', file_path='.env', diff='do-not-send', excluded=True),
                      dict(change_id='h101', file_path='omitted.py', diff='do-not-send-either',
                           excluded=False, omission_reason='한도')])
        data = overview_input(units, {'title': '제목', 'body': '설명'}, max_bytes=8192)
        self.assertLessEqual(len(prompt('overview', data).encode('utf-8')), 8192)
        self.assertEqual(data['coverage']['excluded_units'], 1)
        self.assertEqual(data['coverage']['omitted_units'], 1)
        self.assertNotIn('do-not-send', json.dumps(data))
        self.assertNotIn('.env', json.dumps(data))

    def test_overview_sensitive_and_ids(self):
        secret = 'ghp_12345678901234567890'
        diff = 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n'
        diff += ''.join(f'@@ -{i} +{i} @@\n-old{i}\n+new{i}\n' for i in range(1, 102))
        diff += '@@ -103 +103 @@\n-old\n+TOKEN=' + secret + '\n'
        diff += 'diff --git a/.env b/.env\n@@ -1 +1 @@\n+TOKEN=' + secret + '\n'
        self.snapshot(diff)
        original = self.fake.generate
        def generate(request, schema):
            payload = json.loads(request.split('\n', 1)[1])
            if payload['stage'] == 'overview':
                self.fake.calls.append(request)
                return json.dumps({'intent': '변경 ' + secret, 'focus': []})
            return original(request, schema)
        self.fake.generate = generate
        result = self.analyzer.run()[0]
        self.assertIn(result['status'], ('completed', 'partial'))
        self.assertNotIn(secret, ''.join(self.fake.calls))
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('.env', self.fake.calls[0])
        from skim.analyze.protocol import validate_overview
        good = {'intent': '주문 처리 변경', 'focus': [{'change_id': 'h1', 'reason': '결제 변경'}]}
        self.assertEqual(validate_overview(json.dumps(good), {'h1'}), good)
        for focus in ([{'change_id': 'h2', 'reason': '외부 ID'}],
                      [{'change_id': 'h1', 'reason': '첫째'}, {'change_id': 'h1', 'reason': '중복'}]):
            with self.assertRaises(ValueError):
                validate_overview(json.dumps({'intent': '변경', 'focus': focus}), {'h1'})

    def test_overview_request_size_bounds(self):
        units, _ = parse_diff(DIFF)
        base = dict(units[0], diff='+' + '가' * 500)
        overview = {'intent': '맥락' * 300, 'focus': []}
        plain = len(prompt('chunk', {'metadata': {}, 'units': [wire(base)]}).encode())
        chunks = chunk_units([base], {}, max_bytes=plain + 16, overview=overview)
        self.assertEqual(len(chunks), 1)
        self.assertNotIn('overview', chunks[0])
        self.assertLessEqual(len(prompt('chunk', chunks[0]).encode()), plain + 16)
        from skim.analyze.context import synthesis_input
        units = [dict(base, change_id=f'h{i}', file_path='가' * 500 + str(i), excluded=False) for i in range(500)]
        items = [dict(change_id=unit['change_id'], importance='high', reason='이유' * 250,
                      behavior='changed', risk_flags=[]) for unit in units]
        data = synthesis_input(units, {'title': '제목', 'body': '본문'}, items, {}, max_bytes=8192)
        self.assertLessEqual(len(prompt('synthesis', data).encode()), 8192)
        self.assertGreater(data['coverage']['synthesis_omitted'], 0)
        self.assertTrue(all(item['file_path_truncated'] for item in data['items']))

    def test_overview_budget_and_previous_result(self):
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')
        with self.conn:
            self.conn.execute("UPDATE analysis_budget SET calls=20")
            self.conn.execute("UPDATE pull_requests SET title='새 목적'")
        self.assertEqual(self.analyzer.run()[0]['status'], 'deferred')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM analyses WHERE status='completed'").fetchone()[0], 1)
        self.assertEqual(self.analyzer.run(snapshot_id=1, budgeted=False)[0]['status'], 'completed')
        self.assertEqual(self.conn.execute('SELECT calls FROM analysis_budget').fetchone()[0], 20)

    def test_overview_cache_and_single_chunk(self):
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')
        stages = [json.loads(call.split('\n', 1)[1])['stage'] for call in self.fake.calls]
        self.assertEqual(stages, ['chunk', 'synthesis'])
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')
        self.assertEqual(len(self.fake.calls), 2)

    def test_overview_file_mapping_and_coverage(self):
        diff = 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n'
        diff += ''.join(f'@@ -{i} +{i} @@\n-old{i}\n+new{i}\n' for i in range(1, 102))
        self.snapshot(diff)
        original = self.fake.generate

        def generate(request, schema):
            data = json.loads(request.split('\n', 1)[1])
            if data['stage'] == 'synthesis':
                self.fake.calls.append(request)
                item = data['data']['items'][-1]
                return json.dumps({'intent': '업무 로직 변경', 'focus': [
                    {'change_id': item['change_id'], 'reason': '핵심 동작'}]})
            return original(request, schema)

        self.fake.generate = generate
        result = self.analyzer.run()[0]
        stages = [json.loads(call.split('\n', 1)[1])['stage'] for call in self.fake.calls]
        self.assertEqual(stages, ['overview', 'chunk', 'chunk', 'synthesis'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['coverage']['analyzed'], 101)
        self.assertIn(result['focus'][0]['change_id'], {item['change_id'] for item in result['items']})
        synthesis = json.loads(self.fake.calls[-1].split('\n', 1)[1])['data']
        self.assertTrue(all(item['file_path'] == 'app.py' for item in synthesis['items']))
        self.assertTrue(all('overview' in json.loads(call.split('\n', 1)[1])['data']
                            for call in self.fake.calls if json.loads(call.split('\n', 1)[1])['stage'] == 'chunk'))

    def test_multichunk_foreign_uncertainties_and_strict_items(self):
        diff = 'diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n'
        diff += ''.join(f'@@ -{i} +{i} @@\n-old{i}\n+new{i}\n' for i in range(1, 102))
        self.snapshot(diff)
        original = self.fake.generate
        local = dict(change_id='h1', reason='청크 안의 미확인 내용')
        foreign = dict(change_id='h100', reason='다른 청크의 미확인 내용')

        for mismatch in (None, 'missing', 'duplicate', 'foreign'):
            with self.subTest(mismatch=mismatch):
                with self.conn:
                    self.conn.execute('UPDATE pull_requests SET title=?', (str(mismatch),))
                self.fake.calls.clear()

                def generate(request, schema):
                    payload = json.loads(request.split('\n', 1)[1])
                    value = json.loads(original(request, schema))
                    if payload['stage'] == 'overview':
                        self.assertIn('h101', {u['change_id'] for u in payload['data']['samples']})
                        value['focus'] = [dict(change_id='h101', reason='전체 변경의 핵심')]
                    elif payload['stage'] == 'chunk' and payload['data']['units'][0]['change_id'] == 'h101':
                        self.assertEqual(payload['data']['overview']['focus'][0]['change_id'], 'h101')
                        self.assertNotIn('h100', {u['change_id'] for u in payload['data']['units']})
                        value['uncertainties'] = [foreign, local]
                        if mismatch == 'missing':
                            value['items'].pop()
                        elif mismatch == 'duplicate':
                            value['items'][-1] = value['items'][0]
                        elif mismatch == 'foreign':
                            value['items'][-1]['change_id'] = 'h100'
                    return json.dumps(value)

                self.fake.generate = generate
                result = self.analyzer.run(budgeted=False)[0]
                payloads = [json.loads(call.split('\n', 1)[1]) for call in self.fake.calls]
                if mismatch is None:
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual([p['stage'] for p in payloads], ['overview', 'chunk', 'chunk', 'synthesis'])
                    self.assertEqual(result['coverage']['analyzed'], 101)
                    self.assertEqual(result['uncertainties'], [local])
                    self.assertEqual(payloads[-1]['data']['uncertainties'], [local])
                else:
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual([p['stage'] for p in payloads], ['overview', 'chunk', 'chunk'])

    def test_synthesis_rejects_foreign_and_duplicate_ids(self):
        from skim.analyze.protocol import validate_synthesis
        for focus in ([{'change_id': 'h9', 'reason': '외부 ID'}],
                      [{'change_id': 'h1', 'reason': '첫째'}, {'change_id': 'h1', 'reason': '중복'}]):
            with self.assertRaises(ValueError):
                validate_synthesis(json.dumps({'intent': '변경', 'focus': focus}), {'h1'})

    def test_secret_excluded_and_metadata_sanitized(self):
        secret = 'ghp_12345678901234567890'
        diff = DIFF + '\ndiff --git a/.env b/.env\n@@ -1 +1 @@\n+TOKEN=' + secret
        self.snapshot(diff)
        with self.conn:
            self.conn.execute('UPDATE pull_requests SET body=?', (secret,))
        value = self.analyzer.run()[0]
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['coverage']['excluded'], 1)
        self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn(secret, ''.join(self.fake.calls))
        self.assertTrue(any(u['excluded'] for u in value['units']))

    def test_unknown_model_never_reuses_persistent_cache(self):
        self.fake.model = 'unknown'
        self.analyzer.run()
        self.analyzer.run()
        self.assertEqual(len(self.fake.calls), 4)

    def test_isolation_blocks_before_budget_or_exec(self):
        runner = unittest.mock.Mock()
        adapter = CodexAdapter(runner)
        with self.assertRaises(IsolationError):
            adapter.generate('자료', {})
        runner.assert_not_called()
        value = Analyzer(self.conn, self.config, adapter).run()[0]
        self.assertEqual(value['status'], 'blocked')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analysis_budget').fetchone()[0], 0)

    def test_synthetic_and_hunk_ids(self):
        diff = DIFF + '''diff --git a/pic.png b/pic.png
Binary files a/pic.png and b/pic.png differ
diff --git a/old b/new
similarity index 100%
rename from old
rename to new
diff --git a/run b/run
old mode 100644
new mode 100755
'''
        units, _ = parse_diff(diff, [{'filename': 'removed', 'status': 'removed'}])
        self.assertEqual([u['change_id'] for u in units], [f'h{i}' for i in range(1, 7)])
        self.assertEqual(sum(u['kind'] == 'synthetic' for u in units), 4)
        self.assertTrue(next(u for u in units if u['file_path'] == 'pic.png')['binary'])

    def test_schema_rejects_missing_duplicate_foreign_extra_and_logic_flag(self):
        item = dict(change_id='h1', importance='mid', reason='변경', behavior='changed', risk_flags=[])
        for items in ([], [item,item], [dict(item, change_id='h2')], [dict(item, other=1)], [dict(item, risk_flags=['logic_without_tests'])]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                validate_chunk(json.dumps(dict(items=items, uncertainties=[])), {'h1'})
        with self.assertRaises(ValueError):
            validate_chunk('{"items":[],"items":[],"uncertainties":[]}', set())

    def test_chunk_uncertainties_keep_only_chunk_ids(self):
        item = dict(change_id='h1', importance='mid', reason='변경', behavior='changed', risk_flags=[])
        local = dict(change_id='h1', reason='청크 안의 미확인 내용')
        foreign = dict(change_id='h2', reason='다른 청크의 미확인 내용')
        value = validate_chunk(json.dumps(dict(items=[item], uncertainties=[foreign, local])), {'h1'})
        self.assertEqual(value['items'], [item])
        self.assertEqual(value['uncertainties'], [local])

    def test_install_script_stays_rule_only(self):
        self.assertNotIn('install_script', FLAGS)
        self.assertEqual(PROMPT_VERSION, 'skim-ko-6')
        item = dict(change_id='h1', importance='high', reason='설치 변경',
                    behavior='changed', risk_flags=['install_script'])
        with self.assertRaises(ValueError):
            validate_chunk(json.dumps(dict(items=[item], uncertainties=[])), {'h1'})

    def test_prompt_states_priority_and_partial_context(self):
        text = prompt('chunk', {'metadata': {'title': 'PR 목적'}, 'units': []})
        for phrase in ('권한 검사 제거·변경', '운영 인프라', '파일 경로, 이름이나 단어',
                       '청크는 PR 전체가 아닐 수 있다', 'uncertainties', '지정 JSON만 반환'):
            self.assertIn(phrase, text)

    def test_chunk_limits_prioritize_rules_and_count_metadata(self):
        units, _ = parse_diff(DIFF)
        units = [dict(units[0], change_id=f'h{i}', candidates=[], file_path=f'{i}.txt') for i in range(1, 503)]
        units[-1]['kind_signals'] = ['install_script']
        chunks = chunk_units(units, {'title':'자료'})
        self.assertEqual(sum(len(c['units']) for c in chunks), 200)
        self.assertEqual(chunks[0]['units'][0]['change_id'], 'h502')
        self.assertTrue(all(len(c['units']) <= 100 and len(prompt('chunk', c).encode()) <= 200*1024 for c in chunks))
        huge, _ = parse_diff(DIFF)
        self.assertEqual(chunk_units(huge, {'body':'가' * 100000}), [])
        self.assertTrue(all(u.get('omission_reason') for u in huge))

    def test_input_and_prompt_version_changes_invalidate_cache(self):
        self.analyzer.run()
        with self.conn:
            self.conn.execute("UPDATE pull_requests SET title='새 제목'")
        self.analyzer.run()
        self.assertEqual(len(self.fake.calls), 4)
        with patch('skim.analyze.PROMPT_VERSION', '다음 버전'):
            self.analyzer.run()
        self.assertEqual(len(self.fake.calls), 6)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM changes').fetchone()[0], 3)

    def test_synthesis_failure_preserves_previous_success(self):
        self.analyzer.run()
        with self.conn:
            self.conn.execute("UPDATE pull_requests SET title='다른 입력'")
        original = self.fake.generate
        def generate(request, schema):
            if '"stage":"synthesis"' in request:
                self.fake.calls.append(request)
                return '{"intent":"변경","extra":true}'
            return original(request, schema)
        self.fake.generate = generate
        result = self.analyzer.run()[0]
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['coverage']['analyzed'], 2)
        stored = self.conn.execute('SELECT id,output_json,error FROM analyses ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(json.loads(stored['output_json'])['items'], result['items'])
        self.assertTrue(stored['error'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analysis_items WHERE analysis_id=?', (stored['id'],)).fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM analyses WHERE status='completed'").fetchone()[0], 1)
        self.assertEqual(len(self.fake.calls), 5)

    def test_unit_limit_and_giant_diff_are_visible(self):
        units, _ = parse_diff(DIFF)
        many = [dict(units[0], change_id=f'h{i}', candidates=[]) for i in range(1, 503)]
        chunks = chunk_units(many, {})
        self.assertEqual(sum(len(c['units']) for c in chunks), 500)
        self.assertEqual(sum(bool(u.get('omission_reason')) for u in many), 2)
        self.snapshot(DIFF + 'x' * (1024*1024))
        result = self.analyzer.run()[0]
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage']['omitted'], 1)
        self.assertEqual(result['coverage']['analyzed'], 1)
        self.assertTrue(any(u.get('omission_reason') for u in result['units']))

    def test_assignments_and_secret_rename_are_filtered(self):
        for value in ('"api_key": "sensitive-value"', 'SECRET_KEY=sensitive-value', 'token = sensitive-value'):
            self.assertNotIn('sensitive-value', sanitize(value))
        units, _ = parse_diff('diff --git a/.env b/public.txt\nsimilarity index 100%\nrename from .env\nrename to public.txt\n')
        self.assertTrue(units[0]['excluded'])

    def test_all_excluded_never_calls_model(self):
        self.snapshot('diff --git a/.env b/.env\n@@ -1 +1 @@\n+hello')
        result = self.analyzer.run()[0]
        self.assertEqual(result['coverage']['excluded'], 1)
        self.assertEqual(result['files'][0]['importance'], None)
        self.assertEqual(self.fake.calls, [])

    def test_analyze_cli_uses_injected_fake(self):
        from skim.cli import main
        with patch('skim.analyze.CodexAdapter', return_value=self.fake):
            self.assertEqual(main(['analyze', '--snapshot', '1']), 0)
        self.assertEqual(len(self.fake.calls), 2)

    def test_git_quoted_korean_path_matches_file_inventory(self):
        path = '인증.py'
        quoted = ''.join('\\%03o' % b for b in path.encode())
        diff = f'diff --git "a/{quoted}" "b/{quoted}"\n--- "a/{quoted}"\n+++ "b/{quoted}"\n@@ -1 +1 @@\n+x\n'
        units, _ = parse_diff(diff, [{'filename': path}])
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]['file_path'], path)

    def test_private_key_multiline_redaction(self):
        value = 'hi\n-----BEGIN RSA PRIVATE KEY-----\nSECRET\n-----END RSA PRIVATE KEY-----\nbye'
        self.assertNotIn('SECRET', sanitize(value))
        self.assertIn('bye', sanitize(value))

    def test_sensitive_pattern_redacts_region_and_keeps_auth_hunk(self):
        self.snapshot(DIFF + '\n+session_token = "private-value"\n+if password == hashed:\n+    reject_session()\n')
        value = self.analyzer.run()[0]
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['coverage']['excluded'], 0)
        self.assertEqual(value['coverage']['redacted'], 1)
        self.assertEqual(value['coverage']['analyzed'], 2)
        self.assertNotIn('private-value', ''.join(self.fake.calls))
        self.assertIn('reject_session()', ''.join(self.fake.calls))
        self.assertTrue(value['units'][1]['redacted'])

    def test_snapshot_preparation_failures_are_recorded_and_next_pr_runs(self):
        import sqlite3
        for path in (None, '/tmp/outside-diff', str(self.config.data_dir / 'cache/missing')):
            with self.subTest(path=path):
                with self.conn:
                    self.conn.execute('UPDATE snapshots SET diff_path=? WHERE id=1', (path,))
                result = self.analyzer.run()[0]
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(self.conn.execute('SELECT status FROM analyses ORDER BY id DESC').fetchone()[0], 'failed')
        with self.conn:
            self.conn.execute("INSERT INTO pull_requests(repo_id,number,title,body,base_sha,head_sha) VALUES(1,2,'다음','설명','base','head')")
            self.conn.execute("INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path) SELECT 2,base_sha,head_sha,diff_hash,input_hash,? FROM snapshots WHERE id=1", (str(self.config.data_dir / 'cache/diff'),))
        results = self.analyzer.run()
        self.assertEqual([r['status'] for r in results], ['failed', 'completed'])
        with self.conn:
            self.conn.execute('UPDATE snapshots SET diff_path=?', (str(self.config.data_dir / 'cache/diff'),))
            self.conn.execute('DELETE FROM analyses')
        original = self.analyzer._reserve
        calls = [0]
        def reserve():
            calls[0] += 1
            if calls[0] == 1:
                raise sqlite3.OperationalError('비밀 잠금 오류')
            return original()
        with patch.object(self.analyzer, '_reserve', side_effect=reserve):
            results = self.analyzer.run()
        self.assertEqual([r['status'] for r in results], ['failed', 'completed'])
        self.assertNotIn('비밀', json.dumps(results))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analyses').fetchone()[0], 2)

    def test_overview_process_interruption(self):
        import fcntl
        import subprocess
        import time
        root = Path(self.temp.name) / 'process-check'
        root.mkdir()
        source = root / 'source'
        source.mkdir()
        (source / 'auth.json').write_text('fixture')
        marker, release = root / 'started', root / 'release'
        model = root / 'model'
        model.write_text(f'''#!{sys.executable}
import os, sys, time
from pathlib import Path
for fd in range(3, 128):
    try: os.close(fd)
    except OSError: pass
Path({str(marker)!r}).write_text(str(os.getpid()))
while not Path({str(release)!r}).exists(): time.sleep(0.02)
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('{{}}')
print('{{"type":"turn.completed"}}')
''')
        model.chmod(0o700)
        parent_code = '''from skim.config import Config
from skim.collect import Collector
from skim.analyze.codex import CodexAdapter
config = Config.load()
adapter = CodexAdapter(data_dir=config.data_dir)
adapter.init_home()
with Collector(None, config).lock('analysis.lock') as lock_fd:
    adapter.analysis_lock_fd = lock_fd
    adapter._execute('fixture', {})
'''
        env = dict(os.environ, SKIM_HOME=str(root / 'home'), CODEX_HOME=str(source),
                   SKIM_CODEX_BIN=str(model), PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'lib'))
        parent = subprocess.Popen([sys.executable, '-c', parent_code], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and parent.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), '가짜 모델 시작 실패')
            pid = int(marker.read_text())
            parent.kill()
            parent.wait(timeout=5)
            lock_path = root / 'home/data/analysis.lock'
            with lock_path.open() as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release.touch()
            deadline = time.monotonic() + 10
            while True:
                try:
                    with lock_path.open() as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline: self.fail('모델 종료 후 잠금이 해제되지 않음')
                    time.sleep(0.02)
        finally:
            release.touch()
            if marker.exists():
                try: os.kill(int(marker.read_text()), 9)
                except ProcessLookupError: pass

    def test_analysis_does_not_hold_collection_lock_during_model_call(self):
        import fcntl
        original = self.fake.generate
        def generate(request, schema):
            with (self.config.data_dir / 'collection.lock').open() as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(request, schema)
        self.fake.generate = generate
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')

    def test_rules_do_not_match_author_lock_statement_or_short_test_substring(self):
        from skim.analyze.units import candidates
        flags = candidates('a.py', '+author = value\n+with self.lock', ['test_database.py'])
        self.assertNotIn('auth', flags)
        self.assertNotIn('dependency', flags)
        self.assertIn('logic_without_tests', flags)
        self.assertNotIn('logic_without_tests', candidates('a.py', '', ['test_a.py']))

    def test_normal_auth_output_does_not_retry(self):
        original = self.fake.generate
        def generate(request, schema):
            value = original(request, schema)
            return value.replace('변경된 인증 동작 검토', 'Authorization: 헤더 검토')
        self.fake.generate = generate
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')
        self.assertEqual(len(self.fake.calls), 2)

    def test_explicit_old_snapshot_reports_reason(self):
        self.snapshot(DIFF)
        result = self.analyzer.run(snapshot_id=1)[0]
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('최신', result['error'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analyses').fetchone()[0], 0)


    def test_auth_variants_and_dependency_paths(self):
        from skim.analyze.units import candidates
        for term in ('permissions/check.py', 'sessions', 'authorize', 'authenticated',
                     'isAuthenticated', 'checkPermission', 'authz', 'OAuth', 'authentication'):
            with self.subTest(term=term):
                self.assertIn('auth', candidates(term, '', []))
        for term in ('authorize', 'authenticated', 'isAuthenticated', 'checkPermission',
                     'authz', 'OAuth', 'authentication', 'requires_auth',
                     'permission_required', 'login_required', 'jwt_required'):
            with self.subTest(call=term):
                self.assertIn('auth', candidates('app.py', '+' + term + '()', []))
        for path in ('clock', 'src/clock', 'src/author.py'):
            self.assertNotIn('dependency', candidates(path, '', []))
            self.assertNotIn('auth', candidates(path, '', []))
        for path in ('yarn.lock', 'Cargo.lock', 'package-lock.json', 'poetry.lock'):
            self.assertIn('dependency', candidates(path, '', []))

    def test_deleted_snapshot_during_model_call_does_not_stop_next_pr(self):
        with self.conn:
            self.conn.execute("INSERT INTO pull_requests(repo_id,number,title,body,base_sha,head_sha) VALUES(1,2,'다음','설명','base','head')")
            self.conn.execute("INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path) SELECT 2,base_sha,head_sha,diff_hash,input_hash,diff_path FROM snapshots WHERE id=1")
        other = connect(self.config)
        self.addCleanup(other.close)
        original = self.fake.generate
        def generate(request, schema):
            if not self.fake.calls:
                with other:
                    other.execute('DELETE FROM pull_requests WHERE id=1')
            return original(request, schema)
        self.fake.generate = generate
        results = self.analyzer.run()
        self.assertEqual([r['status'] for r in results], ['cancelled', 'completed'])
        self.assertIn('삭제', results[0]['error'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analyses').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT calls FROM analysis_budget').fetchone()[0], 4)

    def test_failure_after_snapshot_deleted_and_analysis_hash(self):
        other = connect(self.config)
        self.addCleanup(other.close)
        def reserve():
            with other:
                other.execute('DELETE FROM pull_requests WHERE id=1')
            raise RuntimeError('실패')
        with patch.object(self.analyzer, '_reserve', side_effect=reserve):
            self.assertEqual(self.analyzer.run()[0]['status'], 'cancelled')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analyses').fetchone()[0], 0)

    def test_failure_stores_prepared_hash_instead_of_collection_hash(self):
        with patch.object(self.analyzer, '_reserve', side_effect=RuntimeError('실패')):
            self.assertEqual(self.analyzer.run()[0]['status'], 'failed')
        failure_hash = self.conn.execute('SELECT input_hash FROM analyses').fetchone()[0]
        self.assertNotEqual(failure_hash, 'raw')
        self.analyzer.run()
        success_hash = self.conn.execute('SELECT input_hash FROM analyses ORDER BY id DESC').fetchone()[0]
        self.assertEqual(failure_hash, success_hash)

    def test_collection_failure_during_model_call_is_preserved(self):
        other = connect(self.config)
        self.addCleanup(other.close)
        original = self.fake.generate
        def generate(request, schema):
            if not self.fake.calls:
                with other:
                    other.execute('UPDATE snapshots SET coverage=? WHERE id=1',
                                  (json.dumps(dict(status='incomplete', collection_error='수집 권한 실패', files=[])),))
            return original(request, schema)
        self.fake.generate = generate
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')
        coverage = json.loads(self.conn.execute('SELECT coverage FROM snapshots').fetchone()[0])
        self.assertEqual(coverage['status'], 'incomplete')
        self.assertEqual(coverage['collection_error'], '수집 권한 실패')
        self.assertEqual(coverage['analysis']['analyzed'], 2)
        self.assertEqual(coverage['raw_input_hash'], 'raw')
        self.assertEqual(self.analyzer.run()[0]['status'], 'blocked')

    def test_value_only_secrets_keep_candidates_even_when_model_says_low(self):
        for token in ('AKIA' + 'A' * 16, 'ghp_abc', 'sk_live_abc', 'xoxb-abc',
                      'export API_TOKEN=abcd1234', 'DB_PASSWORD=hunter2'):
            with self.subTest(token=token):
                self.snapshot('diff --git a/a.txt b/a.txt\n@@ -1 +1 @@\n+' + token)
                result = self.analyzer.run()[0]
                self.assertEqual(result['items'][0]['importance'], 'low')
                self.assertIn('secrets', result['units'][0]['candidates'])
                self.assertIn('secrets', result['files'][0]['candidates'])
                self.assertEqual(result['coverage']['redacted'], 1)
                self.assertNotIn(token, json.dumps(result))
        units, _ = parse_diff('diff --git a/.env b/.env\n@@ -1 +1 @@\n+ghp_abc')
        self.assertTrue(units[0]['excluded'])
        self.assertFalse(units[0]['redacted'])
        self.assertIsNone(units[0]['redaction_reason'])

    def test_redacted_model_output_is_revalidated(self):
        original = self.fake.generate
        def generate(request, schema):
            value = json.loads(original(request, schema))
            if 'items' in value:
                for item in value['items']:
                    item['reason'] = 'ghp_a ' * 80
            else:
                value['intent'] = 'ghp_a ' * 40
            return json.dumps(value)
        self.fake.generate = generate
        result = self.analyzer.run()[0]
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['items'], [])
        self.assertEqual(len(self.fake.calls), 2)

    def test_identical_preparation_failures_do_not_accumulate(self):
        for coverage, path, status in ((dict(status='incomplete'), None, 'blocked'), ({}, None, 'failed')):
            with self.subTest(status=status):
                with self.conn:
                    self.conn.execute('DELETE FROM analyses')
                    self.conn.execute('UPDATE snapshots SET coverage=?,diff_path=?', (json.dumps(coverage), path))
                for _ in range(3):
                    self.assertEqual(self.analyzer.run()[0]['status'], status)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM analyses').fetchone()[0], 1)

    def test_dependency_and_plain_test_filename_candidates(self):
        from skim.analyze.units import candidates
        for path in ('pnpm-lock.yaml', 'go.sum', 'Gemfile', 'src/Gemfile'):
            self.assertIn('dependency', candidates(path, '', []))
        units, tests = parse_diff(DIFF, [{'filename': 'tests/app.py'}])
        self.assertEqual(tests, ['tests/app.py'])
        self.assertNotIn('logic_without_tests', units[0]['candidates'])

    def test_redacted_synthesis_output_is_revalidated(self):
        original = self.fake.generate
        def generate(request, schema):
            value = json.loads(original(request, schema))
            if 'intent' in value:
                value['intent'] = 'ghp_a ' * 40
            return json.dumps(value)
        self.fake.generate = generate
        result = self.analyzer.run()[0]
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['intent'])
        self.assertEqual(len(self.fake.calls), 3)
