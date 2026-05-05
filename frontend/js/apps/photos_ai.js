/* Photos AI - Face recognition, object detection, smart albums */
AppRegistry['photos-ai'] = function(appDef, launchOpts) {
  const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
      ? NAS.logClient('photos-ai', level, msg, details) : console.log('[photos-ai]', msg, details || '');

  createWindow('photos-ai', {
    title: t('Photos AI'),
    icon: appDef.icon,
    iconColor: appDef.color,
    width: 960, height: 640,
    onRender: body => _paiInit(body),
  });
};

async function _paiInit(body) {
  body.innerHTML = '<div class="pai-loading"><i class="fa-solid fa-spinner fa-spin"></i> ' + t('Sprawdzanie...') + '</div>';
  const st = await api('/photos-ai/pkg-status');
  if (st.error) { body.innerHTML = '<div class="pai-error">' + _paiEsc(st.error) + '</div>'; return; }
  if (!st.installed) { _paiShowInstall(body, st); return; }
  _paiShowMain(body, st);
}

function _paiShowInstall(body, st) {
  const depsHtml = Object.entries(st.deps).map(function(kv) {
    const cls = kv[1] ? 'pai-dep-ok' : '';
    const ico = kv[1] ? 'fa-check' : 'fa-xmark';
    return '<div class="pai-dep ' + cls + '"><i class="fa-solid ' + ico + '"></i> ' + kv[0] + '</div>';
  }).join('');
  const yoloCls = st.yolo_model ? 'pai-dep-ok' : '';
  const yoloIco = st.yolo_model ? 'fa-check' : 'fa-xmark';
  body.innerHTML = '<div class="pai-install">'
    + '<div class="pai-install-icon"><i class="fa-solid fa-brain" style="font-size:48px;color:#8b5cf6"></i></div>'
    + '<h2>' + t('Photos AI') + '</h2>'
    + '<p>' + t('Rozpoznawanie twarzy i wykrywanie obiektow wymaga instalacji zaleznosci (~200MB).') + '</p>'
    + '<div class="pai-deps-grid">' + depsHtml
    + '<div class="pai-dep ' + yoloCls + '"><i class="fa-solid ' + yoloIco + '"></i> YOLOv8n model</div>'
    + '</div>'
    + '<button class="btn btn-primary pai-install-btn" onclick="_paiDoInstall(this)">'
    + '<i class="fa-solid fa-download"></i> ' + t('Zainstaluj') + '</button>'
    + '</div>';
}

async function _paiDoInstall(btn) {
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ' + t('Instalowanie...');
  var r = await api('/photos-ai/install', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); btn.disabled = false; return; }
  toast(t('Instalacja rozpoczeta. To moze potrwac kilka minut...'), 'info');
  if (NAS.socket) {
    NAS.socket.once('photos_ai_install', function(data) {
      if (data.ok) {
        toast(t('Instalacja zakonczona!'), 'success');
        var win = btn.closest('.window');
        if (win) { var bd = win.querySelector('.window-body'); if (bd) _paiInit(bd); }
      } else {
        toast(t('Blad instalacji: ') + (data.error || ''), 'error');
        btn.disabled = false;
      }
    });
  }
}

function _paiShowMain(body, st) {
  body.innerHTML = '<div class="pai-app">'
    + '<div class="pai-sidebar">'
    + '<div class="pai-sidebar-section">'
    + '<div class="pai-sidebar-title">' + t('Widoki') + '</div>'
    + '<div class="pai-nav active" data-view="dashboard"><i class="fa-solid fa-chart-pie"></i> ' + t('Podsumowanie') + '</div>'
    + '<div class="pai-nav" data-view="people"><i class="fa-solid fa-users"></i> ' + t('Osoby') + '</div>'
    + '<div class="pai-nav" data-view="smart"><i class="fa-solid fa-wand-magic-sparkles"></i> ' + t('Albumy AI') + '</div>'
    + '<div class="pai-nav" data-view="tags"><i class="fa-solid fa-tags"></i> ' + t('Tagi') + '</div>'
    + '<div class="pai-nav" data-view="search"><i class="fa-solid fa-magnifying-glass"></i> ' + t('Szukaj AI') + '</div>'
    + '<div class="pai-nav" data-view="merge"><i class="fa-solid fa-code-merge"></i> ' + t('Sugestie łączenia') + '</div>'
    + '</div>'
    + '<div class="pai-sidebar-section">'
    + '<div class="pai-sidebar-title">' + t('Skanowanie') + '</div>'
    + '<div class="pai-scan-controls" id="pai-scan-controls"></div>'
    + '<div class="pai-scan-progress" style="display:none">'
    + '<div class="pai-progress-bar"><div class="pai-progress-fill"></div></div>'
    + '<div class="pai-progress-text"></div></div>'
    + '<div class="pai-scan-settings" id="pai-scan-settings"></div>'
    + '</div></div>'
    + '<div class="pai-main" id="pai-main"></div></div>';

  body.querySelectorAll('.pai-nav').forEach(function(n) {
    n.addEventListener('click', function() {
      body.querySelectorAll('.pai-nav').forEach(function(x) { x.classList.remove('active'); });
      n.classList.add('active');
      _paiLoadView(n.dataset.view);
    });
  });

  if (NAS.socket) {
    NAS.socket.on('photos_ai_progress', _paiOnProgress);
    NAS.socket.on('photos_ai_done', _paiOnDone);
  }
  _paiRefreshScanUI(st.scanning);
  _paiLoadSettings();
  _paiLoadView('dashboard');
}

async function _paiLoadView(view) {
  var main = document.getElementById('pai-main');
  if (!main) return;
  main.innerHTML = '<div class="pai-loading"><i class="fa-solid fa-spinner fa-spin"></i></div>';
  if (view === 'dashboard') await _paiDashboard(main);
  else if (view === 'people') await _paiPeople(main);
  else if (view === 'smart') await _paiSmartAlbums(main);
  else if (view === 'tags') await _paiTags(main);
  else if (view === 'search') _paiSearchView(main);
  else if (view === 'merge') await _paiMergeSuggestions(main);
}

async function _paiDashboard(main) {
  var s = await api('/photos-ai/stats');
  var topHtml = '';
  if (s.top_tags && s.top_tags.length) {
    topHtml = '<div class="pai-top-tags"><h3>' + t('Najczesciej wykrywane') + '</h3><div class="pai-tag-chips">'
      + s.top_tags.map(function(item) { return '<span class="pai-chip">' + _paiEsc(item.tag) + ' <small>(' + item.count + ')</small></span>'; }).join('')
      + '</div></div>';
  } else {
    topHtml = '<p style="color:var(--text-secondary);margin-top:20px">' + t('Brak danych. Kliknij Skanuj aby rozpoczac.') + '</p>';
  }
  main.innerHTML = '<div class="pai-dashboard">'
    + '<h2><i class="fa-solid fa-brain" style="color:#8b5cf6"></i> ' + t('Photos AI') + '</h2>'
    + '<div class="pai-stats-grid">'
    + '<div class="pai-stat"><div class="pai-stat-val">' + (s.scanned_photos||0) + '</div><div class="pai-stat-label">' + t('Przeskanowanych') + '</div></div>'
    + '<div class="pai-stat"><div class="pai-stat-val">' + (s.faces||0) + '</div><div class="pai-stat-label">' + t('Twarzy') + '</div></div>'
    + '<div class="pai-stat"><div class="pai-stat-val">' + (s.people||0) + '</div><div class="pai-stat-label">' + t('Osob') + '</div></div>'
    + '<div class="pai-stat"><div class="pai-stat-val">' + (s.tags||0) + '</div><div class="pai-stat-label">' + t('Tagow') + '</div></div>'
    + '</div>' + topHtml + '</div>';
}

async function _paiPeople(main) {
  var d = await api('/photos-ai/people');
  if (!d.people || !d.people.length) {
    main.innerHTML = '<div class="pai-empty"><i class="fa-solid fa-users" style="font-size:40px;opacity:.4"></i>'
      + '<p>' + t('Brak rozpoznanych osob. Uruchom skanowanie.') + '</p></div>';
    return;
  }
  var allPeople = d.people;
  main.innerHTML = '<div class="pai-people-grid">' + allPeople.map(function(p) {
    var av = p.cover_face_id ? '<img src="/api/photos-ai/face-thumb/' + p.cover_face_id + '" loading="lazy">' : '<i class="fa-solid fa-user"></i>';
    return '<div class="pai-person-card" data-id="' + p.id + '">'
      + '<div class="pai-person-avatar">' + av + '</div>'
      + '<div class="pai-person-name">' + _paiEsc(p.name || t('Osoba') + ' ' + p.id) + '</div>'
      + '<div class="pai-person-count">' + p.photo_count + ' ' + t('zdjec') + '</div>'
      + '<div class="pai-person-actions">'
      + '<button class="btn btn-sm pai-merge-person-btn" title="' + t('Połącz z inną osobą') + '"><i class="fa-solid fa-code-merge"></i></button>'
      + '</div></div>';
  }).join('') + '</div>';

  main.querySelectorAll('.pai-person-card').forEach(function(card) {
    card.addEventListener('click', function() { _paiShowPersonPhotos(card.dataset.id); });
    var nameEl = card.querySelector('.pai-person-name');
    nameEl.addEventListener('dblclick', function(e) {
      e.stopPropagation();
      var pid = card.dataset.id;
      var cur = nameEl.textContent;
      var name = prompt(t('Imie osoby:'), cur);
      if (name && name !== cur) {
        api('/photos-ai/people/' + pid + '/rename', { method: 'POST', body: {name: name} });
        nameEl.textContent = name;
      }
    });
    card.querySelector('.pai-merge-person-btn').addEventListener('click', function(e) {
      e.stopPropagation();
      var pid = parseInt(card.dataset.id);
      var srcName = card.querySelector('.pai-person-name').textContent;
      var others = allPeople.filter(function(p) { return p.id !== pid; });
      _paiShowMergeModal(others, pid, srcName, main);
    });
  });
}

async function _paiShowPersonPhotos(pid) {
  var main = document.getElementById('pai-main');
  if (!main) return;
  var d = await api('/photos-ai/people/' + pid + '/photos?limit=80');
  if (!d.items || !d.items.length) { toast(t('Brak zdjec'), 'info'); return; }
  _paiRenderPhotoGrid(main, d.items, d.total);
}

async function _paiSmartAlbums(main) {
  var d = await api('/photos-ai/smart-albums');
  if (!d.albums || !d.albums.length) {
    main.innerHTML = '<div class="pai-empty"><i class="fa-solid fa-wand-magic-sparkles" style="font-size:40px;opacity:.4"></i>'
      + '<p>' + t('Brak albumow AI. Uruchom skanowanie.') + '</p></div>';
    return;
  }
  var icons = { person: 'fa-user', tag: 'fa-tag', camera: 'fa-camera' };
  main.innerHTML = '<div class="pai-albums-grid">' + d.albums.map(function(a) {
    var ico = a.type === 'person' && a.cover_face_id
      ? '<img src="/api/photos-ai/face-thumb/' + a.cover_face_id + '" class="pai-album-face">'
      : '<i class="fa-solid ' + (icons[a.type]||'fa-images') + '"></i>';
    return '<div class="pai-album-card" data-type="' + a.type + '" data-id="' + _paiEsc(a.id) + '">'
      + '<div class="pai-album-icon">' + ico + '</div>'
      + '<div class="pai-album-name">' + _paiEsc(a.name) + '</div>'
      + '<div class="pai-album-count">' + a.count + ' ' + t('zdjec') + '</div></div>';
  }).join('') + '</div>';

  main.querySelectorAll('.pai-album-card').forEach(function(card) {
    card.addEventListener('click', async function() {
      var res = await api('/photos-ai/album-photos?type=' + card.dataset.type + '&id=' + encodeURIComponent(card.dataset.id));
      if (res.items) _paiRenderPhotoGrid(main, res.items, res.total);
    });
  });
}

async function _paiTags(main) {
  var d = await api('/photos-ai/tags');
  if (!d.tags || !d.tags.length) {
    main.innerHTML = '<div class="pai-empty"><i class="fa-solid fa-tags" style="font-size:40px;opacity:.4"></i>'
      + '<p>' + t('Brak tagow.') + '</p></div>';
    return;
  }
  main.innerHTML = '<div class="pai-tags-view"><h3>' + t('Wszystkie tagi') + '</h3><div class="pai-tag-chips">'
    + d.tags.map(function(item) {
      return '<span class="pai-chip pai-chip-click" data-tag="' + _paiEsc(item.tag) + '">'
        + _paiEsc(item.tag_pl || item.tag) + ' <small>(' + item.count + ')</small></span>';
    }).join('') + '</div></div>';
  main.querySelectorAll('.pai-chip-click').forEach(function(chip) {
    chip.addEventListener('click', async function() {
      var r = await api('/photos-ai/album-photos?type=tag&id=' + encodeURIComponent(chip.dataset.tag));
      if (r.items) _paiRenderPhotoGrid(main, r.items, r.total);
    });
  });
}

function _paiSearchView(main) {
  main.innerHTML = '<div class="pai-search-view">'
    + '<div class="pai-search-box"><i class="fa-solid fa-magnifying-glass"></i>'
    + '<input type="text" class="pai-search-input" placeholder="' + t('Szukaj: pies, kot, osoba...') + '"></div>'
    + '<div class="pai-search-results"></div></div>';
  var timer;
  var input = main.querySelector('.pai-search-input');
  input.addEventListener('input', function(e) {
    clearTimeout(timer);
    timer = setTimeout(async function() {
      var q = e.target.value.trim();
      var res = main.querySelector('.pai-search-results');
      if (!q) { res.innerHTML = ''; return; }
      var d = await api('/photos-ai/search?q=' + encodeURIComponent(q));
      if (!d.items || !d.items.length) { res.innerHTML = '<p style="color:var(--text-secondary)">' + t('Brak wynikow.') + '</p>'; return; }
      _paiRenderPhotoGrid(res, d.items, d.total);
    }, 400);
  });
  input.focus();
}

function _paiRenderPhotoGrid(container, items, total) {
  var backBtn = container.id === 'pai-main'
    ? '<button class="btn btn-sm pai-back-btn" onclick="_paiGoBack()"><i class="fa-solid fa-arrow-left"></i> ' + t('Wstecz') + '</button>' : '';
  var header = backBtn ? '<div style="padding:10px">' + backBtn + ' <span class="pai-grid-count">' + total + ' ' + t('zdjec') + '</span></div>' : '';
  var grid = '<div class="pai-photo-grid">' + items.map(function(item) {
    var thumb = '/api/files/preview?path=' + encodeURIComponent(item.path) + '&w=200&h=200';
    return '<div class="pai-photo-item" data-path="' + _paiEsc(item.path) + '">'
      + '<img src="' + thumb + '" loading="lazy"></div>';
  }).join('') + '</div>';
  container.innerHTML = header + grid;

  container.querySelectorAll('.pai-photo-item').forEach(function(item) {
    item.addEventListener('click', function() { _paiOpenInGallery(item.dataset.path); });
  });
}

function _paiOpenInGallery(path) {
  var folder = path.replace(/\/[^/]+$/, '') || '/';
  if (typeof openApp === 'function') {
    var galApp = null;
    if (NAS.apps) galApp = NAS.apps.find(function(a) { return a.id === 'gallery'; });
    if (galApp) { openApp(galApp, { folder: folder, file: path }); return; }
  }
  toast(t('Zainstaluj Galerie aby otworzyc zdjecia.'), 'info');
}

function _paiShowMergeModal(people, sourcePid, sourceName, container) {
  var existing = document.getElementById('pai-merge-modal');
  if (existing) existing.remove();
  var modal = document.createElement('div');
  modal.id = 'pai-merge-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div style="position:absolute;inset:0;background:rgba(0,0,0,.6)" class="pai-merge-backdrop"></div>'
    + '<div style="position:relative;background:var(--bg-secondary);border-radius:8px;padding:20px;width:380px;max-width:95vw;max-height:80vh;display:flex;flex-direction:column;gap:12px">'
    + '<h3 style="margin:0"><i class="fa-solid fa-code-merge"></i> ' + t('Połącz z…') + '</h3>'
    + '<p style="margin:0;font-size:.85em;opacity:.7">' + _paiEsc(sourceName) + ' ' + t('zostanie usunięta i scalone z wybraną osobą.') + '</p>'
    + '<div style="overflow-y:auto;max-height:320px;display:flex;flex-direction:column;gap:6px" class="pai-merge-list">'
    + people.map(function(p) {
        var av = p.cover_face_id ? '<img src="/api/photos-ai/face-thumb/' + p.cover_face_id + '" style="width:36px;height:36px;border-radius:50%;object-fit:cover" alt="">' : '<i class="fa-solid fa-user" style="font-size:24px;opacity:.5"></i>';
        return '<div class="pai-merge-option" data-id="' + p.id + '" data-name="' + _paiEsc(p.name || t('Osoba') + ' ' + p.id) + '" style="display:flex;align-items:center;gap:10px;padding:8px;border-radius:6px;cursor:pointer;transition:background .15s" onmouseover="this.style.background=\'var(--bg-tertiary)\'" onmouseout="this.style.background=\'\'">'
          + '<div style="width:36px;height:36px;display:flex;align-items:center;justify-content:center;flex-shrink:0">' + av + '</div>'
          + '<div><div style="font-weight:600">' + _paiEsc(p.name || t('Osoba') + ' ' + p.id) + '</div>'
          + '<div style="font-size:.8em;opacity:.6">' + (p.photo_count || 0) + ' ' + t('zdjec') + '</div></div></div>';
      }).join('')
    + '</div>'
    + '<button class="btn btn-sm pai-merge-cancel">' + t('Anuluj') + '</button>'
    + '</div>';
  document.body.appendChild(modal);

  modal.querySelectorAll('.pai-merge-option').forEach(function(el) {
    el.addEventListener('click', async function() {
      var targetPid = parseInt(el.dataset.id);
      var targetName = el.dataset.name;
      modal.remove();
      var r = await api('/photos-ai/people/merge', { method: 'POST', body: { source_id: sourcePid, target_id: targetPid } });
      if (r.ok) {
        toast(t('Połączono z „{name}"', { name: targetName }), 'success');
        _paiLoadView('people');
      } else {
        toast(r.error || t('Błąd'), 'error');
      }
    });
  });
  modal.querySelector('.pai-merge-cancel').addEventListener('click', function() { modal.remove(); });
  modal.querySelector('.pai-merge-backdrop').addEventListener('click', function() { modal.remove(); });
}

function _paiGoBack() {
  var active = document.querySelector('.pai-nav.active');
  if (active) _paiLoadView(active.dataset.view);
}

async function _paiRefreshScanUI(scanning) {
  var ctrl = document.getElementById('pai-scan-controls');
  if (!ctrl) return;
  // Fetch current state from server
  var st = null;
  try { st = await api('/photos-ai/scan-status'); } catch(e) {}
  var running = (st && st.running) || scanning;
  var paused = st && st.paused;
  if (running && paused) {
    ctrl.innerHTML =
      '<div class="pai-scan-status"><i class="fa-solid fa-pause" style="color:var(--warning-color)"></i> ' + t('Wstrzymano') + '</div>'
      + '<div class="pai-scan-btn-row">'
      + '<button class="btn btn-sm btn-primary" onclick="_paiResumeScan()"><i class="fa-solid fa-play"></i> ' + t('Wznów') + '</button>'
      + '<button class="btn btn-sm btn-danger" onclick="_paiStopScan()"><i class="fa-solid fa-stop"></i> ' + t('Zatrzymaj') + '</button>'
      + '</div>';
    var prog = document.querySelector('.pai-scan-progress');
    if (prog) prog.style.display = 'block';
  } else if (running) {
    ctrl.innerHTML =
      '<div class="pai-scan-status"><i class="fa-solid fa-spinner fa-spin" style="color:var(--accent-color)"></i> ' + t('Skanowanie...') + '</div>'
      + '<div class="pai-scan-btn-row">'
      + '<button class="btn btn-sm btn-warning" onclick="_paiPauseScan()"><i class="fa-solid fa-pause"></i> ' + t('Pauza') + '</button>'
      + '<button class="btn btn-sm btn-danger" onclick="_paiStopScan()"><i class="fa-solid fa-stop"></i> ' + t('Zatrzymaj') + '</button>'
      + '</div>';
    var prog = document.querySelector('.pai-scan-progress');
    if (prog) prog.style.display = 'block';
  } else {
    ctrl.innerHTML =
      '<div class="pai-scan-btn-row">'
      + '<button class="btn btn-sm btn-primary" onclick="_paiStartScan()"><i class="fa-solid fa-satellite-dish"></i> ' + t('Skanuj') + '</button>'
      + '<button class="btn btn-sm" onclick="_paiRescanFresh()" title="' + t('Wyczyść historię i skanuj od nowa') + '"><i class="fa-solid fa-arrows-rotate"></i> ' + t('Od nowa') + '</button>'
      + '</div>';
    var prog = document.querySelector('.pai-scan-progress');
    if (prog) prog.style.display = 'none';
  }
}

async function _paiLoadSettings() {
  var el = document.getElementById('pai-scan-settings');
  if (!el) return;
  var d = await api('/photos-ai/ai-settings');
  var auto = d.auto_scan !== false;
  el.innerHTML =
    '<label class="pai-setting-row" title="' + t('Automatycznie wznawiaj skanowanie po restarcie serwera') + '">'
    + '<input type="checkbox" id="pai-auto-scan" ' + (auto ? 'checked' : '') + '>'
    + '<span>' + t('Auto-skan po restarcie') + '</span></label>';
  document.getElementById('pai-auto-scan').addEventListener('change', async function() {
    await api('/photos-ai/ai-settings', { method: 'POST', body: { auto_scan: this.checked } });
    toast(t('Ustawienie zapisane'), 'success');
  });
}

async function _paiStartScan() {
  var r = await api('/photos-ai/scan', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  _paiRefreshScanUI(true);
}

async function _paiPauseScan() {
  var r = await api('/photos-ai/pause-scan', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  _paiRefreshScanUI(true);
}

async function _paiResumeScan() {
  var r = await api('/photos-ai/resume-scan', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  _paiRefreshScanUI(true);
}

async function _paiStopScan() {
  var r = await api('/photos-ai/stop-scan', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  _paiRefreshScanUI(false);
}

async function _paiRescanFresh() {
  if (!confirm(t('Czy na pewno chcesz usunąć historię skanowania i skanować wszystkie zdjęcia od nowa?'))) return;
  var r = await api('/photos-ai/rescan', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  toast(t('Reskan od nowa rozpoczęty'), 'success');
  _paiRefreshScanUI(true);
}

function _paiOnProgress(data) {
  var fill = document.querySelector('.pai-progress-fill');
  var txt = document.querySelector('.pai-progress-text');
  if (fill && data.total > 0) fill.style.width = Math.round(data.processed / data.total * 100) + '%';
  if (txt) txt.textContent = data.processed + '/' + data.total + ' | ' + t('Twarzy') + ': ' + data.faces_found + ' | ' + t('Tagow') + ': ' + data.tags_found;
}

function _paiOnDone(data) {
  _paiRefreshScanUI(false);
  toast(t('Skanowanie zakonczone') + ': ' + data.total_processed + ' ' + t('zdjec') + ', '
    + data.faces + ' ' + t('twarzy') + ', ' + data.tags + ' ' + t('tagow') + ', '
    + data.people + ' ' + t('osob') + ' (' + data.duration + 's)', 'success');
  var active = document.querySelector('.pai-nav.active');
  if (active) _paiLoadView(active.dataset.view);
}

function _paiPollScan() {
  _paiRefreshScanUI(true);
}

function _paiEsc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function _paiMergeSuggestions(main) {
  var d = await api('/photos-ai/merge-suggestions');
  var suggestions = d.suggestions || [];
  if (!suggestions.length) {
    main.innerHTML = '<div class="pai-empty"><i class="fa-solid fa-code-merge" style="font-size:40px"></i>'
      + '<p>' + t('Brak sugestii łączenia. Wszystkie klastry wyglądają na unikalne.') + '</p></div>';
    return;
  }
  main.innerHTML = '<h2 style="margin:0 0 6px;display:flex;align-items:center;gap:10px"><i class="fa-solid fa-code-merge" style="color:#8b5cf6"></i> '
    + t('Sugestie łączenia osób') + '</h2>'
    + '<p style="color:var(--text-secondary);margin:0 0 16px;font-size:13px">'
    + t('Te osoby mogą być tą samą osobą. Kliknij Połącz aby scalić.') + '</p>'
    + '<div class="pai-merge-list">' + suggestions.map(function(s, i) {
    var aImg = s.person_a.cover_face_id ? '<img src="/api/photos-ai/face-thumb/' + s.person_a.cover_face_id + '">' : '<i class="fa-solid fa-user" style="font-size:28px;color:var(--text-secondary)"></i>';
    var bImg = s.person_b.cover_face_id ? '<img src="/api/photos-ai/face-thumb/' + s.person_b.cover_face_id + '">' : '<i class="fa-solid fa-user" style="font-size:28px;color:var(--text-secondary)"></i>';
    return '<div class="pai-merge-card" data-idx="' + i + '">'
      + '<div class="pai-merge-pair">'
      + '<div class="pai-merge-person"><div class="pai-merge-avatar">' + aImg + '</div>'
      + '<div class="pai-merge-name">' + _paiEsc(s.person_a.name) + '</div></div>'
      + '<div class="pai-merge-arrow"><i class="fa-solid fa-arrows-left-right"></i>'
      + '<div class="pai-merge-conf">' + s.confidence + '%</div></div>'
      + '<div class="pai-merge-person"><div class="pai-merge-avatar">' + bImg + '</div>'
      + '<div class="pai-merge-name">' + _paiEsc(s.person_b.name) + '</div></div>'
      + '</div>'
      + '<div class="pai-merge-actions">'
      + '<button class="btn btn-sm btn-primary pai-merge-btn" data-src="' + s.person_a.id + '" data-tgt="' + s.person_b.id + '">'
      + '<i class="fa-solid fa-code-merge"></i> ' + t('Połącz') + '</button>'
      + '<button class="btn btn-sm pai-merge-skip">' + t('Pomiń') + '</button>'
      + '</div></div>';
  }).join('') + '</div>';

  main.querySelectorAll('.pai-merge-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      var card = btn.closest('.pai-merge-card');
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      var r = await api('/photos-ai/people/merge', {
        method: 'POST',
        body: { source_id: parseInt(btn.dataset.src), target_id: parseInt(btn.dataset.tgt) },
      });
      if (r.ok) {
        card.style.opacity = '0.3';
        card.style.pointerEvents = 'none';
        toast(t('Połączono!'), 'success');
      } else {
        toast(r.error || t('Błąd'), 'error');
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-code-merge"></i> ' + t('Połącz');
      }
    });
  });

  main.querySelectorAll('.pai-merge-skip').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var card = btn.closest('.pai-merge-card');
      card.style.display = 'none';
    });
  });
}
