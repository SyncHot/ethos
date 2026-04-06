/* ═══════════════════════════════════════════════════════════════════
   EthOS  —  Gallery  (state-of-the-art photo & video gallery)
   ═══════════════════════════════════════════════════════════════════ */
AppRegistry['gallery'] = function (appDef, launchOpts) {
  const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
      ? NAS.logClient('gallery', level, msg, details) : console.log('[gallery]', msg, details || '');

  createWindow('gallery', {
    title: t('Galeria'),
    icon: 'fa-solid fa-images',
    iconColor: '#ec4899',
    width: 1280,
    height: 820,
    onRender: body => renderGallery(body, launchOpts),
  });
};

/* ━━━━  CONSTANTS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _galThumbSize() {
  const w = window.innerWidth;
  if (w <= 480) return 150;
  if (w <= 768) return 200;
  return 280;
}
const GAL = {
  PAGE: 80,
  THUMB_SIZE: _galThumbSize(),
  items: [],
  total: 0,
  offset: 0,
  loading: false,
  view: 'grid',        // grid | albums | timeline
  sort: 'date_desc',
  type: 'all',
  folder: '',
  subfolder: '',
  query: '',
  monthFilter: '',
  lightboxIdx: -1,
  lightboxItems: [],
  root: null,
  gridEl: null,
  albumsList: [],
  timelineList: [],
  slideshowTimer: null,
  gallerySources: [],
  selectMode: false,
  selected: new Set(),
};

/* ━━━━  ENTRY  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function renderGallery(body, launchOpts) {
  const initFolder = launchOpts?.folder || '';
  const initFile = launchOpts?.file || null;
  GAL.root = body;
  GAL.items = [];
  GAL.offset = 0;
  GAL.total = 0;
  GAL.view = 'grid';
  GAL.sort = 'date_desc';
  GAL.type = 'all';
  GAL.folder = initFolder || '';
  GAL.subfolder = '';
  GAL.query = '';
  GAL.monthFilter = '';
  GAL.lightboxIdx = -1;

  body.innerHTML = `
    <div class="gal-app">
      <div class="gal-sidebar">
        <div class="gal-sidebar-section">
          <div class="gal-sidebar-title">${t('Widoki')}</div>
          <div class="gal-nav-item active" data-view="grid">
            <i class="fa-solid fa-grid-2"></i> ${t('Wszystkie')}
          </div>
          <div class="gal-nav-item" data-view="albums">
            <i class="fa-solid fa-folder-image"></i> ${t('Albumy')}
          </div>
          <div class="gal-nav-item" data-view="timeline">
            <i class="fa-solid fa-timeline"></i> ${t('Oś czasu')}
          </div>
          <div class="gal-nav-item" data-view="favorites">
            <i class="fa-solid fa-star"></i> ${t('Ulubione')}
          </div>
          <div class="gal-nav-item" data-view="map">
            <i class="fa-solid fa-map-location-dot"></i> ${t('Mapa')}
          </div>
          <div class="gal-nav-item" data-view="people">
            <i class="fa-solid fa-users"></i> ${t('Osoby')}
          </div>
          <div class="gal-nav-item" data-view="tags">
            <i class="fa-solid fa-tags"></i> ${t('Tagi AI')}
          </div>
          <div class="gal-nav-item" data-view="smart">
            <i class="fa-solid fa-wand-magic-sparkles"></i> ${t('Albumy AI')}
          </div>
          <div class="gal-nav-item" data-view="search-ai">
            <i class="fa-solid fa-brain"></i> ${t('Szukaj AI')}
          </div>
          <div class="gal-nav-item" data-view="merge">
            <i class="fa-solid fa-code-merge"></i> ${t('Łączenie')}
          </div>
        </div>
        <div class="gal-sidebar-section">
          <div class="gal-sidebar-title">${t('Typ')}</div>
          <div class="gal-type-item active" data-type="all">${t('Wszystko')}</div>
          <div class="gal-type-item" data-type="image">${t('Zdjęcia')}</div>
          <div class="gal-type-item" data-type="video">${t('Filmy')}</div>
        </div>
        <div class="gal-sidebar-section gal-sources-section">
          <div class="gal-sidebar-title">
            ${t('Foldery źródłowe')}
            <button class="gal-add-folder-btn" title="${t('Dodaj folder')}"><i class="fa-solid fa-plus"></i></button>
          </div>
          <div class="gal-sources-list"></div>
        </div>
        <div class="gal-sidebar-section gal-custom-albums-section">
          <div class="gal-sidebar-title">
            ${t('Albumy własne')}
            <button class="gal-add-album-btn" title="${t('Nowy album')}"><i class="fa-solid fa-plus"></i></button>
          </div>
          <div class="gal-custom-albums-list"></div>
        </div>
        <div class="gal-sidebar-section gal-ai-section">
          <div class="gal-sidebar-title"><i class="fa-solid fa-brain"></i> ${t('AI')}</div>
          <div class="gal-ai-controls" id="gal-ai-controls"></div>
          <div class="gal-ai-progress" style="display:none">
            <div class="gal-ai-progress-bar"><div class="gal-ai-progress-fill"></div></div>
            <div class="gal-ai-progress-text"></div>
          </div>
        </div>
      </div>
      <div class="gal-main">
        <div class="gal-toolbar">
          <div class="gal-toolbar-left">
            <div class="gal-search-box">
              <i class="fa-solid fa-magnifying-glass"></i>
              <input type="text" class="gal-search" placeholder="${t('Szukaj…')}">
            </div>
            <span class="gal-count"></span>
          </div>
          <div class="gal-toolbar-right">
            <button class="gal-btn gal-upload-btn" title="${t('Prześlij pliki')}"><i class="fa-solid fa-cloud-arrow-up"></i></button>
            <button class="gal-btn gal-select-toggle" title="${t('Tryb zaznaczania')}"><i class="fa-solid fa-check-double"></i></button>
            <button class="gal-btn gal-stats-btn" title="${t('Statystyki')}"><i class="fa-solid fa-chart-pie"></i></button>
            <select class="gal-sort">
              <option value="date_desc">${t('Najnowsze')}</option>
              <option value="date_asc">${t('Najstarsze')}</option>
              <option value="name">${t('Nazwa')}</option>
              <option value="size">${t('Rozmiar')}</option>
            </select>
            <button class="gal-btn gal-slideshow-btn" title="${t('Pokaz slajdów')}"><i class="fa-solid fa-play"></i></button>
          </div>
        </div>
        <div class="gal-content">
          <div class="gal-grid"></div>
          <div class="gal-albums" style="display:none"></div>
          <div class="gal-timeline-view" style="display:none"></div>
          <div class="gal-favorites-view" style="display:none"></div>
          <div class="gal-map-view" style="display:none"></div>
          <div class="gal-people-view" style="display:none"></div>
          <div class="gal-tags-view" style="display:none"></div>
          <div class="gal-smart-view" style="display:none"></div>
          <div class="gal-searchai-view" style="display:none"></div>
          <div class="gal-merge-view" style="display:none"></div>
          <div class="gal-empty" style="display:none">
            <i class="fa-solid fa-images"></i>
            <p>${t('Brak mediów')}</p>
            <p class="gal-empty-hint">${t('Dodaj foldery źródłowe, aby wyświetlić galerię')}</p>
          </div>
          <div class="gal-loader" style="display:none">
            <div class="gal-spinner"></div>
          </div>
        </div>
      </div>
    </div>
  `;

  GAL.gridEl = body.querySelector('.gal-grid');

  // Wire events
  body.querySelectorAll('.gal-nav-item').forEach(el => {
    el.addEventListener('click', () => {
      GAL.monthFilter = '';
      _galSetView(el.dataset.view);
    });
  });
  body.querySelectorAll('.gal-type-item').forEach(el => {
    el.addEventListener('click', () => _galSetType(el.dataset.type));
  });
  body.querySelector('.gal-sort').addEventListener('change', e => {
    GAL.sort = e.target.value;
    _galReload();
  });
  let searchTimer;
  body.querySelector('.gal-search').addEventListener('input', e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      GAL.query = e.target.value.trim();
      _galReload();
    }, 300);
  });
  body.querySelector('.gal-add-folder-btn').addEventListener('click', _galShowFolderPicker);
  body.querySelector('.gal-slideshow-btn').addEventListener('click', _galStartSlideshow);
  body.querySelector('.gal-stats-btn').addEventListener('click', _galShowStats);
  body.querySelector('.gal-upload-btn').addEventListener('click', _galUploadClick);
  body.querySelector('.gal-select-toggle').addEventListener('click', () => {
    GAL.selectMode = !GAL.selectMode;
    GAL.selected.clear();
    body.querySelectorAll('.gal-card-select').forEach(el => el.style.display = GAL.selectMode ? '' : 'none');
    body.querySelectorAll('.gal-card-check').forEach(cb => cb.checked = false);
    _galUpdateBatchBar();
  });
  body.querySelector('.gal-add-album-btn').addEventListener('click', _galCreateCustomAlbum);

  // Ctrl+A / Escape for multi-select
  body.addEventListener('keydown', e => {
    if (e.ctrlKey && e.key === 'a' && GAL.selectMode) {
      e.preventDefault();
      GAL.items.forEach(item => GAL.selected.add(item.path));
      body.querySelectorAll('.gal-card-check').forEach(cb => cb.checked = true);
      _galUpdateBatchBar();
    }
    if (e.key === 'Escape' && GAL.selectMode) {
      GAL.selectMode = false;
      GAL.selected.clear();
      body.querySelectorAll('.gal-card-select').forEach(el => el.style.display = 'none');
      body.querySelectorAll('.gal-card-check').forEach(cb => cb.checked = false);
      _galUpdateBatchBar();
    }
  });

  // Infinite scroll
  const content = body.querySelector('.gal-content');
  content.addEventListener('scroll', () => {
    if (GAL.loading || GAL.view !== 'grid') return;
    if (content.scrollTop + content.clientHeight >= content.scrollHeight - 400) {
      if (GAL.offset + GAL.PAGE < GAL.total) {
        GAL.offset += GAL.PAGE;
        _galLoadMore();
      }
    }
  });

  // AI scan controls + socket progress
  if (NAS.socket) {
    NAS.socket.on('photos_ai_progress', _galOnAiProgress);
    NAS.socket.on('photos_ai_done', _galOnAiDone);
  }
  _galRefreshAiControls();

  // Load sources then initial data
  await _galLoadSources();
  _galLoadCustomAlbums();
  await _galReload();
  _galInitDragDrop();
  
  if (initFile) {
      const idx = GAL.items.findIndex(i => i.path === initFile);
      if (idx !== -1) _galOpenLightbox(idx);
  }
}

/* ━━━━  SOURCES  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _galLoadSources() {
  try {
    const data = await api('/gallery/folders');
    GAL.gallerySources = data;
    _galRenderSources();
  } catch (e) {
    GAL.gallerySources = [];
    _galRenderSources();
  }
}

function _galRenderSources() {
  const list = GAL.root.querySelector('.gal-sources-list');
  if (!GAL.gallerySources.length) {
    list.innerHTML = `<div class="gal-no-sources">${t('Brak folderów')}</div>`;
    return;
  }
  list.innerHTML = GAL.gallerySources.map(s => `
    <div class="gal-source-item" data-path="${_esc(s.path)}">
      <div class="gal-source-info">
        <i class="fa-solid fa-folder${s.exists ? '' : '-xmark'}" style="color:${s.exists ? '#f59e0b' : '#ef4444'}"></i>
        <span class="gal-source-label" title="${_esc(s.path)}">${_esc(s.label)}</span>
        <span class="gal-source-count">${s.media_count || 0}</span>
      </div>
      <button class="gal-source-del" title="${t('Usuń')}"><i class="fa-solid fa-xmark"></i></button>
    </div>
  `).join('');

  list.querySelectorAll('.gal-source-del').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      const path = btn.closest('.gal-source-item').dataset.path;
      confirmDialog(t('Usunąć folder źródłowy z galerii?') + `<br><small>${_esc(path)}</small>`, async () => {
        await api('/gallery/folders', { method: 'DELETE', body: { path } });
        toast(t('Folder usunięty z galerii'), 'info');
        await _galLoadSources();
        _galReload();
      });
    });
  });

  list.querySelectorAll('.gal-source-item').forEach(el => {
    el.addEventListener('click', () => {
      const path = el.dataset.path;
      if (GAL.folder === path) {
        GAL.folder = '';
        el.classList.remove('selected');
      } else {
        list.querySelectorAll('.gal-source-item').forEach(x => x.classList.remove('selected'));
        el.classList.add('selected');
        GAL.folder = path;
      }
      _galReload();
    });
  });
}

async function _galShowFolderPicker() {
  const modal = document.createElement('div');
  modal.className = 'gal-folder-modal-overlay';
  let currentPath = '/';

  async function render() {
    try {
      const data = await api(`/gallery/browse?path=${encodeURIComponent(currentPath)}`);
      modal.innerHTML = `
        <div class="gal-folder-modal">
          <div class="gal-folder-modal-header">
            <h3><i class="fa-solid fa-folder-plus"></i> Dodaj folder do galerii</h3>
            <button class="gal-folder-modal-close"><i class="fa-solid fa-xmark"></i></button>
          </div>
          <div class="gal-folder-modal-path">
            <i class="fa-solid fa-folder-open"></i> ${_esc(currentPath)}
            ${data.media_count ? `<span class="gal-folder-media-count">${data.media_count} ${t('mediów')}</span>` : ''}
          </div>
          <div class="gal-folder-modal-list">
            ${currentPath !== '/' ? '<div class="gal-folder-modal-item gal-folder-parent" data-path=".."><i class="fa-solid fa-arrow-up"></i> ..</div>' : ''}
            ${data.folders.map(f => `
              <div class="gal-folder-modal-item" data-path="${_esc(currentPath === '/' ? '/' + f : currentPath + '/' + f)}">
                <i class="fa-solid fa-folder" style="color:#f59e0b"></i> ${_esc(f)}
              </div>
            `).join('')}
            ${!data.folders.length && currentPath === '/' ? `<div class="gal-folder-modal-empty">${t('Brak folderów')}</div>` : ''}
          </div>
          <div class="gal-folder-modal-footer">
            <button class="gal-btn gal-folder-add-btn ${data.is_gallery ? 'disabled' : ''}">
              <i class="fa-solid fa-plus"></i> ${data.is_gallery ? t('Już dodany') : 'Dodaj ten folder'}
            </button>
          </div>
        </div>
      `;

      modal.querySelector('.gal-folder-modal-close').onclick = () => modal.remove();
      modal.querySelector('.gal-folder-modal').addEventListener('click', e => e.stopPropagation());
      modal.addEventListener('click', () => modal.remove());

      modal.querySelectorAll('.gal-folder-modal-item').forEach(el => {
        el.addEventListener('click', () => {
          const p = el.dataset.path;
          if (p === '..') {
            currentPath = currentPath.split('/').slice(0, -1).join('/') || '/';
          } else {
            currentPath = p;
          }
          render();
        });
      });

      const addBtn = modal.querySelector('.gal-folder-add-btn');
      if (!data.is_gallery) {
        addBtn.addEventListener('click', async () => {
          const label = await promptDialog(t('Nazwa folderu w galerii:'), currentPath.split('/').pop() || currentPath);
          if (label === null) return;
          await api('/gallery/folders', {
            method: 'POST',
            body: { path: currentPath, label: label || undefined }
          });
          toast(t('Folder dodany do galerii!'), 'success');
          modal.remove();
          await _galLoadSources();
          _galReload();
        });
      }
    } catch (err) {
      toast(t('Błąd ładowania folderów: ') + err.message, 'error');
    }
  }

  document.body.appendChild(modal);
  render();
}

/* ━━━━  VIEW SWITCHING  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _galSetView(view) {
  GAL.view = view;
  GAL.subfolder = '';
  if (!GAL.monthFilter) GAL.monthFilter = '';
  GAL.root.querySelectorAll('.gal-nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === view));
  const viewMap = {
    'grid':'grid', 'albums':'albums', 'timeline-view':'timeline', 'favorites-view':'favorites',
    'map-view':'map', 'people-view':'people', 'tags-view':'tags', 'smart-view':'smart',
    'searchai-view':'search-ai', 'merge-view':'merge',
  };
  Object.entries(viewMap).forEach(([cls, v]) => {
    const el = GAL.root.querySelector('.gal-' + cls);
    if (el) el.style.display = v === view ? '' : 'none';
  });

  if (view === 'grid') _galReload();
  else if (view === 'albums') _galLoadAlbums();
  else if (view === 'timeline') _galLoadTimeline();
  else if (view === 'favorites') _galLoadFavorites();
  else if (view === 'map') _galLoadMap();
  else if (view === 'people') _galLoadPeople();
  else if (view === 'tags') _galLoadTags();
  else if (view === 'smart') _galLoadSmartAlbums();
  else if (view === 'search-ai') _galLoadSearchAi();
  else if (view === 'merge') _galLoadMerge();
}

function _galSetType(type) {
  GAL.type = type;
  GAL.root.querySelectorAll('.gal-type-item').forEach(el => el.classList.toggle('active', el.dataset.type === type));
  _galReload();
}

/* ━━━━  GRID VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _galReload() {
  GAL.offset = 0;
  GAL.items = [];
  GAL.gridEl.innerHTML = '';
  _galShowLoader(true);
  await _galLoadMore();
  _galShowLoader(false);
}

async function _galLoadMore() {
  if (GAL.loading) return;
  GAL.loading = true;
  _galShowLoader(true);

  try {
    const params = new URLSearchParams({
      offset: GAL.offset,
      limit: GAL.PAGE,
      sort: GAL.sort,
      type: GAL.type,
    });
    if (GAL.folder) params.set('folder', GAL.folder);
    if (GAL.subfolder) params.set('subfolder', GAL.subfolder);
    if (GAL.query) params.set('q', GAL.query);
    if (GAL.monthFilter) params.set('month', GAL.monthFilter);

    const data = await api(`/gallery/scan?${params}`);
    GAL.total = data.total;
    GAL.items.push(...data.items);
    _galRenderItems(data.items, GAL.items.length - data.items.length);
    _galUpdateCount();
  } catch (err) {
    toast(t('Błąd ładowania galerii: ') + err.message, 'error');
  }

  GAL.loading = false;
  _galShowLoader(false);
  _galCheckEmpty();
}

function _galRenderItems(items, startIdx) {
  const frag = document.createDocumentFragment();
  items.forEach((item, i) => {
    const idx = startIdx + i;
    const card = document.createElement('div');
    card.className = 'gal-card';
    card.dataset.idx = idx;

    const isVideo = item.type === 'video';
    const thumbUrl = isVideo
      ? `/api/gallery/video-thumb?path=${encodeURIComponent(item.path)}`
      : `/api/files/preview?path=${encodeURIComponent(item.path)}&w=${GAL.THUMB_SIZE}&h=${GAL.THUMB_SIZE}`;

    card.innerHTML = `
      <div class="gal-card-img-wrap">
        <img loading="lazy" src="${thumbUrl}" alt="${_esc(item.name)}">
        ${isVideo ? '<div class="gal-video-badge"><i class="fa-solid fa-play"></i></div>' : ''}
        <div class="gal-card-select" style="display:${GAL.selectMode ? '' : 'none'}">
          <input type="checkbox" class="gal-card-check" ${GAL.selected.has(item.path) ? 'checked' : ''}>
        </div>
        <div class="gal-card-overlay">
          <span class="gal-card-name">${_esc(item.name)}</span>
        </div>
      </div>
    `;

    const checkbox = card.querySelector('.gal-card-check');
    if (checkbox) {
      checkbox.addEventListener('click', e => {
        e.stopPropagation();
        if (checkbox.checked) GAL.selected.add(item.path);
        else GAL.selected.delete(item.path);
        _galUpdateBatchBar();
      });
    }

    card.addEventListener('click', () => _galOpenLightbox(idx));
    frag.appendChild(card);
  });
  GAL.gridEl.appendChild(frag);
}

function _galUpdateCount() {
  const el = GAL.root.querySelector('.gal-count');
  if (el) el.textContent = `${GAL.total} ${GAL.total === 1 ? 'element' : t('elementów')}`;
}

function _galCheckEmpty() {
  const empty = GAL.root.querySelector('.gal-empty');
  if (GAL.view === 'grid' && GAL.total === 0 && !GAL.loading) {
    empty.style.display = 'flex';
    GAL.gridEl.style.display = 'none';
  } else {
    empty.style.display = 'none';
    if (GAL.view === 'grid') GAL.gridEl.style.display = '';
  }
}

function _galShowLoader(show) {
  const loader = GAL.root.querySelector('.gal-loader');
  if (loader) loader.style.display = show ? 'flex' : 'none';
}

/* ━━━━  ALBUMS VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _galLoadAlbums() {
  _galShowLoader(true);
  try {
    const data = await api('/gallery/albums');
    GAL.albumsList = data;
    const container = GAL.root.querySelector('.gal-albums');
    if (!data.length) {
      container.innerHTML = `<div class="gal-empty" style="display:flex"><i class="fa-solid fa-folder-open"></i><p>${t('Brak albumów')}</p></div>`;
      _galShowLoader(false);
      return;
    }
    container.innerHTML = data.map(album => `
      <div class="gal-album-card" data-path="${_esc(album.path)}">
        <div class="gal-album-cover">
          <img loading="lazy" src="/api/files/preview?path=${encodeURIComponent(album.cover)}&w=400&h=300" alt="${_esc(album.name)}">
          <div class="gal-album-info">
            <div class="gal-album-name">${_esc(album.name)}</div>
            <div class="gal-album-count">${album.count} ${album.count === 1 ? 'element' : t('elementów')}</div>
          </div>
        </div>
      </div>
    `).join('');

    container.querySelectorAll('.gal-album-card').forEach(el => {
      el.addEventListener('click', () => {
        GAL.subfolder = el.dataset.path;
        GAL.folder = '';
        GAL.view = 'grid';
        GAL.root.querySelectorAll('.gal-nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === 'grid'));
        GAL.root.querySelector('.gal-grid').style.display = '';
        GAL.root.querySelector('.gal-albums').style.display = 'none';
        _galReload();
      });
    });
  } catch (err) {
    toast(t('Błąd ładowania albumów: ') + err.message, 'error');
  }
  _galShowLoader(false);
}

/* ━━━━  TIMELINE VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
const MONTH_NAMES = ['', t('Styczeń'), t('Luty'), t('Marzec'), t('Kwiecień'), t('Maj'), t('Czerwiec'),
                     t('Lipiec'), t('Sierpień'), t('Wrzesień'), t('Październik'), t('Listopad'), t('Grudzień')];

async function _galLoadTimeline() {
  _galShowLoader(true);
  try {
    const data = await api('/gallery/timeline');
    GAL.timelineList = data;
    const container = GAL.root.querySelector('.gal-timeline-view');
    if (!data.length) {
      container.innerHTML = `<div class="gal-empty" style="display:flex"><i class="fa-solid fa-timeline"></i><p>${t('Brak danych')}</p></div>`;
      _galShowLoader(false);
      return;
    }
    container.innerHTML = data.map(g => `
      <div class="gal-timeline-group" data-key="${g.key}">
        <div class="gal-timeline-header">
          <div class="gal-timeline-dot"></div>
          <h3>${MONTH_NAMES[g.month]} ${g.year}</h3>
          <span class="gal-timeline-count">${g.count} ${t('elementów')}</span>
        </div>
        <div class="gal-timeline-cover">
          <img loading="lazy" src="/api/files/preview?path=${encodeURIComponent(g.cover)}&w=300&h=200" alt="">
        </div>
      </div>
    `).join('');

    container.querySelectorAll('.gal-timeline-group').forEach(el => {
      el.addEventListener('click', () => {
        GAL.monthFilter = el.dataset.key;
        _galSetView('grid');
      });
    });
  } catch (err) {
    toast(t('Błąd ładowania osi czasu: ') + err.message, 'error');
  }
  _galShowLoader(false);
}

/* ━━━━  FAVORITES VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _galLoadFavorites() {
  _galShowLoader(true);
  const container = GAL.root.querySelector('.gal-favorites-view');
  container.innerHTML = '';
  try {
    const data = await api('/gallery/favorites');
    GAL.items = data.items;
    GAL.total = data.total;
    _galUpdateCount();

    if (!data.items.length) {
      container.innerHTML = `<div class="gal-empty" style="display:flex"><i class="fa-solid fa-star"></i><p>${t('Brak ulubionych')}</p><p class="gal-empty-hint">${t('Dodaj zdjęcia do ulubionych klawiszem F lub przyciskiem ★ w podglądzie')}</p></div>`;
      _galShowLoader(false);
      return;
    }

    const grid = document.createElement('div');
    grid.className = 'gal-grid';
    grid.style.display = '';
    container.appendChild(grid);

    data.items.forEach((item, i) => {
      const card = document.createElement('div');
      card.className = 'gal-card';
      card.dataset.idx = i;
      const isVideo = item.type === 'video';
      const thumbUrl = isVideo
        ? `/api/gallery/video-thumb?path=${encodeURIComponent(item.path)}`
        : `/api/files/preview?path=${encodeURIComponent(item.path)}&w=${GAL.THUMB_SIZE}&h=${GAL.THUMB_SIZE}`;
      card.innerHTML = `
        <div class="gal-card-img-wrap">
          <img loading="lazy" src="${thumbUrl}" alt="${_esc(item.name)}">
          ${isVideo ? '<div class="gal-video-badge"><i class="fa-solid fa-play"></i></div>' : ''}
          <div class="gal-fav-badge"><i class="fa-solid fa-star"></i></div>
          <div class="gal-card-overlay"><span class="gal-card-name">${_esc(item.name)}</span></div>
        </div>`;
      card.addEventListener('click', () => _galOpenLightbox(i));
      grid.appendChild(card);
    });
  } catch (err) {
    toast(t('Błąd ładowania ulubionych: ') + err.message, 'error');
  }
  _galShowLoader(false);
}

async function _galToggleFavorite(item) {
  if (!item) return;
  try {
    const check = await api(`/gallery/favorites/check?path=${encodeURIComponent(item.path)}`);
    const isFav = check.favorite;
    await api('/gallery/favorites', {
      method: isFav ? 'DELETE' : 'POST',
      body: { path: item.path }
    });
    // Update lightbox button if open
    const btn = document.querySelector('.gal-lb-fav-btn');
    if (btn) {
      btn.querySelector('i').className = !isFav ? 'fa-solid fa-star' : 'fa-regular fa-star';
      btn.style.color = !isFav ? '#f59e0b' : '';
    }
    toast(!isFav ? t('Dodano do ulubionych ★') : t('Usunięto z ulubionych'), 'info');
    return !isFav;
  } catch (e) {
    toast(t('Błąd: ') + e.message, 'error');
    return null;
  }
}

async function _galDeleteCurrentLightbox() {
  const item = GAL.lightboxItems[GAL.lightboxIdx];
  if (!item) return false;
  try {
    const r = await api('/files/delete', {
      method: 'DELETE',
      body: { path: item.path }
    });
    if (r.error) { toast(t('Błąd usuwania'), 'error'); return false; }
    toast(t('Usunięto: ') + item.name, 'info');
    // Remove from items arrays and update grid
    const arrIdx = GAL.items.indexOf(item);
    if (arrIdx !== -1) {
        GAL.items.splice(arrIdx, 1);
        const gridCards = GAL.gridEl?.querySelectorAll('.gal-card');
        if (gridCards && gridCards[arrIdx]) gridCards[arrIdx].remove();
    }
    const lbIdx = GAL.lightboxItems.indexOf(item);
    if (lbIdx !== -1) GAL.lightboxItems.splice(lbIdx, 1);
    GAL.total--;
    _galUpdateCount();
    if (!GAL.lightboxItems.length) { _galCloseLightbox(); _galReload(); return true; }
    if (GAL.lightboxIdx >= GAL.lightboxItems.length) GAL.lightboxIdx = GAL.lightboxItems.length - 1;
    _galRenderLightbox();
    return true;
  } catch(e) { toast(t('Błąd usuwania: ') + e.message, 'error'); return false; }
}

async function _galUpdateFavBtn() {
  const item = GAL.lightboxItems[GAL.lightboxIdx];
  if (!item) return;
  const btn = document.querySelector('.gal-lb-fav-btn');
  if (!btn) return;
  try {
    const r = await api(`/gallery/favorites/check?path=${encodeURIComponent(item.path)}`);
    btn.querySelector('i').className = r.favorite ? 'fa-solid fa-star' : 'fa-regular fa-star';
    btn.style.color = r.favorite ? '#f59e0b' : '';
  } catch(e) {}
}

/* ━━━━  PEOPLE VIEW (AI)  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function _galLoadPeople() {
  const container = GAL.root.querySelector('.gal-people-view');
  container.innerHTML = '<div class="gal-spinner" style="margin:60px auto"></div>';
  const d = await api('/photos-ai/people');
  if (d.error) {
    container.innerHTML = `<div class="gal-empty" style="display:flex">
      <i class="fa-solid fa-brain"></i>
      <p>${_esc(d.error)}</p>
      <p class="gal-empty-hint">${t('Zainstaluj Photos AI lub uruchom skan AI z paska bocznego.')}</p>
    </div>`;
    return;
  }
  const people = d.people || [];
  if (!people.length) {
    container.innerHTML = `<div class="gal-empty" style="display:flex">
      <i class="fa-solid fa-users"></i>
      <p>${t('Brak rozpoznanych osób')}</p>
      <p class="gal-empty-hint">${t('Kliknij "Skanuj twarze" w panelu AI, aby wykryć osoby na zdjęciach.')}</p>
    </div>`;
    return;
  }
  container.innerHTML = `<div class="gal-people-grid">${people.map(p => {
    const thumbUrl = p.cover_face_id ? `/api/photos-ai/face-thumb/${p.cover_face_id}` : '';
    const name = p.name || t('Osoba') + ' ' + p.id;
    return `<div class="gal-person-card" data-pid="${p.id}">
      <button class="gal-person-del" data-pid="${p.id}" title="${t('Usuń osobę')}"><i class="fa-solid fa-xmark"></i></button>
      <div class="gal-person-avatar">${thumbUrl
        ? `<img src="${thumbUrl}" alt="">`
        : `<i class="fa-solid fa-user"></i>`}</div>
      <div class="gal-person-name">${_esc(name)}</div>
      <div class="gal-person-count">${p.photo_count || 0} ${t('zdjęć')}</div>
    </div>`;
  }).join('')}</div>`;

  container.querySelectorAll('.gal-person-del').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const pid = parseInt(btn.dataset.pid);
      const card = btn.closest('.gal-person-card');
      const name = card.querySelector('.gal-person-name').textContent;
      confirmDialog(t('Usunąć „{name}"? Twarze zostaną odłączone, zdjęcia nie zostaną usunięte.', { name }), async () => {
        const r = await api(`/photos-ai/people/${pid}`, { method: 'DELETE' });
        if (r.ok) { toast(t('Usunięto'), 'success'); _galLoadPeople(); }
        else toast(r.error || t('Błąd'), 'error');
      });
    });
  });

  container.querySelectorAll('.gal-person-card').forEach(card => {
    card.addEventListener('click', () => _galPersonDetail(parseInt(card.dataset.pid), container));
  });
}

async function _galPersonDetail(pid, container) {
  container.innerHTML = '<div class="gal-spinner" style="margin:60px auto"></div>';

  const [pData, photosData] = await Promise.all([
    api('/photos-ai/people'),
    api(`/photos-ai/people/${pid}/photos?limit=200`),
  ]);
  const person = (pData.people || []).find(p => p.id === pid);
  if (!person) { _galLoadPeople(); return; }
  const name = person.name || t('Osoba') + ' ' + pid;

  // Get all face thumbnails for this person
  const facesData = await api(`/photos-ai/people/${pid}/faces`);
  const faces = facesData.faces || [];
  const photos = photosData.items || photosData.photos || [];

  container.innerHTML = `
    <div class="gal-person-detail">
      <div class="gal-person-header">
        <button class="btn btn-sm gal-person-back"><i class="fa-solid fa-arrow-left"></i> ${t('Osoby')}</button>
        <div class="gal-person-title">
          <div class="gal-person-big-avatar">${person.cover_face_id
            ? `<img src="/api/photos-ai/face-thumb/${person.cover_face_id}" alt="">`
            : `<i class="fa-solid fa-user"></i>`}</div>
          <div>
            <h2 class="gal-person-edit-name" contenteditable="true" spellcheck="false" title="${t('Kliknij aby zmienić imię')}">${_esc(name)}</h2>
            <span style="color:var(--text-secondary);font-size:13px">${faces.length} ${t('twarzy')} · ${photos.length} ${t('zdjęć')}</span>
          </div>
        </div>
        <div class="gal-person-actions">
          <button class="btn btn-sm gal-person-select-mode"><i class="fa-solid fa-check-double"></i> ${t('Zaznacz')}</button>
          <button class="btn btn-sm btn-danger gal-person-delete" style="display:none"><i class="fa-solid fa-user-xmark"></i> ${t('Usuń zaznaczone')}</button>
          <button class="btn btn-sm gal-person-move" style="display:none"><i class="fa-solid fa-people-arrows"></i> ${t('Przenieś do…')}</button>
        </div>
      </div>
      <div class="gal-person-faces-section">
        <h3 style="margin:0 0 10px;font-size:14px;color:var(--text-secondary)">
          <i class="fa-solid fa-face-smile"></i> ${t('Twarze')}
          <span style="font-weight:normal;font-size:12px;margin-left:6px">${t('Zaznacz błędnie przypisane twarze i usuń lub przenieś')}</span>
        </h3>
        <div class="gal-person-faces-grid">${faces.map(f => `
          <div class="gal-pf-thumb" data-face-id="${f.id}">
            <img src="/api/photos-ai/face-thumb/${f.id}" alt="">
            <div class="gal-pf-check"><i class="fa-solid fa-check"></i></div>
          </div>
        `).join('')}</div>
      </div>
      <div class="gal-person-photos-section">
        <h3 style="margin:16px 0 10px;font-size:14px;color:var(--text-secondary)">
          <i class="fa-solid fa-images"></i> ${t('Zdjęcia')}
        </h3>
        <div class="gal-person-photos-grid">${photos.map((ph, i) => `
          <div class="gal-card gal-person-photo" data-idx="${i}">
            <div class="gal-card-img-wrap">
              <img loading="lazy" src="/api/files/preview?path=${encodeURIComponent(ph.path)}&w=200&h=200" alt="">
            </div>
          </div>
        `).join('')}</div>
      </div>
    </div>`;

  // State for multi-select
  let selectMode = false;
  const selectedFaces = new Set();

  // Back button
  container.querySelector('.gal-person-back').addEventListener('click', () => _galLoadPeople());

  // Rename on blur
  const nameEl = container.querySelector('.gal-person-edit-name');
  nameEl.addEventListener('blur', async () => {
    const newName = nameEl.textContent.trim();
    if (newName && newName !== name) {
      await api(`/photos-ai/people/${pid}/rename`, { method: 'POST', body: { name: newName } });
    }
  });
  nameEl.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); nameEl.blur(); } });

  // Select mode toggle
  const selectBtn = container.querySelector('.gal-person-select-mode');
  const deleteBtn = container.querySelector('.gal-person-delete');
  const moveBtn = container.querySelector('.gal-person-move');

  selectBtn.addEventListener('click', () => {
    selectMode = !selectMode;
    selectedFaces.clear();
    selectBtn.classList.toggle('btn-primary', selectMode);
    container.querySelectorAll('.gal-pf-check').forEach(el => el.classList.remove('checked'));
    container.querySelectorAll('.gal-pf-thumb').forEach(el => el.classList.remove('selected'));
    deleteBtn.style.display = 'none';
    moveBtn.style.display = 'none';
  });

  // Face thumbnail click → toggle selection (auto-enters select mode)
  container.querySelectorAll('.gal-pf-thumb').forEach(el => {
    el.addEventListener('click', () => {
      if (!selectMode) {
        selectMode = true;
        selectBtn.classList.add('btn-primary');
      }
      const fid = parseInt(el.dataset.faceId);
      if (selectedFaces.has(fid)) {
        selectedFaces.delete(fid);
        el.classList.remove('selected');
        el.querySelector('.gal-pf-check').classList.remove('checked');
      } else {
        selectedFaces.add(fid);
        el.classList.add('selected');
        el.querySelector('.gal-pf-check').classList.add('checked');
      }
      const has = selectedFaces.size > 0;
      deleteBtn.style.display = has ? '' : 'none';
      moveBtn.style.display = has ? '' : 'none';
    });
  });

  // Delete selected faces (unassign from person)
  deleteBtn.addEventListener('click', async () => {
    if (!selectedFaces.size) return;
    confirmDialog(t('Usunąć {n} zaznaczonych twarzy z tej osoby?', { n: selectedFaces.size }), async () => {
      for (const fid of selectedFaces) {
        await api('/photos-ai/assign-face', { method: 'POST', body: { face_id: fid, unassign: true } });
      }
      toast(t('Usunięto {n} twarzy', { n: selectedFaces.size }), 'success');
      _galPersonDetail(pid, container);
    });
  });

  // Move selected faces to another person
  moveBtn.addEventListener('click', async () => {
    if (!selectedFaces.size) return;
    const ppl = (pData.people || []).filter(p => p.id !== pid);
    _galShowMoveModal(ppl, selectedFaces, pid, container);
  });

  // Photo grid → lightbox
  const photoItems = photos.map(ph => ({ path: ph.path, name: ph.path.split('/').pop(), type: 'image' }));
  container.querySelectorAll('.gal-person-photo').forEach(card => {
    card.addEventListener('click', () => {
      const idx = parseInt(card.dataset.idx);
      GAL.lightboxItems = photoItems;
      GAL.lightboxIdx = idx;
      _galRenderLightbox();
    });
  });
}

function _galShowMoveModal(people, selectedFaces, currentPid, container) {
  const modal = document.createElement('div');
  modal.className = 'gal-face-modal';
  modal.innerHTML = `
    <div class="gal-face-modal-backdrop"></div>
    <div class="gal-face-modal-content">
      <h3><i class="fa-solid fa-people-arrows"></i> ${t('Przenieś {n} twarzy do…', { n: selectedFaces.size })}</h3>
      <p class="gal-face-modal-hint">${t('Wybierz osobę lub utwórz nową:')}</p>
      <div class="gal-face-matches" style="max-height:300px;overflow-y:auto">
        ${people.map(p => `
          <div class="gal-face-match" data-person-id="${p.id}">
            ${p.cover_face_id
              ? `<img src="/api/photos-ai/face-thumb/${p.cover_face_id}" alt="">`
              : `<span style="width:40px;height:40px;display:flex;align-items:center;justify-content:center;background:var(--bg-tertiary);border-radius:50%"><i class="fa-solid fa-user"></i></span>`}
            <div>
              <div class="gal-face-match-name">${_esc(p.name || t('Osoba') + ' ' + p.id)}</div>
              <div class="gal-face-match-conf">${p.photo_count || 0} ${t('zdjęć')}</div>
            </div>
          </div>
        `).join('')}
      </div>
      <div class="gal-face-new-name">
        <input type="text" class="gal-face-name-input" placeholder="${t('Lub nowe imię…')}">
        <button class="btn btn-sm btn-primary gal-face-save-btn">${t('Utwórz')}</button>
      </div>
      <button class="btn btn-sm gal-face-modal-close">${t('Anuluj')}</button>
    </div>`;
  document.body.appendChild(modal);

  const doMove = async (targetPid, newName) => {
    modal.remove();
    for (const fid of selectedFaces) {
      const body = newName ? { face_id: fid, new_name: newName } : { face_id: fid, person_id: targetPid };
      await api('/photos-ai/assign-face', { method: 'POST', body });
    }
    toast(t('Przeniesiono {n} twarzy', { n: selectedFaces.size }), 'success');
    _galPersonDetail(currentPid, container);
  };

  modal.querySelectorAll('.gal-face-match').forEach(el => {
    el.addEventListener('click', () => doMove(parseInt(el.dataset.personId), null));
  });
  modal.querySelector('.gal-face-save-btn').addEventListener('click', () => {
    const name = modal.querySelector('.gal-face-name-input').value.trim();
    if (name) doMove(null, name);
  });
  modal.querySelector('.gal-face-modal-close').addEventListener('click', () => modal.remove());
  modal.querySelector('.gal-face-modal-backdrop').addEventListener('click', () => modal.remove());
}

/* ━━━━  AI SCAN CONTROLS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function _galRefreshAiControls() {
  const ctrl = GAL.root?.querySelector('#gal-ai-controls');
  if (!ctrl) return;
  let st = null;
  try { st = await api('/photos-ai/scan-status'); } catch(e) {}
  const running = st && st.running;
  const paused = st && st.paused;
  if (running && paused) {
    ctrl.innerHTML =
      `<div style="font-size:12px;color:var(--warning-color);margin-bottom:4px"><i class="fa-solid fa-pause"></i> ${t('Wstrzymano')}</div>`
      + `<div style="display:flex;gap:4px">`
      + `<button class="btn btn-sm btn-primary" style="flex:1;font-size:11px" onclick="_galResumeAiScan()"><i class="fa-solid fa-play"></i> ${t('Wznów')}</button>`
      + `<button class="btn btn-sm btn-danger" style="flex:1;font-size:11px" onclick="_galStopAiScan()"><i class="fa-solid fa-stop"></i> ${t('Stop')}</button>`
      + `</div>`;
    _galOnAiProgress(st);
  } else if (running) {
    ctrl.innerHTML =
      `<div style="font-size:12px;color:var(--accent-color);margin-bottom:4px"><i class="fa-solid fa-spinner fa-spin"></i> ${t('Skanowanie…')}</div>`
      + `<div style="display:flex;gap:4px">`
      + `<button class="btn btn-sm btn-warning" style="flex:1;font-size:11px" onclick="_galPauseAiScan()"><i class="fa-solid fa-pause"></i> ${t('Pauza')}</button>`
      + `<button class="btn btn-sm btn-danger" style="flex:1;font-size:11px" onclick="_galStopAiScan()"><i class="fa-solid fa-stop"></i> ${t('Stop')}</button>`
      + `</div>`;
    _galOnAiProgress(st);
  } else {
    ctrl.innerHTML =
      `<div style="display:flex;gap:4px">`
      + `<button class="btn btn-sm" style="flex:1;font-size:11px" onclick="_galStartAiScan()"><i class="fa-solid fa-satellite-dish"></i> ${t('Skanuj')}</button>`
      + `<button class="btn btn-sm" style="flex:1;font-size:11px" onclick="_galRescanFresh()" title="${t('Od nowa')}"><i class="fa-solid fa-arrows-rotate"></i> ${t('Od nowa')}</button>`
      + `</div>`;
    const prog = GAL.root?.querySelector('.gal-ai-progress');
    if (prog) prog.style.display = 'none';
  }
}

async function _galStartAiScan() {
  const r = await api('/photos-ai/scan', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  _galRefreshAiControls();
}

async function _galPauseAiScan() {
  await api('/photos-ai/pause-scan', { method: 'POST' });
  _galRefreshAiControls();
}

async function _galResumeAiScan() {
  await api('/photos-ai/resume-scan', { method: 'POST' });
  _galRefreshAiControls();
}

async function _galStopAiScan() {
  await api('/photos-ai/stop-scan', { method: 'POST' });
  _galRefreshAiControls();
}

async function _galRescanFresh() {
  if (!confirm(t('Usunąć historię skanowania i skanować od nowa?'))) return;
  const r = await api('/photos-ai/rescan', { method: 'POST' });
  if (r.error) { toast(r.error, 'error'); return; }
  toast(t('Reskan od nowa rozpoczęty'), 'success');
  _galRefreshAiControls();
}

function _galOnAiProgress(data) {
  const prog = GAL.root?.querySelector('.gal-ai-progress');
  if (!prog) return;
  prog.style.display = 'block';
  const pct = data.total > 0 ? Math.round(data.processed / data.total * 100) : 0;
  const fill = prog.querySelector('.gal-ai-progress-fill');
  const txt = prog.querySelector('.gal-ai-progress-text');
  if (fill) fill.style.width = pct + '%';
  if (txt) txt.textContent = `${data.processed}/${data.total} · ${t('Twarzy')}: ${data.faces_found}`;
}

function _galOnAiDone(data) {
  _galRefreshAiControls();
  toast(`${t('Skan AI zakończony')}: ${data.total_processed} ${t('zdjęć')}, ${data.faces} ${t('twarzy')}, ${data.people} ${t('osób')}`, 'success');
  if (GAL.view === 'people') _galLoadPeople();
}

/* ━━━━  TAGS VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function _galLoadTags() {
  const container = GAL.root.querySelector('.gal-tags-view');
  container.innerHTML = '<div class="gal-spinner" style="margin:60px auto"></div>';
  const d = await api('/photos-ai/tags');
  if (d.error || !d.tags || !d.tags.length) {
    container.innerHTML = `<div class="gal-empty" style="display:flex">
      <i class="fa-solid fa-tags"></i><p>${t('Brak tagów. Uruchom skan AI.')}</p></div>`;
    return;
  }
  container.innerHTML = `<div style="padding:4px 0">
    <h3 style="margin:0 0 12px;font-size:15px"><i class="fa-solid fa-tags" style="color:#8b5cf6"></i> ${t('Wykryte obiekty')}</h3>
    <div class="gal-tag-chips">${d.tags.map(item =>
      `<span class="gal-tag-chip" data-tag="${_esc(item.tag)}">${_esc(item.tag_pl || item.tag)} <small>(${item.count})</small></span>`
    ).join('')}</div>
    <div class="gal-tag-results"></div>
  </div>`;
  container.querySelectorAll('.gal-tag-chip').forEach(chip => {
    chip.addEventListener('click', async () => {
      container.querySelectorAll('.gal-tag-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      const results = container.querySelector('.gal-tag-results');
      results.innerHTML = '<div class="gal-spinner" style="margin:30px auto"></div>';
      const r = await api('/photos-ai/album-photos?type=tag&id=' + encodeURIComponent(chip.dataset.tag));
      _galRenderAiPhotoGrid(results, r.items || [], r.total || 0);
    });
  });
}

/* ━━━━  SMART ALBUMS VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function _galLoadSmartAlbums() {
  const container = GAL.root.querySelector('.gal-smart-view');
  container.innerHTML = '<div class="gal-spinner" style="margin:60px auto"></div>';
  const d = await api('/photos-ai/smart-albums');
  if (d.error || !d.albums || !d.albums.length) {
    container.innerHTML = `<div class="gal-empty" style="display:flex">
      <i class="fa-solid fa-wand-magic-sparkles"></i><p>${t('Brak albumów AI. Uruchom skan.')}</p></div>`;
    return;
  }
  const icons = { person: 'fa-user', tag: 'fa-tag', camera: 'fa-camera' };
  container.innerHTML = `<div class="gal-people-grid">${d.albums.map(a => {
    const ico = a.type === 'person' && a.cover_face_id
      ? `<img src="/api/photos-ai/face-thumb/${a.cover_face_id}" style="width:100%;height:100%;object-fit:cover">`
      : `<i class="fa-solid ${icons[a.type] || 'fa-images'}"></i>`;
    return `<div class="gal-person-card gal-smart-card" data-type="${a.type}" data-id="${_esc(a.id)}">
      <div class="gal-person-avatar">${ico}</div>
      <div class="gal-person-name">${_esc(a.name)}</div>
      <div class="gal-person-count">${a.count} ${t('zdjęć')}</div>
    </div>`;
  }).join('')}</div>`;

  container.querySelectorAll('.gal-smart-card').forEach(card => {
    card.addEventListener('click', async () => {
      container.innerHTML = '<div class="gal-spinner" style="margin:60px auto"></div>';
      const r = await api(`/photos-ai/album-photos?type=${card.dataset.type}&id=${encodeURIComponent(card.dataset.id)}`);
      container.innerHTML = `<button class="btn btn-sm" style="margin-bottom:10px" onclick="this.closest('.gal-smart-view') && _galLoadSmartAlbums()">
        <i class="fa-solid fa-arrow-left"></i> ${t('Albumy AI')}</button><div class="gal-ai-results"></div>`;
      _galRenderAiPhotoGrid(container.querySelector('.gal-ai-results'), r.items || [], r.total || 0);
    });
  });
}

/* ━━━━  AI SEARCH VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

function _galLoadSearchAi() {
  const container = GAL.root.querySelector('.gal-searchai-view');
  container.innerHTML = `<div style="padding:4px 0">
    <div class="gal-ai-search-box">
      <i class="fa-solid fa-brain" style="color:#8b5cf6"></i>
      <input type="text" class="gal-ai-search-input" placeholder="${t('Szukaj: pies, kot, Marcin, aparat…')}">
    </div>
    <div class="gal-ai-search-results"></div>
  </div>`;
  let timer;
  const input = container.querySelector('.gal-ai-search-input');
  input.addEventListener('input', e => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = e.target.value.trim();
      const results = container.querySelector('.gal-ai-search-results');
      if (!q) { results.innerHTML = `<p style="color:var(--text-secondary);margin-top:20px">${t('Wpisz frazę aby wyszukać po tagach AI, osobach, aparacie…')}</p>`; return; }
      results.innerHTML = '<div class="gal-spinner" style="margin:30px auto"></div>';
      const d = await api('/photos-ai/search?q=' + encodeURIComponent(q));
      if (!d.items || !d.items.length) { results.innerHTML = `<p style="color:var(--text-secondary)">${t('Brak wyników.')}</p>`; return; }
      _galRenderAiPhotoGrid(results, d.items, d.total || d.items.length);
    }, 400);
  });
  input.focus();
}

/* ━━━━  MERGE SUGGESTIONS VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function _galLoadMerge() {
  const container = GAL.root.querySelector('.gal-merge-view');
  container.innerHTML = '<div class="gal-spinner" style="margin:60px auto"></div>';
  const d = await api('/photos-ai/merge-suggestions');
  const suggestions = d.suggestions || [];
  if (!suggestions.length) {
    container.innerHTML = `<div class="gal-empty" style="display:flex">
      <i class="fa-solid fa-code-merge"></i><p>${t('Brak sugestii łączenia. Wszystkie klastry wyglądają na unikalne.')}</p></div>`;
    return;
  }
  container.innerHTML = `
    <h3 style="margin:0 0 6px;display:flex;align-items:center;gap:8px;font-size:15px">
      <i class="fa-solid fa-code-merge" style="color:#8b5cf6"></i> ${t('Sugestie łączenia osób')}</h3>
    <p style="color:var(--text-secondary);margin:0 0 14px;font-size:13px">
      ${t('Te osoby mogą być tą samą osobą. Kliknij Połącz aby scalić.')}</p>
    <div class="gal-merge-list">${suggestions.map((s, i) => {
      const aImg = s.person_a.cover_face_id ? `<img src="/api/photos-ai/face-thumb/${s.person_a.cover_face_id}">` : `<i class="fa-solid fa-user"></i>`;
      const bImg = s.person_b.cover_face_id ? `<img src="/api/photos-ai/face-thumb/${s.person_b.cover_face_id}">` : `<i class="fa-solid fa-user"></i>`;
      return `<div class="gal-merge-card">
        <div class="gal-merge-pair">
          <div class="gal-merge-person"><div class="gal-merge-avatar">${aImg}</div><div class="gal-merge-name">${_esc(s.person_a.name)}</div></div>
          <div class="gal-merge-arrow"><i class="fa-solid fa-arrows-left-right"></i><div class="gal-merge-conf">${s.confidence}%</div></div>
          <div class="gal-merge-person"><div class="gal-merge-avatar">${bImg}</div><div class="gal-merge-name">${_esc(s.person_b.name)}</div></div>
        </div>
        <div class="gal-merge-actions">
          <button class="btn btn-sm btn-primary gal-merge-btn" data-src="${s.person_a.id}" data-tgt="${s.person_b.id}">
            <i class="fa-solid fa-code-merge"></i> ${t('Połącz')}</button>
          <button class="btn btn-sm gal-merge-skip">${t('Pomiń')}</button>
        </div>
      </div>`;
    }).join('')}</div>`;

  container.querySelectorAll('.gal-merge-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const card = btn.closest('.gal-merge-card');
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      const r = await api('/photos-ai/people/merge', {
        method: 'POST', body: { source_id: parseInt(btn.dataset.src), target_id: parseInt(btn.dataset.tgt) },
      });
      if (r.ok) { card.style.opacity = '0.3'; card.style.pointerEvents = 'none'; toast(t('Połączono!'), 'success'); }
      else { toast(r.error || t('Błąd'), 'error'); btn.disabled = false; btn.innerHTML = `<i class="fa-solid fa-code-merge"></i> ${t('Połącz')}`; }
    });
  });
  container.querySelectorAll('.gal-merge-skip').forEach(btn => {
    btn.addEventListener('click', () => { btn.closest('.gal-merge-card').style.display = 'none'; });
  });
}

/* ━━━━  SHARED: AI PHOTO GRID RENDERER  ━━━━━━━━━━━━━━━━━━━━━━ */

function _galRenderAiPhotoGrid(container, items, total) {
  container.innerHTML = `<div class="gal-person-photos-grid">${items.map((item, i) =>
    `<div class="gal-card gal-ai-photo" data-idx="${i}">
      <div class="gal-card-img-wrap">
        <img loading="lazy" src="/api/files/preview?path=${encodeURIComponent(item.path)}&w=200&h=200" alt="">
      </div>
    </div>`
  ).join('')}</div>`;
  const photoItems = items.map(ph => ({ path: ph.path, name: ph.path.split('/').pop(), type: 'image' }));
  container.querySelectorAll('.gal-ai-photo').forEach(card => {
    card.addEventListener('click', () => {
      GAL.lightboxItems = photoItems;
      GAL.lightboxIdx = parseInt(card.dataset.idx);
      _galRenderLightbox();
    });
  });
}

/* ━━━━  LIGHTBOX  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _galOpenLightbox(idx) {
  GAL.lightboxIdx = idx;
  GAL.lightboxItems = GAL.items;
  _galRenderLightbox();
}

function _galRenderLightbox() {
  _galRemoveLightbox();
  const item = GAL.lightboxItems[GAL.lightboxIdx];
  if (!item) return;

  const lb = document.createElement('div');
  lb.className = 'gal-lightbox';
  lb.id = 'gal-lightbox';

  const isVideo = item.type === 'video';
  const fullUrl = `/api/files/download?path=${encodeURIComponent(item.path)}`;
  const prevBtn = GAL.lightboxIdx > 0
    ? '<button class="gal-lb-nav gal-lb-prev"><i class="fa-solid fa-chevron-left"></i></button>' : '';
  const nextBtn = GAL.lightboxIdx < GAL.lightboxItems.length - 1
    ? '<button class="gal-lb-nav gal-lb-next"><i class="fa-solid fa-chevron-right"></i></button>' : '';

  lb.innerHTML = `
    <div class="gal-lb-backdrop"></div>
    <div class="gal-lb-toolbar">
      <div class="gal-lb-info">
        <span class="gal-lb-name">${_esc(item.name)}</span>
        <span class="gal-lb-counter">${GAL.lightboxIdx + 1} / ${GAL.lightboxItems.length}</span>
      </div>
      <div class="gal-lb-actions">
        <button class="gal-lb-btn gal-lb-faces-btn" title="${t('Twarze (AI)')}"><i class="fa-solid fa-face-smile"></i></button>
        <button class="gal-lb-btn gal-lb-info-btn" title="Informacje"><i class="fa-solid fa-circle-info"></i></button>
        <button class="gal-lb-btn gal-lb-fav-btn" title="Ulubione (F)"><i class="fa-regular fa-star"></i></button>
        <button class="gal-lb-btn gal-lb-show-fm-btn" title="${t('Pokaż w Menedżerze plików')}"><i class="fa-solid fa-folder-open"></i></button>
        <button class="gal-lb-btn gal-lb-delete-btn" title="${t('Usuń (Delete)')}"><i class="fa-solid fa-trash"></i></button>
        <button class="gal-lb-btn gal-lb-zoom-in" title="${t('Powiększ')}"><i class="fa-solid fa-magnifying-glass-plus"></i></button>
        <button class="gal-lb-btn gal-lb-zoom-out" title="Pomniejsz"><i class="fa-solid fa-magnifying-glass-minus"></i></button>
        <button class="gal-lb-btn gal-lb-rotate" title="${t('Obróć 90°')}"><i class="fa-solid fa-rotate-right"></i></button>
        <button class="gal-lb-btn gal-lb-download" title="Pobierz"><i class="fa-solid fa-download"></i></button>
        <button class="gal-lb-btn gal-lb-share" title="${t('Udostępnij')}"><i class="fa-solid fa-share-nodes"></i></button>
        <button class="gal-lb-btn gal-lb-slideshow" title="${t('Pokaz slajdów')}"><i class="fa-solid fa-play"></i></button>
        <button class="gal-lb-btn gal-lb-fullscreen" title="${t('Pełny ekran (F11)')}"><i class="fa-solid fa-expand"></i></button>
        <button class="gal-lb-btn gal-lb-close" title="Zamknij"><i class="fa-solid fa-xmark"></i></button>
      </div>
    </div>
    <div class="gal-lb-content">
      ${prevBtn}
      <div class="gal-lb-media" id="gal-lb-media">
        ${isVideo
          ? `<video controls autoplay muted class="gal-lb-video" src="${fullUrl}"></video>`
          : `<img class="gal-lb-img" src="${fullUrl}" alt="${_esc(item.name)}" draggable="false">`
        }
      </div>
      ${nextBtn}
    </div>
    <div class="gal-lb-exif" id="gal-lb-exif" style="display:none">
      <div class="gal-lb-exif-content"></div>
    </div>
    <div class="gal-lb-faces-panel" id="gal-lb-faces-panel" style="display:none">
      <div class="gal-lb-faces-content"></div>
    </div>
  `;

  document.body.appendChild(lb);

  // Wire events
  lb.querySelector('.gal-lb-backdrop').addEventListener('click', _galCloseLightbox);
  lb.querySelector('.gal-lb-close').addEventListener('click', _galCloseLightbox);

  const prev = lb.querySelector('.gal-lb-prev');
  const next = lb.querySelector('.gal-lb-next');
  if (prev) prev.addEventListener('click', e => { e.stopPropagation(); _galLightboxNav(-1); });
  if (next) next.addEventListener('click', e => { e.stopPropagation(); _galLightboxNav(1); });

  lb.querySelector('.gal-lb-download').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = fullUrl;
    a.download = item.name;
    a.click();
  });

  // Info panel
  lb.querySelector('.gal-lb-info-btn').addEventListener('click', () => _galToggleExif(item));

  // Face detection panel
  lb.querySelector('.gal-lb-faces-btn').addEventListener('click', () => _galToggleFaces(item));

  // Favorite button
  lb.querySelector('.gal-lb-fav-btn').addEventListener('click', () => _galToggleFavorite(item));
  _galUpdateFavBtn();

  // Show in File Manager
  lb.querySelector('.gal-lb-show-fm-btn').addEventListener('click', () => {
    const folderPath = item.path.replace(/\/[^/]+$/, '') || '/';
    _galCloseLightbox();
    const fmApp = window.NAS?.apps?.find(a => a.id === 'file-manager') || { id: 'file-manager', type: 'builtin' };
    openApp(fmApp, { path: folderPath });
  });

  // Delete button
  lb.querySelector('.gal-lb-delete-btn').addEventListener('click', async () => {
    confirmDialog(t('Usunąć plik: ') + _esc(item.name) + '?', async () => {
      await _galDeleteCurrentLightbox();
    });
  });

  // Zoom
  let scale = 1;
  const mediaEl = lb.querySelector('.gal-lb-img') || lb.querySelector('.gal-lb-video');
  lb.querySelector('.gal-lb-zoom-in').addEventListener('click', () => {
    scale = Math.min(scale + 0.5, 5);
    mediaEl.style.transform = `scale(${scale})`;
  });
  lb.querySelector('.gal-lb-zoom-out').addEventListener('click', () => {
    scale = Math.max(scale - 0.5, 0.5);
    mediaEl.style.transform = `scale(${scale})`;
  });

  // Slideshow from lightbox
  lb.querySelector('.gal-lb-slideshow').addEventListener('click', () => {
    if (GAL.slideshowTimer) {
      _galStopSlideshow();
      lb.querySelector('.gal-lb-slideshow i').className = 'fa-solid fa-play';
    } else {
      _galStartSlideshowFromLightbox();
      lb.querySelector('.gal-lb-slideshow i').className = 'fa-solid fa-pause';
    }
  });

  // Rotate
  lb.querySelector('.gal-lb-rotate').addEventListener('click', async () => {
    const curItem = GAL.lightboxItems[GAL.lightboxIdx];
    if (!curItem || curItem.type !== 'image') { toast(t('Obrót tylko dla zdjęć'), 'info'); return; }
    try {
      await api('/gallery/rotate', { method: 'POST', body: { path: curItem.path, angle: 90 } });
      toast(t('Obrócono o 90°'), 'success');
      const cacheBust = '&_t=' + Date.now();
      const img = document.querySelector('.gal-lb-img');
      if (img) img.src = img.src.split('&_t=')[0] + cacheBust;
      // Refresh thumbnail in any visible grid (main, favorites, custom albums)
      const encodedPath = encodeURIComponent(curItem.path);
      GAL.root.querySelectorAll('.gal-card img').forEach(thumb => {
          if (thumb.src.includes(encodedPath)) {
              thumb.src = thumb.src.split('&_t=')[0] + cacheBust;
          }
      });
    } catch(e) { toast(t('Błąd obrotu: ') + e.message, 'error'); }
  });

  // Share (with optional user targeting)
  lb.querySelector('.gal-lb-share').addEventListener('click', async () => {
    const curItem = GAL.lightboxItems[GAL.lightboxIdx];
    if (!curItem) return;
    const shareOpts = await _galShowShareDialog([curItem.path]);
    if (!shareOpts) return;
    try {
      const body = { path: curItem.path };
      if (shareOpts.shared_with && shareOpts.shared_with.length) body.shared_with = shareOpts.shared_with;
      const r = await api('/gallery/share', { method: 'POST', body });
      const shareUrl = location.origin + r.url;
      await navigator.clipboard.writeText(shareUrl);
      if (shareOpts.shared_with?.length) {
        toast(t('Udostępniono dla:') + ` ${shareOpts.shared_with.join(', ')}. ` + t('Link skopiowany!'), 'success');
      } else {
        toast('Link skopiowany do schowka!', 'success');
      }
    } catch(e) { toast(t('Błąd udostępniania: ') + e.message, 'error'); }
  });

  // Fullscreen
  lb.querySelector('.gal-lb-fullscreen').addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen();
  });

  // Keyboard
  lb._keyHandler = e => {
    if (e.key === 'Escape') _galCloseLightbox();
    if (e.key === 'ArrowLeft') _galLightboxNav(-1);
    if (e.key === 'ArrowRight') _galLightboxNav(1);
    if (e.key === '+' || e.key === '=') { scale = Math.min(scale + 0.5, 5); mediaEl.style.transform = `scale(${scale})`; }
    if (e.key === '-') { scale = Math.max(scale - 0.5, 0.5); mediaEl.style.transform = `scale(${scale})`; }
    if (e.key === 'f' || e.key === 'F') { _galToggleFavorite(GAL.lightboxItems[GAL.lightboxIdx]); }
    if (e.key === 'Delete') {
      const curItem = GAL.lightboxItems[GAL.lightboxIdx];
      if (curItem) confirmDialog(t('Usunąć plik: ') + _esc(curItem.name) + '?', () => _galDeleteCurrentLightbox());
    }
    if (e.key === 'F11') {
      e.preventDefault();
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen();
    }
  };
  document.addEventListener('keydown', lb._keyHandler);

  // Swipe support (horizontal=nav, vertical: up=delete, down=favorite)
  let startX = 0, startY = 0;
  const contentEl = lb.querySelector('.gal-lb-content');
  contentEl.addEventListener('touchstart', e => {
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
  }, { passive: true });
  contentEl.addEventListener('touchend', async e => {
    const dx = e.changedTouches[0].clientX - startX;
    const dy = e.changedTouches[0].clientY - startY;
    if (Math.abs(dy) > 80 && Math.abs(dy) > Math.abs(dx) * 1.2) {
      // Vertical swipe
      if (dy < -80) {
        // Swipe UP → Delete (with confirmation)
        const curItem = GAL.lightboxItems[GAL.lightboxIdx];
        if (curItem) confirmDialog(t('Usunąć plik: ') + _esc(curItem.name) + '?', () => _galDeleteCurrentLightbox());
      } else if (dy > 80) {
        // Swipe DOWN → Add to favorites
        const curItem = GAL.lightboxItems[GAL.lightboxIdx];
        if (curItem) await _galToggleFavorite(curItem);
      }
    } else if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy)) {
      if (dx > 0) _galLightboxNav(-1);
      else _galLightboxNav(1);
    }
  }, { passive: true });

  // Mouse wheel zoom
  contentEl.addEventListener('wheel', e => {
    e.preventDefault();
    if (e.deltaY < 0) scale = Math.min(scale + 0.25, 5);
    else scale = Math.max(scale - 0.25, 0.5);
    mediaEl.style.transform = `scale(${scale})`;
  }, { passive: false });
}

function _galLightboxNav(dir) {
  const newIdx = GAL.lightboxIdx + dir;
  if (newIdx < 0 || newIdx >= GAL.lightboxItems.length) return;
  _galClearFaceOverlay();
  GAL.lightboxIdx = newIdx;
  _galRenderLightbox();
  if (GAL.facesMode) {
    const item = GAL.lightboxItems[GAL.lightboxIdx];
    if (item) _galRefreshFaces(item);
  }
}

function _galCloseLightbox() {
  _galStopSlideshow();
  _galClearFaceOverlay();
  GAL.facesMode = false;
  _galRemoveLightbox();
}

function _galRemoveLightbox() {
  const lb = document.getElementById('gal-lightbox');
  if (lb) {
    if (lb._keyHandler) document.removeEventListener('keydown', lb._keyHandler);
    lb.remove();
  }
}

async function _galToggleExif(item) {
  const exifPanel = document.getElementById('gal-lb-exif');
  if (!exifPanel) return;
  if (exifPanel.style.display !== 'none') {
    exifPanel.style.display = 'none';
    return;
  }

  const content = exifPanel.querySelector('.gal-lb-exif-content');
  content.innerHTML = '<div class="gal-spinner" style="margin:20px auto"></div>';
  exifPanel.style.display = 'flex';

  try {
    const exif = await api(`/gallery/exif?path=${encodeURIComponent(item.path)}`);
    const nice = {
      'Wymiary': exif.width && exif.height ? `${exif.width} × ${exif.height}` : null,
      'Aparat': exif.Model || null,
      'Producent': exif.Make || null,
      'ISO': exif.ISOSpeedRatings || null,
      [t('Przysłona')]: exif.FNumber ? `f/${exif.FNumber}` : null,
      [t('Czas naśw.')]: exif.ExposureTime ? `${exif.ExposureTime}s` : null,
      'Ogniskowa': exif.FocalLength ? `${exif.FocalLength}mm` : null,
      'Data': exif.DateTimeOriginal || exif.DateTime || null,
      'Software': exif.Software || null,
      'GPS': exif.gps ? `${exif.gps.lat}, ${exif.gps.lon}` : null,
    };
    const rows = Object.entries(nice).filter(([, v]) => v !== null);
    content.innerHTML = `
      <h4><i class="fa-solid fa-circle-info"></i> Informacje</h4>
      <table class="gal-exif-table">
        <tr><td>Nazwa</td><td>${_esc(item.name)}</td></tr>
        <tr><td>Rozmiar</td><td>${formatBytes(item.size)}</td></tr>
        <tr><td>Folder</td><td>${_esc(item.album)}</td></tr>
        <tr><td>Typ</td><td>${item.type}</td></tr>
        <tr><td>Data mod.</td><td>${new Date(item.modified * 1000).toLocaleString(getLocale())}</td></tr>
        ${rows.map(([k, v]) => `<tr><td>${k}</td><td>${_esc(String(v))}</td></tr>`).join('')}
      </table>
      ${exif.gps ? `<a class="gal-map-link" href="https://www.google.com/maps?q=${exif.gps.lat},${exif.gps.lon}" target="_blank" rel="noopener">
        <i class="fa-solid fa-map-location-dot"></i> ${t('Pokaż na mapie')}
      </a>` : ''}
    `;
  } catch (err) {
    content.innerHTML = `<p style="color:#ef4444">${t('Nie udało się odczytać danych EXIF')}</p>`;
  }
}

/* ━━━━  FACE DETECTION OVERLAY  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */

async function _galToggleFaces(item) {
  const panel = document.getElementById('gal-lb-faces-panel');
  const exifPanel = document.getElementById('gal-lb-exif');
  if (!panel) return;

  // Toggle off
  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    GAL.facesMode = false;
    _galClearFaceOverlay();
    return;
  }

  // Close EXIF if open
  if (exifPanel) exifPanel.style.display = 'none';
  panel.style.display = 'flex';
  GAL.facesMode = true;

  const content = panel.querySelector('.gal-lb-faces-content');
  content.innerHTML = '<div class="gal-spinner" style="margin:20px auto"></div>';

  if (item.type !== 'image') {
    content.innerHTML = `<p style="color:var(--text-secondary)">${t('Detekcja twarzy działa tylko na zdjęciach')}</p>`;
    return;
  }

  try {
    const data = await api('/photos-ai/photo-ai?path=' + encodeURIComponent(item.path));
    const faces = data.faces || [];
    const tags = data.tags || [];

    // Draw bounding boxes on image
    _galDrawFaceBoxes(faces);

    // Build panel content
    let html = `<h4><i class="fa-solid fa-face-smile"></i> ${t('Twarze')} (${faces.length})</h4>`;

    if (!faces.length) {
      html += `<p class="gal-faces-empty">${t('Nie wykryto twarzy na tym zdjęciu.')}</p>`;
      if (!tags.length) html += `<p class="gal-faces-hint">${t('Uruchom skan w Photos AI, aby wykryć twarze.')}</p>`;
    } else {
      html += '<div class="gal-faces-list">';
      for (const face of faces) {
        const thumbUrl = '/api/photos-ai/face-thumb/' + face.id;
        const name = face.person_name || t('Nieznana osoba');
        html += `
          <div class="gal-face-item" data-face-id="${face.id}" data-person-id="${face.person_id || ''}">
            <img class="gal-face-thumb" src="${thumbUrl}" alt="">
            <div class="gal-face-info">
              <div class="gal-face-name">${_esc(name)}</div>
              <button class="gal-face-identify-btn" data-face-id="${face.id}">
                <i class="fa-solid fa-user-tag"></i> ${face.person_name ? t('Zmień') : t('Kto to?')}
              </button>
            </div>
          </div>`;
      }
      html += '</div>';
    }

    // Tags section
    if (tags.length) {
      const yoloTags = tags.filter(t => t.source === 'yolo');
      if (yoloTags.length) {
        html += `<h4 style="margin-top:16px"><i class="fa-solid fa-tags"></i> ${t('Obiekty')}</h4>`;
        html += '<div class="gal-faces-tags">';
        for (const tag of yoloTags) {
          html += `<span class="gal-face-tag">${_esc(tag.tag_pl || tag.tag)} <small>${Math.round(tag.confidence * 100)}%</small></span>`;
        }
        html += '</div>';
      }
    }

    content.innerHTML = html;

    // Wire "Who is this?" buttons
    content.querySelectorAll('.gal-face-identify-btn').forEach(btn => {
      btn.addEventListener('click', () => _galIdentifyFace(parseInt(btn.dataset.faceId), item));
    });

    // Wire face item hover → highlight box
    content.querySelectorAll('.gal-face-item').forEach(el => {
      el.addEventListener('mouseenter', () => {
        const fid = el.dataset.faceId;
        const box = document.querySelector('.gal-face-box[data-face-id="' + fid + '"]');
        if (box) box.classList.add('gal-face-box-active');
      });
      el.addEventListener('mouseleave', () => {
        document.querySelectorAll('.gal-face-box-active').forEach(b => b.classList.remove('gal-face-box-active'));
      });
    });
  } catch (err) {
    content.innerHTML = `<p style="color:#ef4444">${t('Błąd: ') + err.message}</p>`;
  }
}

function _galDrawFaceBoxes(faces) {
  _galClearFaceOverlay();
  const img = document.querySelector('.gal-lb-img');
  const media = document.getElementById('gal-lb-media');
  if (!img || !media || !faces.length) return;

  const overlay = document.createElement('div');
  overlay.className = 'gal-face-overlay';
  overlay.style.cssText = `position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none;`;

  // Calculate scale: image natural size vs displayed size
  const rect = img.getBoundingClientRect();
  const mediaRect = media.getBoundingClientRect();
  const scaleX = rect.width / img.naturalWidth;
  const scaleY = rect.height / img.naturalHeight;
  const offsetX = rect.left - mediaRect.left;
  const offsetY = rect.top - mediaRect.top;

  for (const face of faces) {
    const box = document.createElement('div');
    box.className = 'gal-face-box';
    box.dataset.faceId = face.id;
    box.style.cssText = `
      position:absolute; pointer-events:auto; cursor:pointer;
      left:${offsetX + face.x * scaleX}px;
      top:${offsetY + face.y * scaleY}px;
      width:${face.w * scaleX}px;
      height:${face.h * scaleY}px;
    `;
    // Name label
    const label = document.createElement('span');
    label.className = 'gal-face-label';
    label.textContent = face.person_name || '?';
    box.appendChild(label);

    box.addEventListener('click', (e) => {
      e.stopPropagation();
      const item = GAL.lightboxItems[GAL.lightboxIdx];
      _galIdentifyFace(face.id, item);
    });

    overlay.appendChild(box);
  }
  media.style.position = 'relative';
  media.appendChild(overlay);
}

async function _galRefreshFaces(item) {
  const panel = document.getElementById('gal-lb-faces-panel');
  if (!panel) return;
  // Force panel visible for refresh (e.g. after nav in faces mode)
  panel.style.display = 'none';
  _galClearFaceOverlay();
  await _galToggleFaces(item);
}

function _galClearFaceOverlay() {
  document.querySelectorAll('.gal-face-overlay').forEach(o => o.remove());
}

async function _galIdentifyFace(faceId, item) {
  // Fetch matching people
  const data = await api('/photos-ai/identify-face', {
    method: 'POST', body: { face_id: faceId },
  });
  if (data.error) { toast(data.error, 'error'); return; }

  const matches = data.matches || [];
  const currentPid = data.current_person_id;

  // Build a modal for selecting or naming the person
  const modal = document.createElement('div');
  modal.className = 'gal-face-modal';
  modal.innerHTML = `
    <div class="gal-face-modal-backdrop"></div>
    <div class="gal-face-modal-content">
      <h3><i class="fa-solid fa-user-tag"></i> ${t('Kto to jest?')}</h3>
      <div class="gal-face-modal-thumb">
        <img src="/api/photos-ai/face-thumb/${faceId}" alt="">
      </div>
      ${matches.length ? `
        <p class="gal-face-modal-hint">${t('Wybierz osobę lub wpisz nowe imię:')}</p>
        <div class="gal-face-matches">
          ${matches.slice(0, 6).map(m => `
            <div class="gal-face-match${m.person_id === currentPid ? ' gal-face-match-current' : ''}"
                 data-person-id="${m.person_id}">
              <img src="/api/photos-ai/face-thumb/${m.cover_face_id || 0}" alt=""
                   onerror="this.replaceWith(document.createTextNode('👤'))">
              <div>
                <div class="gal-face-match-name">${_esc(m.name)}</div>
                <div class="gal-face-match-conf">${m.confidence}% ${t('zgodności')} · ${m.photo_count} ${t('zdjęć')}</div>
              </div>
            </div>
          `).join('')}
        </div>
      ` : `<p class="gal-face-modal-hint">${t('Brak dopasowań. Wpisz imię:')}</p>`}
      <div class="gal-face-new-name">
        <input type="text" class="gal-face-name-input" placeholder="${t('Nowe imię...')}" autofocus>
        <button class="btn btn-sm btn-primary gal-face-save-btn">${t('Zapisz')}</button>
      </div>
      <button class="btn btn-sm gal-face-modal-close">${t('Anuluj')}</button>
    </div>
  `;

  document.body.appendChild(modal);

  // Wire match clicks
  modal.querySelectorAll('.gal-face-match').forEach(el => {
    el.addEventListener('click', async () => {
      const pid = parseInt(el.dataset.personId);
      const r = await api('/photos-ai/assign-face', {
        method: 'POST', body: { face_id: faceId, person_id: pid },
      });
      if (r.ok) {
        toast(t('Przypisano!'), 'success');
        modal.remove();
        _galRefreshFaces(item);
      } else {
        toast(r.error || t('Błąd'), 'error');
      }
    });
  });

  // Wire new name save
  const saveBtn = modal.querySelector('.gal-face-save-btn');
  const nameInput = modal.querySelector('.gal-face-name-input');
  const doSave = async () => {
    const name = nameInput.value.trim();
    if (!name) { nameInput.focus(); return; }
    const r = await api('/photos-ai/assign-face', {
      method: 'POST', body: { face_id: faceId, new_name: name },
    });
    if (r.ok) {
      toast(t('Zapisano: ') + name, 'success');
      modal.remove();
      _galRefreshFaces(item);
    } else {
      toast(r.error || t('Błąd'), 'error');
    }
  };
  saveBtn.addEventListener('click', doSave);
  nameInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSave(); });

  // Wire close
  modal.querySelector('.gal-face-modal-close').addEventListener('click', () => modal.remove());
  modal.querySelector('.gal-face-modal-backdrop').addEventListener('click', () => modal.remove());
}

/* ━━━━  SLIDESHOW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _galStartSlideshow() {
  if (!GAL.items.length) return;
  const images = GAL.items.filter(i => i.type === 'image');
  if (!images.length) { toast(t('Brak zdjęć do pokazu'), 'info'); return; }
  GAL.lightboxItems = images;
  GAL.lightboxIdx = 0;
  _galRenderLightbox();
  _galStartSlideshowFromLightbox();
}

function _galStartSlideshowFromLightbox() {
  _galStopSlideshow();
  GAL.slideshowTimer = setInterval(() => {
    if (GAL.lightboxIdx < GAL.lightboxItems.length - 1) {
      GAL.lightboxIdx++;
      _galRenderLightbox();
    } else {
      _galStopSlideshow();
    }
  }, 4000);
}

function _galStopSlideshow() {
  if (GAL.slideshowTimer) {
    clearInterval(GAL.slideshowTimer);
    GAL.slideshowTimer = null;
  }
}

/* ━━━━  HELPERS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _esc(s) {
  const div = document.createElement('div');
  div.textContent = s || '';
  return div.innerHTML;
}

/* ━━━━  SHARE DIALOG (with optional user targeting)  ━━━━━━━━━━ */
async function _galShowShareDialog(paths) {
  // Fetch other NAS users
  let allUsers = [];
  try {
    const ulist = await api('/users/list');
    const me = (typeof NAS !== 'undefined' && NAS.user) ? NAS.user.username : '';
    allUsers = (ulist || []).filter(u => u.nasos_user && u.username !== me);
  } catch(e) {}

  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal">
        <div class="modal-header"><i class="fas fa-share-nodes" style="margin-right:8px;color:var(--accent)"></i>${t('Udostępnij')} (${paths.length} ${paths.length === 1 ? t('plik') : t('plików')})</div>
        <div class="modal-body">
          <div style="margin-bottom:10px;font-size:12px;color:var(--text-muted)">
            <i class="fas fa-link"></i> Link zostanie skopiowany do schowka
          </div>
          ${allUsers.length ? `
          <div>
            <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer;">
              <input type="checkbox" id="gal-share-user-toggle"> ${t('Udostępnij konkretnemu użytkownikowi')}
            </label>
            <div id="gal-share-users" style="display:none;margin-top:6px;padding:8px;background:var(--bg-secondary);border-radius:8px;max-height:140px;overflow-y:auto;">
              ${allUsers.map(u => `
                <label style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:13px;cursor:pointer;">
                  <input type="checkbox" class="gal-share-user-cb" value="${u.username}">
                  <i class="fas fa-user" style="color:var(--text-muted);font-size:11px;"></i> ${u.username}
                </label>
              `).join('')}
            </div>
          </div>` : ''}
        </div>
        <div class="modal-footer">
          <button class="btn" id="gal-share-cancel">Anuluj</button>
          <button class="btn btn-primary" id="gal-share-ok"><i class="fas fa-share-nodes"></i> ${t('Udostępnij')}</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const toggle = overlay.querySelector('#gal-share-user-toggle');
    const userList = overlay.querySelector('#gal-share-users');
    if (toggle && userList) {
      toggle.addEventListener('change', () => { userList.style.display = toggle.checked ? 'block' : 'none'; });
    }

    overlay.querySelector('#gal-share-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
    overlay.querySelector('#gal-share-ok').addEventListener('click', () => {
      const selectedUsers = [];
      if (toggle && toggle.checked) {
        overlay.querySelectorAll('.gal-share-user-cb:checked').forEach(cb => selectedUsers.push(cb.value));
      }
      overlay.remove();
      resolve({ shared_with: selectedUsers });
    });
    overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
  });
}

/* ━━━━  MULTI-SELECT & BATCH  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _galUpdateBatchBar() {
  let bar = GAL.root.querySelector('.gal-batch-bar');
  if (!GAL.selectMode || GAL.selected.size === 0) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'gal-batch-bar';
    bar.innerHTML = `
      <span class="gal-batch-count"></span>
      <button class="gal-batch-fav" title="Dodaj do ulubionych"><i class="fa-solid fa-star"></i></button>
      <button class="gal-batch-del" title="${t('Usuń')}"><i class="fa-solid fa-trash"></i></button>
      <button class="gal-batch-zip" title="Pobierz ZIP"><i class="fa-solid fa-file-zipper"></i></button>
      <button class="gal-batch-share" title="${t('Udostępnij')}"><i class="fa-solid fa-share-nodes"></i></button>
      <button class="gal-batch-cancel">Anuluj</button>
    `;
    bar.querySelector('.gal-batch-del').addEventListener('click', _galBatchDelete);
    bar.querySelector('.gal-batch-zip').addEventListener('click', _galBatchZip);
    bar.querySelector('.gal-batch-fav').addEventListener('click', _galBatchFavorite);
    bar.querySelector('.gal-batch-share').addEventListener('click', _galBatchShare);
    bar.querySelector('.gal-batch-cancel').addEventListener('click', () => {
      GAL.selectMode = false;
      GAL.selected.clear();
      GAL.root.querySelectorAll('.gal-card-select').forEach(el => el.style.display = 'none');
      GAL.root.querySelectorAll('.gal-card-check').forEach(cb => cb.checked = false);
      _galUpdateBatchBar();
    });
    GAL.root.querySelector('.gal-main').appendChild(bar);
  }
  bar.querySelector('.gal-batch-count').textContent = GAL.selected.size + ' ' + t('zaznaczonych');
}

async function _galBatchDelete() {
  const paths = [...GAL.selected];
  if (!paths.length) return;
  const confirmed = await confirmDialog(t('Usunąć') + ` ${paths.length} ` + t('plików') + '?', t('Tej operacji nie można cofnąć.'));
  if (!confirmed) return;
  let ok = 0;
  for (const p of paths) {
    try { await api('/files/delete', { method: 'DELETE', body: { path: p } }); ok++; } catch(e) {}
  }
  toast(t('Usunięto') + ` ${ok} / ${paths.length}`, 'info');
  GAL.selected.clear();
  GAL.selectMode = false;
  _galUpdateBatchBar();
  _galReload();
}

async function _galBatchZip() {
  const paths = [...GAL.selected];
  if (!paths.length) return;
  toast(`${t('Przygotowywanie ZIP')} (${paths.length} ${t('plików')})...`, 'info');
  try {
    const resp = await fetch('/api/gallery/download-zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (NAS.token || ''), 'X-CSRFToken': NAS.csrfToken },
      body: JSON.stringify({ paths })
    });
    if (!resp.ok) throw new Error(t('Błąd pobierania'));
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'galeria.zip'; a.click();
    URL.revokeObjectURL(url);
    toast(t('ZIP pobrany!'), 'success');
  } catch(e) { toast(t('Błąd ZIP: ') + e.message, 'error'); }
}

async function _galBatchFavorite() {
  const paths = [...GAL.selected];
  if (!paths.length) return;
  let ok = 0;
  for (const p of paths) {
    try { await api('/gallery/favorites', { method: 'POST', body: { path: p } }); ok++; } catch(e) {}
  }
  toast(t('Dodano do ulubionych') + ` (${ok})`, 'success');
}

async function _galBatchShare() {
  const paths = [...GAL.selected];
  if (!paths.length) return;
  const shareOpts = await _galShowShareDialog(paths);
  if (!shareOpts) return;
  try {
    const body = { paths };
    if (shareOpts.shared_with && shareOpts.shared_with.length) body.shared_with = shareOpts.shared_with;
    const r = await api('/gallery/share', { method: 'POST', body });
    const shareUrl = location.origin + r.url;
    await navigator.clipboard.writeText(shareUrl);
    if (shareOpts.shared_with?.length) {
      toast(t('Udostępniono dla:') + ` ${shareOpts.shared_with.join(', ')}. ` + t('Link skopiowany!'), 'success');
    } else {
      toast(t('Link do udostępnionych plików skopiowany!'), 'success');
    }
  } catch(e) { toast(t('Błąd udostępniania: ') + e.message, 'error'); }
}

/* ━━━━  UPLOAD  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
function _galUploadClick() {
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.accept = 'image/*,video/*';
  input.addEventListener('change', async () => {
    const files = input.files;
    if (!files.length) return;
    const targetFolder = GAL.folder || (GAL.gallerySources.length ? GAL.gallerySources[0].path : '');
    if (!targetFolder) { toast(t('Wybierz folder docelowy'), 'error'); return; }
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    fd.append('folder', targetFolder);
    toast(t('Przesyłanie') + ` ${files.length} ` + t('plików') + '...', 'info');
    try {
      const r = await api('/gallery/upload', { method: 'POST', body: fd });
      toast(t('Przesłano') + ` ${r.uploaded} ` + t('plików'), 'success');
      _galReload();
    } catch(e) { toast(t('Błąd przesyłania: ') + e.message, 'error'); }
  });
  input.click();
}

function _galInitDragDrop() {
  const main = GAL.root.querySelector('.gal-main');
  const dropOverlay = document.createElement('div');
  dropOverlay.className = 'gal-drop-overlay';
  dropOverlay.innerHTML = `<div class="gal-drop-zone"><i class="fa-solid fa-cloud-arrow-up"></i><p>${t('Upuść pliki aby przesłać')}</p></div>`;
  dropOverlay.style.display = 'none';
  main.appendChild(dropOverlay);

  let dragCounter = 0;
  main.addEventListener('dragenter', e => { e.preventDefault(); dragCounter++; dropOverlay.style.display = 'flex'; });
  main.addEventListener('dragleave', e => { e.preventDefault(); dragCounter--; if (dragCounter <= 0) { dragCounter = 0; dropOverlay.style.display = 'none'; } });
  main.addEventListener('dragover', e => e.preventDefault());
  main.addEventListener('drop', async e => {
    e.preventDefault(); dragCounter = 0; dropOverlay.style.display = 'none';
    const files = e.dataTransfer.files;
    if (!files.length) return;
    const targetFolder = GAL.folder || (GAL.gallerySources.length ? GAL.gallerySources[0].path : '');
    if (!targetFolder) { toast(t('Wybierz folder docelowy'), 'error'); return; }
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    fd.append('folder', targetFolder);
    toast(t('Przesyłanie') + ` ${files.length} ` + t('plików') + '...', 'info');
    try {
      const r = await api('/gallery/upload', { method: 'POST', body: fd });
      toast(t('Przesłano') + ` ${r.uploaded} ` + t('plików'), 'success');
      _galReload();
    } catch(e) { toast(t('Błąd przesyłania: ') + e.message, 'error'); }
  });
}

/* ━━━━  MAP VIEW  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _galLoadLeaflet() {
  if (window.L) return;
  const css = document.createElement('link');
  css.rel = 'stylesheet'; css.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
  document.head.appendChild(css);
  return new Promise((resolve) => {
    const s = document.createElement('script');
    s.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
    s.onload = resolve;
    document.head.appendChild(s);
  });
}

async function _galLoadMap() {
  _galShowLoader(true);
  const container = GAL.root.querySelector('.gal-map-view');
  container.innerHTML = '<div id="gal-map" style="width:100%;height:100%"></div>';
  try {
    await _galLoadLeaflet();
    const data = await api('/gallery/map');
    const map = L.map('gal-map').setView([52, 19], 6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap'
    }).addTo(map);
    data.forEach(p => {
      const marker = L.marker([p.lat, p.lon]).addTo(map);
      marker.bindPopup(`<img src="/api/files/preview?path=${encodeURIComponent(p.path)}&w=200&h=150" style="max-width:200px"><br>${_esc(p.name)}`);
    });
    if (data.length) {
      const bounds = L.latLngBounds(data.map(p => [p.lat, p.lon]));
      map.fitBounds(bounds, { padding: [30, 30] });
    }
    toast(`${data.length} ${t('zdjęć z lokalizacją GPS')}`, 'info');
  } catch(e) { toast(t('Błąd ładowania mapy: ') + e.message, 'error'); }
  _galShowLoader(false);
}

/* ━━━━  STATISTICS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _galShowStats() {
  try {
    const s = await api('/gallery/stats');
    const modal = document.createElement('div');
    modal.className = 'gal-folder-modal-overlay';
    modal.innerHTML = `
      <div class="gal-folder-modal" style="max-width:500px">
        <div class="gal-folder-modal-header">
          <h3><i class="fa-solid fa-chart-pie"></i> Statystyki galerii</h3>
          <button class="gal-folder-modal-close"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <div style="padding:20px">
          <div class="gal-stat-grid">
            <div class="gal-stat-card"><div class="gal-stat-num">${s.total_images}</div><div class="gal-stat-label">${t('Zdjęcia')}</div></div>
            <div class="gal-stat-card"><div class="gal-stat-num">${s.total_videos}</div><div class="gal-stat-label">Filmy</div></div>
            <div class="gal-stat-card"><div class="gal-stat-num">${s.total_raw}</div><div class="gal-stat-label">RAW</div></div>
            <div class="gal-stat-card"><div class="gal-stat-num">${formatBytes(s.total_size)}</div><div class="gal-stat-label">Rozmiar</div></div>
          </div>
          <h4 style="margin:16px 0 8px">Formaty</h4>
          <div class="gal-formats-list">${Object.entries(s.formats || {}).sort((a,b) => b[1]-a[1]).map(([ext,cnt]) =>
            `<span class="gal-format-tag">.${ext} <b>${cnt}</b></span>`
          ).join('')}</div>
          ${s.earliest ? `<p style="margin-top:12px;color:#94a3b8;font-size:13px">Od ${new Date(s.earliest*1000).toLocaleDateString(getLocale())} do ${new Date(s.latest*1000).toLocaleDateString(getLocale())}</p>` : ''}
        </div>
      </div>`;
    modal.querySelector('.gal-folder-modal-close').onclick = () => modal.remove();
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
    document.body.appendChild(modal);
  } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
}

/* ━━━━  CUSTOM ALBUMS  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
async function _galLoadCustomAlbums() {
  const list = GAL.root.querySelector('.gal-custom-albums-list');
  if (!list) return;
  try {
    const albums = await api('/gallery/custom-albums');
    if (!albums.length) {
      list.innerHTML = `<div class="gal-no-sources">${t('Brak albumów')}</div>`;
      return;
    }
    list.innerHTML = albums.map(a => `
      <div class="gal-custom-album-item" data-id="${_esc(String(a.id))}">
        <i class="fa-solid fa-images" style="color:#8b5cf6"></i>
        <span>${_esc(a.name)}</span>
        <span class="gal-source-count">${a.count || 0}</span>
      </div>
    `).join('');
    list.querySelectorAll('.gal-custom-album-item').forEach(el => {
      el.addEventListener('click', () => _galOpenCustomAlbum(el.dataset.id));
    });
  } catch(e) {
    list.innerHTML = `<div class="gal-no-sources">${t('Błąd ładowania')}</div>`;
  }
}

async function _galCreateCustomAlbum() {
  const name = await promptDialog(t('Nazwa nowego albumu:'));
  if (!name) return;
  try {
    await api('/gallery/custom-albums', { method: 'POST', body: { name } });
    toast(t('Album utworzony: ') + name, 'success');
    _galLoadCustomAlbums();
  } catch(e) { toast(t('Błąd tworzenia albumu: ') + e.message, 'error'); }
}

async function _galOpenCustomAlbum(albumId) {
  _galShowLoader(true);
  try {
    const data = await api(`/gallery/custom-albums/${albumId}`);
    GAL.items = data.items || [];
    GAL.total = GAL.items.length;
    GAL.lightboxItems = GAL.items;
    GAL.view = 'grid';
    GAL.root.querySelectorAll('.gal-nav-item').forEach(n => n.classList.remove('active'));
    GAL.root.querySelector('.gal-grid').style.display = '';
    GAL.root.querySelector('.gal-albums').style.display = 'none';
    GAL.root.querySelector('.gal-timeline-view').style.display = 'none';
    GAL.root.querySelector('.gal-favorites-view').style.display = 'none';
    GAL.root.querySelector('.gal-map-view').style.display = 'none';
    GAL.gridEl.innerHTML = '';
    _galRenderItems(GAL.items, 0);
    _galUpdateCount();
  } catch(e) { toast(t('Błąd otwierania albumu: ') + e.message, 'error'); }
  _galShowLoader(false);
}
