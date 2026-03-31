/* ═══════════════════════════════════════════════════════════════════
   EthOS  —  Surveillance Station
   Camera discovery, live view, recording & playback
   ═══════════════════════════════════════════════════════════════════ */
AppRegistry['surveillance'] = function (appDef, launchOpts) {
  createWindow('surveillance', {
    title: 'Surveillance Station',
    icon: 'fa-solid fa-video',
    iconColor: '#dc2626',
    width: 1300,
    height: 860,
    onRender: body => _survInit(body),
  });
};

/* ━━━━  STATE  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
const SURV = {
  root: null,
  tab: 'live',
  cameras: [],
  settings: {},
  hlsPlayers: {},
};

/* ━━━━  INIT  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _survInit(body) {
  SURV.root = body;
  body.style.display = 'flex';
  body.style.flexDirection = 'column';
  body.style.overflow = 'hidden';
  body.style.background = 'var(--bg-primary)';

  // Check installation status
  let status;
  try {
    status = await api('/surveillance/status');
  } catch (e) {
    status = { installed: false, deps: {} };
  }

  if (!status.installed) {
    _survRenderInstall(body, status);
    return;
  }

  _survRenderApp(body);
}

/* ━━━━  AUTO-DISCOVERY NOTIFICATION LISTENER  ━━━━━━━━━━━━━━━━━ */
(function _survAutoDiscoveryListener() {
  // Attach once — works even before app is opened
  if (NAS.socket && !window._survAutoDiscListenerAttached) {
    window._survAutoDiscListenerAttached = true;
    NAS.socket.on('surveillance_new_cameras', (data) => {
      if (!data.count) return;
      const names = (data.cameras || []).map(c => c.name || c.ip).join(', ');
      toast(`Wykryto ${data.count} now${data.count === 1 ? t('ą kamerę') : 'e kamery'} w sieci: ${names}`, 'info', 8000);
    });
    NAS.socket.on('surveillance_motion', (data) => {
      toast(`🔴 Ruch wykryty: ${data.camera_name || 'Kamera'}`, 'warning', 5000);
    });
  }
})();

/* ━━━━  INSTALL SCREEN  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _survRenderInstall(body, status) {
  body.innerHTML = `
    <div class="surv-install-center">
      <i class="fas fa-video surv-install-icon"></i>
      <h2 class="surv-install-title">Surveillance Station</h2>
      <p class="surv-install-desc">
        ${t('System monitoringu wymaga zainstalowania dodatkowych komponentów (ffmpeg, python-onvif).')}
      </p>
      <div class="surv-deps-row">
        <span class="surv-dep ${status.deps?.ffmpeg ? 'ok' : 'missing'}">
          <i class="fas ${status.deps?.ffmpeg ? 'fa-check-circle' : 'fa-times-circle'}"></i> ffmpeg
        </span>
        <span class="surv-dep ${status.deps?.ffprobe ? 'ok' : 'missing'}">
          <i class="fas ${status.deps?.ffprobe ? 'fa-check-circle' : 'fa-times-circle'}"></i> ffprobe
        </span>
        <span class="surv-dep ${status.deps?.onvif ? 'ok' : 'missing'}">
          <i class="fas ${status.deps?.onvif ? 'fa-check-circle' : 'fa-times-circle'}"></i> python-onvif (opcjonalny)
        </span>
      </div>
      <button class="btn btn-primary surv-install-btn" id="surv-install-btn">
        <i class="fas fa-download"></i> Zainstaluj teraz
      </button>
      <div class="surv-install-progress-wrap" id="surv-install-progress" style="display:none;">
        <div class="surv-progress-bar"><div class="surv-progress-fill" id="surv-progress-fill"></div></div>
        <div class="surv-install-msg" id="surv-install-msg"></div>
      </div>
    </div>
  `;

  // Style injection (once)
  _survInjectStyles();

  body.querySelector('#surv-install-btn').addEventListener('click', async () => {
    const btn = body.querySelector('#surv-install-btn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Instalowanie…';
    body.querySelector('#surv-install-progress').style.display = 'block';

    try {
      const r = await api('/surveillance/install', { method: 'POST' });
      // Listen for progress via SocketIO
      if (NAS.socket) {
        NAS.socket.on('surveillance_install', (data) => {
          const fill = body.querySelector('#surv-progress-fill');
          const msg = body.querySelector('#surv-install-msg');
          if (fill) fill.style.width = (data.percent || 0) + '%';
          if (msg) msg.textContent = data.message || '';
          if (data.stage === 'done') {
            toast('Surveillance Station zainstalowane!', 'success');
            setTimeout(() => _survInit(body), 1500);
          }
          if (data.stage === 'error') {
            toast(t('Błąd instalacji: ') + data.message, 'error');
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-download"></i> ${t('Spróbuj ponownie')}`;
          }
        });
      }
    } catch (e) {
      toast(t('Błąd: ') + e.message, 'error');
      btn.disabled = false;
      btn.innerHTML = `<i class="fas fa-download"></i> ${t('Spróbuj ponownie')}`;
    }
  });
}

/* ━━━━  MAIN APP LAYOUT  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _survRenderApp(body) {
  body.innerHTML = `
    <div class="surv-topbar">
      <div class="surv-tabs">
        <button class="surv-tab active" data-tab="live"><i class="fas fa-tv"></i> ${t('Podgląd na żywo')}</button>
        <button class="surv-tab" data-tab="cameras"><i class="fas fa-camera"></i> Kamery</button>
        <button class="surv-tab" data-tab="recordings"><i class="fas fa-film"></i> Nagrania</button>
        <button class="surv-tab" data-tab="settings"><i class="fas fa-cog"></i> Ustawienia</button>
      </div>
      <div class="surv-topbar-right">
        <span class="surv-rec-indicator" id="surv-rec-indicator" style="display:none;">
          <span class="surv-rec-dot"></span> REC
        </span>
      </div>
    </div>
    <div class="surv-content" id="surv-content"></div>
  `;

  _survInjectStyles();

  // Tabs
  body.querySelectorAll('.surv-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      body.querySelectorAll('.surv-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      SURV.tab = btn.dataset.tab;
      _survRenderTab();
    });
  });

  _survLoadData().then(() => _survRenderTab());
}

async function _survLoadData() {
  try {
    const [cams, settings] = await Promise.all([
      api('/surveillance/cameras'),
      api('/surveillance/settings'),
    ]);
    SURV.cameras = cams || [];
    SURV.settings = settings || {};
  } catch (e) {
    SURV.cameras = [];
    SURV.settings = {};
  }
  // Update REC indicator
  const ind = SURV.root.querySelector('#surv-rec-indicator');
  if (ind) ind.style.display = SURV.settings.recording_enabled ? 'flex' : 'none';
}

function _survRenderTab() {
  const content = SURV.root.querySelector('#surv-content');
  if (!content) return;
  // Stop any active HLS players first
  _survStopAllPlayers();
  switch (SURV.tab) {
    case 'live': _survRenderLive(content); break;
    case 'cameras': _survRenderCameras(content); break;
    case 'recordings': _survRenderRecordings(content); break;
    case 'settings': _survRenderSettings(content); break;
  }
}

/* ━━━━  LIVE VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _survRenderLive(el) {
  const cams = SURV.cameras.filter(c => c.enabled);
  if (!cams.length) {
    el.innerHTML = `
      <div class="surv-empty">
        <i class="fas fa-video-slash"></i>
        <p>${t('Brak kamer. Dodaj kamery w zakładce')} <strong>${t('Kamery')}</strong>.</p>
      </div>`;
    return;
  }

  // Grid layout: auto cols based on camera count
  const cols = cams.length <= 1 ? 1 : cams.length <= 4 ? 2 : cams.length <= 9 ? 3 : 4;
  el.innerHTML = `
    <div class="surv-live-grid" style="grid-template-columns:repeat(${cols},1fr);">
      ${cams.map(c => `
        <div class="surv-live-cell" data-cam="${c.id}">
          <div class="surv-live-header">
            <span class="surv-live-name"><i class="fas fa-circle surv-live-dot"></i> ${_survEsc(c.name)}</span>
            <div class="surv-live-actions">
              <button class="surv-live-btn surv-live-snap" data-cam="${c.id}" title="Zrzut ekranu"><i class="fas fa-camera"></i></button>
              <button class="surv-live-btn surv-live-fullscreen" data-cam="${c.id}" title="${t('Pełny ekran')}"><i class="fas fa-expand"></i></button>
            </div>
          </div>
          <div class="surv-live-video" id="surv-video-${c.id}">
            <div class="surv-live-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Łączenie…')}</div>
          </div>
        </div>
      `).join('')}
    </div>
  `;

  // Start streams
  for (const c of cams) {
    _survStartLiveStream(c);
  }

  // Snapshot buttons
  el.querySelectorAll('.surv-live-snap').forEach(btn => {
    btn.addEventListener('click', async () => {
      const camId = btn.dataset.cam;
      try {
        const resp = await fetch(`/api/surveillance/cameras/${camId}/snapshot`, {
          headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('token') || '') }
        });
        if (!resp.ok) throw new Error(t('Błąd'));
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `snapshot_${camId}_${Date.now()}.jpg`;
        a.click();
        URL.revokeObjectURL(url);
        toast('Zrzut ekranu pobrany', 'success');
      } catch (e) {
        toast(t('Nie udało się pobrać zrzutu'), 'error');
      }
    });
  });

  // Fullscreen buttons
  el.querySelectorAll('.surv-live-fullscreen').forEach(btn => {
    btn.addEventListener('click', () => {
      const cell = btn.closest('.surv-live-cell');
      if (cell) {
        if (document.fullscreenElement) document.exitFullscreen();
        else cell.requestFullscreen();
      }
    });
  });
}

async function _survStartLiveStream(cam) {
  const container = document.getElementById(`surv-video-${cam.id}`);
  if (!container) return;

  try {
    const r = await api(`/surveillance/stream/${cam.id}/start`, { method: 'POST' });
    if (!r.ok && r.error) {
      container.innerHTML = `<div class="surv-live-error"><i class="fas fa-exclamation-triangle"></i> ${r.error}</div>`;
      return;
    }

    // Wait a moment for HLS segments to be created
    await new Promise(res => setTimeout(res, 3000));

    const hlsUrl = `/api/surveillance/stream/${cam.id}/hls/stream.m3u8`;

    // Create video element
    const video = document.createElement('video');
    video.className = 'surv-video-el';
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    video.controls = false;
    container.innerHTML = '';
    container.appendChild(video);

    // Try HLS.js if available, else native
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({ liveDurationInfinity: true, enableWorker: true });
      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.ERROR, (e, data) => {
        if (data.fatal) {
          container.innerHTML = `<div class="surv-live-error"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd strumienia')}</div>`;
        }
      });
      SURV.hlsPlayers[cam.id] = hls;
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = hlsUrl;
    } else {
      // Fallback: show snapshot refreshing
      container.innerHTML = `<img class="surv-video-el" src="/api/surveillance/cameras/${cam.id}/snapshot?t=${Date.now()}" alt="snapshot">`;
      SURV.hlsPlayers[cam.id] = setInterval(() => {
        const img = container.querySelector('img');
        if (img) img.src = `/api/surveillance/cameras/${cam.id}/snapshot?t=${Date.now()}`;
      }, 2000);
    }

    // Add unmute button overlay
    const unmute = document.createElement('button');
    unmute.className = 'surv-unmute-btn';
    unmute.innerHTML = '<i class="fas fa-volume-mute"></i>';
    unmute.title = t('Włącz dźwięk');
    container.appendChild(unmute);
    unmute.addEventListener('click', () => {
      video.muted = !video.muted;
      unmute.innerHTML = video.muted ? '<i class="fas fa-volume-mute"></i>' : '<i class="fas fa-volume-up"></i>';
    });
  } catch (e) {
    container.innerHTML = `<div class="surv-live-error"><i class="fas fa-exclamation-triangle"></i> ${e.message || t('Błąd')}</div>`;
  }
}

function _survStopAllPlayers() {
  for (const [camId, player] of Object.entries(SURV.hlsPlayers)) {
    if (player && typeof player.destroy === 'function') {
      player.destroy();
    } else if (typeof player === 'number') {
      clearInterval(player);
    }
  }
  SURV.hlsPlayers = {};
}

/* ━━━━  CAMERAS TAB  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _survRenderCameras(el) {
  await _survLoadData();
  const cams = SURV.cameras;

  el.innerHTML = `
    <div class="surv-cameras-toolbar">
      <button class="btn btn-primary" id="surv-add-cam"><i class="fas fa-plus"></i> ${t('Dodaj kamerę')}</button>
      <button class="btn" id="surv-discover"><i class="fas fa-search"></i> Wyszukaj kamery w sieci</button>
    </div>
    <div class="surv-cameras-list" id="surv-cameras-list">
      ${cams.length === 0 ? `<div class="surv-empty"><i class="fas fa-camera"></i><p>${t('Brak kamer. Kliknij "Dodaj kamerę" lub "Wyszukaj" aby rozpocząć.')}</p></div>` : ''}
      ${cams.map(c => `
        <div class="surv-cam-card">
          <div class="surv-cam-thumb" id="surv-thumb-${c.id}">
            <img src="/api/surveillance/cameras/${c.id}/snapshot?t=${Date.now()}" alt="" onerror="this.style.display='none'">
            <div class="surv-cam-thumb-overlay">
              <i class="fas fa-video"></i>
            </div>
          </div>
          <div class="surv-cam-info">
            <div class="surv-cam-name">${_survEsc(c.name)}</div>
            <div class="surv-cam-url">${_survEsc(c.url)}</div>
            <div class="surv-cam-badges">
              ${c.enabled ? '<span class="surv-badge ok"><i class="fas fa-check"></i> Aktywna</span>' : `<span class="surv-badge off"><i class="fas fa-pause"></i> ${t('Wyłączona')}</span>`}
              ${c.record ? '<span class="surv-badge rec"><i class="fas fa-circle"></i> Nagrywanie</span>' : ''}
              ${c.record ? (c.recording_mode === 'events' ? '<span class="surv-badge event-mode"><i class="fas fa-bolt"></i> Zdarzenia</span>' : `<span class="surv-badge cont-mode"><i class="fas fa-video"></i> ${t('Ciągłe')}</span>`) : ''}
              ${c.streaming ? '<span class="surv-badge live"><i class="fas fa-broadcast-tower"></i> Live</span>' : ''}
              ${c.onvif_host ? '<span class="surv-badge onvif">ONVIF</span>' : ''}
              ${c.last_error ? '<span class="surv-badge error" title="' + _survEsc(c.last_error) + `"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd')}</span>` : ''}
              ${c.retries >= 10 ? '<span class="surv-badge off"><i class="fas fa-ban"></i> Wstrzymano</span>' : ''}
            </div>
            ${c.last_error ? '<div class="surv-cam-error"><i class="fas fa-exclamation-circle"></i> ' + _survEsc(c.last_error.substring(0, 200)) + (c.last_error_time ? ' <small>(' + new Date(c.last_error_time).toLocaleTimeString('pl') + ')</small>' : '') + '</div>' : ''}
          </div>
          <div class="surv-cam-actions">
            ${c.last_error || !c.streaming ? '<button class="surv-cam-btn" data-action="diagnose" data-cam="' + c.id + '" title="Diagnostyka"><i class="fas fa-stethoscope"></i></button>' : ''}
            ${c.retries >= 10 ? '<button class="surv-cam-btn" data-action="retry" data-cam="' + c.id + '" title="' + t('Ponów próby') + '"><i class="fas fa-redo"></i></button>' : ''}
            <button class="surv-cam-btn" data-action="edit" data-cam="${c.id}" title="Edytuj"><i class="fas fa-edit"></i></button>
            <button class="surv-cam-btn danger" data-action="delete" data-cam="${c.id}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
          </div>
        </div>
      `).join('')}
    </div>
    <div id="surv-discover-results" style="display:none;"></div>
  `;

  // Add camera
  el.querySelector('#surv-add-cam').addEventListener('click', () => _survShowCameraDialog());

  // Discover
  el.querySelector('#surv-discover').addEventListener('click', () => _survDiscoverCameras(el));

  // Edit / Delete / Diagnose / Retry
  el.querySelectorAll('.surv-cam-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const camId = btn.dataset.cam;
      const cam = SURV.cameras.find(c => c.id === camId);
      if (btn.dataset.action === 'edit' && cam) {
        _survShowCameraDialog(cam);
      } else if (btn.dataset.action === 'delete') {
        if (!await confirmDialog(`${t('Usunąć kamerę')} "${cam?.name || camId}"?`)) return;
        try {
          await api(`/surveillance/cameras/${camId}`, { method: 'DELETE' });
          toast(t('Kamera usunięta'), 'success');
          _survRenderCameras(el);
        } catch (e) { toast(t('Błąd usuwania'), 'error'); }
      } else if (btn.dataset.action === 'diagnose') {
        _survDiagnoseCamera(camId, cam?.name || camId);
      } else if (btn.dataset.action === 'retry') {
        try {
          await api(`/surveillance/cameras/${camId}/clear_error`, { method: 'POST' });
          toast('Wyzerowano błędy, ponawiam próby...', 'info');
          _survRenderCameras(el);
        } catch (e) { toast(t('Błąd'), 'error'); }
      }
    });
  });
}

async function _survShowCameraDialog(existing = null) {
  const isEdit = !!(existing && existing.id);
  const result = await new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal surv-modal-narrow">
        <div class="modal-header"><i class="fas fa-camera surv-modal-icon"></i>${isEdit ? t('Edytuj kamerę') : t('Dodaj kamerę')}</div>
        <div class="modal-body surv-modal-body-scroll">
          <label class="modal-label">Nazwa kamery</label>
          <input class="modal-input" id="surv-cam-name" value="${_survEsc(existing?.name || '')}" placeholder="np. Front Door">

          <label class="modal-label surv-label-mt">URL strumienia RTSP</label>
          <input class="modal-input" id="surv-cam-url" value="${_survEsc(existing?.url || '')}" placeholder="rtsp://192.168.1.100:554/stream">

          <label class="modal-label surv-label-mt">${t('URL podstrumienia (opcjonalny, do podglądu)')}</label>
          <input class="modal-input" id="surv-cam-suburl" value="${_survEsc(existing?.substream_url || '')}" placeholder="rtsp://...">

          <div class="surv-section-divider">
            <label class="modal-label surv-label-mb">
              <input type="checkbox" id="surv-cam-onvif-toggle" ${existing?.onvif_host ? 'checked' : ''}> Konfiguracja ONVIF (opcjonalna)
            </label>
            <div id="surv-cam-onvif-fields" style="${existing?.onvif_host ? '' : 'display:none;'}">
              <div class="surv-onvif-grid-hp">
                <input class="modal-input" id="surv-cam-onvif-host" value="${_survEsc(existing?.onvif_host || '')}" placeholder="IP kamery">
                <input class="modal-input" id="surv-cam-onvif-port" value="${existing?.onvif_port || 80}" placeholder="Port">
              </div>
              <div class="surv-onvif-grid-2col">
                <input class="modal-input" id="surv-cam-onvif-user" value="${_survEsc(existing?.onvif_user || '')}" placeholder="${t('Użytkownik')}">
                <input class="modal-input" id="surv-cam-onvif-pass" type="password" value="${_survEsc(existing?.onvif_pass || '')}" placeholder="${t('Hasło')}">
              </div>
              <button class="btn surv-field-note" id="surv-onvif-probe"><i class="fas fa-search"></i> Pobierz URL strumienia</button>
              <div class="surv-field-note" id="surv-onvif-probe-result"></div>
            </div>
          </div>

          <div class="surv-checkbox-row">
            <label class="surv-checkbox-label">
              <input type="checkbox" id="surv-cam-enabled" ${existing ? (existing.enabled ? 'checked' : '') : 'checked'}> Aktywna
            </label>
            <label class="surv-checkbox-label">
              <input type="checkbox" id="surv-cam-record" ${existing?.record ? 'checked' : ''}> Nagrywaj
            </label>
          </div>

          <div class="surv-section-divider-sm">
            <label class="modal-label surv-label-mb-sm">Tryb nagrywania</label>
            <div class="surv-rec-mode-selector">
              <button type="button" class="surv-rec-mode-btn ${(existing?.recording_mode || 'continuous') === 'continuous' ? 'active' : ''}" data-mode="continuous">
                <i class="fas fa-video"></i> ${t('Ciągłe')}
              </button>
              <button type="button" class="surv-rec-mode-btn ${(existing?.recording_mode || 'continuous') === 'events' ? 'active' : ''}" data-mode="events">
                <i class="fas fa-bolt"></i> Zdarzenia
              </button>
            </div>
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn" id="surv-cam-cancel">Anuluj</button>
          <button class="btn btn-primary" id="surv-cam-save"><i class="fas fa-save"></i> ${isEdit ? 'Zapisz' : 'Dodaj'}</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    // Recording mode toggle in camera dialog
    overlay.querySelectorAll('.surv-rec-mode-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        overlay.querySelectorAll('.surv-rec-mode-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });

    // ONVIF toggle
    overlay.querySelector('#surv-cam-onvif-toggle').addEventListener('change', (e) => {
      overlay.querySelector('#surv-cam-onvif-fields').style.display = e.target.checked ? 'block' : 'none';
    });

    // ONVIF probe
    overlay.querySelector('#surv-onvif-probe')?.addEventListener('click', async () => {
      const host = overlay.querySelector('#surv-cam-onvif-host').value.trim();
      const port = overlay.querySelector('#surv-cam-onvif-port').value || 80;
      const user = overlay.querySelector('#surv-cam-onvif-user').value;
      const pass = overlay.querySelector('#surv-cam-onvif-pass').value;
      const res = overlay.querySelector('#surv-onvif-probe-result');
      res.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Sondowanie…';
      try {
        const r = await api('/surveillance/onvif/probe', { method: 'POST', body: { host, port, user, password: pass } });
        if (r.error) {
          res.innerHTML = `<span class="surv-text-danger">${r.error}</span>`;
        } else if (r.streams?.length) {
          res.innerHTML = r.streams.map(s => `
            <div class="surv-onvif-stream surv-onvif-stream-item" data-url="${_survEsc(s.url)}">
              <strong>${_survEsc(s.profile)}</strong> ${s.resolution ? `(${s.resolution})` : ''}<br>
              <code class="surv-code-muted">${_survEsc(s.url)}</code>
            </div>
          `).join('');
          res.querySelectorAll('.surv-onvif-stream').forEach(div => {
            div.addEventListener('click', () => {
              overlay.querySelector('#surv-cam-url').value = div.dataset.url;
              toast('URL strumienia ustawiony', 'success');
            });
          });
        } else {
          res.innerHTML = '<span class="surv-text-muted">Nie znaleziono strumieni</span>';
        }
      } catch (e) {
        res.innerHTML = `<span class="surv-text-danger">${e.message}</span>`;
      }
    });

    overlay.querySelector('#surv-cam-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
    overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
    overlay.querySelector('#surv-cam-save').addEventListener('click', () => {
      overlay.remove();
      resolve({
        name: overlay.querySelector('#surv-cam-name').value.trim(),
        url: overlay.querySelector('#surv-cam-url').value.trim(),
        substream_url: overlay.querySelector('#surv-cam-suburl').value.trim(),
        onvif_host: overlay.querySelector('#surv-cam-onvif-host')?.value.trim() || '',
        onvif_port: parseInt(overlay.querySelector('#surv-cam-onvif-port')?.value) || 80,
        onvif_user: overlay.querySelector('#surv-cam-onvif-user')?.value || '',
        onvif_pass: overlay.querySelector('#surv-cam-onvif-pass')?.value || '',
        enabled: overlay.querySelector('#surv-cam-enabled').checked,
        record: overlay.querySelector('#surv-cam-record').checked,
        recording_mode: overlay.querySelector('.surv-rec-mode-btn.active')?.dataset.mode || 'continuous',
      });
    });
  });

  if (!result || !result.name || !result.url) return;

  try {
    if (isEdit) {
      await api(`/surveillance/cameras/${existing.id}`, { method: 'PUT', body: result });
      toast('Kamera zaktualizowana', 'success');
    } else {
      const resp = await api('/surveillance/cameras', { method: 'POST', body: result });
      if (resp && resp.warning) {
        toast('⚠️ Kamera dodana ale wykryto problem: ' + resp.warning, 'warning', 8000);
      } else {
        toast('Kamera dodana', 'success');
      }
    }
    await _survLoadData();
    const content = SURV.root.querySelector('#surv-content');
    if (content && SURV.tab === 'cameras') _survRenderCameras(content);
  } catch (e) {
    toast(t('Błąd: ') + e.message, 'error');
  }
}

/* ━━━━  DISCOVERY  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _survDiscoverCameras(parentEl) {
  // Get local subnet guess
  let subnet = '';
  try {
    const net = await api('/network/interfaces');
    const iface = (net || []).find(i => i.ip4 && i.ip4 !== '127.0.0.1');
    if (iface) {
      const parts = iface.ip4.split('.');
      subnet = parts.slice(0, 3).join('.') + '.0/24';
    }
  } catch (e) {}

  const sub = prompt(t('Podaj podsieć do skanowania:'), subnet || '192.168.1.0/24');
  if (!sub) return;

  const results = parentEl.querySelector('#surv-discover-results');
  results.style.display = 'block';
  results.innerHTML = `<div class="surv-loading-center"><i class="fas fa-spinner fa-spin"></i> ${t('Skanowanie sieci… To może zająć do 30 sekund.')}</div>`;

  try {
    await api('/surveillance/discover', { method: 'POST', body: { subnet: sub } });

    // Poll for results
    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      try {
        const r = await api('/surveillance/discover');
        if (!r.running || attempts > 30) {
          clearInterval(poll);
          if (r.cameras?.length) {
            results.innerHTML = `
              <div class="surv-discovery-pad">
                <h4 class="surv-discovery-heading"><i class="fas fa-search"></i> ${t('Znaleziono')} ${r.cameras.length} ${t('kamer(ę/y)')}</h4>
                ${r.cameras.map(c => `
                  <div class="surv-discover-item">
                    <div>
                      <strong>${_survEsc(c.name)}</strong><br>
                      <span class="surv-text-sm-muted">${_survEsc(c.ip)}:${c.port} — ${c.method}</span>
                      ${c.url ? `<br><code class="surv-text-xs">${_survEsc(c.url)}</code>` : ''}
                    </div>
                    <button class="btn btn-primary surv-discover-add" data-cam='${JSON.stringify(c).replace(/'/g, "&#39;")}'>
                      <i class="fas fa-plus"></i> Dodaj
                    </button>
                  </div>
                `).join('')}
              </div>
            `;
            results.querySelectorAll('.surv-discover-add').forEach(btn => {
              btn.addEventListener('click', () => {
                const cam = JSON.parse(btn.dataset.cam);
                _survShowCameraDialog({
                  name: cam.name,
                  url: cam.url || `rtsp://${cam.ip}:${cam.port}/`,
                  onvif_host: cam.method === 'onvif' ? cam.ip : '',
                  onvif_port: cam.port || 80,
                });
              });
            });
          } else {
            results.innerHTML = '<div class="surv-empty-msg"><i class="fas fa-exclamation-circle"></i> Nie znaleziono kamer w sieci.</div>';
          }
        }
      } catch (e) { clearInterval(poll); }
    }, 2000);
  } catch (e) {
    results.innerHTML = `<div class="surv-error-msg">${e.message}</div>`;
  }

  // Also listen via SocketIO
  if (NAS.socket) {
    NAS.socket.on('surveillance_discovery', (data) => {
      if (data.status === 'done' && data.cameras) {
        // Will be picked up by polling above
      }
    });
  }
}

/* ━━━━  RECORDINGS TAB  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _survRenderRecordings(el) {
  el.innerHTML = `<div class="surv-empty"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie nagrań…')}</div>`;

  let data;
  try {
    data = await api('/surveillance/recordings');
  } catch (e) {
    el.innerHTML = `<div class="surv-empty"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd ładowania nagrań')}</div>`;
    return;
  }

  if (!data.files?.length && !data.dates?.length) {
    el.innerHTML = `<div class="surv-empty"><i class="fas fa-film"></i><p>${t('Brak nagrań. Włącz nagrywanie w ustawieniach.')}</p></div>`;
    return;
  }

  el.innerHTML = `
    <div class="surv-rec-toolbar">
      <label class="surv-toolbar-label">Data:</label>
      <select class="modal-input" id="surv-rec-date" style="width:auto;min-width:160px;">
        ${data.dates.map(d => `<option value="${d}" ${d === data.date ? 'selected' : ''}>${d}</option>`).join('')}
      </select>
      <label class="surv-toolbar-label" style="margin-left:12px;">Kamera:</label>
      <select class="modal-input" id="surv-rec-cam" style="width:auto;min-width:160px;">
        <option value="">Wszystkie</option>
        ${SURV.cameras.map(c => `<option value="${c.id}">${_survEsc(c.name)}</option>`).join('')}
      </select>
    </div>
    <div class="surv-rec-list" id="surv-rec-list">
      ${_survRenderRecordingsList(data.files)}
    </div>
  `;

  // Date/camera change
  const reload = async () => {
    const date = el.querySelector('#surv-rec-date').value;
    const cam = el.querySelector('#surv-rec-cam').value;
    let url = `/surveillance/recordings?date=${date}`;
    if (cam) url += `&camera=${cam}`;
    try {
      const d = await api(url);
      el.querySelector('#surv-rec-list').innerHTML = _survRenderRecordingsList(d.files || []);
      _survBindRecEvents(el);
    } catch (e) {}
  };
  el.querySelector('#surv-rec-date').addEventListener('change', reload);
  el.querySelector('#surv-rec-cam').addEventListener('change', reload);

  _survBindRecEvents(el);
}

function _survRenderRecordingsList(files) {
  if (!files.length) return `<div class="surv-empty-msg">${t('Brak nagrań dla wybranego dnia.')}</div>`;
  return files.map(f => `
    <div class="surv-rec-item">
      <div class="surv-rec-item-info">
        <i class="fas fa-file-video surv-text-accent"></i>
        <div>
          <div class="surv-rec-item-name">${_survEsc(f.filename)}</div>
          <div class="surv-rec-item-meta">
            <span><i class="fas fa-camera"></i> ${_survEsc(f.camera_name)}</span>
            <span><i class="fas fa-weight-hanging"></i> ${_survFormatBytes(f.size)}</span>
          </div>
        </div>
      </div>
      <div class="surv-rec-item-actions">
        <button class="surv-cam-btn" data-action="play" data-path="${_survEsc(f.path)}" title="${t('Odtwórz')}"><i class="fas fa-play"></i></button>
        <button class="surv-cam-btn" data-action="download" data-path="${_survEsc(f.path)}" data-name="${_survEsc(f.filename)}" title="Pobierz"><i class="fas fa-download"></i></button>
        <button class="surv-cam-btn danger" data-action="delete-rec" data-path="${_survEsc(f.path)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
      </div>
    </div>
  `).join('');
}

function _survBindRecEvents(el) {
  el.querySelectorAll('.surv-cam-btn[data-action="play"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const path = btn.dataset.path;
      _survPlayRecording(path);
    });
  });
  el.querySelectorAll('.surv-cam-btn[data-action="download"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const a = document.createElement('a');
      a.href = `/api/surveillance/recordings/play/${btn.dataset.path}`;
      a.download = btn.dataset.name;
      a.click();
    });
  });
  el.querySelectorAll('.surv-cam-btn[data-action="delete-rec"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!await confirmDialog(t('Usunąć to nagranie?'))) return;
      try {
        await api('/surveillance/recordings/delete', { method: 'DELETE', body: { paths: [btn.dataset.path] } });
        btn.closest('.surv-rec-item')?.remove();
        toast(t('Nagranie usunięte'), 'success');
      } catch (e) { toast(t('Błąd usuwania'), 'error'); }
    });
  });
}

function _survPlayRecording(path) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.style.zIndex = '99999';

  const filename = path.split('/').pop();

  overlay.innerHTML = `
    <div class="surv-player-wrap" onclick="event.stopPropagation()">
      <div class="surv-player-top-bar">
        <span class="surv-player-title"><i class="fas fa-film"></i> ${_survEsc(filename)}</span>
        <button class="surv-player-top-btn" data-act="close" title="Zamknij"><i class="fas fa-times"></i></button>
      </div>
      <div class="surv-player-video-area">
        <video class="surv-player-video" src="/api/surveillance/recordings/play/${path}" preload="metadata"></video>
        <div class="surv-player-big-play"><i class="fas fa-play"></i></div>
        <div class="surv-player-overlay-msg" style="display:none;"></div>
      </div>
      <div class="surv-player-progress-wrap">
        <div class="surv-player-progress-bar">
          <div class="surv-player-progress-buffered"></div>
          <div class="surv-player-progress-fill"></div>
          <div class="surv-player-progress-thumb"></div>
        </div>
        <div class="surv-player-progress-tooltip" style="display:none;">0:00</div>
      </div>
      <div class="surv-player-controls">
        <div class="surv-player-controls-left">
          <button class="surv-player-btn" data-act="play" title="${t('Odtwórz / Pauza')}"><i class="fas fa-play"></i></button>
          <button class="surv-player-btn" data-act="frame-back" title="Klatka wstecz"><i class="fas fa-step-backward"></i></button>
          <button class="surv-player-btn" data-act="skip-back" title="-10s"><i class="fas fa-undo"></i> <span class="surv-skip-label">10</span></button>
          <button class="surv-player-btn" data-act="skip-fwd" title="+10s"><i class="fas fa-redo"></i> <span class="surv-skip-label">10</span></button>
          <button class="surv-player-btn" data-act="frame-fwd" title="${t('Klatka naprzód')}"><i class="fas fa-step-forward"></i></button>
          <div class="surv-player-time"><span class="surv-player-cur">0:00</span> / <span class="surv-player-dur">0:00</span></div>
        </div>
        <div class="surv-player-controls-right">
          <div class="surv-player-vol-wrap">
            <button class="surv-player-btn" data-act="mute" title="Wycisz"><i class="fas fa-volume-up"></i></button>
            <input type="range" class="surv-player-vol" min="0" max="1" step="0.05" value="1">
          </div>
          <button class="surv-player-btn surv-player-speed-btn" data-act="speed" title="${t('Prędkość')}">1x</button>
          <button class="surv-player-btn" data-act="pip" title="Obraz w obrazie"><i class="fas fa-external-link-square-alt"></i></button>
          <button class="surv-player-btn" data-act="download" title="Pobierz"><i class="fas fa-download"></i></button>
          <button class="surv-player-btn" data-act="fullscreen" title="${t('Pełny ekran')}"><i class="fas fa-expand"></i></button>
        </div>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  const wrap = overlay.querySelector('.surv-player-wrap');
  const video = overlay.querySelector('.surv-player-video');
  const bigPlay = overlay.querySelector('.surv-player-big-play');
  const progWrap = overlay.querySelector('.surv-player-progress-wrap');
  const progBar = overlay.querySelector('.surv-player-progress-bar');
  const progFill = overlay.querySelector('.surv-player-progress-fill');
  const progBuffered = overlay.querySelector('.surv-player-progress-buffered');
  const progThumb = overlay.querySelector('.surv-player-progress-thumb');
  const progTooltip = overlay.querySelector('.surv-player-progress-tooltip');
  const playBtn = overlay.querySelector('[data-act="play"]');
  const curTime = overlay.querySelector('.surv-player-cur');
  const durTime = overlay.querySelector('.surv-player-dur');
  const volSlider = overlay.querySelector('.surv-player-vol');
  const muteBtn = overlay.querySelector('[data-act="mute"]');
  const speedBtn = overlay.querySelector('[data-act="speed"]');
  const overlayMsg = overlay.querySelector('.surv-player-overlay-msg');

  const speeds = [0.25, 0.5, 1, 1.5, 2, 4];
  let speedIdx = 2; // start at 1x
  let dragging = false;

  function fmtTime(s) {
    if (!isFinite(s)) return '0:00';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.floor(s % 60);
    return h > 0 ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}` : `${m}:${String(sec).padStart(2,'0')}`;
  }

  function updateProgress() {
    if (!video.duration || dragging) return;
    const pct = (video.currentTime / video.duration) * 100;
    progFill.style.width = pct + '%';
    progThumb.style.left = pct + '%';
    curTime.textContent = fmtTime(video.currentTime);
  }

  function updateBuffered() {
    if (!video.duration) return;
    if (video.buffered.length > 0) {
      const end = video.buffered.end(video.buffered.length - 1);
      progBuffered.style.width = (end / video.duration) * 100 + '%';
    }
  }

  function togglePlay() {
    if (video.paused || video.ended) {
      video.play();
    } else {
      video.pause();
    }
  }

  function showMsg(text) {
    overlayMsg.textContent = text;
    overlayMsg.style.display = 'flex';
    clearTimeout(overlayMsg._t);
    overlayMsg._t = setTimeout(() => { overlayMsg.style.display = 'none'; }, 800);
  }

  // Video events
  video.addEventListener('timeupdate', updateProgress);
  video.addEventListener('progress', updateBuffered);
  video.addEventListener('loadedmetadata', () => {
    durTime.textContent = fmtTime(video.duration);
    updateProgress();
  });
  video.addEventListener('play', () => {
    playBtn.innerHTML = '<i class="fas fa-pause"></i>';
    bigPlay.style.display = 'none';
  });
  video.addEventListener('pause', () => {
    playBtn.innerHTML = '<i class="fas fa-play"></i>';
    if (video.ended) return;
    bigPlay.innerHTML = '<i class="fas fa-pause"></i>';
    bigPlay.style.display = 'flex';
    setTimeout(() => { if (video.paused && !video.ended) bigPlay.style.display = 'none'; }, 600);
  });
  video.addEventListener('ended', () => {
    playBtn.innerHTML = '<i class="fas fa-redo"></i>';
    bigPlay.innerHTML = '<i class="fas fa-redo"></i>';
    bigPlay.style.display = 'flex';
  });
  video.addEventListener('volumechange', () => {
    const icon = video.muted || video.volume === 0 ? 'fa-volume-mute' : video.volume < 0.5 ? 'fa-volume-down' : 'fa-volume-up';
    muteBtn.innerHTML = `<i class="fas ${icon}"></i>`;
    volSlider.value = video.muted ? 0 : video.volume;
  });

  // Big play / video area click
  bigPlay.addEventListener('click', (e) => { e.stopPropagation(); togglePlay(); });
  overlay.querySelector('.surv-player-video-area').addEventListener('click', (e) => {
    if (e.target === video || e.target.closest('.surv-player-video-area') && !e.target.closest('.surv-player-big-play')) togglePlay();
  });

  // Progress bar interaction
  function seekFromEvent(e) {
    const rect = progBar.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    return pct;
  }

  progWrap.addEventListener('mousedown', (e) => {
    dragging = true;
    const pct = seekFromEvent(e);
    progFill.style.width = pct * 100 + '%';
    progThumb.style.left = pct * 100 + '%';
    video.currentTime = pct * video.duration;
  });
  document.addEventListener('mousemove', (e) => {
    if (dragging && video.duration) {
      const pct = seekFromEvent(e);
      progFill.style.width = pct * 100 + '%';
      progThumb.style.left = pct * 100 + '%';
      video.currentTime = pct * video.duration;
      curTime.textContent = fmtTime(video.currentTime);
    }
    // Tooltip
    if (video.duration) {
      const rect = progBar.getBoundingClientRect();
      const x = e.clientX - rect.left;
      if (x >= 0 && x <= rect.width) {
        const pct = x / rect.width;
        progTooltip.textContent = fmtTime(pct * video.duration);
        progTooltip.style.display = 'block';
        progTooltip.style.left = Math.min(Math.max(x, 20), rect.width - 20) + 'px';
      } else {
        progTooltip.style.display = 'none';
      }
    }
  });
  document.addEventListener('mouseup', () => { dragging = false; });
  progWrap.addEventListener('mouseleave', () => { if (!dragging) progTooltip.style.display = 'none'; });

  // Button actions
  overlay.querySelector('[data-act="close"]').addEventListener('click', () => cleanup());
  overlay.querySelector('[data-act="play"]').addEventListener('click', togglePlay);
  overlay.querySelector('[data-act="skip-back"]').addEventListener('click', () => { video.currentTime = Math.max(0, video.currentTime - 10); showMsg('-10s'); });
  overlay.querySelector('[data-act="skip-fwd"]').addEventListener('click', () => { video.currentTime = Math.min(video.duration, video.currentTime + 10); showMsg('+10s'); });
  overlay.querySelector('[data-act="frame-back"]').addEventListener('click', () => { video.pause(); video.currentTime = Math.max(0, video.currentTime - 1/30); showMsg('◀ Klatka'); });
  overlay.querySelector('[data-act="frame-fwd"]').addEventListener('click', () => { video.pause(); video.currentTime = Math.min(video.duration, video.currentTime + 1/30); showMsg('Klatka ▶'); });

  speedBtn.addEventListener('click', () => {
    speedIdx = (speedIdx + 1) % speeds.length;
    video.playbackRate = speeds[speedIdx];
    speedBtn.textContent = speeds[speedIdx] + 'x';
    showMsg(speeds[speedIdx] + 'x');
  });

  muteBtn.addEventListener('click', () => { video.muted = !video.muted; });
  volSlider.addEventListener('input', () => { video.volume = parseFloat(volSlider.value); video.muted = false; });

  overlay.querySelector('[data-act="pip"]').addEventListener('click', async () => {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await video.requestPictureInPicture();
    } catch (e) { showMsg(t('PiP niedostępny')); }
  });

  overlay.querySelector('[data-act="download"]').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = `/api/surveillance/recordings/play/${path}`;
    a.download = filename;
    a.click();
  });

  overlay.querySelector('[data-act="fullscreen"]').addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else wrap.requestFullscreen?.();
  });

  // Keyboard handling
  function onKey(e) {
    if (!document.body.contains(overlay)) return;
    switch (e.key) {
      case ' ': case 'k': e.preventDefault(); togglePlay(); break;
      case 'ArrowLeft': e.preventDefault(); video.currentTime = Math.max(0, video.currentTime - 10); showMsg('-10s'); break;
      case 'ArrowRight': e.preventDefault(); video.currentTime = Math.min(video.duration, video.currentTime + 10); showMsg('+10s'); break;
      case 'ArrowUp': e.preventDefault(); video.volume = Math.min(1, video.volume + 0.1); showMsg(Math.round(video.volume * 100) + '%'); break;
      case 'ArrowDown': e.preventDefault(); video.volume = Math.max(0, video.volume - 0.1); showMsg(Math.round(video.volume * 100) + '%'); break;
      case 'f': wrap.requestFullscreen?.(); break;
      case 'm': video.muted = !video.muted; break;
      case 'Escape': cleanup(); break;
      case ',': e.preventDefault(); video.pause(); video.currentTime = Math.max(0, video.currentTime - 1/30); break;
      case '.': e.preventDefault(); video.pause(); video.currentTime += 1/30; break;
      case '<': e.preventDefault(); speedIdx = Math.max(0, speedIdx - 1); video.playbackRate = speeds[speedIdx]; speedBtn.textContent = speeds[speedIdx] + 'x'; showMsg(speeds[speedIdx] + 'x'); break;
      case '>': e.preventDefault(); speedIdx = Math.min(speeds.length - 1, speedIdx + 1); video.playbackRate = speeds[speedIdx]; speedBtn.textContent = speeds[speedIdx] + 'x'; showMsg(speeds[speedIdx] + 'x'); break;
    }
  }
  document.addEventListener('keydown', onKey);

  overlay.addEventListener('click', (e) => { if (e.target === overlay) cleanup(); });

  function cleanup() {
    document.removeEventListener('keydown', onKey);
    video.pause();
    video.src = '';
    overlay.remove();
  }

  // Autoplay
  video.play().catch(() => {});
}

/* ━━━━  SETTINGS TAB  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _survRenderSettings(el) {
  await _survLoadData();
  const s = SURV.settings;

  el.innerHTML = `
    <div class="surv-settings">
      <h3 class="surv-settings-title"><i class="fas fa-cog"></i> Ustawienia Surveillance Station</h3>

      <div class="surv-settings-group">
        <h4><i class="fas fa-circle surv-rec-dot-icon"></i> Nagrywanie</h4>
        <div class="surv-settings-row">
          <label>Status nagrywania</label>
          <div class="surv-flex-gap-sm">
            <button class="btn ${s.recording_enabled ? 'btn-danger' : 'btn-primary'}" id="surv-rec-toggle">
              <i class="fas ${s.recording_enabled ? 'fa-stop' : 'fa-circle'}"></i>
              ${s.recording_enabled ? 'Zatrzymaj nagrywanie' : 'Rozpocznij nagrywanie'}
            </button>
          </div>
        </div>
        <div class="surv-settings-row">
          <label>Tryb nagrywania</label>
          <span class="surv-text-sm-muted"><i class="fas fa-info-circle"></i> ${t('Tryb (ciągłe / zdarzenia) ustawiany jest indywidualnie per kamera w edycji kamery.')}</span>
        </div>
        <div class="surv-settings-row">
          <label>${t('Długość segmentu — ciągłe (minuty)')}</label>
          <input class="modal-input" type="number" id="surv-seg-min" value="${s.segment_minutes || 15}" min="1" max="120" style="width:100px;">
        </div>
        <div>
          <p class="surv-event-info"><i class="fas fa-bolt surv-text-warning"></i> ${t('Domyślne parametry nagrywania zdarzeniowego (dla kamer w trybie "Zdarzenia"):')}</p>
          <div class="surv-settings-row">
            <label>${t('Czułość detekcji ruchu')}</label>
            <div class="surv-flex-center">
              <input type="range" id="surv-evt-sensitivity" min="10" max="95" value="${s.motion_sensitivity || 50}" style="width:140px;">
              <span class="surv-sens-value" id="surv-evt-sensitivity-val">${s.motion_sensitivity || 50}%</span>
            </div>
          </div>
          <div class="surv-settings-row">
            <label>Czas nagrywania po zdarzeniu (sekundy)</label>
            <input class="modal-input" type="number" id="surv-evt-post" value="${s.event_post_seconds || 15}" min="5" max="300" style="width:100px;">
          </div>
          <div class="surv-settings-row">
            <label>Bufor przed zdarzeniem (sekundy)</label>
            <input class="modal-input" type="number" id="surv-evt-pre" value="${s.event_pre_seconds || 5}" min="0" max="30" style="width:100px;">
          </div>
          <div class="surv-settings-row">
            <label>${t('Cooldown między zdarzeniami (sekundy)')}</label>
            <input class="modal-input" type="number" id="surv-evt-cooldown" value="${s.event_cooldown || 10}" min="1" max="120" style="width:100px;">
          </div>
        </div>
        <div class="surv-settings-row">
          <label>${t('Retencja nagrań (dni)')}</label>
          <input class="modal-input" type="number" id="surv-ret-days" value="${s.retention_days || 30}" min="1" max="365" style="width:100px;">
        </div>
        <div class="surv-settings-row">
          <label>${t('Ścieżka nagrań')}</label>
          <input class="modal-input surv-input-flex" id="surv-rec-path" value="${_survEsc(s.recordings_path || '')}">
        </div>
      </div>

      <div class="surv-settings-group surv-mt-lg">
        <h4><i class="fas fa-search-location surv-text-purple"></i> Automatyczne wyszukiwanie kamer</h4>
        <div class="surv-settings-row">
          <label>${t('Włącz automatyczne skanowanie sieci')}</label>
          <label class="surv-toggle">
            <input type="checkbox" id="surv-auto-disc" ${s.auto_discovery ? 'checked' : ''}>
            <span class="surv-toggle-slider"></span>
          </label>
        </div>
        <div class="surv-settings-row">
          <label>${t('Interwał skanowania (sekundy)')}</label>
          <input class="modal-input" type="number" id="surv-disc-interval" value="${s.auto_discovery_interval || 300}" min="60" max="3600" style="width:100px;">
        </div>
        <p class="surv-info-note"><i class="fas fa-info-circle"></i> ${t('System automatycznie skanuje sieć LAN (ONVIF + porty RTSP) i powiadomi o nowych kamerach.')}</p>
      </div>

      <div class="surv-mt-xl">
        <button class="btn btn-primary" id="surv-save-settings"><i class="fas fa-save"></i> Zapisz ustawienia</button>
      </div>

      <div class="surv-settings-group surv-mt-2xl">
        <h4><i class="fas fa-info-circle surv-text-accent"></i> Informacje</h4>
        <div class="surv-settings-row">
          <label>Kamery</label>
          <span>${SURV.cameras.length}</span>
        </div>
        <div class="surv-settings-row">
          <label>Aktywne strumienie</label>
          <span>${SURV.cameras.filter(c => c.streaming).length}</span>
        </div>
        <div class="surv-settings-row">
          <label>Nagrywane</label>
          <span>${SURV.cameras.filter(c => c.recording).length}</span>
        </div>
      </div>
    </div>
  `;

  // Recording toggle
  el.querySelector('#surv-rec-toggle').addEventListener('click', async () => {
    try {
      if (s.recording_enabled) {
        await api('/surveillance/recording/stop', { method: 'POST' });
        toast('Nagrywanie zatrzymane', 'success');
      } else {
        await api('/surveillance/recording/start', { method: 'POST' });
        toast('Nagrywanie uruchomione', 'success');
      }
      await _survLoadData();
      _survRenderSettings(el);
    } catch (e) { toast(t('Błąd: ') + e.message, 'error'); }
  });

  // Sensitivity slider live value
  const sensSlider = el.querySelector('#surv-evt-sensitivity');
  if (sensSlider) {
    sensSlider.addEventListener('input', () => {
      el.querySelector('#surv-evt-sensitivity-val').textContent = sensSlider.value + '%';
    });
  }

  // Save settings
  el.querySelector('#surv-save-settings').addEventListener('click', async () => {
    try {
      await api('/surveillance/settings', {
        method: 'PUT',
        body: {
          segment_minutes: parseInt(el.querySelector('#surv-seg-min').value) || 15,
          retention_days: parseInt(el.querySelector('#surv-ret-days').value) || 30,
          recordings_path: el.querySelector('#surv-rec-path').value.trim(),
          motion_sensitivity: parseInt(el.querySelector('#surv-evt-sensitivity').value) || 50,
          event_post_seconds: parseInt(el.querySelector('#surv-evt-post').value) || 15,
          event_pre_seconds: parseInt(el.querySelector('#surv-evt-pre').value) || 5,
          event_cooldown: parseInt(el.querySelector('#surv-evt-cooldown').value) || 10,
          auto_discovery: el.querySelector('#surv-auto-disc').checked,
          auto_discovery_interval: parseInt(el.querySelector('#surv-disc-interval').value) || 300,
        }
      });
      await _survLoadData();
      toast('Ustawienia zapisane', 'success');
    } catch (e) { toast(t('Błąd zapisu'), 'error'); }
  });
}

/* ━━━━  CAMERA DIAGNOSTICS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _survDiagnoseCamera(camId, camName) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal surv-modal-narrow">
      <div class="modal-header"><i class="fas fa-stethoscope surv-modal-icon"></i>Diagnostyka: ${_survEsc(camName)}</div>
      <div class="modal-body surv-modal-body-scroll">
        <div class="surv-loading-center" id="surv-diag-content">
          <i class="fas fa-spinner fa-spin surv-spinner-icon"></i>
          <div class="surv-loading-msg">Uruchamiam testy...</div>
        </div>
      </div>
      <div class="modal-footer"><button class="btn" id="surv-diag-close">Zamknij</button></div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#surv-diag-close').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

  try {
    const data = await api(`/surveillance/cameras/${camId}/diagnose`, { method: 'POST' });
    const content = overlay.querySelector('#surv-diag-content');
    if (!content) return;

    let html = '<div style="text-align:left;">';
    if (data.url_masked) {
      html += '<div class="surv-diag-url"><i class="fas fa-link"></i> ' + _survEsc(data.url_masked) + '</div>';
    }

    for (const check of (data.checks || [])) {
      const icon = check.ok ? '<i class="fas fa-check-circle surv-diag-ok"></i>' : '<i class="fas fa-times-circle surv-text-danger"></i>';
      html += '<div class="surv-diag-check">';
      html += '<div class="surv-flex-center">' + icon + ' <strong>' + _survEsc(check.name) + '</strong></div>';
      html += '<div class="surv-diag-msg">' + _survEsc(check.msg) + '</div>';
      if (check.suggestion) {
        html += '<div class="surv-diag-suggestion">';
        html += '<i class="fas fa-lightbulb surv-text-amber"></i> Sugerowany URL: <code class="surv-text-xs" style="word-break:break-all;">' + _survEsc(check.suggestion) + '</code>';
        html += ' <button class="btn btn-sm surv-diag-copy-btn" onclick="navigator.clipboard.writeText(\'' + _survEsc(check.suggestion).replace(/'/g, "\\'") + '\');this.textContent=\'Skopiowano!\';">Kopiuj</button>';
        html += '</div>';
      }
      if (check.info) {
        html += '<div class="surv-diag-info">';
        html += check.info.codec ? ('Kodek: ' + check.info.codec) : '';
        html += check.info.width ? (' | ' + t('Rozdzielczość:') + ' ' + check.info.width + '×' + check.info.height) : '';
        html += '</div>';
      }
      html += '</div>';
    }

    // Summary
    const summaryColor = data.status === 'ok' ? '#22c55e' : '#ef4444';
    html += '<div class="surv-diag-summary" style="background:' + (data.status === 'ok' ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)') + ';color:' + summaryColor + ';">';
    html += (data.status === 'ok' ? '<i class="fas fa-check-circle"></i> ' : '<i class="fas fa-exclamation-triangle"></i> ') + _survEsc(data.summary);
    html += '</div>';
    html += '</div>';
    content.innerHTML = html;
  } catch (e) {
    const content = overlay.querySelector('#surv-diag-content');
    if (content) content.innerHTML = '<div class="surv-text-danger"><i class="fas fa-exclamation-triangle"></i> ' + t('Błąd diagnostyki:') + ' ' + _survEsc(e.message || t('Nieznany błąd')) + '</div>';
  }
}

/* ━━━━  HELPERS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _survEsc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function _survFormatBytes(b) {
  if (!b) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i > 0 ? 1 : 0) + ' ' + u[i];
}

/* ━━━━  STYLES  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _survInjectStyles() {
  if (document.getElementById('surv-styles')) return;
  const style = document.createElement('style');
  style.id = 'surv-styles';
  style.textContent = `
    /* Topbar & Tabs */
    .surv-topbar { display:flex; align-items:center; justify-content:space-between; padding:8px 16px; background:var(--bg-secondary); border-bottom:1px solid var(--border); }
    .surv-tabs { display:flex; gap:4px; }
    .surv-tab { background:none; border:none; color:var(--text-muted); padding:8px 16px; border-radius:8px; cursor:pointer; font-size:13px; transition:all .15s; }
    .surv-tab:hover { background:var(--bg-tertiary); color:var(--text-primary); }
    .surv-tab.active { background:var(--accent); color:#fff; }
    .surv-topbar-right { display:flex; align-items:center; gap:12px; }
    .surv-rec-indicator { display:flex; align-items:center; gap:6px; color:#dc2626; font-weight:600; font-size:13px; }
    .surv-rec-dot { width:10px; height:10px; border-radius:50%; background:#dc2626; animation:surv-blink 1s infinite; }
    @keyframes surv-blink { 50% { opacity:0.3; } }

    /* Content */
    .surv-content { flex:1; overflow-y:auto; padding:0; }
    .surv-empty { display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%; gap:12px; color:var(--text-muted); padding:40px; }
    .surv-empty i { font-size:48px; opacity:0.3; }

    /* Live grid */
    .surv-live-grid { display:grid; gap:2px; height:100%; background:#000; }
    .surv-live-cell { position:relative; background:#111; display:flex; flex-direction:column; overflow:hidden; }
    .surv-live-header { position:absolute; top:0; left:0; right:0; display:flex; justify-content:space-between; align-items:center; padding:6px 10px; background:linear-gradient(180deg,rgba(0,0,0,.7),transparent); z-index:5; }
    .surv-live-name { color:#fff; font-size:12px; font-weight:600; display:flex; align-items:center; gap:6px; }
    .surv-live-dot { font-size:8px; color:#22c55e; }
    .surv-live-actions { display:flex; gap:4px; }
    .surv-live-btn { background:rgba(255,255,255,.15); border:none; color:#fff; width:28px; height:28px; border-radius:6px; cursor:pointer; font-size:12px; }
    .surv-live-btn:hover { background:rgba(255,255,255,.3); }
    .surv-live-video { flex:1; display:flex; align-items:center; justify-content:center; position:relative; }
    .surv-video-el { width:100%; height:100%; object-fit:contain; }
    .surv-live-loading, .surv-live-error { color:rgba(255,255,255,.5); font-size:13px; display:flex; align-items:center; gap:8px; }
    .surv-live-error { color:#ef4444; }
    .surv-unmute-btn { position:absolute; bottom:8px; right:8px; background:rgba(0,0,0,.6); border:none; color:#fff; padding:6px 8px; border-radius:6px; cursor:pointer; font-size:12px; z-index:5; }

    /* Camera cards */
    .surv-cameras-toolbar { display:flex; gap:8px; padding:16px; border-bottom:1px solid var(--border); }
    .surv-cameras-list { padding:16px; display:flex; flex-direction:column; gap:10px; }
    .surv-cam-card { display:flex; align-items:center; gap:14px; padding:12px; background:var(--bg-secondary); border-radius:10px; border:1px solid var(--border); }
    .surv-cam-thumb { width:120px; height:72px; border-radius:8px; overflow:hidden; background:#111; position:relative; flex-shrink:0; }
    .surv-cam-thumb img { width:100%; height:100%; object-fit:cover; }
    .surv-cam-thumb-overlay { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:rgba(255,255,255,.2); font-size:24px; }
    .surv-cam-info { flex:1; min-width:0; }
    .surv-cam-name { font-weight:600; color:var(--text-primary); font-size:14px; }
    .surv-cam-url { font-size:11px; color:var(--text-muted); font-family:monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:2px; }
    .surv-cam-badges { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }
    .surv-badge { font-size:10px; padding:2px 8px; border-radius:20px; font-weight:600; }
    .surv-badge.ok { background:#dcfce7; color:#16a34a; }
    .surv-badge.off { background:#f3f4f6; color:#6b7280; }
    .surv-badge.rec { background:#fef2f2; color:#dc2626; }
    .surv-badge.live { background:#eff6ff; color:#2563eb; }
    .surv-badge.onvif { background:#faf5ff; color:#7c3aed; }
    .surv-badge.error { background:#fef2f2; color:#dc2626; cursor:help; }
    .surv-badge.event-mode { background:#fefce8; color:#a16207; }
    .surv-badge.cont-mode { background:#eff6ff; color:#2563eb; }
    .surv-cam-error { font-size:11px; color:#ef4444; margin-top:4px; line-height:1.3; word-break:break-word; }
    .surv-cam-error small { color:#999; }

    /* Recording mode selector */
    .surv-rec-mode-selector { display:flex; gap:0; border-radius:8px; overflow:hidden; border:1px solid var(--border); }
    .surv-rec-mode-btn { background:var(--bg-tertiary); border:none; color:var(--text-muted); padding:8px 16px; font-size:13px; cursor:pointer; display:flex; align-items:center; gap:6px; transition:all .15s; }
    .surv-rec-mode-btn:hover { background:var(--bg-secondary); color:var(--text-primary); }
    .surv-rec-mode-btn.active { background:var(--accent); color:#fff; }
    .surv-rec-mode-btn:first-child { border-right:1px solid var(--border); }

    .surv-cam-actions { display:flex; gap:6px; }
    .surv-cam-btn { background:var(--bg-tertiary); border:1px solid var(--border); color:var(--text-muted); width:34px; height:34px; border-radius:8px; cursor:pointer; font-size:13px; display:flex; align-items:center; justify-content:center; }
    .surv-cam-btn:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
    .surv-cam-btn.danger:hover { background:#ef4444; border-color:#ef4444; }

    /* Discovery */
    .surv-discover-item { display:flex; justify-content:space-between; align-items:center; padding:10px; background:var(--bg-tertiary); border-radius:8px; margin:6px 0; }

    /* Recordings */
    .surv-rec-toolbar { display:flex; align-items:center; gap:8px; padding:12px 16px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
    .surv-rec-list { padding:8px 16px; }
    .surv-rec-item { display:flex; justify-content:space-between; align-items:center; padding:10px 12px; border-bottom:1px solid var(--border); }
    .surv-rec-item:hover { background:var(--bg-secondary); }
    .surv-rec-item-info { display:flex; align-items:center; gap:10px; }
    .surv-rec-item-name { font-weight:500; color:var(--text-primary); font-size:13px; }
    .surv-rec-item-meta { font-size:11px; color:var(--text-muted); display:flex; gap:12px; margin-top:2px; }
    .surv-rec-item-actions { display:flex; gap:4px; }

    /* Settings */
    .surv-settings { padding:24px; max-width:700px; }
    .surv-settings-group { background:var(--bg-secondary); border-radius:10px; padding:16px; border:1px solid var(--border); margin-top:12px; }
    .surv-settings-group h4 { margin:0 0 12px; color:var(--text-primary); font-size:14px; display:flex; align-items:center; gap:8px; }
    .surv-settings-row { display:flex; justify-content:space-between; align-items:center; padding:8px 0; }
    .surv-settings-row label { color:var(--text-muted); font-size:13px; }

    /* Install */
    .surv-dep { display:inline-flex; align-items:center; gap:6px; padding:6px 14px; border-radius:20px; font-size:13px; font-weight:500; }
    .surv-dep.ok { background:#dcfce7; color:#16a34a; }
    .surv-dep.missing { background:#fef2f2; color:#dc2626; }
    .surv-progress-bar { height:6px; border-radius:3px; background:var(--bg-tertiary); overflow:hidden; }
    .surv-progress-fill { height:100%; background:var(--accent); border-radius:3px; transition:width .3s; width:0%; }

    /* Toggle */
    .surv-toggle { position:relative; display:inline-block; width:44px; height:24px; }
    .surv-toggle input { opacity:0; width:0; height:0; }
    .surv-toggle-slider { position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0; background:var(--bg-tertiary); border-radius:24px; transition:.2s; border:1px solid var(--border); }
    .surv-toggle-slider:before { content:""; position:absolute; height:18px; width:18px; left:2px; bottom:2px; background:#fff; border-radius:50%; transition:.2s; }
    .surv-toggle input:checked + .surv-toggle-slider { background:var(--accent); border-color:var(--accent); }
    .surv-toggle input:checked + .surv-toggle-slider:before { transform:translateX(20px); }

    /* ── Player ── */
    .surv-player-wrap { position:relative; width:94vw; max-width:1100px; background:#000; border-radius:12px; overflow:hidden; display:flex; flex-direction:column; }
    .surv-player-wrap:fullscreen { width:100vw; max-width:100vw; border-radius:0; }
    .surv-player-top-bar { display:flex; justify-content:space-between; align-items:center; padding:8px 14px; background:rgba(0,0,0,.85); }
    .surv-player-title { color:#fff; font-size:13px; font-weight:600; display:flex; align-items:center; gap:8px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .surv-player-top-btn { background:none; border:none; color:rgba(255,255,255,.7); font-size:18px; cursor:pointer; padding:4px 8px; border-radius:6px; }
    .surv-player-top-btn:hover { color:#fff; background:rgba(255,255,255,.15); }
    .surv-player-video-area { position:relative; flex:1; display:flex; align-items:center; justify-content:center; background:#000; cursor:pointer; min-height:200px; }
    .surv-player-video { width:100%; max-height:75vh; display:block; }
    .surv-player-wrap:fullscreen .surv-player-video { max-height:calc(100vh - 100px); }
    .surv-player-big-play { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:54px; color:rgba(255,255,255,.85); pointer-events:auto; cursor:pointer; transition:opacity .2s; }
    .surv-player-big-play:hover { color:#fff; }
    .surv-player-overlay-msg { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:rgba(0,0,0,.7); color:#fff; font-size:18px; font-weight:700; padding:10px 22px; border-radius:8px; pointer-events:none; display:flex; align-items:center; justify-content:center; }
    .surv-player-progress-wrap { position:relative; padding:0 14px; background:rgba(0,0,0,.85); cursor:pointer; }
    .surv-player-progress-bar { position:relative; height:5px; background:rgba(255,255,255,.2); border-radius:3px; transition:height .1s; }
    .surv-player-progress-wrap:hover .surv-player-progress-bar { height:8px; }
    .surv-player-progress-buffered { position:absolute; top:0; left:0; height:100%; background:rgba(255,255,255,.15); border-radius:3px; }
    .surv-player-progress-fill { position:absolute; top:0; left:0; height:100%; background:#3b82f6; border-radius:3px; }
    .surv-player-progress-thumb { position:absolute; top:50%; width:14px; height:14px; background:#fff; border-radius:50%; transform:translate(-50%,-50%); opacity:0; transition:opacity .15s; box-shadow:0 0 4px rgba(0,0,0,.5); }
    .surv-player-progress-wrap:hover .surv-player-progress-thumb { opacity:1; }
    .surv-player-progress-tooltip { position:absolute; top:-28px; transform:translateX(-50%); background:rgba(0,0,0,.85); color:#fff; font-size:11px; padding:3px 8px; border-radius:4px; white-space:nowrap; pointer-events:none; }
    .surv-player-controls { display:flex; justify-content:space-between; align-items:center; padding:6px 10px 8px; background:rgba(0,0,0,.85); gap:4px; }
    .surv-player-controls-left, .surv-player-controls-right { display:flex; align-items:center; gap:4px; }
    .surv-player-btn { background:none; border:none; color:rgba(255,255,255,.8); font-size:14px; cursor:pointer; padding:6px 8px; border-radius:6px; display:flex; align-items:center; gap:3px; white-space:nowrap; }
    .surv-player-btn:hover { color:#fff; background:rgba(255,255,255,.15); }
    .surv-player-speed-btn { font-weight:700; font-size:13px; min-width:36px; justify-content:center; }
    .surv-player-time { color:rgba(255,255,255,.7); font-size:12px; font-family:monospace; margin-left:6px; white-space:nowrap; }
    .surv-player-vol-wrap { display:flex; align-items:center; gap:2px; }
    .surv-player-vol { -webkit-appearance:none; appearance:none; width:70px; height:4px; background:rgba(255,255,255,.25); border-radius:2px; outline:none; cursor:pointer; }
    .surv-player-vol::-webkit-slider-thumb { -webkit-appearance:none; width:12px; height:12px; border-radius:50%; background:#fff; cursor:pointer; }
    .surv-player-vol::-moz-range-thumb { width:12px; height:12px; border-radius:50%; background:#fff; cursor:pointer; border:none; }
    @media (max-width:600px) {
      .surv-player-wrap { width:100vw; border-radius:0; }
      .surv-player-vol-wrap { display:none; }
      .surv-player-btn { padding:4px 6px; font-size:13px; }
      .surv-player-time { font-size:11px; }
    }

    /* Dark mode badge overrides */
    @media (prefers-color-scheme: dark) {
      .surv-badge.ok { background:rgba(22,163,74,.15); }
      .surv-badge.off { background:rgba(107,114,128,.15); }
      .surv-badge.rec { background:rgba(220,38,38,.15); }
      .surv-badge.live { background:rgba(37,99,235,.15); }
      .surv-badge.onvif { background:rgba(124,58,237,.15); }
      .surv-badge.error { background:rgba(220,38,38,.15); }
      .surv-badge.event-mode { background:rgba(234,179,8,.15); color:#facc15; }
      .surv-badge.cont-mode { background:rgba(37,99,235,.15); color:#60a5fa; }
      .surv-dep.ok { background:rgba(22,163,74,.15); }
      .surv-dep.missing { background:rgba(220,38,38,.15); }
    }
  `;
  document.head.appendChild(style);
}
