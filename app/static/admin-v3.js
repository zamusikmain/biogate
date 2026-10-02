let v3LiveSource = null;
const v3Page = { access: 0, biometric: 0, security: 0, unknown: 0, archive: 0 };
const v3PageSize = 20;
const accessFilter = { result:'', method:'', user:'', period:'', storage:'' };

window.loadBioGateV3Admin = async view => {
  if (v3LiveSource && view !== 'live') {
    v3LiveSource.close();
    v3LiveSource = null;
  }
  if (!accountAuth && ['access', 'biometric-requests', 'security-events', 'unknown-faces', 'archive', 'live', 'backups'].includes(view)) {
    const target = document.querySelector(`#${view} > div:last-child`);
    if (target) target.innerHTML = `<div class="data-card">${L.t('v3AccountRequired')}</div>`;
    return;
  }
  if (view === 'access') await loadV3Access();
  if (view === 'events') await loadV3Events();
  if (view === 'biometric-requests') await loadV3BiometricRequests();
  if (view === 'security-events') await loadV3SecurityEvents();
  if (view === 'unknown-faces') await loadV3UnknownFaces();
  if (view === 'archive') await loadV3Archive();
  if (view === 'live') loadV3Live();
  if (view === 'backups') await loadV3Backups();
  if (view === 'settings' && accountAuth) await loadV3Settings();
};

async function loadV3Access() {
  const query=new URLSearchParams({limit:String(v3PageSize),offset:String(v3Page.access*v3PageSize)});
  if(accessFilter.user)query.set('user',accessFilter.user);if(accessFilter.method)query.set('method',accessFilter.method);if(accessFilter.result)query.set('result',accessFilter.result);
  if(accessFilter.period){const days=Number(accessFilter.period);const from=new Date(Date.now()-days*86400000);query.set('date_from',from.toISOString().slice(0,10));}
  const rows = await api(`/api/v3/admin/access-events?${query}`);
  $('v3-access').innerHTML=`<div class="access-live"><span class="live-indicator">● ${L.t('realtime')}</span><div id="v3-access-live">${L.t('waitingForEvents')}</div></div><div class="filter-bar access-filters"><input data-access-filter="user" value="${esc(accessFilter.user)}" placeholder="${L.t('searchUsers')}"><select data-access-filter="result"><option value="">${L.t('allResultsFilter')}</option><option value="SUCCESS">${L.t('successful')}</option><option value="DENIED">${L.t('deniedFilter')}</option><option value="UNKNOWN">${L.t('unknown')}</option><option value="BLOCKED">${L.t('blocked')}</option><option value="AMBIGUOUS">${L.t('ambiguousFilter')}</option></select><select data-access-filter="method"><option value="">${L.t('allMethods')}</option><option value="FACE">${L.t('FACE')}</option><option value="PASSWORD">${L.t('PASSWORD')}</option></select><select data-access-filter="period"><option value="">${L.t('allPeriods')}</option><option value="1">${L.t('today')}</option><option value="7">${L.t('days7')}</option><option value="30">${L.t('days30')}</option><option value="60">${L.t('days60')}</option></select><button type="button" data-access-apply>${L.t('apply')}</button></div><div id="v3-access-table"></div>`;
  document.querySelectorAll('[data-access-filter]').forEach(control=>control.value=accessFilter[control.dataset.accessFilter]||'');
  table('v3-access-table', [L.t('time'), L.t('user'), L.t('photo'), L.t('method'), L.t('result'), L.t('similarity'), L.t('reason'), L.t('actions')], rows.map(row => [
    date(row.timestamp), accountIdentityCell(row), row.photo_id?evidenceThumbnail(row.photo_id):L.t('photoAbsent'), esc(humanMethod(row.method)), badge(row.result), pct(row.similarity), esc(humanCode(row.reason)), eventDetailsButton(row, 'access'),
  ]));
  renderAdminPager('v3-access-table', v3Page.access, rows.length, page => { v3Page.access = page; loadV3Access(); });
  document.querySelector('[data-access-apply]').onclick=()=>{document.querySelectorAll('[data-access-filter]').forEach(control=>accessFilter[control.dataset.accessFilter]=control.value);v3Page.access=0;loadV3Access();};
  bindEventDetailButtons();
  bindEvidenceErrors();
  startAccessLive();
}

function evidenceThumbnail(photoId){const id=encodeURIComponent(photoId);return `<button type="button" class="request-photo access-photo" data-access-photo="${esc(photoId)}" aria-label="${L.t('evidencePreview')}"><img class="archive-thumb" data-evidence-image src="/api/v3/photos/${id}" alt="${L.t('evidencePreview')}"></button>`;}

function startAccessLive(){if(v3LiveSource)v3LiveSource.close();v3LiveSource=new EventSource('/api/v3/admin/live');v3LiveSource.addEventListener('access',event=>{const row=JSON.parse(event.data),feed=$('v3-access-live');if(!feed)return;feed.innerHTML=`<div class="live-row">${row.photo_id?evidenceThumbnail(row.photo_id):'<span>—</span>'}${accountIdentityCell(row)}<span>${esc(humanMethod(row.method))} · ${badge(row.result)}</span><time>${date(row.timestamp)}</time></div>`;bindEvidenceErrors();feed.querySelector('[data-access-photo]')?.addEventListener('click',()=>openPhotoViewer([row.photo_id],0));});v3LiveSource.onerror=()=>{const feed=$('v3-access-live');if(feed)feed.textContent=L.t('reconnecting');};document.querySelectorAll('[data-access-photo]').forEach(button=>button.onclick=()=>openPhotoViewer([button.dataset.accessPhoto],0));}

async function loadV3Events(){
  const [security,audit]=await Promise.all([api(`/api/v3/admin/security-events?limit=${v3PageSize}&offset=${v3Page.security*v3PageSize}`),api(`/api/admin/audit?limit=${v3PageSize}&offset=${v3Page.security*v3PageSize}`)]);
  const rows=[...security.map(row=>({...row,_category:'security'})),...audit.map(row=>({...row,_category:'audit',severity:row.result}))].sort((a,b)=>String(b.timestamp).localeCompare(String(a.timestamp))).slice(0,v3PageSize);
  table('v3-events',[L.t('time'),L.t('event'),L.t('user'),L.t('category'),L.t('severity'),L.t('risk'),L.t('result'),L.t('actions')],rows.map(row=>[date(row.timestamp),esc(humanCode(row.event_type)),accountIdentityCell(row),L.t(row._category==='security'?'securityCategory':'systemCategory'),badge(row.severity),row.risk?.reason_code?badge(row.risk.risk_level):'—',badge(row.result||row.severity),eventDetailsButton(row,row._category)]));
  renderAdminPager('v3-events',v3Page.security,rows.length,page=>{v3Page.security=page;loadV3Events();});bindEventDetailButtons();
}

async function loadV3BiometricRequests() {
  $('v3-biometric-requests').innerHTML = `<div class="data-card">${L.t('loading')}</div>`;
  const rows = await api(`/api/v3/admin/biometric-requests?limit=${v3PageSize}&offset=${v3Page.biometric * v3PageSize}`);
  table('v3-biometric-requests', [L.t('time'), L.t('user'), L.t('status'), L.t('similarity'), L.t('actions')], rows.map(row => [
    date(row.created_at), accountIdentityCell(row), badge(row.status), pct(row.duplicate_similarity),
    `<button type="button" data-bio-details="${esc(row.id)}">${L.t('details')}</button> ${row.status === 'PENDING_REVIEW' ? `<button data-bio-review="${esc(row.id)}" data-decision="APPROVED">${L.t('approve')}</button> <button data-bio-review="${esc(row.id)}" data-decision="REVISION_REQUIRED">${L.t('revision')}</button> <button data-bio-review="${esc(row.id)}" data-decision="REJECTED">${L.t('reject')}</button>` : ''}`,
  ]));
  renderAdminPager('v3-biometric-requests', v3Page.biometric, rows.length, page => { v3Page.biometric = page; loadV3BiometricRequests(); });
  document.querySelectorAll('[data-bio-details]').forEach(button => button.onclick = () => openBiometricRequest(button.dataset.bioDetails));
  document.querySelectorAll('[data-bio-review]').forEach(button => button.onclick = async () => {
    const decision = button.dataset.decision;
    const comment = await adminAsk({title:L.t(decision),message:L.t('reviewDecisionHint'),label:decision==='APPROVED'?'':L.t('adminComment'),required:decision!=='APPROVED',confirmKey:decision==='APPROVED'?'approve':'save',danger:decision==='REJECTED'});
    if (comment === null) return;
    button.disabled = true;
    try {
      await api(`/api/v3/admin/biometric-requests/${button.dataset.bioReview}/review`, jsonOptions('POST', { decision, comment: comment || '' }));
      await loadV3BiometricRequests();
    } finally { button.disabled = false; }
  });
}

async function openBiometricRequest(requestId) {
  const request = await api(`/api/v3/admin/biometric-requests/${encodeURIComponent(requestId)}`);
  const actions = request.status === 'PENDING_REVIEW'
    ? `<h3>${L.t('availableActions')}</h3><div class="detail-actions"><button data-detail-review="APPROVED">${L.t('approve')}</button><button data-detail-review="REVISION_REQUIRED">${L.t('revision')}</button><button data-detail-review="REJECTED">${L.t('reject')}</button></div>`
    : request.status === 'REVISION_REQUIRED' ? `<div class="info-state">${L.t('waitingForBiometricResubmission')}</div>` : `<div class="info-state">${L.t('requestIsReadOnly')}</div>`;
  $('event-detail-content').innerHTML = `<dl class="event-details">${field(L.t('user'),accountIdentity(request))}${field(L.t('loginName'),request.login)}${field(L.t('role'),humanCode(request.role))}${field(L.t('status'),humanCode(request.status,'status'))}${field(L.t('created'),date(request.created_at))}${field(L.t('updated'),date(request.updated_at))}${field(L.t('adminComment'),request.admin_comment||'—')}</dl><h3>${L.t('submittedPhotos')}</h3>${requestGallery(request.photos)}<h3>${L.t('requestHistory')}</h3>${request.history.length?request.history.map(row=>`<p>${date(row.timestamp)} · ${esc(humanCode(row.event_type))}</p>`).join(''):`<p>${L.t('noData')}</p>`}${actions}`;
  bindRequestGallery($('event-detail-content'));
  $('event-detail-content').querySelectorAll('[data-detail-review]').forEach(button=>button.onclick=async()=>{const decision=button.dataset.detailReview;const comment=await adminAsk({title:L.t(decision),message:L.t('reviewDecisionHint'),label:decision==='APPROVED'?'':L.t('adminComment'),required:decision!=='APPROVED',confirmKey:decision==='APPROVED'?'approve':'save',danger:decision==='REJECTED'});if(comment===null)return;await api(`/api/v3/admin/biometric-requests/${encodeURIComponent(request.id)}/review`,jsonOptions('POST',{decision,comment}));$('event-detail-dialog').close();await loadV3BiometricRequests();});
  $('event-detail-dialog').showModal();
}

async function loadV3SecurityEvents() {
  $('v3-security-events').innerHTML = `<div class="data-card">${L.t('loading')}</div>`;
  const rows = await api(`/api/v3/admin/security-events?limit=${v3PageSize}&offset=${v3Page.security * v3PageSize}`);
  table('v3-security-events', [L.t('time'), L.t('event'), L.t('severity'), L.t('user'), L.t('status'), L.t('actions')], rows.map(row => [
    date(row.timestamp), esc(humanCode(row.event_type)), badge(row.severity), accountIdentityCell(row), row.reviewed_at ? badge('REVIEWED') : badge('OPEN'),
    `${eventDetailsButton(row, 'security')} ${row.reviewed_at ? '' : `<button data-security-review="${row.id}">${L.t('review')}</button>`}`,
  ]));
  renderAdminPager('v3-security-events', v3Page.security, rows.length, page => { v3Page.security = page; loadV3SecurityEvents(); });
  bindEventDetailButtons();
  document.querySelectorAll('[data-security-review]').forEach(button => button.onclick = async () => {
    const note = await adminAsk({title:L.t('reviewEvent'),message:L.t('reviewEventHint'),label:L.t('adminComment'),required:true,confirmKey:'save'}); if(note===null)return;
    await api(`/api/v3/admin/security-events/${button.dataset.securityReview}/review`, jsonOptions('POST', { note }));
    await loadV3SecurityEvents();
  });
}

async function loadV3UnknownFaces() {
  $('v3-unknown-faces').innerHTML = `<div class="data-card">${L.t('loading')}</div>`;
  const rows = await api(`/api/v3/admin/unknown-faces?limit=${v3PageSize}&offset=${v3Page.unknown * v3PageSize}`);
  table('v3-unknown-faces', [L.t('photo'), L.t('time'), L.t('result'), L.t('similarity'), L.t('status'), L.t('actions')], rows.map(row => [
    row.photo_id ? evidenceLinks(row.photo_id) : '—',
    date(row.timestamp), badge(row.result), pct(row.similarity), row.reviewed_at ? badge('REVIEWED') : badge('OPEN'),
    `${eventDetailsButton(row, 'unknown')} ${row.reviewed_at || !row.security_event_id ? '' : `<button data-security-review="${row.security_event_id}">${L.t('review')}</button>`}`,
  ]));
  renderAdminPager('v3-unknown-faces', v3Page.unknown, rows.length, page => { v3Page.unknown = page; loadV3UnknownFaces(); });
  bindEventDetailButtons();
  bindEvidenceErrors();
  document.querySelectorAll('[data-security-review]').forEach(button => button.onclick = async () => {
    const note = await adminAsk({title:L.t('reviewEvent'),message:L.t('reviewEventHint'),label:L.t('adminComment'),required:true,confirmKey:'save'}); if(note===null)return;
    await api(`/api/v3/admin/security-events/${button.dataset.securityReview}/review`, jsonOptions('POST', { note }));
    await loadV3UnknownFaces();
  });
}

async function loadV3Archive() {
  $('v3-archive').innerHTML = `<div class="data-card">${L.t('loading')}</div>`;
  const rows = await api(`/api/v3/admin/archive?limit=${v3PageSize}&offset=${v3Page.archive * v3PageSize}`);
  table('v3-archive', [L.t('photo'), 'Photo ID', L.t('time'), L.t('source'), L.t('user'), L.t('status'), L.t('integrity')], rows.map(row => [
    evidenceLinks(row.id),
    `<code>${esc(row.id)}</code>`, date(row.created_at), esc(humanCode(row.source_type)), row.user_id == null ? L.t('unknownUser') : `ID ${row.user_id}`, badge(row.storage_state), badge(row.integrity_status),
  ]));
  renderAdminPager('v3-archive', v3Page.archive, rows.length, page => { v3Page.archive = page; loadV3Archive(); });
  bindEvidenceErrors();
}

function evidenceLinks(photoId) {
  const id = encodeURIComponent(photoId);
  return `<span class="evidence-links"><a href="/api/v3/photos/${id}" target="_blank" rel="noopener"><img class="archive-thumb" data-evidence-image src="/api/v3/photos/${id}" alt="${L.t('evidencePreview')}"></a><a class="secondary-button" href="/api/v3/photos/${id}/download" download>${L.t('download')}</a><small data-evidence-error hidden>${L.t('fileUnavailable')}</small></span>`;
}

function bindEvidenceErrors() {
  document.querySelectorAll('[data-evidence-image]').forEach(image => image.onerror = () => {
    image.hidden = true;
    const error = image.closest('.evidence-links')?.querySelector('[data-evidence-error]');
    if (error) error.hidden = false;
  });
}

function loadV3Live() {
  const target = $('v3-live');
  target.innerHTML = `<div class="data-card" id="v3-live-feed">${L.t('waitingForEvents')}</div>`;
  v3LiveSource = new EventSource('/api/v3/admin/live');
  v3LiveSource.addEventListener('access', event => {
    const row = JSON.parse(event.data);
    const feed = $('v3-live-feed');
    if (feed.textContent === L.t('waitingForEvents')) feed.textContent = '';
    feed.insertAdjacentHTML('afterbegin', `<div class="live-row">${accountIdentityCell(row)}<span>${esc(humanMethod(row.method))} · ${badge(row.result)}</span><time>${date(row.timestamp)}</time></div>`);
  });
  v3LiveSource.onerror = () => { target.dataset.connection = 'retrying'; };
}

async function loadV3Backups() {
  const rows = await api('/api/v3/admin/backups');
  table('v3-backups', [L.t('time'), L.t('file'), L.t('size'), L.t('status'), L.t('actions')], rows.map(row => [
    date(row.created_at), esc(row.file_name), `${Math.round(row.size_bytes / 1024)} KB`, badge(row.validation_status),
    `<button data-backup-validate="${esc(row.id)}">${L.t('validate')}</button> <button data-backup-restore="${esc(row.id)}" data-backup-file="${esc(row.file_name)}" data-backup-date="${esc(row.created_at)}">${L.t('restore')}</button> <button data-backup-delete="${esc(row.id)}" class="danger">${L.t('delete')}</button>`,
  ]));
  document.querySelectorAll('[data-backup-validate]').forEach(button => button.onclick = async () => {
    const result = await api(`/api/v3/admin/backups/${button.dataset.backupValidate}/validate`);
    toast(result.valid ? 'backupValid' : 'backupInvalid');
  });
  document.querySelectorAll('[data-backup-restore]').forEach(button => button.onclick = async () => {
    const confirmed=await adminAsk({title:L.t('restoreBackupTitle'),message:tr('restoreSelectedBackup',{name:button.dataset.backupFile,date:date(button.dataset.backupDate)}),confirmKey:'restore',danger:true});if(confirmed===null)return;
    await api(`/api/v3/admin/backups/${button.dataset.backupRestore}/restore`, jsonOptions('POST', { confirmation: 'RESTORE' }));
    await adminAsk({title:L.t('backupRestored'),message:L.t('internalSafetyBackupCreated'),confirmKey:'continue'});await loadV3Backups();
  });
  document.querySelectorAll('[data-backup-delete]').forEach(button => button.onclick = async () => {
    const reason=await adminAsk({title:L.t('deleteBackupTitle'),message:L.t('confirmBackupDelete'),label:L.t('actionReason'),required:true,confirmKey:'delete',danger:true});if(reason===null)return;
    await api(`/api/v3/admin/backups/${button.dataset.backupDelete}`, jsonOptions('DELETE', { reason }));
    await loadV3Backups();
  });
}

async function loadV3Settings() {
  const settings = await api('/api/v3/admin/settings');
  const form = $('v3-settings-form');
  Object.entries(settings).forEach(([key, value]) => {
    const input = form.elements.namedItem(key);
    if (!input) return;
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = value;
  });
}

$('run-archive').onclick = async () => { await api('/api/v3/admin/archive/run', { method: 'POST' }); await loadV3Archive(); };
$('create-backup').onclick = async event => { const button=event.currentTarget; if(button.disabled)return; button.disabled=true; const label=button.textContent; button.textContent=L.t('creating'); try { await api('/api/v3/admin/backups', { method: 'POST' }); await loadV3Backups(); } catch(error) { toast(error.code||error.message); } finally { button.disabled=false; button.textContent=label; } };
$('v3-settings-form').onsubmit = async event => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  if (button.disabled) return;
  button.disabled = true;
  const label = button.textContent;
  button.textContent = L.t('saving');
  const values = Object.fromEntries(new FormData(form));
  const payload = {
    allow_face_login: form.allow_face_login.checked,
    allow_password_login: form.allow_password_login.checked,
    account_session_ttl_seconds: Number(values.account_session_ttl_seconds),
    account_session_idle_seconds: Number(values.account_session_idle_seconds),
    identification_threshold: Number(values.identification_threshold),
    identification_ambiguity_margin: Number(values.identification_ambiguity_margin),
    duplicate_biometric_threshold: Number(values.duplicate_biometric_threshold),
  };
  try {
    await api('/api/v3/admin/settings', jsonOptions('PUT', payload));
    toast('settingsSaved');
  } catch (error) {
    toast(error.code || error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
};

function consolidateAdminNavigation(){
  const nav=document.querySelector('.admin-nav');
  ['create','security-events','unknown-faces','archive','live','attempts','audit'].forEach(view=>{const button=nav.querySelector(`[data-view="${view}"]`);if(button)button.hidden=true;});
  const biometric=nav.querySelector('[data-view="biometric-requests"] b');if(biometric){biometric.dataset.i18n='biometrics';biometric.textContent=L.t('biometrics');}
  if(!nav.querySelector('[data-view="events"]')){const events=document.createElement('button');events.type='button';events.dataset.view='events';events.innerHTML=`<span>⌁</span><b data-i18n="eventLog">${L.t('eventLog')}</b>`;nav.querySelector('[data-view="backups"]').before(events);events.onclick=()=>show('events');}
  if(!$('events')){const section=document.createElement('section');section.id='events';section.className='admin-view';section.innerHTML=`<div class="card-heading"><h2 data-i18n="eventLog">${L.t('eventLog')}</h2></div><div id="v3-events"></div>`;$('settings').before(section);}
  const header=document.querySelector('.admin-header-actions');if(!header.querySelector('.header-theme')){const theme=document.createElement('div');theme.className='segmented header-theme';theme.setAttribute('aria-label',L.t('theme'));theme.innerHTML=`<button type="button" data-v3-theme="light" title="${L.t('light')}">☀</button><button type="button" data-v3-theme="dark" title="${L.t('dark')}">☾</button><button type="button" data-v3-theme="system" title="${L.t('system')}">◐</button>`;header.querySelector('.locale').before(theme);window.V3Theme.init();}
  const settings=$('settings');if(settings&&!$('retention-card')){const card=document.createElement('div');card.id='retention-card';card.className='form-card';card.innerHTML=`<h2>${L.t('storageRetention')}</h2><p>${L.t('retentionHint')}</p><button type="button" id="run-archive-settings" class="primary-small">${L.t('runArchiveNow')}</button><p id="archive-result" role="status"></p>`;settings.appendChild(card);$('run-archive-settings').onclick=async()=>{const result=await api('/api/v3/admin/archive/run',{method:'POST'});$('archive-result').textContent=result.archived?tr('archiveCompleted',{count:result.archived}):L.t('nothingToArchive');};}
  const filterLabels={status:'allStatuses',role:'allRoles',biometric:'anyBiometric'};[['user-status-filter','status'],['user-role-filter','role'],['user-biometric-filter','biometric']].forEach(([id,key])=>{const option=$(id)?.querySelector('option[value=""]');if(option){option.dataset.i18n=filterLabels[key];option.textContent=L.t(filterLabels[key]);}});
}
consolidateAdminNavigation();
window.V3Theme.init();
