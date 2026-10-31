/* 토큰은 저장하지 않고 전송 전에 주소 표시줄에서 제거한다. */
'use strict';
const token = location.hash.slice(1);
history.replaceState(null, '', '/entry');
if (token) {
  fetch('/session', {
    method: 'POST', credentials: 'same-origin', redirect: 'manual',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({token})
  }).then(response => {
    if (response.type === 'opaqueredirect' || response.status === 303) location.replace('/');
    else document.getElementById('status').textContent = '진입 토큰이 만료되었거나 사용되었습니다. skim web으로 새 URL을 받으세요.';
  }).catch(() => { document.getElementById('status').textContent = '인증 연결 실패'; });
} else document.getElementById('status').textContent = 'skim web으로 진입 URL을 받으세요.';
