import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.web import list_controls


class ListControlsTest(unittest.TestCase):
    def test_search_pagination_and_other_state(self):
        items = [f'Repo-{i:02}' for i in range(45)]
        page, controls, navigation = list_controls(items, {'other_q': ['x'], 'repo_page': ['2']}, 'repo', '저장소', str)
        self.assertEqual(page, items[20:40])
        self.assertIn('other_q', controls)
        self.assertIn('repo_page=3', navigation)
        page, controls, _ = list_controls(items, {'repo_q': ['REPO-4'], 'repo_page': ['999']}, 'repo', '저장소', str)
        self.assertEqual(page, items[40:])
        self.assertIn('1/1 페이지', controls)
        page, controls, _ = list_controls(items, {'repo_q': ['missing']}, 'repo', '저장소', str)
        self.assertFalse(page)
        self.assertIn('검색 결과가 없습니다', controls)
