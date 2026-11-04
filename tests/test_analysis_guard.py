"""실제 모델 없이 비정상 종료와 모델 실행 표식을 검증한다."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.analyze.codex import CodexAdapter, IsolationError

GUARD = Path(__file__).resolve().parents[1] / 'lib/skim/analyze/_guard.py'


class GuardTests(unittest.TestCase):
    def test_successful_model_call_cleans_marker(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / 'source'
            source.mkdir()
            (source / 'auth.json').write_text('fixture')
            model = root / 'model'
            model.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text('{{}}')
print('{{"type":"turn.completed"}}')
''')
            model.chmod(0o700)
            with patch.dict(os.environ, CODEX_HOME=str(source), SKIM_CODEX_BIN=str(model)):
                adapter = CodexAdapter(data_dir=root / 'data')
                adapter.init_home()
                self.assertEqual(adapter._execute('fixture', {}), '{}')
                self.assertFalse((adapter.data_dir / 'analysis-inflight').exists())

    def test_status_distinguishes_running_from_uncertain_marker(self):
        import fcntl
        from skim.doctor import inflight_status
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            marker = data / 'analysis-inflight'
            self.assertIsNone(inflight_status(data))
            marker.touch()
            self.assertEqual(inflight_status(data), 'uncertain')
            lock_path = data / 'analysis.lock'
            lock_path.touch()
            with lock_path.open('r+') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                self.assertEqual(inflight_status(data), 'running')
            self.assertEqual(inflight_status(data), 'uncertain')

    def test_marker_blocks_before_automatic_budget_reservation(self):
        from skim.analyze import Analyzer
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            data = root / 'data'
            data.mkdir()
            (data / 'analysis-inflight').touch()
            adapter = CodexAdapter(Mock(), data)
            analyzer = Analyzer(None, None, adapter)
            with patch.object(analyzer, '_reserve') as reserve:
                with self.assertRaises(IsolationError):
                    analyzer._call('synthesis', {}, {}, budgeted=True)
                reserve.assert_not_called()

    def test_timeout_terminates_model_and_clears_marker(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'inflight'
            result = subprocess.run([sys.executable, str(GUARD), str(marker), '0.2',
                                     sys.executable, '-c', 'import time; time.sleep(30)'], timeout=10)
            self.assertEqual(result.returncode, 124)
            self.assertFalse(marker.exists())

    def test_guard_killed_leaves_marker_and_blocks_new_call(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / 'source'
            source.mkdir()
            (source / 'auth.json').write_text('fixture')
            data = root / 'data'
            ready = root / 'ready'
            marker = data / 'analysis-inflight'
            with patch.dict(os.environ, CODEX_HOME=str(source)):
                runner = Mock()
                adapter = CodexAdapter(runner, data)
                adapter.init_home()
                code = (f'import os,time; tmp={str(ready)!r} + ".tmp"; open(tmp, "w").write(str(os.getpid())); '
                        f'os.replace(tmp, {str(ready)!r}); time.sleep(30)')
                guard = subprocess.Popen([sys.executable, str(GUARD), str(marker), '30',
                                          sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                pid = None
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and guard.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists())
                    pid = int(ready.read_text())
                    guard.kill()
                    guard.wait(timeout=5)
                    self.assertTrue(marker.exists())
                    with self.assertRaisesRegex(IsolationError, '종료 여부'):
                        adapter._execute('fixture', {})
                    runner.assert_not_called()
                finally:
                    if guard.poll() is None:
                        guard.kill()
                        guard.wait(timeout=5)
                    if pid:
                        try:
                            os.kill(pid, 9)
                        except ProcessLookupError:
                            pass
