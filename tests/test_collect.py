"""실제 gh나 네트워크 호출 없이 수집 계약을 검증한다."""
import contextlib
import io
import json
import os
from pathlib import Path
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
            value = [{'filename': 'a.py', 'status': 'modified'}]
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
        self.assertEqual(self.conn.execute('SELECT state FROM pull_requests').fetchone()[0], 'closed')

    def test_race_retry_and_failure_preserves_data(self):
        self.fake.race = 1
        self.collector.collect()
        self.assertEqual({r[0] for r in self.conn.execute('SELECT head_sha FROM snapshots')}, {'head1x'})
        self.fake.race = 2
        with self.assertRaises(CollectionError):
            self.collector.collect()
        self.assertEqual(self.count('snapshots'), 2)
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
        with self.assertRaises(CollectionError):
            self.collector.collect()
        self.assertEqual(self.count('snapshots'), 2)

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


if __name__ == '__main__':
    unittest.main()
