/* eslint-disable */
/**
 * Radio & Music — internet radio, podcasts, and music player.
 * CSS prefix: rm-
 */
AppRegistry['radio-music'] = function(appDef, launchOpts) {

    let bodyEl, activeSection = 'radio', _audio = null, _playing = null;
    let _favorites = [], _subscriptions = [], _countries = [], _tags = [];
    let _recentStations = [];  // for prev/next navigation

    const escH = s => s ? s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';

    // Deterministic color from station name (for letter-avatar fallback)
    const _COLORS = ['#ef4444','#f97316','#f59e0b','#22c55e','#14b8a6','#3b82f6','#6366f1','#a855f7','#ec4899','#06b6d4'];
    function _stationColor(name) { let h=0; for(let i=0;i<name.length;i++) h=((h<<5)-h)+name.charCodeAt(i); return _COLORS[Math.abs(h)%_COLORS.length]; }
    function _stationInitial(name) { return (name||'?').replace(/^(radio|polskie)\s*/i,'').charAt(0).toUpperCase(); }

    function _stationIconHtml(s) {
        if (s.favicon) {
            const letter = _stationInitial(s.name);
            const bg = _stationColor(s.name);
            return '<img src="' + escH(s.favicon) + '" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
                 + '<span class="rm-letter-icon" style="display:none;background:' + bg + '">' + escH(letter) + '</span>';
        }
        const letter = _stationInitial(s.name);
        const bg = _stationColor(s.name);
        return '<span class="rm-letter-icon" style="background:' + bg + '">' + escH(letter) + '</span>';
    }

    function getCSS() { return [
/* layout */
'.rm-wrap{display:flex;height:100%;overflow:hidden;font-size:13px}',
'.rm-sidebar{width:200px;min-width:200px;background:var(--bg-secondary);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto}',
'.rm-sidebar-item{padding:10px 16px;cursor:pointer;display:flex;align-items:center;gap:8px;color:var(--text-primary);font-size:13px;transition:background .12s;border-left:3px solid transparent}',
'.rm-sidebar-item:hover{background:var(--bg-hover)}',
'.rm-sidebar-item.active{background:var(--bg-hover);border-left-color:var(--accent);font-weight:600}',
'.rm-sidebar-item i{width:16px;text-align:center;opacity:.7;font-size:13px}',
'.rm-sidebar-label{padding:16px 16px 6px;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--text-muted);font-weight:600}',
'.rm-main{flex:1;display:flex;flex-direction:column;overflow:hidden}',
'.rm-toolbar{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);flex-wrap:wrap}',
'.rm-search{flex:1;min-width:180px;padding:8px 14px;border:1px solid var(--border);border-radius:20px;background:var(--bg-primary);color:var(--text-primary);font-size:13px;outline:none}',
'.rm-search:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(59,130,246,.15)}',
'.rm-content{flex:1;overflow-y:auto;padding:16px}',

/* station / podcast cards */
'.rm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}',
'.rm-card{display:flex;align-items:center;gap:12px;padding:10px 12px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;cursor:pointer;transition:all .15s}',
'.rm-card:hover{background:var(--bg-hover);box-shadow:0 2px 12px rgba(0,0,0,.18);transform:translateY(-1px)}',
'.rm-card.rm-playing{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 2px 12px rgba(59,130,246,.2)}',
'.rm-card-icon{width:48px;height:48px;border-radius:10px;background:#1a1a2e;display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;position:relative}',
'.rm-card-icon img{width:100%;height:100%;object-fit:cover;border-radius:10px}',
'.rm-card-icon i{font-size:20px;color:var(--text-muted)}',
'.rm-letter-icon{width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:20px;border-radius:10px}',
'.rm-card-info{flex:1;min-width:0}',
'.rm-card-name{font-weight:600;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}',
'.rm-card-meta{font-size:11px;color:var(--text-muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-card-actions{display:flex;gap:4px;flex-shrink:0}',
'.rm-card-btn{background:none;border:none;color:var(--text-muted);cursor:pointer;padding:6px;font-size:14px;border-radius:50%;transition:color .12s,background .12s}',
'.rm-card-btn:hover{color:var(--accent);background:rgba(59,130,246,.1)}',
'.rm-card-btn.rm-fav-active{color:#f59e0b}',
'.rm-card-codec{font-size:9px;padding:1px 5px;border-radius:4px;background:rgba(255,255,255,.08);color:var(--text-muted);font-weight:600;letter-spacing:.5px}',

/* country / tag chips */
'.rm-chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}',
'.rm-chip{padding:6px 14px;border-radius:20px;background:var(--bg-secondary);border:1px solid var(--border);color:var(--text-primary);font-size:12px;cursor:pointer;transition:all .12s;white-space:nowrap}',
'.rm-chip:hover{border-color:var(--accent);color:var(--accent)}',
'.rm-chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}',

/* ── Player bar (Audials-style) ────────────────────────── */
'.rm-player{display:flex;align-items:center;gap:14px;padding:10px 20px;background:linear-gradient(180deg,var(--bg-secondary) 0%,rgba(0,0,0,.15) 100%);border-top:1px solid var(--border);min-height:68px}',
'.rm-player-art{width:50px;height:50px;border-radius:10px;background:#1a1a2e;display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.3)}',
'.rm-player-art img{width:100%;height:100%;object-fit:cover}',
'.rm-player-art .rm-letter-icon{font-size:18px}',
'.rm-player-art i{font-size:18px;color:var(--text-muted)}',
'.rm-player-info{flex:1;min-width:0}',
'.rm-player-name{font-weight:700;font-size:14px;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-player-meta{font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}',
'.rm-player-controls{display:flex;align-items:center;gap:4px}',
'.rm-player-btn{background:none;border:none;color:var(--text-primary);font-size:16px;cursor:pointer;padding:8px;border-radius:50%;transition:all .12s;line-height:1}',
'.rm-player-btn:hover{background:var(--bg-hover);color:var(--accent)}',
'.rm-player-btn.rm-btn-play{font-size:22px;width:42px;height:42px;display:flex;align-items:center;justify-content:center;background:var(--accent);color:#fff;border-radius:50%;box-shadow:0 2px 8px rgba(59,130,246,.3)}',
'.rm-player-btn.rm-btn-play:hover{background:var(--accent);filter:brightness(1.15);transform:scale(1.05)}',
'.rm-vol-wrap{display:flex;align-items:center;gap:6px}',
'.rm-vol-wrap i{font-size:13px;color:var(--text-muted)}',
'.rm-vol-slider{width:80px;accent-color:var(--accent);height:4px}',
'.rm-player-eq{display:flex;align-items:flex-end;gap:2px;height:18px;margin-left:4px}',
'.rm-player-eq span{width:3px;background:var(--accent);border-radius:1px;animation:rm-eq .6s ease-in-out infinite alternate}',
'.rm-player-eq span:nth-child(1){animation-delay:0s;height:6px}',
'.rm-player-eq span:nth-child(2){animation-delay:.15s;height:12px}',
'.rm-player-eq span:nth-child(3){animation-delay:.3s;height:8px}',
'.rm-player-eq span:nth-child(4){animation-delay:.45s;height:14px}',
'.rm-player-eq span:nth-child(5){animation-delay:.1s;height:10px}',
'@keyframes rm-eq{0%{height:4px}100%{height:18px}}',

/* podcast episode list */
'.rm-ep-list{display:flex;flex-direction:column;gap:8px}',
'.rm-ep-item{display:flex;align-items:center;gap:12px;padding:12px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--r-md);cursor:pointer;transition:background .12s}',
'.rm-ep-item:hover{background:var(--bg-hover)}',
'.rm-ep-item.rm-playing{border-color:var(--accent)}',
'.rm-ep-play{font-size:16px;color:var(--accent);width:32px;text-align:center;flex-shrink:0}',
'.rm-ep-info{flex:1;min-width:0}',
'.rm-ep-title{font-weight:600;color:var(--text-primary);margin-bottom:2px}',
'.rm-ep-meta{font-size:11px;color:var(--text-muted)}',

/* podcast detail header */
'.rm-pod-header{display:flex;gap:16px;margin-bottom:20px;align-items:flex-start}',
'.rm-pod-art{width:120px;height:120px;border-radius:var(--r-md);object-fit:cover;flex-shrink:0}',
'.rm-pod-details{flex:1;min-width:0}',
'.rm-pod-title{font-size:18px;font-weight:700;color:var(--text-primary);margin-bottom:4px}',
'.rm-pod-author{font-size:13px;color:var(--text-muted);margin-bottom:6px}',
'.rm-pod-desc{font-size:12px;color:var(--text-secondary);line-height:1.5;max-height:60px;overflow:hidden}',
'.rm-pod-sub-btn{margin-top:8px;padding:6px 16px;border-radius:var(--r-md);border:1px solid var(--accent);background:transparent;color:var(--accent);font-size:12px;font-weight:600;cursor:pointer;transition:all .12s}',
'.rm-pod-sub-btn:hover{background:var(--accent);color:#fff}',
'.rm-pod-sub-btn.subscribed{background:var(--accent);color:#fff}',

/* empty state */
'.rm-empty{text-align:center;padding:60px 20px;color:var(--text-muted)}',
'.rm-empty i{font-size:48px;margin-bottom:12px;display:block;opacity:.3}',
'.rm-empty p{font-size:14px}',

/* mobile nav bar */
'.rm-mobile-nav{display:none;overflow-x:auto;white-space:nowrap;background:var(--bg-secondary);border-bottom:1px solid var(--border);padding:6px 8px;gap:4px;-webkit-overflow-scrolling:touch;scrollbar-width:none}',
'.rm-mobile-nav::-webkit-scrollbar{display:none}',
'.rm-mobile-nav .rm-mnav-btn{display:inline-flex;align-items:center;gap:5px;padding:7px 12px;border:1px solid var(--border);border-radius:20px;background:var(--bg-primary);color:var(--text-primary);font-size:12px;white-space:nowrap;cursor:pointer;flex-shrink:0;transition:all .12s}',
'.rm-mobile-nav .rm-mnav-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}',
'.rm-mobile-nav .rm-mnav-btn i{font-size:11px}',

/* responsive */
'@media(max-width:768px){.rm-sidebar{display:none}.rm-mobile-nav{display:flex}.rm-grid{grid-template-columns:1fr}.rm-pod-header{flex-direction:column;align-items:center;text-align:center}.rm-pod-art{width:100px;height:100px}.rm-toolbar{padding:8px 10px}.rm-content{padding:10px}.rm-player{padding:8px 12px;gap:10px}.rm-vol-wrap{display:none}.rm-player-art{width:40px;height:40px}.rm-player-btn.rm-btn-play{width:36px;height:36px;font-size:18px}}',
    ].join('\n'); }

    createWindow('radio-music', {
        title: t('Radio & Music'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1000, height: 650,
        onRender(body) {
            bodyEl = body;
            body.innerHTML = '';

            body.innerHTML = `
<style>${getCSS()}</style>
<div class="rm-wrap">
  <div class="rm-sidebar">
    <div class="rm-sidebar-label">${t('Radio')}</div>
    <div class="rm-sidebar-item active" data-section="radio"><i class="fas fa-broadcast-tower"></i> ${t('Przeglądaj')}</div>
    <div class="rm-sidebar-item" data-section="favorites"><i class="fas fa-heart"></i> ${t('Ulubione')}</div>
    <div class="rm-sidebar-item" data-section="countries"><i class="fas fa-globe"></i> ${t('Kraje')}</div>
    <div class="rm-sidebar-item" data-section="tags"><i class="fas fa-tags"></i> ${t('Gatunki')}</div>
    <div class="rm-sidebar-label">${t('Podcasty')}</div>
    <div class="rm-sidebar-item" data-section="podcasts"><i class="fas fa-podcast"></i> ${t('Szukaj')}</div>
    <div class="rm-sidebar-item" data-section="subscriptions"><i class="fas fa-rss"></i> ${t('Subskrypcje')}</div>
    <div class="rm-sidebar-label">${t('Inne')}</div>
    <div class="rm-sidebar-item" data-section="history"><i class="fas fa-history"></i> ${t('Historia')}</div>
  </div>
  <div class="rm-main">
    <div class="rm-mobile-nav" id="rm-mobile-nav">
      <button class="rm-mnav-btn active" data-section="radio"><i class="fas fa-broadcast-tower"></i> ${t('Radio')}</button>
      <button class="rm-mnav-btn" data-section="favorites"><i class="fas fa-heart"></i> ${t('Ulubione')}</button>
      <button class="rm-mnav-btn" data-section="countries"><i class="fas fa-globe"></i> ${t('Kraje')}</button>
      <button class="rm-mnav-btn" data-section="tags"><i class="fas fa-tags"></i> ${t('Gatunki')}</button>
      <button class="rm-mnav-btn" data-section="podcasts"><i class="fas fa-podcast"></i> ${t('Podcasty')}</button>
      <button class="rm-mnav-btn" data-section="subscriptions"><i class="fas fa-rss"></i> ${t('Subskrypcje')}</button>
      <button class="rm-mnav-btn" data-section="history"><i class="fas fa-history"></i> ${t('Historia')}</button>
    </div>
    <div class="rm-toolbar" id="rm-toolbar"></div>
    <div class="rm-content" id="rm-content"></div>
    <div class="rm-player" id="rm-player" style="display:none">
      <div class="rm-player-art" id="rm-player-art"><i class="fas fa-music"></i></div>
      <div class="rm-player-info">
        <div class="rm-player-name" id="rm-player-name"></div>
        <div class="rm-player-meta" id="rm-player-meta"></div>
      </div>
      <div class="rm-player-eq" id="rm-player-eq" style="display:none"><span></span><span></span><span></span><span></span><span></span></div>
      <div class="rm-player-controls">
        <button class="rm-player-btn" id="rm-prev-btn" title="${t('Poprzednia')}"><i class="fas fa-step-backward"></i></button>
        <button class="rm-player-btn rm-btn-play" id="rm-play-pause"><i class="fas fa-play"></i></button>
        <button class="rm-player-btn" id="rm-next-btn" title="${t('Następna')}"><i class="fas fa-step-forward"></i></button>
        <button class="rm-player-btn" id="rm-stop-btn" title="${t('Stop')}"><i class="fas fa-stop"></i></button>
      </div>
      <div class="rm-vol-wrap">
        <i class="fas fa-volume-up"></i>
        <input type="range" class="rm-vol-slider" id="rm-vol" min="0" max="100" value="80">
      </div>
    </div>
  </div>
</div>`;

            // Sidebar navigation
            body.querySelectorAll('.rm-sidebar-item').forEach(el => {
                el.onclick = () => {
                    body.querySelectorAll('.rm-sidebar-item').forEach(e => e.classList.remove('active'));
                    el.classList.add('active');
                    // sync mobile nav
                    body.querySelectorAll('.rm-mnav-btn').forEach(b => b.classList.toggle('active', b.dataset.section === el.dataset.section));
                    activeSection = el.dataset.section;
                    loadSection(activeSection);
                };
            });

            // Mobile nav
            body.querySelectorAll('.rm-mnav-btn').forEach(btn => {
                btn.onclick = () => {
                    body.querySelectorAll('.rm-mnav-btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    // sync sidebar
                    body.querySelectorAll('.rm-sidebar-item').forEach(e => e.classList.toggle('active', e.dataset.section === btn.dataset.section));
                    activeSection = btn.dataset.section;
                    loadSection(activeSection);
                };
            });

            // Player controls
            const playPauseBtn = body.querySelector('#rm-play-pause');
            playPauseBtn.onclick = () => {
                if (!_audio) return;
                if (_audio.paused) { _audio.play(); playPauseBtn.innerHTML = '<i class="fas fa-pause"></i>'; _showEq(true); }
                else { _audio.pause(); playPauseBtn.innerHTML = '<i class="fas fa-play"></i>'; _showEq(false); }
            };
            body.querySelector('#rm-stop-btn').onclick = () => stopPlayback();
            body.querySelector('#rm-prev-btn').onclick = () => _skipStation(-1);
            body.querySelector('#rm-next-btn').onclick = () => _skipStation(1);
            body.querySelector('#rm-vol').oninput = (e) => { if (_audio) _audio.volume = e.target.value / 100; };

            loadSection('radio');
        },
        onClose() {
            stopPlayback();
        },
    });

    function loadSection(section) {
        const toolbar = bodyEl.querySelector('#rm-toolbar');
        const content = bodyEl.querySelector('#rm-content');
        toolbar.innerHTML = '';
        content.innerHTML = '';

        switch(section) {
            case 'radio': loadRadio(toolbar, content); break;
            case 'favorites': loadFavorites(content); break;
            case 'countries': loadCountries(toolbar, content); break;
            case 'tags': loadTags(content); break;
            case 'podcasts': loadPodcasts(toolbar, content); break;
            case 'subscriptions': loadSubscriptions(content); break;
            case 'history': loadHistory(content); break;
        }
    }

    /* ── Radio Browse ───────────────────────────────── */

    async function loadRadio(toolbar, content) {
        toolbar.innerHTML = `<input class="rm-search" id="rm-radio-search" placeholder="${t('Szukaj stacji radiowych...')}" autofocus>`;
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-broadcast-tower"></i><p>' + t('Wpisz nazwę stacji lub przeglądaj Top stacje') + '</p></div>';

        const searchInput = bodyEl.querySelector('#rm-radio-search');
        let debounce;
        searchInput.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => searchRadio(searchInput.value, content), 400);
        };

        // Load top stations by default
        const data = await api('/radio-music/radio/top?limit=50');
        if (data.items && data.items.length) renderStations(data.items, content);
    }

    async function searchRadio(q, content) {
        if (!q.trim()) {
            const data = await api('/radio-music/radio/top?limit=50');
            if (data.items) renderStations(data.items, content);
            return;
        }
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        const data = await api('/radio-music/radio/search?q=' + encodeURIComponent(q));
        if (data.items && data.items.length) renderStations(data.items, content);
        else content.innerHTML = '<div class="rm-empty"><i class="fas fa-search"></i><p>' + t('Brak wyników') + '</p></div>';
    }

    function renderStations(stations, container) {
        // Track visible stations for prev/next navigation
        _recentStations = stations;
        container.innerHTML = '<div class="rm-grid" id="rm-stations-grid"></div>';
        const grid = container.querySelector('#rm-stations-grid');
        stations.forEach(s => {
            const isFav = _favorites.some(f => f.uuid === s.uuid);
            const isPlaying = _playing && _playing.uuid === s.uuid;
            const altCount = (s.alt_urls || []).length;
            const codecTag = s.codec ? '<span class="rm-card-codec">' + escH(s.codec) + (s.bitrate ? ' ' + s.bitrate + 'k' : '') + '</span>' : '';
            const fallbackTag = altCount ? ' <span class="rm-card-codec">' + (altCount+1) + ' src</span>' : '';
            const card = document.createElement('div');
            card.className = 'rm-card' + (isPlaying ? ' rm-playing' : '');
            card.innerHTML = `
                <div class="rm-card-icon">${_stationIconHtml(s)}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(s.name)}</div>
                    <div class="rm-card-meta">${escH([s.country, s.tags].filter(Boolean).join(' · '))} ${codecTag}${fallbackTag}</div>
                </div>
                <div class="rm-card-actions">
                    <button class="rm-card-btn rm-fav-btn ${isFav ? 'rm-fav-active' : ''}" title="${t('Ulubione')}"><i class="fas fa-heart"></i></button>
                </div>`;
            card.onclick = (e) => {
                if (e.target.closest('.rm-fav-btn')) return;
                playStation(s);
            };
            card.querySelector('.rm-fav-btn').onclick = (e) => {
                e.stopPropagation();
                toggleFavorite(s);
            };
            grid.appendChild(card);
        });
    }

    /* ── Favorites ──────────────────────────────────── */

    async function loadFavorites(content) {
        const data = await api('/radio-music/radio/favorites');
        _favorites = data.items || [];
        if (!_favorites.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-heart"></i><p>' + t('Brak ulubionych stacji') + '</p></div>';
            return;
        }
        renderStations(_favorites, content);
    }

    async function toggleFavorite(station) {
        const isFav = _favorites.some(f => f.uuid === station.uuid);
        const data = await api('/radio-music/radio/favorites', {
            method: 'POST',
            body: { action: isFav ? 'remove' : 'add', station }
        });
        _favorites = data.items || [];
        loadSection(activeSection);
    }

    /* ── Countries ─────────────────────────────────── */

    async function loadCountries(toolbar, content) {
        toolbar.innerHTML = `<input class="rm-search" id="rm-country-search" placeholder="${t('Szukaj krajów...')}">`;
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';

        if (!_countries.length) {
            const data = await api('/radio-music/radio/countries');
            _countries = data.items || [];
        }

        const searchInput = bodyEl.querySelector('#rm-country-search');
        searchInput.onkeyup = () => renderCountries(searchInput.value, content);
        renderCountries('', content);
    }

    function renderCountries(filter, content) {
        const NAMES = _getCountryNames();
        let items = _countries;
        if (filter) {
            const f = filter.toLowerCase();
            items = items.filter(c => (NAMES[c.code] || c.code).toLowerCase().includes(f) || c.code.toLowerCase().includes(f));
        }
        content.innerHTML = '<div class="rm-chips"></div><div id="rm-country-results"></div>';
        const chips = content.querySelector('.rm-chips');
        items.slice(0, 60).forEach(c => {
            const chip = document.createElement('span');
            chip.className = 'rm-chip';
            chip.textContent = (NAMES[c.code] || c.code) + ' (' + c.count + ')';
            chip.onclick = async () => {
                chips.querySelectorAll('.rm-chip').forEach(ch => ch.classList.remove('active'));
                chip.classList.add('active');
                const results = content.querySelector('#rm-country-results');
                results.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
                const data = await api('/radio-music/radio/search?country=' + c.code + '&limit=50');
                if (data.items && data.items.length) renderStations(data.items, results);
                else results.innerHTML = '<div class="rm-empty"><p>' + t('Brak stacji') + '</p></div>';
            };
            chips.appendChild(chip);
        });
    }

    /* ── Tags / Genres ─────────────────────────────── */

    async function loadTags(content) {
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        if (!_tags.length) {
            const data = await api('/radio-music/radio/tags');
            _tags = data.items || [];
        }
        content.innerHTML = '<div class="rm-chips"></div><div id="rm-tag-results"></div>';
        const chips = content.querySelector('.rm-chips');
        _tags.forEach(tg => {
            const chip = document.createElement('span');
            chip.className = 'rm-chip';
            chip.textContent = tg.name + ' (' + tg.count + ')';
            chip.onclick = async () => {
                chips.querySelectorAll('.rm-chip').forEach(ch => ch.classList.remove('active'));
                chip.classList.add('active');
                const results = content.querySelector('#rm-tag-results');
                results.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
                const data = await api('/radio-music/radio/search?tag=' + encodeURIComponent(tg.name) + '&limit=50');
                if (data.items && data.items.length) renderStations(data.items, results);
                else results.innerHTML = '<div class="rm-empty"><p>' + t('Brak stacji') + '</p></div>';
            };
            chips.appendChild(chip);
        });
    }

    /* ── Podcasts Search ───────────────────────────── */

    async function loadPodcasts(toolbar, content) {
        toolbar.innerHTML = `<input class="rm-search" id="rm-pod-search" placeholder="${t('Szukaj podcastów (iTunes)...')}" autofocus>`;
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-podcast"></i><p>' + t('Wpisz nazwę podcastu') + '</p></div>';

        const searchInput = bodyEl.querySelector('#rm-pod-search');
        let debounce;
        searchInput.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => searchPodcasts(searchInput.value, content), 500);
        };
    }

    async function searchPodcasts(q, content) {
        if (!q.trim()) return;
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        const data = await api('/radio-music/podcasts/search?q=' + encodeURIComponent(q));
        if (!data.items || !data.items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-search"></i><p>' + t('Brak wyników') + '</p></div>';
            return;
        }
        content.innerHTML = '<div class="rm-grid"></div>';
        const grid = content.querySelector('.rm-grid');
        data.items.forEach(p => {
            const card = document.createElement('div');
            card.className = 'rm-card';
            card.innerHTML = `
                <div class="rm-card-icon">${p.artwork ? '<img src="' + escH(p.artwork) + '">' : '<i class="fas fa-podcast"></i>'}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(p.name)}</div>
                    <div class="rm-card-meta">${escH(p.artist)} · ${p.count} ${t('odcinków')}</div>
                </div>`;
            card.onclick = () => openPodcast(p);
            grid.appendChild(card);
        });
    }

    async function openPodcast(podcast) {
        const content = bodyEl.querySelector('#rm-content');
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';

        if (!podcast.feed_url) {
            content.innerHTML = '<div class="rm-empty"><p>' + t('Podcast nie ma feedu RSS') + '</p></div>';
            return;
        }

        const data = await api('/radio-music/podcasts/feed?url=' + encodeURIComponent(podcast.feed_url));
        if (data.error) {
            content.innerHTML = '<div class="rm-empty"><p>' + escH(data.error) + '</p></div>';
            return;
        }

        const isSub = _subscriptions.some(s => s.feed_url === podcast.feed_url);
        const pod = data.podcast || {};
        const eps = data.episodes || [];

        let html = `<div class="rm-pod-header">
            ${pod.image ? '<img class="rm-pod-art" src="' + escH(pod.image) + '">' : ''}
            <div class="rm-pod-details">
                <div class="rm-pod-title">${escH(pod.title || podcast.name)}</div>
                <div class="rm-pod-author">${escH(pod.author || podcast.artist)}</div>
                <div class="rm-pod-desc">${escH(pod.description || '').substring(0, 300)}</div>
                <button class="rm-pod-sub-btn ${isSub ? 'subscribed' : ''}" id="rm-sub-btn">${isSub ? t('Subskrybowano ✓') : t('Subskrybuj')}</button>
            </div>
        </div>`;

        html += '<div class="rm-ep-list">';
        eps.forEach(ep => {
            if (!ep.audio_url) return;
            html += `<div class="rm-ep-item" data-url="${escH(ep.audio_url)}">
                <div class="rm-ep-play"><i class="fas fa-play-circle"></i></div>
                <div class="rm-ep-info">
                    <div class="rm-ep-title">${escH(ep.title)}</div>
                    <div class="rm-ep-meta">${escH([ep.pub_date, ep.duration_fmt].filter(Boolean).join(' · '))}</div>
                </div>
            </div>`;
        });
        html += '</div>';

        content.innerHTML = html;

        // Subscribe button
        content.querySelector('#rm-sub-btn').onclick = async () => {
            const action = isSub ? 'remove' : 'add';
            const res = await api('/radio-music/podcasts/subscribe', {
                method: 'POST',
                body: { action, podcast: { ...podcast, image: pod.image || podcast.artwork, title: pod.title || podcast.name } }
            });
            _subscriptions = res.items || [];
            openPodcast(podcast);
        };

        // Episode play
        content.querySelectorAll('.rm-ep-item').forEach(el => {
            el.onclick = () => {
                const url = el.dataset.url;
                const title = el.querySelector('.rm-ep-title').textContent;
                playAudio({
                    name: title,
                    url: url,
                    type: 'podcast',
                    meta: pod.title || podcast.name,
                    image: pod.image || podcast.artwork || '',
                });
            };
        });
    }

    /* ── Subscriptions ─────────────────────────────── */

    async function loadSubscriptions(content) {
        const data = await api('/radio-music/podcasts/subscriptions');
        _subscriptions = data.items || [];
        if (!_subscriptions.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-rss"></i><p>' + t('Brak subskrypcji') + '</p></div>';
            return;
        }
        content.innerHTML = '<div class="rm-grid"></div>';
        const grid = content.querySelector('.rm-grid');
        _subscriptions.forEach(p => {
            const card = document.createElement('div');
            card.className = 'rm-card';
            card.innerHTML = `
                <div class="rm-card-icon">${p.artwork || p.image ? '<img src="' + escH(p.artwork || p.image) + '">' : '<i class="fas fa-podcast"></i>'}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(p.name || p.title)}</div>
                    <div class="rm-card-meta">${escH(p.artist || p.author || '')}</div>
                </div>`;
            card.onclick = () => openPodcast(p);
            grid.appendChild(card);
        });
    }

    /* ── History ────────────────────────────────────── */

    async function loadHistory(content) {
        const data = await api('/radio-music/history');
        const items = data.items || [];
        if (!items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-history"></i><p>' + t('Brak historii odtwarzania') + '</p></div>';
            return;
        }
        content.innerHTML = '<div class="rm-grid"></div>';
        const grid = content.querySelector('.rm-grid');
        items.forEach(item => {
            const card = document.createElement('div');
            card.className = 'rm-card';
            const icon = item.type === 'podcast' ? 'fa-podcast' : 'fa-broadcast-tower';
            card.innerHTML = `
                <div class="rm-card-icon">${item.image || item.favicon ? '<img src="' + escH(item.image || item.favicon) + '">' : '<i class="fas ' + icon + '"></i>'}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(item.name)}</div>
                    <div class="rm-card-meta">${escH(item.meta || item.country || '')}</div>
                </div>`;
            card.onclick = () => {
                if (item.type === 'podcast') playAudio(item);
                else playStation(item);
            };
            grid.appendChild(card);
        });
    }

    /* ── Playback Engine ───────────────────────────── */

    function playStation(station) {
        playAudio({
            name: station.name,
            url: station.url,
            alt_urls: station.alt_urls || [],
            type: 'radio',
            meta: [station.country, station.tags].filter(Boolean).join(' · '),
            image: station.favicon || '',
            uuid: station.uuid,
        });
    }

    function playAudio(item) {
        if (_audio) { _audio.pause(); _audio.src = ''; }
        _audio = new Audio();
        _audio.volume = (bodyEl.querySelector('#rm-vol')?.value || 80) / 100;
        _playing = item;

        // Build ordered list of URLs to try (primary + fallbacks)
        const urls = [item.url, ...(item.alt_urls || [])];
        let urlIdx = 0;
        let hasPlayed = false;

        function tryUrl(idx) {
            if (idx >= urls.length) {
                toast(t('Nie udało się odtworzyć żadnego źródła'), 'error');
                _showEq(false);
                return;
            }
            const src = item.type === 'radio'
                ? '/api/radio-music/radio/proxy?url=' + encodeURIComponent(urls[idx])
                  + '&token=' + (NAS.token || '')
                : urls[idx];

            _audio.src = src;
            _audio.play().catch(() => {
                // Silently try next fallback
                tryUrl(idx + 1);
            });
        }

        _audio.onplay = () => {
            hasPlayed = true;
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-pause"></i>';
            _showEq(true);
        };
        _audio.onpause = () => {
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';
            _showEq(false);
        };
        _audio.onerror = () => {
            if (!hasPlayed) {
                // Haven't successfully played yet — try next fallback
                urlIdx++;
                tryUrl(urlIdx);
            } else {
                // Was playing but stream died — show error
                toast(t('Strumień przerwany'), 'error');
                _showEq(false);
            }
        };

        // Update player bar
        const player = bodyEl.querySelector('#rm-player');
        player.style.display = 'flex';
        bodyEl.querySelector('#rm-player-name').textContent = item.name;
        bodyEl.querySelector('#rm-player-meta').textContent = item.meta || '';
        const art = bodyEl.querySelector('#rm-player-art');
        if (item.image) {
            const letter = _stationInitial(item.name);
            const bg = _stationColor(item.name);
            art.innerHTML = '<img src="' + escH(item.image) + '" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
                + '<span class="rm-letter-icon" style="display:none;background:' + bg + '">' + escH(letter) + '</span>';
        } else {
            const letter = _stationInitial(item.name);
            const bg = _stationColor(item.name);
            art.innerHTML = '<span class="rm-letter-icon" style="background:' + bg + '">' + escH(letter) + '</span>';
        }

        // Save to history
        api('/radio-music/history', { method: 'POST', body: { item } });

        // Highlight playing card
        bodyEl.querySelectorAll('.rm-card, .rm-ep-item').forEach(c => c.classList.remove('rm-playing'));

        // Start playback with fallback chain
        tryUrl(0);
    }

    function _skipStation(dir) {
        if (!_playing || !_recentStations.length) return;
        const idx = _recentStations.findIndex(s => s.uuid === _playing.uuid || s.name === _playing.name);
        let next = idx + dir;
        if (next < 0) next = _recentStations.length - 1;
        if (next >= _recentStations.length) next = 0;
        playStation(_recentStations[next]);
    }

    function stopPlayback() {
        if (_audio) {
            _audio.pause();
            _audio.src = '';
            _audio = null;
        }
        _playing = null;
        const player = bodyEl?.querySelector('#rm-player');
        if (player) player.style.display = 'none';
        _showEq(false);
    }

    function _showEq(show) {
        const eq = bodyEl?.querySelector('#rm-player-eq');
        if (eq) eq.style.display = show ? 'flex' : 'none';
    }

    /* ── Country name helper ───────────────────────── */
    function _getCountryNames() {
        return {PL:'Polska',US:'USA',GB:'Wielka Brytania',DE:'Niemcy',FR:'Francja',ES:'Hiszpania',IT:'Włochy',NL:'Holandia',
            BR:'Brazylia',CA:'Kanada',AU:'Australia',JP:'Japonia',KR:'Korea Płd.',IN:'Indie',RU:'Rosja',UA:'Ukraina',
            CZ:'Czechy',AT:'Austria',CH:'Szwajcaria',SE:'Szwecja',NO:'Norwegia',DK:'Dania',FI:'Finlandia',PT:'Portugalia',
            MX:'Meksyk',AR:'Argentyna',CL:'Chile',CO:'Kolumbia',BE:'Belgia',IE:'Irlandia',GR:'Grecja',TR:'Turcja',
            RO:'Rumunia',HU:'Węgry',SK:'Słowacja',BG:'Bułgaria',HR:'Chorwacja',RS:'Serbia',SI:'Słowenia',LT:'Litwa',
            LV:'Łotwa',EE:'Estonia',IL:'Izrael',ZA:'RPA',EG:'Egipt',NG:'Nigeria',KE:'Kenia',TH:'Tajlandia',
            PH:'Filipiny',MY:'Malezja',ID:'Indonezja',VN:'Wietnam',NZ:'Nowa Zelandia',CN:'Chiny',TW:'Tajwan'};
    }

};
