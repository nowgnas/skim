"""기간 정리의 로컬 DB 및 diff 보호 계약."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.config import Config
from skim.db import connect
from skim.lifecycle import cleanup


class FakeGH:
    host = 'github.com'
    def __init__(self):
        self.responses = {}
    def get(self, endpoint):
        response = self.responses[endpoint]
        if isinstance(response, Exception):
            raise response
        return response


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, SKIM_HOME=tmp.name)
        env.start()
        self.addCleanup(env.stop)
        self.config = Config.load()
        self.conn = connect(self.config)
        self.addCleanup(self.conn.close)
        with self.conn:
            self.conn.execute("INSERT INTO repositories(owner,name) VALUES('org','repo')")
        self.gh = FakeGH()
        self.cache = self.config.data_dir / 'cache' / 'diffs'
        self.cache.mkdir(mode=0o700)

    def add_pr(self, number, state='closed', closed='2026-01-01T00:00:00Z', diff='shared.diff'):
        with self.conn:
            pr = self.conn.execute('INSERT INTO pull_requests(repo_id,number,state,closed_at) VALUES(1,?,?,?)', (number, state, closed)).lastrowid
            path = self.cache / diff
            path.write_text('diff')
            self.conn.execute("INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path) VALUES(?, 'b','h','d','i',?)", (pr, str(path)))
        self.gh.responses[f'repos/org/repo/pulls/{number}'] = {'state': 'closed', 'closed_at': closed, 'merged_at': None}
        return pr

    def test_closed_expired_deleted_but_shared_diff_preserved(self):
        expired = self.add_pr(1)
        self.add_pr(2, state='open')
        result = cleanup(self.config, self.conn, self.gh, now=1769000000)
        self.assertEqual(result['removed'], 1)
        self.assertIsNone(self.conn.execute('SELECT id FROM pull_requests WHERE id=?', (expired,)).fetchone())
        self.assertTrue((self.cache / 'shared.diff').exists())

    def test_inaccessible_reopened_and_unknown_close_are_preserved(self):
        for number in (1, 2, 3):
            self.add_pr(number, closed=None, diff=f'{number}.diff')
        self.gh.responses['repos/org/repo/pulls/1'] = ValueError('private')
        self.gh.responses['repos/org/repo/pulls/2'] = {'state': 'open', 'closed_at': None, 'merged_at': None}
        self.gh.responses['repos/org/repo/pulls/3'] = {'state': 'closed', 'closed_at': None, 'merged_at': None}
        cleanup(self.config, self.conn, self.gh, now=1769000000)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pull_requests').fetchone()[0], 3)

    def test_active_job_keeps_pr_and_history_cannot_rebind(self):
        pr = self.add_pr(1)
        with self.conn:
            self.conn.execute("INSERT INTO jobs(type,target_id,status) VALUES('analyze',?,'pending')", (pr,))
        cleanup(self.config, self.conn, self.gh, now=1769000000)
        self.assertIsNotNone(self.conn.execute('SELECT id FROM pull_requests WHERE id=?', (pr,)).fetchone())
        with self.conn:
            self.conn.execute("UPDATE jobs SET status='completed'")
        cleanup(self.config, self.conn, self.gh, now=1769000000)
        self.assertLess(self.conn.execute('SELECT target_id FROM jobs').fetchone()[0], 0)

    def test_symlinked_cache_is_rejected_without_deleting_external_files_or_db(self):
        import shutil
        self.add_pr(1)
        shutil.rmtree(self.cache)
        with tempfile.TemporaryDirectory() as outside:
            victim = Path(outside) / 'victim.diff'
            victim.write_text('external')
            self.cache.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                cleanup(self.config, self.conn, self.gh, now=1769000000)
            self.assertTrue(victim.exists())
            self.assertEqual(self.conn.execute('SELECT count(*) FROM pull_requests').fetchone()[0], 1)

    def test_exact_age_boundary_and_recent_pr(self):
        from datetime import datetime, timezone
        now = 1769000000
        boundary = datetime.fromtimestamp(now - 7 * 86400, timezone.utc).isoformat()
        recent = datetime.fromtimestamp(now - 7 * 86400 + 1, timezone.utc).isoformat()
        self.add_pr(1, closed=boundary, diff='old.diff')
        self.add_pr(2, closed=recent, diff='new.diff')
        result = cleanup(self.config, self.conn, self.gh, now=now)
        self.assertEqual(result['removed'], 1)
        self.assertEqual(self.conn.execute('SELECT number FROM pull_requests').fetchone()[0], 2)
        self.assertFalse((self.cache / 'old.diff').exists())

    def test_busy_analysis_defers_deletion(self):
        import fcntl
        self.add_pr(1)
        fd = os.open(self.config.data_dir / 'analysis.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = cleanup(self.config, self.conn, self.gh, now=1769000000)
            self.assertTrue(result['busy'])
            self.assertEqual(self.conn.execute('SELECT count(*) FROM pull_requests').fetchone()[0], 1)
        finally:
            os.close(fd)

    def test_orphan_temp_removed_and_disabled_policy_preserves_data(self):
        self.add_pr(1)
        temporary = self.cache / 'abandoned.tmp'
        temporary.write_text('raw diff')
        settings = self.config.config_dir / 'config.json'
        settings.write_text(json.dumps({'retention_enabled': False}))
        cleanup(self.config, self.conn, self.gh, now=1769000000)
        self.assertTrue(temporary.exists())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pull_requests').fetchone()[0], 1)
        settings.write_text('{}')
        cleanup(self.config, self.conn, self.gh, now=1769000000)
        self.assertFalse(temporary.exists())

    def test_restart_finishes_orphan_cleanup_after_committed_delete(self):
        self.add_pr(1, diff='orphan.diff')
        with patch('skim.collect.Collector._clean_cache', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                cleanup(self.config, self.conn, self.gh, now=1769000000)
        self.assertTrue((self.cache / 'orphan.diff').exists())
        self.assertEqual(self.conn.execute('SELECT count(*) FROM pull_requests').fetchone()[0], 0)
        cleanup(self.config, self.conn, self.gh, now=1769000001)
        self.assertFalse((self.cache / 'orphan.diff').exists())

    def test_backup_removal_rejects_traversal_and_pending_update(self):
        from skim.lifecycle import remove_backup
        from skim.maintenance import UpdateInProgress
        from skim.releases import UpdateJournal
        backup = self.config.data_dir / 'skim.db.v7.123.bak'
        backup.write_text('backup')
        backup.chmod(0o600)
        with self.assertRaises(ValueError):
            remove_backup(self.config, '../skim.db')
        UpdateJournal(self.config.data_dir).start('a' * 32, 'v99.0.0', 'b' * 64)
        with self.assertRaises(UpdateInProgress):
            remove_backup(self.config, backup.name)
        self.assertTrue(backup.exists())

    def test_worker_start_interval_and_clock_rollback(self):
        from skim.lifecycle import worker
        times = iter([100000.0, 100001.0, 143200.0, 90000.0])
        conn = self.conn
        class Stop:
            count = 0
            observed = []
            def is_set(self):
                return self.count >= 4
            def wait(self, _):
                self.observed.append(conn.execute('SELECT last_run FROM cleanup_state WHERE id=1').fetchone()[0])
                self.count += 1
        result = {'removed': 0, 'deferred': 0, 'remaining': 0, 'processed': 0, 'cursor': 0, 'busy': False}
        stop = Stop()
        with patch('skim.lifecycle.time.time', side_effect=lambda: next(times)), patch('skim.lifecycle.cleanup', return_value=result) as run:
            worker(self.config, stop)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(stop.observed, [100000.0, 100000.0, 143200.0, 90000.0])

    def test_worker_finishes_single_round_of_deferred_batches(self):
        from skim.lifecycle import worker
        class Stop:
            count = 0
            def is_set(self):
                return self.count >= 4
            def wait(self, _):
                self.count += 1
        first = {'removed': 0, 'deferred': 21, 'remaining': 1, 'processed': 20, 'cursor': 20, 'busy': False}
        second = {'removed': 0, 'deferred': 21, 'remaining': 1, 'processed': 20, 'cursor': 19, 'busy': False}
        with patch('skim.lifecycle.time.time', return_value=100000), patch('skim.lifecycle.cleanup', side_effect=[first, second]) as run:
            worker(self.config, Stop())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(self.conn.execute('SELECT requested FROM cleanup_state WHERE id=1').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
