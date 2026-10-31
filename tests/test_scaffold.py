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
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
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
        with sqlite3.connect(backup) as saved:
            self.assertIsNotNone(saved.execute("SELECT name FROM sqlite_master WHERE name='existing'").fetchone())

    def test_wrapper_symlink_and_stubs(self):
        link = Path(self.temp.name) / 'skim'
        link.symlink_to(ROOT / 'bin/skim')
        result = subprocess.run([str(link), 'repo', 'add', 'a/b'], capture_output=True, text=True, cwd='/tmp')
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('collect', 'analyze', 'web', 'doctor'):
            self.assertEqual(self.cli(command)[0], 2)

    def test_default_paths(self):
        with patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.home', return_value=Path(self.temp.name)):
            config = Config.load()
            self.assertEqual(config.config_dir, Path(self.temp.name) / '.config/skim')
            self.assertEqual(config.data_dir, Path(self.temp.name) / '.local/share/skim')


if __name__ == '__main__':
    unittest.main()
