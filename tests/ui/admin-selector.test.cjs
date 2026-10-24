const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

test('admin enrollment selector changes the real pane and accessible selection', () => {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const start = source.indexOf('function selectEnrollmentMode(mode)');
  const end = source.indexOf('\n}\n\nasync function', start) + 2;
  assert.ok(start >= 0 && end > start, 'production selector helper must be present');

  const makeElement = () => ({
    attributes: {},
    classList: {
      values: new Set(),
      toggle(name, force) { if (force) this.values.add(name); else this.values.delete(name); },
      contains(name) { return this.values.has(name); },
    },
    setAttribute(name, value) { this.attributes[name] = value; },
  });
  const elements = Object.fromEntries(
    ['webcam-pane', 'upload-pane', 'tab-webcam', 'tab-upload'].map(id => [id, makeElement()]),
  );
  let cameraStops = 0;
  const context = {
    $: id => elements[id],
    stopAdminCamera: () => { cameraStops += 1; },
  };
  vm.runInNewContext(source.slice(start, end), context);

  context.selectEnrollmentMode('webcam');
  assert.equal(elements['webcam-pane'].classList.contains('hidden'), false);
  assert.equal(elements['upload-pane'].classList.contains('hidden'), true);
  assert.equal(elements['tab-webcam'].attributes['aria-selected'], 'true');
  assert.equal(elements['tab-upload'].attributes['aria-selected'], 'false');

  context.selectEnrollmentMode('upload');
  assert.equal(elements['webcam-pane'].classList.contains('hidden'), true);
  assert.equal(elements['upload-pane'].classList.contains('hidden'), false);
  assert.equal(elements['tab-webcam'].attributes['aria-selected'], 'false');
  assert.equal(elements['tab-upload'].attributes['aria-selected'], 'true');
  assert.equal(cameraStops, 1);
});

function loadEraseDialog() {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const start = source.indexOf('function eraseConfirmationValid(reason, word)');
  const dialogStart = source.indexOf('function openEraseAccountDialog(user)', start);
  const end = source.indexOf('\n}\n', dialogStart) + 2;
  assert.ok(start >= 0 && dialogStart > start && end > dialogStart, 'erase dialog helpers must be present');
  const field = () => ({ value: '', disabled: false, oninput: null });
  const fields = { '[data-erase-reason]': field(), '[data-erase-word]': field(), '[data-erase-confirm]': field() };
  const dialog = {
    innerHTML: '', returnValue: '', listeners: {}, opened: false,
    querySelector: selector => fields[selector],
    addEventListener(name, handler) { this.listeners[name] = handler; },
    showModal() { this.opened = true; },
  };
  const calls = { api: [], toast: [], show: [] };
  const context = {
    $: id => (id === 'erase-account-dialog' ? dialog : null),
    document: { createElement: () => dialog, body: { appendChild: () => {} } },
    esc: value => String(value), tr: key => key, L: { t: key => key }, accountIdentity: user => user.full_name,
    jsonOptions: (method, body) => ({ method, body }),
    api: async (path, options) => { calls.api.push({ path, options }); return { erased: true }; },
    toast: message => calls.toast.push(message), show: view => calls.show.push(view),
    selectedV3UserId: 7,
  };
  vm.runInNewContext(source.slice(start, end), context);
  context.openEraseAccountDialog({ id: 7, full_name: 'Person' });
  return { context, dialog, fields, calls };
}

test('erase person stays disabled until a reason and the exact word ERASE are entered', async () => {
  const { context, dialog, fields, calls } = loadEraseDialog();
  assert.equal(dialog.opened, true);
  assert.match(dialog.innerHTML, /data-erase-confirm disabled/);
  const reason = fields['[data-erase-reason]'];
  const word = fields['[data-erase-word]'];
  const confirm = fields['[data-erase-confirm]'];
  reason.value = 'subject request'; reason.oninput();
  word.value = 'erase'; word.oninput();
  assert.equal(confirm.disabled, true);
  word.value = 'ERASE'; word.oninput();
  assert.equal(confirm.disabled, false);
  reason.value = '   '; reason.oninput();
  assert.equal(confirm.disabled, true);
  assert.equal(context.eraseConfirmationValid('x', 'ERASE'), true);

  dialog.returnValue = 'confirm';
  await dialog.listeners.close();
  assert.equal(calls.api.length, 0, 'whitespace reason must not call the API');
});

test('confirmed erase calls the V3 endpoint and returns to the refreshed user list', async () => {
  const { context, dialog, fields, calls } = loadEraseDialog();
  fields['[data-erase-reason]'].value = ' subject request ';
  fields['[data-erase-word]'].value = 'ERASE';
  dialog.returnValue = 'confirm';
  await dialog.listeners.close();
  // Objects created inside the vm context have a different prototype, so compare plain JSON.
  assert.deepEqual(JSON.parse(JSON.stringify(calls.api)), [{
    path: '/api/v3/admin/accounts/7',
    options: { method: 'DELETE', body: { reason: 'subject request', confirmation: 'ERASE' } },
  }]);
  assert.deepEqual(calls.toast, ['personErased']);
  assert.deepEqual(calls.show, ['users']);
  assert.equal(context.selectedV3UserId, null);

  const cancelled = loadEraseDialog();
  cancelled.fields['[data-erase-reason]'].value = 'x';
  cancelled.fields['[data-erase-word]'].value = 'ERASE';
  cancelled.dialog.returnValue = 'cancel';
  await cancelled.dialog.listeners.close();
  assert.equal(cancelled.calls.api.length, 0);
});

test('erase person action is on the user card and localized', () => {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const locales = fs.readFileSync('app/static/locales.js', 'utf8');
  assert.match(source, /<button data-user-erase class="danger">\$\{L\.t\('erasePerson'\)\}<\/button>/);
  assert.match(source, /\[data-user-erase\]'\)\.onclick = \(\) => openEraseAccountDialog\(user\)/);
  assert.match(locales, /erasePerson:'Удалить человека'/);
  assert.match(locales, /erasePerson:'Erase person'/);
  assert.match(locales, /CANNOT_ERASE_OWN_ACCOUNT:'Нельзя удалить собственный аккаунт\.'/);
});

test('password reset cancellation bypasses submit validation and reset API', () => {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const start = source.indexOf('function openPasswordResetDialog(userId)');
  const end = source.indexOf('\n}\n\nasync function api', start) + 2;
  const implementation = source.slice(start, end);

  assert.match(implementation, /data-reset-cancel[^>]*>.*?<\/button>/s);
  assert.match(implementation, /<button type="button" data-reset-cancel>/);
  assert.doesNotMatch(implementation, /textarea name="reason" required/);
  assert.match(implementation, /\[data-reset-cancel\]'\)\.onclick = closeDialog/);
  const closeHandler = implementation.slice(
    implementation.indexOf('const closeDialog'),
    implementation.indexOf('form.onsubmit'),
  );
  assert.match(closeHandler, /dialog\.close\(\)/);
  assert.doesNotMatch(closeHandler, /validate|\/reset-password|\bapi\(/);
  assert.match(implementation, /form\.onsubmit[\s\S]*reason\.value\.trim\(\)/);
  assert.match(implementation, /ADMIN_ACTION_REASON_REQUIRED/);
});

test('password reset audit reason is escaped and localized in history/details', () => {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const locales = fs.readFileSync('app/static/locales.js', 'utf8');
  assert.match(source, /esc\(action\.reason\|\|'—'\)/);
  assert.match(source, /eventMetadataDetails\(row\)/);
  assert.match(source, /eventDetailsButton\(\{\.\.\.row,event_type:row\.event\},'audit'\)/);
  assert.match(locales, /administrator:'Администратор'/);
  assert.match(locales, /administrator:'Administrator'/);
  assert.match(locales, /PASSWORD_RESET_BY_ADMIN:'Пароль сброшен администратором'/);
  assert.match(locales, /PASSWORD_RESET_BY_ADMIN:'Password reset by administrator'/);
});

test('event details use an allowlist presentation mapping for action and risk reasons', () => {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const locales = fs.readFileSync('app/static/locales.js', 'utf8');
  assert.match(source, /function eventMetadataDetails\(row\)/);
  assert.match(source, /action_metadata/);
  assert.match(source, /actionReasonLabel/);
  assert.match(source, /resetMode\(data\.mode\)/);
  assert.match(source, /humanBoolean\(data\.must_change_password\)/);
  assert.doesNotMatch(source, /function safeMetadata\(/);
  assert.doesNotMatch(source, /Object\.entries\(data\).*metadata/);
  assert.match(source, /activeEventDetailKey/);
  assert.match(source, /openEventDetails\(activeEventDetailKey\)/);
  for (const code of ['MULTIPLE_FAILED_LOGINS', 'REPEATED_UNKNOWN_FACE', 'REPEATED_BIOMETRIC_FAILURES', 'RECOVERY_ABUSE', 'BLOCKED_ACCOUNT_ATTEMPTS', 'DISABLED_ACCOUNT_ATTEMPTS', 'EXCESSIVE_PASSWORD_RESET']) {
    assert.match(locales, new RegExp(`${code}:`));
  }
  for (const label of ['actionReasonLabel:', 'must_change_password:', 'mode:', "PERMANENT_PASSWORD_REQUIRED:"]) {
    assert.match(locales, new RegExp(label));
  }
});

test('an open Event Details dialog has an accessible live locale control', () => {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const locales = fs.readFileSync('app/static/locales.js', 'utf8');
  assert.match(source, /function bindEventDialogLocale\(\)/);
  assert.match(source, /dialog\.querySelector\('\.dialog-locale'\)/);
  assert.match(source, /data-lang="\$\{lang\}"/);
  assert.match(source, /button\.onclick=\(\)=>L\.set\(button\.dataset\.lang\)/);
  assert.match(source, /bindEventDialogLocale\(\); if \(!\$\('event-detail-dialog'\)\.open\)/);
  assert.match(locales, /b\.setAttribute\('aria-pressed',String\(active\)\)/);
});

test('account identity presentation keeps names, logins, and roles separate', () => {
  const source = fs.readFileSync('app/static/admin.js', 'utf8');
  const v3 = fs.readFileSync('app/static/admin-v3.js', 'utf8');
  const locales = fs.readFileSync('app/static/locales.js', 'utf8');
  const v3Locales = fs.readFileSync('app/static/v3-locales.js', 'utf8');
  const user = fs.readFileSync('app/static/user-v3.js', 'utf8');
  assert.match(source, /function accountIdentity\(row\)/);
  assert.match(source, /function accountIdentityCell\(row\)/);
  assert.match(source, /function eventLabel\(name\).*loginName/);
  assert.match(v3Locales, /window\.BioGateAccountIdentity/);
  assert.match(v3Locales, /\['biogate administrator','biogate user'\]/);
  assert.match(source, /name==='role'\)return humanCode/);
  assert.match(source, /'login','role','timestamp'/);
  assert.match(v3, /function startAccessLive\(\).*accountIdentityCell\(row\)/);
  assert.match(v3, /function loadV3Events\([\s\S]*accountIdentityCell\(row\)/);
  assert.match(user, /BioGateAccountIdentity\(account, V\.t\('unknownUser'\)\)/);
  assert.match(user, /kv\(V\.t\('role'\), V\.t\(profile\.role\)\)/);
  for (const text of ["ADMIN:'Администратор'", "USER:'Пользователь'", "ADMIN:'Administrator'", "USER:'User'"]) {
    assert.match(locales, new RegExp(text));
  }
});

test('informational user cards are static while session revocation remains an explicit action', () => {
  const user = fs.readFileSync('app/static/user-v3.js', 'utf8');
  const userCss = fs.readFileSync('app/static/user-extra.css', 'utf8');
  const locales = fs.readFileSync('app/static/v3-locales.js', 'utf8');

  assert.match(user, /<article class="v3-notification\$\{item\.read_at \? ' is-read' : ' is-unread'\}">/);
  assert.doesNotMatch(user, /data-notification/);
  assert.doesNotMatch(user, /notifications\/\$\{button\.dataset\.notification\}\/read/);
  assert.match(user, /<article class="v3-session">/);
  assert.match(user, /<span class="v3-badge">\$\{V\.t\('currentSession'\)\}<\/span>/);
  assert.match(user, /<button type="button" data-session=/);
  assert.match(userCss, /\.v3-notification,\.v3-session\{[^}]*background:var\(--panel\);color:var\(--ink\)/);
  assert.match(userCss, /\.v3-notification\.is-unread\{border-inline-start:3px solid var\(--accent\)\}/);
  assert.doesNotMatch(userCss, /\.v3-(?:notification|session)[^{]*\{[^}]*cursor:pointer/);
  for (const label of ["read:'Прочитано'", "unread:'Непрочитано'", "read:'Read'", "unread:'Unread'"]) {
    assert.match(locales, new RegExp(label));
  }
});

test('password-change form stays compact beside a long session list', () => {
  const user = fs.readFileSync('app/static/user-v3.js', 'utf8');
  const css = fs.readFileSync('app/static/v3.css', 'utf8');

  assert.match(user, /<form id="password-change" class="v3-card v3-form">/);
  for (const key of ['currentPassword', 'newPassword', 'confirmPassword']) {
    assert.match(user, new RegExp(`<label>\\$\\{V\\.t\\('${key}'\\)\\}<input`));
  }
  assert.match(css, /\.v3-grid>\.v3-form\{align-self:start\}/);
  assert.match(css, /\.v3-form\{align-content:start\}/);
  assert.match(css, /\.v3-form input,\.v3-form select\{min-height:44px;align-self:start\}/);
  assert.doesNotMatch(css, /\.v3-form input[^}]*min-height:(?:[5-9]\d|\d{3,})px/);
});
