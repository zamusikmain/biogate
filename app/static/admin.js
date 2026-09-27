const $ = id => document.getElementById(id);
const L = window.BioGateLocale;
let selectedUser = null;
let adminStream = null;
let selectedUserName = '';
let selectedUserHasBiometrics = false;
let deleteBiometricInFlight = false;
let csrfToken = '';
let adminReady = false;

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
      location.href = '/admin/login?reason=session_expired';
      throw new Error('SESSION_EXPIRED');
    }
    const error = new Error(body.detail?.reason_code || body.detail || 'REQUEST_FAILED');
    error.code = body.detail?.reason_code || 'REQUEST_FAILED';
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

function show(view) {
  document.querySelectorAll('.admin-view').forEach(element => element.classList.toggle('active', element.id === view));
  document.querySelectorAll('.admin-nav button').forEach(element => element.classList.toggle('active', element.dataset.view === view));
  $('admin-title').textContent = L.t(view === 'create' ? 'createUser' : view);
  history.replaceState({}, '', view === 'dashboard' ? '/admin' : `/admin#${view}`);
  if (view === 'dashboard') loadDashboard();
  if (view === 'users') loadUsers();
  if (view === 'attempts') loadAttempts();
  if (view === 'audit') loadAudit();
  if (view === 'settings') loadSettings();
}

async function loadDashboard() {
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
  const users = await api('/api/admin/users');
  table('users-list', ['User ID', L.t('displayName'), L.t('status'), L.t('biometrics'), L.t('created')], users.map(user => [
    `<a href="/admin/users/${encodeURIComponent(user.external_id)}">${esc(user.external_id)}</a>`,
    esc(user.display_name), badge(user.status), user.enrolled_at ? badge('ENROLLED') : '—', date(user.created_at),
  ]));
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
    const name = prompt(L.t('displayName'), user.display_name);
    if (name) { await api(`/api/admin/users/${encodeURIComponent(id)}`, jsonOptions('PATCH', { display_name: name })); openUser(id); }
  };
  document.querySelector('[data-action="toggle"]').onclick = async () => {
    await api(`/api/admin/users/${encodeURIComponent(id)}`, jsonOptions('PATCH', { status: user.status === 'BLOCKED' ? 'ACTIVE' : 'BLOCKED' })); openUser(id);
  };
  document.querySelector('[data-action="enroll"]').onclick = () => openEnrollment(id, hasBiometrics);
  if (document.querySelector('[data-action="empty-enroll"]')) document.querySelector('[data-action="empty-enroll"]').onclick = () => openEnrollment(id, false);
  if (document.querySelector('[data-action="delete-bio"]')) document.querySelector('[data-action="delete-bio"]').onclick = () => openDeleteBiometric(id, user.display_name);
  document.querySelector('[data-action="delete-user"]').onclick = async () => {
    if (confirm(L.t('confirmDeleteUser'))) { await api(`/api/admin/users/${encodeURIComponent(id)}`, { method: 'DELETE' }); location.href = '/admin#users'; }
  };
  $('history-filter').onchange = async event => {
    const value = event.target.value;
    const query = value === 'VERIFIED' || value === 'REJECTED' ? `?result=${value}` : value ? `?reason=${value}` : '';
    renderHistory(await api(`/api/admin/users/${encodeURIComponent(id)}/verifications${query}`));
  };
}

function renderHistory(rows) {
  table('user-history', [L.t('time'), L.t('result'), L.t('similarity'), L.t('threshold'), L.t('quality'), L.t('liveness'), L.t('latency'), L.t('reason')], rows.map(row => [
    `<a href="/admin/verifications/${row.id}">${date(row.timestamp)}</a>`, badge(row.result), pct(row.similarity_score), pct(row.threshold), pct(row.quality_score), pct(row.liveness_score), `${fmt(row.inference_time_ms)} ms`, esc(row.reason_code),
  ]));
}

async function loadAttempts() {
  const params = new URLSearchParams();
  [['filter-user', 'user'], ['filter-result', 'result'], ['filter-live', 'liveness'], ['filter-reason', 'reason'], ['filter-from', 'date_from'], ['filter-to', 'date_to']].forEach(([id, key]) => { if ($(id)?.value) params.set(key, $(id).value); });
  const rows = await api(`/api/admin/verifications?${params}`);
  table('attempts-list', [L.t('time'), L.t('user'), L.t('result'), L.t('similarity'), L.t('liveness'), L.t('quality'), L.t('reason'), L.t('latency')], rows.map(row => [
    `<a href="/admin/verifications/${row.id}">${date(row.timestamp)}</a>`, esc(row.claimed_external_id), badge(row.result), pct(row.similarity_score), pct(row.liveness_score), pct(row.quality_score), esc(row.reason_code), `${fmt(row.inference_time_ms)} ms`,
  ]));
}

async function openAttempt(id) {
  const attempt = await api(`/api/admin/verifications/${id}`);
  showDirect('attempt-detail', `Attempt #${id}`);
  const steps = parseJson(attempt.challenge_steps);
  $('attempt-card').innerHTML = `<div class="detail-hero"><div><span class="section-label">VERIFICATION / #${attempt.id}</span><h2>${badge(attempt.result)} ${esc(attempt.claimed_external_id)}</h2><p>${date(attempt.timestamp)}</p></div></div>
    <div class="profile-grid">${field(L.t('faceDetectedLabel'), attempt.face_detected ? 'PASS' : 'FAIL')}${field(L.t('quality'), pct(attempt.quality_score))}${field(L.t('liveness'), pct(attempt.liveness_score))}${field(L.t('similarity'), pct(attempt.similarity_score))}${field(L.t('threshold'), pct(attempt.threshold))}${field(L.t('latency'), `${fmt(attempt.inference_time_ms)} ms`)}${field(L.t('reason'), attempt.reason_code)}${field(L.t('model'), attempt.model_name ? `${attempt.model_name}-${attempt.model_version}` : '—')}${field(L.t('challenge'), Array.isArray(steps) ? steps.join(' → ') : L.t('legacyFlow'))}${field(L.t('challengeResult'), attempt.challenge_result || '—')}</div>
    ${attempt.snapshot_id ? `<div class="data-card"><h2>${L.t('attemptSnapshot')}</h2><p class="warning">${L.t('snapshotWarning')}</p><img class="snapshot" src="/api/admin/verifications/${attempt.id}/snapshot"><button id="delete-snapshot">${L.t('deleteSnapshot')}</button></div>` : ''}<div class="data-card"><h2>${L.t('auditLog')}</h2><div id="attempt-audit"></div></div>`;
  table('attempt-audit', [L.t('date'), L.t('event'), L.t('result')], attempt.audit_events.map(event => [date(event.timestamp), esc(event.event_type), badge(event.result)]));
  if ($('delete-snapshot')) $('delete-snapshot').onclick = async () => { await api(`/api/admin/verifications/${id}/snapshot`, { method: 'DELETE' }); openAttempt(id); };
}

async function loadAudit() {
  const rows = await api('/api/admin/audit');
  table('audit-list', [L.t('date'), L.t('event'), 'User ID', L.t('result'), 'Metadata'], rows.map(event => [date(event.timestamp), esc(event.event_type), event.user_id ?? '—', badge(event.result), `<code>${esc(event.metadata)}</code>`]));
}

async function loadSettings() { $('store-snapshots').checked = (await api('/api/admin/settings')).store_attempt_images; }
function openEnrollment(id, hasBiometrics = selectedUserHasBiometrics) {
  selectedUser = id;
  selectedUserHasBiometrics = hasBiometrics;
  $('photo-results').innerHTML = '';
  $('photo-files').value = '';
  $('webcam-progress').textContent = L.t('webcamHint');
  $('enroll-title').textContent = L.t(hasBiometrics ? 'updateBiometric' : 'addBiometric');
  $('enroll-dialog').showModal();
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
      await openUser(selectedUser);
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
  button.disabled = true;
  try {
    $('webcam-progress').textContent = L.t('prepareCamera');
    adminStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 960 } }, audio: false });
    $('admin-video').srcObject = adminStream; await $('admin-video').play();
    const files = [];
    let previous = null;
    let stableCount = 0;
    let lastAcceptedAt = 0;
    for (let attempt = 0; files.length < 3 && attempt < 70; attempt++) {
      await delay(650);
      const blob = await cameraBlob($('admin-video'));
      const candidate = new File([blob], `webcam-${files.length + 1}.jpg`, { type: 'image/jpeg' });
      const form = new FormData();
      form.append('photo', candidate, candidate.name);
      const result = await api(`/api/admin/users/${encodeURIComponent(selectedUser)}/enrollment/validate`, { method: 'POST', body: form });
      if (!result.accepted) {
        previous = null;
        stableCount = 0;
        $('webcam-progress').textContent = L.t(result.reason_code);
        continue;
      }
      $('webcam-progress').textContent = `${L.t('faceDetected')} ✓ · ${L.t('qualityPass')} ✓ · ${L.t('holdStill')}`;
      stableCount = previous && enrollmentPoseStable(previous, result) ? stableCount + 1 : 1;
      previous = result;
      const now = Date.now();
      if (stableCount >= 2 && now - lastAcceptedAt >= 800) {
        files.push(candidate);
        lastAcceptedAt = now;
        stableCount = 0;
        $('webcam-progress').textContent = `${L.t('frameAccepted')} ${files.length}/3`;
      }
    }
    if (files.length < 3) throw Object.assign(new Error('ENROLLMENT_TIMEOUT'), { code: 'ENROLLMENT_TIMEOUT' });
    stopAdminCamera();
    $('webcam-progress').textContent = L.t('creatingBiometricTemplate');
    await sendPhotos(files);
  } catch (error) {
    stopAdminCamera();
    toast(error.code || error.message);
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

function stopAdminCamera() { if (adminStream) adminStream.getTracks().forEach(track => track.stop()); adminStream = null; }
async function cameraBlob(video) { const canvas = document.createElement('canvas'); canvas.width = 560; canvas.height = Math.round(video.videoHeight / video.videoWidth * 560); canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height); return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .88)); }
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
function showDirect(view, title) { document.querySelectorAll('.admin-view').forEach(element => element.classList.toggle('active', element.id === view)); document.querySelectorAll('.admin-nav button').forEach(element => element.classList.remove('active')); $('admin-title').textContent = title; }
function table(id, heads, rows) { $(id).innerHTML = `<div class="table-wrap"><table><thead><tr>${heads.map(head => `<th>${head}</th>`).join('')}</tr></thead><tbody>${rows.length ? rows.map(row => `<tr>${row.map(value => `<td>${value}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${heads.length}">—</td></tr>`}</tbody></table></div>`; }
function field(label, value) { return `<div class="profile-field"><span>${label}</span><strong>${esc(value)}</strong></div>`; }
function badge(value) { const good = ['ACTIVE', 'VERIFIED', 'SUCCESS', 'ENROLLED'].includes(value); return `<span class="badge ${good ? 'good' : value === 'BLOCKED' || value === 'REJECTED' ? 'bad' : ''}">${esc(value || '—')}</span>`; }
function date(value) { return value ? new Date(value).toLocaleString(L.lang) : '—'; }
function fmt(value) { return value == null ? '—' : Number(value).toFixed(1); }
function pct(value) { return value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`; }
function parseJson(value) { try { return JSON.parse(value); } catch { return null; } }
function esc(value) { return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]); }
function jsonOptions(method, body) { return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }; }

document.querySelectorAll('.admin-nav button').forEach(button => button.onclick = () => show(button.dataset.view));
document.querySelectorAll('[data-go]').forEach(button => button.onclick = () => show(button.dataset.go));
$('create-user').onsubmit = async event => { event.preventDefault(); try { const user = await api('/api/admin/users', jsonOptions('POST', Object.fromEntries(new FormData(event.target)))); location.href = `/admin/users/${encodeURIComponent(user.external_id)}`; } catch (error) { toast(error.message); } };
$('refresh-attempts').onclick = loadAttempts;
$('submit-photos').onclick = () => sendPhotos($('photo-files').files);
$('start-webcam-enroll').onclick = webcamEnroll;
$('tab-upload').onclick = () => { $('upload-pane').classList.remove('hidden'); $('webcam-pane').classList.add('hidden'); $('tab-upload').classList.add('active'); $('tab-webcam').classList.remove('active'); stopAdminCamera(); };
$('tab-webcam').onclick = () => { $('webcam-pane').classList.remove('hidden'); $('upload-pane').classList.add('hidden'); $('tab-webcam').classList.add('active'); $('tab-upload').classList.remove('active'); };
$('cancel-delete-biometric').onclick = () => { if (!deleteBiometricInFlight) $('delete-biometric-dialog').close(); };
$('confirm-delete-biometric').onclick = confirmDeleteBiometric;
$('save-settings').onclick = async () => { await api('/api/admin/settings', jsonOptions('PUT', { store_attempt_images: $('store-snapshots').checked })); toast('save'); };
$('admin-logout').onclick = async () => { await api('/api/admin/auth/logout', { method: 'POST' }); location.href = '/admin/login'; };
$('enroll-dialog').addEventListener('close', stopAdminCamera);
window.addEventListener('biogate:locale', () => {
  if (!adminReady) return;
  const active = document.querySelector('.admin-view.active');
  if (active?.id === 'dashboard') loadDashboard();
  else if (active?.id === 'users') loadUsers();
  else if (active?.id === 'attempts') loadAttempts();
  else if (active?.id === 'audit') loadAudit();
  else if (active?.id === 'user-detail' && selectedUser) openUser(selectedUser);
  if ($('enroll-dialog').open) $('enroll-title').textContent = L.t(selectedUserHasBiometrics ? 'updateBiometric' : 'addBiometric');
  if ($('delete-biometric-dialog').open) $('delete-biometric-message').textContent = tr('confirmDeleteBiometric', { name: selectedUserName });
});
const userMatch = location.pathname.match(/^\/admin\/users\/([^/]+)$/);
const attemptMatch = location.pathname.match(/^\/admin\/verifications\/(\d+)$/);

async function bootAdmin() {
  L.init();
  const identity = await api('/api/admin/auth/me');
  csrfToken = identity.csrf_token;
  adminReady = true;
  $('admin-username').textContent = identity.username;
  if (userMatch) await openUser(decodeURIComponent(userMatch[1]));
  else if (attemptMatch) await openAttempt(Number(attemptMatch[1]));
  else show(location.hash.slice(1) || 'dashboard');
}

bootAdmin();
