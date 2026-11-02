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
        const summary = JSON.stringify(job.summary_parts);
        if (summary !== lastSummary) {
          field.replaceChildren(...job.summary_parts.map(text => {
            const span = document.createElement('span');
            span.textContent = text;
            return span;
          }));
          lastSummary = summary;
        }
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
          const text = [job.error, job.result].filter(Boolean).join('\n');
          resultText.textContent = text;
          result.hidden = !text;
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
