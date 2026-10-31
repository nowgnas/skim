"""실제 소켓·네트워크·모델 없이 HTTP와 데모 fixture를 검증한다."""
import io
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
from skim.web import Auth, Handler, CSP, Server, load_prs, worker, read_diff, serve
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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, SKIM_HOME=self.temp.name)
        env.start()
        self.addCleanup(env.stop)
        self.config = Config.load()
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

    def test_csrf_and_fixed_refresh_job(self):
        for data in [{}, {'csrf': 'wrong'}, {'csrf': '한글'}, {'csrf': self.csrf, 'target_id': '99'}, {'csrf': [self.csrf, self.csrf]}]:
            self.assertIn(self.request('/refresh', 'POST', data)[0], (400, 403))
        self.assertEqual(self.request('/refresh')[0], 404)
        self.assertEqual(self.request('/refresh', 'POST', {'csrf': self.csrf})[0], 303)
        self.assertEqual(self.request('/refresh', 'POST', {'csrf': self.csrf})[0], 303)
        with closing(connect(self.config)) as conn, conn:
            rows = conn.execute('SELECT type,target_id,status FROM jobs').fetchall()
            self.assertEqual([tuple(row) for row in rows], [('collect', 0, 'pending')])

    def test_security_headers_on_success_errors_static(self):
        for path, authenticated in [('/', True), ('/pr/1', True), ('/', False), ('/missing', True), ('/static/app.css', False), ('/static/entry.js', False), ('/static/../../config.json', True)]:
            _, headers, body = self.request(path, authenticated=authenticated)
            self.assertEqual(headers['Content-Security-Policy'], CSP)
            self.assertNotIn('unsafe-inline', CSP)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
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
        self.assertIn('<details open><summary>auth/session.py', body)
        self.assertIn('<details open><summary>.env', body)
        self.assertIn('<details><summary>README.md', body)
        self.assertIn('규칙 기반 후보', body)
        self.assertIn('미분석', body)
        self.assertIn('https://github.com/demo/checkout/pull/1', body)
        self.assertIn('전체 원본 diff', self.request('/pr/1?diff=1')[2])
        self.assertIn('이전 SHA 결과', self.request('/pr/5')[2])
        self.assertIn('대기', self.request('/pr/4')[2])
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET state='closed' WHERE id=1")
        self.assertNotIn('결제 인증 경계', self.request()[2])

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
        self.assertIn('README.md · h', body)
        self.assertNotIn('<details><summary>README.md', body)

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
        with patch('skim.web.CollectionScheduler') as scheduler, patch('skim.web.Collector'), patch('skim.analyze.Analyzer') as analyzer:
            stop.is_set = unittest.mock.Mock(side_effect=[False, True])
            scheduler.return_value.tick.return_value = True
            worker(self.config, stop)
            scheduler.return_value.tick.assert_called_once()
            analyzer.return_value.run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
