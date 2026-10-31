"""가짜 gh와 로컬 fixture만으로 Sessio 연결을 검증한다."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.config import Config
from skim.collect import CollectionError
from skim.sessio import session_link, valid_base_url


class SessioTests(unittest.TestCase):
    def test_matching_and_invalid_index(self):
        fixture = Path(__file__).parent / 'fixtures/sessio.jsonl'
        rows = [json.loads(line) for line in fixture.read_text().splitlines()]
        pr = dict(owner='demo', name='checkout', head_branch='feature/payment', author='me')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sessions.jsonl'
            config = Config(Path(tmp), Path(tmp), sessio_index_path=path)
            with patch('skim.sessio.GitHub.get', return_value={'login': 'ME'}) as gh:
                for data in ['', 'invalid', '[]', json.dumps(rows[1]), '\n'.join(map(json.dumps, [rows[0], rows[0]])), json.dumps(rows[0]) + '\ninvalid']:
                    path.write_text(data)
                    self.assertIsNone(session_link(config, pr))
                gh.assert_not_called()
                path.write_text(fixture.read_text())
                self.assertEqual(session_link(config, pr)[0], 'abc-123')
                self.assertIsNone(session_link(config, dict(pr, head_branch='other')))
                self.assertIsNone(session_link(config, dict(pr, name='different')))
                for field, value in [('session_id', '../bad'), ('session_id', '<script>'), ('session_id', 'bad#id'), ('harness', 'unknown')]:
                    path.write_text(json.dumps(dict(rows[0], **{field: value})))
                    self.assertIsNone(session_link(config, pr))
            path.write_text(fixture.read_text())
            with patch('skim.sessio.GitHub.get', side_effect=CollectionError('인증 실패')):
                self.assertIsNone(session_link(config, pr))
            path.unlink()
            self.assertIsNone(session_link(config, pr))

    def test_config_and_link_validation(self):
        for url in ['javascript:alert(1)', 'https://evil.test/', 'http://127.0.0.1:8765@evil.test/', 'http://127.0.0.1:8765/?t=secret', 'http://localhost:0/', 'http://localhost:99999/', 'http://localhost:8765/#bad', 'http://localhost:8765/\n']:
            self.assertFalse(valid_base_url(url), url)
        self.assertTrue(valid_base_url('http://localhost:8766/'))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, SKIM_HOME=tmp, SESSIO_HOME=tmp + '/sessio'):
            config = Config.load()
            self.assertEqual(config.sessio_index_path, Path(tmp) / 'sessio/index/sessions.jsonl')
            settings = config.config_dir / 'config.json'
            settings.write_text(json.dumps(dict(sessio_index_path=tmp + '/custom.jsonl', sessio_web_url='http://localhost:8766/')))
            config = Config.load()
            self.assertEqual(config.sessio_index_path, Path(tmp) / 'custom.jsonl')
            self.assertEqual(config.sessio_web_url, 'http://localhost:8766/')
            settings.write_text('{"sessio_web_url":"https://evil.test/"}')
            with self.assertRaises(ValueError):
                Config.load()
