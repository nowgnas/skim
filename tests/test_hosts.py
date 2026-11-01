"""GHE 경계는 임시 DB와 목 실행기로만 검증한다."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim import db, doctor
from skim.config import Config
from skim.hosts import hostname, authenticated
from skim.collect import Collector
from skim.web import github
import test_collect
import test_web_settings

HOST = 'github.corp.example'


class HostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, SKIM_HOME=temp.name)
        env.start()
        self.addCleanup(env.stop)
        self.config = Config.load()

    def test_validation(self):
        self.assertEqual(hostname('GitHub.Corp.Example'), HOST)
        self.assertEqual(hostname('intranet'), 'intranet')
        self.assertEqual(len(hostname('a' * 63 + '.' + 'b' * 63 + '.' + 'c' * 63 + '.' + 'd' * 61)), 253)
        for value in ['', 'https://github.com', '//github.com', 'github.com/', 'github.com:443',
                      'user@github.com', 'github.com?x', 'github.com#x', 'github.com\\x',
                      ' github.com', 'github.com ', 'git hub.com', 'github.com\n',
                      '.github.com', 'github.com.', 'git..com', '-git.com', 'git-.com',
                      'git._com', 'git.한글', 'git\x00.com', 'K.com', 'a' * 64, 'a.' * 127 + 'a']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                hostname(value)

    def test_v5_migration_preserves_entire_reference_chain(self):
        with patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:5]):
            with closing(db.connect(self.config)) as conn, conn:
                conn.execute("INSERT INTO repositories(id,owner,name) VALUES(8,'org','repo')")
                conn.execute('INSERT INTO pull_requests(id,repo_id,number) VALUES(9,8,1)')
                conn.execute("INSERT INTO pr_sources VALUES(9,'repository')")
                conn.execute("INSERT INTO snapshots(id,pr_id,base_sha,head_sha,diff_hash,input_hash) VALUES(10,9,'b','h','d','i')")
                conn.execute("INSERT INTO changes(id,snapshot_id,file_path,kind) VALUES(11,10,'a','file')")
                conn.execute("INSERT INTO analyses(id,snapshot_id,input_hash,provider,model,cli_version,prompt_version,schema_version,status) VALUES(12,10,'i','fake','fake','1','1','1','completed')")
                conn.execute("INSERT INTO analysis_items(analysis_id,change_id,importance,reason) VALUES(12,11,'low','ok')")
        with closing(db.connect(self.config)) as conn, conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], len(db.MIGRATIONS))
            self.assertEqual(conn.execute('SELECT host FROM repositories WHERE id=8').fetchone()[0], 'github.com')
            self.assertEqual(conn.execute('SELECT reason FROM analysis_items').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            conn.execute('INSERT INTO repositories(host,owner,name) VALUES(?,?,?)', (HOST, 'org', 'repo'))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO repositories(owner,name) VALUES('ORG','REPO')")
            conn.execute('DELETE FROM repositories WHERE id=8')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM analysis_items').fetchone()[0], 0)

    def test_v6_failure_rolls_back_and_restores_foreign_keys(self):
        with patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:5]):
            with closing(db.connect(self.config)) as conn, conn:
                conn.execute("INSERT INTO repositories(id,owner,name) VALUES(8,'org','repo')")
                conn.execute('INSERT INTO pull_requests(id,repo_id,number) VALUES(9,8,1)')
        failures = [
            db.MIGRATIONS[5].replace('DROP TABLE repositories;', 'SELECT missing_column FROM repositories;'),
            db.MIGRATIONS[5] + "UPDATE pull_requests SET repo_id=999 WHERE id=9;",
        ]
        with closing(sqlite3.connect(self.config.db_path)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            for migration in failures:
                with self.subTest(migration=migration), patch.object(db, 'MIGRATIONS', db.MIGRATIONS[:5] + (migration,)):
                    with self.assertRaises((sqlite3.Error, ValueError)):
                        db.migrate(conn, self.config.db_path)
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 5)
                    self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                    self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='repositories_v6'").fetchone())
                    self.assertEqual(conn.execute('SELECT id,owner,name FROM repositories').fetchall(), [(8, 'org', 'repo')])
                    self.assertEqual(conn.execute('SELECT id,repo_id,number FROM pull_requests').fetchall(), [(9, 8, 1)])

    def test_collection_is_scoped_read_only_and_url_and_consent(self):
        fake = test_collect.FakeGH()
        calls = []
        def runner(command, **kwargs):
            calls.append(command[:])
            # Reuse existing payload fixture, while independently asserting real host/method.
            self.assertIn(command[3], ['github.com', HOST])
            self.assertEqual(command[4:6], ['--method', 'GET'])
            fixture_command = command[:]
            fixture_command[3] = 'github.com'
            return fake(fixture_command, **kwargs)
        with closing(db.connect(self.config)) as conn, conn:
            conn.execute('INSERT INTO github_hosts VALUES(?)', (HOST,))
            for host in ['github.com', HOST]:
                conn.execute("INSERT INTO repositories(host,owner,name,tracked) VALUES(?,'org','repo',1)", (host,))
            conn.execute("INSERT INTO organization_policies VALUES('org','allow')")
            Collector(conn, self.config, runner).collect()
            self.assertEqual({c[3] for c in calls}, {'github.com', HOST})
            rows = conn.execute('SELECT r.*,p.number,p.url FROM repositories r JOIN pull_requests p ON p.repo_id=r.id').fetchall()
            self.assertGreaterEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row['url'], github(dict(row)))
                self.assertEqual(row['url'], f"https://{row['host']}/{row['owner']}/{row['name']}/pull/{row['number']}")
                self.assertEqual(db.analysis_allowed(conn, row['id']), row['host'] == 'github.com')
            conn.execute("INSERT INTO host_organization_policies VALUES(?,'org','allow')", (HOST,))
            self.assertTrue(all(db.analysis_allowed(conn, row['id']) for row in rows))

    def test_failed_host_does_not_stop_other_host_collection(self):
        from skim.collect import CollectionError
        fake = test_collect.FakeGH()
        calls = []
        def runner(cmd, **kwargs):
            calls.append(cmd[3])
            if cmd[3] == HOST:
                raise OSError('mock failure')
            return fake(cmd, **kwargs)
        with closing(db.connect(self.config)) as conn, conn:
            conn.execute('INSERT INTO github_hosts VALUES(?)', (HOST,))
            conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('org','repo',1)")
            with self.assertRaisesRegex(CollectionError, HOST):
                Collector(conn, self.config, runner).collect()
            self.assertIn('github.com', calls)
            self.assertIn(HOST, calls)
            self.assertGreater(conn.execute("SELECT count(*) FROM pull_requests p JOIN repositories r ON r.id=p.repo_id WHERE r.host='github.com'").fetchone()[0], 0)

    def test_cli_host_scopes_repository_and_organization_policy(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from skim.cli import main
        with closing(db.connect(self.config)) as conn, conn:
            conn.execute('INSERT INTO github_hosts VALUES(?)', (HOST,))
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            self.assertEqual(main(['repo', 'add', 'org/repo']), 0)
            self.assertEqual(main(['repo', 'add', 'org/repo', '--host', HOST.upper()]), 0)
            self.assertEqual(main(['analysis', 'allow', '--org', 'org', '--host', HOST]), 0)
            with closing(db.connect(self.config)) as conn:
                rows = conn.execute('SELECT id,host FROM repositories ORDER BY host').fetchall()
                for row in rows:
                    self.assertEqual(db.analysis_allowed(conn, row['id']), row['host'] == HOST)
            self.assertEqual(main(['analysis', 'deny', 'org/repo', '--host', HOST]), 0)
            self.assertEqual(main(['repo', 'list']), 0)
            self.assertIn('호스트\t저장소', output.getvalue())
            self.assertIn(HOST + '\torg/repo', output.getvalue())
            self.assertEqual(main(['repo', 'remove', 'org/repo', '--host', HOST]), 0)
            self.assertEqual(main(['analysis', 'reset', '--org', 'org', '--host', HOST]), 0)
            self.assertEqual(main(['analysis', 'reset', 'org/repo', '--host', HOST]), 0)
            for command in [['repo', 'add', 'org/other'], ['repo', 'remove', 'org/repo'], ['analysis', 'allow', 'org/repo']]:
                self.assertEqual(main(command + ['--host', 'unregistered.example']), 1)
            for host in ['bad:443', 'K.com', 'bad\x00.com']:
                with self.assertRaises(SystemExit):
                    main(['repo', 'add', 'org/repo', '--host', host])
        with closing(db.connect(self.config)) as conn:
            self.assertEqual([tuple(r) for r in conn.execute('SELECT host,tracked,analysis_policy FROM repositories ORDER BY host')],
                             [('github.com', 1, 'inherit'), (HOST, 0, 'inherit')])
            self.assertEqual(conn.execute('SELECT count(*) FROM host_organization_policies').fetchone()[0], 0)

    def test_doctor_checks_all_hosts_and_hides_raw_output(self):
        with closing(db.connect(self.config)) as conn, conn:
            conn.execute('INSERT INTO github_hosts VALUES(?)', (HOST,))
        runner = Mock(side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, int(cmd[-1] == HOST), 'SECRET', 'TOKEN'))
        lines = []
        doctor.run(self.config, runner=runner, which=lambda name: '/fake',
                   adapter=Mock(model='fake', unknown_enabled=[]), out=lines.append)
        self.assertEqual([c.args[0] for c in runner.call_args_list],
                         [['gh','auth','status','--hostname', h] for h in ['github.com', HOST]])
        self.assertIn('실패: gh ' + HOST, '\n'.join(lines))
        self.assertNotIn('SECRET', '\n'.join(lines))
        self.assertNotIn('TOKEN', '\n'.join(lines))
        self.assertFalse(authenticated(HOST, Mock(side_effect=OSError)))


class HostWebTests(unittest.TestCase):
    setUp = test_web_settings.SettingsTests.setUp
    setup_http = test_web_settings.SettingsTests.setup_http
    request = test_web_settings.SettingsTests.request
    post = test_web_settings.SettingsTests.post
    wait_lookup = test_web_settings.SettingsTests.wait_lookup

    def test_host_lifecycle_and_auth_status(self):
        with patch('skim.web_settings.authenticated', side_effect=lambda h: h == 'git.enterprise.example') as status:
            self.assertEqual(self.post('/hosts/add', host='GIT.ENTERPRISE.EXAMPLE')[0], 303)
            self.assertEqual(self.post('/hosts/check', host='github.com')[0], 303)
            status.reset_mock()
            body = self.request('/repos')[2]
            host_row = body.split('<h3>git.enterprise.example</h3>', 1)[1].split('</article>', 1)[0]
            self.assertIn('<span class="badge">로그인됨</span>', host_row)
            github_row = body.split('<h3>github.com</h3>', 1)[1].split('</article>', 1)[0]
            self.assertIn('<span class="badge">로그인 안 됨</span>', github_row)
            self.request('/repos')
            status.assert_not_called()
            self.assertIn('마지막 확인:', body)
            self.assertEqual(self.post('/hosts/check', host='git.enterprise.example')[0], 303)
            status.assert_called_once_with('git.enterprise.example')
        self.assertEqual(self.post('/hosts/remove', host='github.com')[0], 400)
        self.assertEqual(self.request('/hosts/remove', 'POST', {'host': 'git.enterprise.example'})[0], 403)
        self.assertEqual(self.post('/hosts/remove', host='git.enterprise.example')[0], 303)
        with closing(db.connect(self.config)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM github_hosts WHERE host='git.enterprise.example'").fetchone())
        for host in ['https://bad', 'bad:443', 'bad/name', 'bad host']:
            self.assertEqual(self.post('/hosts/add', host=host)[0], 400)

    def test_lookup_and_add_host_through_worker_and_policy_isolation(self):
        lookup = self.server.repo_lookup
        with patch.object(lookup.gh, 'runner', return_value=subprocess.CompletedProcess([], 0, '{"login":"me"}', '')) as runner:
            # List payload differs from user payload, so route by endpoint.
            runner.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '{"login":"me"}' if cmd[7] == 'user' else '[]', '')
            self.assertEqual(self.post('/repos/load', host=HOST)[0], 303)
            self.wait_lookup()
            self.assertTrue(all(c.args[0][3] == HOST for c in runner.call_args_list))
            self.assertEqual(lookup.host, HOST)
        with patch.object(lookup.gh, 'get', return_value={'full_name': 'demo/checkout'}):
            self.assertEqual(self.post('/repos/add', host=HOST, repository='demo/checkout', source='responsible')[0], 303)
            self.wait_lookup()
        with closing(db.connect(self.config)) as conn:
            row = conn.execute("SELECT id FROM repositories WHERE host=? AND owner='demo' AND name='checkout'", (HOST,)).fetchone()
            self.assertFalse(db.analysis_allowed(conn, row[0]))
        self.assertEqual(self.post('/settings/analysis', host=HOST, scope='org', target='demo', policy='allow', confirm='yes')[0], 303)
        with closing(db.connect(self.config)) as conn:
            self.assertTrue(db.analysis_allowed(conn, row[0]))
        self.assertEqual(self.post('/settings/analysis', host=HOST, scope='repo', target='demo/checkout', policy='deny')[0], 303)
        with closing(db.connect(self.config)) as conn:
            self.assertFalse(db.analysis_allowed(conn, row[0]))
            self.assertTrue(db.analysis_allowed(conn, 1))

    def test_removal_and_manual_analysis_use_host_identity(self):
        with closing(db.connect(self.config)) as conn, conn:
            repo_id = conn.execute("INSERT INTO repositories(host,owner,name,tracked,analysis_policy) VALUES(?,'demo','checkout',1,'allow')", (HOST,)).lastrowid
            pr_id = conn.execute("INSERT INTO pull_requests(repo_id,number,base_sha,head_sha) VALUES(?,1,'base','head')", (repo_id,)).lastrowid
            conn.execute("INSERT INTO pr_sources VALUES(?,'repository')", (pr_id,))
            conn.execute("INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash) VALUES(?,'base','head','d','i')", (pr_id,))
        self.assertEqual(self.post('/jobs/analyze', host=HOST, repository='demo/checkout', number='1')[0], 303)
        with closing(db.connect(self.config)) as conn:
            self.assertEqual(conn.execute("SELECT target_id FROM jobs WHERE type='analyze'").fetchone()[0], pr_id)
        self.assertEqual(self.post('/repos/remove', host=HOST, repository='demo/checkout')[0], 303)
        with closing(db.connect(self.config)) as conn:
            self.assertEqual(conn.execute('SELECT tracked FROM repositories WHERE id=?', (repo_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT tracked FROM repositories WHERE id=1').fetchone()[0], 1)
        self.assertEqual(self.post('/hosts/remove', host=HOST)[0], 303)
        with closing(db.connect(self.config)) as conn:
            self.assertEqual(conn.execute('SELECT enabled FROM repositories WHERE id=?', (repo_id,)).fetchone()[0], 0)

    def test_host_add_security_and_unknown_host_rejected(self):
        for data, headers in [({'host': 'new.example'}, None),
                              ({'csrf': self.csrf, 'host': 'new.example'}, [('!Origin', 'http://evil.example')])]:
            self.assertEqual(self.request('/hosts/add', 'POST', data, headers)[0], 403)
        with closing(db.connect(self.config)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM github_hosts WHERE host='new.example'").fetchone())
        self.assertEqual(self.post('/repos/load', host='unknown.example')[0], 400)
        self.assertEqual(self.post('/jobs/analyze', host='unknown.example', repository='demo/checkout', number='1')[0], 400)
        with closing(db.connect(self.config)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_deleted_host_pr_hidden_and_policies_disabled(self):
        from skim.web import load_prs
        with closing(db.connect(self.config)) as conn, conn:
            repo = conn.execute("INSERT INTO repositories(host,owner,name,tracked,analysis_policy) VALUES(?,'org','private',1,'allow')", (HOST,)).lastrowid
            pr = conn.execute("INSERT INTO pull_requests(repo_id,number,title) VALUES(?,42,'deleted host PR')", (repo,)).lastrowid
            conn.execute("INSERT INTO pr_sources VALUES(?,'review_request')", (pr,))
            conn.execute("INSERT INTO host_organization_policies VALUES(?,'org','allow')", (HOST,))
            self.assertIn(pr, [p['id'] for p in load_prs(conn, self.config)])
        self.assertEqual(self.post('/hosts/remove', host=HOST)[0], 303)
        with closing(db.connect(self.config)) as conn:
            self.assertNotIn(pr, [p['id'] for p in load_prs(conn, self.config)])
            self.assertEqual(tuple(conn.execute('SELECT tracked,enabled,analysis_policy FROM repositories WHERE id=?', (repo,)).fetchone()), (0, 0, 'deny'))
            self.assertFalse(db.analysis_allowed(conn, repo))
            self.assertEqual(conn.execute('SELECT count(*) FROM host_organization_policies WHERE host=?', (HOST,)).fetchone()[0], 0)
            self.assertIsNotNone(conn.execute('SELECT 1 FROM pull_requests WHERE id=?', (pr,)).fetchone())
        self.assertNotIn('deleted host PR', self.request('/')[2])
        self.assertIn('gh auth login --hostname &lt;host&gt;', self.request('/status')[2])
