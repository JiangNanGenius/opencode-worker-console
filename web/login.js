(function () {
  'use strict';
  const form = document.getElementById('login-form');
  const username = document.getElementById('login-username');
  const password = document.getElementById('login-password');
  const submit = document.getElementById('login-submit');
  const toggle = document.getElementById('toggle-password');
  const error = document.getElementById('login-error');
  const t = (key) => window.I18n.t(key);
  let busy = false;
  let errorKey = '';

  function labels() {
    toggle.textContent = t(password.type === 'password' ? 'auth.show' : 'auth.hide');
    submit.textContent = t(busy ? 'auth.signingIn' : 'auth.signIn');
    if (errorKey) error.textContent = t(errorKey);
  }

  toggle.addEventListener('click', () => {
    const visible = password.type === 'password';
    password.type = visible ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(visible));
    labels();
  });
  document.addEventListener('i18n:change', labels);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    busy = true;
    submit.disabled = true;
    errorKey = '';
    error.hidden = true;
    labels();
    try {
      const response = await fetch('/console-api/auth/login', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: username.value, password: password.value})
      });
      if (!response.ok) {
        errorKey = response.status === 401 ? 'auth.invalid' :
          response.status === 429 ? 'auth.tooMany' : 'auth.unavailable';
        if (response.status === 401) { password.value = ''; password.focus(); }
        throw new Error('login_failed');
      }
      const result = await response.json();
      if (result.authenticated !== true) throw new Error('invalid_response');
      password.value = '';
      window.location.replace('/console');
    } catch (_) {
      errorKey = errorKey || 'auth.unavailable';
      error.textContent = t(errorKey);
      error.hidden = false;
    } finally {
      busy = false;
      submit.disabled = false;
      labels();
    }
  });

  fetch('/console-api/auth/status', {credentials: 'same-origin', cache: 'no-store'})
    .then(response => response.ok ? response.json() : null)
    .then(status => { if (status && status.authenticated) window.location.replace('/console'); })
    .catch(() => {});
}());
