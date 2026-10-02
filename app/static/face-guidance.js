(function () {
  'use strict';
  const STATES = Object.freeze(['NEUTRAL', 'INVALID', 'READY', 'CHALLENGE', 'CHALLENGE_SUCCESS', 'PROCESSING', 'SUCCESS', 'DENIED']);
  const actions = new Set(['CENTER', 'TURN_LEFT', 'TURN_RIGHT', 'BLINK']);
  const quality = { NO_FACE: 'noFace', MULTIPLE_FACES: 'multiple', FACE_TOO_SMALL: 'small', IMAGE_BLURRY: 'blurry', BAD_LIGHTING: 'lighting' };
  const decisions = { IDENTIFIED: ['SUCCESS', 'verified'], RECOVERY_VERIFIED: ['SUCCESS', 'verified'], UNKNOWN: ['DENIED', 'unknown', 'unknownDetail'], AMBIGUOUS: ['DENIED', 'ambiguous', 'retryDetail'], BLOCKED: ['DENIED', 'blocked'], DISABLED: ['DENIED', 'disabled'], DENIED: ['DENIED', 'denied'] };
  const mounted = new WeakMap();
  const t = (key, lang = document.documentElement.lang) => {
    const dictionary = window.BioGateI18n || (typeof BioGateV3Messages !== 'undefined' ? BioGateV3Messages : {});
    return dictionary[lang]?.['fg_' + key] || dictionary.en?.fg_failed || '';
  };
  function position(geometry, viewport = { width: 320, height: 240 }) {
    if (!geometry || ![geometry.x, geometry.y, geometry.width, geometry.height, geometry.image_width, geometry.image_height].every(Number.isFinite) || geometry.image_width <= 0 || geometry.image_height <= 0) return null;
    // Match object-fit:cover, then the CSS-only mirrored preview. JPEG stays unmirrored.
    const scale = Math.max(viewport.width / geometry.image_width, viewport.height / geometry.image_height);
    const x = viewport.width - ((geometry.x + geometry.width / 2) * scale - (geometry.image_width * scale - viewport.width) / 2);
    const y = (geometry.y + geometry.height / 2) * scale - (geometry.image_height * scale - viewport.height) / 2;
    const unit = Math.min(viewport.width / 320, viewport.height / 240);
    // Visual target corridor only, derived from the guide; never an acceptance threshold.
    const dx = (x - viewport.width / 2) / unit;
    const dy = (y - viewport.height / 2) / unit;
    if (Math.abs(dx) > 26 && Math.abs(dx) >= Math.abs(dy)) return dx < 0 ? 'moveRight' : 'moveLeft';
    if (Math.abs(dy) > 24) return dy < 0 ? 'moveDown' : 'moveUp';
    return null;
  }
  function fromBackend(payload, previous = {}, viewport) {
    if (!payload) return { state: 'NEUTRAL', key: 'neutral' };
    if (payload.completed === true) {
      const [state, key, detail] = decisions[payload.decision] || decisions.DENIED;
      return { state, key, detail };
    }
    if (quality[payload.reason_code]) return { state: 'INVALID', key: quality[payload.reason_code] };
    if (payload.accepted === true) {
      // expected_action is already the NEXT action after the server advances its index.
      return previous.expected_action === 'CENTER'
        ? { state: 'READY', key: 'ready' }
        : { state: 'CHALLENGE_SUCCESS', key: 'done' };
    }
    const expected = payload.expected_action;
    if (payload.guidance_reason === 'POSE_NOT_CENTERED') return { state: 'INVALID', key: 'CENTER' };
    if (payload.guidance_reason === 'UNSTABLE') return { state: 'INVALID', key: 'hold' };
    if (payload.guidance_reason === 'TURN_MORE_LEFT') return { state: 'CHALLENGE', key: 'turnMoreLeft', hint: 'TURN_LEFT' };
    if (payload.guidance_reason === 'TURN_MORE_RIGHT') return { state: 'CHALLENGE', key: 'turnMoreRight', hint: 'TURN_RIGHT' };
    if (payload.guidance_reason === 'BLINK_CENTER') return { state: 'CHALLENGE', key: 'blinkCenter', hint: 'BLINK' };
    const alignment = position(payload.face_geometry, viewport);
    if ((!expected || expected === 'CENTER') && alignment) return { state: 'INVALID', key: alignment, hint: alignment };
    const observed = payload.observed_action;
    const opposite = (expected === 'TURN_LEFT' && observed === 'TURN_RIGHT') || (expected === 'TURN_RIGHT' && observed === 'TURN_LEFT');
    if (opposite || (expected === 'CENTER' && ['TURN_LEFT', 'TURN_RIGHT'].includes(observed))) {
      return { state: 'INVALID', key: expected, detail: 'wrong' };
    }
    // CALIBRATING/BETWEEN_POSES/CENTER while waiting for a turn are not failures.
    return { state: expected === 'CENTER' ? 'NEUTRAL' : 'CHALLENGE', key: actions.has(expected) ? expected : 'hold', hint: expected };
  }
  function fromError(error) {
    const code = typeof error === 'string' ? error : error?.code || error?.message;
    if (quality[code]) return { state: 'INVALID', key: quality[code], recoverable: true };
    if (['NotAllowedError', 'SecurityError'].includes(error?.name)) return { state: 'NEUTRAL', key: 'cameraDenied', detail: 'retryDetail' };
    if (['NotFoundError', 'NotReadableError', 'OverconstrainedError'].includes(error?.name)) return { state: 'NEUTRAL', key: 'cameraUnavailable', detail: 'retryDetail' };
    if (['CHALLENGE_TIMEOUT', 'ENROLLMENT_TIMEOUT'].includes(code)) return { state: 'NEUTRAL', key: 'timeout', detail: 'retryDetail' };
    if (error?.status === 429) return { state: 'NEUTRAL', key: 'limited' };
    return { state: 'NEUTRAL', key: 'failed', detail: 'retryDetail', technical: true };
  }
  function mount(root) {
    if (!root) throw new Error('FaceGuidance requires its component root');
    if (mounted.has(root)) return mounted.get(root);
    const video = root.querySelector('video');
    if (!video) throw new Error('FaceGuidance requires a video inside its component root');
    root.classList.remove('camera-card', 'mini-camera');
    root.classList.add('face-guidance');
    const camera = document.createElement('div');
    camera.className = 'face-camera';
    camera.appendChild(video);
    camera.insertAdjacentHTML('beforeend', '<div class="face-guide-visual" aria-hidden="true"><svg class="face-guide-contour" viewBox="0 0 320 240" preserveAspectRatio="xMidYMid meet"><path d="M160 34 C120 34 105 54 104 87 C101 105 104 131 113 152 C122 174 142 202 160 204 C178 202 198 174 207 152 C216 131 219 105 216 87 C215 54 200 34 160 34 Z"/></svg><span class="face-guide-direction" data-face-guide-direction></span><span class="face-guide-confirm" data-face-guide-confirm></span></div>');
    const status = document.createElement('div');
    status.className = 'face-guide-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
    status.innerHTML = '<span class="face-guide-symbol" aria-hidden="true" data-face-guide-icon></span><div><strong data-face-guide-text></strong><p data-face-guide-detail></p><small data-face-guide-progress></small></div>';
    root.replaceChildren(camera, status);
    let current = { state: 'NEUTRAL', key: 'neutral' };
    function render(view = current) {
      current = view;
      root.dataset.guidanceState = STATES.includes(view.state) ? view.state : 'NEUTRAL';
      const icon = { READY: '✓', CHALLENGE_SUCCESS: '✓', SUCCESS: '✓', INVALID: '!', DENIED: '×', PROCESSING: '◌', CHALLENGE: '◎', NEUTRAL: '○' }[root.dataset.guidanceState];
      // Optional presentation nodes are scoped and may be omitted by a host layout.
      const write = (selector, value) => { const node = root.querySelector(selector); if (node && node.textContent !== value) node.textContent = value; };
      write('[data-face-guide-text]', t(view.key));
      write('[data-face-guide-detail]', view.detail ? t(view.detail) : '');
      write('[data-face-guide-icon]', view.key === 'blocked' ? '🔒' : icon);
      write('[data-face-guide-direction]', { moveLeft: '←', moveRight: '→', moveUp: '↑', moveDown: '↓', TURN_LEFT: '↶', TURN_RIGHT: '↷', BLINK: '◡ ◡', small: '↔' }[view.hint || view.key] || '');
      write('[data-face-guide-confirm]', ['READY', 'CHALLENGE_SUCCESS', 'SUCCESS'].includes(view.state) ? '✓' : view.state === 'DENIED' ? '×' : '');
      write('[data-face-guide-progress]', Number.isInteger(view.current_step) && Number.isInteger(view.total_steps) ? `${view.current_step} / ${view.total_steps}` : '');
    }
    const refresh = () => render();
    window.addEventListener('v3:locale', refresh);
    window.addEventListener('biogate:locale', refresh);
    render.destroy = () => { window.removeEventListener('v3:locale', refresh); window.removeEventListener('biogate:locale', refresh); mounted.delete(root); };
    mounted.set(root, render);
    render();
    return render;
  }
  function mountSafe(root) {
    let render;
    try { render = mount(root); } catch (error) { console.error('FaceGuidance initialization failed', error); }
    // Local presentation boundary: a broken optional view must not cancel capture/fetch.
    const safe = view => { try { render?.(view); } catch (error) { console.error('FaceGuidance rendering failed', error); } };
    safe.destroy = () => render?.destroy();
    return safe;
  }
  function pause(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new DOMException('Cancelled', 'AbortError')); return; }
      const abort = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
      const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, ms);
      signal?.addEventListener('abort', abort, { once: true });
    });
  }
  async function waitForVideo(video, signal) {
    const until = Date.now() + 10000;
    while (video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
      if (Date.now() >= until) throw new DOMException('Camera did not produce frames', 'NotReadableError');
      await pause(50, signal);
    }
  }
  async function capture(video) {
    if (!video.videoWidth || !video.videoHeight) throw new DOMException('Camera has no frame', 'NotReadableError');
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 560 / video.videoWidth);
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', .88));
    if (!blob) throw new Error('INVALID_IMAGE');
    return blob;
  }
  function frameDelay(action, accepted = false) {
    if (accepted) return 180;
    return action === 'BLINK' ? 45 : 120;
  }
  function createMetrics(log = (...args) => console.debug(...args)) {
    const state = { captures: 0, skipped: 0, inFlight: 0, lastCaptureAt: null, startedAt: null };
    let captureStartedAt = 0;
    let requestStartedAt = 0;
    return {
      captureStarted() {
        const now = Date.now();
        state.startedAt ??= now;
        captureStartedAt = now;
        const interval = state.lastCaptureAt === null ? null : now - state.lastCaptureAt;
        state.lastCaptureAt = now;
        state.captures++;
        return interval;
      },
      requestStarted() { requestStartedAt = Date.now(); state.inFlight++; },
      skipped() { state.skipped++; },
      completed(payload, captureInterval) {
        const now = Date.now();
        state.inFlight = Math.max(0, state.inFlight - 1);
        if (!payload?.debug) return;
        const elapsed = Math.max(now - state.startedAt, 1);
        log('BioGate liveness metrics', {
          capture_interval_ms: captureInterval,
          capture_duration_ms: requestStartedAt - captureStartedAt,
          request_duration_ms: now - requestStartedAt,
          backend_analysis_ms: payload.debug.analysisDurationMs,
          effective_analyzed_fps: Number((state.captures * 1000 / elapsed).toFixed(2)),
          skipped_frames: state.skipped,
          in_flight_requests: state.inFlight,
          challenge_state: {
            expected_action: payload.expected_action,
            current_step: payload.current_step,
            total_steps: payload.total_steps,
          },
        });
      },
      failed() { state.inFlight = Math.max(0, state.inFlight - 1); },
      snapshot() { return { ...state }; },
    };
  }
  function createGeometrySmoother(size = 3) {
    let samples = [];
    return {
      push(geometry) {
        if (!geometry) return geometry;
        samples.push(geometry);
        samples = samples.slice(-size);
        return Object.fromEntries(Object.keys(geometry).map(key => [key, samples.reduce((sum, item) => sum + item[key], 0) / samples.length]));
      },
      reset() { samples = []; },
    };
  }
  const viewport = root => { const node = root.querySelector('.face-camera'); return { width: node?.clientWidth || 320, height: node?.clientHeight || 240 }; };
  const captureGeometry = (box, video) => { const scale = Math.min(1, 560 / video.videoWidth); return { ...box, image_width: Math.round(video.videoWidth * scale), image_height: Math.round(video.videoHeight * scale) }; };
  window.FaceGuidance = Object.freeze({ STATES, t, fromBackend, fromError, mount, mountSafe, position, viewport, captureGeometry, pause, waitForVideo, capture, frameDelay, createMetrics, createGeometrySmoother });
}());
