// KaraokeGen word-level editor.
// Each WORD in the alignment = one wavesurfer Region. Drag a region's body
// or edges to nudge/move the word; drag the playhead cursor to scrub audio.
// Click a word chip to play just that word, a line chip for the whole line.
// Edits mutate `alignment` in memory; "Render MP4" burns per-word karaoke.
// State persists to localStorage + IndexedDB so a tab refresh resumes cleanly.
// Live regions are the source of truth: every read of a word's timings first
// syncs from its region, so clicks always use the current on-screen times.
// Mouse-driven only: there are no keyboard shortcuts.

const $ = (id) => document.getElementById(id);

// Safe listener attach: if the element was removed from the HTML, log and skip
// instead of throwing and aborting the whole script (which silently killed
// every button handler before).
function on(id, event, fn) {
  const el = $(id);
  if (el) el.addEventListener(event, fn);
  else console.warn('[editor] missing element #' + id + ' — ' + event + ' listener skipped');
}

// Surface any uncaught JS error in the UI so broken states are never silent again.
window.addEventListener('error', (e) => {
  console.error('[editor] uncaught error:', e.message, e.filename, e.lineno);
  try {
    const errEl = $('error');
    if (errEl) {
      errEl.textContent = 'UI error: ' + (e.message || e) + ' (line ' + (e.lineno || '?') + ')';
      errEl.hidden = false;
    }
  } catch (_) { /* element may not exist on the page yet */ }
});
let alignment = null;
let draftJobId = null;
let callId = null;
let videoMode = false;  // lyric-video upload: separate -> remux -> download (no editor)
let draftXhr = null;  // active draft submission XHR (so Stop Modal can cancel it)
let renderDownloadUrl = null;
let renderDownloadName = 'karaoke.mp4';
// Save-As download: in the exe (pywebview) window.location.href just navigates
// the webview and never prompts. Route through Python's native save dialog;
// in a plain browser fall back to an anchor download.
async function downloadUrlWithSaveDialog(url, filename) {
  const name = filename || 'karaoke.mp4';
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.saveFile) {
      setStatus('Choose where to save…', 'busy');
      const res = await window.pywebview.api.saveFile(url, name);
      if (res && res.ok) setStatus('Saved to ' + res.path);
      else if (res && res.cancelled) setStatus('Save cancelled');
      else { showError('Save failed: ' + ((res && res.error) || 'unknown')); setStatus('error', 'err'); }
      return;
    }
  } catch (e) { console.warn('[download] pywebview save failed, falling back', e); }
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
let wavesurfer = null;
let regions = null;
let activeLineEl = null;
let activeSegI = null;   // index into alignment.segments for the selected line
let playingSegI = null;  // line under the playhead (for white highlight)
let progressTimer = null;
let draftFileName = null;  // sanitized upload basename (no ext) — the render output name
// ---------- transcribe-first flow (no lyrics pasted) ----------
// Phase 1 returns only text; the ORIGINAL uploaded file plays locally while
// the user fixes it, and phase 2 aligns the edited text (stems cached).
let _transcriptAudioUrl = null;  // blob URL of the uploaded file for review
const TRANSCRIPT_KEY = 'kg_transcript';  // {transcript, job_id, file_name, file_size}
let stageTimes = {};       // stage -> ms timestamp of first sight (per job, for the stage log)
let lastStage = null;
let _timelineMode = 'audio'; // mode of the currently running job's timeline (captured at start)
let jobComplete = false;
let jobStartTs = null;      // ms timestamp when the current GPU job started
let elapsedTimer = null;    // 1s interval that updates the elapsed display
let lineStopTimer = null;  // shared stop-when-line-ends checker (never overlap)
let _previewSeq = 0;  // invalidates stale word/line previews (seek race + rapid clicks)
function _cancelPreview() {
  // Manual stop (Play/Pause button, new draft, text edit pause): the pending
  // wall-clock stop must not snap the cursor afterwards.
  _previewSeq++;
  if (lineStopTimer) { clearInterval(lineStopTimer); lineStopTimer = null; }
}
// Seek and wait until the HTMLAudio cursor actually lands. setTime() only sets
// media.currentTime — the seek completes async. Starting play() + the stop
// poll before it lands reads the OLD playhead: a far-away click then either
// pauses instantly (word) or disarms the stop and plays past the bracket
// (line). Poll with rAF until close or 400ms so preview never hangs.
function seekPreviewTo(t) {
  return new Promise((resolve) => {
    if (!wavesurfer) return resolve(false);
    t = Math.max(0, +t || 0);
    try { wavesurfer.pause(); } catch (_) {}
    try { wavesurfer.setTime(t); } catch (_) { return resolve(false); }
    const deadline = performance.now() + 400;
    const check = () => {
      if (!wavesurfer) return resolve(false);
      let cur = null;
      try { cur = wavesurfer.getCurrentTime(); } catch (_) {}
      if (cur != null && isFinite(cur) && Math.abs(cur - t) < 0.04) return resolve(true);
      if (performance.now() >= deadline) return resolve(false);
      requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
  });
}
let lineChips = [];  // cached palette chip elements for fast highlight lookup
// ---------- word mode (permanent) ----------
// The waveform always shows ONE REGION PER WORD (from seg.words).
// Dragging a word's body/edges writes w.start/w.end directly; the parent
// line's span follows its first/last word. There is no line mode anymore.
const wordMode = true;
let activeWordI = null;      // selected word index within activeSegI
let playingWordKey = null;   // "segI:wordI" under the playhead
let _playingWordChip = null; // word chip element currently lit as playing
let lineDivs = [];           // verse block elements, indexed by segI
let lineWordChips = [];      // per-verse word chip elements, indexed [segI][wi]
let lineTimes = [];  // cached palette time elements
let playingLineEl = null;  // chip currently highlighted as "playing" (separate from selection)
let _customTimeline = null;  // custom time ruler element

// ---------- job history (sidebar, ChatGPT-style) ----------
const JOB_LIST_KEY = 'kg_jobs';
let currentJobId = null;  // client-side id of the job currently open in the editor

function jobList() {
  try { return JSON.parse(localStorage.getItem(JOB_LIST_KEY)) || []; } catch (e) { return []; }
}
function persistJobList(list) {
  localStorage.setItem(JOB_LIST_KEY, JSON.stringify(list));
  renderJobList();
}
function jobPayloadKey(id) { return 'kg_job_' + id; }
function loadJobPayload(id) {
  try { return JSON.parse(localStorage.getItem(jobPayloadKey(id))); } catch (e) { return null; }
}
function saveJobPayload(id, payload) {
  localStorage.setItem(jobPayloadKey(id), JSON.stringify(payload));
}
function createJobEntry(title, mode) {
  const list = jobList();
  // a new draft supersedes any still-running one (its polling is abandoned)
  for (const j of list) if (j.status === 'running') j.status = 'stopped';
  const id = 'job_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  list.unshift({ id, title, ts: Date.now(), mode, status: 'running' });
  currentJobId = id;
  persistJobList(list);
  return id;
}
function updateJobEntry(id, patch) {
  const list = jobList();
  const j = list.find((x) => x.id === id);
  if (j) {
    Object.assign(j, patch);
    persistJobList(list);
  }
}
function renderJobList() {
  const ul = $('job-list');
  const empty = $('job-empty');
  const list = jobList();
  if (empty) empty.hidden = list.length > 0;
  if (!ul) return;
  ul.innerHTML = '';
  for (const j of list) {
    const li = document.createElement('li');
    li.className = 'job-item' + (j.id === currentJobId ? ' active' : '');
    const title = document.createElement('div');
    title.className = 'job-title';
    title.textContent = j.title || 'Draft';
    title.title = j.title || '';
    const meta = document.createElement('div');
    meta.className = 'job-meta';
    const d = new Date(j.ts);
    const pad = (n) => String(n).padStart(2, '0');
    meta.textContent = (j.mode === 'video' ? 'video' : 'audio') + ' · ' +
      (d.getMonth() + 1) + '/' + d.getDate() + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    const status = document.createElement('span');
    status.className = 'job-status ' + (j.status || 'done');
    status.textContent = j.status === 'running' ? 'running' : j.status === 'stopped' ? 'stopped' : 'done';
    const del = document.createElement('button');
    del.className = 'job-del';
    del.title = 'Delete job';
    del.textContent = '\u2715';
    del.addEventListener('click', (ev) => { ev.stopPropagation(); deleteJob(j.id); });
    li.appendChild(title);
    li.appendChild(meta);
    li.appendChild(status);
    li.appendChild(del);
    li.addEventListener('click', () => loadJob(j.id));
    ul.appendChild(li);
  }
}
async function deleteJob(id) {
  if (!confirm('Delete this job?')) return;
  const list = jobList().filter((j) => j.id !== id);
  localStorage.setItem(JOB_LIST_KEY, JSON.stringify(list));
  // Read the payload BEFORE dropping it: it holds the draft (audio-hash) id that
  // the stem cache is keyed by, so deleting a job can actually free its stems.
  const payload = loadJobPayload(id);
  const draftId = payload && (payload.job_id || payload.draft_job_id);
  if (draftId) audioCacheKeysForJob(draftId).forEach((k) => idbDelete(k).catch(() => {}));
  localStorage.removeItem(jobPayloadKey(id));
  if (id === currentJobId) {
    currentJobId = null;
    startNew();
  } else {
    renderJobList();
  }
}
async function loadJob(id) {
  const payload = loadJobPayload(id);
  if (!payload) return;
  stopRulerLoop();
  if (wavesurfer) { wavesurfer.pause(); wavesurfer.destroy(); wavesurfer = null; }
  if (_waveformBlobUrl) { try { URL.revokeObjectURL(_waveformBlobUrl); } catch(_){} _waveformBlobUrl = null; }
  $('step-transcript').hidden = true;
  setTranscriptReviewMode(false);
  setStepOneActionVisible(true);
  regions = null;
  if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
  stopElapsed();
  clearError();
  activeLineEl = null;
  playingLineEl = null;
  activeSegI = null;
  currentJobId = id;
  renderJobList();
  $('step-video-done').hidden = true;
  $('progress-bar').hidden = true;
  $('render-progress').hidden = true;
  $('line-palette').innerHTML = '';
  $('line-info').textContent = 'Click a line to select it';
  renderDownloadUrl = null;
  renderDownloadName = 'karaoke.mp4';
  $('btn-render').textContent = 'Render MP4';
  $('btn-download').disabled = true;
  $('btn-download').textContent = 'Download';

  if (payload.video_mode) {
    videoMode = true;
    draftJobId = payload.job_id || null;
    $('step-edit').hidden = true;
    $('step-render').hidden = true;
    $('step-video-done').hidden = false;
    const dl = $('video-download-link');
    if (dl && draftJobId) dl.href = '/api/video-karaoke/' + draftJobId;
    if (draftJobId) localStorage.setItem('kg_draft_job_id', draftJobId);
    setStatus('Vocals removed — your karaoke video is ready to download');
    return;
  }

  videoMode = false;
  alignment = payload.alignment || null;
  draftJobId = payload.job_id || draftJobId;
  if ($('lyrics')) $('lyrics').value = payload.lyrics || '';
  _history = alignment ? [_snapshot()] : [];
  _historyIdx = 0;
  syncHistoryButtons();
  clearEditedAlignment();
  if (alignment) {
    localStorage.setItem('kg_draft_result', JSON.stringify({
      alignment,
      lyrics: payload.lyrics || '',
      report: payload.report || '',
      job_id: draftJobId,
      duration: payload.duration || 0,
      client_job_id: id,
    }));
  }
  if (draftJobId) localStorage.setItem('kg_draft_job_id', draftJobId);
  _waveformJobId = draftJobId;
  if ($('voc-toggle')) $('voc-toggle').checked = true;
  if ($('sv-toggle')) $('sv-toggle').checked = true;
  if ($('sv-volume')) $('sv-volume').value = 100;
  // Stems live on Modal (7-day cache) — audio loads on the first source
  // toggle; the alignment/lyrics are fully editable without it.
  $('step-edit').hidden = false;
  $('step-render').hidden = false;
  setStatus('Restored job (audio not available locally)');
}

// ---------- config wizard (AppData-backed, blocks drafts until saved) ----------
// NOTE: this must NEVER auto-hide the overlay when already configured — the
// gear icon opens Settings on demand, and auto-hiding made Settings
// impossible to open after first setup. We only force-SHOW when blocked.
// Visibility is driven SOLELY by the `hidden` attribute (see editor.html +
// style.css). Never use inline style.display — it overrides the [hidden]
// CSS rule and makes the X / backdrop click unable to close the window.
function setConfigVisible(visible) {
  const o = $('config-overlay');
  if (!o) return;
  // Clear any legacy inline display from older server-rendered HTML
  // (style="display:flex") so `hidden` actually hides the overlay.
  o.style.removeProperty('display');
  o.hidden = !visible;
}
async function refreshConfigUI() {
  try {
    const r = await fetch('/api/config');
    const cfg = await r.json();
    const blocked = !cfg.configured;
    const draftBtn = $('btn-draft');
    if (draftBtn) {
      // keep file-selected state: only force-disable when blocked, otherwise
      // re-enable iff a file is chosen
      if (blocked) {
        draftBtn.disabled = true;
        draftBtn.title = 'Open Settings (gear icon) and paste your Modal token';
      } else {
        syncDraftButtons();
        draftBtn.title = '';
      }
    }
    if (blocked) setConfigVisible(true);
    await renderConfigStatus(cfg);
  } catch (e) {
    console.warn('[config] refresh failed', e);
    setConfigVisible(true);
  }
}
// Show current account/connection inside Settings so users can switch accounts.
async function renderConfigStatus(cfg) {
  const el = $('cfg-current');
  if (!el) return;
  try {
    cfg = cfg || await (await fetch('/api/config')).json();
    let ms = {};
    try { ms = await (await fetch('/api/modal/status')).json(); } catch (_) {}
    const url = cfg.modal_api_url || ms.modal_api_url || '(not set)';
    const keyPrev = cfg.modal_api_key_preview || ms.modal_api_key_preview || '';
    const login = ms.logged_in ? 'yes' : 'no';
    el.innerHTML = '';
    const mk = (k, v) => {
      const d = document.createElement('div');
      const b = document.createElement('b');
      b.textContent = k + ': ';
      b.style.color = '#cbd5e1';
      d.appendChild(b);
      d.appendChild(document.createTextNode(v));
      return d;
    };
    el.appendChild(mk('Backend', url));
    el.appendChild(mk('Key', keyPrev || '(none)'));
    el.appendChild(mk('Modal login', login));
    el.appendChild(mk('Status', cfg.configured ? 'connected' : 'not connected — paste token below'));
    if (cfg.config_path) el.appendChild(mk('Config', cfg.config_path));
  } catch (e) {
    el.textContent = 'Could not load connection status.';
  }
}
// browser copy-paste setup (no CLI): paste whole `modal token set ...` command → save → deploy
let _deployPoll = null;
function parseTokenCmd(s) {
  // Modal prints `--token-id ak-... --token-secret as-...`; tolerate
  // `--token-id=ak-...`, quotes, and extra flags like --profile.
  const clean = (v) => (v || '').replace(/^["']|["']$/g, '').trim();
  const idM = s.match(/--token-id\s*[=\s]\s*"?([^\s"]+)/);
  const secM = s.match(/--token-secret\s*[=\s]\s*"?([^\s"]+)/);
  return { id: clean(idM && idM[1]), sec: clean(secM && secM[1]) };
}
async function saveTokenAndDeploy() {
  const cmdEl = $('cfg-token-cmd');
  const btn = $('cfg-token-save'), statusEl = $('cfg-auto-status'), logEl = $('cfg-deploy-log');
  const cmd = cmdEl ? cmdEl.value.trim() : '';
  const p = parseTokenCmd(cmd);
  const tid = p.id, tsec = p.sec;
  if (!tid || !tsec) { if(statusEl){statusEl.textContent='Paste the whole modal token set command.'; statusEl.style.color='#f88';} return; }
  if (btn) btn.disabled = true;
  if (statusEl) { statusEl.textContent = 'Saving token...'; statusEl.style.color=''; }
  if (logEl) { logEl.hidden = false; logEl.textContent = 'Saving token...\n'; }
  try {
    let r = await fetch('/api/modal/token', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token_id: tid, token_secret: tsec})});
    if (!r.ok) throw new Error(await r.text());
    if (statusEl) statusEl.textContent = 'Token saved — deploying backend (a few minutes the first time)...';
    if (logEl) logEl.textContent += 'Token saved.\nDeploying...\n';
    await startDeploy(btn, statusEl, logEl);
  } catch(e) {
    if(statusEl){statusEl.textContent='Save failed: '+e.message; statusEl.style.color='#f88';}
    if(btn) btn.disabled=false;
  }
}
// Deploy (or redeploy) with the already-saved token — no paste needed.
// Shared by first-time setup (after token save) and the Redeploy button.
async function startDeploy(btn, statusEl, logEl) {
  const dr = await fetch('/api/modal/deploy', {method:'POST'});
  if (!dr.ok) {
    let msg = '';
    try { msg = await dr.text(); } catch (_) { msg = dr.statusText; }
    try { const j = JSON.parse(msg); msg = j.detail || msg; } catch (_) {}
    throw new Error(msg || ('Deploy start failed (HTTP ' + dr.status + ')'));
  }
  if (_deployPoll) clearInterval(_deployPoll);
  let _pollFails = 0;
  _deployPoll = setInterval(async () => {
    try {
      const d = await (await fetch('/api/modal/deploy/log')).json();
      _pollFails = 0;
      if (logEl) { logEl.textContent = (d.log || '').slice(-6000); logEl.scrollTop = logEl.scrollHeight; }
      if (d.state.done) {
        clearInterval(_deployPoll); _deployPoll=null;
        if (btn) btn.disabled = false;
        const rb = $('cfg-redeploy'); if (rb) rb.disabled = false;
        if (d.state.ok) {
          if (statusEl) { statusEl.textContent = 'Deployed and connected — you can now run drafts.'; statusEl.style.color='#8f8'; }
          if (logEl) logEl.textContent += '\nDone.\n';
          const tokenInput = $('cfg-token-cmd'); if (tokenInput) tokenInput.value='';
          await refreshConfigUI();
          setTimeout(()=>{ if(!$('cfg-token-cmd') || !$('cfg-token-cmd').value) setConfigVisible(false); },2500);
        } else {
          if (statusEl) { statusEl.textContent = 'Deploy failed: ' + (d.state.error||'see log'); statusEl.style.color='#f88'; }
        }
      }
    } catch(e) {
      // Local server restarted mid-deploy (or network blip): don't spin
      // forever with a disabled button — surface after 5 straight failures.
      if (++_pollFails >= 5) {
        clearInterval(_deployPoll); _deployPoll=null;
        if (btn) btn.disabled = false;
        const rb = $('cfg-redeploy'); if (rb) rb.disabled = false;
        if (statusEl) { statusEl.textContent = 'Deploy status unreachable — is the app still running? Retry.'; statusEl.style.color='#f88'; }
      }
    }
  }, 1500);
}
async function redeployBackend() {
  const btn = $('cfg-redeploy'), statusEl = $('cfg-auto-status'), logEl = $('cfg-deploy-log');
  if (_deployPoll) { if(statusEl){statusEl.textContent='A deploy is already running — wait for it.';} return; }
  if (btn) btn.disabled = true;
  const sb = $('cfg-token-save'); if (sb) sb.disabled = true;
  if (statusEl) { statusEl.textContent = 'Redeploying backend with your saved token...'; statusEl.style.color=''; }
  if (logEl) { logEl.hidden = false; logEl.textContent = 'Redeploying...\n'; }
  try {
    await startDeploy(btn, statusEl, logEl);
  } catch(e) {
    if(statusEl){statusEl.textContent='Redeploy failed to start: '+e.message+' (paste a token above if you disconnected)'; statusEl.style.color='#f88';}
    if(btn) btn.disabled=false;
  } finally {
    if (sb) sb.disabled = false;
  }
}
on('btn-config', 'click', async () => {
  setConfigVisible(true);
  await refreshConfigUI();
  // refreshConfigUI never auto-hides — re-assert visible in case of race
  setConfigVisible(true);
});
on('cfg-close', 'click', () => setConfigVisible(false));
on('config-overlay', 'click', (e) => { if (e.target.id === 'config-overlay') setConfigVisible(false); });
// Escape also closes Settings (but not the help overlay logic below).
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const o = $('config-overlay');
    if (o && !o.hidden) setConfigVisible(false);
  }
});
on('cfg-open-modal', 'click', () => window.open('https://modal.com/', '_blank'));
on('cfg-token-save', 'click', saveTokenAndDeploy);
on('cfg-redeploy', 'click', redeployBackend);
on('cfg-disconnect', 'click', async () => {
  const statusEl = $('cfg-auto-status'), logEl = $('cfg-deploy-log');
  if (!confirm('Disconnect Modal backend and forget saved token? You can paste a new token to switch account.')) return;
  try {
    if (statusEl) { statusEl.textContent = 'Disconnecting...'; statusEl.style.color=''; }
    const r = await fetch('/api/modal/disconnect', {method:'POST'});
    if (!r.ok) {
      let msg = await r.text().catch(() => r.statusText);
      try { const j = JSON.parse(msg); msg = j.detail || msg; } catch (_) {}
      throw new Error(msg || ('HTTP ' + r.status));
    }
    const cmdEl = $('cfg-token-cmd'); if (cmdEl) cmdEl.value = '';
    if (logEl) { logEl.hidden = true; logEl.textContent = ''; }
    if (statusEl) { statusEl.textContent = 'Disconnected — paste a new token above to switch account.'; statusEl.style.color=''; }
    await refreshConfigUI();
    setConfigVisible(true);
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Disconnect failed: ' + e.message; statusEl.style.color='#f88'; }
  }
});

// ---------- undo / redo ----------
let _history = [];
let _historyIdx = -1;
const _HISTORY_LIMIT = 100;
let _suppressHistory = false;  // set true while applying undo/redo to avoid re-push

function _snapshot() {
  return JSON.parse(JSON.stringify({ segments: alignment.segments }));
}
function _restore(snap) {
  _suppressHistory = true;
  alignment.segments = JSON.parse(JSON.stringify(snap.segments));
  buildRegions();
  buildLinePalette();
  if (activeSegI != null && activeSegI < alignment.segments.length) {
    selectLineSilent(activeSegI);
  }
  persistAllFromRegions();
  persistAlignment();
  _suppressHistory = false;
}
function pushHistory() {
  if (_suppressHistory || !alignment) return;
  _history = _history.slice(0, _historyIdx + 1);  // drop redo branch
  _history.push(_snapshot());
  if (_history.length > _HISTORY_LIMIT) _history.shift();
  _historyIdx = _history.length - 1;
  syncHistoryButtons();
}
function undo() {
  if (_historyIdx <= 0) return;
  _historyIdx--;
  _restore(_history[_historyIdx]);
  flashSaved();
}
function redo() {
  if (_historyIdx >= _history.length - 1) return;
  _historyIdx++;
  _restore(_history[_historyIdx]);
  flashSaved();
}
function syncHistoryButtons() {
  const ub = $('btn-undo');
  const rb = $('btn-redo');
  if (ub) ub.disabled = _historyIdx <= 0;
  if (rb) rb.disabled = _historyIdx >= _history.length - 1;
}

// ---------- IndexedDB (for large data: vocals audio blob) ----------
const DB_NAME = 'karaokegen';
function idb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('blobs');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbPut(key, val) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('blobs', 'readwrite');
    tx.objectStore('blobs').put(val, key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}
async function idbGet(key) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('blobs', 'readonly');
    const req = tx.objectStore('blobs').get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbDelete(key) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('blobs', 'readwrite');
    tx.objectStore('blobs').delete(key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

// ---------- helpers ----------
function setStatus(msg, cls) {
  const s = $('status');
  s.textContent = msg;
  s.className = 'status' + (cls ? ' ' + cls : '');
}
function showError(msg) {
  const e = $('error');
  e.textContent = msg;
  e.hidden = false;
}
function clearError() { $('error').hidden = true; }

// Edits live only in memory otherwise — a refresh before Render would silently
// revert the waveform to the draft timings. Save the mutated alignment back
// into the stored draft on every edit. Written to two keys so a restore never
// falls back to the original generated alignment:
//   kg_draft_result     the full draft payload (alignment + lyrics + report)
//   kg_alignment_edited the edited alignment on its own (refresh proof)
const EDIT_KEY = 'kg_alignment_edited';
let editStateTimer = null;
function flashSaved() {
  const el = $('edit-state');
  if (!el) return;
  el.textContent = 'saved';
  el.classList.add('show');
  clearTimeout(editStateTimer);
  editStateTimer = setTimeout(() => el.classList.remove('show'), 1500);
}
function persistAlignment() {
  if (!alignment || !draftJobId) return;
  try {
    const saved = localStorage.getItem('kg_draft_result');
    const data = saved ? JSON.parse(saved) : { lyrics: '', report: '', job_id: draftJobId };
    data.alignment = alignment;
    localStorage.setItem('kg_draft_result', JSON.stringify(data));
    localStorage.setItem(EDIT_KEY, JSON.stringify(alignment));
    const jid = currentJobId;
    if (jid) {
      const p = loadJobPayload(jid) || {};
      p.alignment = alignment;
      p.job_id = draftJobId;
      if (p.lyrics === undefined && $('lyrics')) p.lyrics = $('lyrics').value;
      saveJobPayload(jid, p);
    }
    flashSaved();
  } catch (e) { /* storage quota — edits still live in memory */ }
}
function clearEditedAlignment() {
  localStorage.removeItem(EDIT_KEY);
}

// Re-sync every segment from its live region and persist. Called on pointer
// release and page unload so a refresh right after a drag never loses edits.
function persistAllFromRegions() {
  if (!alignment || !regions) return;
  if (wordMode) {
    for (let si = 0; si < alignment.segments.length; si++) {
      const seg = alignment.segments[si];
      for (let wi = 0; wi < (seg.words || []).length; wi++) syncWordFromRegion(si, wi, { history: false });
      syncLineSpanFromWords(si);
    }
    for (let si = 0; si < alignment.segments.length; si++) updateLineWordTimings(si);
  } else {
    for (let si = 0; si < alignment.segments.length; si++) syncSegmentFromRegion(si);
  }
}
window.addEventListener('beforeunload', persistAllFromRegions);

const COLORS = ['#e74c3c', '#e67e22', '#f1c40f', '#2ecc71', '#1abc9c', '#3498db', '#9b59b6', '#e91e63'];

// ---------- step 1: draft ----------
on('audio-file', 'change', () => {
  const files = $('audio-file').files;
  // A different file invalidates a pending transcript review (its text and
  // audio belong to the previous file). Keep the lyrics text itself — it is
  // now just user input for the new file.
  if (files.length && localStorage.getItem(TRANSCRIPT_KEY)) {
    try {
      const saved = JSON.parse(localStorage.getItem(TRANSCRIPT_KEY));
      const f = files[0];
      if (saved && (saved.file_name !== f.name || saved.file_size !== f.size)) {
        localStorage.removeItem(TRANSCRIPT_KEY);
        if (_transcriptAudioUrl) { try { URL.revokeObjectURL(_transcriptAudioUrl); } catch(_){} _transcriptAudioUrl = null; }
        const aud = $('transcript-audio'); if (aud) aud.removeAttribute('src');
        $('step-transcript').hidden = true;
        setTranscriptReviewMode(false);
        setStepOneActionVisible(true);
      } else if (saved && _transcriptAudioUrl) {
        // same file re-selected — rebind the review player to the new blob
        if (_transcriptAudioUrl) { try { URL.revokeObjectURL(_transcriptAudioUrl); } catch(_){} }
        _transcriptAudioUrl = URL.createObjectURL(f);
        const aud2 = $('transcript-audio'); if (aud2) aud2.src = _transcriptAudioUrl;
      }
    } catch (_) {}
  }
  // No pending transcript (normal state) → step-1 action must be visible.
  if (!localStorage.getItem(TRANSCRIPT_KEY)) setStepOneActionVisible(true);
  syncDraftButtons();
});

on('lyrics', 'input', () => {
  // In transcript-review mode the explainer hints are replaced by the audio
  // player — typing must not bring them back.
  if ($('draft-review') && !$('draft-review').hidden) {
    const _asr = $('asr-explain');
    if (_asr) _asr.style.display = 'none';
  } else {
    $('asr-explain').style.display = $('lyrics').value.trim() ? 'block' : 'none';
  }
  syncDraftButtons();
  // Persist transcript edits so a refresh before alignment loses nothing.
  if (!$('step-transcript').hidden) {
    try {
      const saved = JSON.parse(localStorage.getItem(TRANSCRIPT_KEY) || 'null');
      if (saved) {
        saved.transcript = $('lyrics').value;
        localStorage.setItem(TRANSCRIPT_KEY, JSON.stringify(saved));
      }
    } catch (_) {}
  }
});

// ---------- step 1: song start (lead-in skip) ----------
// A live take opens with talk that is NOT lyrics. Deleting those lines from
// the lyrics box does NOT delete the audio: the aligner still hears several
// seconds of speech with real vocal energy and can latch the first lyric
// lines onto it. Sending the timestamp where the song really starts makes the
// backend cut that intro out of every stem it analyzes, then shift the
// resulting timings back onto the full-song clock (the rendered MP4 still
// contains the intro).
const SONG_START_KEY = 'kg_song_start';

// "0:45" / "45" / "1:02.5" -> seconds. null = not a usable timestamp.
function parseSongStart(raw) {
  const t = (raw || '').trim().replace(',', '.');
  if (!t) return 0;
  const m = t.match(/^(?:(\d+):)?(\d+(?:\.\d+)?)$/);
  if (!m) return null;
  const mins = m[1] ? parseInt(m[1], 10) : 0;
  const secs = parseFloat(m[2]);
  if (!isFinite(secs)) return null;
  if (m[1] && secs >= 60) return null;   // "1:75" is a typo, not a time
  const total = mins * 60 + secs;
  if (total < 0 || total >= 3600) return null;
  return total;
}

function songStartSeconds() {
  const el = $('song-start');
  return el ? parseSongStart(el.value) : 0;
}

// Returns the validated seconds to send, or shows the error and returns null.
function songStartForSubmit() {
  const v = songStartSeconds();
  if (v === null) {
    showError('"Song starts at" must be a time like 0:45 or 45 — clear it if the song starts at 0:00.');
    return null;
  }
  return v;
}

function restoreSongStart() {
  const el = $('song-start');
  if (!el) return;
  try { el.value = localStorage.getItem(SONG_START_KEY) || ''; } catch (_) {}
}

on('song-start', 'input', () => {
  const el = $('song-start');
  if (!el) return;
  try { localStorage.setItem(SONG_START_KEY, el.value); } catch (_) {}
});

function syncDraftButtons() {
  const hasFile = !!($('audio-file') && $('audio-file').files.length);
  $('btn-draft').disabled = !hasFile;
  // No lyrics pasted = transcribe-first flow; label says what will happen.
  const noLyrics = !$('lyrics') || !$('lyrics').value.trim();
  const lbl = $('draft-btn-label');
  if (lbl) lbl.textContent = (hasFile && noLyrics) ? 'Transcribe' : 'Run draft';
}

// While a GPU job runs, step 1 shows ONLY the progress area (status + bar +
// stage timeline + log + slow note). The input form comes back on every exit
// path that needs it (submit error, stop, transcribe-done, new job) and stays
// hidden behind the editor / video result on success.
function setDraftFormVisible(v) {
  const f = $('draft-form');
  if (f) f.hidden = !v;
}

// Transcript-review mode: the two step-1 explainer hints are replaced by the
// audio player (which lives inside the draft form where the hints were).
// on=true  → hide hints, show player.  on=false → restore hints, hide player.
function setTranscriptReviewMode(on) {
  const asr = $('asr-explain');
  const vid = $('video-explain');
  const rev = $('draft-review');
  if (on) {
    if (asr) asr.style.display = 'none';
    if (vid) vid.style.display = 'none';
    if (rev) rev.hidden = false;
  } else {
    if (vid) vid.style.display = '';
    if (rev) rev.hidden = true;
    if (asr) asr.style.display = ($('lyrics') && $('lyrics').value.trim()) ? 'block' : 'none';
  }
}

// Step-1 action visibility: once the transcript lands, Run draft/Transcribe
// is done — the ONLY next step is Run alignment (step "Check the transcript").
// Keeping the old button around just invites a duplicate align run.
function setStepOneActionVisible(v) {
  const b = $('btn-draft');
  if (b) b.hidden = !v;
  // the slow-note explains the finished transcribe; hide it with the button
  // (the status pill stays — it points at the next step).
  if (!v) {
    const n = $('draft-slow-note');
    if (n) n.style.display = 'none';
  }
}

on('btn-draft', 'click', () => {
  clearError();
  const fd = new FormData();
  const file = $('audio-file').files[0];
  if (!file) { showError('Select an audio file first.'); return; }
  const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
  if (file.size > 300 * 1024 * 1024) {
    showError('File is ' + sizeMB + ' MB — too large. Please use a file under 300 MB.');
    return;
  }
   const isVideo = file.type.startsWith('video/');
  videoMode = isVideo;
  const jobTitle = (file.name || (isVideo ? 'video' : 'audio')).replace(/\.[^.]+$/, '').slice(0, 40) || (isVideo ? 'Video draft' : 'Audio draft');
  // Render output name = the uploaded filename (sanitized, no extension).
  draftFileName = (file.name || '').replace(/\.[^.]+$/, '').replace(/[^\w\- ]+/g, '').trim().slice(0, 60) || jobTitle;
  localStorage.setItem('kg_file_name', draftFileName);
  createJobEntry(jobTitle, isVideo ? 'video' : 'audio');
  if (isVideo) {
    fd.append('video', file);
    fd.append('lyrics', $('lyrics').value);
    fd.append('language', $('language').value);
    fd.append('genre', $('genre').value);
    submitDraft(fd, 'Extracting audio from video...', 'Uploading video');
  } else {
    if (file.size > 100 * 1024 * 1024) {
      showError('Audio file is ' + sizeMB + ' MB — too large. Please use a file under 100 MB.');
      return;
    }
    const songStart = songStartForSubmit();
    if (songStart === null) return;
    const pastedLyrics = $('lyrics').value;
    if (!pastedLyrics.trim()) {
      // No-lyrics flow, phase 1: transcribe only. The transcript lands in the
      // lyrics box for editing; alignment runs after (stems cached = fast).
      fd.append('audio', file);
      fd.append('language', $('language').value);
      fd.append('genre', $('genre').value);
      fd.append('start_s', String(songStart));
      submitDraft(fd, 'Transcribing vocals on Modal...', 'Uploading audio to Modal', {
        endpoint: '/api/transcribe',
        actionLabel: 'Transcribe',
        onDone: onTranscribeDone,
        slowNote: 'Transcribing only — separation + speech recognition, no timing yet. ' +
          'When the text arrives, fix it in the lyrics box while listening to your ' +
          'original audio, then hit Run alignment. First run after idle can take a ' +
          'few minutes — this is normal.',
      });
    } else {
      fd.append('audio', file);
      fd.append('lyrics', pastedLyrics);
      fd.append('language', $('language').value);
      fd.append('genre', $('genre').value);
      fd.append('start_s', String(songStart));
      submitDraft(fd, 'Submitting draft to Modal...', 'Uploading audio to Modal');
    }
  }
});

// No-lyrics flow, phase 2: align the user-edited transcript. Same audio file
// (stems cached on Modal by content hash, so separation is skipped).
on('btn-align', 'click', () => {
  clearError();
  const file = $('audio-file').files[0];
  if (!file) { showError('Re-select the same audio file above, then Run alignment.'); return; }
  const edited = $('lyrics').value;
  if (!edited.trim()) { showError('The transcript is empty — nothing to align.'); return; }
  const songStart = songStartForSubmit();
  if (songStart === null) return;
  const fd = new FormData();
  fd.append('audio', file);
  fd.append('lyrics', edited);
  fd.append('language', $('language').value);
  fd.append('genre', $('genre').value);
  fd.append('start_s', String(songStart));
  const tstat = $('transcript-status');
  if (tstat) tstat.textContent = 'Aligning your edited lyrics...';
  submitDraft(fd, 'Aligning edited lyrics on Modal...', 'Uploading audio to Modal', {
    actionLabel: 'Run alignment',
  });
});

function submitDraft(fd, submitLabel, uploadLabel, opts) {
  opts = opts || {};
  const endpoint = opts.endpoint || '/api/draft';
  const actionLabel = opts.actionLabel || 'Run draft';
  clearError();
  // A new job replaces whatever is playing — stop the player first so the
  // old audio never keeps going under the upload/GPU run.
  if (wavesurfer && wavesurfer.isPlaying()) wavesurfer.pause();
  const _pb = $('btn-play'); if (_pb) _pb.textContent = '\u25B6 Play';
  _cancelPreview();
  const _ta = $('transcript-audio'); if (_ta && !_ta.paused) _ta.pause();
  // The job's progress (bar + stages + log) is the whole step-1 view while
  // it runs — hide the input form so it can't be edited mid-upload.
  setDraftFormVisible(false);
  setStatus(submitLabel, 'busy');
  $('btn-draft').disabled = true;
  const alignBtn = $('btn-align');
  if (alignBtn) alignBtn.disabled = true;
  $('draft-spinner').hidden = false;
  $('draft-btn-label').textContent = 'Submitting...';
  $('draft-slow-note').style.display = 'none';
  $('step-edit').hidden = true;
  $('step-render').hidden = true;
  $('step-video-done').hidden = true;
  $('step-transcript').hidden = true;
  renderDownloadUrl = null;
  renderDownloadName = 'karaoke.mp4';
  $('btn-render').textContent = 'Render MP4';
  $('btn-download').disabled = true;
  $('btn-download').textContent = 'Download';
  clearEditedAlignment();

  const showProgress = (pct, detail) => {
    setProgress(pct, detail);
    $('draft-slow-note').textContent = opts.slowNote || (videoMode
      ? 'Your video is uploaded to a Modal GPU where the vocals are separated from the ' +
        'instrumental in a single AI pass. When it finishes, the instrumental is put back on ' +
        'your original video and the download appears below. First run after idle can ' +
        'take a few minutes — this is normal.'
      : 'Drafts are slow by nature: your audio is uploaded, a Modal GPU server is booted, ' +
        'then the vocals are separated from the beat, transcribed, and aligned. The green bar ' +
        'shows the stage. First run after idle can take a few minutes — this is normal.');
    $('draft-slow-note').style.display = 'block';
  };

  let uploadComplete = false;
  draftXhr = new XMLHttpRequest();
  const xhr = draftXhr;
  xhr.open('POST', endpoint);
  xhr.responseType = 'json';
  xhr.timeout = 5 * 60 * 1000;
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      showProgress(pct, uploadLabel + ' ' + pct + '%');
      if (e.loaded === e.total) {
        uploadComplete = true;
        setStatus('Processing on Modal... (spawning GPU job)', 'busy');
        console.log('[draft] upload complete, waiting for Modal response');
      }
    } else {
      showProgress(5, 'Uploading...');
    }
  };
  xhr.onload = () => {
    draftXhr = null;
    console.log('[draft] XHR onload', xhr.status, xhr.response);
    let errDetail = "";
    try { errDetail = xhr.statusText || String(xhr.status); } catch (_) { errDetail = String(xhr.status); }
    let data = xhr.response;
    // NOTE: with responseType='json', reading xhr.responseText throws
    // InvalidStateError in some browsers — guard it.
    let rawText = "";
    try { rawText = xhr.responseText || ""; } catch (_) { rawText = ""; }
    if (!data && rawText) {
      try { data = JSON.parse(rawText); } catch(_) { data = null; errDetail = rawText.slice(0,500); }
    }
    if (!data && xhr.status >= 400) {
      // non-JSON error (HTML or empty)
      data = null;
    }
    if (xhr.status >= 400) {
      const detail = (data && (data.detail || data.error)) || errDetail;
      showError('Draft failed: ' + detail);
      setStatus('error', 'err');
      finishDraftSubmitUI();
      setDraftFormVisible(true);
      syncDraftButtons();
      // mark the running job as stopped so the sidebar doesn't stay "running"
      if (currentJobId) updateJobEntry(currentJobId, { status: 'stopped' });
      return;
    }
    if (!data || !data.job_id || !data.draft_job_id) {
      showError('Draft failed: unexpected server response');
      setStatus('error', 'err');
      finishDraftSubmitUI();
      setDraftFormVisible(true);
      syncDraftButtons();
      if (currentJobId) updateJobEntry(currentJobId, { status: 'stopped' });
      return;
    }
    callId = data.job_id;
    draftJobId = data.draft_job_id;
    localStorage.setItem('kg_call_id', callId);
    localStorage.setItem('kg_draft_job_id', draftJobId);
    localStorage.setItem('kg_video_mode', videoMode ? '1' : '0');
    finishDraftSubmitUI(actionLabel);
    const capturedMode = videoMode;
    if (opts.onDone) {
      pollJob(callId, draftJobId, opts.onDone);
    } else {
      pollJob(callId, draftJobId, capturedMode ? onVideoDone : onDraftDone);
    }
  };
  xhr.onerror = () => {
    draftXhr = null;
    console.error('[draft] XHR network error');
    showError('Draft failed: network error — is the local server still running?');
    setStatus('error', 'err');
    finishDraftSubmitUI();
    setDraftFormVisible(true);
    syncDraftButtons();
  };
  xhr.ontimeout = () => {
    draftXhr = null;
    console.error('[draft] XHR timeout after 5 minutes');
    showError('Draft submission timed out after 5 minutes. Check your network or try a smaller file.');
    setStatus('error', 'err');
    finishDraftSubmitUI();
    setDraftFormVisible(true);
    syncDraftButtons();
  };
  xhr.send(fd);
}

function finishDraftSubmitUI(label) {
  $('draft-spinner').hidden = true;
  $('draft-btn-label').textContent = label || 'Run draft';
  const alignBtn = $('btn-align');
  if (alignBtn) alignBtn.disabled = false;
}

// ---------- stage timeline ----------
// Order of stages for each mode; each id maps to a chip that lights up.
const STAGE_ORDER = {
  audio: ['starting', 'separating', 'loading_model', 'transcribing', 'aligning', 'polishing', 'preparing', 'done'],
  video: ['starting', 'separating', 'preparing', 'done'],
};
const STAGE_LABEL = {
  starting: 'Starting',
  separating: 'Separating stems',
  loading_model: 'Loading model',
  transcribing: 'Transcribing',
  aligning: 'Aligning',
  polishing: 'Polishing',
  preparing: 'Preparing',
  done: 'Done',
};

function buildTimeline(mode) {
  const tl = $('stage-timeline');
  if (!tl) return;
  _timelineMode = mode || 'audio';
  stageTimes = {};
  lastStage = null;
  const log = $('stage-log');
  if (log) log.innerHTML = '';
  tl.innerHTML = '';
  for (const id of STAGE_ORDER[_timelineMode] || STAGE_ORDER.audio) {
    const step = document.createElement('span');
    step.className = 'stage-step';
    step.dataset.stage = id;
    // textContent for the label: stage ids arrive from the server and must
    // never be interpreted as HTML (XSS-safe by construction).
    const dot = document.createElement('span');
    dot.className = 'dot';
    step.appendChild(dot);
    step.appendChild(document.createTextNode(STAGE_LABEL[id] || id));
    tl.appendChild(step);
  }
}

function logStage(stage, now) {
  const log = $('stage-log');
  if (!log) return;
  const d = new Date(now);
  const pad = (n) => ('0' + n).slice(-2);
  const line = document.createElement('div');
  line.textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' +
    pad(d.getSeconds()) + '  ' + (STAGE_LABEL[stage] || stage);
  log.appendChild(line);
  while (log.children.length > 10) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function markTimeline(stage) {
  const tl = $('stage-timeline');
  if (!tl) return;
  const now = Date.now();
  if (stage && stage !== lastStage) {
    // stamp the stage we just left with how long it took
    if (lastStage && stageTimes[lastStage]) {
      const el = tl.querySelector('[data-stage="' + lastStage + '"]');
      if (el) {
        let t = el.querySelector('.stage-time');
        if (!t) { t = document.createElement('span'); t.className = 'stage-time'; el.appendChild(t); }
        t.textContent = fmtDuration(Math.max(0, Math.round((now - stageTimes[lastStage]) / 1000)));
      }
    }
    if (!stageTimes[stage]) stageTimes[stage] = now;
    lastStage = stage;
    logStage(stage, now);
  }
  const order = STAGE_ORDER[_timelineMode] || STAGE_ORDER.audio;
  const idx = order.indexOf(stage);
  const steps = tl.querySelectorAll('.stage-step');
  steps.forEach((el, i) => {
    const elStage = el.dataset.stage;
    const elIdx = order.indexOf(elStage);
    el.classList.toggle('done', elIdx < idx || stage === 'done');
    el.classList.toggle('active', elIdx === idx && stage !== 'done');
  });
}

// ---------- elapsed timer ----------
function startElapsed() {
  stopElapsed();
  jobStartTs = Date.now();
  const el = $('progress-elapsed');
  if (el) el.textContent = '';
  elapsedTimer = setInterval(() => {
    if (!jobStartTs) return;
    const secs = Math.floor((Date.now() - jobStartTs) / 1000);
    const el = $('progress-elapsed');
    if (el) el.textContent = fmtDuration(secs);
  }, 1000);
}

function stopElapsed() {
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
  jobStartTs = null;
}

function fmtDuration(secs) {
  if (secs < 60) return secs + 's';
  const m = Math.floor(secs / 60), s = secs % 60;
  return m + 'm ' + (s < 10 ? '0' : '') + s + 's';
}

// ---------- progress bar (draft = section 1, render = section 3) ----------
function setProgress(pct, detail, scope) {
  const bar = $(scope === 'render' ? 'render-progress' : 'progress-bar');
  const fill = $(scope === 'render' ? 'render-progress-fill' : 'progress-fill');
  const txt = $(scope === 'render' ? 'render-progress-text' : 'progress-text');
  if (bar) bar.hidden = false;
  if (fill) {
    // never move the bar backwards (estimator vs real checkpoints can overlap)
    const cur = parseFloat(fill.style.width) || 0;
    fill.style.width = Math.min(100, Math.max(cur, pct)) + '%';
  }
  if (txt) txt.textContent = detail || '';
}

async function pollProgress(djId, scope) {
  try {
    if (jobComplete) return;  // stale in-flight poll must not overwrite the final status
    const endpoint = scope === 'render' ? '/api/render-progress/' : '/api/progress/';
    const r = await fetch(endpoint + djId);
    if (!r.ok) {
      if (r.status === 401) {
        showError('Modal API key invalid (401) — open Settings (gear) and re-save your key');
        setStatus('error', 'err');
      }
      return;
    }
    const d = await r.json();
    // Phase 2 (align) reuses the same audio-hash progress file as phase 1 —
    // the first polls can read the OLD run's terminal "done" before the new
    // container overwrites it (that's the phantom Done + wrong elapsed that
    // shows up right after hitting Run alignment). Never accept 'done'
    // before any live stage: a real job always passes through starting first.
    if (scope !== 'render' && d.stage === 'done' && !lastStage) return;
    if (d.pct !== undefined) setProgress(d.pct, ((d.detail || '') + ' ' + Math.round(d.pct || 0) + '%').trim(), scope);
    if (scope === 'render') {
      const labels = {
        starting: 'Starting render...',
        rendering: 'Rendering video (ffmpeg)...',
        finalizing: 'Finalizing MP4...',
        done: 'Done!',
      };
      setStatus((labels[d.stage] || d.stage) + ' ' + Math.round(d.pct || 0) + '%', 'busy');
      return;
    }
    const stageLabels = {
      starting: 'Starting GPU job...',
      separating: 'Separating stems...',
      loading_model: 'Loading model...',
      transcribing: 'Transcribing vocals (ASR)...',
      aligning: 'Aligning lyrics...',
      polishing: 'Polishing timings...',
      preparing: _timelineMode === 'video' ? 'Preparing your karaoke video...' : 'Preparing editor data...',
      done: 'Done!',
    };
    setStatus((stageLabels[d.stage] || d.stage) + ' ' + Math.round(d.pct || 0) + '%', 'busy');
    markTimeline(d.stage);
  } catch (e) { console.warn('[progress] poll failed', e); }
}

// ---------- job polling (with progress) ----------
async function pollJob(cId, djId, onDone, scope) {
  jobComplete = false;
  setStatus(scope === 'render' ? 'Rendering on Modal...' : 'Running on GPU...', 'busy');
  setProgress(0, 'Waiting for a Modal container...', scope);
  if (scope !== 'render') { buildTimeline(videoMode ? 'video' : 'audio'); startElapsed(); }
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = setInterval(() => { if (djId) pollProgress(djId, scope); }, 2000);
  const poll = async () => {
    try {
      const r = await fetch('/api/jobs/' + cId);
      if (!r.ok) {
        const text = await r.text();
        throw new Error(text || r.status);
      }
      const data = await r.json();
      if (data.status === 'done') {
        if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
        stopElapsed();
        jobComplete = true;
        onDone(data.result);
        return;
      }
      if (data.status === 'error') {
        if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
        stopElapsed();
        jobComplete = true;
        showError('Job error: ' + (data.error || '?'));
        setStatus('error', 'err');
        $('btn-render').disabled = false;
        $('btn-render').textContent = 'Render MP4';
        if (renderDownloadUrl) $('btn-download').disabled = false;
        if (scope !== 'render') {
          // back to the form so the user can fix and retry (the button was
          // disabled at submit and nothing else re-enables it on this path)
          setDraftFormVisible(true);
          syncDraftButtons();
        }
        return;
      }
      setTimeout(poll, 3000);
    } catch (e) {
      showError('Poll failed: ' + e.message);
      setStatus('error', 'err');
      if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
      stopElapsed();
      jobComplete = true;
      if (scope === 'render') {
        $('btn-render').disabled = false;
        $('btn-render').textContent = 'Render MP4';
        if (renderDownloadUrl) $('btn-download').disabled = false;
      } else {
        setDraftFormVisible(true);
        syncDraftButtons();
        localStorage.removeItem('kg_call_id');
      }
    }
  };
  poll();
}

// Backstop: model-emitted musical-note emojis (Whisper emits them on sung
// parts; Qwen can too). The backend strips these; this covers backends not
// yet redeployed.
// NOTE: written with BMP-only escapes on purpose — astral chars (all emoji)
// are matched as surrogate pairs, so no brace escapes are needed.
function stripTranscriptNoise(t) {
  if (!t) return t;
  var ASTRAL_PAIR = '[\uD800-\uDBFF][\uDC00-\uDFFF]';
  var BMP_SYMBOLS = '[\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F]';
  t = t.replace(new RegExp(ASTRAL_PAIR + '|' + BMP_SYMBOLS, 'g'), '');
  return t.split('\n').map((l) => l.trim()).filter((l) => l).join('\n');
}

function onTranscribeDone(result) {
  // Phase 1 done: text only, no timing yet. Put it in the lyrics box for
  // editing and play the ORIGINAL upload (not vocals) for reference.
  // The form (lyrics box included) comes back — it's the workspace now.
  setDraftFormVisible(true);
  const transcript = stripTranscriptNoise((result && result.transcript) || '');
  if (!transcript.trim()) {
    showError('Transcription came back empty — try again or paste lyrics manually.');
    setStatus('error', 'err');
    setTranscriptReviewMode(false);
    setStepOneActionVisible(true);
    syncDraftButtons();
    if (currentJobId) updateJobEntry(currentJobId, { status: 'stopped' });
    return;
  }
  draftJobId = result.job_id || draftJobId;
  if ($('lyrics')) $('lyrics').value = transcript;
  setTranscriptReviewMode(true);
  const file = $('audio-file').files[0];
  if (file) {
    if (_transcriptAudioUrl) { try { URL.revokeObjectURL(_transcriptAudioUrl); } catch(_){} }
    _transcriptAudioUrl = URL.createObjectURL(file);
    const aud = $('transcript-audio');
    if (aud) aud.src = _transcriptAudioUrl;
    try {
      localStorage.setItem(TRANSCRIPT_KEY, JSON.stringify({
        transcript,
        job_id: draftJobId,
        file_name: file.name,
        file_size: file.size,
      }));
    } catch (_) {}
  }
  if (currentJobId) updateJobEntry(currentJobId, { status: 'done' });
  localStorage.removeItem('kg_call_id');
  if (draftJobId) localStorage.setItem('kg_draft_job_id', draftJobId);
  $('progress-bar').hidden = true;
  $('step-edit').hidden = true;
  $('step-render').hidden = true;
  $('step-video-done').hidden = true;
  $('step-transcript').hidden = false;
  // Step 1's job is done — only Run alignment remains.
  setStepOneActionVisible(false);
  const tstat = $('transcript-status');
  if (tstat) tstat.textContent = 'Transcript ready — fix the text above, then Run alignment.';
  setStatus('Transcript ready — fix the text, then Run alignment');
  syncDraftButtons();
}

function onDraftDone(result) {
  setStatus('Draft complete — edit line timings below');
  alignment = result.alignment;
  draftJobId = result.job_id;
  // init undo history
  _history = [_snapshot()];
  _historyIdx = 0;
  syncHistoryButtons();
  // persist for resumability (active-job keys + the job history entry)
  if (!draftFileName) draftFileName = localStorage.getItem('kg_file_name');
  const jid = currentJobId || createJobEntry('Draft', 'audio');
  currentJobId = jid;
  localStorage.setItem('kg_draft_result', JSON.stringify({
    alignment: result.alignment,
    lyrics: result.lyrics,
    report: result.report,
    job_id: result.job_id,
    duration: result.duration,
    client_job_id: jid,
  }));
  const _srcFile = $('audio-file') && $('audio-file').files[0];
  saveJobPayload(jid, {
    alignment: result.alignment,
    lyrics: result.lyrics,
    report: result.report,
    job_id: result.job_id,
    duration: result.duration,
    client_job_id: jid,
    // Identity of the uploaded audio for this job, so the stem-failure fallback
    // can refuse to load a DIFFERENT song's file as this job's waveform.
    file_name: _srcFile ? _srcFile.name : null,
    file_size: _srcFile ? _srcFile.size : null,
  });
  updateJobEntry(jid, { status: 'done' });
  localStorage.removeItem('kg_call_id');
  if (draftFileName) localStorage.setItem('kg_file_name', draftFileName);
  // transcript review consumed by this alignment — clean up its state
  localStorage.removeItem(TRANSCRIPT_KEY);
  $('step-transcript').hidden = true;
  setTranscriptReviewMode(false);
  setStepOneActionVisible(true);
  clearEditedAlignment();

  // hide progress bar
  $('progress-bar').hidden = true;

  // Set job ID for stem audio fetching and load waveform (vocals only by default)
  _waveformJobId = draftJobId;
  if ($('voc-toggle')) $('voc-toggle').checked = true;
  if ($('sv-toggle')) $('sv-toggle').checked = true;
  if ($('sv-volume')) $('sv-volume').value = 100;
  reloadWaveformAudio();
  $('step-edit').hidden = false;
  $('step-render').hidden = false;
  syncDraftButtons();
}

function onVideoDone(result) {
  setStatus('Vocals removed — your karaoke video is ready to download');
  draftJobId = result.job_id || draftJobId;
  if (currentJobId) {
    saveJobPayload(currentJobId, { video_mode: true, job_id: draftJobId, client_job_id: currentJobId });
    updateJobEntry(currentJobId, { status: 'done', mode: 'video' });
  }
  localStorage.removeItem('kg_call_id');
  localStorage.removeItem('kg_draft_job_id');
  localStorage.removeItem('kg_draft_result');
  draftFileName = null;
  localStorage.removeItem('kg_file_name');
  clearEditedAlignment();

  // hide progress bar
  $('progress-bar').hidden = true;
  $('step-edit').hidden = true;
  $('step-render').hidden = true;
  $('step-transcript').hidden = true;
  setTranscriptReviewMode(false);
  setStepOneActionVisible(true);
  $('step-video-done').hidden = false;
  const dl = $('video-download-link');
  if (dl) dl.href = '/api/video-karaoke/' + draftJobId;
  syncDraftButtons();
}

// ---------- resumability: restore on page load ----------
async function checkResumable() {
  const savedCallId = localStorage.getItem('kg_call_id');
  const savedDraftJobId = localStorage.getItem('kg_draft_job_id');
  const savedResult = localStorage.getItem('kg_draft_result');

  // case 0: stale stored job — before locking the UI, verify it's actually
  // alive. A killed/expired job used to pin the Run-draft button disabled on
  // every reload, so clicks did nothing and no job ever reached Modal.
  if (savedCallId && savedDraftJobId) {
    let alive = false;
    try {
      const r = await fetch('/api/jobs/' + savedCallId);
      if (r.ok) {
        const d = await r.json();
        alive = d.status === 'done' || d.status === 'pending' || d.status === 'running';
      }
    } catch (e) { /* treat as stale */ }
     if (!alive) {
       localStorage.removeItem('kg_call_id');
       localStorage.removeItem('kg_draft_job_id');
       localStorage.removeItem('kg_video_mode');
       // the sidebar entry for the dead job is not "running" anymore
       const dead = jobList().find((j) => j.status === 'running');
       if (dead) updateJobEntry(dead.id, { status: 'stopped' });
       syncDraftButtons();
       setStatus('ready');
       return;
     }
  }

  // case 1: job still in progress (tab refreshed mid-draft)
  if (savedCallId && savedDraftJobId) {
    callId = savedCallId;
    draftJobId = savedDraftJobId;
    videoMode = localStorage.getItem('kg_video_mode') === '1';
    // the running job entry (if any) becomes the active job again
    const running = jobList().find((j) => j.status === 'running');
    if (running) currentJobId = running.id;
    renderJobList();
    $('step-edit').hidden = true;
    $('step-render').hidden = true;
    $('step-video-done').hidden = true;
    $('btn-draft').disabled = true;
    // re-fetch the result from Modal once the job finishes
    pollJob(savedCallId, savedDraftJobId, async (result) => {
      // Transcribe-phase job (no alignment) resumed across a refresh.
      if (result && result.transcript && !result.alignment) {
        onTranscribeDone(result);
        return;
      }
      // Never let a finishing job clobber an alignment the user already
      // edited and had persisted — restore takes priority.
      if (!videoMode && localStorage.getItem(EDIT_KEY)) {
        restoreFromStorage();
        return;
      }
      if (videoMode) onVideoDone(result);
      else onDraftDone(result);
    });
    // also try to restore from Modal directly if already done
    try {
      const r = await fetch('/api/draft-result/' + savedDraftJobId);
      if (r.ok) {
        const result = await r.json();
        if (result && result.transcript && !result.alignment) {
          onTranscribeDone(result);
          return;
        }
        if (!videoMode && localStorage.getItem(EDIT_KEY)) {
          restoreFromStorage();
          return;
        }
        if (videoMode) onVideoDone(result);
        else onDraftDone(result);
      }
    } catch (e) { /* not ready yet, polling will handle it */ }
    return;
  }

  // case 2: completed draft (restore editor from storage)
  if (savedResult) {
    try {
      restoreFromStorage();
    } catch (e) {
      localStorage.removeItem('kg_draft_result');
    }
  }

  // case 3: transcript reviewed but not yet aligned (refresh between phase 1
  // and phase 2). The audio file itself can't survive a refresh (browser
  // clears file inputs), so restore the text and ask for the file again.
  try {
    const savedT = localStorage.getItem(TRANSCRIPT_KEY);
    if (savedT && !localStorage.getItem('kg_draft_result')) {
      const t = JSON.parse(savedT);
      if (t && t.transcript) {
        if ($('lyrics')) $('lyrics').value = t.transcript;
        draftJobId = t.job_id || draftJobId;
        setDraftFormVisible(true);
        setTranscriptReviewMode(true);
        $('step-transcript').hidden = false;
        setStepOneActionVisible(false);
        const tstat = $('transcript-status');
        if (tstat) tstat.textContent = 'Transcript restored — re-select the same audio file above (' + (t.file_name || 'original') + '), then Run alignment.';
        setStatus('Transcript restored — re-select audio, then Run alignment');
        syncDraftButtons();
      }
    }
  } catch (_) {}
}

function restoreFromStorage() {
  const saved = localStorage.getItem('kg_draft_result');
  if (!saved) return;
  const data = JSON.parse(saved);
  // Edited alignment (if any) wins over the generated draft timings.
  const edited = localStorage.getItem(EDIT_KEY);
  if (edited) {
    try { data.alignment = JSON.parse(edited); } catch (e) { /* use stored draft */ }
  }
  if (data.client_job_id) currentJobId = data.client_job_id;
  renderJobList();
  alignment = data.alignment;
  draftJobId = data.job_id;
  draftFileName = localStorage.getItem('kg_file_name');
  _waveformJobId = draftJobId;
  if ($('voc-toggle')) $('voc-toggle').checked = true;
  if ($('sv-toggle')) $('sv-toggle').checked = true;
  if ($('sv-volume')) $('sv-volume').value = 100;
  _history = [_snapshot()];
  _historyIdx = 0;
  syncHistoryButtons();
  reloadWaveformAudio();
  $('step-edit').hidden = false;
  $('step-render').hidden = false;
  setStatus(edited
    ? 'Restored draft WITH your waveform edits'
    : 'Restored previous draft — edit or render below');
}

function startNew() {
  stopRulerLoop();
  if (wavesurfer) { wavesurfer.pause(); wavesurfer.destroy(); wavesurfer = null; }
  if (_waveformBlobUrl) { try { URL.revokeObjectURL(_waveformBlobUrl); } catch(_){} _waveformBlobUrl = null; }
  if (_transcriptAudioUrl) { try { URL.revokeObjectURL(_transcriptAudioUrl); } catch(_){} _transcriptAudioUrl = null; }
  const _aud = $('transcript-audio'); if (_aud) _aud.removeAttribute('src');
  localStorage.removeItem(TRANSCRIPT_KEY);
  $('step-transcript').hidden = true;
  setTranscriptReviewMode(false);
  setStepOneActionVisible(true);
  if (regions) { regions = null; }
  if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
  stopElapsed();
  activeLineEl = null;
  playingLineEl = null;
  activeSegI = null;
  alignment = null;
  _history = [];
  _historyIdx = -1;
  draftJobId = null;
  callId = null;
  videoMode = false;
  currentJobId = null;
  draftFileName = null;
  localStorage.removeItem('kg_call_id');
  localStorage.removeItem('kg_draft_job_id');
  localStorage.removeItem('kg_draft_result');
  localStorage.removeItem('kg_video_mode');
  localStorage.removeItem('kg_file_name');
  localStorage.removeItem(SONG_START_KEY);
  setDraftFormVisible(true);
  clearEditedAlignment();
  // clear the draft inputs so a "new" job really starts fresh
  const audioInput = $('audio-file');
  if (audioInput) audioInput.value = '';
  const lyricsBox = $('lyrics');
  if (lyricsBox) lyricsBox.value = '';
  const startBox = $('song-start');
  if (startBox) startBox.value = '';
  const langBox = $('language');
  if (langBox) langBox.value = 'tl';
  const genreBox = $('genre');
  if (genreBox) genreBox.value = 'hiphop';
  $('step-edit').hidden = true;
  $('step-render').hidden = true;
  $('step-video-done').hidden = true;
  syncDraftButtons();
  setStatus('ready');
  $('progress-bar').hidden = true;
  $('render-progress').hidden = true;
  $('line-palette').innerHTML = '';
  $('line-info').textContent = 'Click a line to select it';
  renderDownloadUrl = null;
  renderDownloadName = 'karaoke.mp4';
  $('btn-render').textContent = 'Render MP4';
  $('btn-download').disabled = true;
  $('btn-download').textContent = 'Download';
  renderJobList();
}

// ---------- custom time ruler (shows every second, updates on scroll/zoom) ----------
let _rulerLoop = null;        // continuous redraw loop (avoids scroll event issues)
let _lastRulerScroll = -1;    // only redraw when scroll actually changes
let _lastRulerWidth = -1;     // only redraw when width changes too

function createTimeRuler() {
  const container = $('wave-timeline');
  if (!container) return null;
  container.innerHTML = '';
  const canvas = document.createElement('canvas');
  canvas.style.display = 'block';
  canvas.style.width = '100%';
  container.appendChild(canvas);
  _lastRulerScroll = -1;
  _lastRulerWidth = -1;
  startRulerLoop();
  return canvas;
}

function startRulerLoop() {
  stopRulerLoop();
  const loop = () => {
    drawTimeRuler();
    _rulerLoop = requestAnimationFrame(loop);
  };
  _rulerLoop = requestAnimationFrame(loop);
}

function stopRulerLoop() {
  if (_rulerLoop) {
    cancelAnimationFrame(_rulerLoop);
    _rulerLoop = null;
  }
}

function drawTimeRuler() {
  const canvas = _customTimeline;
  if (!canvas || !wavesurfer) return;
  const duration = wavesurfer.getDuration() || 0;
  if (!duration) return;
  const container = canvas.parentElement;
  const w = container.clientWidth;
  if (!w) return;

  // wavesurfer v7 scrolls with a CSS transform, NOT native scrollLeft. The
  // official timeline plugin reads the scroll offset via getScroll() and maps
  // pixels via getWrapper().scrollWidth / duration. BUT at 'ready' the wave
  // canvas isn't laid out yet — scrollWidth is still ~viewport width, which
  // would make pxPerSec tiny and every label overlap into garbage. Only trust
  // scrollWidth once it actually overflows the viewport; otherwise use
  // options.minPxPerSec (the true render scale, updated by zoom()).
  const minPx = wavesurfer.options.minPxPerSec || 50;
  const fullPx = wavesurfer.getWrapper().scrollWidth || 0;
  const pxPerSec = fullPx > w ? fullPx / duration : minPx;
  const scrollLeft = typeof wavesurfer.getScroll === 'function' ? wavesurfer.getScroll() : 0;

  // Skip BEFORE mutating the canvas. clearRect()/resize() blank the canvas,
  // so skipping the redraw AFTER clearing leaves the ruler invisible at rest
  // (it only appeared while scrolling because each scroll frame redrew).
  if (scrollLeft === _lastRulerScroll && w === _lastRulerWidth) return;
  _lastRulerScroll = scrollLeft;
  _lastRulerWidth = w;

  const dpr = window.devicePixelRatio || 1;
  const h = 22;
  const pxW = Math.round(w * dpr);
  const pxH = Math.round(h * dpr);
  if (canvas.width !== pxW || canvas.height !== pxH) {
    canvas.width = pxW;
    canvas.height = pxH;
    canvas.style.height = h + 'px';
  }
  const ctx = canvas.getContext('2d');
  // setTransform REPLACES the transform — ctx.scale() would stack every frame
  // and progressively blow up the drawing scale until the ruler vanishes.
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const t0 = scrollLeft / pxPerSec;
  const t1 = t0 + w / pxPerSec;
  ctx.font = '10px system-ui, sans-serif';
  ctx.textBaseline = 'top';
  const startSec = Math.max(0, Math.floor(t0));
  const endSec = Math.min(duration, Math.ceil(t1));
  for (let s = startSec; s <= endSec; s++) {
    const x = s * pxPerSec - scrollLeft;
    const isMajor = s % 5 === 0;
    ctx.strokeStyle = isMajor ? '#aaa' : '#444';
    ctx.beginPath();
    ctx.moveTo(x + 0.5, h - (isMajor ? 10 : 5));
    ctx.lineTo(x + 0.5, h);
    ctx.stroke();
    ctx.fillStyle = '#ccc';
    const m = Math.floor(s / 60);
    const label = m > 0 ? m + ':' + String(s % 60).padStart(2, '0') : String(s);
    ctx.fillText(label, x + 3, 2);
  }
}

// ---------- step 2: wavesurfer line-level editor ----------
function initWaveSurfer(url, opts) {
  opts = opts || {};
  if (wavesurfer) { try { wavesurfer.destroy(); } catch (_) {} wavesurfer = null; }
  const regionsPlugin = WaveSurfer.Regions.create();
  wavesurfer = WaveSurfer.create({
    container: '#waveform',
    waveColor: '#4a6cf7',
    progressColor: '#2ecc71',
    cursorColor: '#fff',
    cursorWidth: 2,
    height: 120,
    url: url,
    minPxPerSec: 50,
    dragToSeek: { debounceTime: 0 },
    plugins: [regionsPlugin],
  });
  regions = regionsPlugin;
  _customTimeline = createTimeRuler();

  regions.on('region-updated', (region) => {
    const d = region.data;
    if (!d || !alignment) return;
    pushHistory();  // snapshot BEFORE the change
    if (d.wordI !== undefined) {
      // WORD region drag: write the word, then keep the line span glued.
      syncWordFromRegion(d.segI, d.wordI);
      syncLineSpanFromWords(d.segI);
      // The write-back may have clamped an illegal drop — snap the bracket
      // back so it never disagrees with the chip label / click-to-play.
      snapWordRegionToData(d.segI, d.wordI);
      setWhiteRegion(activeSegI, activeWordI);
      updateLineWordTimings(d.segI);
      updateLinePaletteTiming(d.segI);
      persistAlignment();
      return;
    }
    const seg = alignment.segments[d.segI];
    const oldStart = seg.start;
    const oldEnd = seg.end;
    seg.start = +region.start.toFixed(3);
    seg.end = +region.end.toFixed(3);
    const ds = seg.start - oldStart;
    const de = seg.end - oldEnd;
    for (const w of seg.words) {
      w.start += ds;
      w.end += de;
    }
    updateLinePaletteTiming(d.segI);
    persistAlignment();
  });

  regions.on('region-clicked', (region, e) => {
    e.stopPropagation();
    const d = region.data;
    if (!d || !alignment) return;
    // Click position -> song time using the TRUE scale (full rendered width
    // over duration). The old code fell back to a guessed 50 px/sec whenever
    // the song fit on screen, which seeks to the wrong time when zoomed out.
    const duration = wavesurfer.getDuration() || 0;
    const wrapper = wavesurfer.getWrapper ? wavesurfer.getWrapper() : null;
    const scrollLeft = typeof wavesurfer.getScroll === 'function' ? wavesurfer.getScroll() : 0;
    const fullPx = wrapper ? wrapper.scrollWidth : 0;
    const rect = $('waveform').getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const clickTime = (fullPx > 0 && duration > 0)
      ? Math.max(0, Math.min(duration, (clickX + scrollLeft) / (fullPx / duration)))
      : 0;
    // Select without overriding cursor position — and without scrolling the
    // page (a waveform click must never move the screen).
    activeSegI = d.segI;
    if (activeLineEl) activeLineEl.classList.remove('pal-active');
    const chip = lineChips[d.segI];
    if (chip) {
      chip.classList.add('pal-active');
      activeLineEl = chip;
    }
    if (d.wordI !== undefined) {
      selectWord(d.segI, d.wordI, { play: false, keepCursor: true });
    } else {
      activeWordI = null;
      clearWordActive();
      setWhiteRegion(d.segI);
    }
    $('line-info').textContent = lineInfoText(d.segI);
    syncLineButtons();
    // Set cursor position AFTER selection so it isn't overridden
    wavesurfer.setTime(clickTime);
  });

  // Defensive persistence: even if a drag never fired region-updated (or the
  // user refreshed right after releasing the mouse), re-sync every segment
  // from its live region so the stored alignment always matches the screen.
  $('waveform').addEventListener('pointerup', () => persistAllFromRegions());

  // Ctrl+mousewheel = zoom waveform (not browser zoom)
  $('waveform').addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const cur = wavesurfer.options.minPxPerSec || 50;
    const delta = e.deltaY < 0 ? 1.2 : 0.8;
    const next = Math.max(10, Math.min(1000, Math.round(cur * delta)));
    wavesurfer.zoom(next);
    const z = $('zoom');
    if (z) z.value = next;
  }, { passive: false });

  wavesurfer.on('ready', () => {
    buildRegions();
    buildLinePalette();
    if (alignment && alignment.segments.length) {
      // keep the current selection across a Voc/BV reload (don't reset to 0)
      const keep = (activeSegI != null && activeSegI < alignment.segments.length) ? activeSegI : 0;
      selectLineSilent(keep);
    }
    // Resume playback at the exact position where the swap happened.
    if (opts.resumeTime != null && opts.resumeTime <= (wavesurfer.getDuration() || 0)) {
      wavesurfer.setTime(opts.resumeTime);
    }
    if (opts.resume && opts.resumeTime != null) {
      wavesurfer.play();
      $('btn-play').textContent = '\u23F8 Pause';
    }
    startRulerLoop();
    setAudioLoading(false);
  });
  wavesurfer.on('finish', () => { $('btn-play').textContent = '\u25B6 Play';  });
  // Decode/fetch failures were silent before (empty waveform, zero lines, no
  // message). Surface them so a bad blob never looks like a hang.
  wavesurfer.on('error', (e) => {
    console.error('[waveform] wavesurfer error:', e);
    setAudioLoading(false);
    showError('Waveform failed to load audio (' + ((e && e.message) || e || 'decode error') +
      ') — try re-selecting the file and running the draft again.');
  });
  wavesurfer.on('audioprocess', (t) => highlightPlayingLine(t));
  wavesurfer.on('zoom', () => {
    _lastRulerScroll = -1;  // force redraw on zoom (px-per-sec changed)
    _lastRulerWidth = -1;
  });
  wavesurfer.on('redraw', () => {
    _lastRulerScroll = -1;  // wave canvas re-laid-out: re-check scrollWidth
    _lastRulerWidth = -1;
  });
}

function buildRegions() {
  if (!wavesurfer || !alignment || !regions) return;
  regions.clearRegions();
  if (wordMode) {
    // One draggable region per WORD. Line color tints the word so verse
    // grouping stays visible; words are slightly more opaque than lines.
    alignment.segments.forEach((seg, segI) => {
      const baseColor = COLORS[segI % COLORS.length];
      (seg.words || []).forEach((w, wi) => {
        const start = Math.max(0, w.start);
        const end = Math.max(start + 0.03, w.end);
        const region = regions.addRegion({
          start, end,
          color: baseColor + '70',
          drag: true,
          resize: true,
          content: '',
          minLength: 0.03,
        });
        region.data = { segI, wordI: wi };
      });
    });
  } else {
    alignment.segments.forEach((seg, segI) => {
      const baseColor = COLORS[segI % COLORS.length];
      const start = Math.max(0, seg.start);
      const end = Math.max(start + 0.03, seg.end);
      const region = regions.addRegion({
        start, end,
        color: baseColor + '40',
        drag: true,   // drag BODY to move the whole line; drag edges to stretch
        resize: true,
        content: seg.text,
        minLength: 0.03,
      });
      // The wavesurfer Region class does NOT store an options `data` property —
      // tag the segI onto the region object ourselves or every lookup bails out.
      region.data = { segI };
    });
  }
  // Style handles after all regions are built (async so DOM is ready)
  setTimeout(() => {
    for (const r of regions.getRegions()) {
      _styleHandles(r.element, false);
    }
  }, 0);
}


// ---------- line palette (clickable line list) ----------
function buildLinePalette() {
  if (!alignment) return;
  const pal = $('line-palette');
  pal.innerHTML = '';
  lineChips = [];
  lineDivs = [];
  lineWordChips = [];
  _playingWordChip = null;
  playingWordKey = null;
  alignment.segments.forEach((seg, segI) => {
    const lineDiv = document.createElement('div');
    lineDiv.className = 'pal-line';
    const baseColor = COLORS[segI % COLORS.length];
    const chip = document.createElement('span');
    chip.className = 'pal-line-chip';
    chip.textContent = seg.text;
    chip.dataset.segI = segI;
    chip.style.borderBottom = '3px solid ' + baseColor;
    chip.addEventListener('click', () => selectLine(segI));
    chip.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      editChipText(chip, segI);
    });
    lineChips[segI] = chip;
    const time = document.createElement('span');
    time.className = 'pal-line-time';
    time.dataset.segI = segI;
    lineTimes[segI] = time;
    lineDiv.appendChild(chip);
    lineDiv.appendChild(time);
    // Every verse carries its own words row: click a word to play just it,
    // double-click to fix its wording. Line chip above still plays/retypes
    // the whole verse.
    const wordsBox = document.createElement('div');
    wordsBox.className = 'pal-words';
    lineDiv.appendChild(wordsBox);
    lineDivs[segI] = lineDiv;
    pal.appendChild(lineDiv);
    buildLineWords(segI);
    syncSegmentFromRegion(segI);
    updateLinePaletteTiming(segI);
  });
}

// Live timing shown next to each clickable verse, refreshed whenever a
// waveform region is dragged or a line is nudged via buttons/keyboard.
function updateLinePaletteTiming(segI) {
  if (!alignment || !alignment.segments[segI]) return;
  if (activeLineEl && +activeLineEl.dataset.segI === segI) {
    $('line-info').textContent = lineInfoText(segI);
  }
  const t = lineTimes[segI];
  if (t) {
    const seg = alignment.segments[segI];
    t.textContent = seg.start.toFixed(2) + 's\u2013' + seg.end.toFixed(2) + 's';
  }
}

// Double-click a verse chip to edit its text inline. Enter/blur commits
// (writes the new text into the alignment + region label), Escape cancels.
function editChipText(chip, segI) {
  if (!alignment) return;
  const seg = alignment.segments[segI];
  if (!seg) return;
  const oldText = seg.text;
  chip.contentEditable = 'true';
  chip.classList.add('pal-editing');
  chip.title = 'Edit text — Enter to save, Esc to cancel';
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(chip);
  sel.removeAllRanges();
  sel.addRange(range);
  const commit = () => {
    chip.contentEditable = 'false';
    chip.removeAttribute('title');
    chip.classList.remove('pal-editing');
    const t = (chip.textContent || '').trim();
    if (!t) { chip.textContent = oldText; return; }
    if (t !== seg.text) {
      pushHistory();
      seg.text = t;
      const words = t.split(/\s+/).filter(Boolean);
      const oldWords = (seg.words || []).slice();
      if (oldWords.length === words.length && oldWords.length > 0) {
        // Same word count (typo fix, casing, punctuation): timings are
        // positions, not text — keep every dragged boundary, only swap labels.
        // Even-splitting here would silently discard all per-word drags on
        // this line, and the render would burn the evenly-split times.
        seg.words = words.map((w, i) => ({
          text: w,
          start: oldWords[i].start,
          end: oldWords[i].end,
          probability: oldWords[i].probability ?? 1.0,
        }));
      } else {
        const dur = Math.max(0.03, seg.end - seg.start);
        seg.words = words.map((w, i) => ({
          text: w,
          start: +(seg.start + dur * i / words.length).toFixed(3),
          end: +(seg.start + dur * (i + 1) / words.length).toFixed(3),
          probability: 1.0,
        }));
      }
      const r = findRegion(segI);
      if (r && r.setOptions) r.setOptions({ content: t });
      if (wordMode) {
        // the even split created new word boundaries — rebuild all regions
        // and this verse's words row
        buildRegions();
        buildLineWords(segI);
      }
      if (activeLineEl && +activeLineEl.dataset.segI === segI) {
        $('line-info').textContent = lineInfoText(segI);
      }
      persistAlignment();
    }
  };
  chip.onblur = commit;
  chip.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); chip.blur(); }
    else if (e.key === 'Escape') { e.preventDefault(); chip.textContent = oldText; chip.blur(); }
  };
}

function lineInfoText(segI) {
  const seg = alignment.segments[segI];
  return seg.text;  // the actual lyrics only — no line number, no timings
}

// ---------- per-verse words rows (live inside each palette line) ----------
// One chip per WORD: single click plays it, double-click edits its text
// inline (Enter/blur saves, Esc cancels). No edit mode, no extra buttons.
let _wordClickTimer = null;

function makeWordChip(segI, wi) {
  const seg = alignment.segments[segI];
  const w = seg.words[wi];
  const chip = document.createElement('span');
  chip.className = 'pal-word';
  chip.dataset.wi = wi;
  const txt = document.createElement('b');
  txt.textContent = w.text;
  chip.appendChild(txt);
  const tm = document.createElement('time');
  tm.textContent = w.start.toFixed(2);
  chip.appendChild(tm);
  // Single click plays — delayed a beat so a double-click can cancel it
  // and edit instead (otherwise every text edit starts with audio).
  chip.addEventListener('click', () => {
    if (_wordClickTimer) clearTimeout(_wordClickTimer);
    _wordClickTimer = setTimeout(() => { _wordClickTimer = null; selectWord(segI, wi, { play: true }); }, 260);
  });
  chip.addEventListener('dblclick', (e) => {
    e.stopPropagation();
    if (_wordClickTimer) { clearTimeout(_wordClickTimer); _wordClickTimer = null; }
    editWordText(chip, segI, wi);
  });
  return chip;
}

// (Re)build one verse's words row (word count changed: text edit, undo).
function buildLineWords(segI) {
  const box = lineDivs[segI] && lineDivs[segI].querySelector('.pal-words');
  if (!box || !alignment || !alignment.segments[segI]) return;
  box.innerHTML = '';
  const seg = alignment.segments[segI];
  lineWordChips[segI] = [];
  (seg.words || []).forEach((w, wi) => {
    const chip = makeWordChip(segI, wi);
    lineWordChips[segI][wi] = chip;
    box.appendChild(chip);
  });
  restoreActiveWord(segI);
}

// Refresh one verse's word time labels after a drag (count unchanged).
function updateLineWordTimings(segI) {
  if (!alignment || segI == null || !alignment.segments[segI]) return;
  const seg = alignment.segments[segI];
  const arr = lineWordChips[segI] || [];
  (seg.words || []).forEach((w, wi) => {
    const chip = arr[wi];
    if (!chip) return;
    const tm = chip.querySelector('time');
    if (tm) tm.textContent = w.start.toFixed(2);
  });
}

function clearWordActive() {
  for (const arr of lineWordChips) {
    if (!arr) continue;
    for (const c of arr) if (c) c.classList.remove('pal-word-active');
  }
}

function markActiveWord(segI, wordI) {
  clearWordActive();
  const c = lineWordChips[segI] && lineWordChips[segI][wordI];
  if (c) c.classList.add('pal-word-active');
}

// After a words-row rebuild, re-light the selected word if it's in this verse
// (fresh chips are born without classes).
function restoreActiveWord(segI) {
  if (activeSegI === segI && activeWordI != null && alignment &&
      alignment.segments[segI] && activeWordI < alignment.segments[segI].words.length) {
    markActiveWord(segI, activeWordI);
  }
}

// Double-click a word chip to edit its text inline. Only the word itself
// becomes editable (the timestamp is untouched). Enter/blur saves, Esc
// cancels. Typing spaces splits one word into words sharing its span evenly;
// clearing the word deletes it (a line keeps at least one word). One undo step.
function editWordText(chip, segI, wi) {
  const seg = alignment.segments[segI];
  if (!seg || !seg.words || !seg.words[wi]) return;
  if (chip.classList.contains('pal-editing')) return;
  // Typing over playback is chaos — pause first.
  if (wavesurfer && wavesurfer.isPlaying()) {
    wavesurfer.pause();
    const _pb2 = $('btn-play'); if (_pb2) _pb2.textContent = '\u25B6 Play';
    _cancelPreview();
  }
  const w = seg.words[wi];
  const oldText = w.text;
  const b = chip.querySelector('b') || chip;
  b.contentEditable = 'true';
  chip.classList.add('pal-editing');
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(b);
  sel.removeAllRanges();
  sel.addRange(range);
  let _done = false;
  const finish = (save) => {
    if (_done) return;
    _done = true;
    b.contentEditable = 'false';
    b.onblur = null;
    b.onkeydown = null;
    chip.classList.remove('pal-editing');
    const t = (b.textContent || '').trim().replace(/\s+/g, ' ');
    if (!save || (!t && seg.words.length <= 1)) {
      if (!save) clearError();
      else showError('A line needs at least one word — keeping the old text.');
      b.textContent = oldText;
      buildLineWords(segI);
      return;
    }
    if (!t) {
      // emptied with siblings remaining = delete this word
      pushHistory();
      seg.words.splice(wi, 1);
      if (activeWordI != null && activeWordI >= seg.words.length) activeWordI = seg.words.length - 1;
    } else if (t !== oldText) {
      pushHistory();
      const parts = t.split(' ').filter(Boolean);
      if (parts.length <= 1) {
        w.text = parts[0];
      } else {
        const span = Math.max(0.03, w.end - w.start);
        const repl = parts.map((p, k) => ({
          text: p,
          start: +(w.start + span * k / parts.length).toFixed(3),
          end: +(w.start + span * (k + 1) / parts.length).toFixed(3),
          probability: w.probability ?? 1.0,
        }));
        seg.words.splice(wi, 1, ...repl);
      }
    } else {
      buildLineWords(segI);
      return;
    }
    // keep seg.text in sync so renders/exports use the edited words
    syncLineSpanFromWords(segI);
    seg.text = seg.words.map((x) => x.text).join(' ');
    const lc = lineChips[segI];
    if (lc) lc.textContent = seg.text;
    buildRegions();
    buildLineWords(segI);
    setWhiteRegion(segI, null);
    updateLinePaletteTiming(segI);
    $('line-info').textContent = lineInfoText(segI);
    persistAlignment();
  };
  b.onblur = () => finish(true);
  b.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
}

function selectWord(segI, wordI, opts = {}) {
  if (!wavesurfer || !alignment) return;
  // A word-chip single-click may still be pending (play delay) — an explicit
  // selection from anywhere else wins and the stale timer must not fire after.
  if (_wordClickTimer) { clearTimeout(_wordClickTimer); _wordClickTimer = null; }
  const seg = alignment.segments[segI];
  if (!seg || !seg.words || !seg.words[wordI]) return;
  activeSegI = segI;
  activeWordI = wordI;
  if (opts.keepCursor !== true) {
    syncWordFromRegion(segI, wordI, { history: false });
    // Same clamp-then-snap as the drag path: a click must never leave the
    // bracket disagreeing with the word it just (re)read.
    snapWordRegionToData(segI, wordI);
  }
  // palette highlight follows too
  if (activeLineEl) activeLineEl.classList.remove('pal-active');
  const chip = lineChips[segI];
  if (chip) {
    chip.classList.add('pal-active');
    activeLineEl = chip;
  }
  markActiveWord(segI, wordI);
  setWhiteRegion(segI, wordI);
  $('line-info').textContent = lineInfoText(segI) +
    '   \u2014 word ' + (wordI + 1) + '/' + seg.words.length;
  if (opts.play) playWord(segI, wordI);
}

async function playWord(segI, wordI) {
  if (!wavesurfer || !alignment) return;
  const seg = alignment.segments[segI];
  const w = seg.words && seg.words[wordI];
  if (!w) return;
  // Play EXACTLY the word's own bracket [w.start, w.end] — no pre-roll, no
  // tail. Any offset (even the old 30ms pre-roll) reads as "plays earlier than
  // the bracket", and any overshoot reads as "surpasses the bracket". If the
  // attack is clipped the fix is to drag the bracket, not to offset playback.
  const mySeq = ++_previewSeq;
  if (lineStopTimer) { clearInterval(lineStopTimer); lineStopTimer = null; }
  const start = Math.max(0, +w.start);
  const stopAt = Math.max(start + 0.03, +w.end);
  await seekPreviewTo(start);
  if (mySeq !== _previewSeq || !wavesurfer) return;
  try { await wavesurfer.play(); } catch (_) {}
  if (mySeq !== _previewSeq || !wavesurfer) return;
  const btn = $('btn-play'); if (btn) btn.textContent = '\u23F8 Pause';
  let stopped = false;
  const doStop = (snap) => {
    if (stopped || mySeq !== _previewSeq) return;
    stopped = true;
    if (lineStopTimer) { clearInterval(lineStopTimer); lineStopTimer = null; }
    try { wavesurfer.pause(); } catch (_) {}
    // Rest the cursor ON the bracket end so the visual always agrees with
    // what was just heard (previously it rested at the overshoot point).
    // Re-assert after 150ms: a queued timeupdate from the pre-pause position
    // can overwrite the first setTime while paused (no ticks to correct it),
    // leaving the cursor permanently past the bracket as in the screenshot.
    if (snap) {
      try { wavesurfer.setTime(stopAt); } catch (_) {}
      setTimeout(() => {
        if (mySeq !== _previewSeq || !wavesurfer) return;
        try {
          if (!wavesurfer.isPlaying()) wavesurfer.setTime(stopAt);
        } catch (_) {}
      }, 150);
    }
    const b2 = $('btn-play'); if (b2) b2.textContent = '\u25B6 Play';
  };
  // Primary stop: wall-clock duration measured from play() start (immune to
  // currentTime quantization). Backstop poll below handles user seeks.
  // Stop a hair early (12ms) so HTMLAudio pause latency lands ON the edge
  // instead of past it; the snap above then rests exactly on stopAt.
  const durMs = Math.max(40, (stopAt - start) * 1000 - 12);
  setTimeout(() => doStop(true), durMs);
  // Backstop poll, armed only AFTER playback is observed inside the window.
  // The old code polled immediately, so a still-pending seek read the OLD
  // playhead and paused instantly (or never armed) — the "sometimes wrong"
  // flake when clicking words far apart.
  let armed = false;
  lineStopTimer = setInterval(() => {
    if (mySeq !== _previewSeq || !wavesurfer) { clearInterval(lineStopTimer); lineStopTimer = null; return; }
    let t = null;
    try { t = wavesurfer.getCurrentTime(); } catch (_) { return; }
    if (t == null || !isFinite(t)) return;
    if (!armed) {
      if (t >= start - 0.06 && t < stopAt) armed = true;
      return;
    }
    // User scrubbed away mid-preview — hand control back without snapping.
    if (t < start - 0.25) { clearInterval(lineStopTimer); lineStopTimer = null; return; }
    if (t >= stopAt) doStop(true);
  }, 15);
}

function selectLine(segI) {
  if (!wavesurfer || !alignment) return;
  // Same stale-timer guard as selectWord: a pending word-click play must not
  // override an explicit line selection (line chip, Prev/Next, waveform).
  if (_wordClickTimer) { clearTimeout(_wordClickTimer); _wordClickTimer = null; }
  const seg = syncSegmentFromRegion(segI);  // use live region timings, never stale
  activeSegI = segI;
  activeWordI = null;

  if (activeLineEl) activeLineEl.classList.remove('pal-active');
  const chip = lineChips[segI];
  if (chip) {
    chip.classList.add('pal-active');
    // Never yank the screen on a click — the chip you tapped is already
    // visible. Only scroll when Follow is on (Prev/Next stepping, playback).
    if (followEnabled()) chip.scrollIntoView({ behavior: 'smooth', block: 'center' });
    activeLineEl = chip;
  }

  // No setTime here: playLine() does the single seek-then-play so there is
  // exactly one async seek (two back-to-back seeks raced and played stale audio).

  // The white line + move handle follow the selected verse.

  playingSegI = null;
  clearWordActive();
  setWhiteRegion(segI);

  $('line-info').textContent = lineInfoText(segI);

  playLine(segI, seg);
}

// Same as selectLine but does not auto-play — used to arm the first line so the
// spacebar and the move handle are ready immediately after a draft loads.
function selectLineSilent(segI) {
  if (!wavesurfer || !alignment) return;
  syncSegmentFromRegion(segI);
  activeSegI = segI;
  activeWordI = null;
  if (activeLineEl) activeLineEl.classList.remove('pal-active');
  const chip = lineChips[segI];
  if (chip) {
    chip.classList.add('pal-active');
    activeLineEl = chip;
  }

  playingSegI = null;
  clearWordActive();
  setWhiteRegion(segI);
  $('line-info').textContent = lineInfoText(segI);
  syncLineButtons();
  wavesurfer.setTime(alignment.segments[segI].start);
}

function highlightPlayingLine(t) {
  if (!alignment || !regions) return;
  for (let si = 0; si < alignment.segments.length; si++) {
    const seg = alignment.segments[si];
    if (t >= seg.start && t < seg.end) {
      const chip = lineChips[si];
      if (chip && playingLineEl !== chip) {
        if (playingLineEl) playingLineEl.classList.remove('pal-playing');
        chip.classList.add('pal-playing');
        playingLineEl = chip;
      }
      // Track the white line + move handle onto the line being sung.
      if (playingSegI !== si) {
        playingSegI = si;
        if (!wordMode) setWhiteRegion(si);
        // Follow mode only: scroll the playing line to center. With Follow
        // off the screen stays put (highlight still moves).
        if (followEnabled()) {
          const chip = lineChips[si];
          if (chip) chip.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }
      // Word-mode: highlight the word under the playhead in that verse's
      // own words row (every verse shows its words now, not just the active
      // line — so the playing word lights up wherever it is).
      if (wordMode) {
        let cur = null;
        for (let wi = 0; wi < (seg.words || []).length; wi++) {
          const w = seg.words[wi];
          if (t >= w.start && t < w.end) { cur = wi; break; }
        }
        const key = si + ':' + (cur == null ? 'none' : cur);
        if (key !== playingWordKey) {
          if (_playingWordChip) { _playingWordChip.classList.remove('pal-word-playing'); _playingWordChip = null; }
          playingWordKey = key;
          const wc = cur != null && lineWordChips[si] ? lineWordChips[si][cur] : null;
          if (wc) {
            _playingWordChip = wc;
            wc.classList.add('pal-word-playing');
            if (followEnabled()) wc.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
          }
        }
      }
        // keep the handle glued to the white line every tick
      return;
    }
  }
}

function _styleHandles(el, isSel) {
  if (!el) return;
  // Handles use part="region-handle region-handle-left/right" (not class)
  const handles = el.querySelectorAll('[part*="region-handle"]');
  handles.forEach(h => {
    const isLeft = h.getAttribute('part').includes('left');
    h.style.display = isSel ? 'block' : 'none';
    h.style.position = 'absolute';
    h.style.top = '0';
    h.style.bottom = '0';
    h.style.width = isSel ? '6px' : '3px';
    h.style.background = isSel ? 'rgba(0,0,0,0.85)' : 'rgba(255,255,255,0.2)';
    h.style.border = isSel ? '2px solid #ffee00' : 'none';
    h.style.borderRadius = '3px';
    h.style.boxShadow = isSel ? '0 0 6px rgba(255,238,0,0.5)' : 'none';
    h.style.cursor = isSel ? 'ew-resize' : 'default';
    h.style.zIndex = '25';
    h.style.pointerEvents = isSel ? 'auto' : 'none';
    if (isLeft) {
      h.style.left = '-2px';
    } else {
      h.style.right = '-2px';
    }
  });
}

function setWhiteRegion(segI, wordI = null) {
  if (!regions) return;
  for (const r of regions.getRegions()) {
    if (!r.data) continue;
    let isSel;
    if (wordMode && r.data.wordI !== undefined) {
      // word region: white only the active WORD (or all words of the line
      // when a whole line is selected without a word)
      isSel = wordI == null
        ? r.data.segI === segI
        : (r.data.segI === segI && r.data.wordI === wordI);
    } else {
      isSel = r.data.segI === segI && r.data.wordI === undefined;
    }
    const base = COLORS[r.data.segI % COLORS.length] + (r.data.wordI !== undefined ? '70' : '40');
    r.setOptions({ color: isSel ? '#ffffff90' : base });
    _styleHandles(r.element, isSel);
  }
}

async function playLine(segI, segOverride) {
  if (!wavesurfer || !alignment) return;
  // Pin the LIVE timings captured at selection time; do not re-read a region
  // that may lag behind an in-flight drag. In word mode there is no line
  // region, so re-derive the span from the words first (drag path keeps it
  // glued, but never trust a stale seg here).
  if (wordMode) syncLineSpanFromWords(segI);
  const seg = segOverride || syncSegmentFromRegion(segI);
  if (!seg) return;
  const mySeq = ++_previewSeq;
  if (lineStopTimer) { clearInterval(lineStopTimer); lineStopTimer = null; }
  const start = Math.max(0, +seg.start);
  const stopAt = Math.max(start + 0.03, +seg.end);
  await seekPreviewTo(start);
  if (mySeq !== _previewSeq || !wavesurfer) return;
  try { await wavesurfer.play(); } catch (_) {}
  if (mySeq !== _previewSeq || !wavesurfer) return;
  const btn = $('btn-play'); if (btn) btn.textContent = '\u23F8 Pause';
  let stopped = false;
  const doStop = (snap) => {
    if (stopped || mySeq !== _previewSeq) return;
    stopped = true;
    if (lineStopTimer) { clearInterval(lineStopTimer); lineStopTimer = null; }
    try { wavesurfer.pause(); } catch (_) {}
    if (snap) {
      try { wavesurfer.setTime(stopAt); } catch (_) {}
      setTimeout(() => {
        if (mySeq !== _previewSeq || !wavesurfer) return;
        try {
          if (!wavesurfer.isPlaying()) wavesurfer.setTime(stopAt);
        } catch (_) {}
      }, 150);
    }
    const b2 = $('btn-play'); if (b2) b2.textContent = '\u25B6 Play';
  };
  // Exact line window: no +0.05 tail (it played audibly past the bracket).
  const durMs = Math.max(40, (stopAt - start) * 1000 - 12);
  setTimeout(() => doStop(true), durMs);
  let armed = false;
  lineStopTimer = setInterval(() => {
    if (mySeq !== _previewSeq || !wavesurfer) { clearInterval(lineStopTimer); lineStopTimer = null; return; }
    let t = null;
    try { t = wavesurfer.getCurrentTime(); } catch (_) { return; }
    if (t == null || !isFinite(t)) return;
    if (!armed) {
      if (t >= start - 0.06 && t < stopAt) armed = true;
      return;
    }
    // The user moved elsewhere (seek / continuous play / another line) — let
    // that playback run without a stale stop point.
    if (t < start - 0.25) {
      clearInterval(lineStopTimer);
      lineStopTimer = null;
      return;
    }
    if (t >= stopAt) doStop(true);
  }, 15);
}

// ---------- precision line editing (buttons + keyboard) ----------
function findRegion(segI, wordI) {
  if (!regions) return null;
  for (const r of regions.getRegions()) {
    if (!r.data || r.data.segI !== segI) continue;
    if (wordI === undefined) {
      if (r.data.wordI === undefined) return r;   // line region
    } else if (r.data.wordI === wordI) {
      return r;                                    // word region
    }
  }
  return null;
}

// The live wavesurfer region is the source of truth: a drag updates the region
// in real time, so before ANY seek/play/edit read, copy the region's timings
// into the alignment segment (shifting its words by the same delta). This keeps
// palette clicks, prev/next and the playhead consistent after waveform edits.
function syncSegmentFromRegion(segI) {
  if (!alignment) return null;
  const seg = alignment.segments[segI];
  const r = findRegion(segI);
  if (!r) return seg;
  const ns = Math.max(0, +r.start.toFixed(3));
  const ne = Math.max(ns + 0.03, +r.end.toFixed(3));
  const ds = ns - seg.start;
  const de = ne - seg.end;
  if (Math.abs(ds) > 1e-9 || Math.abs(de) > 1e-9) {
    seg.start = ns;
    seg.end = ne;
    for (const w of seg.words) { w.start += ds; w.end += de; }
    updateLinePaletteTiming(segI);
    persistAlignment();
  }
  return seg;
}

function setLineTiming(segI, start, end) {
  if (!alignment) return;
  syncSegmentFromRegion(segI);  // base the edit on live region values
  const seg = alignment.segments[segI];
  start = Math.max(0, +start.toFixed(3));
  end = Math.max(start + 0.03, +end.toFixed(3));
  if (Math.abs(start - seg.start) < 1e-9 && Math.abs(end - seg.end) < 1e-9) return;
  pushHistory();
  const ds = start - seg.start;
  const de = end - seg.end;
  seg.start = start;
  seg.end = end;
  for (const w of seg.words) { w.start += ds; w.end += de; }
  if (wordMode) {
    // No line regions exist in word mode — move every word region instead,
    // otherwise rebuildRegion would spawn a stray line region.
    (seg.words || []).forEach((w2, wi) => rebuildWordRegion(segI, wi));
    updateLineWordTimings(segI);
  } else {
    rebuildRegion(segI);
  }
  setWhiteRegion(segI);
  updateLineWordTimings(segI);
  updateLinePaletteTiming(segI);
  persistAlignment();
}

// ---------- word-level editing (word mode) ----------
// Copy a live word-region's timings into w.start/w.end. Neighbours are the
// sequential clamp: words render as a SEQUENTIAL \kf chain in the MP4, so an
// overlap in the editor collapses there and every follower shifts (measured
// late). A word can never be dragged past its siblings' EDGES —
// start >= prev.end, end <= next.start — so the bracket order the user sees
// is exactly what the render burns. opts.history=false during bulk
// re-syncs (one history snapshot per gesture, not per word).
function syncWordFromRegion(segI, wordI, opts = {}) {
  if (!alignment) return null;
  const seg = alignment.segments[segI];
  if (!seg || !seg.words || !seg.words[wordI]) return null;
  const w = seg.words[wordI];
  const r = findRegion(segI, wordI);
  if (!r) return w;
  let ns = Math.max(0, +r.start.toFixed(3));
  let ne = Math.max(ns + 0.03, +r.end.toFixed(3));
  const prev = seg.words[wordI - 1];
  const next = seg.words[wordI + 1];
  if (prev && ns < prev.end) ns = prev.end;
  if (next) {
    if (ns > next.start - 0.03) ns = Math.max(0, next.start - 0.03);
    if (ne > next.start) ne = next.start;
  }
  if (ne < ns + 0.03) ne = ns + 0.03;
  ns = +ns.toFixed(3); ne = +ne.toFixed(3);
  if (!opts.history) {
    // bulk path: write directly
    if (Math.abs(w.start - ns) > 1e-9 || Math.abs(w.end - ne) > 1e-9) {
      w.start = ns; w.end = ne;
    }
    return w;
  }
  if (Math.abs(w.start - ns) < 1e-9 && Math.abs(w.end - ne) < 1e-9) return w;
  w.start = ns; w.end = ne;
  syncLineSpanFromWords(segI);
  updateLineWordTimings(segI);
  updateLinePaletteTiming(segI);
  persistAlignment();
  return w;
}

// The line span always follows its words in word mode: start = first word,
// end = last word. Keeps palette times, ASS \\kf and exports coherent.
function syncLineSpanFromWords(segI) {
  const seg = alignment.segments[segI];
  if (!seg || !seg.words || !seg.words.length) return;
  let s = Infinity, e = -Infinity;
  for (const w of seg.words) {
    if (w.start < s) s = w.start;
    if (w.end > e) e = w.end;
  }
  if (s !== Infinity) { seg.start = +s.toFixed(3); }
  if (e !== -Infinity) { seg.end = +e.toFixed(3); }
}

function setWordTiming(segI, wordI, start, end) {
  if (!alignment) return;
  syncWordFromRegion(segI, wordI, { history: false });
  const seg = alignment.segments[segI];
  const w = seg.words[wordI];
  start = Math.max(0, +start.toFixed(3));
  end = Math.max(start + 0.03, +end.toFixed(3));
  const prev = seg.words[wordI - 1], next = seg.words[wordI + 1];
  if (prev && start < prev.end) start = prev.end;
  if (next && end > next.start) end = next.start;
  if (end < start + 0.03) end = start + 0.03;
  if (Math.abs(start - w.start) < 1e-9 && Math.abs(end - w.end) < 1e-9) return;
  pushHistory();
  w.start = start; w.end = end;
  syncLineSpanFromWords(segI);
  rebuildWordRegion(segI, wordI);
  setWhiteRegion(segI, wordI);
  updateLineWordTimings(segI);
  updateLinePaletteTiming(segI);
  persistAlignment();
}

// Recreate one word region from its alignment value (wavesurfer v7 does not
// reliably move regions via setOptions).
function rebuildWordRegion(segI, wordI) {
  if (!regions || !wavesurfer || !alignment) return;
  const seg = alignment.segments[segI];
  if (!seg || !seg.words || !seg.words[wordI]) return;
  const old = findRegion(segI, wordI);
  if (old) regions.removeRegion(old);
  const w = seg.words[wordI];
  const region = regions.addRegion({
    start: Math.max(0, w.start),
    end: Math.max(w.start + 0.03, w.end),
    color: COLORS[segI % COLORS.length] + '70',
    drag: true, resize: true, content: '', minLength: 0.03,
  });
  region.data = { segI, wordI };
  setTimeout(() => _styleHandles(region.element, true), 0);
}

// Snap a word's bracket back onto its stored timing when they disagree.
// Drag write-backs clamp against neighbours (and the 0.03s floor) while the
// region element keeps the raw drop point — without this the bracket, the
// chip label and click-to-play permanently disagree after an illegal drop.
// No-op (no rebuild, no flicker) when they already agree within 1ms, which
// is far below a pixel at any zoom.
function snapWordRegionToData(segI, wordI) {
  if (!regions || !alignment) return;
  const seg = alignment.segments[segI];
  const w = seg && seg.words && seg.words[wordI];
  if (!w) return;
  const r = findRegion(segI, wordI);
  if (!r) return;
  if (Math.abs(r.start - w.start) > 1e-3 || Math.abs(r.end - w.end) > 1e-3) {
    rebuildWordRegion(segI, wordI);
  }
}

// Recreate the waveform region for a line from its alignment segment, so the
// on-screen region always matches the value we just wrote (r.setOptions does
// not reliably move a region in wavesurfer v7).
function rebuildRegion(segI) {
  if (!regions || !wavesurfer || !alignment) return;
  const seg = alignment.segments[segI];
  if (!seg) return;
  const old = findRegion(segI);
  if (old) {

    regions.removeRegion(old);
  }
  const baseColor = COLORS[segI % COLORS.length];
  const region = regions.addRegion({
    start: Math.max(0, seg.start),
    end: Math.max(seg.start + 0.03, seg.end),
    color: baseColor + '40',
    drag: true,
    resize: true,
    content: seg.text,
    minLength: 0.03,
  });
  region.data = { segI };   // Region class ignores `data` in options — tag it
  // Ensure handles exist and are styled for the newly created region
  setTimeout(() => _styleHandles(region.element, activeSegI === segI), 0);
}

function syncLineButtons() {
  if (!$('btn-del-line')) return;
  $('btn-del-line').disabled = !alignment || alignment.segments.length <= 1;
}

// Add a new line after the selected one. Timing starts at the current line's
// end and extends 2s (or up to the next line's start if there is one).
function addLine() {
  if (!alignment || activeSegI == null) return;
  const seg = alignment.segments[activeSegI];
  const nextStart = activeSegI + 1 < alignment.segments.length
    ? alignment.segments[activeSegI + 1].start : seg.end + 3.0;
  const newStart = Math.max(seg.end, nextStart - 2.0);
  const newEnd = Math.min(nextStart - 0.05, newStart + 2.0);
  const newSeg = {
    words: [{ text: 'New line', start: newStart, end: newEnd, probability: 1.0 }],
    start: newStart,
    end: Math.max(newEnd, newStart + 0.1),
    text: 'New line'
  };
  pushHistory();
  alignment.segments.splice(activeSegI + 1, 0, newSeg);
  buildRegions();
  buildLinePalette();
  syncLineButtons();
  selectLine(activeSegI + 1);
  persistAlignment();
}

// Delete the selected line. Refused if only one line remains.
function deleteLine() {
  if (!alignment || activeSegI == null) return;
  if (alignment.segments.length <= 1) {
    showError('Cannot delete — at least one line is required.');
    return;
  }
  pushHistory();
  alignment.segments.splice(activeSegI, 1);
  const newActive = Math.min(activeSegI, alignment.segments.length - 1);
  buildRegions();
  buildLinePalette();
  syncLineButtons();
  selectLine(newActive);
  persistAlignment();
}

// Mouse-driven only (owner decision): there are no keyboard shortcuts.
// All timing edits happen by dragging region bodies/edges; all actions have
// buttons. Text fields keep their native typing behavior (Enter commits an
// inline chip edit via that input's own handler).

// ---------- controls ----------
on('btn-play', 'click', () => {
  if (!wavesurfer) return;
  if (wavesurfer.isPlaying()) {
    wavesurfer.pause();
    $('btn-play').textContent = '\u25B6 Play';
    _cancelPreview();
  } else {
    _cancelPreview();
    wavesurfer.play();
    $('btn-play').textContent = '\u23F8 Pause';
  }
});

// Prev / Next line: jump to (and play) the previous/next line using live
// region timings (clamped to the first/last line at either end).
function stepLine(dir) {
  if (activeSegI == null || !wavesurfer || !alignment) return;
  const next = Math.max(0, Math.min(alignment.segments.length - 1, activeSegI + dir));
  selectLine(next);
}
on('btn-prev-line', 'click', () => stepLine(-1));
on('btn-next-line', 'click', () => stepLine(1));

on('btn-add-line', 'click', addLine);
on('btn-del-line', 'click', deleteLine);
on('btn-undo', 'click', undo);
on('btn-redo', 'click', redo);

// Follow-playback toggle (default OFF — the screen never jumps on its own).
// Persisted, so it stays how you left it across reloads.
const FOLLOW_KEY = 'kg_follow';
function followEnabled() {
  const el = $('follow-toggle');
  return !!(el && el.checked);
}
on('follow-toggle', 'change', () => {
  try { localStorage.setItem(FOLLOW_KEY, followEnabled() ? '1' : '0'); } catch (_) {}
  setStatus(followEnabled() ? 'Follow on — list tracks playback' : 'Follow off', '');
});

on('volume', 'input', (e) => {
  if (wavesurfer) wavesurfer.setVolume(+e.target.value / 100);
});

on('zoom', 'input', (e) => {
  if (wavesurfer) wavesurfer.zoom(+e.target.value);

});

// 2nd-voice volume: debounced re-mix so the preview level follows the slider.
// The same gain rides along with the render request (see btn-render body).
on('sv-volume', 'input', () => {
  if (_svVolumeTimer) clearTimeout(_svVolumeTimer);
  _svVolumeTimer = setTimeout(() => reloadWaveformAudio(), 150);
});

// Audition model (owner decision): the instrumental bed is ALWAYS on at a
// fixed level; Voc is a monitor-only listening aid while adjusting words
// (it never reaches the MP4); only the 2nd-voice gain below is burned into
// the render. Toggling re-mixes the waveform audio in the background; the
// old waveform keeps playing until the new mix is ready, and playback
// resumes at the same position.
on('voc-toggle', 'change', () => reloadWaveformAudio());
on('sv-toggle', 'change', () => reloadWaveformAudio());

let _waveformJobId = null;
let _audioLoadSeq = 0;  // guards against stale loads when toggling fast
let _svVolumeTimer = null;
let _waveformBlobUrl = null; // current ObjectURL for the mixed wav — revoked on next load

function setAudioLoading(visible, label) {
  const el = $('audio-loading');
  if (!el) return;
  el.hidden = !visible;
  if (visible && label) {
    const t = el.querySelector('.audio-loading-label');
    if (t) t.textContent = label;
  }
  if ($('voc-toggle')) $('voc-toggle').disabled = visible;
  if ($('sv-toggle')) $('sv-toggle').disabled = visible;
  if ($('sv-volume')) $('sv-volume').disabled = visible;
}

function svVolumeGain() {
  const v = parseFloat($('sv-volume') && $('sv-volume').value);
  return (isFinite(v) ? v : 100) / 100;
}

async function reloadWaveformAudio() {
  if (!_waveformJobId) return;
  const useVoc = !$('voc-toggle') || $('voc-toggle').checked;
  const useSV = !$('sv-toggle') || $('sv-toggle').checked;
  await loadWaveformAudio(useVoc, useSV, false);
}

function _setWaveformBlobUrl(url) {
  const prevUrl = _waveformBlobUrl;
  _waveformBlobUrl = url;
  // previous blob no longer needed after wavesurfer has fetched it (decoded
  // to AudioBuffer) — delay revoke one tick so a pending fetch can finish.
  if (prevUrl) {
    setTimeout(() => { try { URL.revokeObjectURL(prevUrl); } catch(_){} }, 1000);
  }
}

async function loadWaveformAudio(useVoc, useSV, useInst) {
  if (!_waveformJobId) return;
  const seq = ++_audioLoadSeq;
  // Timing editor is vocals-only; instrumental is NOT in preview (render bed
  // is separate). Voc is monitor, 2nd voice is burned into MP4.
  useInst = !!useInst;
  const parts = [useVoc ? 'Voc' : null, useSV ? '2nd' : null, useInst ? 'Inst' : null]
    .filter(Boolean).join('+') || 'Silence';
  setAudioLoading(true, parts + ' …');
  // Capture playback state BEFORE the swap so the reload resumes in place —
  // toggling or re-mixing then keeps playing the same spot and the wave just
  // morphs under the playhead instead of restarting from zero.
  const wasPlaying = !!(wavesurfer && wavesurfer.isPlaying());
  const resumeTime = wavesurfer ? wavesurfer.getCurrentTime() : 0;
  try {
    const vocalsUrl = '/api/vocals-raw/' + _waveformJobId;
    // Drafts run with_second_voice=False, so second_voice.wav is never written
    // for new jobs (only backing_vocals.wav from the 3-stem pass). Preview the
    // backing stem; fall back to the legacy second_voice.wav for old jobs.
    const bvUrl = '/api/backing-vocals/' + _waveformJobId;
    const svLegacyUrl = '/api/second-voice/' + _waveformJobId;
    const instUrl = '/api/instrumental/' + _waveformJobId;
    const sources = [];
    if (useVoc) {
      const v = parseFloat($('volume').value);
      sources.push({ url: vocalsUrl, gain: (isFinite(v) ? v : 100) / 100, name: 'Voc' });
    }
    if (useSV) sources.push({ urls: [bvUrl, svLegacyUrl], gain: svVolumeGain(), name: '2nd' });
    if (useInst) sources.push({ url: instUrl, gain: 1.0, name: 'Inst' });
    // One bad stem must not kill the whole editor (the old Promise.all did —
    // a single 500 left the waveform empty with zero lines). Mix whatever
    // arrives and say exactly which stem failed.
    const settled = await Promise.all(sources.map(async (src) => {
      const urls = src.urls || [src.url];
      let lastErr = null;
      for (const u of urls) {
        try {
          return { src, buf: await _getCachedAudio(u) };
        } catch (err) {
          lastErr = err;
          // 404 on the backing stem = try the legacy name; any other error
          // (or legacy 404) is the real failure.
          if (!(err && err.status === 404)) break;
        }
      }
      return { src, err: lastErr };
    }));
    if (seq !== _audioLoadSeq) return;  // a newer toggle superseded this load
    const ok = settled.filter((r) => r.buf);
    const failed = settled.filter((r) => r.err);
    if (ok.length === 0) throw (failed[0] && failed[0].err) || new Error('no audio');
    if (failed.length) {
      showError('Stem unavailable (' +
        failed.map((f) => f.src.name + ': ' + ((f.err && f.err.message) || f.err)).join('; ') +
        ') — playing the rest (auto-retried once). Toggle its checkbox off/on to retry, ' +
        'or re-run the draft. Server-side detail (if any) is in %LOCALAPPDATA%\\KaraokeGen\\proxy.log.');
    }
    const mixedBuffer = await _mixAudioBuffers(ok.map((r) => ({ buffer: r.buf, gain: r.src.gain })));
    if (seq !== _audioLoadSeq) return;
    const blob = _audioBufferToWavBlob(mixedBuffer);
    const url = URL.createObjectURL(blob);
    _setWaveformBlobUrl(url);
    initWaveSurfer(url, { resume: wasPlaying, resumeTime });
  } catch (e) {
    console.error('[waveform] failed to load stem audio:', e);
    // Total stem failure (old backend, expired stems, Modal hiccup): fall back
    // to the ORIGINAL uploaded file so the editor still opens with timing
    // regions instead of a dead empty page.
    //
    // The fallback must NEVER load an unrelated file: #audio-file holds whatever
    // the user last selected, so if they picked a different song this would draw
    // one song's waveform (and play its audio) under another song's alignment —
    // the words then look and sound completely wrong. Only accept the file when
    // it provably matches the audio this job was built from.
    const file = $('audio-file') && $('audio-file').files[0];
    const jp = currentJobId ? loadJobPayload(currentJobId) : null;
    const sameFile = !!(file && jp && jp.file_name && jp.file_size != null &&
      jp.file_name === file.name && jp.file_size === file.size);
    if (file && !sameFile) {
      console.warn('[waveform] not falling back to "' + file.name +
        '" — it is not the audio this job was built from.');
    }
    if (sameFile && seq === _audioLoadSeq) {
      try {
        const url = URL.createObjectURL(file);
        _setWaveformBlobUrl(url);
        initWaveSurfer(url, { resume: false, resumeTime: 0 });
        showError('Cloud stems unavailable (' + ((e && e.message) || e) +
          ') — playing your original audio instead. Editing works; re-run the ' +
          'draft to restore stem preview. If the server was just updated, restart it.');
        return;
      } catch (_) { /* fall through to the hard error below */ }
    }
    if (_waveformJobId) {
      showError('Could not load ' + (parts || 'audio') + ' audio (' + ((e && e.message) || e) +
        '). If the local server was just updated, restart it (uvicorn). ' +
        'For jobs created before the 2nd-voice update, run the draft again.' +
        (file && !sameFile
          ? ' Your selected file "' + file.name + '" is a different song, so it was ' +
            'NOT loaded as a fallback — re-select this job\'s audio to preview it.'
          : ''));
    } else {
      showError('Failed to load audio: ' + ((e && e.message) || e));
    }
  } finally {
    if (seq === _audioLoadSeq) setAudioLoading(false);
  }
}

function _mixAudioBuffers(sources) {
  // sources: [{ buffer, gain }] — per-source gain so the 2nd-voice preview
  // volume matches the rendered MP4 (same gain is sent to Modal).
  if (sources.length === 1) {
    const only = sources[0];
    if (Math.abs(only.gain - 1) < 1e-9) return only.buffer;
    const offline = new OfflineAudioContext(only.buffer.numberOfChannels, only.buffer.length, only.buffer.sampleRate);
    const src = offline.createBufferSource();
    const g = offline.createGain();
    g.gain.value = only.gain;
    src.buffer = only.buffer;
    src.connect(g);
    g.connect(offline.destination);
    src.start(0);
    return offline.startRendering();
  }
  const len = Math.max.apply(null, sources.map((s) => s.buffer.length));
  const rate = sources[0].buffer.sampleRate;
  const chans = sources[0].buffer.numberOfChannels;
  const offline = new OfflineAudioContext(chans, len, rate);
  for (const s of sources) {
    const src = offline.createBufferSource();
    const g = offline.createGain();
    g.gain.value = s.gain;
    src.buffer = s.buffer;
    src.connect(g);
    g.connect(offline.destination);
    src.start(0);
  }
  return offline.startRendering();
}

function _audioBufferToWavBlob(buffer) {
  const numChans = buffer.numberOfChannels;
  const len = buffer.length * numChans * 2 + 44;
  const buf = new ArrayBuffer(len);
  const view = new DataView(buf);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  view.setUint32(4, len - 8, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChans, true);
  view.setUint32(24, buffer.sampleRate, true);
  view.setUint32(28, buffer.sampleRate * numChans * 2, true);
  view.setUint16(32, numChans * 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, len - 44, true);
  const channels = [];
  for (let c = 0; c < numChans; c++) channels.push(buffer.getChannelData(c));
  let off = 44;
  for (let i = 0; i < buffer.length; i++) {
    for (let c = 0; c < numChans; c++) {
      let s = Math.max(-1, Math.min(1, channels[c][i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
      off += 2;
    }
  }
  return new Blob([buf], { type: 'audio/wav' });
}

// Stem-audio cache keys. ONE definition, shared by the reader below and the
// job-delete sweep in deleteJob(): the old delete removed 'vocals_'+id, a key
// nothing ever wrote (the reader writes the versioned url key), so cached stems
// were never freed — and a stem cached before a pipeline change kept being
// served afterwards, which makes the waveform/playback disagree with an
// alignment that was computed from the NEW stems. Bump AUDIO_CACHE_VER
// whenever stem generation changes.
const AUDIO_CACHE_VER = 'v3_';
const _STEM_URL_PREFIXES = ['/api/vocals-raw/', '/api/second-voice/', '/api/backing-vocals/', '/api/instrumental/'];
function audioCacheKey(url) { return AUDIO_CACHE_VER + url.replace(/\//g, '_'); }
function audioCacheKeysForJob(draftJobId) {
  return _STEM_URL_PREFIXES.map((p) => audioCacheKey(p + draftJobId));
}

async function _getCachedAudio(url) {
  // Check IndexedDB cache first (key = versioned url path)
  const cacheKey = audioCacheKey(url);
  const cached = await idbGet(cacheKey).catch(() => null);
  if (cached) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    return ctx.decodeAudioData(await cached.arrayBuffer());
  }
  // Fetch ONCE: snapshot into a Blob for the cache, then decode. (The old
  // code fetched every stem twice — once decoded, once for the cache.)
  // Transient Modal/volume 5xx gets ONE retry — a single blip no longer kills
  // the whole waveform. 4xx (missing stem, bad key) fails immediately.
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    const resp = await fetch(url);
    if (resp.ok) {
      const buf = await resp.arrayBuffer();
      try {
        idbPut(cacheKey, new Blob([buf])).catch(() => {});
      } catch (e) {
        // caching is best-effort
      }
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      return ctx.decodeAudioData(buf);
    }
    // Read the body ONCE as text, then try JSON — json() then text() would
    // read a consumed body and lose the server's message.
    let detail = '';
    try {
      const txt = await resp.text();
      try {
        const j = JSON.parse(txt);
        detail = ((j && (j.detail || j.error)) || '').toString() || txt.slice(0, 300);
      } catch (_) { detail = txt.slice(0, 300); }
    } catch (_) { /* body unreadable */ }
    lastErr = new Error('HTTP ' + resp.status + (detail ? ' - ' + detail.slice(0, 300) : ''));
    lastErr.status = resp.status;
    if (attempt === 0 && [500, 502, 503, 504].includes(resp.status)) {
      console.warn('[audio] ' + url + ' -> ' + lastErr.message + ' — retrying once...');
      await new Promise((r) => setTimeout(r, 1500));
      continue;
    }
    throw lastErr;
  }
  throw lastErr;
}
on('btn-new-side', 'click', startNew);

on('btn-help', 'click', () => { $('help-overlay').hidden = false; });
on('help-close', 'click', () => { $('help-overlay').hidden = true; });
document.addEventListener('DOMContentLoaded', refreshConfigUI);
document.addEventListener('DOMContentLoaded', restoreSongStart);
on('help-overlay', 'click', (e) => { if (e.target === $('help-overlay')) $('help-overlay').hidden = true; });

on('btn-stop-modal', 'click', async () => {
  if (draftXhr) {
    draftXhr.abort();
    draftXhr = null;
    showError('Draft submission cancelled.');
    setStatus('error', 'err');
    finishDraftSubmitUI();
    setDraftFormVisible(true);
    syncDraftButtons();
    stopElapsed();
    return;
  }
  if (!callId) {
    showError('No active Modal job to stop.');
    return;
  }
  setStatus('Stopping Modal job...', 'busy');
  try {
    await fetch('/api/cancel/' + callId, { method: 'POST' });
  } catch (e) { /* ignore */ }
  if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
  stopElapsed();
  localStorage.removeItem('kg_call_id');
  callId = null;
  setDraftFormVisible(true);
  syncDraftButtons();
  setStatus('Modal job stopped');
  $('progress-bar').hidden = true;
});

// ---------- step 3: render (single button, per-word fill) ----------
async function startRender() {
  clearError();
  if (!alignment || !draftJobId) { showError('No draft to render.'); return; }
  // CRITICAL: Sync all edited timings from waveform regions to alignment
  // before sending to Modal. Without this, the render uses stale timings.
  persistAllFromRegions();
  setStatus('Rendering on Modal (per-word karaoke)...', 'busy');
  $('btn-render').disabled = true;
  $('btn-download').disabled = true;
  const body = {
    draft_job_id: draftJobId,
    alignment: alignment,
    final_lyrics: alignment.segments.map((s) => s.text).join('\n'),
    bg_color: $('bg-color').value,
    output_name: draftFileName || draftJobId || 'karaoke',
    second_voice: !!$('sv-toggle') && $('sv-toggle').checked,
    second_voice_gain: svVolumeGain(),
  };
  try {
    const r = await fetch('/api/render-word', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    pollJob(data.job_id, draftJobId, onRenderDone, 'render');
  } catch (e) {
    showError('Render submit failed: ' + e.message);
    setStatus('error', 'err');
    $('btn-render').disabled = false;
  }
}

on('btn-render', 'click', () => startRender());

on('btn-download', 'click', () => {
  if (renderDownloadUrl) downloadUrlWithSaveDialog(renderDownloadUrl, renderDownloadName);
});
// Lyric-video mode link: same Save-As routing, else the webview navigates away.
on('video-download-link', 'click', (e) => {
  const dl = $('video-download-link');
  const href = dl && dl.getAttribute('href');
  if (!href || href === '#') return;
  if (window.pywebview && window.pywebview.api && window.pywebview.api.saveFile) {
    e.preventDefault();
    downloadUrlWithSaveDialog(href, 'karaoke_video.mp4');
  }
});

// ---------- word mode (permanent) ----------
function applyWordModeUI() {
  // No toggle anymore: word mode is always on. Keep the body class so the
  // word-first CSS applies, and rebuild regions/strip when audio is loaded.
  document.body.classList.add('word-mode');
  if (alignment && regions && wavesurfer) {
    buildRegions();
    restoreActiveWord(activeSegI);
    setWhiteRegion(activeSegI, activeWordI);
  }
}

function onRenderDone(result) {
  setStatus('Render complete');
  $('btn-render').disabled = false;
  $('render-progress').hidden = true;
  renderDownloadUrl = '/api/file/' + result.file_id;
  renderDownloadName = result.filename || 'karaoke.mp4';
  $('btn-download').disabled = false;
  $('btn-download').textContent = '\u2B07 Download ' + renderDownloadName;
}

// ---------- debug helper (browser console: __dbg()) ----------
function __dbg() {
  if (!regions) { alert('no regions'); return; }
  const r = regions.getRegions()[0];
  if (!r) { alert('no region[0]'); return; }
  const el = r.element;
  const info = [
    'tag: ' + el?.tagName,
    'class: ' + el?.className,
    'children: ' + (el?.children.length || 0),
    'handle elems: ' + (el?.querySelectorAll('[class*="handle"]').length || 0),
    'HTML: ' + (el?.outerHTML?.substring(0, 300) || 'null'),
  ].join('\n');
  console.log(info);
  alert(info);
}
window.__dbg = __dbg;

// ---------- init ----------
try {
  const fv = localStorage.getItem(FOLLOW_KEY);
  if ($('follow-toggle') && fv === '1') $('follow-toggle').checked = true;
} catch (_) {}
renderJobList();
checkResumable();
applyWordModeUI();
