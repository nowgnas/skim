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
from skim.analyze.protocol import chunk_units, validate_chunk, prompt
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
            return '{"intent":"인증 동작을 변경한다."}'
        return json.dumps(dict(items=[dict(change_id=u['change_id'], importance='high' if u['change_id'] == 'h2' else 'low', reason='변경된 인증 동작 검토', risk_flags=[]) for u in value['data']['units']], uncertainties=[]))


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
            self.conn.execute("INSERT INTO pull_requests(repo_id,number,title,body,base_sha,head_sha) VALUES(1,1,'변경','설명','base','head')")
        self.snapshot(DIFF)
        self.fake = Fake()
        self.analyzer = Analyzer(self.conn, self.config, self.fake, today=lambda: '2026-10-31')

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

    def test_opt_in_draft_and_policy_precedence(self):
        with self.conn:
            self.conn.execute("UPDATE repositories SET analysis_policy='deny'")
            self.conn.execute("INSERT INTO organization_policies VALUES('org','allow')")
        self.assertEqual(self.analyzer.run(), [])
        with self.conn:
            self.conn.execute("UPDATE repositories SET analysis_policy='inherit'")
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
        self.assertEqual(self.conn.execute('SELECT calls FROM analysis_budget').fetchone()[0], 20)
        self.analyzer.today = lambda: '2026-11-01'
        self.fake.invalid = False
        self.assertEqual(self.analyzer.run()[0]['status'], 'completed')

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
        item = dict(change_id='h1', importance='mid', reason='변경', risk_flags=[])
        for items in ([], [item,item], [dict(item, change_id='h2')], [dict(item, other=1)], [dict(item, risk_flags=['logic_without_tests'])]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                validate_chunk(json.dumps(dict(items=items, uncertainties=[])), {'h1'})
        with self.assertRaises(ValueError):
            validate_chunk('{"items":[],"items":[],"uncertainties":[]}', set())
        with self.assertRaises(ValueError):
            validate_chunk(json.dumps(dict(items=[item], uncertainties=[dict(change_id='h2', reason='모름')])), {'h1'})

    def test_chunk_limits_prioritize_rules_and_count_metadata(self):
        units, _ = parse_diff(DIFF)
        units = [dict(units[0], change_id=f'h{i}', candidates=[], file_path=f'{i}.txt') for i in range(1, 503)]
        units[-1]['candidates'] = ['auth']
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
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['items'], [])
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
