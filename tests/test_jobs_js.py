"""브라우저 폴링 스크립트의 실제 상태 전환을 Node의 가짜 DOM으로 검증한다."""
from pathlib import Path
import shutil
import subprocess
import unittest


class JobPollingScriptTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js가 없어 브라우저 스크립트 실행을 생략합니다')
    def test_pending_to_running_and_unknown_progress(self):
        script = Path(__file__).resolve().parents[1] / 'lib/skim/web_static/jobs.js'
        harness = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const queue = [];
const field = { replaceChildren(...items) { this.items = items; } };
const label = { textContent: '대기 중' };
const progress = { hidden: true, removeAttribute(key) { delete this[key]; } };
const box = { dataset: { jobId: '1' }, querySelector(selector) {
  return selector.includes('summary') ? field : selector.includes('label') ? label : progress;
}};
let job = {active: true, status: 'running', processed: 2, total: 5, summary_parts: ['실행 중']};
const listeners = {};
const context = { document: { querySelectorAll: () => [box], createElement: () => ({}), addEventListener: (name, fn) => { listeners[name] = fn; } },
  fetch: () => Promise.resolve({ok: true, json: () => Promise.resolve(job)}),
  setTimeout: fn => queue.push(fn), location: {reload: () => { throw Error('unexpected reload'); }},
  encodeURIComponent, Number };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const tick = async () => { queue.shift()(); await new Promise(resolve => setImmediate(resolve)); };
(async () => {
  await tick();
  assert.equal(label.textContent, '작업 중');
  assert.equal(progress.hidden, false);
  assert.equal(progress.value, 2);
  assert.equal(progress.max, 5);
  job = {active: true, status: 'running', processed: null, total: null, summary_parts: []};
  await tick();
  assert.equal('value' in progress, false);
  job = {active: true, status: 'pending', processed: null, total: null, summary_parts: []};
  await tick();
  assert.equal(label.textContent, '대기 중');
  assert.equal(progress.hidden, true);
  listeners.input({target: {closest: () => true}});
  const added = [];
  box.append = node => added.push(node);
  job = {active: false, status: 'completed', summary_parts: ['완료']};
  await tick();
  assert.equal(label.textContent, '작업 종료');
  assert.equal(added[0].textContent, '결과 새로고침');
})().catch(err => { console.error(err); process.exitCode = 1; });
'''
        subprocess.run(['node', '-e', harness, str(script)], check=True, capture_output=True, text=True)
