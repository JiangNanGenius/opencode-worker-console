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
  let authStatus = null;
  let passkeyBusy = false;
  let statusIssue = '';
  const passkey = document.getElementById('passkey-login');
  const passkeyError = document.getElementById('passkey-error');
  const retry = document.getElementById('login-retry');
  const pt = key => window.WorkerPasskeys.t(key);

  function labels() {
    toggle.textContent = t(password.type === 'password' ? 'auth.show' : 'auth.hide');
    submit.textContent = t(busy ? 'auth.signingIn' : 'auth.signIn');
    if (errorKey) error.textContent = t(errorKey);
    passkey.textContent = pt(passkeyBusy ? 'signingIn' : 'signIn');
    document.getElementById('login-help').textContent = pt(!authStatus ? 'loading' :
      authStatus.allowed_methods.includes('passkey') ? 'publicHelp' : 'lanHelp');
    if (statusIssue) passkeyError.textContent = pt(statusIssue);
  }

  toggle.addEventListener('click', () => {
    const visible = password.type === 'password';
    password.type = visible ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', String(visible));
    labels();
  });
  document.addEventListener('i18n:change', labels);
  document.addEventListener('DOMContentLoaded', labels);

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

  async function status() {
    retry.disabled = true;
    statusIssue = ''; passkeyError.hidden = true; retry.hidden = true;
    try {
      const response = await fetch('/console-api/auth/status', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('status_unavailable');
      const value = await response.json();
      if (!Array.isArray(value.allowed_methods)) throw new Error('invalid_status');
      authStatus = value;
      if (value.authenticated) { window.location.replace('/console'); return; }
      form.hidden = !value.allowed_methods.includes('password');
      passkey.hidden = !value.allowed_methods.includes('passkey');
      passkey.disabled = !value.passkey_available;
      if (!value.passkey_available && !passkey.hidden) {
        statusIssue = 'unavailable'; passkeyError.hidden = false; retry.hidden = false;
      }
    } catch (_) {
      authStatus = null; form.hidden = true; passkey.hidden = true;
      statusIssue = 'connectionError'; passkeyError.hidden = false; retry.hidden = false;
    } finally { retry.disabled = false; labels(); }
  }
  passkey.addEventListener('click', async () => {
    passkeyBusy = true; passkey.disabled = true; statusIssue = ''; passkeyError.hidden = true; labels();
    try { await window.WorkerPasskeys.signIn(); window.location.replace('/console'); }
    catch (e) { statusIssue = window.WorkerPasskeys.errorKey(e); passkeyError.hidden = false; }
    finally { passkeyBusy = false; passkey.disabled = !authStatus?.passkey_available; labels(); }
  });
  retry.addEventListener('click', status);
  labels();
  status();
}());
