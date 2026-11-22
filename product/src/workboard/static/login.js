'use strict';
(async function () {
  const clock = document.querySelector('#login-clock');
  function updateClock() {
    const now = new Date();
    clock.dateTime = now.toISOString();
    clock.textContent = new Intl.DateTimeFormat('zh-TW', {timeZone: 'Asia/Taipei', dateStyle: 'short', timeStyle: 'medium', hour12: false}).format(now);
  }
  updateClock();
  setInterval(updateClock, 1000);
  const form = document.querySelector('#login-form');
  const error = document.querySelector('#login-error');
  const button = form.querySelector('button');
  let csrf = '';
  button.disabled = true;
  try {
    const response = await fetch('/api/auth/status', {credentials: 'same-origin', cache: 'no-store'});
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || '登入服務暫時無法使用。');
    if (state.user) { location.replace('/'); return; }
    csrf = state.csrf;
    button.disabled = false;
  } catch (failure) { error.textContent = failure.message; }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    error.textContent = '';
    button.disabled = true;
    try {
      const data = Object.fromEntries(new FormData(form));
      const response = await fetch('/api/auth/login', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(data)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '登入失敗。');
      location.replace('/');
    } catch (failure) { error.textContent = failure.message; button.disabled = false; }
  });
})();
