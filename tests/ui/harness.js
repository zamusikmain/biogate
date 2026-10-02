/* TEST ONLY. Served exclusively by tests.ui.qa_server, never by production. */
(function () {
  const originalFetch = window.fetch.bind(window);
  let frame = 0, sessions = 0, frames = 0;
  let outcome = 'UNKNOWN';
  let held = false;
  const base = { total_steps: 4, completed: false, quality_score: .9 };
  const observation = (expected_action, observed_action, accepted, current_step) => ({ ...base, expected_action, observed_action, accepted, current_step });
  const geometry = { x: 115, y: 65, width: 90, height: 110, image_width: 320, image_height: 240 };
  const scenarios = {
    INITIAL: [null], NO_FACE: [{ reason_code: 'NO_FACE' }], FACE_TOO_SMALL: [{ reason_code: 'FACE_TOO_SMALL' }],
    MULTIPLE_FACES: [{ reason_code: 'MULTIPLE_FACES' }], BAD_LIGHTING: [{ reason_code:'BAD_LIGHTING' }],
    BLUR: [{ reason_code:'IMAGE_BLURRY' }], UNSTABLE: [{ expected_action:'CENTER', guidance_reason:'UNSTABLE' }],
    LEFT_OFFSET: [{ expected_action: 'CENTER', face_geometry: { ...geometry, x: 210 } }],
    RIGHT_OFFSET: [{ expected_action: 'CENTER', face_geometry: { ...geometry, x: 15 } }],
    UP: [{ expected_action: 'CENTER', face_geometry: { ...geometry, y: 5 } }],
    DOWN: [{ expected_action: 'CENTER', face_geometry: { ...geometry, y: 140 } }],
    READY: [observation('TURN_LEFT','CENTER',true,1), { expected_action: 'CENTER' }],
    TURN_LEFT: [observation('TURN_LEFT','CENTER',false,1)],
    TURN_RIGHT: [observation('TURN_RIGHT','CENTER',false,2)],
    BLINK: [observation('BLINK','BLINK',false,3)],
    WRONG: [observation('TURN_LEFT','TURN_RIGHT',false,1)],
    CHALLENGE_SUCCESS: [observation('TURN_RIGHT','TURN_LEFT',true,2), { expected_action: 'TURN_LEFT' }],
    PROCESSING: [], SUCCESS: [{ completed:true,decision:'IDENTIFIED' }],
    UNKNOWN: [{ completed:true,decision:'UNKNOWN' }], AMBIGUOUS: [{ completed:true,decision:'AMBIGUOUS' }],
    BLOCKED: [{ completed:true,decision:'BLOCKED' }], DISABLED: [{ completed:true,decision:'DISABLED' }],
    DENIED: [{ completed:true,decision:'DENIED' }], CAMERA_FAILURE: [{ ui_error:'CAMERA_PERMISSION_DENIED' }],
    NETWORK_FAILURE: [{ ui_error:'REQUEST_FAILED' }],
  };
  const response = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  // Synthetic frames have no personal or biometric data. No device permission is requested.
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: async () => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 480;
    const context = canvas.getContext('2d');
    context.fillStyle = '#bac9cd'; context.fillRect(0,0,640,480);
    context.fillStyle = '#688497'; context.beginPath(); context.ellipse(320,240,100,145,0,0,Math.PI*2); context.fill();
    context.fillStyle = '#334b5d'; context.font = '18px sans-serif'; context.fillText('SYNTHETIC VIDEO / UI TEST', 190, 445);
    return canvas.captureStream(5);
  } });
  window.fetch = async (url, options = {}) => {
    const endpoint = String(url);
    if (endpoint === '/api/v3/face/challenges' || endpoint === '/api/v3/recovery/challenges') {
      frame = 0; sessions++;
      return response({ ...base, challenge_id: 'ui-' + sessions, expected_action:'CENTER',current_step:0 });
    }
    if (/\/face\/challenges\/[^/]+\/frames$/.test(endpoint)) {
      frames++;
      document.querySelector('#qa-count').textContent = `sessions=${sessions}; frames=${frames}`;
      if (held) return response(observation('CENTER','CALIBRATING',false,0));
      const sequence = [
        { status:422, detail:{reason_code:'NO_FACE'} },
        observation('CENTER','CALIBRATING',false,0),
        { ...observation('CENTER','CALIBRATING',false,0), face_geometry:{...geometry,x:210} },
        observation('TURN_LEFT','CENTER',true,1),
        observation('TURN_LEFT','CENTER',false,1),
        observation('TURN_LEFT','TURN_RIGHT',false,1),
        observation('TURN_RIGHT','TURN_LEFT',true,2),
        observation('TURN_RIGHT','CENTER',false,2),
        observation('BLINK','TURN_RIGHT',true,3),
        observation('BLINK','BLINK',false,3),
        { completed:true,decision:outcome,account:{csrf_token:'test-only',role:'USER',must_change_password:true} },
      ];
      const result = sequence[Math.min(frame++,sequence.length-1)];
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, 120);
        options.signal?.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Cancelled','AbortError')); }, {once:true});
      });
      return response(result, result.status || 200);
    }
    if (endpoint.endsWith('/enrollment/validate') || endpoint === '/api/v3/user/biometric-frame') {
      const chosen = document.querySelector('#qa-state')?.value;
      if (['NO_FACE','FACE_TOO_SMALL','MULTIPLE_FACES'].includes(chosen)) return response({accepted:false,reason_code:chosen});
      return response({accepted:true,reason_code:'OK',quality_score:.9,yaw:0,bounding_box:{x:200,y:110,width:160,height:190}});
    }
    if (endpoint.endsWith('/enrollment/photos')) return response({template_created:true,accepted_frames:3,required_frames:3,results:[]});
    if (endpoint === '/api/v3/user/biometric-requests' && options.method === 'POST') return response({id:'ui-only',status:'PENDING_REVIEW'});
    return originalFetch(url, options);
  };
  window.addEventListener('DOMContentLoaded', () => {
    const tools = document.createElement('aside'); tools.id='qa-tools';
    tools.style.cssText='position:relative;grid-column:1/-1;padding:12px;background:#e6edf3;color:#142d44;max-width:100%;font:13px sans-serif;';
    tools.innerHTML='<strong>TEST ONLY — synthetic video and responses</strong> <a href="/">Login</a> <a href="/__qa/session/admin">Admin</a> <a href="/__qa/session/user">User</a><div id="qa-count">sessions=0; frames=0</div><select id="qa-state" aria-label="QA state"></select><button id="qa-preview">Preview state</button><button id="qa-run">Run face flow</button><button id="qa-hold">Hold calibration</button><button id="qa-check">Check all visuals</button><pre id="qa-report"></pre>';
    document.body.appendChild(tools);
    document.addEventListener('click', event => { if(event.target.id==='start-webcam-enroll') document.querySelector('#enroll-dialog').appendChild(tools); });
    document.querySelector('#enroll-dialog')?.addEventListener('close',()=>document.body.appendChild(tools));
    tools.querySelector('#qa-report').style.cssText='max-height:140px;overflow:auto;white-space:pre-wrap;';
    const select = tools.querySelector('select');
    for (const name of Object.keys(scenarios)) { const option = document.createElement('option'); option.value=name; option.textContent=name; select.appendChild(option); }
    const display = name => {
      if (typeof stopCamera === 'function') stopCamera();
      document.querySelector('#password-panel')?.classList.add('v3-hidden');
      document.querySelector('#camera-panel')?.classList.remove('v3-hidden');
      const root = document.querySelector('#camera-stage') || document.querySelector('#admin-video')?.closest('.face-guidance') || document.querySelector('#bio-camera-stage');
      if (!root) throw new Error('Open a capture component first');
      const G=window.FaceGuidance;
      const [payload, previous] = scenarios[name];
      const view = name === 'PROCESSING' ? {state:'PROCESSING',key:'processing'} : payload?.ui_error ? G.fromError(payload.ui_error) : G.fromBackend(payload, previous || {}, G.viewport(root));
      G.mount(root)(view);
      return root;
    };
    tools.querySelector('#qa-preview').onclick=()=>display(select.value);
    tools.querySelector('#qa-run').onclick=()=> { held=false; outcome=['SUCCESS','UNKNOWN','AMBIGUOUS','BLOCKED','DISABLED','DENIED'].includes(select.value) ? (select.value==='SUCCESS'?'IDENTIFIED':select.value) : 'UNKNOWN'; if(typeof startFace==='function') startFace(false); };
    tools.querySelector('#qa-hold').onclick=()=> {held=true; if(typeof startFace==='function') startFace(false);};
    tools.querySelector('#qa-check').onclick=()=> {
      const results=[];
      for(const lang of ['ru','en']) for(const theme of ['light','dark','system']) {
        window.V3Locale.set(lang); window.V3Theme.set(theme);
        for(const name of Object.keys(scenarios)) {
          const root=display(name), text=root.querySelector('[data-face-guide-text]').textContent;
          const contour=root.querySelector('.face-guide-contour path');
          if(!text || !contour || /fg_|TypeError|UNKNOWN/.test(text)) throw new Error('Bad rendered state '+name);
          const color=getComputedStyle(contour).stroke;
          if(!color || color==='none') throw new Error('Missing contour color');
          const direction=root.querySelector('[data-face-guide-direction]').textContent;
          const arrows={LEFT_OFFSET:'→',RIGHT_OFFSET:'←',UP:'↓',DOWN:'↑',TURN_LEFT:'↶',TURN_RIGHT:'↷',BLINK:'◡ ◡'};
          if(arrows[name] && direction!==arrows[name]) throw new Error('Wrong arrow '+name);
          const video=root.querySelector('.face-camera').getBoundingClientRect();
          const status=root.querySelector('.face-guide-status').getBoundingClientRect();
          if(status.top < video.bottom-1) throw new Error('Status overlaps camera');
          results.push(`${lang}/${theme}/${name}: ${root.dataset.guidanceState} ${text}`);
        }
      }
      const root=display('TURN_LEFT'); const optional=root.querySelector('[data-face-guide-progress]'); optional.remove();
      window.FaceGuidance.mount(root)({state:'CHALLENGE',key:'TURN_RIGHT',hint:'TURN_RIGHT'});
      results.push('Optional progress removed: PASS');
      tools.querySelector('#qa-report').textContent=`PASS ${results.length} checks\n`+results.join('\n');
    };
  });
}());
