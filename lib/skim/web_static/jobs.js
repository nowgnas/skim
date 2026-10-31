/* 활성 작업만 2초 간격으로 읽기 전용 조회하고 종료되면 결과를 보이도록 한 번 새로 고친다. */
'use strict';
for (const box of document.querySelectorAll('[data-job-active="1"]')) {
  const field = box.querySelector('[data-job-field="summary"]');
  const tick = () => {
    fetch('/jobs/' + encodeURIComponent(box.dataset.jobId), {credentials: 'same-origin', headers: {Accept: 'application/json'}})
      .then(response => response.ok ? response.json() : Promise.reject(response.status))
      .then(job => {
        field.textContent = job.summary;
        if (job.active) setTimeout(tick, 2000);
        else location.reload();
      })
      .catch(() => { field.textContent += ' · 상태 조회 실패: 페이지를 새로 고치세요'; });
  };
  setTimeout(tick, 2000);
}
