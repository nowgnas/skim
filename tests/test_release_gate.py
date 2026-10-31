"""실제 모델 평가 없는 버전의 출시 차단 검사."""
import subprocess
import sys
import unittest
from pathlib import Path


class ReleaseGateTests(unittest.TestCase):
    def test_unverified_prompt_blocks_release(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / 'scripts/check_release.py')],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('출시 차단', result.stderr)
        self.assertIn('실제 모델 회귀 평가 미검증', result.stderr)
