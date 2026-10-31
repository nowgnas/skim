"""외부 CLI와 네트워크 없이 초기 골격을 검증한다."""
import contextlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
from skim.cli import main
from skim.config import Config
from skim.db import analysis_allowed, connect, migrate


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'SKIM_HOME': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(args)
        return code, out.getvalue()

    def test_repository_and_policy_lifecycle(self):
        self.assertEqual(self.cli('repo', 'add', 'Org/Repo', '--source', 'managed')[0], 0)
        config = Config.load()
        with contextlib.closing(connect(config)) as conn:
            self.assertFalse(analysis_allowed(conn, 1))
        self.assertEqual(self.cli('analysis', 'allow', '--org', 'org')[0], 0)
        self.assertIn('inherit\t1', self.cli('repo', 'list')[1])
        self.assertEqual(self.cli('analysis', 'deny', 'org/repo')[0], 0)
        self.assertIn('deny\t0', self.cli('repo', 'list')[1])
        self.cli('repo', 'add', 'ORG/REPO')
        self.assertIn('deny\t0', self.cli('repo', 'list')[1])
        self.cli('analysis', 'reset', 'Org/Repo')
        self.assertIn('inherit\t1', self.cli('repo', 'list')[1])
        self.cli('analysis', 'deny', '--org', 'org')
        self.assertIn('inherit\t0', self.cli('repo', 'list')[1])
        self.cli('analysis', 'allow', 'Org/Repo')
        self.assertIn('allow\t1', self.cli('repo', 'list')[1])
        self.assertEqual(self.cli('repo', 'remove', 'org/repo')[0], 0)
        self.assertNotIn('Repo', self.cli('repo', 'list')[1])
        self.assertEqual(self.cli('analysis', 'allow', 'org/missing')[0], 1)

    def test_schema_constraints_and_permissions(self):
        config = Config.load()
        with contextlib.closing(connect(config)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            conn.execute("INSERT INTO jobs(type,target_id,input_hash) VALUES('analyze',1,'a')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO jobs(type,target_id,input_hash) VALUES('analyze',1,'a')")
            conn.execute("UPDATE jobs SET status='completed'")
            conn.execute("INSERT INTO jobs(type,target_id,input_hash) VALUES('analyze',1,'a')")
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO pull_requests(repo_id,number) VALUES(999,1)')
            for table, fields in {'pull_requests': {'head_branch','state'}, 'snapshots': {'input_hash'},
                                  'analyses': {'input_hash'}, 'jobs': {'id'}}.items():
                self.assertTrue(fields <= {r[1] for r in conn.execute(f'PRAGMA table_info({table})')})
        for path in Path(self.temp.name).rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)

    def test_backup_and_rollback(self):
        config = Config.load()
        conn = sqlite3.connect(config.db_path)
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE existing(value TEXT)')
        conn.commit()
        with patch('skim.db.MIGRATIONS', ('CREATE TABLE partial(x); INVALID SQL;',)):
            with self.assertRaises(sqlite3.Error):
                migrate(conn, config.db_path)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
        backup = next(config.data_dir.glob('*.bak'))
        with contextlib.closing(sqlite3.connect(backup)) as saved:
            self.assertIsNotNone(saved.execute("SELECT name FROM sqlite_master WHERE name='existing'").fetchone())

    def test_wrapper_symlink_and_stubs(self):
        link = Path(self.temp.name) / 'skim'
        link.symlink_to(ROOT / 'bin/skim')
        result = subprocess.run([str(link), 'repo', 'add', 'a/b'], capture_output=True, text=True, cwd='/tmp')
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('web', 'doctor'):
            self.assertEqual(self.cli(command)[0], 2)

    def test_review_request_survives_untracking(self):
        config = Config.load()
        with contextlib.closing(connect(config)) as conn, conn:
            conn.execute("INSERT INTO repositories(owner,name) VALUES('org','outside')")
            repo_id = conn.execute('SELECT id FROM repositories').fetchone()[0]
            self.assertEqual(conn.execute('SELECT tracked FROM repositories').fetchone()[0], 0)
            for number in (1, 2):
                conn.execute('INSERT INTO pull_requests(repo_id,number) VALUES(?,?)', (repo_id, number))
            conn.execute("INSERT INTO pr_sources VALUES(1,'review_request')")
            conn.execute("INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash) "
                         "VALUES(1,'a','b','d','i')")
            conn.execute("INSERT INTO analyses(snapshot_id,input_hash,provider,model,cli_version,"
                         "prompt_version,schema_version,status) VALUES(1,'i','fake','fake','1','1','1','completed')")
        self.assertNotIn('outside', self.cli('repo', 'list')[1])
        self.assertEqual(self.cli('analysis', 'allow', '--org', 'org')[0], 0)
        with contextlib.closing(connect(config)) as conn:
            self.assertTrue(analysis_allowed(conn, repo_id))
        self.cli('repo', 'add', 'org/outside')
        with contextlib.closing(connect(config)) as conn, conn:
            conn.executemany("INSERT INTO pr_sources VALUES(?,'repository')", [(1,), (2,)])
        self.assertIn('outside', self.cli('repo', 'list')[1])
        self.assertEqual(self.cli('repo', 'remove', 'org/outside')[0], 0)
        self.assertNotIn('outside', self.cli('repo', 'list')[1])
        with contextlib.closing(connect(config)) as conn:
            self.assertEqual([tuple(r) for r in conn.execute('SELECT pr_id,source FROM pr_sources')],
                             [(1, 'review_request')])
            self.assertEqual(conn.execute('SELECT count(*) FROM pull_requests').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT count(*) FROM analyses').fetchone()[0], 1)
            self.assertTrue(analysis_allowed(conn, repo_id))
        self.assertEqual(self.cli('repo', 'remove', 'org/outside')[0], 1)
        self.cli('repo', 'add', 'ORG/OUTSIDE')
        with contextlib.closing(connect(config)) as conn:
            self.assertEqual(conn.execute('SELECT id FROM repositories').fetchone()[0], repo_id)

    def test_settings_validation_and_shared_home_permissions(self):
        root = Path(self.temp.name)
        root.chmod(0o755)
        config = Config.load()
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)
        path = config.config_dir / 'config.json'
        path.write_text('{"poll_interval_seconds": 60, "retention_days": 7}')
        config = Config.load()
        self.assertEqual((config.poll_interval_seconds, config.retention_days), (60, 7))
        for name in ('poll_interval_seconds', 'retention_days'):
            for value in ('true', '0', '-1', '1.5', '"30"', 'null'):
                path.write_text('{"' + name + '":' + value + '}')
                with self.assertRaises(ValueError):
                    Config.load()

    def test_policy_notice_and_owner_validation(self):
        self.cli('repo', 'add', 'a/b')
        code, output = self.cli('analysis', 'allow', 'a/b')
        self.assertEqual(code, 0)
        self.assertLess(output.index('전송될 수 있습니다'), output.index('정책을 변경'))
        self.assertEqual(self.cli('analysis', 'allow', '--org', 'bad-')[0], 1)

    def test_default_paths(self):
        with patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.home', return_value=Path(self.temp.name)):
            config = Config.load()
            self.assertEqual(config.config_dir, Path(self.temp.name) / '.config/skim')
            self.assertEqual(config.data_dir, Path(self.temp.name) / '.local/share/skim')


if __name__ == '__main__':
    unittest.main()
