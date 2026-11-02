"""데모 임시 경로와 모의 macOS 명령만 사용한다."""
import io
import os
from pathlib import Path
import plistlib
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim import macos_bundle as bundle
from skim import web




class OpenTests(unittest.TestCase):
    def test_open_running_invalid_missing_and_no_url_output(self):
        config = SimpleNamespace(data_dir=Path('/demo'))
        for value in (b'https://evil.invalid/#secret', b'http://127.0.0.1:12/entry#demo', None):
            with patch.object(web.socket, 'socket') as sock, patch.object(web, 'open_entry', return_value=True) as opened, redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                if value is None:
                    sock.return_value.__enter__.return_value.connect.side_effect = FileNotFoundError()
                else:
                    sock.return_value.__enter__.return_value.recv.return_value = value
                self.assertEqual(web.open_running(config), 0 if value and b'/entry#demo' in value else 1)
                self.assertNotIn('http', out.getvalue() + err.getvalue())
                self.assertNotIn('secret', out.getvalue() + err.getvalue())
                self.assertEqual(opened.call_count, int(value is not None and b'/entry#demo' in value))

    def test_browser_argv_never_contains_token_except_system_open(self):
        url = 'http://127.0.0.1:1234/entry#DEMO_SECRET'
        with patch.object(web.sys, 'platform', 'linux'), patch.dict(os.environ, BROWSER='/demo/browser %s'), patch('webbrowser.open', return_value=True) as browser, redirect_stdout(io.StringIO()):
            self.assertFalse(web.open_entry(url))
            self.assertEqual(browser.call_args.args, ('http://127.0.0.1:1234/',))
        with patch.object(web.sys, 'platform', 'darwin'), patch('subprocess.run', return_value=SimpleNamespace(returncode=0)) as run, patch('webbrowser.open') as browser:
            self.assertTrue(web.open_entry(url))
            self.assertEqual(run.call_args.args[0], ['/usr/bin/open', url])
            browser.assert_not_called()

    def test_stderr_request_error_never_includes_exception_url(self):
        with redirect_stderr(io.StringIO()) as output:
            try:
                raise ValueError('http://127.0.0.1:1234/entry#DEMO_SECRET')
            except ValueError:
                web.Server.handle_error(None, None, None)
        self.assertNotIn('DEMO_SECRET', output.getvalue())
        self.assertNotIn('http', output.getvalue())
