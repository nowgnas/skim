"""PR 상세의 검토 흐름: 중복 없는 먼저 볼 파일, 앞뒤 파일 이동, 버튼 위계, 전체 경로."""
from contextlib import closing
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from test_web import WebTests
from skim.db import connect
from skim.web import file_anchor
from skim.web_settings import RepoLookup


class DetailFlowTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)

    def test_focus_files_link_to_workspace_without_duplicating_diff(self):
        body = self.request('/pr/1')[2]
        focus = body.split('class="detail-focus"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn('<pre', focus)
        self.assertIn(f'data-select-file="{file_anchor("auth/session.py")}"', focus)
        self.assertEqual(body.count('id="' + file_anchor('auth/session.py') + '"'), 1)

    def test_each_panel_links_previous_and_next_in_list_order(self):
        body = self.request('/pr/1')[2]
        panels = re.findall(r'<section class="detail-file" id="([^"]+)"[^>]*data-file-path="([^"]+)"', body)
        self.assertGreaterEqual(len(panels), 3)
        for index, (anchor, _) in enumerate(panels):
            block = body.split(f'id="{anchor}"', 1)[1].split('</section>', 1)[0]
            step = block.split('class="file-step"', 1)[1]
            previous = re.search(r'rel="prev"[^>]*data-select-file="([^"]+)"', step)
            following = re.search(r'rel="next"[^>]*data-select-file="([^"]+)"', step)
            self.assertEqual(previous and previous[1], panels[index - 1][0] if index else None)
            self.assertEqual(following and following[1], panels[index + 1][0] if index + 1 < len(panels) else None)
        # 링크만으로도(스크립트 없이) 다음 파일이 선택된 상태로 열린다.
        href = re.search(r'rel="next" href="([^"]+)"', body)[1].replace('&amp;', '&')
        opened = self.request('/pr/1' + href.split('#')[0])[2]
        self.assertRegex(opened, rf'id="{panels[1][0]}"[^>]*data-file-path="[^"]+">')

    def test_reanalysis_is_secondary_and_status_is_visible(self):
        body = self.request('/pr/1')[2]
        action = body.split('class="detail-action"', 1)[1].split('</article>', 1)[0]
        self.assertIn('class="btn secondary">다시 분석</button>', action)
        self.assertIn('class="analysis-summary"', action)
        self.assertIn('분석 ', action)

    def test_selected_file_title_shows_full_path_and_no_english_eyebrow(self):
        body = self.request('/pr/1')[2]
        self.assertIn('<span class="file-parent">auth/</span><span class="file-basename">session.py</span>', body)
        self.assertNotIn('PR REVIEW', body)
        self.assertNotIn('1/1 페이지', body)

    @unittest.skipUnless(shutil.which('node'), 'Node.js가 없어 브라우저 스크립트 실행을 생략합니다')
    def test_script_records_history_and_handles_keys(self):
        script = Path(__file__).resolve().parents[1] / 'lib/skim/web_static/jobs.js'
        harness = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const mk = id => ({id, hidden: id !== 'a', dataset: {filePath: id + '.py'}, focus() {}, scrollIntoView() {}});
const panels = [mk('a'), mk('b'), mk('c')];
const listeners = {}, winListeners = {}, pushed = [];
const workspace = {querySelectorAll: s => s.includes('data-file-panel') ? panels : [], querySelector: () => null,
  contains: () => true, scrollIntoView() {}};
const location = {href: 'http://x/pr/1#a', hash: '#a'};
const context = {URL,
  document: {documentElement: {classList: {add() {}}}, querySelector: s => s.includes('workspace') ? workspace : null,
    querySelectorAll: () => [], addEventListener: (n, f) => { listeners[n] = f; }},
  window: {location, addEventListener: (n, f) => { winListeners[n] = f; }},
  history: {pushState: (s, t, u) => { pushed.push(String(u)); location.href = String(u); location.hash = new URL(u).hash; }}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const key = (k, target = {closest: () => null}) => { let stopped = false;
  listeners.keydown({key: k, target, preventDefault() { stopped = true; }}); return stopped; };
assert.ok(key('j'));
assert.deepEqual(panels.map(p => p.hidden), [true, false, true]);
assert.equal(pushed.length, 1);
assert.match(pushed[0], /file=b\.py#b$/);
assert.equal(key('j', {closest: () => ({})}), false);           // 입력 중에는 가로채지 않음
key('j'); assert.equal(key('j'), false);                         // 마지막에서 멈춤
assert.deepEqual(panels.map(p => p.hidden), [true, true, false]);
location.hash = '#a'; winListeners.popstate();                  // 뒤로 가기
assert.deepEqual(panels.map(p => p.hidden), [false, true, true]);
assert.equal(pushed.length, 2);                                 // 뒤로 가기는 기록을 늘리지 않음
'''
        result = subprocess.run(['node', '-e', harness, str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
