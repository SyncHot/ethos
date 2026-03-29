/* ═══════════════════════════════════════════════════════════════════
   EthOS  —  Gallery  (state-of-the-art photo & video gallery)
   ═══════════════════════════════════════════════════════════════════ */
AppRegistry['gallery'] = function (appDef, launchOpts) {
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
  GAL.subfolder = '';  // clear album filter when switching views
  if (!GAL.monthFilter) GAL.monthFilter = '';  // preserve if set by timeline click
  GAL.root.querySelectorAll('.gal-nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === view));
  GAL.root.querySelector('.gal-grid').style.display = view === 'grid' ? '' : 'none';
  GAL.root.querySelector('.gal-albums').style.display = view === 'albums' ? '' : 'none';
  GAL.root.querySelector('.gal-timeline-view').style.display = view === 'timeline' ? '' : 'none';
  GAL.root.querySelector('.gal-favorites-view').style.display = view === 'favorites' ? '' : 'none';
  GAL.root.querySelector('.gal-map-view').style.display = view === 'map' ? '' : 'none';

  if (view === 'grid') _galReload();
  else if (view === 'albums') _galLoadAlbums();
  else if (view === 'timeline') _galLoadTimeline();
  else if (view === 'favorites') _galLoadFavorites();
  else if (view === 'map') _galLoadMap();
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
  GAL.lightboxIdx = newIdx;
  _galRenderLightbox();
}

function _galCloseLightbox() {
  _galStopSlideshow();
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
