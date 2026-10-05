const $ = (id) => document.getElementById(id);
const stages = ['converting', 'separating', 'transcribing', 'engraving'];
const labels = { queued: '대기', running: '분석 중', completed: '완료', failed: '실패', cancelled: '취소' };
const kit = [[42, 'Hi-hat'], [49, 'Cymbal'], [47, 'Tom'], [38, 'Snare'], [35, 'Kick']];
let selectedFile = null, currentId = null, currentJob = null, system = null;
let timer = null, resultKey = null, draftEvents = [], editCount = 0, uploading = false;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { detail = `${response.status} ${response.statusText}`; }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return response.status === 204 ? null : response.json();
}
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function busy() { return uploading || ['queued', 'running'].includes(currentJob?.status); }
function updateStart() { $('start').disabled = !selectedFile || busy() || !system?.ffmpeg; }
function chooseFile(file) {
  if (!file) return;
  if (system && file.size > system.max_upload_bytes) { notice('파일 크기 제한을 초과했습니다.'); return; }
  selectedFile = file;
  $('filename').textContent = file.name;
  $('filesize').textContent = `${(file.size / 1024 ** 2).toFixed(1)} MB · ${file.type || '미디어 파일'}`;
  $('file-card').hidden = false;
  notice(); updateStart();
}
function readOptions(form) {
  const values = Object.fromEntries(new FormData(form));
  for (const field of ['bpm', 'offset']) values[field] = values[field] === '' ? null : Number(values[field]);
  if ('sensitivity' in values) values.sensitivity = Number(values.sensitivity);
  if (values.meter === '7/8' && values.grid === 'triplet') throw new Error('7/8 박자에서는 8분 또는 16분음표 격자를 선택하세요.');
  return values;
}
function fileUrl(name, download = false) { return `/api/jobs/${currentId}/files/${encodeURIComponent(name)}${download ? '?download=true' : ''}`; }

$('dropzone').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', (event) => chooseFile(event.target.files[0]));
for (const event of ['dragenter', 'dragover']) $('dropzone').addEventListener(event, (e) => { e.preventDefault(); $('dropzone').classList.add('dragging'); });
for (const event of ['dragleave', 'drop']) $('dropzone').addEventListener(event, (e) => { e.preventDefault(); $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', (event) => {
  if (event.dataTransfer.files.length > 1) { notice('한 번에 하나의 파일을 선택하세요.'); return; }
  chooseFile(event.dataTransfer.files[0]);
});
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => event.preventDefault());
$('remove-file').addEventListener('click', () => { selectedFile = null; $('file').value = ''; $('file-card').hidden = true; updateStart(); });
$('options-form').addEventListener('submit', (event) => event.preventDefault());
$('new-job').addEventListener('click', () => {
  clearTimeout(timer); currentId = null; currentJob = null; resultKey = null;
  $('result').hidden = true; $('player').pause(); notice(); renderProgress(null); refreshHistory(); updateStart();
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

$('start').addEventListener('click', async () => {
  if (!selectedFile || busy() || !$('options-form').reportValidity()) return;
  let options;
  try { options = readOptions($('options-form')); } catch (error) { notice(error.message); return; }
  uploading = true; updateStart(); notice();
  const form = new FormData(); form.append('file', selectedFile); form.append('options', JSON.stringify(options));
  $('progress-message').textContent = '파일을 로컬 호스트에 전송합니다.';
  try {
    const job = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); xhr.open('POST', '/api/jobs');
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) $('progress-message').textContent = `파일 전송 · ${Math.round(event.loaded / event.total * 100)}%`;
      };
      xhr.onload = () => {
        let body; try { body = JSON.parse(xhr.responseText); } catch { reject(new Error('서버 응답을 읽지 못했습니다.')); return; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)));
      };
      xhr.onerror = () => reject(new Error('서버 연결에 실패했습니다.'));
      xhr.send(form);
    });
    clearTimeout(timer); currentId = job.id; currentJob = job; resultKey = null;
    $('result').hidden = true; await showJob(job); refreshHistory(); schedulePoll();
  } catch (error) { notice(error.message); }
  finally { uploading = false; updateStart(); }
});

async function refreshHistory() {
  try {
    const jobs = await api('/api/jobs'); $('history-count').textContent = jobs.length;
    const history = $('history'); history.replaceChildren();
    if (!jobs.length) { const empty = document.createElement('p'); empty.className = 'empty-history'; empty.textContent = '첫 번째 악보를 만들어 보세요.'; history.append(empty); }
    for (const job of jobs) {
      const button = document.createElement('button'); button.className = `history-item${job.id === currentId ? ' active' : ''}`;
      const title = document.createElement('strong'); title.textContent = job.filename;
      const detail = document.createElement('small'); detail.textContent = `${labels[job.status]} · ${new Date(job.created * 1000).toLocaleDateString('ko-KR')}`;
      button.append(title, detail);
      button.addEventListener('click', async () => {
        try {
          clearTimeout(timer); currentId = job.id; resultKey = null; notice();
          await showJob(await api(`/api/jobs/${currentId}`)); refreshHistory(); schedulePoll();
        } catch (error) { notice(error.message); }
      }); history.append(button);
    }
  } catch (error) { notice(error.message); }
}
function schedulePoll() {
  clearTimeout(timer);
  if (!busy() || !currentId) return;
  const id = currentId;
  timer = setTimeout(async () => {
    try {
      const job = await api(`/api/jobs/${id}`);
      if (currentId !== id) return;
      await showJob(job);
      if (!busy()) refreshHistory();
    } catch (error) { if (currentId === id) notice(error.message); }
    if (currentId === id) schedulePoll();
  }, 1200);
}
async function showJob(job) {
  currentJob = job; renderProgress(job); updateStart();
  if (job.status === 'failed') notice(job.error || '작업에 실패했습니다. 작업 로그를 확인하세요.');
  if (job.result && resultKey !== `${job.id}:${job.result.generation}`) {
    resultKey = `${job.id}:${job.result.generation}`; await renderResult(job);
  } else if (!job.result) $('result').hidden = true;
  else if (!busy()) renderEditor();
}
function renderProgress(job) {
  const progress = job?.progress || { progress: 0, stage: 'queued', message: '파일을 선택하면 분석을 시작할 수 있습니다.' };
  let index = stages.indexOf(progress.stage);
  if (progress.stage === 'rhythm') index = 2;
  if (job?.status === 'completed') index = 4;
  document.querySelectorAll('.pipeline-step').forEach((element, i) => {
    element.classList.toggle('done', i < index);
    element.classList.toggle('active', job?.status === 'running' && i === index);
    element.querySelector('.stage-check').textContent = i < index ? '✓' : '';
  });
  $('job-status').textContent = job ? labels[job.status] : '준비';
  $('job-status').className = `status-tag ${job?.status || ''}`;
  const fraction = job?.status === 'completed' ? 1 : progress.progress;
  $('progress-bar').style.width = `${fraction * 100}%`; $('progress-percent').textContent = `${Math.round(fraction * 100)}%`;
  $('progress-message').textContent = job?.status === 'cancelled' ? '분석이 취소되었습니다.' : progress.message;
  $('elapsed').textContent = progress.elapsed_seconds ? `최근 실행 ${progress.elapsed_seconds.toFixed(0)}초 · 재시도는 저장된 분석을 재사용할 수 있습니다.` : '첫 실행에는 모델 다운로드가 필요합니다.';
  $('cancel').hidden = !['running', 'queued'].includes(job?.status);
  $('retry').hidden = !['failed', 'cancelled'].includes(job?.status);
  $('log-link').hidden = !job; $('log-link').href = job ? `/api/jobs/${job.id}/log` : '#';
  $('delete-job').hidden = !job || ['running', 'queued'].includes(job.status);
  $('revision-form').querySelector('button').disabled = ['running', 'queued'].includes(job?.status);
}
for (const action of ['cancel', 'retry']) $(action).addEventListener('click', async () => {
  if (!currentId) return;
  try { notice(); await showJob(await api(`/api/jobs/${currentId}/${action}`, { method: 'POST' })); refreshHistory(); schedulePoll(); }
  catch (error) { notice(error.message); }
});
$('delete-job').addEventListener('click', async () => {
  if (!currentId) return;
  try {
    await api(`/api/jobs/${currentId}`, { method: 'DELETE' }); currentId = null; currentJob = null; resultKey = null;
    $('result').hidden = true; $('player').pause(); renderProgress(null); refreshHistory(); updateStart(); notice();
  } catch (error) { notice(error.message); }
});

async function renderResult(job) {
  const result = job.result; const grid = result.grid;
  $('result').hidden = false; $('result-title').textContent = job.filename.replace(/\.[^.]+$/, '');
  $('result-meta').textContent = `${grid.bpm.toFixed(1)} BPM · ${grid.meter} · ${grid.bars}마디 · ${result.event_count} 타격 · ${result.renderer === 'musescore' ? 'MuseScore' : 'Verovio'} · ${result.transcription_device.toUpperCase()}`;
  $('counts').replaceChildren();
  for (const [name, count] of Object.entries(result.counts)) { const pill = document.createElement('span'); pill.className = 'count-pill'; const number = document.createElement('b'); number.textContent = count; pill.append(document.createTextNode(name), number); $('counts').append(pill); }
  for (const [id, name] of [['download-bundle', 'score-bundle.zip'], ['download-xml', 'score.musicxml'], ['download-midi', 'score.mid'], ['download-pdf', 'score.pdf']]) $(id).href = fileUrl(name, true);
  $('download-pdf').hidden = !result.pdf;
  $('audio-source').value = 'drums.wav'; $('player').src = fileUrl('drums.wav');
  $('warnings').replaceChildren();
  for (const warning of result.warnings) { const p = document.createElement('p'); p.textContent = `↳ ${warning}`; $('warnings').append(p); }
  for (const key of ['bpm', 'offset', 'meter', 'grid']) $('revision-form').elements[key].value = result.options[key];
  $('editor-bar').max = grid.bars; $('editor-bar').value = 1;
  const events = await api(`/api/jobs/${job.id}/files/events.json`);
  if (currentId !== job.id || resultKey !== `${job.id}:${result.generation}`) return;
  draftEvents = events; editCount = 0; renderEditor();
  $('score-pages').replaceChildren();
  for (const [index, page] of result.pages.entries()) {
    const wrapper = document.createElement('div'); wrapper.className = 'score-page';
    const image = document.createElement('img'); image.src = fileUrl(page) + `?v=${encodeURIComponent(result.generation)}`; image.alt = `${job.filename} 드럼 악보 ${index + 1}페이지`;
    const caption = document.createElement('div'); caption.className = 'page-caption';
    const link = document.createElement('a'); link.href = fileUrl(page, true); link.textContent = 'SVG ↓';
    caption.append(document.createTextNode(`PAGE ${String(index + 1).padStart(2, '0')} / ${String(result.pages.length).padStart(2, '0')}`), link);
    wrapper.append(image, caption); $('score-pages').append(wrapper);
  }
}
$('audio-source').addEventListener('change', () => { const position = $('player').currentTime; $('player').src = fileUrl($('audio-source').value); $('player').addEventListener('loadedmetadata', () => { $('player').currentTime = position; }, { once: true }); });
$('print').addEventListener('click', async () => {
  try { await Promise.all([...$('score-pages').querySelectorAll('img')].map((image) => image.decode())); window.print(); }
  catch { notice('SVG 페이지 로딩을 기다린 후 다시 인쇄하세요.'); }
});

function eventTick(event, grid) { return Math.max(0, Math.floor((event.time - grid.offset) * grid.bpm / 60 * 12 / grid.step + 0.5) * grid.step); }
function renderEditor() {
  if (!currentJob?.result) return;
  const grid = currentJob.result.grid;
  const bar = Math.max(1, Math.min(grid.bars, Number($('editor-bar').value) || 1)); $('editor-bar').value = bar;
  const cells = grid.bar_ticks / grid.step; const offset = (bar - 1) * grid.bar_ticks;
  const container = document.createElement('div'); container.className = 'editor-grid'; container.style.gridTemplateColumns = `65px repeat(${cells}, minmax(22px, 1fr))`;
  container.append(document.createElement('span'));
  for (let i = 0; i < cells; i++) { const label = document.createElement('span'); label.className = 'editor-position'; label.textContent = i % (12 / grid.step) === 0 ? String(i / (12 / grid.step) + 1) : '·'; container.append(label); }
  for (const [pitch, name] of kit) {
    const label = document.createElement('span'); label.className = 'editor-label'; label.textContent = name; container.append(label);
    for (let i = 0; i < cells; i++) {
      const tick = offset + i * grid.step;
      const on = draftEvents.some((e) => e.pitch === pitch && eventTick(e, grid) === tick);
      const button = document.createElement('button'); button.type = 'button'; button.className = `editor-cell${on ? ' on' : ''}${tick % 12 === 0 ? ' beat-start' : ''}`; button.textContent = on ? '●' : '';
      button.setAttribute('aria-pressed', String(on)); button.setAttribute('aria-label', `${bar}마디 ${i + 1}번째 격자 ${name}`);
      button.disabled = busy();
      button.addEventListener('click', () => {
        if (on) draftEvents = draftEvents.filter((e) => e.pitch !== pitch || eventTick(e, grid) !== tick);
        else {
          const time = grid.offset + tick / 12 * 60 / grid.bpm;
          if (time >= currentJob.result.duration) { notice('오디오 끝 이후에는 타격을 추가할 수 없습니다.'); return; }
          draftEvents.push({ time, pitch, strength: 0.8 });
        }
        editCount++; renderEditor();
      }); container.append(button);
    }
  }
  $('event-editor').replaceChildren(container);
  $('edit-count').textContent = editCount ? `${editCount}회 수정 · 다시 생성하면 저장됩니다.` : '타격을 클릭해서 추가하거나 제거하세요.';
}
$('editor-bar').addEventListener('change', renderEditor);
$('reset-edits').addEventListener('click', async () => { try { draftEvents = await api(fileUrl('events.json')); editCount = 0; renderEditor(); } catch (error) { notice(error.message); } });
$('revision-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (busy() || !currentId || !$('revision-form').reportValidity()) return;
  try {
    const options = { ...currentJob.result.options, ...readOptions($('revision-form')) };
    notice(); await showJob(await api(`/api/jobs/${currentId}/score`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ options, events: editCount ? draftEvents : null }) }));
    renderEditor(); refreshHistory(); schedulePoll();
  } catch (error) { notice(error.message); }
});

async function init() {
  try {
    system = await api('/api/system');
    $('hardware').textContent = `${system.default_device.toUpperCase()} · ${system.musescore ? 'MuseScore' : 'Verovio'} · LOCAL`;
    for (const device of system.devices.filter((d) => d !== 'cpu')) { const option = document.createElement('option'); option.value = device; option.textContent = device.toUpperCase(); $('device').append(option); }
    $('limits').textContent = `최대 ${(system.max_upload_bytes / 1024 ** 3).toFixed(1)} GB · ${system.max_duration_seconds / 60}분 · 파일은 외부 서버에 업로드되지 않습니다.`;
    if (!system.ffmpeg) notice('FFmpeg/ffprobe가 없습니다. README의 설치 안내를 확인하세요.');
    updateStart(); await refreshHistory();
  } catch (error) { $('hardware').textContent = '연결 실패'; notice(`서버를 확인하세요. ${error.message}`); }
}
init();
