"""실제 소켓·네트워크·모델 없이 HTTP와 데모 fixture를 검증한다."""
import io
from dataclasses import replace
from contextlib import closing, redirect_stdout
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'scripts'))
from skim.config import Config
from skim.db import connect
from skim.web import Auth, Handler, CSP, Server, badges, load_prs, worker, read_diff, render_diff, serve
from demo_seed import seed


class FakeConnection:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, mode, *args):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class WebTests(unittest.TestCase):
    def setUp(self):
        auth_status = patch('skim.web_settings.authenticated', return_value=False)
        auth_status.start()
        self.addCleanup(auth_status.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, SKIM_HOME=self.temp.name)
        env.start()
        self.addCleanup(env.stop)
        # 실행 환경의 모델 지정이 fixture 설정을 덮어쓰지 않게 격리한다.
        os.environ.pop('SKIM_CODEX_MODEL', None)
        self.config = replace(Config.load(), sessio_index_path=Path(self.temp.name) / 'sessions.jsonl')
        self.now = 10
        self.auth = Auth(clock=lambda: self.now)
        self.server = SimpleNamespace(config=self.config, auth=self.auth,
                                      authority='127.0.0.1:8123', origin='http://127.0.0.1:8123')
        # fixture조차 실제 외부 실행을 호출하면 테스트가 즉시 실패한다.
        with patch('subprocess.run', side_effect=AssertionError('외부 실행 금지')), patch('socket.socket', side_effect=AssertionError('소켓 금지')):
            with redirect_stdout(io.StringIO()):
                seed(self.config)
        session = self.auth.redeem(self.auth.issue())
        self.cookie = self.auth.cookie + '=' + session
        self.csrf = self.auth.csrf(self.cookie)

    def request(self, path='/', method='GET', data=None, headers=None, authenticated=True):
        body = urlencode(data or {}).encode()
        fields = [('Host', self.server.authority)]
        if authenticated:
            fields.append(('Cookie', self.cookie))
        if method == 'POST':
            fields += [('Origin', self.server.origin), ('Content-Type', 'application/x-www-form-urlencoded'), ('Content-Length', str(len(body)))]
        for key, value in headers or []:
            if key.startswith('!'):
                fields = [(k, v) for k, v in fields if k.lower() != key[1:].lower()]
                if value is not None:
                    fields.append((key[1:], value))
            else:
                fields.append((key, value))
        raw = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in fields) + '\r\n'
        conn = FakeConnection(raw.encode() + body)
        Handler(conn, ('127.0.0.1', 12345), self.server)
        head, content = conn.output.getvalue().split(b'\r\n\r\n', 1)
        lines = head.decode().split('\r\n')
        return int(lines[0].split()[1]), dict(line.split(': ', 1) for line in lines[1:]), content.decode()

    def test_page_titles_and_separator_free_markup(self):
        # PR 원문은 보존하고, 화면에서 추가하는 구분 문자를 검사한다.
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET title='수집 완료, 분석 대기' WHERE id=4")
        self.server.offline = True
        for path, title in [('/', '대시보드'), ('/repos', '저장소'),
                            ('/settings', '설정'), ('/status', '상태'),
                            ('/pr/1', '결제 인증 경계 보완')]:
            with self.subTest(path=path):
                body = self.request(path)[2]
                self.assertIn('<title>' + title + ' | skim</title>', body)
                active = '/' if path.startswith('/pr/') else path
                self.assertIn(f'href="{active}" aria-current="page"', body)
                self.assertIn('<main id="main">', body)
                self.assertNotIn('·', body)
                self.assertNotIn('—', body)
                if path != '/status':
                    self.assertIn('class="meta"', body)
        with closing(connect(self.config)) as conn, conn:
            conn.execute('UPDATE pull_requests SET title=? WHERE id=1', ('<PR & 제목>',))
        self.assertIn('<title>&lt;PR &amp; 제목&gt; | skim</title>', self.request('/pr/1')[2])

    def test_search_state_survives_filters_and_other_lists(self):
        body = self.request('/?pr_q=payment&importance=high')[2]
        filter_form = body.split('<form class="filters" method="get">', 1)[1].split('</form>', 1)[0]
        self.assertIn('type="hidden" name="pr_q" value="payment"', filter_form)
        self.assertIn('name="importance"', filter_form)
        search_form = body.split('<form class="filters" method="get">', 2)[2].split('</form>', 1)[0]
        self.assertIn('type="hidden" name="importance" value="high"', search_form)
        body = self.request('/repos?tracked_q=missing&available_q=other')[2]
        tracked_form = body.split('<form class="filters" method="get">', 1)[1].split('</form>', 1)[0]
        self.assertIn('type="hidden" name="available_q" value="other"', tracked_form)
        self.assertIn('name="tracked_q" value="missing"', tracked_form)
        self.assertIn('검색 결과가 없습니다', body)
        body = self.request('/settings?policy_q=missing&org_q=other')[2]
        policy_form = body.split('<form class="filters" method="get">', 1)[1].split('</form>', 1)[0]
        self.assertIn('type="hidden" name="org_q" value="other"', policy_form)
        self.assertIn('name="policy_q" value="missing"', policy_form)
        body = self.request('/pr/1?unit_q=payment&order=original')[2]
        self.assertIn('unit_q=payment', body)
        self.assertIn('name="order" value="original"', body)

    def test_repository_page_navigation_preserves_other_list_state(self):
        with closing(connect(self.config)) as conn, conn:
            conn.executemany("INSERT INTO repositories(owner,name,tracked) VALUES('paging',?,1)",
                             [(f'repo-{i:02}',) for i in range(25)])
        body = self.request('/repos?tracked_q=paging&available_q=other&available_page=2')[2]
        self.assertIn('paging/repo-00', body)
        self.assertIn('paging/repo-19', body)
        self.assertNotIn('paging/repo-20', body)
        self.assertIn('available_q=other&amp;available_page=2&amp;tracked_page=2', body)
        body = self.request('/repos?tracked_q=paging&available_q=other&available_page=2&tracked_page=2')[2]
        self.assertNotIn('paging/repo-19', body)
        self.assertIn('paging/repo-20', body)
        self.assertIn('paging/repo-24', body)
        self.assertIn('type="hidden" name="available_page" value="2"', body)

    def test_null_pr_title_does_not_break_listing(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute('UPDATE pull_requests SET title=NULL WHERE id=1')
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/?pr_q=checkout')[0], 200)

    def test_detail_analysis_action_precedes_change_units(self):
        body = self.request('/pr/1')[2]
        self.assertLess(body.index('PR 분석 요청'), body.index('aria-label="변경 보기"'))
        self.assertNotIn('aria-label="검토하기:', self.request('/')[2])

    def test_analysis_time_shared_once_when_identical(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE analyses SET created_at='same-analysis'")
        body = self.request()[2]
        cards = body.split('<article class="issue-row">')[1:]
        if '<span>분석 same-analysis</span>' in body.split('<h2><a href="/pr/')[0]:
            self.assertEqual(body.count('<span>분석 same-analysis</span>'), 1)
            for card in cards:
                self.assertNotIn('<span>분석 ', card)
        else:
            self.assertTrue(any('<span>분석 대기</span>' in card for card in cards))

    def test_card_heading_precedes_meta(self):
        card = self.request()[2].split('<article class="issue-row">')[1]
        self.assertLess(card.index('<h2>'), card.index('class="meta"'))

    def test_collection_time_shared_only_for_visible_matching_cards(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE snapshots SET collected_at='shared-time'")
        body = self.request()[2]
        self.assertEqual(body.count('<span>수집 shared-time</span>'), 1)
        self.assertLess(body.index('<span>수집 shared-time</span>'), body.index('<h2><a href="/pr/'))
        for card in body.split('<article class="issue-row">')[1:]:
            self.assertNotIn('<span>수집 ', card)
        self.assertIn('<span>분석 ', body)
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE snapshots SET collected_at='other-time' WHERE pr_id=1")
        body = self.request()[2]
        self.assertIn('<span>수집 other-time</span>', body)
        self.assertGreater(body.count('<span>수집 shared-time</span>'), 1)
        filtered = self.request('/?importance=low')[2]
        self.assertEqual(filtered.count('<span>수집 shared-time</span>'), 1)
        self.assertNotIn('other-time', filtered)
        empty = self.request('/?repo=missing')[2]
        self.assertNotIn('<span>수집 ', empty)
        with closing(connect(self.config)) as conn, conn:
            conn.execute('DELETE FROM analyses')
            conn.execute('DELETE FROM snapshots WHERE pr_id=1')
        body = self.request()[2]
        self.assertIn('<span>수집 대기</span>', body)
        self.assertIn('<span>수집 shared-time</span>', body)

    def test_token_ttl_one_use_and_restart(self):
        token = self.auth.issue()
        self.assertIsNone(self.auth.redeem(token + 'bad'))
        self.assertIsNotNone(self.auth.redeem(token))
        self.assertIsNone(self.auth.redeem(token))
        token = self.auth.issue()
        self.now += 60
        self.assertIsNone(self.auth.redeem(token))
        self.assertIsNone(Auth().csrf(self.cookie))
        self.assertIsNone(self.auth.csrf(self.auth.cookie + '=한글'))

    def test_sessio_detail_uses_fixture_and_current_user(self):
        self.config.sessio_index_path.write_text((ROOT / 'tests/fixtures/sessio.jsonl').read_text())
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET author='Me',head_branch='feature/payment' WHERE id=1")
        with patch('skim.sessio.GitHub.get', return_value={'login': 'me'}) as gh:
            body = self.request('/pr/1')[2]
            self.assertIn('Sessio: abc-123', body)
            self.assertIn('http://127.0.0.1:8765/#/s/codex%3Aabc-123', body)
            gh.assert_called_once_with('user')
            self.assertNotIn('Sessio', self.request('/')[2])
        with patch('skim.sessio.GitHub.get', return_value={'login': 'someone-else'}):
            self.assertNotIn('Sessio', self.request('/pr/1')[2])
        self.server.offline = True
        with patch('skim.sessio.GitHub.get', side_effect=AssertionError('외부 실행 금지')):
            self.assertNotIn('Sessio', self.request('/pr/1')[2])

    def test_entry_has_no_data_and_session_cookie(self):
        code, headers, body = self.request('/entry', authenticated=False)
        self.assertEqual(code, 200)
        self.assertNotIn('결제 인증', body)
        self.assertIn('src="/static/entry.js"', body)
        code, headers, _ = self.request('/session', 'POST', {'token': self.auth.issue()}, authenticated=False)
        self.assertEqual(code, 303)
        self.assertIn('HttpOnly; SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn('Domain=', headers['Set-Cookie'])
        for path in ['/', '/pr/1', '/settings', '/?diff=1']:
            self.assertEqual(self.request(path, authenticated=False)[0], 401)
        self.assertEqual(self.request('/?token=secret')[0], 400)

    def test_strict_host_and_origin(self):
        for fields in [[('!Host', None)], [('Host', self.server.authority)], [('!Host', 'localhost:8123')], [('!Host', 'evil.test')], [('!Host', '127.0.0.1:9999')]]:
            self.assertEqual(self.request(headers=fields)[0], 403)
        for origin in [None, 'null', 'http://127.0.0.1:9999', 'https://evil.test']:
            self.assertEqual(self.request('/refresh', 'POST', {'csrf': self.csrf}, [('!Origin', origin)])[0], 403)
        self.assertEqual(self.request('/refresh', 'POST', {'csrf': self.csrf}, [('Origin', self.server.origin)])[0], 403)
        self.assertEqual(self.request(headers=[('Origin', 'http://evil.test')])[0], 403)
        # 전달 헤더를 신뢰하지 않는다.
        self.assertEqual(self.request(headers=[('!Host', 'evil.test'), ('X-Forwarded-Host', self.server.authority)])[0], 403)

    def test_refresh_is_outside_pr_a(self):
        self.assertEqual(self.request('/refresh')[0], 405)
        self.assertEqual(self.request('/refresh', 'POST', {'csrf': self.csrf})[0], 405)
        self.assertNotIn('action="/refresh"', self.request()[2])
        with closing(connect(self.config)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_security_headers_on_success_errors_static(self):
        for path, authenticated in [('/', True), ('/pr/1', True), ('/', False), ('/missing', True), ('/static/app.css', False), ('/static/entry.js', False), ('/static/../../config.json', True)]:
            _, headers, body = self.request(path, authenticated=authenticated)
            self.assertEqual(headers['Content-Security-Policy'], CSP)
            self.assertNotIn('unsafe-inline', CSP)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['Referrer-Policy'], 'same-origin')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(self.request('/refresh', 'POST', {'csrf': self.csrf}, [('Content-Length', '0')])[0], 400)
        self.assertEqual(self.request('/refresh', 'POST', {'csrf': self.csrf}, [('Transfer-Encoding', 'chunked')])[0], 400)

    def test_demo_priority_filters_and_collapsed_units(self):
        code, _, body = self.request()
        self.assertEqual(code, 200)
        self.assertLess(body.index('결제 인증 경계'), body.index('사용 설명 보완'))
        self.assertIn('리뷰 요청', body)
        self.assertIn('수집', body)
        body = self.request('/?importance=low')[2]
        self.assertIn('사용 설명 보완', body)
        self.assertNotIn('결제 인증 경계', body)
        self.assertNotIn('일반 API 응답 정리', self.request('/?review=1')[2])
        body = self.request('/pr/1')[2]
        with closing(connect(self.config)) as conn:
            analysis = conn.execute('SELECT model, cli_version FROM analyses WHERE snapshot_id=1 ORDER BY id DESC LIMIT 1').fetchone()
        self.assertIn('모델 ' + analysis['model'], body)
        self.assertIn('Codex CLI ' + analysis['cli_version'], body)
        self.assertIn('<details open><summary><span class="meta"><span>auth/session.py', body)
        self.assertIn('<details open><summary><span class="meta"><span>.env', body)
        self.assertIn('<details><summary><span class="meta"><span>README.md', body)
        self.assertIn('규칙 기반 후보', body)
        self.assertIn('미분석', body)
        self.assertIn('https://github.com/demo/checkout/pull/1', body)
        self.assertIn('전체 원본 diff', self.request('/pr/1?diff=1')[2])
        self.assertIn('이전 SHA 결과', self.request('/pr/5')[2])
        self.assertIn('대기', self.request('/pr/4')[2])
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET state='closed' WHERE id=1")
        self.assertNotIn('결제 인증 경계', self.request()[2])

    def test_risk_sources_are_separate_in_summary_and_hunks(self):
        with closing(connect(self.config)) as conn, conn:
            row = conn.execute('SELECT id,output_json FROM analyses WHERE snapshot_id=1 ORDER BY id DESC LIMIT 1').fetchone()
            result = json.loads(row['output_json'])
            unit = next(u for u in result['units']
                        if any(i['change_id'] == u['change_id'] for i in result['items']))
            item = next(i for i in result['items'] if i['change_id'] == unit['change_id'])
            unit['candidates'] = ['logic_without_tests', 'install_script']
            item['risk_flags'] = ['config']
            conn.execute('UPDATE analyses SET output_json=? WHERE id=?', (json.dumps(result), row['id']))
        body = self.request('/pr/1')[2]
        hunk = next(part.split('</details>')[0] for part in body.split('<details')
                    if '<span>' + unit['change_id'] + '</span>' in part)
        self.assertIn('<p>규칙 후보: logic_without_tests, 설치 스크립트</p>', hunk)
        self.assertIn('<p>모델 판단: config</p>', hunk)
        for page in (body.split('<details')[0], self.request()[2]):
            self.assertIn('설치 스크립트', page)
            self.assertIn('규칙 후보: ', page)
            self.assertIn('모델 판단: ', page)

    def test_unanalyzed_model_label_and_empty_badges(self):
        body = self.request('/pr/1')[2]
        self.assertIn('<p>모델 판단: 미분석</p>', body)
        base = dict(importance='low', rule_flags=[], model_flags=[],
                    unanalysed=False, items={'h1': {}}, stale=False, draft=False)
        self.assertNotIn('규칙 후보:', badges(base))
        self.assertNotIn('모델 판단:', badges(base))
        self.assertIn('모델 판단: 미분석', badges(dict(base, unanalysed=True, items={})))
        self.assertIn('모델 판단: 일부 미분석', badges(dict(base, unanalysed=True)))

    def test_diff_colors_preserve_and_escape_content(self):
        diff = '--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+<script>\n+++ content\n context\n'
        rendered = render_diff(diff)
        self.assertIn('<span class="diff-header">--- a/file\n</span>', rendered)
        self.assertIn('<span class="diff-hunk">@@ -1 +1 @@\n</span>', rendered)
        self.assertIn('<span class="diff-delete">-old\n</span>', rendered)
        self.assertIn('<span class="diff-add">+&lt;script&gt;\n</span>', rendered)
        self.assertIn('<span class="diff-add">+++ content\n</span>', rendered)
        self.assertIn('<span class="diff-context"> context\n</span>', rendered)
        self.assertIn('class="diff-add"', self.request('/pr/1')[2])
        self.assertIn('class="diff-hunk"', self.request('/pr/1?diff=1')[2])

    def test_untrusted_content_and_cache_paths(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET title=?,url=? WHERE id=1", ('<script>alert(1)</script>', 'javascript:alert(1)'))
            conn.execute("UPDATE analyses SET intent=? WHERE snapshot_id=1", ('<img src=x onerror=alert(1)>',))
        body = self.request('/pr/1')[2]
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('&lt;img', body)
        self.assertNotIn('javascript:', body)
        self.assertNotIn('<img', body)
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('private')
        self.assertEqual(read_diff(self.config, {'diff_path': str(outside)}), '')
        link = self.config.data_dir / 'cache' / 'link'
        link.symlink_to(outside)
        self.assertEqual(read_diff(self.config, {'diff_path': str(link)}), '')

    def test_new_snapshot_does_not_reuse_old_hunk_verdict(self):
        with closing(connect(self.config)) as conn, conn:
            row = conn.execute('SELECT * FROM snapshots WHERE pr_id=1').fetchone()
            conn.execute("INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path,coverage) VALUES(1,'base','new','d','i',?,?)", (row['diff_path'], row['coverage']))
        body = self.request('/pr/1')[2]
        self.assertIn('이전 SHA 결과', body)
        self.assertIn('README.md</span><span>h', body)
        self.assertNotIn('<details><summary><span class="meta"><span>README.md', body)

    def test_single_instance_reissues_url_and_private_socket_cleanup(self):
        url = self.server.origin + '/entry#fixture-token'
        with patch('skim.web.fcntl.flock', side_effect=BlockingIOError), patch('skim.web.socket.socket') as sock, patch('webbrowser.open') as browser, redirect_stdout(io.StringIO()) as out:
            sock.return_value.__enter__.return_value.recv.return_value = url.encode()
            self.assertEqual(serve(self.config, open_browser=False), 0)
            self.assertEqual(out.getvalue().strip(), url)
            browser.assert_not_called()
        self.assertEqual((self.config.data_dir / 'web.lock').stat().st_mode & 0o777, 0o600)
        with patch('skim.web.Server') as server, patch('skim.web.socket.socket') as sock, patch('skim.web.threading.Thread'), redirect_stdout(io.StringIO()):
            instance = server.return_value.__enter__.return_value
            instance.entry_url.return_value = url
            instance.serve_forever.side_effect = KeyboardInterrupt
            control = sock.return_value.__enter__.return_value
            def bind(path):
                Path(path).touch()
                self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            control.bind.side_effect = bind
            self.assertEqual(serve(self.config, open_browser=False, offline=True), 0)
        self.assertFalse((self.config.data_dir / 'web.sock').exists())

    def test_server_loopback_and_worker_uses_existing_scheduler(self):
        with patch('skim.web.ThreadingHTTPServer.__init__', autospec=True) as init:
            # 서버 포트는 실제 bind 결과에서 받으므로 가짜 기반 생성자가 설정한다.
            def fake_init(server, address, handler):
                self.assertEqual(address, ('127.0.0.1', 0))
                server.server_port = 8123
            init.side_effect = fake_init
            server = Server(self.config)
            self.assertTrue(server.entry_url().startswith(self.server.origin + '/entry#'))
        stop = SimpleNamespace(is_set=lambda: False, wait=lambda seconds: None)
        with patch('skim.jobs.CollectionScheduler') as scheduler, patch('skim.jobs.Collector'), patch('skim.analyze.Analyzer') as analyzer:
            stop.is_set = unittest.mock.Mock(side_effect=[False, True])
            scheduler.return_value.tick.return_value = True
            scheduler.return_value.last_ok = True
            analyzer.return_value.run.return_value = []
            worker(self.config, stop)
            scheduler.return_value.tick.assert_called_once()
            # 수집 성공 후 자동 분석은 같은 큐의 analyze 작업으로 실행된다.
            analyzer.return_value.run.assert_called_once_with()
        with closing(connect(self.config)) as conn:
            self.assertEqual(conn.execute("SELECT type,status FROM jobs").fetchall()[0][:], ('analyze', 'completed'))


if __name__ == '__main__':
    unittest.main()
