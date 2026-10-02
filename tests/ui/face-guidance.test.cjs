// Runtime tests: execute shipped scripts against the IDs in the real login template.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const rootPath = path.resolve(__dirname, '../..');
const read = name => fs.readFileSync(path.join(rootPath, name), 'utf8');
class Element {
  constructor(tag = 'div', attrs = {}) {
    this.tagName = tag; this.attrs = attrs; this.children = []; this.dataset = {}; this._text = '';
    this.clientWidth = 320; this.clientHeight = 240;
    const classes = new Set((attrs.class || '').split(' '));
    this.classList = { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x) };
    for (const [k, v] of Object.entries(attrs)) if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
  }
  set textContent(value) { this._text = String(value); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(value) { this.children = parse(value).children; }
  setAttribute(key, value) { this.attrs[key] = value; }
  appendChild(child) { if (child.parent) child.parent.children = child.parent.children.filter(c => c !== child); child.parent = this; this.children.push(child); return child; }
  replaceChildren(...children) { this.children = []; children.forEach(c => this.appendChild(c)); }
  insertAdjacentHTML(_, html) { parse(html).children.slice().forEach(c => this.appendChild(c)); }
  matches(selector) {
    if (selector.startsWith('#')) return this.attrs.id === selector.slice(1);
    if (selector.startsWith('.')) return this.className === selector.slice(1) || this.classList.contains(selector.slice(1));
    if (selector.startsWith('[')) return selector.slice(1, -1) in this.attrs;
    return this.tagName === selector;
  }
  querySelectorAll(selector) { return this.children.flatMap(c => [...(c.matches(selector) ? [c] : []), ...c.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function parse(html) {
  const root = new Element('root'); const stack = [root];
  for (const m of html.matchAll(/<\/?([\w-]+)([^>]*?)>/g)) {
    if (m[0].startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
    const attrs = {}; for (const a of m[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] || '';
    const el = new Element(m[1], attrs); stack.at(-1).appendChild(el);
    if (!['input', 'meta', 'link', 'br'].includes(m[1])) stack.push(el);
  }
  return root;
}
function setup() {
  const dom = parse(read('app/templates/login.html'));
  dom.documentElement = { lang: 'ru', dataset: {} };
  dom.getElementById = id => dom.querySelector('#' + id);
  const draws = [];
  dom.createElement = tag => tag === 'canvas' ? { width: 0, height: 0, getContext: () => ({ drawImage: (...args) => draws.push(args) }), toBlob: callback => callback(new Blob(['synthetic'], { type: 'image/jpeg' })) } : new Element(tag);
  const video = dom.getElementById('camera-video');
  video.videoWidth = 640; video.videoHeight = 480; video.readyState = 2; video.play = async () => {};
  const timers = new Map(); let timerID = 0; let created = 0; let stopped = 0;
  const logs = [], requests = [], responses = [], events = new Map();
  const ctx = { document: dom, Blob, FormData, DOMException, AbortController, console: { error: (...a) => logs.push(a) },
    setTimeout: fn => { timers.set(++timerID, fn); return timerID; }, clearTimeout: id => timers.delete(id),
    localStorage: { getItem: () => null, setItem: () => {} }, CustomEvent: class { constructor(type) { this.type = type; } },
    location: { href: '/' },
    navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => stopped++ }] }) } },
    addEventListener: (name, fn) => { if (!events.has(name)) events.set(name, new Set()); events.get(name).add(fn); },
    removeEventListener: (name, fn) => events.get(name)?.delete(fn),
    dispatchEvent: event => events.get(event.type)?.forEach(fn => fn(event)),
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith('/challenges')) return { ok: true, json: async () => ({ challenge_id: 'test-' + ++created, expected_action: 'CENTER', current_step: 0, total_steps: 4 }) };
      const result = responses.shift() || { expected_action: 'CENTER', observed_action: 'CALIBRATING', accepted: false, current_step: 0, total_steps: 4 };
      if (result instanceof Error) throw result;
      return { ok: !result.status, status: result.status, json: async () => result.status ? { detail: { reason_code: result.code } } : result };
    },
  };
  ctx.window = ctx; vm.createContext(ctx);
  const run = file => vm.runInContext(read('app/static/' + file), ctx, { filename: file });
  run('v3-locales.js'); run('face-guidance-locales.js'); run('face-guidance.js');
  const flush = async () => { for (let i = 0; i < 35; i++) await Promise.resolve(); };
  const tick = async () => { const entry = timers.entries().next().value; if (entry) { timers.delete(entry[0]); entry[1](); } await flush(); };
  return { ctx, dom, video, requests, responses, logs, run, flush, tick, draws, stopped: () => stopped };
}
test('actual template starts camera, captures JPEG and continues analysis without legacy IDs', async () => {
  const h = setup(); h.run('login-v3.js');
  assert.equal(h.dom.getElementById('camera-action'), null);
  assert.equal(h.dom.getElementById('face-result'), null);
  h.dom.getElementById('face-login').onclick(); await h.flush();
  assert.equal(h.requests.filter(r => r.url.endsWith('/frames')).length, 1);
  await h.tick(); assert.equal(h.requests.filter(r => r.url.endsWith('/frames')).length, 2);
  assert.equal(h.draws[0][1], 0); assert.equal(h.logs.length, 0);
  h.dom.getElementById('camera-cancel').onclick(); await h.flush();
});
test('adaptive frame loop is fast for blink and keeps pose sampling bounded', () => {
  const G = setup().ctx.FaceGuidance;
  assert.equal(G.frameDelay('BLINK'), 45);
  assert.equal(G.frameDelay('TURN_LEFT'), 120);
  assert.equal(G.frameDelay('CENTER'), 120);
  assert.equal(G.frameDelay('BLINK', true), 180);
});
test('development metrics contain timing and state but no challenge identifier', () => {
  const rows = []; const metrics = setup().ctx.FaceGuidance.createMetrics((label, payload) => rows.push({ label, payload }));
  const interval = metrics.captureStarted(); metrics.requestStarted();
  metrics.completed({ expected_action: 'BLINK', current_step: 3, total_steps: 4, debug: { analysisDurationMs: 12 } }, interval);
  assert.equal(rows.length, 1); assert.equal(rows[0].payload.backend_analysis_ms, 12);
  assert.equal(rows[0].payload.challenge_state.expected_action, 'BLINK');
  assert.equal('challenge_id' in rows[0].payload, false);
});
test('face position guidance uses a short rolling window', () => {
  const smooth = setup().ctx.FaceGuidance.createGeometrySmoother(3);
  assert.equal(smooth.push({ x: 0, y: 0, width: 30, height: 30 }).x, 0);
  assert.equal(smooth.push({ x: 30, y: 0, width: 30, height: 30 }).x, 15);
  assert.equal(smooth.push({ x: 30, y: 0, width: 30, height: 30 }).x, 20);
  smooth.reset();
  assert.equal(smooth.push({ x: 9, y: 0, width: 30, height: 30 }).x, 9);
});
test('frame loop allows only one request in flight', async () => {
  const h = setup(); const original = h.ctx.fetch; let resolveFrame; let active = 0; let maximum = 0;
  h.ctx.fetch = async (url, options) => {
    if (!url.endsWith('/frames')) return original(url, options);
    active++; maximum = Math.max(maximum, active);
    return new Promise(resolve => { resolveFrame = payload => { active--; resolve({ ok: true, json: async () => payload }); }; });
  };
  h.run('login-v3.js'); h.dom.getElementById('face-login').onclick(); await h.flush();
  assert.equal(h.requests.filter(r => r.url.endsWith('/frames')).length, 0);
  assert.equal(active, 1); assert.equal(maximum, 1);
  await h.flush(); assert.equal(active, 1); assert.equal(maximum, 1);
  resolveFrame({ expected_action: 'CENTER', accepted: false, current_step: 0, total_steps: 4 }); await h.flush();
  await h.tick(); assert.equal(active, 1); assert.equal(maximum, 1);
  h.dom.getElementById('camera-cancel').onclick();
});
test('legacy camera-action write reproduces exact null DOM regression', () => {
  const h = setup(); assert.throws(() => { h.dom.getElementById('camera-action').textContent = 'CENTER'; }, /Cannot set properties of null/);
});
test('optional nodes absent and actual render exception cannot kill analysis', async () => {
  const h = setup(); h.run('login-v3.js');
  const stage = h.dom.getElementById('camera-stage');
  const status = stage.querySelector('.face-guide-status'); status.replaceChildren();
  h.dom.getElementById('face-login').onclick(); await h.flush();
  assert.equal(h.logs.length, 0);
  const original = stage.querySelector.bind(stage);
  stage.querySelector = selector => { if (selector === '[data-face-guide-text]') throw new Error('test render fault'); return original(selector); };
  await h.tick(); assert.equal(h.requests.filter(r => r.url.endsWith('/frames')).length, 2);
  assert.ok(h.logs.some(row => row[0] === 'FaceGuidance rendering failed'));
  h.dom.getElementById('camera-cancel').onclick();
});
test('backend accepted CENTER alone produces green READY; waiting/calibration never does', () => {
  const { ctx } = setup(), G = ctx.FaceGuidance;
  assert.equal(G.fromBackend({ expected_action: 'CENTER', observed_action: 'CENTER', accepted: false }).state, 'NEUTRAL');
  assert.equal(G.fromBackend({ accepted: true, expected_action: 'TURN_LEFT' }, { expected_action: 'CENTER' }).state, 'READY');
  assert.equal(G.fromBackend({ accepted: true, expected_action: 'TURN_RIGHT' }, { expected_action: 'TURN_LEFT' }).key, 'done');
});
for (const code of ['NO_FACE', 'FACE_TOO_SMALL', 'MULTIPLE_FACES', 'IMAGE_BLURRY', 'BAD_LIGHTING']) test(code + ' is recoverable red', () => {
  const G = setup().ctx.FaceGuidance; assert.equal(G.fromError(code).state, 'INVALID'); assert.equal(G.fromError(code).recoverable, true);
});
for (const action of ['TURN_LEFT', 'TURN_RIGHT', 'BLINK']) test(action + ' instruction retains backend semantics', () => {
  const G = setup().ctx.FaceGuidance; const view = G.fromBackend({ expected_action: action, observed_action: 'CENTER' });
  assert.equal(view.state, 'CHALLENGE'); assert.equal(view.key, action); assert.equal(view.hint, action);
});
test('opposite turn red; BETWEEN_POSES is not failure', () => {
  const G = setup().ctx.FaceGuidance;
  assert.equal(G.fromBackend({ expected_action: 'TURN_LEFT', observed_action: 'TURN_RIGHT' }).state, 'INVALID');
  assert.equal(G.fromBackend({ expected_action: 'TURN_RIGHT', observed_action: 'BETWEEN_POSES' }).state, 'CHALLENGE');
});
for (const [decision, state, key] of [['IDENTIFIED','SUCCESS','verified'],['RECOVERY_VERIFIED','SUCCESS','verified'],['UNKNOWN','DENIED','unknown'],['AMBIGUOUS','DENIED','ambiguous'],['BLOCKED','DENIED','blocked'],['DISABLED','DENIED','disabled']]) test(decision + ' preserves its meaning', () => {
  const view = setup().ctx.FaceGuidance.fromBackend({ completed: true, decision }); assert.equal(view.state, state); assert.equal(view.key, key);
});
test('generic DENIED stays controlled and never exposes the machine code', () => {
  const view=setup().ctx.FaceGuidance.fromBackend({completed:true,decision:'DENIED'});
  assert.equal(view.state,'DENIED'); assert.equal(view.key,'denied'); assert.notEqual(view.detail,'DENIED');
});
test('position coach maps mirrored preview and object-fit cover; alignment never accepts', () => {
  const G = setup().ctx.FaceGuidance;
  const geo = { x: 140, y: 100, width: 40, height: 40, image_width: 320, image_height: 240 };
  assert.equal(G.position(geo), null);
  assert.equal(G.position({ ...geo, x: 250 }), 'moveRight');
  assert.equal(G.position({ ...geo, x: 20 }), 'moveLeft');
  assert.equal(G.position({ ...geo, y: 10 }), 'moveDown');
  assert.equal(G.position({ ...geo, y: 180 }), 'moveUp');
  assert.equal(G.position({ ...geo, image_width: 640, image_height: 480, x: 280, y: 200, width: 80, height: 80 }, { width: 390, height: 292.5 }), null);
  assert.equal(G.fromBackend({ expected_action: 'CENTER', face_geometry: geo }).state, 'NEUTRAL');
});
test('raw errors are logged, localized and terminal; retry creates a fresh challenge', async () => {
  const h = setup(); h.responses.push(new TypeError("Cannot set properties of null (setting 'textContent')")); h.run('login-v3.js');
  h.dom.getElementById('face-login').onclick(); await h.flush();
  const stage = h.dom.getElementById('camera-stage');
  assert.match(stage.textContent, /Не удалось выполнить проверку лица/); assert.doesNotMatch(stage.textContent, /TypeError|textContent|REQUEST_FAILED/);
  assert.ok(h.logs.length); assert.equal(h.dom.getElementById('camera-retry').classList.contains('v3-hidden'), false);
  h.dom.getElementById('camera-retry').onclick(); await h.flush();
  assert.ok(h.requests.some(r => r.url.includes('/test-2/frames')));
  h.dom.getElementById('camera-cancel').onclick();
});
test('RU/EN refreshes current state without camera restart and optional progress is safe', () => {
  const h = setup(), G = h.ctx.FaceGuidance, stage = h.dom.getElementById('camera-stage'); const render = G.mount(stage);
  render({ state: 'CHALLENGE', key: 'TURN_LEFT', hint: 'TURN_LEFT' }); assert.match(stage.textContent, /Поверните голову влево/);
  h.ctx.V3Locale.set('en'); assert.match(stage.textContent, /Turn your head left/); assert.equal(stage.dataset.guidanceState, 'CHALLENGE');
  render({ state: 'PROCESSING', key: 'processing' }); assert.match(stage.textContent, /Verifying/);
});
test('unknown persists and retry/fallback stop camera and old scheduling', async () => {
  const h = setup(); h.responses.push({ completed: true, decision: 'UNKNOWN' }); h.run('login-v3.js');
  h.dom.getElementById('face-login').onclick(); await h.flush();
  assert.equal(h.dom.getElementById('camera-stage').dataset.guidanceState, 'DENIED'); assert.ok(h.stopped());
  const count = h.requests.length; await h.tick(); assert.equal(h.requests.length, count);
  h.dom.getElementById('camera-cancel').onclick(); assert.equal(h.dom.getElementById('password-panel').classList.contains('v3-hidden'), false);
});
test('camera readiness prevents empty frames and cancellation prevents delayed start', async () => {
  const h = setup(); h.video.readyState = 0; h.video.videoWidth = 0; h.run('login-v3.js');
  h.dom.getElementById('face-login').onclick(); await h.flush(); assert.equal(h.requests.length, 0);
  h.video.readyState = 2; h.video.videoWidth = 640; await h.tick(); assert.ok(h.requests.some(r => r.url.endsWith('/frames')));
  h.dom.getElementById('camera-cancel').onclick(); const count = h.requests.length; await h.tick(); assert.equal(h.requests.length, count);
});
test('accepted step stays visible for confirmation then uses NEXT backend action', async () => {
  const h = setup(); h.responses.push({accepted:true,expected_action:'TURN_LEFT',current_step:1,total_steps:4}); h.run('login-v3.js');
  h.dom.getElementById('face-login').onclick(); await h.flush();
  const stage=h.dom.getElementById('camera-stage'); assert.equal(stage.dataset.guidanceState,'READY');
  assert.match(stage.textContent,/Положение правильное/);
  await h.tick(); assert.equal(stage.dataset.guidanceState,'CHALLENGE'); assert.match(stage.textContent,/Поверните голову влево/);
  h.dom.getElementById('camera-cancel').onclick();
});
test('identified result confirms before existing redirect and cancellation cancels redirect', async () => {
  const h=setup(); h.responses.push({completed:true,decision:'IDENTIFIED',account:{role:'USER',csrf_token:'test'}}); h.run('login-v3.js');
  h.dom.getElementById('face-login').onclick(); await h.flush();
  assert.equal(h.ctx.location.href,'/'); assert.equal(h.dom.getElementById('camera-stage').dataset.guidanceState,'SUCCESS');
  await h.tick(); assert.equal(h.ctx.location.href,'/user');
  const other=setup(); other.responses.push({completed:true,decision:'IDENTIFIED',account:{role:'USER',csrf_token:'test'}}); other.run('login-v3.js');
  other.dom.getElementById('face-login').onclick(); await other.flush(); other.dom.getElementById('camera-cancel').onclick(); await other.tick(); assert.equal(other.ctx.location.href,'/');
});
test('camera failure is not UNKNOWN and does not create a challenge', async () => {
  const h=setup(); h.ctx.navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('denied','NotAllowedError');}; h.run('login-v3.js');
  h.dom.getElementById('face-login').onclick(); await h.flush();
  assert.equal(h.requests.length,0); assert.equal(h.dom.getElementById('camera-stage').dataset.guidanceState,'NEUTRAL');
  assert.match(h.dom.getElementById('camera-stage').textContent,/камере/);
});
test('cancel while permission pending stops late stream and sends no frames', async () => {
  const h=setup(); let resolveCamera; let stops=0;
  h.ctx.navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{resolveCamera=resolve;}); h.run('login-v3.js');
  h.dom.getElementById('face-login').onclick(); await h.flush(); h.dom.getElementById('camera-cancel').onclick();
  resolveCamera({getTracks:()=>[{stop:()=>stops++}]}); await h.flush();
  assert.equal(stops,1); assert.equal(h.requests.length,0);
});
test('explicit backend unstable/pose observations are red, not geometric guesses',()=>{
  const G=setup().ctx.FaceGuidance;
  assert.equal(G.fromBackend({expected_action:'CENTER',guidance_reason:'UNSTABLE'}).key,'hold');
  assert.equal(G.fromBackend({expected_action:'CENTER',guidance_reason:'POSE_NOT_CENTERED'}).state,'INVALID');
});
test('backend supplies actionable turn and blink guidance without changing acceptance',()=>{
  const G=setup().ctx.FaceGuidance;
  assert.equal(G.fromBackend({expected_action:'TURN_LEFT',guidance_reason:'TURN_MORE_LEFT'}).key,'turnMoreLeft');
  assert.equal(G.fromBackend({expected_action:'TURN_RIGHT',guidance_reason:'TURN_MORE_RIGHT'}).key,'turnMoreRight');
  assert.equal(G.fromBackend({expected_action:'BLINK',guidance_reason:'BLINK_CENTER'}).key,'blinkCenter');
});
test('late old challenge creation cannot replace the new challenge ID', async()=>{
  const h=setup(); const original=h.ctx.fetch; let finishOld; let first=true;
  h.ctx.fetch=async(url, options)=>{
    if(url.endsWith('/challenges') && first) { first=false; return new Promise(resolve=>{finishOld=()=>resolve({ok:true,json:async()=>({challenge_id:'obsolete',expected_action:'CENTER',current_step:0,total_steps:4})});}); }
    return original(url,options);
  };
  h.run('login-v3.js'); h.dom.getElementById('face-login').onclick(); await h.flush();
  h.dom.getElementById('camera-cancel').onclick(); h.dom.getElementById('face-login').onclick(); await h.flush();
  finishOld(); await h.flush(); await h.tick();
  assert.ok(h.requests.filter(r=>r.url.endsWith('/frames')).length>=2);
  assert.ok(h.requests.every(r=>!r.url.includes('obsolete'))); h.dom.getElementById('camera-cancel').onclick();
});
test('late frame response from an old challenge cannot complete a new challenge', async()=>{
  const h=setup(); const original=h.ctx.fetch; let resolveOld;
  h.ctx.fetch=async(url,options)=>{
    if(url.includes('/test-1/frames')) return new Promise(resolve=>{resolveOld=()=>resolve({ok:true,json:async()=>({completed:true,decision:'IDENTIFIED',account:{role:'USER',csrf_token:'old'}})});});
    return original(url,options);
  };
  h.run('login-v3.js'); h.dom.getElementById('face-login').onclick(); await h.flush();
  h.dom.getElementById('camera-cancel').onclick(); h.dom.getElementById('face-login').onclick(); await h.flush();
  assert.ok(h.requests.some(r=>r.url.includes('/test-2/frames')));
  resolveOld(); await h.flush();
  assert.equal(h.ctx.location.href,'/');
  assert.ok(h.requests.every(r=>!r.url.includes('/test-1/frames') || r.url.endsWith('/frames')));
  h.dom.getElementById('camera-cancel').onclick();
});
