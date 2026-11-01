/* 활성 작업을 조회하고, 통신 실패는 최대 30초 간격으로 재시도한다. */
'use strict';
for (const box of document.querySelectorAll('[data-job-active="1"]')) {
  const field = box.querySelector('[data-job-field="summary"]');
  let retryDelay = 2000;
  const tick = () => {
    fetch('/jobs/' + encodeURIComponent(box.dataset.jobId), {credentials: 'same-origin', headers: {Accept: 'application/json'}})
      .then(response => response.ok ? response.json() : Promise.reject(response.status))
      .then(job => {
        retryDelay = 2000;
        field.replaceChildren(...job.summary_parts.map(text => {
          const span = document.createElement('span');
          span.textContent = text;
          return span;
        }));
        if (job.active) setTimeout(tick, 2000);
        else location.reload();
      })
      .catch(() => {
        field.textContent = '상태 조회 실패, 자동 재시도 중';
        setTimeout(tick, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 30000);
      });
  };
  setTimeout(tick, 2000);
}
