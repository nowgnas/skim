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
          if (box.dataset.jobReload === '1' && job.status === 'completed') location.reload();
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
  const position = workspace.querySelector('[data-file-position-label]');
  const fileList = workspace.querySelector('.file-nav-links');
  const revealSelected = () => {
    if (!fileList) return;
    const selected = fileList.querySelector('[aria-current="true"]');
    if (!selected) return;
    const item = selected.getBoundingClientRect(), list = fileList.getBoundingClientRect();
    if (item.top < list.top) fileList.scrollTop -= list.top - item.top;
    else if (item.bottom > list.bottom) fileList.scrollTop += item.bottom - list.bottom + 2;
  };
  revealSelected();
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
    revealSelected();
    if (status) status.textContent = '선택한 파일: ' + panel.dataset.filePath;
    if (position && panel.dataset.filePosition) position.textContent = '파일 ' + panel.dataset.filePosition;
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
      if (selectFile(link.dataset.selectFile, !workspace.contains(link))) {
        event.preventDefault();
        if (link.closest('.file-step')) {
          const panel = panels.find(item => item.id === link.dataset.selectFile);
          panel.focus({preventScroll: true});
          panel.scrollIntoView({block: 'start'});
        }
      }
    });
  }
  if (select) select.addEventListener('change', () => selectFile(select.value, false));
  window.addEventListener('popstate', () => {
    const path = new URL(window.location.href).searchParams.get('file');
    const fromPath = path && panels.find(item => item.dataset.filePath === path);
    const hash = window.location.hash.slice(1);
    const id = panels.some(item => item.id === hash) ? hash : fromPath && fromPath.id;
    if (!selectFile(id, false, false) && panels.length) selectFile(panels[0].id, false, false);
  });
  // j/k로 현재 목록 순서의 다음, 이전 파일로 넘긴다. 입력 중에는 가로채지 않는다.
  document.addEventListener('keydown', event => {
    const chooser = typeof document.getElementById === 'function' && document.getElementById('symbol-chooser');
    if ((chooser && !chooser.hidden) || event.metaKey || event.ctrlKey || event.altKey || !['j', 'k'].includes(event.key)) return;
    if (event.target.closest && event.target.closest('input, textarea, select, [contenteditable]')) return;
    const current = panels.findIndex(item => !item.hidden);
    const next = panels[current + (event.key === 'j' ? 1 : -1)];
    if (!next) {
      const currentPanel = panels[current];
      const link = currentPanel && currentPanel.querySelector && currentPanel.querySelector('a[rel="' + (event.key === 'j' ? 'next' : 'prev') + '"]');
      if (link) { event.preventDefault(); link.click(); }
      return;
    }
    event.preventDefault();
    selectFile(next.id, false);
    next.focus({preventScroll: true});
    next.scrollIntoView({block: 'start'});
  });
}

// 함수 후보는 이미 렌더링된 PR diff에서만 찾는다.
const symbolIndex = typeof document.getElementById === 'function' ? document.getElementById('symbol-index') : null;
const symbolChooser = typeof document.getElementById === 'function' ? document.getElementById('symbol-chooser') : null;
if (symbolIndex && symbolChooser) {
  let definitions = {};
  try { definitions = JSON.parse(symbolIndex.dataset.symbolIndex); } catch (_) { definitions = {}; }
  const title = symbolChooser.querySelector('#symbol-title');
  const results = symbolChooser.querySelector('[data-symbol-results]');
  const close = symbolChooser.querySelector('[data-symbol-close]');
  let opener = null;
  const dismiss = () => { symbolChooser.hidden = true; if (opener) opener.focus(); };
  const go = href => {
    history.replaceState({...history.state, skimReturnY: window.scrollY}, '', location.href);
    symbolChooser.hidden = true;
    location.assign(href);
  };
  const back = document.querySelector('[data-symbol-return]');
  if (back) back.addEventListener('click', () => history.back());
  document.addEventListener('click', event => {
    const button = event.target.closest('.symbol-call');
    if (!button) return;
    opener = button;
    const name = button.dataset.symbol;
    const side = button.closest('[data-symbol-side]').dataset.symbolSide;
    const candidates = Array.isArray(definitions[name]) ? definitions[name] : [];
    const matching = candidates.filter(item => side === 'both' || item.side === 'both' || item.side === side);
    title.textContent = name + ' 정의와 사용 위치';
    results.replaceChildren();
    for (const [kind, label] of [['definition', '구현 위치'], ['usage', '사용 위치']]) {
      const entries = matching.filter(item => item.kind === kind);
      const group = document.createElement('section');
      group.className = 'symbol-group';
      const heading = document.createElement('h3');
      heading.className = 'symbol-group-heading';
      heading.textContent = label;
      const count = document.createElement('span');
      count.className = 'symbol-group-count';
      count.textContent = entries.length + '곳';
      heading.append(count);
      group.append(heading);
      results.append(group);
      if (!entries.length) {
        const message = document.createElement('p');
        message.className = 'symbol-empty';
        message.textContent = '이 PR의 변경 내용에서 찾지 못했습니다.';
        group.append(message);
        continue;
      }
      const list = document.createElement('ul');
      list.className = 'symbol-location-list';
      for (const [index, item] of entries.entries()) {
        const destination = new URL(item.href, location.href);
        if (destination.origin !== location.origin || destination.pathname !== location.pathname) continue;
        const row = document.createElement('li');
        const link = document.createElement('a');
        link.href = destination.href;
        link.className = 'symbol-location';
        const locationText = document.createElement('span');
        locationText.className = 'symbol-location-path';
        locationText.textContent = item.path + (item.line == null ? ' (' + item.unit + ', 후보 ' + (index + 1) + ')' : ':' + item.line);
        const side = document.createElement('span');
        side.className = 'symbol-location-side';
        side.textContent = item.side === 'old' ? '변경 전' : item.side === 'new' ? '변경 후' : '변경 전후';
        const arrow = document.createElement('span');
        arrow.className = 'symbol-location-arrow';
        arrow.setAttribute('aria-hidden', 'true');
        arrow.textContent = '→';
        link.append(locationText, side, arrow);
        link.addEventListener('click', event => { event.preventDefault(); go(link.href); });
        row.append(link);
        list.append(row);
      }
      group.append(list);
    }
    symbolChooser.hidden = false;
    close.focus();
  });
  close.addEventListener('click', dismiss);
  symbolChooser.addEventListener('click', event => { if (event.target === symbolChooser) dismiss(); });
  document.addEventListener('keydown', event => {
    if (symbolChooser.hidden) return;
    if (event.key === 'Escape') { dismiss(); return; }
    if (event.key === 'Tab') {
      const controls = [...symbolChooser.querySelectorAll('button, a[href]')];
      if (!controls.length) return;
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener('popstate', event => {
    if (event.state && Number.isFinite(event.state.skimReturnY)) {
      symbolChooser.hidden = true;
      requestAnimationFrame(() => window.scrollTo(0, event.state.skimReturnY));
    }
  });
}

// 검색 후보는 PR 전체 파일을 대상으로 하고, 입력이 멈춘 뒤 갱신한다.
const fileSearch = typeof document.querySelector === 'function' ? document.querySelector('[data-file-search]') : null;
if (fileSearch) {
  const input = fileSearch.querySelector('input[name="unit_q"]');
  const popup = fileSearch.querySelector('[data-file-suggestions]');
  const empty = fileSearch.querySelector('[data-file-no-match]');
  const options = [...popup.querySelectorAll('[role="option"]')];
  let timer;
  let selected = -1;
  const close = () => {
    popup.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  };
  const show = (term) => {
    const needle = term.toLocaleLowerCase();
    for (const option of options) {
      option.parentElement.hidden = !option.textContent.toLocaleLowerCase().includes(needle);
      option.removeAttribute('aria-selected');
    }
    selected = -1;
    input.removeAttribute('aria-activedescendant');
    empty.hidden = options.some(option => !option.parentElement.hidden);
    popup.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  };
  input.addEventListener('focus', () => { clearTimeout(timer); show(''); });
  input.addEventListener('input', () => {
    clearTimeout(timer);
    selected = -1;
    input.removeAttribute('aria-activedescendant');
    for (const option of options) option.removeAttribute('aria-selected');
    timer = setTimeout(() => { timer = null; show(input.value); }, 250);
  });
  input.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Escape') { clearTimeout(timer); close(); return; }
    const visible = options.filter(option => !option.parentElement.hidden);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (popup.hidden || timer) { clearTimeout(timer); timer = null; show(input.value); }
      const available = options.filter(option => !option.parentElement.hidden);
      if (!available.length) return;
      selected = event.key === 'ArrowDown' ? Math.min(selected + 1, available.length - 1) : (selected <= 0 ? available.length - 1 : selected - 1);
      for (const option of options) option.removeAttribute('aria-selected');
      available[selected].setAttribute('aria-selected', 'true');
      input.setAttribute('aria-activedescendant', available[selected].id);
      available[selected].scrollIntoView({block: 'nearest'});
    } else if (event.key === 'Enter' && !popup.hidden && selected >= 0 && visible[selected]) {
      event.preventDefault();
      window.location.assign(visible[selected].href);
    }
  });
  fileSearch.addEventListener('focusout', event => { if (!fileSearch.contains(event.relatedTarget)) { clearTimeout(timer); close(); } });
  document.addEventListener('click', event => { if (!fileSearch.contains(event.target)) { clearTimeout(timer); close(); } });
}
