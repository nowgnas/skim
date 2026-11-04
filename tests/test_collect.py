"""실제 gh나 네트워크 호출 없이 수집 계약을 검증한다."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.collect import Collector, GitHub, Deferred, CollectionError, CollectionScheduler
from skim.config import Config
from skim.db import connect
from skim.cli import main


class FakeGH:
    def __init__(self):
        self.calls = []
        self.state = 'open'
        self.head = 'head1'
        self.body = '설명'
        self.personal = True
        self.team = True
        self.race = 0
        self.fail = False
        self.review_repo = 'outside'
        self.file_count = 1

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        assert command[:7] == ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', '--include']
        assert kwargs == dict(capture_output=True, text=True, timeout=120)
        endpoint = command[7]
        url = urlsplit(endpoint)
        path, query = url.path, parse_qs(url.query)
        if self.fail:
            return subprocess.CompletedProcess(command, 1, '', '비밀 원문')
        if path == 'user':
            value = {'login': 'me'}
        elif path == 'user/repos':
            value = [{'full_name': 'org/candidate', 'owner': {'login': 'org'}, 'permissions': {'admin': True}}]
        elif path == 'user/teams':
            value = [{'organization': {'login': 'org'}, 'slug': 'team'}]
        elif path == 'search/issues':
            active = self.team if 'team-review-requested' in query['q'][0] else self.personal
            value = dict(total_count=int(active), incomplete_results=False, items=[dict(repository_url='https://api.github.com/repos/org/' + self.review_repo, number=1 if self.review_repo == 'repo' else 2)] if active else [])
        elif path.endswith('/pulls'):
            value = [{'number': 1}] if self.state == 'open' else []
        elif path.endswith('/files'):
            value = [{'filename': 'a.py', 'status': 'modified', 'patch': '+중복 저장 금지'}]
        elif '-H' in command:
            if self.race:
                self.head += 'x'
                self.race -= 1
            return subprocess.CompletedProcess(command, 0, 'HTTP/2.0 200 OK\r\n\r\ndiff --git a/a.py b/a.py\n+변경\n', '')
        else:
            value = dict(title='제목', body=self.body, state='open' if self.state == 'merged' else self.state,
                         merged_at='2026-01-01' if self.state == 'merged' else None,
                         user={'login': 'me'}, head={'sha': self.head, 'ref': 'branch'}, base={'sha': 'base'},
                         updated_at='2026-10-31T00:00:00Z', draft=False, changed_files=self.file_count)
        return subprocess.CompletedProcess(command, 0, 'HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 500\r\n\r\n' + json.dumps(value), '')


class CollectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, SKIM_HOME=temp.name)
        env.start()
        self.addCleanup(env.stop)
        self.config = Config.load()
        self.conn = connect(self.config)
        self.addCleanup(self.conn.close)
        with self.conn:
            self.conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('org','repo',1)")
        self.fake = FakeGH()
        self.collector = Collector(self.conn, self.config, self.fake)

    def count(self, table):
        return self.conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def test_collect_dedupe_candidates_and_snapshots(self):
        self.assertEqual(self.collector.collect(), 2)
        self.assertEqual(self.count('pull_requests'), 2)
        self.assertEqual(self.count('snapshots'), 2)
        candidate = self.conn.execute("SELECT * FROM repositories WHERE name='candidate'").fetchone()
        self.assertEqual((candidate['tracked'], candidate['source']), (0, 'managed'))
        outside = self.conn.execute("SELECT * FROM repositories WHERE name='outside'").fetchone()
        self.assertEqual(outside['tracked'], 0)
        self.assertFalse(any('repos/org/candidate/pulls' in c[7] for c in self.fake.calls))
        self.collector.collect()
        self.assertEqual(self.count('snapshots'), 2)
        self.fake.body = '수정 설명'
        self.collector.collect()
        self.assertEqual(self.count('snapshots'), 4)
        for row in self.conn.execute('SELECT * FROM snapshots'):
            self.assertEqual(Path(row['diff_path']).stat().st_mode & 0o777, 0o600)
            self.assertIn('변경', Path(row['diff_path']).read_text())
            self.assertEqual(len(row['input_hash']), 64)
            self.assertEqual(json.loads(row['coverage'])['files'][0]['filename'], 'a.py')
            self.assertNotIn('patch', json.loads(row['coverage'])['files'][0])

    def test_recollect_after_analysis_does_not_duplicate_snapshot(self):
        from skim.analyze import Analyzer
        from test_analyze import Fake
        self.collector.collect()
        with self.conn:
            self.conn.execute("UPDATE github_hosts SET analysis_scope='all'")
        Analyzer(self.conn, self.config, Fake()).run()
        self.collector.collect()
        self.assertEqual(self.count('snapshots'), 2)
        self.fake.body = '설명 변경'
        self.collector.collect()
        self.assertEqual(self.count('snapshots'), 4)

    def test_review_removal_and_closed_merged(self):
        self.collector.collect()
        self.fake.personal = False
        self.collector.collect()
        self.assertEqual(self.count('pull_requests'), 2)  # 팀 요청이 남아 있다.
        self.fake.team = False
        self.collector.collect()
        self.assertEqual(self.count('pull_requests'), 1)
        self.fake.state = 'merged'
        self.collector.collect()
        self.assertEqual(self.conn.execute('SELECT state FROM pull_requests').fetchone()[0], 'merged')
        self.assertEqual(self.count('pr_sources'), 0)
        self.fake.state = 'closed'
        self.collector.collect()
        self.assertEqual(self.conn.execute('SELECT state FROM pull_requests').fetchone()[0], 'merged')

    def test_race_retry_and_failure_preserves_data(self):
        self.fake.race = 1
        self.collector.collect()
        self.assertEqual({r[0] for r in self.conn.execute('SELECT head_sha FROM snapshots')}, {'head1x'})
        self.fake.race = 2
        with self.assertRaises(CollectionError):
            self.collector.collect()
        self.assertEqual(self.count('snapshots'), 3)  # 다른 PR은 계속 갱신한다.
        self.fake.fail = True
        with self.assertRaises(CollectionError) as raised:
            self.collector.collect()
        self.assertNotIn('비밀', str(raised.exception))
        self.assertEqual(self.count('pr_sources'), 2)

    def test_scheduler_shared_queue_and_backoff(self):
        now = [1000]
        scheduler = CollectionScheduler(self.conn, 300, lambda: now[0])
        scheduler.request()
        scheduler.request()
        self.assertEqual(self.count('jobs'), 1)
        self.fake.fail = True
        self.assertTrue(scheduler.tick(self.collector))
        self.assertFalse(scheduler.tick(self.collector))
        self.fake.fail = False
        now[0] += 31
        self.assertTrue(scheduler.tick(self.collector))
        self.assertEqual(self.conn.execute('SELECT status FROM jobs').fetchone()[0], 'completed')
        self.assertFalse(scheduler.tick(self.collector))
        now[0] += 300
        self.assertTrue(scheduler.tick(self.collector))

    def test_cli_uses_injected_subprocess(self):
        with patch('skim.collect.subprocess.run', self.fake), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['collect']), 0)
        self.assertIn('2개', out.getvalue())

    def test_repository_and_review_sources_and_candidate_cli(self):
        self.fake.review_repo = 'repo'
        self.collector.collect()
        self.assertEqual(self.count('pull_requests'), 1)
        self.assertEqual(self.count('pr_sources'), 2)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['repo', 'list', '--candidates']), 0)
        self.assertIn('org/candidate', out.getvalue())
        self.assertNotIn('org/repo', out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['repo', 'remove', 'org/repo']), 0)
        self.assertEqual(self.count('snapshots'), 1)
        self.fake.personal = self.fake.team = False
        self.collector.collect()
        self.assertEqual(self.count('pull_requests'), 0)
        self.assertEqual(list((self.config.data_dir / 'cache/diffs').glob('*.diff')), [])

    def test_incomplete_files_preserve_previous_snapshot(self):
        self.collector.collect()
        self.fake.file_count = 3001
        self.assertEqual(self.collector.collect(), 0)
        self.assertEqual(self.count('snapshots'), 4)
        old = list(self.conn.execute('SELECT coverage FROM snapshots WHERE id<=2'))
        self.assertTrue(all(json.loads(r[0])['status'] == 'collected' for r in old))
        self.assertTrue(all('omitted' not in json.loads(r[0]) for r in old))
        latest = list(self.conn.execute('SELECT coverage FROM snapshots WHERE id>2'))
        self.assertTrue(all(json.loads(r[0])['omitted'] == 3001 for r in latest))

    def test_per_pr_http_failures_keep_snapshot_and_allow_reconciliation(self):
        self.collector.collect()
        before = self.conn.execute('SELECT s.* FROM snapshots s JOIN pull_requests p ON p.id=s.pr_id WHERE p.number=2').fetchone()
        def runner(command, **kwargs):
            if command[7] == 'repos/org/outside/pulls/2':
                return subprocess.CompletedProcess(command, 1, 'HTTP/2.0 403 Error\n\n{}', '비밀')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = runner
        self.fake.state = 'closed'
        with self.assertRaisesRegex(CollectionError, '인증 또는 권한'):
            self.collector.collect()
        self.assertEqual(self.count('pr_sources'), 1)
        coverage = json.loads(self.conn.execute('SELECT coverage FROM snapshots WHERE id=?', (before['id'],)).fetchone()[0])
        self.assertEqual(coverage['status'], 'incomplete')
        self.assertNotIn('비밀', coverage['collection_error'])
        self.collector.gh.runner = self.fake
        self.fake.state = 'open'
        self.collector.collect()
        coverage = json.loads(self.conn.execute('SELECT coverage FROM snapshots WHERE id=?', (before['id'],)).fetchone()[0])
        self.assertEqual(coverage['status'], 'collected')

    def test_404_and_diff_limit_do_not_backoff_scheduler(self):
        self.collector.collect()
        def runner(command, **kwargs):
            if command[7] == 'repos/org/outside/pulls/2':
                code = 406 if '-H' in command else 404
                return subprocess.CompletedProcess(command, 1, f'HTTP/2.0 {code} Error\n\n{{}}', '')
            if command[7] == 'repos/org/repo/pulls/1' and '-H' in command:
                return subprocess.CompletedProcess(command, 1, 'HTTP/2.0 406 Error\n\n{}', '')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = runner
        now = [1000]
        scheduler = CollectionScheduler(self.conn, clock=lambda: now[0])
        self.assertTrue(scheduler.tick(self.collector))
        self.assertEqual(self.conn.execute('SELECT status FROM jobs').fetchone()[0], 'completed')
        self.assertEqual(self.count('pull_requests'), 1)
        latest = self.conn.execute('SELECT coverage FROM snapshots ORDER BY id DESC').fetchone()[0]
        self.assertIn('한도 초과', latest)
        now[0] += 300
        self.assertTrue(scheduler.tick(self.collector))

    def test_manual_request_expedites_backoff_and_preserves_running_refresh(self):
        scheduler = CollectionScheduler(self.conn, clock=lambda: 1000)
        self.fake.fail = True
        scheduler.tick(self.collector)
        scheduler.request()
        self.fake.fail = False
        with patch.object(self.collector, 'collect', side_effect=lambda: scheduler.request()):
            self.assertTrue(scheduler.tick(self.collector))
        self.assertEqual([r[0] for r in self.conn.execute('SELECT status FROM jobs ORDER BY id')], ['completed', 'pending'])
        self.assertTrue(scheduler.tick(self.collector))

    def test_invalid_and_naive_retry_after(self):
        for date, expected in [('broken', 1060), ('Thu, 01 Jan 1970 00:20:00', 1200)]:
            runner = lambda c, **kw: subprocess.CompletedProcess(c, 1, f'HTTP/2 429 Error\nRetry-After: {date}\n\n{{}}', '')
            with self.assertRaises(Deferred) as caught:
                GitHub(runner, lambda: 1000).get('user')
            self.assertEqual(caught.exception.retry_at, expected)

    def test_closed_pr_is_not_requested_again(self):
        self.fake.personal = self.fake.team = False
        self.collector.collect()
        self.fake.state = 'closed'
        self.collector.collect()
        self.fake.calls.clear()
        self.collector.collect()
        self.assertFalse(any(c[7] == 'repos/org/repo/pulls/1' for c in self.fake.calls))

    def test_malformed_pr_does_not_prevent_review_removal(self):
        self.collector.collect()
        self.fake.personal = self.fake.team = False
        def runner(command, **kwargs):
            if command[7] == 'repos/org/repo/pulls/1':
                return subprocess.CompletedProcess(command, 0, '{}', '')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = runner
        with self.assertRaises(CollectionError):
            self.collector.collect()
        self.assertEqual(self.count('pull_requests'), 1)
        coverage = json.loads(self.conn.execute('SELECT coverage FROM snapshots').fetchone()[0])
        self.assertEqual(coverage['status'], 'incomplete')

    def test_deleted_tracked_repo_does_not_block_other_pr(self):
        self.collector.collect()
        self.fake.body = '새 설명'
        def runner(command, **kwargs):
            if command[7].startswith('repos/org/repo/'):
                return subprocess.CompletedProcess(command, 1, 'HTTP/2.0 404 Error\n\n{}', '')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = runner
        with self.assertRaisesRegex(CollectionError, '저장소 목록.*접근 불가'):
            self.collector.collect()
        self.assertEqual(self.count('snapshots'), 2)
        self.assertEqual(self.count('pr_sources'), 1)

    def test_failed_repository_listing_preserves_repository_source(self):
        self.collector.collect()
        def runner(command, **kwargs):
            if command[7].startswith('repos/org/repo/pulls?'):
                return subprocess.CompletedProcess(command, 1, 'HTTP/2.0 403 Error\n\n{}', '')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = runner
        with self.assertRaises(CollectionError):
            self.collector.collect()
        self.assertEqual(self.count('pull_requests'), 2)
        self.assertEqual(self.count('pr_sources'), 2)

    def test_case_preserved_and_lost_candidate_removed(self):
        self.fake.review_repo = 'MixedCase'
        self.collector.collect()
        row = self.conn.execute("SELECT r.name,p.url FROM repositories r JOIN pull_requests p ON p.repo_id=r.id WHERE r.name='mixedcase'").fetchone()
        self.assertEqual(row['name'], 'MixedCase')
        self.assertIn('/org/MixedCase/pull/', row['url'])
        original = self.fake
        def runner(command, **kwargs):
            if command[7].startswith('user/repos?'):
                return subprocess.CompletedProcess(command, 0, '[]', '')
            return original(command, **kwargs)
        self.collector.gh.runner = runner
        self.collector.collect()
        self.assertEqual(self.conn.execute("SELECT source FROM repositories WHERE name='candidate'").fetchone()[0], 'responsible')

    def test_scheduler_recovers_crashed_job_and_unexpected_errors(self):
        now = [1000]
        scheduler = CollectionScheduler(self.conn, clock=lambda: now[0])
        scheduler.request()
        with self.conn:
            self.conn.execute("UPDATE jobs SET status='running'")
        for exc in (KeyError('비밀'), TypeError('비밀'), sqlite3.OperationalError('비밀')):
            with self.subTest(exc=type(exc).__name__):
                with patch.object(self.collector, 'collect', side_effect=exc):
                    self.assertTrue(scheduler.tick(self.collector))
                row = self.conn.execute('SELECT * FROM jobs').fetchone()
                self.assertEqual(row['status'], 'pending')
                self.assertNotIn('비밀', row['last_error'])
                now[0] += 400
        self.assertTrue(scheduler.tick(self.collector))
        self.assertEqual(self.conn.execute('SELECT status FROM jobs').fetchone()[0], 'completed')

    def test_scheduler_recovers_even_if_failure_record_could_not_be_saved(self):
        scheduler = CollectionScheduler(self.conn, clock=lambda: 1000)
        with self.conn:
            self.conn.execute("CREATE TRIGGER fail_pending BEFORE UPDATE ON jobs WHEN NEW.status='pending' BEGIN SELECT RAISE(FAIL,'DB 실패'); END")
        with patch.object(self.collector, 'collect', side_effect=ValueError('오류')):
            with self.assertRaises(sqlite3.Error):
                scheduler.tick(self.collector)
        self.assertEqual(self.conn.execute('SELECT status FROM jobs').fetchone()[0], 'running')
        with self.conn:
            self.conn.execute('DROP TRIGGER fail_pending')
        self.assertTrue(scheduler.tick(self.collector))
        self.assertEqual(self.conn.execute('SELECT status FROM jobs').fetchone()[0], 'completed')

    def test_atomic_diff_replacement_and_process_lock(self):
        self.collector.collect()
        path = Path(self.conn.execute('SELECT diff_path FROM snapshots').fetchone()[0])
        with path.open() as reader:
            old_inode = os.fstat(reader.fileno()).st_ino
            self.collector.collect()
            self.assertNotEqual(old_inode, path.stat().st_ino)
            self.assertEqual(reader.read(), path.read_text())
        original = path.read_bytes()
        with patch('skim.collect.os.replace', side_effect=OSError('교체 실패')):
            with self.assertRaises(OSError):
                self.collector.collect()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(path.parent.glob('*.tmp')))
        code = "import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        with self.collector.lock():
            result = subprocess.run([sys.executable, '-c', code, str(self.config.data_dir / 'collection.lock')], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        result = subprocess.run([sys.executable, '-c', code, str(self.config.data_dir / 'collection.lock')], capture_output=True)
        self.assertEqual(result.returncode, 0)

    def test_rate_limit_and_retry_after(self):
        for headers, code, expected in [('Retry-After: 120', 429, 1120),
                                        ('X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1300', 403, 1300),
                                        ('Retry-After: Thu, 01 Jan 1970 00:20:00 GMT', 429, 1200)]:
            runner = lambda command, **kw: subprocess.CompletedProcess(command, 1, f'HTTP/2.0 {code} Error\n{headers}\n\n{{}}', '')
            gh = GitHub(runner, lambda: 1000)
            with self.assertRaises(Deferred) as raised:
                gh.get('user')
            self.assertEqual(raised.exception.retry_at, expected)
            with self.assertRaises(Deferred):
                gh.get('user')

    def test_successful_last_request_defers_following_call_and_preserves_diff(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0,
                'HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 1300\r\n\r\n+line\r\n', '')
        gh = GitHub(runner, lambda: 1000)
        self.assertEqual(gh.get('repos/org/repo/pulls/1', diff=True), '+line\r\n')
        with self.assertRaises(Deferred):
            gh.get('user')
        self.assertEqual(len(calls), 1)

    def test_pagination_and_incomplete_search(self):
        seen = []
        def runner(command, **kwargs):
            seen.append(command[7])
            page = int(parse_qs(urlsplit(command[7]).query)['page'][0])
            return subprocess.CompletedProcess(command, 0, json.dumps([{}] * (100 if page == 1 else 1)), '')
        self.assertEqual(len(GitHub(runner).pages('user/repos')), 101)
        self.assertIn('page=2', seen[1])
        runner = lambda c, **kw: subprocess.CompletedProcess(c, 0, json.dumps(dict(total_count=1001, items=[])), '')
        with self.assertRaises(CollectionError):
            GitHub(runner).pages('search/issues', q='is:pr')


    def test_new_pr_failures_remain_visible_in_job(self):
        for status in (500, 403):
            with self.subTest(status=status):
                def runner(command, **kwargs):
                    if command[7] == 'repos/org/outside/pulls/2':
                        return subprocess.CompletedProcess(command, 1, f'HTTP/2 {status} Error\n\n{{}}', '비밀')
                    return self.fake(command, **kwargs)
                self.collector.gh.runner = runner
                scheduler = CollectionScheduler(self.conn, clock=lambda: 1000)
                scheduler.request()
                self.assertTrue(scheduler.tick(self.collector))
                row = self.conn.execute("SELECT * FROM jobs WHERE status='pending'").fetchone()
                self.assertIn('org/outside#2', row['last_error'])
                self.assertNotIn('비밀', row['last_error'])
                self.assertEqual(row['next_run_at'], '1970-01-01T00:17:10+00:00')
                self.assertEqual(self.count('pull_requests'), 1)
                with self.conn:
                    self.conn.execute('DELETE FROM jobs')

    def test_manual_refresh_preserves_retry_after_across_restart(self):
        scheduler = CollectionScheduler(self.conn, clock=lambda: 1000)
        def deferred_collect():
            scheduler.request()
            raise Deferred(1300)
        with patch.object(self.collector, 'collect', side_effect=deferred_collect):
            self.assertTrue(scheduler.tick(self.collector))
        row = self.conn.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(row['next_run_at'], '1970-01-01T00:21:40+00:00')
        self.assertEqual(row['refresh_requested'], 0)
        restarted_conn = connect(self.config)
        self.addCleanup(restarted_conn.close)
        restarted = CollectionScheduler(restarted_conn, clock=lambda: 1001)
        restarted.request()
        collector = Collector(restarted_conn, self.config, self.fake)
        self.assertFalse(restarted.tick(collector))
        self.assertEqual(self.fake.calls, [])
        restarted.clock = lambda: 1300
        self.assertTrue(restarted.tick(collector))


    def test_diff_422_is_a_collection_failure(self):
        self.collector.collect()
        def runner(command, **kwargs):
            if '-H' in command:
                return subprocess.CompletedProcess(command, 1, 'HTTP/2 422 Error\n\n{}', '')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = runner
        with self.assertRaises(CollectionError):
            self.collector.collect()
        self.assertEqual(self.count('snapshots'), 2)
        for row in self.conn.execute('SELECT coverage FROM snapshots'):
            coverage = json.loads(row[0])
            self.assertEqual(coverage['status'], 'incomplete')
            self.assertNotIn('omission_reason', coverage)

    def test_oversized_diff_rechecks_sha_before_storing(self):
        def runner(command, **kwargs):
            if '-H' in command:
                self.fake.head += 'x'
                return subprocess.CompletedProcess(command, 1, 'HTTP/2 406 Error\n\n{}', '')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = runner
        with self.assertRaises(CollectionError):
            self.collector.collect()
        self.assertEqual(self.count('snapshots'), 0)
        self.fake.head = 'stable'
        def stable_runner(command, **kwargs):
            if '-H' in command:
                return subprocess.CompletedProcess(command, 1, 'HTTP/2 406 Error\n\n{}', '')
            return self.fake(command, **kwargs)
        self.collector.gh.runner = stable_runner
        self.assertEqual(self.collector.collect(), 0)
        self.assertEqual(self.count('snapshots'), 2)
        self.assertEqual({r[0] for r in self.conn.execute('SELECT head_sha FROM snapshots')}, {'stable'})

    def test_nested_different_lock_names_are_both_held(self):
        code = "import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        def available(name):
            return subprocess.run([sys.executable, '-c', code, str(self.config.data_dir / name)], capture_output=True).returncode == 0
        with self.collector.lock():
            with self.collector.lock('analysis.lock'):
                with self.collector.lock('analysis.lock'):
                    self.assertFalse(available('collection.lock'))
                    self.assertFalse(available('analysis.lock'))
            self.assertTrue(available('analysis.lock'))
            self.assertFalse(available('collection.lock'))
        self.assertTrue(available('collection.lock'))


if __name__ == '__main__':
    unittest.main()
