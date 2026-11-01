"""설정·저장소의 보안 경계는 가짜 HTTP와 GitHub 응답으로 검증한다."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import threading
from types import SimpleNamespace
from contextlib import closing
from html.parser import HTMLParser
import json
import os
import unittest
from unittest.mock import patch, Mock

from test_web import WebTests
from skim.cli import main
from skim.collect import GitHub, Deferred
from skim.db import connect, analysis_allowed
from skim.web_settings import POST_PATHS, RepoLookup
from skim.jobs import Runner
from skim.repos import web_repository


class SettingsTests(unittest.TestCase):
    # 기존 HTTP fixture를 공유하되 기존 테스트를 중복 실행하지 않는다.
    setup_http = WebTests.setUp
    request = WebTests.request
    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def post(self, path, **data):
        return self.request(path, 'POST', {'csrf': self.csrf, **data})

    def repo_jobs(self, status=None):
        with closing(connect(self.config)) as conn:
            query = "SELECT * FROM jobs WHERE type='repos'" + (' AND status=?' if status else '') + ' ORDER BY id'
            return conn.execute(query, (status,) if status else ()).fetchall()

    def wait_lookup(self):
        # 실제 워커 대신 같은 Runner를 동기 실행한다. 수집 tick은 가짜로 막는다.
        with closing(connect(self.config)) as conn, closing(connect(self.config)) as state:
            runner = Runner(self.config, conn, state, self.server.repo_lookup)
            runner.scheduler = SimpleNamespace(tick=lambda collector: False, last_ok=False)
            while runner.step():
                pass

    def test_bare_cli_is_web_and_subcommands_preserved(self):
        with patch('skim.web.serve', return_value=0) as serve:
            self.assertEqual(main([]), 0)
            serve.assert_called_once_with(self.config.__class__.load(), 0, True, False)
            serve.reset_mock()
            self.assertEqual(main(['web', '--offline', '--no-browser', '--port', '8181']), 0)
            serve.assert_called_once_with(self.config.__class__.load(), 8181, False, True)
        with patch('skim.doctor.run', return_value=0) as doctor:
            self.assertEqual(main(['doctor']), 0)
            self.assertFalse(doctor.call_args.kwargs['probe'])

    def test_every_mutation_has_auth_csrf_origin_host_and_no_get(self):
        data = {'csrf': self.csrf}
        for path in POST_PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.request(path, 'POST', data, authenticated=False)[0], 403)
                for fields in [[('!Host', None)], [('Host', self.server.authority)], [('!Host', 'evil.test')],
                               [('!Origin', None)], [('!Origin', 'null')], [('Origin', self.server.origin)],
                               [('!Origin', 'http://127.0.0.1:9999')]]:
                    self.assertEqual(self.request(path, 'POST', data, fields)[0], 403)
                for bad in [{}, {'csrf': 'wrong'}, {'csrf': '한글'}, {'csrf': [self.csrf, self.csrf]}]:
                    self.assertEqual(self.request(path, 'POST', bad)[0], 403)
                self.assertEqual(self.request(path)[0], 405)
                for method in ['PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'BOGUS']:
                    self.assertEqual(self.request(path, method, headers=[('Origin', self.server.origin)])[0], 405)
        with closing(connect(self.config)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_header_csrf_and_duplicate_or_cross_session_rejection(self):
        data = {'model': 'gpt-test:1'}
        path = '/settings/model'
        self.assertEqual(self.request(path, 'POST', data, [('X-CSRF-Token', self.csrf)])[0], 303)
        for headers in [[('X-CSRF-Token', self.csrf), ('X-CSRF-Token', self.csrf)],
                        [('X-CSRF-Token', 'bad')]]:
            self.assertEqual(self.request(path, 'POST', {'csrf': self.csrf, **data}, headers)[0], 403)
        session = self.auth.redeem(self.auth.issue())
        other = self.auth.csrf(self.auth.cookie + '=' + session)
        self.assertEqual(self.request(path, 'POST', data, [('X-CSRF-Token', other)])[0], 403)

    def test_all_forms_have_hidden_csrf_and_escaped_values(self):
        class Forms(HTMLParser):
            def __init__(self):
                super().__init__()
                self.forms = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'form':
                    self.forms.append([])
                if tag == 'input' and self.forms:
                    self.forms[-1].append(attrs)
        for path in ['/', '/repos', '/settings']:
            body = self.request(path)[2]
            parser = Forms()
            parser.feed(body)
            for form in parser.forms:
                self.assertTrue(any(i.get('type') == 'hidden' and i.get('name') == 'csrf' and i.get('value') == self.csrf for i in form))
            self.assertIn('href="/repos"', body)
        with closing(connect(self.config)) as conn, conn:
            conn.execute("INSERT INTO jobs(type,target_id,status,last_error) VALUES('repos',0,'failed','<script>bad</script>')")
        self.assertIn('&lt;script&gt;', self.request('/repos')[2])
        self.assertNotIn('<script>bad', self.request('/repos')[2])

    def test_add_validates_input_and_source_before_external_call(self):
        for name in ['a/..', 'a/a..b', 'a/.', 'a/b/c', '-a/b', 'a/b?x', 'a/b;sh', 'a/b c', 'a'*40+'/b', 'a/'+'b'*101]:
            self.assertEqual(self.post('/repos/add', repository=name, source='responsible')[0], 400, name)
        self.assertEqual(self.post('/repos/add', repository='org/repo', source='evil')[0], 400)
        self.assertEqual(self.repo_jobs(), [])
        self.assertEqual(web_repository('a-/._name-'), ('a-', '._name-'))
        self.assertEqual(web_repository('A'*39+'/'+'x'*100), ('A'*39, 'x'*100))

    def test_add_worker_reads_access_and_preserves_policy(self):
        lookup = self.server.repo_lookup
        with patch.object(lookup.gh, 'get', return_value={'full_name': 'demo/checkout'}) as gh:
            self.assertEqual(self.post('/repos/add', repository='demo/checkout', source='managed')[0], 303)
            self.wait_lookup()
            gh.assert_called_once_with('repos/demo/checkout')
        with closing(connect(self.config)) as conn:
            row = conn.execute("SELECT tracked,source,analysis_policy FROM repositories WHERE owner='demo'").fetchone()
            self.assertEqual(tuple(row), (1, 'managed', 'allow'))
        with patch.object(lookup.gh, 'get', return_value={'full_name': 'org/repo'}):
            self.post('/repos/add', repository='org/repo', source='responsible')
            self.wait_lookup()
        with closing(connect(self.config)) as conn:
            row = conn.execute("SELECT * FROM repositories WHERE owner='org'").fetchone()
            self.assertFalse(analysis_allowed(conn, row['id']))
        with patch.object(lookup.gh, 'get', side_effect=ValueError('SECRET')):
            self.post('/repos/add', repository='org/inaccessible', source='owned')
            self.wait_lookup()
        self.assertIn('실패', self.request('/repos')[2])
        self.assertNotIn('SECRET', self.request('/repos')[2])
        with closing(connect(self.config)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM repositories WHERE name='inaccessible'").fetchone())

    def test_remove_reuses_cli_semantics_and_keeps_review_policy(self):
        self.assertEqual(self.post('/repos/remove', repository='demo/checkout')[0], 303)
        with closing(connect(self.config)) as conn:
            self.assertEqual(tuple(conn.execute("SELECT tracked,analysis_policy FROM repositories WHERE owner='demo'").fetchone()), (0, 'allow'))
            self.assertEqual([r[0] for r in conn.execute("SELECT number FROM pull_requests WHERE repo_id=1 ORDER BY number")], [1, 3, 5])
            self.assertEqual(conn.execute("SELECT count(*) FROM pr_sources WHERE source='repository'").fetchone()[0], 0)
            self.assertGreater(conn.execute('SELECT count(*) FROM analyses').fetchone()[0], 0)
        self.assertEqual(self.post('/repos/remove', repository='demo/checkout')[0], 400)

    def test_accessible_list_includes_orgs_collaborators_cache_and_no_auto_add(self):
        lookup = self.server.repo_lookup
        def response(endpoint):
            if endpoint == 'user':
                return {'login': 'me'}
            self.assertEqual(endpoint, 'user/repos?affiliation=owner,organization_member,collaborator&per_page=100&page=1')
            return [{'full_name': 'org/collaborator'}, {'full_name': 'me/owned'}, {'full_name': '<bad>/x'}, {'full_name': 'org/a..b'}]
        with patch.object(lookup.gh, 'get', side_effect=response) as gh:
            self.assertEqual(self.post('/repos/load')[0], 303)
            self.wait_lookup()
            self.assertEqual(lookup.items, ['me/owned', 'org/collaborator'])
            self.post('/repos/load')
            self.wait_lookup()
            self.assertEqual(gh.call_count, 2)
            self.assertEqual(len(self.repo_jobs()), 1)
        body = self.request('/repos')[2]
        self.assertIn('org/collaborator', body)
        with closing(connect(self.config)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM repositories WHERE owner='org'").fetchone())

    def test_listing_limit_failure_and_worker_deduplication(self):
        lookup = self.server.repo_lookup
        with patch.object(lookup.gh, 'get', side_effect=lambda endpoint: {'login': 'me'} if endpoint == 'user' else [{'full_name': 'org/repo'}]*100) as gh:
            self.post('/repos/load')
            self.wait_lookup()
            self.assertEqual(gh.call_count, 11)
            self.assertIn('1,000', self.repo_jobs()[-1]['result'])
        lookup.expires = 0
        with patch.object(lookup.gh, 'get', side_effect=Deferred(1000)):
            self.post('/repos/load')
            self.wait_lookup()
        self.assertEqual(self.repo_jobs()[-1]['status'], 'failed')
        self.assertIn('실패', self.repo_jobs()[-1]['last_error'])
        self.assertEqual(lookup.items, ['org/repo'])
        # 대기 중에는 다른 대상이라도 새 작업을 만들지 않는다.
        lookup.expires = 0
        self.post('/repos/load')
        self.post('/repos/add', repository='org/another', source='owned')
        pending = self.repo_jobs('pending')
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['target'], '접근 가능한 저장소 목록')

    def test_github_uses_fixed_read_only_argv_no_shell(self):
        runner = Mock(return_value=Mock(returncode=0, stdout='{"login":"me"}'))
        gh = GitHub(runner)
        gh.get('user')
        self.assertEqual(runner.call_args.args[0], ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', '--include', 'user'])
        self.assertNotIn('shell', runner.call_args.kwargs)

    def test_offline_load_and_add_never_call_gh(self):
        self.server.offline = True
        with patch('subprocess.run', side_effect=AssertionError('외부 실행 금지')):
            self.assertEqual(self.post('/repos/load')[0], 303)
            self.assertEqual(self.post('/repos/add', repository='org/repo', source='owned')[0], 303)
            self.assertIn('오프라인', self.request('/repos')[2])
        self.assertEqual(self.repo_jobs(), [])

    def test_repo_org_policy_confirmation_precedence_reset_and_default(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('neworg','repo',1)")
        def allowed():
            with closing(connect(self.config)) as conn:
                return analysis_allowed(conn, conn.execute("SELECT id FROM repositories WHERE owner='neworg'").fetchone()[0])
        self.assertFalse(allowed())
        for scope, target in [('repo', 'neworg/repo'), ('org', 'neworg')]:
            self.assertEqual(self.post('/settings/analysis', scope=scope, target=target, policy='allow')[0], 400)
        self.assertEqual(self.post('/settings/analysis', scope='org', target='neworg', policy='allow', confirm='yes')[0], 303)
        self.assertTrue(allowed())
        self.post('/settings/analysis', scope='repo', target='neworg/repo', policy='deny')
        self.assertFalse(allowed())
        self.post('/settings/analysis', scope='repo', target='neworg/repo', policy='reset')
        self.assertTrue(allowed())
        self.post('/settings/analysis', scope='org', target='neworg', policy='reset')
        self.assertFalse(allowed())
        self.post('/settings/analysis', scope='repo', target='neworg/repo', policy='allow', confirm='yes')
        self.post('/settings/analysis', scope='org', target='neworg', policy='deny')
        self.assertTrue(allowed())
        body = self.request('/settings')[2]
        for text in ['PR 코드, diff', '모델 서비스로 전송', 'Codex(모델 서비스)', '저장소의 명시적', '실제 적용: 허용']:
            self.assertIn(text, body)

    def test_invalid_policy_targets_and_extra_fields(self):
        for data in [dict(scope='org', target='../evil', policy='deny'), dict(scope='repo', target='no/repo', policy='deny'),
                     dict(scope='invalid', target='org', policy='deny'), dict(scope='org', target='org', policy='invalid'),
                     dict(scope='org', target='org', policy='allow', confirm='no'),
                     dict(scope='org', target='org', policy='deny', unexpected='x')]:
            self.assertEqual(self.post('/settings/analysis', **data)[0], 400)

    def test_model_validation_atomic_permissions_and_override(self):
        target = self.config.config_dir / 'config.json'
        before = json.loads(target.read_text())
        for value in ['', 'unknown', '-flag', 'a/b', 'a b', '<script>', 'x'*65]:
            self.assertEqual(self.post('/settings/model', model=value)[0], 400, value)
            self.assertEqual(json.loads(target.read_text()), before)
        self.assertEqual(self.post('/settings/model', model='gpt-test_v1:fast')[0], 303)
        settings = json.loads(target.read_text())
        self.assertEqual(settings['codex_model'], 'gpt-test_v1:fast')
        self.assertEqual(settings['poll_interval_seconds'], before['poll_interval_seconds'])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(target.parent.glob('.config-*')))
        with patch.dict(os.environ, SKIM_CODEX_MODEL='override'):
            body = self.request('/settings')[2]
            self.assertIn('gpt-test_v1:fast', body)
            self.assertIn('실제 적용: override', body)
            self.assertIn('SKIM_CODEX_MODEL', body)

    def test_lookup_does_not_block_http_and_reads_do_not_start_it(self):
        from skim.jobs import worker
        lookup = self.server.repo_lookup
        started, release, stop = threading.Event(), threading.Event(), threading.Event()
        def response(endpoint):
            started.set()
            if not release.wait(timeout=3):
                raise AssertionError('워커 해제 누락')
            return {'login': 'me'} if endpoint == 'user' else []
        # 워커의 정기 수집은 가짜로 막고 저장소 조회만 실제 워커 스레드에서 실행한다.
        with patch.object(lookup.gh, 'get', side_effect=response), patch('skim.jobs.CollectionScheduler') as scheduler:
            scheduler.return_value.tick.return_value = False
            for path in ['/', '/repos', '/settings', '/status']:
                self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(self.repo_jobs(), [])
            thread = threading.Thread(target=worker, args=(self.config, stop, lookup), daemon=True)
            thread.start()
            try:
                self.assertEqual(self.post('/repos/load')[0], 303)
                self.assertTrue(started.wait(timeout=3))
                self.assertEqual(self.repo_jobs()[0]['status'], 'running')
                self.assertEqual(self.request('/settings')[0], 200)
                body = self.request('/repos')[2]
                self.assertIn('실행 중', body)
                self.assertIn('data-job-active="1"', body)
                self.post('/repos/add', repository='org/another', source='owned')
                self.assertIn('완료 후 다시', self.request('/repos')[2])
                self.assertEqual(len(self.repo_jobs()), 1)
            finally:
                release.set()
                stop.set()
                thread.join(timeout=3)
        self.assertEqual(self.repo_jobs()[0]['status'], 'completed')

    def test_next_scheduled_analysis_uses_new_model_and_keeps_running_config(self):
        original = replace(self.config, codex_model='old')
        self.post('/settings/model', model='new')
        analyzer = Mock()
        analyzer.return_value.run.return_value = []
        with closing(connect(self.config)) as conn, closing(connect(self.config)) as state:
            runner = Runner(original, conn, state, analyzer=analyzer)
            runner.scheduler = SimpleNamespace(tick=lambda collector: True, last_ok=True)
            self.assertTrue(runner.step())
        self.assertEqual(analyzer.call_args.args[1].codex_model, 'new')
        self.assertEqual(original.codex_model, 'old')
        with closing(connect(self.config)) as conn:
            self.assertEqual(conn.execute("SELECT model FROM jobs WHERE type='analyze'").fetchone()[0], 'new')

    def test_browser_failure_shows_terminal_fallback(self):
        from skim.web import open_entry
        for outcome in [False, OSError('사용 불가')]:
            with patch('webbrowser.open', side_effect=outcome if isinstance(outcome, Exception) else None, return_value=False), redirect_stdout(io.StringIO()) as output:
                open_entry('http://127.0.0.1:8123/entry#fixture')
                self.assertIn('위 URL', output.getvalue())

    def test_rate_limit_rejects_excess_and_recovers(self):
        for _ in range(30):
            self.assertEqual(self.post('/settings/model', model='test')[0], 303)
        self.assertEqual(self.post('/settings/model', model='test')[0], 429)
        self.now += 60
        self.assertEqual(self.post('/settings/model', model='test')[0], 303)


del WebTests
