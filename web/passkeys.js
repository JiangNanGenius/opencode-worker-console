(function () {
  'use strict';
  const copy = {
    en: {
      title: 'Passkeys', intro: 'Unlock your device to sign in on the public site. Username and password remain available on the LAN.',
      language: 'Language', loading: 'Checking sign-in options…', retry: 'Try again', manage: 'Manage passkeys',
      firstTitle: 'Connect your first passkey', firstHelp: 'Re-enter your password for a one-use registration link, valid for 10 minutes. Complete device verification on the HTTPS site.',
      password: 'Current password', createLink: 'Create registration link', openLink: 'Open HTTPS registration page',
      addTitle: 'Add a passkey', deviceName: 'Device name', defaultName: 'My passkey', create: 'Verify device and add',
      registerHelp: 'Registration does not sign you in. Use your new passkey to sign in afterward.',
      verifyTitle: 'Verify your identity', verifyHelp: 'Adding or removing passkeys requires passkey verification within the last 5 minutes.',
      verify: 'Verify with a passkey', connected: 'Connected passkeys',
      backupHelp: 'Add a backup passkey on another device. The public site cannot remove your last passkey. If all passkeys are lost, create a registration link using the local recovery command.',
      back: 'Back to sign in', signIn: 'Sign in with a passkey', signingIn: 'Verifying…',
      publicHelp: 'This public site requires a passkey. For your first passkey, create a registration link from the LAN console or use local recovery.',
      lanHelp: 'Sign in with the account configured by your workspace administrator. Manage passkeys after signing in.',
      unavailable: 'The passkey service is unavailable. Try again, or contact your workspace administrator.',
      connectionError: 'Unable to load sign-in options. Check your connection and try again.',
      requestFailed: 'The request could not be verified. Sign in again, or request a new registration link and retry.',
      invalid: 'The password is incorrect. Enter your current password and try again.',
      tooMany: 'Too many attempts. Wait a few minutes and try again.',
      unsupported: 'Open the HTTPS site in a browser that supports passkeys.',
      cancelled: 'Device verification was cancelled or timed out. Try again.',
      ready: 'Your registration link is ready. Select “Verify device and add” to continue.',
      signedIn: 'You are signed in. Passkey changes require recent verification.',
      lanSignIn: 'Sign in to the LAN console first.',
      publicSignIn: 'Sign in with a passkey, or create your first registration link from the LAN console.',
      disabled: 'Passkeys are not enabled for this console.', remove: 'Remove',
      confirmRemove: 'Remove this passkey and end its active sessions?',
      lastKey: 'You cannot remove your last passkey from the public site. Add a backup passkey first.',
      created: 'Created', lastUsed: 'Last used', neverUsed: 'Not used yet', unknownDate: 'Unavailable', empty: 'No passkeys connected yet.',
      linkCreated: 'Registration link created. It expires in 10 minutes and can be used once.',
      registered: 'Passkey added. You can now verify your identity with it.', nameRequired: 'Enter a device name to identify this passkey.'
    },
    zh: {
      title: '通行密钥', intro: '在公网使用设备解锁验证身份。局域网仍可使用用户名与密码。',
      language: '语言', loading: '正在读取登录方式…', retry: '重试', manage: '管理通行密钥',
      firstTitle: '连接你的第一把通行密钥', firstHelp: '再次输入密码，获取仅供注册、10 分钟内有效的一次性链接。在 HTTPS 页面完成设备验证。',
      password: '当前密码', createLink: '创建注册链接', openLink: '打开 HTTPS 注册页面',
      addTitle: '添加通行密钥', deviceName: '设备名称', defaultName: '我的通行密钥', create: '验证设备并添加',
      registerHelp: '注册本身不会登录。完成后，使用刚注册的通行密钥登录。',
      verifyTitle: '验证身份', verifyHelp: '添加或删除密钥前，需要在最近 5 分钟内完成通行密钥验证。',
      verify: '使用通行密钥验证', connected: '已连接的密钥',
      backupHelp: '建议在另一台设备上添加备用密钥。公网无法删除最后一把密钥；全部丢失时请通过本机恢复命令创建注册链接。',
      back: '返回登录', signIn: '使用通行密钥登录', signingIn: '正在验证…',
      publicHelp: '公网仅支持通行密钥登录。首次使用时，请从局域网控制台创建注册链接，或通过本机恢复。',
      lanHelp: '使用管理员配置的账号登录。登录后可管理通行密钥。',
      unavailable: '通行密钥服务暂不可用。请重试，或联系工作区管理员。',
      connectionError: '无法读取登录方式。请检查网络连接后重试。',
      requestFailed: '请求未能通过验证。请重新登录，或获取新的注册链接后重试。',
      invalid: '密码不正确。请重新输入当前密码。', tooMany: '尝试次数过多，请等待几分钟后重试。',
      unsupported: '请在支持通行密钥的浏览器中打开 HTTPS 页面。', cancelled: '设备验证已取消或超时，请重试。',
      ready: '注册链接已就绪。请点击“验证设备并添加”，完成设备验证。',
      signedIn: '已登录。密钥管理需要近期验证。', lanSignIn: '请先在局域网控制台登录。',
      publicSignIn: '请使用通行密钥登录，或从局域网获取首次注册链接。', disabled: '此控制台尚未启用通行密钥。',
      remove: '删除', confirmRemove: '删除这把通行密钥，并结束它的登录会话？',
      lastKey: '公网无法删除最后一把通行密钥。请先添加备用密钥。',
      created: '创建时间', lastUsed: '最近使用', neverUsed: '尚未使用', unknownDate: '暂无记录', empty: '尚未连接通行密钥。',
      linkCreated: '链接已创建，10 分钟内有效，仅可使用一次。', registered: '通行密钥已添加。现在可以使用它验证身份。',
      nameRequired: '请输入设备名称，便于辨认这把密钥。'
    }
  };
  const locale = () => /^zh/i.test(window.I18n?.getLocale() || navigator.language || '') ? 'zh' : 'en';
  const t = key => copy[locale()][key] || copy.en[key] || key;
  const problem = key => Object.assign(new Error(t(key)), {key});
  const errorKey = error => error.name === 'NotAllowedError' ? 'cancelled' : error.key || 'connectionError';
  function labels() {
    document.querySelectorAll('[data-pk-text]').forEach(node => { node.textContent = t(node.dataset.pkText); });
    if (document.getElementById('passkey-manager')) document.title = t('title') + ' · Worker Desk';
  }
  labels();
  document.addEventListener('i18n:change', labels);
  document.addEventListener('DOMContentLoaded', labels);
  const unbase = value => Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  const base = value => btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  async function api(path, body) {
    const response = await fetch('/console-api/auth/' + path, {method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin', cache: 'no-store', headers: {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) {
      const key = response.status === 429 ? 'tooMany' : response.status === 503 ? 'unavailable' :
        response.status === 401 ? 'invalid' : result.error?.startsWith('The last passkey') ? 'lastKey' : 'requestFailed';
      throw problem(key);
    }
    return result;
  }
  function options(value) {
    const result = {...value, challenge: unbase(value.challenge)};
    if (value.user) result.user = {...value.user, id: unbase(value.user.id)};
    for (const key of ['allowCredentials', 'excludeCredentials']) {
      if (value[key]) result[key] = value[key].map(item => ({...item, id: unbase(item.id)}));
    }
    return result;
  }
  function serialize(credential) {
    const response = {};
    for (const key of ['clientDataJSON', 'attestationObject', 'authenticatorData', 'signature', 'userHandle']) {
      if (credential.response[key]) response[key] = base(credential.response[key]);
    }
    if (credential.response.getTransports) response.transports = credential.response.getTransports();
    return {id: credential.id, rawId: base(credential.rawId), type: credential.type, response,
      authenticatorAttachment: credential.authenticatorAttachment,
      clientExtensionResults: credential.getClientExtensionResults()};
  }
  function supported() {
    if (!window.isSecureContext || !window.PublicKeyCredential) throw problem('unsupported');
  }
  async function signIn() {
    supported();
    const request = await api('passkey/authenticate/options', {});
    const credential = await navigator.credentials.get({publicKey: options(request.publicKey)});
    return api('passkey/authenticate/verify', {ceremony_id: request.ceremony_id, credential: serialize(credential)});
  }
  window.WorkerPasskeys = {signIn, t, errorKey};
  const manager = document.getElementById('passkey-manager');
  if (!manager) return;
  // Enrollment material is removed from history before any asynchronous work.
  let grant = new URLSearchParams(location.hash.slice(1)).get('enroll');
  if (location.hash) history.replaceState(null, '', location.pathname);
  const byId = id => document.getElementById(id);
  let lastMessage = 'loading';
  const message = key => { lastMessage = key; byId('pk-message').textContent = t(key); };
  let state, entries = [], nameEdited = false;
  function date(value, emptyKey) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return t(emptyKey);
    return new Intl.DateTimeFormat(locale() === 'zh' ? 'zh-CN' : 'en', {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(value * 1000));
  }
  function renderEntries() {
    byId('pk-list').replaceChildren();
    if (!entries.length) {
      const empty = document.createElement('li'); empty.textContent = t('empty'); byId('pk-list').append(empty);
    }
    for (const entry of entries) {
      const li = document.createElement('li'), label = document.createElement('div'), name = document.createElement('strong'), metadata = document.createElement('span'), remove = document.createElement('button');
      name.textContent = entry.name; metadata.className = 'key-metadata';
      metadata.textContent = t('created') + ': ' + date(entry.created_at, 'unknownDate') + ' · ' + t('lastUsed') + ': ' + date(entry.last_used_at, 'neverUsed');
      label.append(name, metadata); remove.textContent = t('remove'); remove.type = 'button';
      remove.addEventListener('click', () => act(remove, async () => {
        if (!window.confirm(t('confirmRemove'))) return;
        await api('passkeys/delete', {id: entry.id}); await refresh();
      }));
      li.append(label, remove); byId('pk-list').append(li);
    }
  }
  function localized() {
    message(lastMessage);
    if (!nameEdited) byId('pk-name').value = t('defaultName');
    renderEntries();
    const select = manager.querySelector('[data-language-select]');
    if (select) select.setAttribute('aria-label', t('language'));
  }
  document.addEventListener('i18n:change', localized);
  byId('pk-name').addEventListener('input', () => { nameEdited = true; });
  localized();
  async function refresh() {
    byId('pk-retry').hidden = true;
    state = await api('status');
    if (!Array.isArray(state.allowed_methods)) throw problem('connectionError');
    const lan = state.allowed_methods.includes('password');
    byId('pk-lan').hidden = !state.passkey_enabled || !lan || !state.authenticated;
    byId('pk-register').hidden = !state.passkey_enabled || lan || !(grant || state.authenticated);
    byId('pk-signin').hidden = !state.passkey_enabled || lan;
    byId('pk-existing').hidden = !state.passkey_enabled || !state.authenticated;
    message(grant ? 'ready' : state.authenticated ? 'signedIn' : lan ? 'lanSignIn' : 'publicSignIn');
    if (!state.passkey_enabled) message('disabled');
    if (state.passkey_enabled && state.authenticated) {
      const result = await api('passkeys');
      entries = result.passkeys;
      renderEntries();
    }
  }
  async function act(button, operation) {
    button.disabled = true;
    try { await operation(); } catch (error) { message(errorKey(error)); byId('pk-retry').hidden = false; }
    finally { button.disabled = false; }
  }
  byId('pk-grant-form').addEventListener('submit', event => {
    event.preventDefault();
    const password = byId('pk-password').value; byId('pk-password').value = '';
    act(event.submitter || event.currentTarget.querySelector('button'), async () => {
      const result = await api('enrollment-grant', {password});
      byId('pk-enrollment-link').href = result.enrollment_url;
      byId('pk-link-container').hidden = false; message('linkCreated');
    });
  });
  byId('pk-authenticate').addEventListener('click', event => act(event.target, async () => { await signIn(); await refresh(); }));
  byId('pk-create').addEventListener('click', event => act(event.target, async () => {
    supported();
    if (!byId('pk-name').value.trim()) throw problem('nameRequired');
    const request = await api('passkey/register/options', grant ? {grant} : {}); grant = null;
    const credential = await navigator.credentials.create({publicKey: options(request.publicKey)});
    await api('passkey/register/verify', {ceremony_id: request.ceremony_id, credential: serialize(credential), name: byId('pk-name').value});
    await refresh(); message('registered');
  }));
  byId('pk-retry').addEventListener('click', event => act(event.target, refresh));
  refresh().catch(error => { message(errorKey(error)); byId('pk-retry').hidden = false; });
}());
