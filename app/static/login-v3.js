const q = id => document.getElementById(id);
const V = window.V3Locale;
let stream = null;
let challenge = null;
let faceRun = 0;
let faceAbort = null;
let faceMode = { recovery: false, login: '' };
let resetAuthorization = null;
let csrfToken = '';
const G = window.FaceGuidance;
const cameraPanel = q('camera-panel');
const stage = cameraPanel.querySelector('#camera-stage');
const video = stage.querySelector('video');
const setGuidance = G.mountSafe(stage);
const retry = cameraPanel.querySelector('#camera-retry');
const frameMetrics = G.createMetrics();
const geometrySmoother = G.createGeometrySmoother(3);
let frameRequestInFlight = false;
let frameRequestOwner = -1;

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(body.detail?.reason_code || 'error'), { status: response.status });
  return body;
}

function panel(id) {
  ['password-panel', 'recovery-panel', 'camera-panel', 'reset-panel', 'forced-panel']
    .forEach(name => q(name).classList.toggle('v3-hidden', name !== id));
}
function json(method, value) {
  return { method, headers: { 'Content-Type': 'application/json', ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}) }, body: JSON.stringify(value) };
}
function redirect(account) { location.href = account.role === 'ADMIN' ? '/admin' : '/user'; }
function userError(error) {
  const message = V.t(error.message);
  if (message !== error.message) return message;
  console.error('Authentication request failed', error);
  return G.t('failed');
}

q('password-form').onsubmit = async event => {
  event.preventDefault();
  q('login-error').textContent = '';
  const data = Object.fromEntries(new FormData(event.target));
  try {
    const account = await request('/api/v3/auth/password', json('POST', data));
    csrfToken = account.csrf_token;
    if (account.must_change_password) { panel('forced-panel'); return; }
    redirect(account);
  } catch (error) { q('login-error').textContent = userError(error); }
};

q('forgot').onclick = () => panel('recovery-panel');
q('recovery-cancel').onclick = () => panel('password-panel');
q('face-login').onclick = () => startFace(false);
q('recovery-start').onsubmit = async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  await startFace(true, data.login);
};
q('camera-cancel').onclick = () => { stopCamera(); panel('password-panel'); };
retry.onclick = () => startFace(faceMode.recovery, faceMode.login);

async function startFace(recovery, login = '') {
  stopCamera();
  geometrySmoother.reset();
  const run = faceRun;
  const controller = new AbortController();
  faceAbort = controller;
  faceMode = { recovery, login };
  panel('camera-panel');
  retry.classList.add('v3-hidden');
  setGuidance({ state: 'NEUTRAL', key: 'starting' });
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new DOMException('Camera unavailable', 'NotFoundError');
    const acquired = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 960 }, height: { ideal: 720 } }, audio: false });
    if (run !== faceRun) { acquired.getTracks().forEach(track => track.stop()); return; }
    stream = acquired;
    video.srcObject = acquired;
    await video.play();
    // A playing element may still have zero-sized frames during camera startup.
    await G.waitForVideo(video, controller.signal);
    if (run !== faceRun) return;
    const created = await request(
      recovery ? '/api/v3/recovery/challenges' : '/api/v3/face/challenges',
      { ...(recovery ? json('POST', { login }) : { method: 'POST' }), signal: controller.signal },
    );
    if (run !== faceRun) return;
    challenge = created;
    setGuidance(G.fromBackend(challenge));
    await analyze(run, controller.signal);
  } catch (error) {
    if (run !== faceRun || error.name === 'AbortError') return;
    const view = G.fromError(error);
    if (view.technical) console.error('Face verification failed', error);
    setGuidance(view);
    stopCamera();
    retry.classList.remove('v3-hidden');
  }
}

async function analyze(run, signal) {
  while (run === faceRun && stream && challenge) {
    if (frameRequestInFlight && frameRequestOwner === run) {
      frameMetrics.skipped();
      await G.pause(G.frameDelay(challenge.expected_action), signal);
      continue;
    }
    const previous = challenge;
    // Show processing only while the last liveness action is submitted.
    if (previous.current_step === previous.total_steps - 1) setGuidance({ state: 'PROCESSING', key: previous.expected_action, detail: 'processing', hint: previous.expected_action });
    let result;
    let captureInterval = null;
    try {
      frameRequestInFlight = true;
      frameRequestOwner = run;
      captureInterval = frameMetrics.captureStarted();
      const frame = await G.capture(video);
      if (run !== faceRun) return;
      const form = new FormData();
      form.append('frame', frame, 'frame.jpg');
      frameMetrics.requestStarted();
      result = await request(`/api/v3/face/challenges/${previous.challenge_id}/frames`, { method: 'POST', body: form, signal });
      frameMetrics.completed(result, captureInterval);
    } catch (error) {
      frameMetrics.failed();
      if (run !== faceRun || error.name === 'AbortError') return;
      const view = G.fromError(error);
      if (!view.recoverable) throw error;
      setGuidance(view);
      await G.pause(previous.expected_action === 'BLINK' ? 180 : 300, signal);
      continue;
    } finally {
      if (frameRequestOwner === run) {
        frameRequestInFlight = false;
        frameRequestOwner = -1;
      }
    }
    if (run !== faceRun) return;
    const displayResult = result.face_geometry
      ? { ...result, face_geometry: geometrySmoother.push(result.face_geometry) }
      : result;
    const view = G.fromBackend(displayResult, previous, G.viewport(stage));
    setGuidance({ ...view, current_step: result.current_step, total_steps: result.total_steps });
    if (result.completed) {
      releaseCamera();
      challenge = null;
      if (view.state === 'SUCCESS') {
        // Presentation only: this delay cannot accept an action or identity.
        await G.pause(500, signal);
        if (run !== faceRun) return;
        if (result.decision === 'RECOVERY_VERIFIED') {
          resetAuthorization = result.reset_authorization;
          panel('reset-panel');
        } else {
          csrfToken = result.account.csrf_token;
          if (result.account.must_change_password) panel('forced-panel');
          else redirect(result.account);
        }
      } else retry.classList.remove('v3-hidden');
      return;
    }
    challenge = { ...previous, ...result };
    if (result.accepted) {
      await G.pause(G.frameDelay(previous.expected_action, true), signal);
      if (run !== faceRun) return;
      setGuidance(G.fromBackend({ expected_action: challenge.expected_action }));
      await G.pause(G.frameDelay(challenge.expected_action), signal);
    } else await G.pause(G.frameDelay(challenge.expected_action), signal);
  }
}

function releaseCamera() {
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
}
function stopCamera() {
  faceRun++;
  if (faceAbort) faceAbort.abort();
  faceAbort = null;
  releaseCamera();
  challenge = null;
  // The backend has no cancel endpoint; abandoned challenges expire via existing TTL.
}

q('reset-form').onsubmit = async event => {
  event.preventDefault();
  q('reset-error').textContent = '';
  const data = Object.fromEntries(new FormData(event.target));
  const policyError = window.BioGatePasswordPolicy.validate(data.password);
  if (policyError) { q('reset-error').textContent = V.t(policyError); return; }
  if (data.password !== data.confirmation) { q('reset-error').textContent = V.t('PASSWORD_CONFIRMATION_MISMATCH'); return; }
  try {
    await request('/api/v3/recovery/reset', json('POST', { token: resetAuthorization, new_password: data.password, confirmation: data.confirmation }));
    resetAuthorization = null;
    panel('password-panel');
  } catch (error) { q('reset-error').textContent = userError(error); }
};

q('forced-form').onsubmit = async event => {
  event.preventDefault();
  q('forced-error').textContent = '';
  const data = Object.fromEntries(new FormData(event.target));
  const policyError = window.BioGatePasswordPolicy.validate(data.password);
  if (policyError) { q('forced-error').textContent = V.t(policyError); return; }
  if (data.password !== data.confirmation) { q('forced-error').textContent = V.t('PASSWORD_CONFIRMATION_MISMATCH'); return; }
  try {
    await request('/api/v3/auth/change-password', json('POST', { current_password: data.current, new_password: data.password, confirmation: data.confirmation }));
    const account = await request('/api/v3/auth/me');
    redirect({ ...account, must_change_password: false });
  } catch (error) { q('forced-error').textContent = userError(error); }
};

window.addEventListener('beforeunload', stopCamera);
V.init();
window.V3Theme.init();
