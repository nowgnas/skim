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
const result = {hidden: true};
const resultText = {textContent: ''};
const refresh = {hidden: true};
const box = { dataset: { jobId: '1' }, querySelectorAll: () => [], querySelector(selector) {
  if (selector.includes('summary')) return field;
  if (selector.includes('progress-label')) return label;
  if (selector.includes('result-text')) return resultText;
  if (selector.includes('result]')) return result;
  if (selector.includes('refresh')) return refresh;
  return progress;
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
  job = {active: false, status: 'failed', status_label: '실패', error: '<unsafe>', result: '검증 결과', summary_parts: ['실패']};
  await tick();
  assert.equal(label.textContent, '실패');
  assert.equal(resultText.textContent, '<unsafe>\n검증 결과');
  assert.equal(result.hidden, false);
  assert.equal(refresh.hidden, false);
  assert.equal(box.dataset.jobActive, '0');
})().catch(err => { console.error(err); process.exitCode = 1; });
'''
        subprocess.run(['node', '-e', harness, str(script)], check=True, capture_output=True, text=True)

    @unittest.skipUnless(shutil.which('node'), 'Node.js가 없어 브라우저 스크립트 실행을 생략합니다')
    def test_history_card_fields_follow_polling(self):
        script = Path(__file__).resolve().parents[1] / 'lib/skim/web_static/jobs.js'
        harness = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const queue = [];
const el = (extra = {}) => ({textContent: '', hidden: false, className: '', children: [],
  append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }, ...extra});
const classes = new Set(['job-row', 'job-running']);
const row = {classList: {toggle: (name, on) => on ? classes.add(name) : classes.delete(name)}};
const parts = {'[data-job-facts]': el(), '[data-job-progress-label]': el(), 'progress': el({removeAttribute(key) { delete this[key]; }}),
  '[data-job-result]': el(), '[data-job-result-text]': el(), '[data-job-refresh]': el({hidden: true}),
  '[data-job-status]': el(), '.job-note': el(), '.job-error': el({hidden: true}), '[data-job-progress]': el()};
const box = {dataset: {jobId: '6', jobRecord: '1'}, querySelectorAll: () => [], closest: () => row,
  querySelector(selector) { assert.ok(selector in parts, selector); return parts[selector]; }};
const view = extra => ({status_label: '실행 중', tone: 'running', facts: [['시작', '오늘 00:37 KST', '3분 전'], ['경과', '3분 1초', '']], note: '저장소 수집 중 2/5', error_line: '', ...extra});
let job = {active: true, status: 'running', processed: 2, total: 5, summary_parts: ['수집 #6'], record: view()};
let fail = false;
const context = { document: { querySelectorAll: () => [box], createElement: () => el(), addEventListener() {} },
  fetch: () => fail ? Promise.reject(Error('offline')) : Promise.resolve({ok: true, json: () => Promise.resolve(job)}),
  setTimeout: fn => queue.push(fn), location: {reload: () => { throw Error('unexpected reload'); }},
  encodeURIComponent, Number, JSON, Boolean };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const tick = async () => { queue.shift()(); await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve)); };
const facts = () => parts['[data-job-facts]'].children.map(fact => fact.children.map(item => item.textContent));
(async () => {
  await tick();
  assert.equal(parts['[data-job-status]'].textContent, '실행 중');
  assert.equal(parts['[data-job-status]'].className, 'badge job-status running');
  assert.deepEqual(facts(), [['시작', '오늘 00:37 KST', '3분 전'], ['경과', '3분 1초']]);
  assert.equal(parts['[data-job-facts]'].children[0].className, 'job-fact');
  assert.equal(parts['.job-note'].textContent, '저장소 수집 중 2/5');
  assert.equal(parts['.job-error'].hidden, true);
  assert.equal(parts['progress'].value, 2);
  assert.ok(classes.has('job-running'));
  // A failed request keeps polling and the next success redraws the card.
  fail = true;
  await tick();
  assert.equal(parts['[data-job-facts]'].textContent, '상태 조회 실패, 자동 재시도 중');
  fail = false;
  job = {active: false, status: 'failed', status_label: '실패', error: '수집 실패: <b>\n둘째 줄', summary_parts: ['수집 #6'],
    record: view({status_label: '실패', tone: 'high', facts: [['시작', '오늘 00:37 KST', ''], ['종료', '오늘 00:41 KST', ''], ['소요', '4분', '']], note: '', error_line: '수집 실패: <b>'})};
  await tick();
  assert.equal(parts['[data-job-status]'].textContent, '실패');
  assert.equal(parts['[data-job-status]'].className, 'badge job-status high');
  assert.deepEqual(facts(), [['시작', '오늘 00:37 KST'], ['종료', '오늘 00:41 KST'], ['소요', '4분']]);
  assert.equal(parts['.job-note'].hidden, true);
  assert.equal(parts['.job-error'].textContent, '수집 실패: <b>');
  assert.equal(parts['.job-error'].hidden, false);
  assert.equal(parts['[data-job-progress]'].hidden, true);
  assert.equal(parts['[data-job-result-text]'].textContent, '수집 실패: <b>\n둘째 줄');
  assert.equal(parts['[data-job-refresh]'].hidden, false);
  assert.ok(classes.has('job-failed') && !classes.has('job-running'));
  assert.equal(box.dataset.jobActive, '0');
  assert.equal(queue.length, 0);
})().catch(err => { console.error(err); process.exitCode = 1; });
'''
        done = subprocess.run(['node', '-e', harness, str(script)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
