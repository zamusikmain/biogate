const q = id => document.getElementById(id);
const V = window.V3Locale;
let me = null;
let accessPage = 0;
let notificationPage = 0;
let currentView = 'home';
const userPageSize = 20;
let biometricCamera = null;
let biometricController = null;
let biometricGuidance = null;
let biometricDraftPhotos = [];
let biometricPreviewUrls = [];
let biometricSubmitInFlight = false;
let biometricNotice = '';
function stopBiometricCamera() { biometricController?.abort(); biometricController = null; biometricCamera?.getTracks().forEach(track => track.stop()); biometricCamera = null; biometricGuidance?.destroy(); biometricGuidance = null; }
function clearBiometricDraft() { biometricPreviewUrls.forEach(url => URL.revokeObjectURL(url)); biometricPreviewUrls = []; biometricDraftPhotos = []; biometricSubmitInFlight = false; }

async function api(path, options = {}) {
  const opts = { ...options };
  const method = (opts.method || 'GET').toUpperCase();
  const headers = new Headers(opts.headers || {});
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && me?.csrf_token) headers.set('X-CSRF-Token', me.csrf_token);
  opts.headers = headers;
  const response = await fetch(path, opts);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) location.href = '/';
    const fallback = {403:'FORBIDDEN',404:'NOT_FOUND',409:'CONFLICT',422:'VALIDATION_FAILED',500:'SERVER_ERROR',503:'DATABASE_TEMPORARILY_UNAVAILABLE'}[response.status] || 'error';
    throw new Error(body.detail?.reason_code || fallback);
  }
  return body;
}

const esc = value => String(value ?? '—').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const date = value => value ? new Date(value).toLocaleString(V.lang) : '—';
const accountIdentity = account => window.BioGateAccountIdentity(account, V.t('unknownUser')).primary;
function sessionDevice(agent) { const value=String(agent||''); const browser=/Edg\//.test(value)?'Edge':/Chrome\//.test(value)?'Chrome':/Firefox\//.test(value)?'Firefox':/Safari\//.test(value)?'Safari':V.t('unknownBrowser'); const os=/Windows/.test(value)?'Windows':/Android/.test(value)?'Android':/(iPhone|iPad)/.test(value)?'iOS':/Mac OS/.test(value)?'macOS':/Linux/.test(value)?'Linux':V.t('unknownDevice'); return `${browser} · ${os}`; }
function kv(label, value) { return `<div class="v3-kv"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`; }
function eventsTable(rows) {
  return `<div class="v3-table-wrap"><table class="v3-table"><thead><tr><th>${V.t('lastLogin')}</th><th>${V.t('loginMethod')}</th><th>${V.t('status')}</th></tr></thead><tbody>${rows.length?rows.map(row => `<tr><td>${date(row.timestamp)}</td><td>${esc(V.t(row.method))}</td><td><span class="v3-badge">${esc(V.t(row.result))}</span></td></tr>`).join(''):`<tr><td colspan="3">${V.t('noData')}</td></tr>`}</tbody></table></div>`;
}
function pager(page,count,onPage){return `<nav class="pager"><button type="button" data-user-prev ${page===0?'disabled':''}>${V.t('previous')}</button><span>${V.t('page')} ${page+1}</span><button type="button" data-user-next ${count<userPageSize?'disabled':''}>${V.t('next')}</button></nav>`;}
function bindPager(page,onPage){const prev=document.querySelector('[data-user-prev]');const next=document.querySelector('[data-user-next]');if(prev)prev.onclick=()=>onPage(page-1);if(next)next.onclick=()=>onPage(page+1);}

async function show(view) {
  currentView = view;
  stopBiometricCamera();
  document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  q('view-title').textContent = V.t(view === 'access' ? 'myAccess' : view);
  const content = q('content');
  content.innerHTML = `<div class="v3-card">${V.t('processing')}</div>`;
  if (view === 'home') {
    const [profile, events, notifications] = await Promise.all([api('/api/v3/user/profile'), api('/api/v3/user/access-events'), api('/api/v3/user/notifications')]);
    content.innerHTML = `<div class="v3-grid"><div class="v3-card"><small>${V.t('fullName')}</small><h2>${esc(accountIdentity(profile))}</h2><span class="v3-badge">${esc(V.t(profile.status))}</span></div><div class="v3-card"><small>${V.t('lastLogin')}</small><h2>${date(profile.last_login_at)}</h2><span>${esc(V.t(me.login_method))}</span></div><div class="v3-card"><small>${V.t('notifications')}</small><h2>${notifications.filter(item => !item.read_at).length}</h2></div></div><div class="v3-card"><h2>${V.t('myAccess')}</h2>${eventsTable(events.slice(0, 8))}</div>`;
  } else if (view === 'profile') {
    const profile = await api('/api/v3/user/profile');
    content.innerHTML = `<div class="v3-card">${kv(V.t('fullName'), accountIdentity(profile))}${kv(V.t('login'), profile.login)}${kv(V.t('role'), V.t(profile.role))}${kv(V.t('employeeId'), profile.employee_id)}${kv(V.t('department'), profile.department)}${kv(V.t('position'), profile.position)}${kv(V.t('status'), V.t(profile.status))}${kv(V.t('createdAt'), date(profile.created_at))}${kv(V.t('biometricStatus'), V.t(profile.enrolled_at ? 'ACTIVE' : 'NOT_ENROLLED'))}</div>`;
  } else if (view === 'access') {
    const rows=await api(`/api/v3/user/access-events?limit=${userPageSize}&offset=${accessPage*userPageSize}`);
    content.innerHTML = `<div class="v3-card">${eventsTable(rows)}${pager(accessPage,rows.length)}</div>`;
    bindPager(accessPage,page=>{accessPage=page;show('access');});
  } else if (view === 'biometrics') {
    await showBiometrics(content);
  } else if (view === 'security') {
    await showSecurity(content);
  } else if (view === 'notifications') {
    const rows = await api(`/api/v3/user/notifications?limit=${userPageSize}&offset=${notificationPage*userPageSize}`);
    content.innerHTML = `<div class="v3-stack">${rows.map(item => `<article class="v3-notification${item.read_at ? ' is-read' : ' is-unread'}"><strong>${esc(V.t(item.message_key))}</strong><p>${date(item.created_at)}</p><small>${V.t(item.read_at ? 'read' : 'unread')}</small></article>`).join('') || V.t('noData')}</div>${pager(notificationPage,rows.length)}`;
    bindPager(notificationPage,page=>{notificationPage=page;show('notifications');});
  } else {
    content.innerHTML = `<div class="v3-card"><h2>${V.t('settings')}</h2><p>${V.t('language')}</p><div class="segmented"><button data-v3-lang="ru" aria-label="Русский">RU</button><button data-v3-lang="en" aria-label="English">EN</button></div><p>${V.t('theme')}</p><div class="segmented"><button data-v3-theme="light">${V.t('light')}</button><button data-v3-theme="dark">${V.t('dark')}</button><button data-v3-theme="system">${V.t('system')}</button></div></div>`;
    document.querySelectorAll('[data-v3-lang]').forEach(button => {
      button.classList.toggle('active', button.dataset.v3Lang === V.lang);
      button.onclick = () => V.set(button.dataset.v3Lang);
    });
    window.V3Theme.init();
  }
}

async function showBiometrics(content) {
  const request = await api('/api/v3/user/biometric-request');
  const canSubmit = !request || request.status === 'REVISION_REQUIRED';
  const gallery=request?.photos?.length?`<h3>${V.t('submittedPhotos')}</h3><div class="v3-request-gallery">${request.photos.map((photo,index)=>`<a href="/api/v3/photos/${encodeURIComponent(photo.id)}" target="_blank" rel="noopener"><img src="/api/v3/photos/${encodeURIComponent(photo.id)}" alt="${esc(`${V.t('submittedPhotos')} ${index+1}`)}" loading="lazy"></a>`).join('')}</div>`:request?`<p class="v3-photo-empty">${V.t('photosUnavailable')}</p>`:'';
  const details = request ? kv(V.t('requestStatus'), V.t(request.status)) + kv(V.t('adminComment'), request.admin_comment) + gallery : '';
  const form = `<form id="bio-form" class="v3-form"><label>${V.t('selectPhotos')}<input name="photos" type="file" accept="image/jpeg,image/png,image/webp" multiple required></label><button class="v3-primary">${V.t('submitBiometric')}</button><button id="bio-camera" type="button" class="v3-secondary">${V.t('startCamera')}</button></form>`;
  const cancel = request?.status === 'PENDING_REVIEW' ? `<button id="bio-cancel" class="v3-secondary">${V.t('cancel')}</button>` : '';
  const notice = biometricNotice ? `<p class="v3-success" role="status">${esc(biometricNotice)}</p>` : '';
  biometricNotice = '';
  content.innerHTML = `<div class="v3-card"><h2>${V.t('biometrics')}</h2>${notice}${details}${canSubmit ? form : cancel}</div>`;
  if (canSubmit) {
    q('bio-form').onsubmit = submitBiometric;
    q('bio-camera').onclick = webcamBiometric;
  } else if (request?.status === 'PENDING_REVIEW') {
    q('bio-cancel').onclick = async () => {
      await api(`/api/v3/user/biometric-requests/${request.id}`, { method: 'DELETE' });
      await show('biometrics');
    };
  }
}

async function showSecurity(content) {
  const sessions = await api('/api/v3/sessions');
  content.innerHTML = `<div class="v3-grid"><form id="password-change" class="v3-card v3-form"><h2>${V.t('changePassword')}</h2><label>${V.t('currentPassword')}<input name="current" type="password" placeholder="${V.t('currentPassword')}" required></label><label>${V.t('newPassword')}<input name="password" type="password" placeholder="${V.t('newPassword')}" required minlength="12"></label><label>${V.t('confirmPassword')}<input name="confirmation" type="password" placeholder="${V.t('confirmPassword')}" required minlength="12"></label><p class="password-requirements">${esc(V.t('passwordRequirements'))}</p><p class="v3-error" data-password-error role="alert"></p><button class="v3-primary">${V.t('save')}</button></form><div class="v3-card"><h2>${V.t('activeSessions')}</h2><div class="v3-stack">${sessions.map(item => `<article class="v3-session"><div><strong>${sessionDevice(item.user_agent)}</strong><p>${esc(V.t(item.login_method))} · ${date(item.last_seen_at)}</p></div>${item.id===me.session_id?`<span class="v3-badge">${V.t('currentSession')}</span>`:!item.revoked_at?`<button type="button" data-session="${esc(item.id)}">${V.t('revoke')}</button>`:''}</article>`).join('')||V.t('noData')}</div></div></div>`;
  q('password-change').onsubmit = changePassword;
  document.querySelectorAll('[data-session]').forEach(button => button.onclick = async () => {
    await api(`/api/v3/sessions/${button.dataset.session}`, { method: 'DELETE' });
    await show('security');
  });
}

async function changePassword(event) {
  event.preventDefault();
  const value = Object.fromEntries(new FormData(event.target));
  const error = event.target.querySelector('[data-password-error]');
  const policyError = window.BioGatePasswordPolicy.validate(value.password);
  if (policyError) { error.textContent = V.t(policyError); return; }
  if (value.password !== value.confirmation) { error.textContent = V.t('PASSWORD_CONFIRMATION_MISMATCH'); return; }
  try {
    await api('/api/v3/auth/change-password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current_password: value.current, new_password: value.password, confirmation: value.confirmation }) });
    event.target.reset();
    error.textContent = V.t('passwordChanged');
  } catch (requestError) { error.textContent = V.t(requestError.message); }
}

async function submitBiometric(event) {
  event.preventDefault();
  const button = event.submitter || event.target.querySelector('[type="submit"]');
  if (biometricSubmitInFlight) return;
  biometricSubmitInFlight = true;
  button.disabled = true;
  const form = new FormData();
  for (const file of event.target.photos.files) form.append('photos', file);
  try {
    const request = await api('/api/v3/user/biometric-requests', { method: 'POST', body: form });
    if (request.status !== 'PENDING_REVIEW') throw new Error('error');
    biometricNotice = V.t('requestSubmittedToAdmin');
    await show('biometrics');
  } finally {
    biometricSubmitInFlight = false;
    if (button.isConnected) button.disabled = false;
  }
}

function renderBiometricPreview(photos) {
  stopBiometricCamera();
  clearBiometricDraft();
  biometricDraftPhotos = photos;
  biometricPreviewUrls = photos.map(photo => URL.createObjectURL(photo));
  q('content').innerHTML = `<div class="v3-card biometric-preview"><h2>${V.t('photosReady')}</h2><p>${V.t('reviewPhotos')}</p><div class="biometric-preview-grid">${biometricPreviewUrls.map((url,index)=>`<img src="${url}" alt="${esc(`${V.t('capturedPhoto')} ${index+1}`)}">`).join('')}</div><p class="v3-error" id="biometric-submit-error" role="alert"></p><div class="biometric-preview-actions"><button type="button" class="v3-primary" id="biometric-preview-submit">${V.t('submitForReview')}</button><button type="button" class="v3-secondary" id="biometric-preview-retake">${V.t('retake')}</button><button type="button" class="v3-secondary" id="biometric-preview-cancel">${V.t('cancel')}</button></div></div>`;
  q('biometric-preview-submit').onclick = submitCapturedBiometric;
  q('biometric-preview-retake').onclick = () => { clearBiometricDraft(); webcamBiometric(); };
  q('biometric-preview-cancel').onclick = async () => { clearBiometricDraft(); await show('biometrics'); };
}

async function submitCapturedBiometric() {
  if (biometricSubmitInFlight || !biometricDraftPhotos.length) return;
  biometricSubmitInFlight = true;
  const button = q('biometric-preview-submit');
  button.disabled = true;
  const error = q('biometric-submit-error');
  error.textContent = '';
  const form = new FormData();
  biometricDraftPhotos.forEach((photo,index) => form.append('photos', photo, `webcam-${index+1}.jpg`));
  try {
    const request = await api('/api/v3/user/biometric-requests', { method: 'POST', body: form });
    if (request.status !== 'PENDING_REVIEW') throw new Error('error');
    clearBiometricDraft();
    biometricNotice = V.t('requestSubmittedToAdmin');
    await show('biometrics');
  } catch (requestError) {
    error.textContent = V.t(requestError.message);
    biometricSubmitInFlight = false;
    button.disabled = false;
  }
}

async function webcamBiometric() {
  stopBiometricCamera();
  const G = window.FaceGuidance;
  const controller = new AbortController();
  biometricController = controller;
  q('content').innerHTML = '<div class="v3-card"><div class="face-guidance" id="bio-camera-stage"><video id="bio-video" playsinline muted></video></div><button type="button" class="v3-secondary" id="bio-camera-cancel"></button></div>';
  const root = q('bio-camera-stage');
  const video = root.querySelector('video');
  const setGuidance = G.mountSafe(root);
  biometricGuidance = setGuidance;
  const cancel = q('bio-camera-cancel');
  cancel.textContent = V.t('cancel');
  cancel.onclick = async () => { stopBiometricCamera(); clearBiometricDraft(); await show('biometrics'); };
  setGuidance({ state: 'NEUTRAL', key: 'starting' });
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new DOMException('Camera unavailable', 'NotFoundError');
    const camera = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 960 } }, audio: false });
    if (controller.signal.aborted) { camera.getTracks().forEach(track => track.stop()); return; }
    biometricCamera = camera;
    video.srcObject = camera;
    await video.play();
    await G.waitForVideo(video, controller.signal);
    const photos = [];
    let count = 0;
    for (let attempt = 0; count < 3 && attempt < 70; attempt++) {
      setGuidance({ state: 'NEUTRAL', key: 'capturing' });
      await G.pause(800, controller.signal);
      const blob = await G.capture(video);
      if (controller.signal.aborted) return;
      const form = new FormData();
      form.append('photo', blob, 'frame.jpg');
      const result = await api('/api/v3/user/biometric-frame', { method: 'POST', body: form, signal: controller.signal });
      if (controller.signal.aborted) return;
      if (!result.accepted) {
        setGuidance(G.fromError(result.reason_code));
        await G.pause(650, controller.signal);
        continue;
      }
      const hint = G.position(G.captureGeometry(result.bounding_box, video), G.viewport(root));
      if (hint) { setGuidance({ state: 'INVALID', key: hint, hint }); await G.pause(650, controller.signal); }
      // Server accepts the source frame; cosmetic alignment is not a new biometric gate.
      photos.push(blob);
      count += 1;
      setGuidance({ state: 'CHALLENGE_SUCCESS', key: 'done', current_step: count, total_steps: 3 });
      await G.pause(450, controller.signal);
    }
    if (count !== 3) throw new Error('ENROLLMENT_TIMEOUT');
    renderBiometricPreview(photos);
  } catch (error) {
    if (controller.signal.aborted || error.name === 'AbortError') return;
    const view = G.fromError(error);
    if (view.technical) console.error('Biometric capture failed', error);
    setGuidance(view);
  } finally {
    if (biometricController === controller) {
      biometricCamera?.getTracks().forEach(track => track.stop());
      biometricCamera = null;
    }
  }
}

document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => show(button.dataset.view));
q('logout').onclick = async () => { await api('/api/v3/auth/logout', { method: 'POST' }); location.href = '/'; };
window.addEventListener('beforeunload', stopBiometricCamera);
window.addEventListener('v3:locale', () => {
  window.V3Theme.init();
  if (q('bio-camera-stage')) { q('bio-camera-cancel').textContent = V.t('cancel'); return; }
  if (biometricDraftPhotos.length) { renderBiometricPreview([...biometricDraftPhotos]); return; }
  if (me) show(currentView);
});

(async () => {
  V.init();
  window.V3Theme.init();
  me = await api('/api/v3/auth/me');
  if (me.role === 'ADMIN') { location.href = '/admin'; return; }
  q('identity').textContent = accountIdentity(me);
  await show('home');
})();
