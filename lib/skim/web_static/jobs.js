/* 활성 작업을 조회하고, 통신 실패는 최대 30초 간격으로 재시도한다. */
'use strict';
for (const box of document.querySelectorAll('[data-job-active="1"]')) {
  const field = box.querySelector('[data-job-field="summary"]');
  const label = box.querySelector('[data-job-progress-label]');
  const progress = box.querySelector('progress');
  const result = box.querySelector('[data-job-result]');
  const resultText = box.querySelector('[data-job-result-text]');
  const refresh = box.querySelector('[data-job-refresh]');
  let retryDelay = 2000;
  let lastSummary = null;
  const tick = () => {
    fetch('/jobs/' + encodeURIComponent(box.dataset.jobId), {credentials: 'same-origin', headers: {Accept: 'application/json'}})
      .then(response => response.ok ? response.json() : Promise.reject(response.status))
      .then(job => {
        retryDelay = 2000;
        const parts = box.dataset.jobCompact === '1' ? job.collection_summary_parts :
          box.dataset.jobAnalysis === '1' ? job.analysis_summary_parts : job.summary_parts;
        const summary = JSON.stringify(parts);
        if (summary !== lastSummary) {
          field.replaceChildren(...parts.map(text => {
            const span = document.createElement('span');
            span.textContent = text;
            return span;
          }));
          lastSummary = summary;
        }
        let text = [job.error, job.result].filter(Boolean).join('\n');
        if (box.dataset.jobAnalysis === '1' && text === parts[0]) text = '';
        if (resultText.textContent !== text) resultText.textContent = text;
        result.hidden = !text;
        if (job.active) {
          const running = job.status === 'running';
          label.textContent = running ? '작업 중' : '대기 중';
          progress.hidden = !running;
          if (running && Number.isFinite(job.total) && job.total > 0 && Number.isFinite(job.processed) && job.processed >= 0 && job.processed <= job.total) {
            progress.max = job.total;
            progress.value = job.processed;
          } else {
            progress.removeAttribute('value');
          }
        }
        if (job.active) setTimeout(tick, 2000);
        else {
          label.textContent = job.status_label || '작업 종료';
          progress.hidden = true;
          refresh.hidden = false;
          box.dataset.jobActive = '0';
        }
      })
      .catch(() => {
        field.textContent = '상태 조회 실패, 자동 재시도 중';
        lastSummary = null;
        setTimeout(tick, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30000);
      });
  };
  setTimeout(tick, 2000);
}

// 파일 선택은 목록과 검토 위치를 유지한 채 내용만 바꾼다.
const workspace = typeof document.querySelector === 'function' ? document.querySelector('[data-file-workspace]') : null;
if (workspace) {
  document.documentElement.classList.add('file-workspace-ready');
  const panels = [...workspace.querySelectorAll('[data-file-panel]')];
  const select = workspace.querySelector('[data-file-select]');
  const status = workspace.querySelector('[data-selected-file]');
  const links = [...document.querySelectorAll('[data-select-file]')];
  const selectFile = (id, fromOverview, record = true) => {
    const panel = panels.find(item => item.id === id);
    if (!panel) return false;
    for (const item of panels) item.hidden = item !== panel;
    for (const link of links) {
      if (link.dataset.selectFile === id) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
    if (select) select.value = id;
    if (status) status.textContent = '선택한 파일: ' + panel.dataset.filePath;
    for (const link of document.querySelectorAll('[aria-label="변경 보기"] a')) {
      const target = new URL(link.href);
      target.searchParams.set('file', panel.dataset.filePath);
      link.href = target.href;
    }
    const url = new URL(window.location.href);
    url.searchParams.set('file', panel.dataset.filePath);
    url.hash = id;
    // 파일 전환을 기록에 남겨 브라우저 뒤로 가기로 이전 파일에 돌아올 수 있게 한다.
    if (record && url.href !== window.location.href) history.pushState({file: id}, '', url);
    if (fromOverview) {
      workspace.scrollIntoView({block: 'start'});
      panel.focus({preventScroll: true});
    }
    return true;
  };
  for (const link of links) {
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (selectFile(link.dataset.selectFile, !workspace.contains(link))) event.preventDefault();
    });
  }
  if (select) select.addEventListener('change', () => selectFile(select.value, false));
  window.addEventListener('popstate', () => {
    const id = window.location.hash.slice(1);
    if (!selectFile(id, false, false) && panels.length) selectFile(panels[0].id, false, false);
  });
  // j/k로 현재 목록 순서의 다음, 이전 파일로 넘긴다. 입력 중에는 가로채지 않는다.
  document.addEventListener('keydown', event => {
    if (event.metaKey || event.ctrlKey || event.altKey || !['j', 'k'].includes(event.key)) return;
    if (event.target.closest && event.target.closest('input, textarea, select, [contenteditable]')) return;
    const current = panels.findIndex(item => !item.hidden);
    const next = panels[current + (event.key === 'j' ? 1 : -1)];
    if (!next) return;
    event.preventDefault();
    selectFile(next.id, false);
    next.focus({preventScroll: true});
    next.scrollIntoView({block: 'start'});
  });
}
