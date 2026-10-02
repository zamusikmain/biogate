const $ = id => document.getElementById(id);
const L = window.BioGateLocale;
let selectedUser = null;
let adminStream = null;
let adminCameraGeneration = 0;
let selectedUserName = '';
let selectedUserHasBiometrics = false;
let deleteBiometricInFlight = false;
let csrfToken = '';
let adminReady = false;
let accountAuth = false;
let selectedV3UserId = null;
let selectedV3Tab = 'overview';
let usersPage = 0;
let auditPage = 0;
const adminPageSize = 20;
let currentAdminSessionId = '';
let eventDetailCounter = 0;
const eventDetailRows = new Map();

function secureTemporaryPassword() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%';
  const bytes = crypto.getRandomValues(new Uint8Array(18));
  const random = Array.from(bytes, value => alphabet[value % alphabet.length]).join('');
  return `Bg3!aA7${random}`;
}

function showOneTimePassword(password) {
  const dialog = $('temporary-password-dialog');
  $('temporary-password-value').textContent = password;
  let copy = dialog.querySelector('[data-copy-temporary]');
  if (!copy) {
    copy = document.createElement('button');
    copy.type = 'button';
    copy.dataset.copyTemporary = '';
    copy.textContent = L.t('copy');
    dialog.append(copy);
  }
  copy.onclick = async () => {
    await navigator.clipboard.writeText($('temporary-password-value').textContent || '');
    toast('copied');
  };
  dialog.showModal();
}

function openPasswordResetDialog(userId) {
  let dialog = $('admin-password-reset-dialog');
  if (!dialog) {
    dialog = document.createElement('dialog');
    dialog.id = 'admin-password-reset-dialog';
    document.body.append(dialog);
  }
  dialog.innerHTML = `<form class="v3-form password-reset-form">
    <h2>${L.t('resetPassword')}</h2>
    <label>${L.t('passwordMode')}<select name="mode"><option value="TEMPORARY">${L.t('temporaryPassword')}</option><option value="PERMANENT">${L.t('permanentPassword')}</option></select></label>
    <label>${L.t('newPassword')}<span class="password-input-row"><input name="password" type="password" autocomplete="new-password" required><button type="button" data-toggle-password>${L.t('showPassword')}</button></span></label>
    <label data-confirm-row hidden>${L.t('confirmPassword')}<span class="password-input-row"><input name="confirmation" type="password" autocomplete="new-password"><button type="button" data-toggle-confirm>${L.t('showPassword')}</button></span></label>
    <button type="button" data-generate-password>${L.t('generatePassword')}</button>
    <p class="password-requirements">${esc(L.t('passwordRequirements'))}</p>
    <label>${L.t('actionReason')}<textarea name="reason" maxlength="500"></textarea></label>
    <p class="form-error" data-reset-error role="alert"></p>
    <div class="confirm-actions"><button type="button" data-reset-cancel>${L.t('cancel')}</button><button type="submit" class="primary-small">${L.t('resetPassword')}</button></div>
  </form>`;
  const form = dialog.querySelector('form');
  const mode = form.elements.mode;
  const password = form.elements.password;
  const confirmation = form.elements.confirmation;
  const confirmRow = dialog.querySelector('[data-confirm-row]');
  const generate = dialog.querySelector('[data-generate-password]');
  const error = dialog.querySelector('[data-reset-error]');
  let generated = false;
  const syncMode = () => {
    const permanent = mode.value === 'PERMANENT';
    confirmRow.hidden = !permanent;
    confirmation.required = permanent;
    generate.hidden = permanent;
    password.value = '';
    confirmation.value = '';
    generated = false;
    error.textContent = '';
  };
  const toggle = (input, button) => { input.type = input.type === 'password' ? 'text' : 'password'; button.textContent = L.t(input.type === 'password' ? 'showPassword' : 'hidePassword'); };
  mode.onchange = syncMode;
  dialog.querySelector('[data-toggle-password]').onclick = event => toggle(password, event.currentTarget);
  dialog.querySelector('[data-toggle-confirm]').onclick = event => toggle(confirmation, event.currentTarget);
  generate.onclick = () => { password.value = secureTemporaryPassword(); generated = true; error.textContent = ''; };
  const closeDialog = () => { error.textContent = ''; dialog.close(); };
  dialog.querySelector('[data-reset-cancel]').onclick = closeDialog;
  form.onsubmit = async event => {
    event.preventDefault();
    error.textContent = '';
    const reason = form.elements.reason.value.trim();
    if (!reason) { error.textContent = L.t('ADMIN_ACTION_REASON_REQUIRED'); return; }
    const policyError = window.BioGatePasswordPolicy.validate(password.value);
    if (policyError) { error.textContent = L.t(policyError); return; }
    if (mode.value === 'PERMANENT' && password.value !== confirmation.value) { error.textContent = L.t('PASSWORD_CONFIRMATION_MISMATCH'); return; }
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      const payload = mode.value === 'TEMPORARY'
        ? { mode: mode.value, temporary_password: password.value, generate: false, reason }
        : { mode: mode.value, new_password: password.value, confirmation: confirmation.value, generate: false, reason };
      await api(`/api/v3/admin/accounts/${userId}/reset-password`, jsonOptions('POST', payload));
      const oneTimePassword = generated ? password.value : '';
      dialog.close();
      if (oneTimePassword) showOneTimePassword(oneTimePassword); else toast('passwordResetComplete');
    } catch (requestError) {
      error.textContent = L.t(requestError.code || requestError.message);
    } finally { submit.disabled = false; }
  };
  syncMode();
  dialog.showModal();
}

async function api(path, options = {}) {
  const requestOptions = { ...options };
  const method = (requestOptions.method || 'GET').toUpperCase();
  const headers = new Headers(requestOptions.headers || {});
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken);
  requestOptions.headers = headers;
  const response = await fetch(path, requestOptions);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      location.href = '/?reason=session_expired';
      throw new Error('SESSION_EXPIRED');
    }
    const fallback = {403:'FORBIDDEN',404:'NOT_FOUND',409:'CONFLICT',422:'VALIDATION_FAILED',500:'SERVER_ERROR',503:'DATABASE_TEMPORARILY_UNAVAILABLE'}[response.status] || 'REQUEST_FAILED';
    const reason = body.detail?.reason_code || (typeof body.detail === 'string' ? body.detail : fallback);
    const error = new Error(reason);
    error.code = reason;
    error.status = response.status;
    throw error;
  }
  return body;
}

function toast(message) {
  const element = $('toast');
  element.textContent = L.t(message);
  element.classList.add('show');
  setTimeout(() => element.classList.remove('show'), 3000);
}

function tr(key, values = {}) {
  return Object.entries(values).reduce((text, [name, value]) => text.replaceAll(`{${name}}`, value), L.t(key));
}

function normalizeAdminView(view) { const map={ 'security-events':'events',audit:'events','unknown-faces':'access',live:'access',archive:'access',attempts:'access','verification-attempts':'access' }; if(view==='unknown-faces')accessFilter.result='UNKNOWN'; return map[view]||view; }
function show(view) {
  view=normalizeAdminView(view);
  document.querySelectorAll('.admin-view').forEach(element => element.classList.toggle('active', element.id === view));
  document.querySelectorAll('.admin-nav button').forEach(element => element.classList.toggle('active', element.dataset.view === view));
  $('admin-title').textContent = L.t(view === 'create' ? 'createUser' : view);
  history.replaceState({}, '', view === 'dashboard' ? '/admin' : `/admin#${view}`);
  if (view === 'dashboard') loadDashboard();
  if (view === 'users') loadUsers();
  if (view === 'attempts') loadAttempts();
  if (view === 'audit') loadAudit();
  if (view === 'settings') loadSettings();
  if (window.loadBioGateV3Admin) window.loadBioGateV3Admin(view).catch(error => toast(error.code || error.message));
}

async function loadDashboard() {
  if (accountAuth) {
    const data = await api('/api/v3/admin/dashboard');
    const stats = data.metrics;
    const items = [
      ['totalUsers', stats.total_users], ['active', stats.active_users], ['blocked', stats.blocked_users],
      ['disabled', stats.disabled_users], ['enrolledUsers', stats.enrolled_users],
      ['successful', stats.successful_today], ['rejectedCount', stats.denied_today],
      ['unknown', stats.unknown_today], ['pendingBiometricRequests',stats.pending_biometric_requests], ['securityEvents', stats.security_alerts],
    ];
    $('admin-stats').innerHTML = items.map(([key, value]) => `<div class="metric-card"><span>${L.t(key)}</span><strong>${value || 0}</strong></div>`).join('');
    table('recent-events', [L.t('date'),L.t('photo'), L.t('user'), L.t('method'), L.t('result')], data.recent_access.map(event => [date(event.timestamp),event.photo_id?evidenceThumbnail(event.photo_id):L.t('photoAbsent'),accountIdentityCell(event), esc(humanMethod(event.method)), badge(event.result)]));bindEvidenceErrors();document.querySelectorAll('[data-access-photo]').forEach(button=>button.onclick=()=>openPhotoViewer([button.dataset.accessPhoto],0));
    const health = await api('/api/v3/admin/system-health');
    $('system-health').innerHTML = `<div class="metric-grid">${Object.entries(health.components).map(([name, value]) => `<div class="metric-card"><span>${esc(name)}</span><strong>${badge(value)}</strong></div>`).join('')}</div><p>${L.t('activeStorage')}: ${health.storage.active.files} · ${Math.round(health.storage.active.size_bytes / 1024)} KB &nbsp; ${L.t('archive')}: ${health.storage.archive.files} · ${Math.round(health.storage.archive.size_bytes / 1024)} KB</p>`;
    return;
  }
  const data = await api('/api/admin/dashboard');
  const stats = data.stats;
  const items = [
    ['totalUsers', stats.total_users], ['enrolledUsers', stats.enrolled_users],
    ['todayChecks', stats.verifications_today || 0], ['successful', stats.successful_verifications || 0],
    ['rejectedCount', stats.rejected_verifications || 0], ['avgInference', `${fmt(stats.average_inference_time_ms)} ms`],
    ['avgSimilarity', pct(stats.average_success_similarity)], ['livenessFailures', stats.liveness_failures || 0],
    ['qualityFailures', stats.quality_failures || 0],
  ];
  $('admin-stats').innerHTML = items.map(([key, value]) => `<div class="metric-card"><span>${L.t(key)}</span><strong>${value}</strong></div>`).join('');
  table('recent-events', [L.t('date'), L.t('event'), L.t('result')], data.recent_events.map(event => [date(event.timestamp), esc(event.event_type), badge(event.result)]));
}

async function loadUsers() {
  if (accountAuth) {
    $('users-list').innerHTML = `<div class="data-card">${L.t('loading')}</div>`;
    const search = $('user-search')?.value || '';
    const status = $('user-status-filter')?.value || '';
    const role = $('user-role-filter')?.value || '';
    const biometric = $('user-biometric-filter')?.value || '';
    const query = new URLSearchParams({ limit: String(adminPageSize), offset: String(usersPage * adminPageSize) });
    if (search) query.set('search', search);
    if (status) query.set('status', status);
    if (role) query.set('role', role);
    if (biometric) query.set('biometric', biometric);
    const users = await api(`/api/v3/admin/accounts?${query}`);
    table('users-list', ['External ID', L.t('displayName'), L.t('role'), L.t('status'), L.t('biometrics'), L.t('actions')], users.map(user => [
      esc(user.external_id), accountIdentityCell(user), badge(user.role), badge(user.status), user.biometric_template_id ? badge('ENROLLED') : '—',
      `<button data-open-account="${user.id}">${L.t('details')}</button> <button data-account-status="${user.id}" data-next-status="${user.status === 'ACTIVE' ? 'BLOCKED' : 'ACTIVE'}">${L.t(user.status === 'ACTIVE' ? 'block' : 'unblock')}</button> <button data-account-reset="${user.id}">${L.t('resetPassword')}</button>`,
    ]));
    renderAdminPager('users-list', usersPage, users.length, page => { usersPage = page; loadUsers(); });
    document.querySelectorAll('[data-open-account]').forEach(button => button.onclick = event => { event.stopPropagation(); openV3User(Number(button.dataset.openAccount)); });
    document.querySelectorAll('[data-account-status]').forEach(button => button.onclick = async event => {
      event.stopPropagation();
      const reason = await adminAsk({title:L.t(button.dataset.nextStatus==='BLOCKED'?'block':'unblock'),label:L.t('actionReason'),required:true,confirmKey:'save',danger:button.dataset.nextStatus==='BLOCKED'}); if (reason===null) return;
      await api(`/api/v3/admin/accounts/${button.dataset.accountStatus}`, jsonOptions('PATCH', { status: button.dataset.nextStatus, reason }));
      await loadUsers();
    });
    document.querySelectorAll('[data-account-reset]').forEach(button => button.onclick = event => { event.stopPropagation(); openPasswordResetDialog(Number(button.dataset.accountReset)); });
    localizePresentationOptions();
    return;
  }
  const users = await api('/api/admin/users');
  table('users-list', ['User ID', L.t('displayName'), L.t('status'), L.t('biometrics'), L.t('created')], users.map(user => [
    `<a href="/admin/users/${encodeURIComponent(user.external_id)}">${esc(user.external_id)}</a>`,
    esc(user.display_name), badge(user.status), user.enrolled_at ? badge('ENROLLED') : '—', date(user.created_at),
  ]));
}

async function openV3User(id, tab = 'overview') {
  selectedV3UserId = id;
  selectedV3Tab = tab;
  selectedUser = id;
  const user = await api(`/api/v3/admin/accounts/${id}`);
  showDirect('user-detail', accountIdentity(user));
  history.replaceState({}, '', `/admin#users/${id}/${tab}`);
  const tabs = ['overview', 'biometrics', 'access', 'security', 'sessions', 'history', 'notes'];
  $('user-card').innerHTML = `<button type="button" data-back-users>← ${L.t('users')}</button><div class="detail-hero"><div><h2>${esc(accountIdentity(user))}</h2><p>${esc(user.login)} · ${badge(user.role)} ${badge(user.status)}</p></div><div class="detail-actions"><button data-user-edit>${L.t('edit')}</button><button data-user-reset>${L.t('resetPassword')}</button></div></div><div class="detail-tabs">${tabs.map(name => `<button data-user-tab="${name}" class="${name === tab ? 'active' : ''}">${L.t(name)}</button>`).join('')}</div><div id="v3-user-tab"></div>`;
  document.querySelector('[data-back-users]').onclick = () => show('users');
  document.querySelectorAll('[data-user-tab]').forEach(button => button.onclick = () => openV3User(id, button.dataset.userTab));
  document.querySelector('[data-user-reset]').onclick = () => openPasswordResetDialog(id);
  document.querySelector('[data-user-edit]').onclick = () => renderV3UserEdit(user, tab);
  await renderV3UserTab(user, tab);
}

function renderV3UserEdit(user, tab) {
  $('v3-user-tab').innerHTML = `<form id="v3-user-edit" class="v3-form"><input name="full_name" value="${esc(user.full_name)}" required><input name="login" value="${esc(user.login)}" required><input name="employee_id" value="${esc(user.employee_id || '')}" placeholder="Employee ID"><input name="department" value="${esc(user.department || '')}" placeholder="${L.t('department')}"><input name="position" value="${esc(user.position || '')}" placeholder="${L.t('position')}"><button class="primary-small">${L.t('save')}</button></form>`;
  $('v3-user-edit').onsubmit = async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); await api(`/api/v3/admin/accounts/${user.id}`, jsonOptions('PATCH', values)); toast('settingsSaved'); await openV3User(user.id, tab); };
}

async function renderV3UserTab(user, tab) {
  const target = $('v3-user-tab');
  if (tab === 'overview') target.innerHTML = `<div class="profile-grid">${field(L.t('displayName'), accountIdentity(user))}${field(L.t('loginName'), user.login)}${field(L.t('role'), humanCode(user.role))}${field(L.t('employeeId'), user.employee_id || '—')}${field(L.t('department'), user.department || '—')}${field(L.t('position'), user.position || '—')}${field(L.t('created'), date(user.created_at))}${field(L.t('lastLogin'), date(user.last_login_at))}${field(L.t('loginMethod'), humanMethod(user.last_login_method)||'—')}</div>`;
  else if (tab === 'access') { const rows = await api(`/api/v3/admin/accounts/${user.id}/access-events`); table('v3-user-tab', [L.t('time'), L.t('method'), L.t('result'), L.t('reason')], rows.map(row => [date(row.timestamp), esc(humanMethod(row.method)), badge(row.result), esc(humanCode(row.reason))])); }
  else if (tab === 'security') { const rows = await api(`/api/v3/admin/accounts/${user.id}/security-events`); target.innerHTML = `<div class="v3-card"><label><input id="account-password-enabled" type="checkbox" ${user.password_enabled ? 'checked' : ''}> ${L.t('allowPasswordLogin')}</label><label><input id="account-face-enabled" type="checkbox" ${user.face_enabled ? 'checked' : ''}> ${L.t('allowFaceLogin')}</label><label><input id="account-recovery-enabled" type="checkbox" ${user.biometric_recovery_enabled ? 'checked' : ''}> ${L.t('biometricRecovery')}</label><button id="save-account-security" class="primary-small">${L.t('save')}</button></div><div id="security-list"></div>`; table('security-list', [L.t('time'), L.t('event'), L.t('severity')], rows.map(row => [date(row.timestamp), esc(humanCode(row.event_type)), badge(row.severity)])); $('save-account-security').onclick = async () => { await api(`/api/v3/admin/accounts/${user.id}`, jsonOptions('PATCH', { password_enabled: $('account-password-enabled').checked, face_enabled: $('account-face-enabled').checked, biometric_recovery_enabled: $('account-recovery-enabled').checked })); toast('settingsSaved'); await openV3User(user.id, tab); }; }
  else if (tab === 'sessions') { const rows = await api(`/api/v3/admin/accounts/${user.id}/sessions`); table('v3-user-tab', [L.t('device'), L.t('created'), L.t('lastLogin'), L.t('loginMethod'), L.t('status'), L.t('actions')], rows.map(row => [sessionDevice(row.user_agent)+(row.id===currentAdminSessionId?` · ${L.t('currentSession')}`:''), date(row.created_at), date(row.last_seen_at), esc(humanMethod(row.login_method)), row.revoked_at ? badge('REVOKED') : badge('ACTIVE'), row.revoked_at ? '—' : `<button data-revoke-session="${esc(row.id)}">${L.t('revoke')}</button>`])); document.querySelectorAll('[data-revoke-session]').forEach(button => button.onclick = async () => { if((await adminAsk({title:L.t('revoke'),message:L.t('confirmRevoke'),confirmKey:'revoke',danger:true}))===null)return; await api(`/api/v3/admin/accounts/${user.id}/sessions/${button.dataset.revokeSession}`, { method: 'DELETE' }); await openV3User(user.id, tab); }); }
  else if (tab === 'history') { const rows = await api(`/api/v3/admin/accounts/${user.id}/timeline`); table('v3-user-tab', [L.t('time'), L.t('event'), L.t('administrator'), L.t('actionReasonLabel'), L.t('mode'), L.t('result'), L.t('actions')], rows.map(row => { const metadata=eventMetadata(row.action_metadata); const own=eventMetadata(row.metadata); const action=Object.keys(metadata).length?metadata:row.event==='PASSWORD_RESET_BY_ADMIN'?own:{}; return [date(row.timestamp), esc(humanCode(row.event)), esc(action.administrator||'—'), esc(action.reason||'—'), esc(action.mode?resetMode(action.mode):'—'), badge(row.result), row._category==='audit'?eventDetailsButton({...row,event_type:row.event},'audit'):'—']; })); bindEventDetailButtons(); }
  else if (tab === 'notes') { const rows = await api(`/api/v3/admin/accounts/${user.id}/notes`); target.innerHTML = `<form id="note-form" class="v3-form"><textarea name="text" required></textarea><button class="primary-small">${L.t('save')}</button></form><div id="notes-list">${rows.map(row => `<div class="data-card">${accountIdentityCell(row)}<time>${date(row.created_at)}</time><p>${esc(row.text)}</p></div>`).join('')}</div>`; $('note-form').onsubmit = async event => { event.preventDefault(); await api(`/api/v3/admin/accounts/${user.id}/notes`, jsonOptions('POST', Object.fromEntries(new FormData(event.target)))); await openV3User(user.id, tab); }; }
  else if (tab === 'biometrics') {
    const summary = await api(`/api/v3/admin/accounts/${user.id}/biometric`);
    const request = summary.request;
    const requestActions = request?.status === 'PENDING_REVIEW' ? `<h3>${L.t('availableActions')}</h3><div class="detail-actions"><button data-card-bio-review="APPROVED">${L.t('approve')}</button><button data-card-bio-review="REVISION_REQUIRED">${L.t('revision')}</button><button data-card-bio-review="REJECTED">${L.t('reject')}</button></div>` : request?.status === 'REVISION_REQUIRED' ? `<div class="info-state">${L.t('waitingForBiometricResubmission')}</div>` : request ? `<div class="info-state">${L.t('requestIsReadOnly')}</div>` : '';
    const requestCard = request ? `<div class="v3-card"><h3>${L.t('biometricRequest')}</h3>${field(L.t('status'),humanCode(request.status,'status'))}${field(L.t('created'),date(request.created_at))}${field(L.t('updated'),date(request.updated_at))}${field(L.t('similarity'),pct(request.duplicate_similarity))}${field(L.t('adminComment'),request.admin_comment||'—')}<h3>${L.t('submittedPhotos')}</h3>${requestGallery(summary.photos)}${requestActions}</div>` : '';
    target.innerHTML = `<div class="v3-card"><p>${summary.template ? badge('ENROLLED') : badge('NOT_ENROLLED')}</p>${summary.template ? `${field(L.t('enrollmentDate'),date(summary.template.created_at))}${field(L.t('sampleCount'),summary.template.sample_count)}` : ''}<button id="manage-v3-biometric" class="primary-small">${L.t(summary.template ? 'updateBiometric' : 'addBiometric')}</button> <button id="remove-v3-biometric" class="danger" ${summary.template ? '' : 'disabled'}>${L.t('deleteBiometric')}</button></div>${requestCard}<div class="v3-card"><h3>${L.t('history')}</h3>${summary.history.length?summary.history.map(row=>`<div>${date(row.timestamp)} · ${esc(row.event_type)}</div>`).join(''):`<p>${L.t('noData')}</p>`}</div>`;
    $('manage-v3-biometric').onclick = () => openEnrollment(user.external_id, Boolean(summary.template));
    bindRequestGallery(target);
    document.querySelectorAll('[data-card-bio-review]').forEach(button => button.onclick = async () => { const decision=button.dataset.cardBioReview; const comment=await adminAsk({title:L.t(decision),message:L.t('reviewDecisionHint'),label:decision==='APPROVED'?'':L.t('adminComment'),required:decision!=='APPROVED',confirmKey:decision==='APPROVED'?'approve':'save',danger:decision==='REJECTED'});if(comment===null)return; button.disabled=true; try { await api(`/api/v3/admin/biometric-requests/${request.id}/review`,jsonOptions('POST',{decision,comment})); await openV3User(user.id,'biometrics'); } finally { button.disabled=false; } });
  }
  if (tab === 'biometrics' && $('remove-v3-biometric') && !$('remove-v3-biometric').disabled) $('remove-v3-biometric').onclick = async () => { const reason=await adminAsk({title:L.t('deleteBiometric'),message:L.t('confirmDeleteBiometric').replace('{name}',user.full_name),label:L.t('actionReason'),required:true,confirmKey:'deleteBiometric',danger:true});if(reason===null)return; await api(`/api/v3/admin/accounts/${user.id}/biometric`, jsonOptions('DELETE', { reason })); await openV3User(user.id, tab); };
}

async function openUser(id) {
  selectedUser = id;
  const [user, historyRows] = await Promise.all([
    api(`/api/admin/users/${encodeURIComponent(id)}`),
    api(`/api/admin/users/${encodeURIComponent(id)}/verifications`),
  ]);
  const hasBiometrics = Boolean(user.biometric_template_id);
  selectedUserName = user.display_name;
  selectedUserHasBiometrics = hasBiometrics;
  showDirect('user-detail', user.display_name);
  const biometricActions = hasBiometrics
    ? `<button data-action="enroll" class="primary-small">${L.t('updateBiometric')}</button><button data-action="delete-bio">${L.t('deleteBiometric')}</button>`
    : `<button data-action="enroll" class="primary-small">＋ ${L.t('addBiometric')}</button>`;
  const biometricDetails = hasBiometrics
    ? `${field(L.t('biometricStatus'), L.t('biometricRegistered'))}${field(L.t('enrollmentDate'), date(user.biometric_created_at || user.enrolled_at))}${field(L.t('model'), user.model_name)}${field(L.t('modelVersion'), user.model_version)}${field(L.t('sampleCount'), user.sample_count)}`
    : `${field(L.t('biometricStatus'), L.t('biometricNotRegistered'))}`;
  const emptyState = hasBiometrics ? '' : `<div class="biometric-empty"><h3>${L.t('biometricNotRegistered')}</h3><p>${L.t('biometricRequired')}</p><button data-action="empty-enroll" class="primary-small">＋ ${L.t('addBiometric')}</button></div>`;
  $('user-card').innerHTML = `
    <div class="detail-hero"><div><span class="section-label">USER / ${esc(user.external_id)}</span><h2>${esc(user.display_name)}</h2><p>${badge(user.status)} ${hasBiometrics ? badge('ENROLLED') : ''}</p></div>
    <div class="detail-actions"><button data-action="edit">${L.t('edit')}</button><button data-action="toggle">${L.t(user.status === 'BLOCKED' ? 'unblock' : 'block')}</button>${biometricActions}<button data-action="delete-user">${L.t('deleteUser')}</button></div></div>
    <div class="profile-grid">${field('User ID', user.external_id)}${field(L.t('displayName'), user.display_name)}${field(L.t('status'), user.status)}${field(L.t('created'), date(user.created_at))}${biometricDetails}${field(L.t('lastVerification'), user.last_verification ? date(user.last_verification) : '—')}${field(L.t('successful'), user.successful || 0)}${field(L.t('rejectedCount'), user.rejected || 0)}</div>
    ${emptyState}
    <div class="data-card"><div class="card-heading"><h2>${L.t('history')}</h2><select id="history-filter"><option value="">${L.t('all')}</option><option value="VERIFIED">${L.t('successful')}</option><option value="REJECTED">${L.t('rejectedCount')}</option><option value="LIVENESS_FAILED">${L.t('livenessFailure')}</option><option value="IMAGE_BLURRY">${L.t('qualityFailure')}</option><option value="FACE_MISMATCH">${L.t('faceMismatch')}</option></select></div><div id="user-history"></div></div>`;
  renderHistory(historyRows);
  document.querySelector('[data-action="edit"]').onclick = async () => {
    const name = await adminAsk({title:L.t('edit'),label:L.t('displayName'),required:true,value:user.display_name,confirmKey:'save'});
    if (name) { await api(`/api/admin/users/${encodeURIComponent(id)}`, jsonOptions('PATCH', { display_name: name })); openUser(id); }
  };
  document.querySelector('[data-action="toggle"]').onclick = async () => {
    await api(`/api/admin/users/${encodeURIComponent(id)}`, jsonOptions('PATCH', { status: user.status === 'BLOCKED' ? 'ACTIVE' : 'BLOCKED' })); openUser(id);
  };
  document.querySelector('[data-action="enroll"]').onclick = () => openEnrollment(id, hasBiometrics);
  if (document.querySelector('[data-action="empty-enroll"]')) document.querySelector('[data-action="empty-enroll"]').onclick = () => openEnrollment(id, false);
  if (document.querySelector('[data-action="delete-bio"]')) document.querySelector('[data-action="delete-bio"]').onclick = () => openDeleteBiometric(id, user.display_name);
  document.querySelector('[data-action="delete-user"]').onclick = async () => {
    if((await adminAsk({title:L.t('deleteUser'),message:L.t('confirmDeleteUser'),confirmKey:'deleteUser',danger:true}))!==null){await api(`/api/admin/users/${encodeURIComponent(id)}`, { method: 'DELETE' }); location.href = '/admin#users';}
  };
  $('history-filter').onchange = async event => {
    const value = event.target.value;
    const query = value === 'VERIFIED' || value === 'REJECTED' ? `?result=${value}` : value ? `?reason=${value}` : '';
    renderHistory(await api(`/api/admin/users/${encodeURIComponent(id)}/verifications${query}`));
  };
}

function renderHistory(rows) {
  table('user-history', [L.t('time'), L.t('result'), L.t('similarity'), L.t('threshold'), L.t('quality'), L.t('liveness'), L.t('latency'), L.t('reason')], rows.map(row => [
    `<a href="/admin/verifications/${row.id}">${date(row.timestamp)}</a>`, badge(row.result), pct(row.similarity_score), pct(row.threshold), pct(row.quality_score), pct(row.liveness_score), `${fmt(row.inference_time_ms)} ms`, esc(humanCode(row.reason_code)),
  ]));
}

async function loadAttempts() {
  const params = new URLSearchParams();
  [['filter-user', 'user'], ['filter-result', 'result'], ['filter-live', 'liveness'], ['filter-reason', 'reason'], ['filter-from', 'date_from'], ['filter-to', 'date_to']].forEach(([id, key]) => { if ($(id)?.value) params.set(key, $(id).value); });
  const rows = await api(`/api/admin/verifications?${params}`);
  table('attempts-list', [L.t('time'), L.t('user'), L.t('result'), L.t('similarity'), L.t('liveness'), L.t('quality'), L.t('reason'), L.t('latency')], rows.map(row => [
    `<a href="/admin/verifications/${row.id}">${date(row.timestamp)}</a>`, esc(row.claimed_external_id||L.t('unknownUser')), badge(row.result), pct(row.similarity_score), pct(row.liveness_score), pct(row.quality_score), esc(humanCode(row.reason_code)), `${fmt(row.inference_time_ms)} ms`,
  ]));
}

async function openAttempt(id) {
  const attempt = await api(`/api/admin/verifications/${id}`);
  showDirect('attempt-detail', `Attempt #${id}`);
  const steps = parseJson(attempt.challenge_steps);
  $('attempt-card').innerHTML = `<div class="detail-hero"><div><span class="section-label">VERIFICATION / #${attempt.id}</span><h2>${badge(attempt.result)} ${esc(attempt.claimed_external_id)}</h2><p>${date(attempt.timestamp)}</p></div></div>
    <div class="profile-grid">${field(L.t('faceDetectedLabel'), attempt.face_detected ? humanCode('SUCCESS') : humanCode('FAILED'))}${field(L.t('quality'), pct(attempt.quality_score))}${field(L.t('liveness'), pct(attempt.liveness_score))}${field(L.t('similarity'), pct(attempt.similarity_score))}${field(L.t('threshold'), pct(attempt.threshold))}${field(L.t('latency'), `${fmt(attempt.inference_time_ms)} ms`)}${field(L.t('reason'), humanCode(attempt.reason_code))}${field(L.t('model'), attempt.model_name ? `${attempt.model_name}-${attempt.model_version}` : '—')}${field(L.t('challenge'), Array.isArray(steps) ? steps.map(step=>humanCode(step)).join(' → ') : L.t('legacyFlow'))}${field(L.t('challengeResult'), humanCode(attempt.challenge_result) || '—')}</div>
    ${attempt.snapshot_id ? `<div class="data-card"><h2>${L.t('attemptSnapshot')}</h2><p class="warning">${L.t('snapshotWarning')}</p><img class="snapshot" src="/api/admin/verifications/${attempt.id}/snapshot"><button id="delete-snapshot">${L.t('deleteSnapshot')}</button></div>` : ''}<div class="data-card"><h2>${L.t('auditLog')}</h2><div id="attempt-audit"></div></div>`;
  table('attempt-audit', [L.t('date'), L.t('event'), L.t('result')], attempt.audit_events.map(event => [date(event.timestamp), esc(humanCode(event.event_type)), badge(event.result)]));
  if ($('delete-snapshot')) $('delete-snapshot').onclick = async () => { await api(`/api/admin/verifications/${id}/snapshot`, { method: 'DELETE' }); openAttempt(id); };
}

async function loadAudit() {
  $('audit-list').innerHTML = `<div class="data-card">${L.t('loading')}</div>`;
  const rows = await api(`/api/admin/audit?limit=${adminPageSize}&offset=${auditPage * adminPageSize}`);
  table('audit-list', [L.t('date'), L.t('event'), L.t('user'), L.t('result'), L.t('details'), L.t('actions')], rows.map(event => [date(event.timestamp), esc(humanCode(event.event_type)), accountIdentityCell(event), badge(event.result), eventMetadataDetails(event)||'—', eventDetailsButton(event, 'audit')]));
  renderAdminPager('audit-list', auditPage, rows.length, page => { auditPage = page; loadAudit(); });
  bindEventDetailButtons();
}

async function loadSettings() { $('store-snapshots').checked = (await api('/api/admin/settings')).store_attempt_images; }
function openEnrollment(id, hasBiometrics = selectedUserHasBiometrics) {
  selectedUser = id;
  selectedUserHasBiometrics = hasBiometrics;
  $('photo-results').innerHTML = '';
  $('photo-files').value = '';
  $('webcam-progress').textContent = L.t('webcamHint');
  $('enroll-title').textContent = L.t(hasBiometrics ? 'updateBiometric' : 'addBiometric');
  selectEnrollmentMode('upload');
  $('enroll-dialog').showModal();
}

function selectEnrollmentMode(mode) {
  const webcamSelected = mode === 'webcam';
  $('webcam-pane').classList.toggle('hidden', !webcamSelected);
  $('upload-pane').classList.toggle('hidden', webcamSelected);
  $('tab-webcam').classList.toggle('active', webcamSelected);
  $('tab-upload').classList.toggle('active', !webcamSelected);
  $('tab-webcam').setAttribute('aria-selected', String(webcamSelected));
  $('tab-upload').setAttribute('aria-selected', String(!webcamSelected));
  if (!webcamSelected) stopAdminCamera();
}

function openDeleteBiometric(id, name) {
  selectedUser = id;
  selectedUserName = name;
  $('delete-biometric-message').textContent = tr('confirmDeleteBiometric', { name });
  $('delete-biometric-dialog').showModal();
}

async function confirmDeleteBiometric() {
  if (deleteBiometricInFlight || !selectedUser) return;
  deleteBiometricInFlight = true;
  const button = $('confirm-delete-biometric');
  button.disabled = true;
  button.textContent = L.t('deleting');
  try {
    await api(`/api/admin/users/${encodeURIComponent(selectedUser)}/biometric`, { method: 'DELETE' });
    $('delete-biometric-dialog').close();
    toast('biometricDeleted');
    await openUser(selectedUser);
  } catch (error) {
    toast(error.code || error.message);
  } finally {
    deleteBiometricInFlight = false;
    button.disabled = false;
    button.textContent = L.t('deleteBiometric');
  }
}

async function sendPhotos(files) {
  const list = [...files];
  if (list.length < 3 || list.length > 5) return toast('INVALID_FRAME_COUNT');
  const button = $('submit-photos');
  button.disabled = true;
  const form = new FormData();
  list.forEach(file => form.append('photos', file, file.name));
  try {
    const data = await api(`/api/admin/users/${encodeURIComponent(selectedUser)}/enrollment/photos`, { method: 'POST', body: form });
    $('photo-results').innerHTML = `<div class="upload-results">${data.results.map(result => `<div class="${result.accepted ? 'accepted' : 'rejected'}"><strong>${esc(result.filename)}</strong><span>${result.accepted ? `✓ ${L.t('photoAccepted')} · ${pct(result.quality_score)}` : L.t(result.reason_code)}</span></div>`).join('')}</div><p>${tr('validPhotos', { accepted: data.accepted_frames, required: data.required_frames })}</p>`;
    if (data.template_created) {
      stopAdminCamera();
      $('enroll-dialog').close();
      toast(selectedUserHasBiometrics ? 'biometricUpdated' : 'biometricEnrolled');
      if (accountAuth && selectedV3UserId) await openV3User(selectedV3UserId, selectedV3Tab);
      else await openUser(selectedUser);
    }
  } catch (error) {
    toast(error.code || error.message);
  } finally {
    button.disabled = false;
  }
}

async function webcamEnroll() {
  const button = $('start-webcam-enroll');
  if (button.disabled) return;
  stopAdminCamera();
  const generation = adminCameraGeneration;
  const G = window.FaceGuidance;
  const progress = $('enroll-dialog').querySelector('#webcam-progress');
  const showProgress = text => { if (progress) progress.textContent = text; };
  const stage = $('admin-video').closest('.face-guidance') || $('admin-video').parentElement;
  const setGuidance = G.mountSafe(stage);
  setGuidance({ state: 'NEUTRAL', key: 'starting' });
  button.disabled = true;
  try {
    setGuidance({ state: 'NEUTRAL', key: 'starting' });
    showProgress(L.t('prepareCamera'));
    if (!navigator.mediaDevices?.getUserMedia) throw new DOMException('Camera unavailable', 'NotFoundError');
    const acquired = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 960 } }, audio: false });
    if (generation !== adminCameraGeneration || !$('enroll-dialog').open) { acquired.getTracks().forEach(track => track.stop()); return; }
    adminStream = acquired;
    $('admin-video').srcObject = adminStream; await $('admin-video').play();
    await G.waitForVideo($('admin-video'));
    setGuidance({ state: 'NEUTRAL', key: 'CENTER' });
    const files = [];
    let previous = null;
    let stableCount = 0;
    let lastAcceptedAt = 0;
    for (let attempt = 0; files.length < 3 && attempt < 70; attempt++) {
      await delay(650);
      if (generation !== adminCameraGeneration || !adminStream || !$('enroll-dialog').open) return;
      const blob = await cameraBlob($('admin-video'));
      const candidate = new File([blob], `webcam-${files.length + 1}.jpg`, { type: 'image/jpeg' });
      const form = new FormData();
      form.append('photo', candidate, candidate.name);
      const result = await api(`/api/admin/users/${encodeURIComponent(selectedUser)}/enrollment/validate`, { method: 'POST', body: form });
      if (generation !== adminCameraGeneration || !adminStream || !$('enroll-dialog').open) return;
      if (!result.accepted) {
        previous = null;
        stableCount = 0;
        showProgress(G.t(G.fromError(result.reason_code).key));
        setGuidance(window.FaceGuidance.fromError(result.reason_code));
        continue;
      }
      const hint = G.position(G.captureGeometry(result.bounding_box, $('admin-video')), G.viewport(stage));
      setGuidance(hint ? { state: 'INVALID', key: hint, hint } : { state: 'NEUTRAL', key: 'hold' });
      showProgress(`${L.t('faceDetected')} ✓ · ${L.t('qualityPass')} ✓ · ${L.t('holdStill')}`);
      stableCount = previous && enrollmentPoseStable(previous, result) ? stableCount + 1 : 1;
      previous = result;
      const now = Date.now();
      if (stableCount >= 2 && now - lastAcceptedAt >= 800) {
        files.push(candidate);
        lastAcceptedAt = now;
        stableCount = 0;
        showProgress(`${L.t('frameAccepted')} ${files.length}/3`);
        setGuidance({ state: 'CHALLENGE_SUCCESS', key: 'done' });
        await delay(450);
        if (generation !== adminCameraGeneration) return;
        if (files.length < 3) setGuidance({ state: 'NEUTRAL', key: 'hold' });
      }
    }
    if (files.length < 3) throw Object.assign(new Error('ENROLLMENT_TIMEOUT'), { code: 'ENROLLMENT_TIMEOUT' });
    stopAdminCamera();
    setGuidance({ state: 'PROCESSING', key: 'processing' });
    showProgress(L.t('creatingBiometricTemplate'));
    await sendPhotos(files);
  } catch (error) {
    const view = G.fromError(error);
    if (generation !== adminCameraGeneration) return;
    setGuidance(view);
    if (view.technical) console.error('Enrollment camera failed', error);
    stopAdminCamera();
    showProgress(G.t(view.key));
  } finally {
    button.disabled = false;
  }
}

function enrollmentPoseStable(previous, current) {
  if (previous.yaw == null || current.yaw == null || !previous.bounding_box || !current.bounding_box) return false;
  const before = previous.bounding_box;
  const after = current.bounding_box;
  const centerBeforeX = before.x + before.width / 2;
  const centerBeforeY = before.y + before.height / 2;
  const centerAfterX = after.x + after.width / 2;
  const centerAfterY = after.y + after.height / 2;
  const scale = Math.max(before.width, before.height, 1);
  return Math.abs(previous.yaw - current.yaw) <= 0.035
    && Math.abs(centerBeforeX - centerAfterX) / scale <= 0.06
    && Math.abs(centerBeforeY - centerAfterY) / scale <= 0.06;
}

function stopAdminCamera() { adminCameraGeneration++; if (adminStream) adminStream.getTracks().forEach(track => track.stop()); adminStream = null; }
async function cameraBlob(video) { const canvas = document.createElement('canvas'); canvas.width = 560; canvas.height = Math.round(video.videoHeight / video.videoWidth * 560); canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height); return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .88)); }
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
function showDirect(view, title) { document.querySelectorAll('.admin-view').forEach(element => element.classList.toggle('active', element.id === view)); document.querySelectorAll('.admin-nav button').forEach(element => element.classList.remove('active')); $('admin-title').textContent = title; }
function table(id, heads, rows) { $(id).innerHTML = `<div class="table-wrap"><table><thead><tr>${heads.map(head => `<th>${head}</th>`).join('')}</tr></thead><tbody>${rows.length ? rows.map(row => `<tr>${row.map(value => `<td>${value}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${heads.length}" class="empty-state">${L.t('noData')}</td></tr>`}</tbody></table></div>`; }
function localizePresentationOptions() { const codes=new Set(['ACTIVE','BLOCKED','DISABLED','PENDING_REVIEW','REVISION_REQUIRED','APPROVED','REJECTED','CANCELLED','ENROLLED','NOT_ENROLLED','UPDATE_PENDING','VERIFIED','SUCCESS','DENIED','UNKNOWN','AMBIGUOUS','FACE','PASSWORD','ADMIN','USER']); document.querySelectorAll('select option').forEach(option=>{const code=option.getAttribute('value');if(code&&codes.has(code))option.textContent=humanCode(code);}); }
function renderAdminPager(id, page, count, onPage) { const host=$(id); host.insertAdjacentHTML('beforeend', `<nav class="pager" aria-label="${L.t('pagination')}"><button type="button" data-page-prev ${page===0?'disabled':''}>${L.t('previous')}</button><span>${L.t('page')} ${page+1}</span><button type="button" data-page-next ${count<adminPageSize?'disabled':''}>${L.t('next')}</button></nav>`); host.querySelector('[data-page-prev]').onclick=()=>onPage(page-1); host.querySelector('[data-page-next]').onclick=()=>onPage(page+1); }
function eventDetailsButton(row, category='') { const key=String(++eventDetailCounter); eventDetailRows.set(key,{...row,_category:category}); return `<button type="button" data-event-detail="${key}">${L.t('details')}</button>`; }
function bindEventDetailButtons() { document.querySelectorAll('[data-event-detail]').forEach(button => button.onclick=()=>openEventDetails(button.dataset.eventDetail).catch(error=>toast(error.code||error.message))); }
function eventMetadata(value) { if(value&&typeof value==='object'&&!Array.isArray(value))return value;try{const parsed=JSON.parse(value||'{}');return parsed&&typeof parsed==='object'&&!Array.isArray(parsed)?parsed:{};}catch{return {};} }
function humanCode(value, fallback='systemEvent') { if (value == null || value === '') return '—'; const translated=L.t(String(value)); return translated===String(value)&&/^[A-Z][A-Z0-9_]+$/.test(String(value))?L.t(fallback):translated; }
function humanMethod(value) { return value === 'ADMIN' ? L.t('administrativeAction') : humanCode(value); }
function accountIdentityData(row) { const fallback=row?.result==='UNKNOWN'||row?.event_type==='UNKNOWN_FACE'?L.t('unknownFace'):L.t('unknownUser'); return window.BioGateAccountIdentity(row,fallback); }
function accountIdentity(row) { return accountIdentityData(row).primary; }
function accountIdentityCell(row) { const identity=accountIdentityData(row); return `<span class="account-identity"><strong>${esc(identity.primary)}</strong>${identity.login?`<small>${esc(identity.login)}</small>`:''}</span>`; }
function humanBoolean(value) { return L.t(value ? 'yes' : 'no'); }
function resetMode(value) { return value === 'TEMPORARY' ? L.t('temporaryPassword') : value === 'PERMANENT' ? L.t('permanentPassword') : L.t('securityEvent'); }
function humanMetadata(value, { action=false, skipAction=false } = {}) {
  const data = eventMetadata(value);
  const fields = [];
  const add = (key, label, shown) => { if (shown != null && shown !== '') fields.push(`<div><dt>${esc(label)}</dt><dd>${esc(shown)}</dd></div>`); };
  if (!skipAction && typeof data.reason === 'string') add('reason', L.t(action ? 'actionReasonLabel' : 'reason'), data.reason);
  if (!skipAction && typeof data.administrator === 'string') add('administrator', L.t('administrator'), data.administrator);
  if (!skipAction && typeof data.mode === 'string') add('mode', L.t('mode'), resetMode(data.mode));
  if (!skipAction && typeof data.must_change_password === 'boolean') add('must_change_password', L.t('must_change_password'), humanBoolean(data.must_change_password));
  if (typeof data.method === 'string') add('method', L.t('method'), humanMethod(data.method));
  if (typeof data.old_status === 'string') add('old_status', L.t('old_status'), humanCode(data.old_status));
  if (typeof data.new_status === 'string') add('new_status', L.t('new_status'), humanCode(data.new_status));
  if (typeof data.note === 'string') add('note', L.t('note'), data.note);
  return fields.join('');
}
function eventMetadataDetails(row) {
  const source = eventMetadata(row.action_metadata);
  const own = eventMetadata(row.metadata);
  const auditReset = row.event_type === 'PASSWORD_RESET_BY_ADMIN';
  const action = Object.keys(source).length ? source : auditReset ? own : {};
  return `${humanMetadata(action, { action: true })}${humanMetadata(own, { skipAction: Object.keys(action).length > 0 })}${humanMetadata(row.candidate_metadata)}`;
}
function livenessLabel(value,method){if(method!=='FACE'||value==null)return L.t('notApplicable');return Number(value)>0?L.t('passed'):L.t('notPassed');}
function eventValue(name,value,row={}){if(name==='method')return humanMethod(value);if(name==='role')return humanCode(value,'status');if(['event_type','result','reason','severity'].includes(name))return humanCode(value);if(['timestamp','reviewed_at','created_at','updated_at'].includes(name))return date(value);if(name==='full_name')return accountIdentity(row);if(name==='liveness_score')return livenessLabel(value,row.method);if(name==='user_agent')return sessionDevice(value);if(name==='similarity')return pct(value);return value;}
function eventLabel(name){return name==='login'?L.t('loginName'):L.t(name);}
function bindEventDialogLocale() { const dialog=$('event-detail-dialog'); let locale=dialog.querySelector('.dialog-locale'); if(!locale){locale=document.createElement('div');locale.className='locale dialog-locale';dialog.querySelector('h2')?.insertAdjacentElement('afterend',locale);} locale.setAttribute('aria-label',L.t('language'));locale.innerHTML=['ru','en'].map(lang=>`<button type="button" data-lang="${lang}" class="${L.lang===lang?'active':''}" aria-pressed="${L.lang===lang}">${lang.toUpperCase()}</button>`).join('');locale.querySelectorAll('[data-lang]').forEach(button=>button.onclick=()=>L.set(button.dataset.lang));}
let activeEventDetailKey = null;
async function openEventDetails(key) { activeEventDetailKey=key; let row=eventDetailRows.get(key); if(!row)return; if(row._category&&row.id!=null) row=await api(`/api/v3/admin/events/${row._category}/${row.id}`); const allowed=['full_name','login','role','timestamp','event_type','method','result','reason','similarity','liveness_score','client_address','user_agent','severity','reviewed_at','note']; const metadata=eventMetadataDetails(row); const risk=row.risk?.reason_code?`<div><dt>${esc(L.t('risk'))}</dt><dd>${esc(humanCode(row.risk.risk_level,'securityEvent'))}</dd></div><div><dt>${esc(L.t('riskReason'))}</dt><dd>${esc(humanCode(row.risk.reason_code,'securityEvent'))}</dd></div>`:''; $('event-detail-content').innerHTML=`<dl class="event-details">${allowed.filter(name=>row[name]!=null&&row[name]!=='').map(name=>`<div><dt>${esc(eventLabel(name))}</dt><dd>${esc(eventValue(name,row[name],row))}</dd></div>`).join('')}${risk}${metadata}</dl><div class="detail-actions">${row.photo_id?`<button type="button" class="secondary-button" data-single-photo="${esc(row.photo_id)}">${L.t('viewPhoto')}</button><a class="secondary-button" href="/api/v3/photos/${encodeURIComponent(row.photo_id)}/download" download>${L.t('downloadPhoto')}</a>`:`<span>${L.t('photoAbsent')}</span>`}</div>`; const photoButton=$('event-detail-content').querySelector('[data-single-photo]');if(photoButton)photoButton.onclick=()=>openPhotoViewer([photoButton.dataset.singlePhoto],0); bindEventDialogLocale(); if (!$('event-detail-dialog').open) $('event-detail-dialog').showModal(); }
function field(label, value) { return `<div class="profile-field"><span>${label}</span><strong>${esc(value)}</strong></div>`; }
function badge(value) { const good = ['ACTIVE', 'VERIFIED', 'SUCCESS', 'ENROLLED', 'APPROVED', 'IDENTIFIED'].includes(value); const bad=['BLOCKED','REJECTED','DISABLED','DENIED','FAILED','UNKNOWN'].includes(value); return `<span class="badge ${good ? 'good' : bad ? 'bad' : ''}">${esc(humanCode(value,'status'))}</span>`; }
let viewerPhotos=[];let viewerIndex=0;
function openPhotoViewer(photoIds,index=0){viewerPhotos=photoIds.filter(Boolean);if(!viewerPhotos.length)return;viewerIndex=Math.max(0,Math.min(index,viewerPhotos.length-1));renderPhotoViewer();$('photo-viewer-dialog').showModal();}
function renderPhotoViewer(){const id=viewerPhotos[viewerIndex];$('photo-viewer-image').src=`/api/v3/photos/${encodeURIComponent(id)}`;$('photo-viewer-image').alt=tr('photoNumber',{current:viewerIndex+1,total:viewerPhotos.length});$('photo-viewer-count').textContent=tr('photoNumber',{current:viewerIndex+1,total:viewerPhotos.length});$('photo-viewer-previous').disabled=viewerIndex===0;$('photo-viewer-next').disabled=viewerIndex===viewerPhotos.length-1;}
$('photo-viewer-previous').onclick=()=>{if(viewerIndex>0){viewerIndex--;renderPhotoViewer();}};$('photo-viewer-next').onclick=()=>{if(viewerIndex<viewerPhotos.length-1){viewerIndex++;renderPhotoViewer();}};$('photo-viewer-dialog').addEventListener('keydown',event=>{if(event.key==='ArrowLeft')$('photo-viewer-previous').click();if(event.key==='ArrowRight')$('photo-viewer-next').click();});
function requestGallery(photos){if(!photos?.length)return `<div class="photo-empty">${L.t('photosUnavailable')}</div>`;return `<div class="request-gallery">${photos.map((photo,index)=>`<button type="button" class="request-photo" data-request-photo="${index}" data-photo-id="${esc(photo.id)}" aria-label="${esc(tr('photoNumber',{current:index+1,total:photos.length}))}"><img src="/api/v3/photos/${encodeURIComponent(photo.id)}" alt="${esc(tr('photoNumber',{current:index+1,total:photos.length}))}" loading="lazy"><small>${index+1} / ${photos.length}</small></button>`).join('')}</div>`;}
function bindRequestGallery(root=document){const buttons=[...root.querySelectorAll('[data-request-photo]')];const ids=buttons.map(button=>button.dataset.photoId);buttons.forEach(button=>button.onclick=()=>openPhotoViewer(ids,Number(button.dataset.requestPhoto)));root.querySelectorAll('.request-photo img').forEach(image=>image.onerror=()=>{image.closest('.request-photo').disabled=true;image.alt=L.t('fileUnavailable');});}
function date(value) { return value ? new Date(value).toLocaleString(L.lang) : '—'; }
function fmt(value) { return value == null ? '—' : Number(value).toFixed(1); }
function pct(value) { return value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`; }
function sessionDevice(agent) { const value=String(agent||''); const browser=/Edg\//.test(value)?'Edge':/Chrome\//.test(value)?'Chrome':/Firefox\//.test(value)?'Firefox':/Safari\//.test(value)?'Safari':L.t('unknownBrowser'); const os=/Windows/.test(value)?'Windows':/Android/.test(value)?'Android':/(iPhone|iPad)/.test(value)?'iOS':/Mac OS/.test(value)?'macOS':/Linux/.test(value)?'Linux':L.t('unknownDevice'); return `${browser} · ${os}`; }
function parseJson(value) { try { return JSON.parse(value); } catch { return null; } }
function esc(value) { return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]); }
function jsonOptions(method, body) { return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }; }
function adminAsk({title,message='',label='',required=false,value='',confirmKey='continue',danger=false}={}){return new Promise(resolve=>{let dialog=$('admin-action-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='admin-action-dialog';dialog.className='confirm-dialog';document.body.appendChild(dialog);}dialog.innerHTML=`<form method="dialog"><h2>${esc(title||L.t('confirmation'))}</h2>${message?`<p>${esc(message)}</p>`:''}${label?`<label class="dialog-field"><span>${esc(label)}</span><textarea data-admin-input ${required?'required':''}>${esc(value)}</textarea></label>`:''}<div class="confirm-actions"><button value="cancel">${L.t('cancel')}</button><button value="confirm" class="${danger?'danger':'primary-small'}">${L.t(confirmKey)}</button></div></form>`;const form=dialog.querySelector('form');form.addEventListener('submit',event=>{const input=dialog.querySelector('[data-admin-input]');if(event.submitter?.value==='confirm'&&required&&!input?.value.trim()){event.preventDefault();input?.focus();return;}});dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'?(dialog.querySelector('[data-admin-input]')?.value.trim()??''):null),{once:true});dialog.showModal();});}

document.querySelectorAll('.admin-nav button').forEach(button => button.onclick = () => show(button.dataset.view));
document.querySelectorAll('[data-go]').forEach(button => button.onclick = () => show(button.dataset.go));
$('create-user').onsubmit = async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  const originalLabel = submit.textContent;
  submit.textContent = L.t('creating');
  try {
    const values = Object.fromEntries(new FormData(form));
    if (accountAuth) {
      const temporary = values.temporary_password;
      const policyError = temporary ? window.BioGatePasswordPolicy.validate(temporary) : '';
      if (policyError) { toast(policyError); return; }
      const user = await api('/api/v3/admin/accounts', jsonOptions('POST', {
        external_id: values.external_id, login: values.login || values.external_id,
        full_name: values.display_name, employee_id: values.employee_id || null,
        department: values.department, position: values.position, role: values.role,
        status: values.status, comment: values.comment,
        temporary_password: temporary || null,
        generate_temporary_password: !temporary,
      }));
      if (user.temporary_password) {
        showOneTimePassword(user.temporary_password);
      }
      form.reset();
      toast('userCreated');
      await openV3User(Number(user.id), 'overview');
    } else {
      const user = await api('/api/admin/users', jsonOptions('POST', values));
      location.href = `/admin/users/${encodeURIComponent(user.external_id)}`;
    }
  } catch (error) {
    form.elements.temporary_password.value = '';
    toast(error.code || error.message);
  } finally {
    submit.disabled = false;
    submit.textContent = originalLabel;
  }
};
$('refresh-attempts').onclick = loadAttempts;
$('apply-user-filters').onclick = () => { usersPage = 0; loadUsers(); };
$('submit-photos').onclick = () => sendPhotos($('photo-files').files);
$('start-webcam-enroll').onclick = webcamEnroll;
$('tab-upload').onclick = () => selectEnrollmentMode('upload');
$('tab-webcam').onclick = () => selectEnrollmentMode('webcam');
$('cancel-delete-biometric').onclick = () => { if (!deleteBiometricInFlight) $('delete-biometric-dialog').close(); };
$('confirm-delete-biometric').onclick = confirmDeleteBiometric;
$('save-settings').onclick = async event => { const button=event.currentTarget; if(button.disabled)return; button.disabled=true; const label=button.textContent; button.textContent=L.t('saving'); try { await api('/api/admin/settings', jsonOptions('PUT', { store_attempt_images: $('store-snapshots').checked })); toast('settingsSaved'); } catch(error) { toast(error.code||error.message); } finally { button.disabled=false; button.textContent=label; } };
$('admin-logout').onclick = async () => { await api('/api/v3/auth/logout', { method: 'POST' }); location.href = '/'; };
$('enroll-dialog').addEventListener('close', stopAdminCamera);
window.addEventListener('biogate:locale', () => {
  window.V3Theme?.init();
  localizePresentationOptions();
  document.querySelector('.enroll-tabs')?.setAttribute('aria-label', L.t('chooseEnrollmentMethod'));
  if (!adminReady) return;
  const active = document.querySelector('.admin-view.active');
  if (active?.id === 'dashboard') loadDashboard();
  else if (active?.id === 'users') loadUsers();
  else if (active?.id === 'attempts') loadAttempts();
  else if (active?.id === 'audit') loadAudit();
  else if (active?.id === 'user-detail' && selectedV3UserId) openV3User(selectedV3UserId, selectedV3Tab);
  else if (active?.id === 'user-detail' && selectedUser) openUser(selectedUser);
  else if (active?.id && window.loadBioGateV3Admin) {
    window.loadBioGateV3Admin(active.id).catch(error => toast(error.code || error.message));
  }
  if ($('enroll-dialog').open) $('enroll-title').textContent = L.t(selectedUserHasBiometrics ? 'updateBiometric' : 'addBiometric');
  if ($('delete-biometric-dialog').open) $('delete-biometric-message').textContent = tr('confirmDeleteBiometric', { name: selectedUserName });
  if ($('event-detail-dialog').open && activeEventDetailKey) openEventDetails(activeEventDetailKey).catch(error => toast(error.code || error.message));
});
const userMatch = location.pathname.match(/^\/admin\/users\/([^/]+)$/);
const attemptMatch = location.pathname.match(/^\/admin\/verifications\/(\d+)$/);

async function bootAdmin() {
  L.init();
  localizePresentationOptions();
  const accountResponse = await fetch('/api/v3/auth/me');
  if (!accountResponse.ok) { location.href = '/'; return; }
  const identity = await accountResponse.json();
  accountAuth = true;
  if (identity.role !== 'ADMIN') { location.href = '/user'; return; }
  csrfToken = identity.csrf_token;
  currentAdminSessionId = identity.session_id;
  adminReady = true;
  $('admin-username').textContent = accountIdentity(identity);
  if (userMatch) await openUser(decodeURIComponent(userMatch[1]));
  else if (attemptMatch) await openAttempt(Number(attemptMatch[1]));
  else {
    const route = location.hash.slice(1) || 'dashboard';
    const userRoute = route.match(/^users\/(\d+)(?:\/(\w+))?$/);
    if (userRoute) await openV3User(Number(userRoute[1]), userRoute[2] || 'overview');
    else show(route);
  }
}

window.addEventListener('load', localizePresentationOptions, { once: true });
bootAdmin();
