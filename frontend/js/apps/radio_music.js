/* eslint-disable */
/**
 * Radio & Music — internet radio, podcasts, and music player.
 * CSS prefix: rm-
 */
AppRegistry['radio-music'] = function(appDef, launchOpts) {

    let bodyEl, activeSection = 'most-played', _audio = null, _playing = null;
    let _favorites = [], _subscriptions = [], _countries = [], _tags = [];
    let _likedSongs = [];     // user's liked music tracks (heart in NP overlay)
    let _recentStations = [];  // for prev/next navigation
    let _musicQueue = [];      // music track queue (music + audiobook types only)
    let _musicQueueIdx = -1;   // index of currently playing track in queue
    let _podQueue = [];        // podcast episode queue (separate from music)
    let _podQueueIdx = -1;
    let _savedMusicQueue = null; // snapshot saved when switching to radio
    let _repeatMode = 0;       // 0=off, 1=repeat all, 2=repeat one
    let _shuffle = false;
    let _ytdlpReady = null;    // null = unknown, true/false
    let _seekInterval = null;  // interval for updating seekbar
    let _playlists = [];       // user's playlists
    let _saveStateInterval = null;
    let _seekThrottleTs = 0;   // throttle seekbar DOM updates (ms)
    let _preloadAudio = null;  // preload next track for near-gapless playback
    let _radioRetryTimer = null;
    let _radioRetries = 0;
    // Offline archive: keyed by YT URL → {key, status, progress, size_bytes}
    let _archiveDb = {};
    let _swReady = false;      // Service Worker available for offline caching
    let _nasSpinTimer = null;  // detect slow NAS wake (>3s)
    let _queueContent = null;  // DOM node of the queue panel (null when not visible)
    let _renderNpQueueFn = null; // ref to _renderNpQueue inside the overlay closure
    let _npSyncFav = null;       // ref to sync NP favorite button state
    let _npSyncDownload = null;  // ref to sync NP download button state
    let _npReloadSimilar = null;  // ref to reload similar artists on track change
    let _activePolls = [];       // download poll intervals to clear on close
    let _sleepTimer = null;      // sleep timer timeout ID
    let _sleepEnd = 0;           // timestamp when sleep timer fires (0 = off)
    let _sleepMode = '';         // 'time' or 'track'
    let _playbackRate = 1;       // current playback speed (0.5–2)
    let _crossfadeDuration = 1500; // crossfade ms, user-configurable 0–12000
    let _syncedLyrics = null;    // parsed LRC lines: [{time: ms, text: ''}, ...]
    let _lyrSyncInterval = null; // lyrics auto-scroll timer
    let _epProgress = {};        // podcast episode progress: {url: {pos: sec, dur: sec, done: bool}}

    // ── Local radio logo cache (UUID → /img/radio-logos/filename) ──
    let _logoManifest = null;
    fetch('/img/radio-logos/manifest.json').then(r => r.ok ? r.json() : {}).then(d => { _logoManifest = d; }).catch(() => { _logoManifest = {}; });

    // ── Chromecast state ──
    let _isCasting = false;
    let _castSession = null;
    let _castAvail = false;
    let _preCastVolume = 0.8;
    let _castPlayer = null;
    let _castController = null;
    let _advanceLock = false;   // debounce double-advance from Cast + local onended
    let _castQueueActive = false; // true when Cast queue manages playlist advancement
    let _isBuffering = false;   // true while track is loading — blocks rapid Next/Prev
    let _bufferingSafetyTimer = null; // auto-release buffering after timeout
    // LAN origin for Chromecast URLs (fetched once from /cast-info; Chromecast cannot use localhost)
    let _castLanOrigin = location.origin;

    // Web Audio API — shared across plays (createMediaElementSource can only be called once per element)
    let _audioCtx = null, _analyser = null, _audioSource = null, _visRafId = null;

    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('radio-music', level, msg, details) : console.log('[radio-music]', msg, details || '');

    const _LS_KEY = 'rm_playback_state';
    let _savePending = false;

    // ── Shared playback state store (Observable pattern) ──────────────────────
    // Single source of truth for currentTrack + queueIndex so PlaylistView and
    // NowPlayingOverlay always stay in sync without rebuilding DOM.
    const _rmStore = (() => {
        let _state = { currentTrack: null, currentTrackIndex: -1 };
        const _subs = [];
        return {
            get state() { return _state; },
            set(patch) {
                _state = Object.assign({}, _state, patch);
                _subs.forEach(fn => { try { fn(_state); } catch(e) {} });
            },
            subscribe(fn) {
                _subs.push(fn);
                return () => { const i = _subs.indexOf(fn); if (i > -1) _subs.splice(i, 1); };
            }
        };
    })();

    // Sidebar group definitions — order is user-customisable via DnD (saved in localStorage)
    const _SIDEBAR_GROUPS = [
        { id: 'discover', label: 'Odkrywaj', items: [
            { key: 'search', icon: 'fas fa-search', label: 'Szukaj wszędzie' },
            { key: 'discovery', icon: 'fas fa-compass', label: 'Odkrywaj' },
        ]},
        { id: 'music', label: 'Muzyka', items: [
            { key: 'music', icon: 'fab fa-youtube', label: 'Szukaj' },
            { key: 'local', icon: 'fas fa-folder-open', label: 'Lokalna muzyka' },
            { key: 'artists', icon: 'fas fa-user-circle', label: 'Artyści' },
            { key: 'recently-added', icon: 'fas fa-clock', label: 'Ostatnio dodane' },
            { key: 'local-audiobooks', icon: 'fas fa-book-reader', label: 'Lok. audiobooki' },
            { key: 'playlists', icon: 'fas fa-list', label: 'Playlisty' },
            { key: 'queue', icon: 'fas fa-list-ol', label: 'Kolejka' },
        ]},
        { id: 'radio', label: 'Radio', items: [
            { key: 'radio', icon: 'fas fa-broadcast-tower', label: 'Przeglądaj' },
            { key: 'favorites', icon: 'fas fa-heart', label: 'Ulubione' },
            { key: 'countries', icon: 'fas fa-globe', label: 'Kraje' },
            { key: 'tags', icon: 'fas fa-tags', label: 'Gatunki' },
        ]},
        { id: 'podcasts', label: 'Podcasty', items: [
            { key: 'podcasts', icon: 'fas fa-podcast', label: 'Szukaj' },
            { key: 'subscriptions', icon: 'fas fa-rss', label: 'Subskrypcje' },
            { key: 'pod-queue', icon: 'fas fa-list-ul', label: 'Kolejka odcinków' },
        ]},
        { id: 'other', label: 'Inne', items: [
            { key: 'most-played', icon: 'fas fa-fire', label: 'Najczęściej grane' },
            { key: 'audiobooks', icon: 'fas fa-book-open', label: 'Audiobooki' },
            { key: 'history', icon: 'fas fa-history', label: 'Historia' },
            { key: 'settings', icon: 'fas fa-cog', label: 'Ustawienia' },
        ]},
    ];

    // Detect user's country from browser language (e.g. 'pl-PL' → 'PL')
    function _detectCountry() {
        const saved = localStorage.getItem('rm_user_country');
        if (saved) return saved;
        const lang = navigator.language || navigator.languages?.[0] || 'en';
        const parts = lang.split('-');
        const country = parts.length > 1 ? parts[1].toUpperCase() : parts[0].toUpperCase();
        return country;
    }

    const _POD_GENRES = [
        {key:'', label:'Wszystkie'}, {key:'truecrime', label:'True Crime'}, {key:'comedy', label:'Komedia'},
        {key:'news', label:'Wiadomości'}, {key:'society', label:'Społeczeństwo'}, {key:'education', label:'Edukacja'},
        {key:'technology', label:'Technologia'}, {key:'business', label:'Biznes'}, {key:'health', label:'Zdrowie'},
        {key:'history', label:'Historia'}, {key:'science', label:'Nauka'}, {key:'sports', label:'Sport'},
        {key:'music', label:'Muzyka'}, {key:'arts', label:'Sztuka'}, {key:'fiction', label:'Fikcja'},
        {key:'kids', label:'Dla dzieci'}, {key:'tv', label:'TV i Film'},
    ];
    const _POD_COUNTRIES = [
        {code:'pl',name:'Polska'},{code:'us',name:'USA'},{code:'gb',name:'UK'},{code:'de',name:'Niemcy'},
        {code:'fr',name:'Francja'},{code:'es',name:'Hiszpania'},{code:'it',name:'Włochy'},
        {code:'br',name:'Brazylia'},{code:'ca',name:'Kanada'},{code:'au',name:'Australia'},
        {code:'jp',name:'Japonia'},{code:'se',name:'Szwecja'},{code:'nl',name:'Holandia'},
    ];
    const _MUSIC_GENRES = [
        {q:'top hits 2024 2025', label:'🔥 Hity'},
        {q:'pop music', label:'Pop'}, {q:'rock music', label:'Rock'},
        {q:'hip hop rap', label:'Hip-Hop'}, {q:'electronic dance music', label:'Electronic'},
        {q:'r&b soul music', label:'R&B'}, {q:'jazz music', label:'Jazz'},
        {q:'classical music', label:'Klasyczna'}, {q:'reggae music', label:'Reggae'},
        {q:'metal music', label:'Metal'}, {q:'indie alternative', label:'Indie'},
        {q:'polish music polskie', label:'🇵🇱 Polskie'},
    ];

    let _npOverlay = null;
    let _npSeekDragging = false;
    let _npMinimizing = false;  // debounce guard for _onPopState / _minimizeNowPlaying
    let _lockOverlay = null;
    let _wakeLock = null;
    let _seekLocked = false;    // true during track switch → seekbar won't jump to 0
    let _hlsInstance = null;    // hls.js instance for HLS live streams
    let _prevNextTs = 0;        // debounce timestamp for Next/Prev buttons (500ms cooldown)

    // BroadcastChannel — coordinate multi-tab audio (E-07/E-12): only one tab plays at a time
    const _tabId = Math.random().toString(36).slice(2);
    let _bc = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('rm-audio-master') : null;
    if (_bc) {
        _bc.onmessage = (e) => {
            // Another tab started playing — pause this tab silently
            if (e.data.type === 'playing' && e.data.tabId !== _tabId && _audio && !_audio.paused) {
                _audio.pause();
                _cl('info', 'Paused: another tab took audio focus', { remoteTab: e.data.tabId });
            }
        };
    }

    const _AUDIOBOOK_CATEGORIES = [
        {q: 'najlepsze audiobooki dla dzieci po polsku 2024 2025', label: '🏆 Top bajki'},
        {q: 'bajki dla dzieci audiobook po polsku', label: '🇵🇱 Bajki po polsku'},
        {q: 'bajki na dobranoc dla dzieci audiobook', label: '🌙 Na dobranoc'},
        {q: 'baśnie braci grimm audiobook dla dzieci', label: '📖 Baśnie Grimm'},
        {q: 'baśnie andersena audiobook dla dzieci', label: '👑 Andersen'},
        {q: 'harry potter audiobook po polsku', label: '⚡ Harry Potter'},
        {q: 'władca pierścieni audiobook po polsku', label: '💍 Władca Pierścieni'},
        {q: 'narnia audiobook po polsku', label: '🦁 Opowieści z Narnii'},
        {q: 'mały książę audiobook po polsku', label: '🌹 Mały Książę'},
        {q: 'pippi langstrumpf audiobook po polsku', label: '🧦 Pippi'},
        {q: 'muminki audiobook po polsku', label: '🏔️ Muminki'},
        {q: 'kubuś puchatek audiobook', label: '🍯 Kubuś Puchatek'},
        {q: 'smerfy audiobook bajka po polsku', label: '🔵 Smerfy'},
        {q: 'masza i niedźwiedź bajka audiobook po polsku', label: '🐻 Masza'},
        {q: 'franklin żółw audiobook bajka po polsku', label: '🐢 Franklin'},
        {q: 'bolek i lolek audiobook bajka', label: '👦 Bolek i Lolek'},
        {q: 'reksio audiobook bajka po polsku', label: '🐕 Reksio'},
        {q: 'przygody audiobook dla dzieci po polsku', label: '🏴‍☠️ Przygody'},
        {q: 'bajki zwierzęta audiobook dla dzieci', label: '🦊 Zwierzęta'},
        {q: 'audiobook dla dzieci edukacyjny', label: '🎓 Edukacyjne'},
        {q: 'pan tadeusz audiobook lektura', label: '📚 Lektury'},
        {q: 'audiobook children english fairy tales', label: '🇬🇧 English'},
    ];

    function _buildPlaybackState() {
        if (!_playing) return null;
        return {
            playing: _playing,
            queue: _musicQueue.slice(0, 200),
            queueIdx: _musicQueueIdx,
            repeatMode: _repeatMode,
            shuffle: _shuffle,
            currentTime: _audio ? _audio.currentTime : 0,
            duration: _audio && isFinite(_audio.duration) ? _audio.duration : 0,
            ts: Date.now(),
        };
    }

    function _savePlaybackState() {
        const state = _buildPlaybackState();
        if (!state) { localStorage.removeItem(_LS_KEY); return; }
        try { localStorage.setItem(_LS_KEY, JSON.stringify(state)); } catch (e) {}
        // Debounce server save (max once per 5s)
        if (!_savePending) {
            _savePending = true;
            setTimeout(() => {
                _savePending = false;
                const s = _buildPlaybackState();
                if (s) api('/radio-music/playback-state', { method: 'POST', body: s }).catch(() => {});
            }, 3000);
        }
    }

    async function _restorePlaybackState() {
        // Try localStorage first (fast)
        try {
            const raw = localStorage.getItem(_LS_KEY);
            if (raw) {
                const state = JSON.parse(raw);
                if (state && state.playing && (Date.now() - (state.ts || 0)) < 7 * 86400000) return state;
            }
        } catch (e) {}
        // Fall back to server (cross-device)
        try {
            const data = await api('/radio-music/playback-state');
            if (data && data.playing && (Date.now() - (data.ts || 0)) < 7 * 86400000) return data;
        } catch (e) {}
        return null;
    }

    async function _restoreAndShowLastTrack(body) {
        const state = await _restorePlaybackState();
        if (!state) return;
        const item = state.playing;

        // Restore queue and mode settings
        _musicQueue = state.queue || [];
        _musicQueueIdx = state.queueIdx ?? -1;
        _repeatMode = state.repeatMode || 0;
        _shuffle = !!state.shuffle;
        _playing = item;

        // Show player bar in paused state
        const player = body.querySelector('#rm-player');
        player.style.display = 'flex';
        body.querySelector('#rm-player-name').textContent = item.name;
        body.querySelector('#rm-player-meta').textContent = item.meta || '';
        body.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';

        // Player art
        const art = body.querySelector('#rm-player-art');
        const isMusic = item.type === 'music' || item.type === 'local';
        if (isMusic && item.image) {
            art.innerHTML = '<img src="' + escH(item.image) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">';
        } else if (item.image || item.favicon) {
            art.innerHTML = '<img src="' + escH(item.image || item.favicon) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">';
        }

        // Show seekbar with saved position
        const savedTime = state.currentTime || 0;
        const savedDur = state.duration || 0;
        if (savedDur > 0) {
            const seekbar = body.querySelector('#rm-seekbar');
            if (seekbar) {
                seekbar.classList.add('visible');
                const pct = (savedTime / savedDur) * 100;
                const fill = seekbar.querySelector('#rm-seek-fill');
                const thumb = seekbar.querySelector('#rm-seek-thumb');
                if (fill) fill.style.width = pct + '%';
                if (thumb) thumb.style.left = pct + '%';
                const curEl = seekbar.querySelector('#rm-seek-cur');
                const durEl = seekbar.querySelector('#rm-seek-dur');
                if (curEl) curEl.textContent = _fmtTime(savedTime);
                if (durEl) durEl.textContent = _fmtTime(savedDur);
            }
        }

        // Override play/pause to resume from saved position on first click
        const playPauseBtn = body.querySelector('#rm-play-pause');
        const _origHandler = playPauseBtn.onclick;
        playPauseBtn.onclick = () => {
            playPauseBtn.onclick = _origHandler;
            // Start playback from saved position
            playAudio(item);
            // Seek to saved position once audio is ready
            if (savedTime > 1) {
                const _onReady = () => {
                    if (_audio && isFinite(_audio.duration) && savedTime < _audio.duration) {
                        _audio.currentTime = savedTime;
                    }
                    if (_audio) _audio.removeEventListener('canplay', _onReady);
                };
                if (_audio) _audio.addEventListener('canplay', _onReady, { once: true });
            }
        };

        // Sync repeat/shuffle button states
        const repeatBtn = body.querySelector('#rm-repeat-btn');
        if (repeatBtn) {
            repeatBtn.classList.toggle('rm-mode-active', _repeatMode > 0);
            repeatBtn.innerHTML = _repeatMode === 2 ? '<i class="fas fa-redo"></i><span style="font-size:9px;position:absolute;font-weight:700">1</span>' : '<i class="fas fa-redo"></i>';
            repeatBtn.style.position = _repeatMode === 2 ? 'relative' : '';
        }
        const shuffleBtn = body.querySelector('#rm-shuffle-btn');
        if (shuffleBtn) shuffleBtn.classList.toggle('rm-mode-active', _shuffle);
    }

    const escH = s => s ? s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';

    // Deterministic color from station name (for letter-avatar fallback)
    const _COLORS = ['#ef4444','#f97316','#f59e0b','#22c55e','#14b8a6','#3b82f6','#6366f1','#a855f7','#ec4899','#06b6d4'];
    function _stationColor(name) { if (!name) return _COLORS[0]; let h=0; for(let i=0;i<name.length;i++) h=((h<<5)-h)+name.charCodeAt(i); return _COLORS[Math.abs(h)%_COLORS.length]; }
    function _stationInitial(name) { return (name||'?').replace(/^(radio|polskie)\s*/i,'').charAt(0).toUpperCase(); }

    // Extract domain from a URL for logo services
    function _domainOf(url) {
        if (!url) return '';
        try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; }
    }

    // Build a Google high-res favicon URL from a domain
    function _googleIcon(domain) {
        return domain ? 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(domain) + '&sz=128' : '';
    }

    // Format seconds to M:SS or H:MM:SS
    function _fmtSecs(s) {
        if (!s) return '';
        s = Math.round(s);
        if (s >= 3600) return Math.floor(s / 3600) + ':' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
        return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    }

    function _skeletonGrid(count) {
        let html = '<div class="rm-skel-grid">';
        for (let i = 0; i < (count || 6); i++) html += '<div class="rm-skel-card"></div>';
        return html + '</div>';
    }

    function _skeletonTracks(count) {
        let html = '';
        for (let i = 0; i < (count || 5); i++) html += '<div class="rm-skel-track"></div>';
        return html;
    }

    // Build playlist cover art: 2×2 mosaic from first 4 track thumbnails, or fallback icon
    function _playlistCoverHtml(pl, size) {
        const imgs = (pl.tracks || []).map(t => t.image || t.thumbnail || t.favicon || '').filter(Boolean);
        const unique = [...new Set(imgs)].slice(0, 4);
        if (!unique.length) {
            return '<div class="rm-pl-icon" style="width:' + size + 'px;height:' + size + 'px"><i class="fas fa-music"></i></div>';
        }
        if (unique.length === 1) {
            return '<img src="' + escH(unique[0]) + '" style="width:' + size + 'px;height:' + size + 'px;border-radius:8px;object-fit:cover" onerror="this.outerHTML=\'<div class=\\\'rm-pl-icon\\\' style=\\\'width:' + size + 'px;height:' + size + 'px\\\'><i class=\\\'fas fa-music\\\'></i></div>\'">';
        }
        // 2×2 grid mosaic
        const half = Math.floor(size / 2);
        let h = '<div style="width:' + size + 'px;height:' + size + 'px;border-radius:8px;overflow:hidden;display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;flex-shrink:0">';
        for (let i = 0; i < 4; i++) {
            const src = unique[i % unique.length];
            h += '<img src="' + escH(src) + '" style="width:100%;height:100%;object-fit:cover" loading="lazy" onerror="this.style.background=\'#282828\';this.removeAttribute(\'src\')">';
        }
        h += '</div>';
        return h;
    }

    // Multi-layer logo: Google favicon (128px, from homepage domain) → Radio Browser favicon → letter avatar
    // Google service returns high-quality logos; Radio Browser favicons are often tiny/broken
    // Priority: local cached logo (from manifest) → Google favicon → RB favicon → letter avatar
    function _stationIconHtml(s) {
        const letter = _stationInitial(s.name);
        const bg = _stationColor(s.name);
        const letterFallback = '<span class="rm-letter-icon" style="display:none;background:' + bg + '">' + escH(letter) + '</span>';
        const uuid = s.stationuuid || s.uuid || '';
        const localFile = uuid && _logoManifest ? _logoManifest[uuid] : null;
        const localSrc = localFile ? '/img/radio-logos/' + localFile : null;

        // If we have a pre-downloaded local logo, use it first (fast, no external request)
        if (localSrc) {
            return '<img src="' + escH(localSrc) + '" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
                 + letterFallback;
        }

        const domain = _domainOf(s.homepage || s.url);
        const googleSrc = _googleIcon(domain);
        const rbFavicon = s.favicon || '';

        if (googleSrc && rbFavicon) {
            // Google → Radio Browser → letter
            return '<img src="' + escH(googleSrc) + '" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline\'">'
                 + '<img style="display:none" src="' + escH(rbFavicon) + '" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
                 + letterFallback;
        }
        if (googleSrc) {
            return '<img src="' + escH(googleSrc) + '" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
                 + letterFallback;
        }
        if (rbFavicon) {
            return '<img src="' + escH(rbFavicon) + '" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
                 + letterFallback;
        }
        return '<span class="rm-letter-icon" style="background:' + bg + '">' + escH(letter) + '</span>';
    }

    function getCSS() { return [
/* ── CSS Variables ─────────────────────────────────────── */
'.rm-wrap{--rm-accent:#1DB954;--rm-accent-hover:#1ed760;--rm-accent-active:#0f9240;--rm-accent-rgb:29,185,84;--rm-bg:#121212;--rm-bg-sidebar:#000;--rm-bg-surface:#282828;--rm-bg-elevated:#181818;--rm-bg-card:#282828;--rm-text:#fff;--rm-text-secondary:rgba(255,255,255,.65);--rm-text-muted:rgba(255,255,255,.35);--rm-text-dim:rgba(255,255,255,.2);--rm-border:rgba(255,255,255,.08);--rm-error:#ef4444;--rm-warning:#f59e0b;--rm-overlay:rgba(0,0,0,.85)}',
'[data-theme="light"] .rm-wrap{--rm-bg:#f5f5f5;--rm-bg-sidebar:#e8e8e8;--rm-bg-surface:#fff;--rm-bg-elevated:#f0f0f0;--rm-bg-card:#fff;--rm-text:#1a1a1a;--rm-text-secondary:rgba(0,0,0,.65);--rm-text-muted:rgba(0,0,0,.4);--rm-text-dim:rgba(0,0,0,.2);--rm-border:rgba(0,0,0,.1);--rm-overlay:rgba(255,255,255,.9)}',
/* ── Spotify-inspired layout ─────────────────────────── */
'.rm-wrap{display:flex;height:100%;overflow:hidden;font-size:13px;background:var(--rm-bg);color:var(--rm-text);border-radius:0}',
'.rm-sidebar{width:220px;min-width:220px;background:var(--rm-bg-sidebar);display:flex;flex-direction:column;overflow-y:auto;padding:8px 0;scrollbar-width:thin;scrollbar-color:var(--rm-text-dim) transparent}',
'.rm-sidebar-item{padding:10px 20px;cursor:pointer;display:flex;align-items:center;gap:10px;color:var(--rm-text-secondary);font-size:13px;transition:all .15s;border-radius:0;border-left:3px solid transparent}',
'.rm-sidebar-item:hover{color:var(--rm-text);background:rgba(255,255,255,.05)}',
'.rm-sidebar-item.active{color:var(--rm-accent);border-left-color:var(--rm-accent);background:rgba(var(--rm-accent-rgb),.08);font-weight:600}',
'.rm-sidebar-item i{width:18px;text-align:center;font-size:14px}',
'.rm-sidebar-label{padding:20px 20px 6px;font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:var(--rm-text-muted);font-weight:700;display:flex;align-items:center;justify-content:space-between;user-select:none}',
'.rm-sidebar-group{transition:opacity .15s}',
'.rm-sidebar-group.rm-dnd-dragging{opacity:.4}',
'.rm-sidebar-group.rm-dnd-over{box-shadow:inset 0 2px 0 var(--rm-accent)}',
'.rm-sidebar-edit-btn{background:none;border:none;color:var(--rm-text-muted);font-size:11px;cursor:pointer;padding:2px 8px 2px 4px;border-radius:4px;transition:color .15s;white-space:nowrap}',
'.rm-sidebar-edit-btn:hover{color:var(--rm-accent)}',
'.rm-sidebar-drag-handle{display:none;color:rgba(255,255,255,.25);font-size:12px;padding:0 8px;cursor:grab}',
'.rm-sidebar-edit-mode .rm-sidebar-drag-handle{display:block}',
'.rm-sidebar-edit-mode .rm-sidebar-group{cursor:default}',
'.rm-sidebar-edit-mode .rm-sidebar-label{color:var(--rm-text-secondary)}',
'.rm-sidebar-done-btn{display:none;margin:8px 12px;padding:8px 16px;background:var(--rm-accent);color:#000;border:none;border-radius:20px;font-size:12px;font-weight:700;cursor:pointer;width:calc(100% - 24px)}',
'.rm-sidebar-done-btn:hover{background:var(--rm-accent-hover)}',
'.rm-sidebar-edit-mode .rm-sidebar-done-btn{display:block}',
/* Discovery section — immersive design */
'.rm-disc-country{display:flex;align-items:center;gap:10px;padding:16px 20px 8px;background:rgba(255,255,255,.03);border-bottom:1px solid rgba(255,255,255,.06);flex-wrap:wrap}',
'.rm-disc-country-label{font-size:12px;color:rgba(255,255,255,.5)}',
'.rm-disc-country-select{padding:6px 12px;border:1px solid rgba(255,255,255,.1);border-radius:16px;background:rgba(255,255,255,.06);color:var(--rm-text);font-size:12px;outline:none;cursor:pointer}',
/* Hero banner */
'.rm-disc-hero{position:relative;padding:40px 28px 32px;margin:-20px -20px 24px;overflow:hidden;border-radius:0 0 20px 20px}',
'.rm-disc-hero-bg{position:absolute;inset:0;background:linear-gradient(135deg,rgba(var(--rm-accent-rgb),.45) 0%,rgba(80,30,120,.55) 50%,rgba(20,20,40,.85) 100%);z-index:0}',
'.rm-disc-hero-bg::after{content:"";position:absolute;inset:0;background:radial-gradient(ellipse at 30% 20%,rgba(var(--rm-accent-rgb),.3),transparent 60%)}',
'.rm-disc-hero-content{position:relative;z-index:1}',
'.rm-disc-hero h2{font-size:26px;font-weight:800;color:#fff;margin:0 0 6px;letter-spacing:-.5px}',
'.rm-disc-hero p{font-size:14px;color:rgba(255,255,255,.7);margin:0 0 20px}',
'.rm-disc-hero-chips{display:flex;gap:8px;flex-wrap:wrap}',
'.rm-disc-hero-chip{padding:8px 18px;border-radius:24px;background:rgba(255,255,255,.12);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.15);color:#fff;font-size:12px;font-weight:600;cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:6px}',
'.rm-disc-hero-chip:hover{background:rgba(var(--rm-accent-rgb),.4);border-color:var(--rm-accent);transform:scale(1.04)}',
'.rm-disc-hero-chip i{font-size:11px;color:var(--rm-accent)}',
/* Section headers */
'.rm-disc-section{margin-bottom:32px;animation:rm-disc-fadein .4s ease both}',
'@keyframes rm-disc-fadein{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}',
'.rm-disc-section:nth-child(2){animation-delay:.05s}',
'.rm-disc-section:nth-child(3){animation-delay:.1s}',
'.rm-disc-section:nth-child(4){animation-delay:.15s}',
'.rm-disc-section:nth-child(5){animation-delay:.2s}',
'.rm-disc-title{font-size:16px;font-weight:700;color:var(--rm-text);padding:0 0 12px;display:flex;align-items:center;gap:10px}',
'.rm-disc-title i{color:var(--rm-accent);font-size:14px}',
'.rm-disc-title .rm-disc-seeall{margin-left:auto;font-size:11px;font-weight:500;color:var(--rm-accent);cursor:pointer;opacity:.7;transition:opacity .15s}',
'.rm-disc-title .rm-disc-seeall:hover{opacity:1}',
/* Carousel */
'.rm-disc-carousel{display:flex;gap:14px;overflow-x:auto;padding:0 0 16px;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;scrollbar-width:none}',
'.rm-disc-carousel::-webkit-scrollbar{display:none}',
/* Cards — larger, glassmorphic */
'.rm-disc-card{flex-shrink:0;width:175px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.06);border-radius:14px;overflow:hidden;cursor:pointer;transition:all .2s ease;scroll-snap-align:start;touch-action:pan-x}',
'.rm-disc-card:hover{background:rgba(255,255,255,.12);transform:translateY(-4px);box-shadow:0 8px 32px rgba(0,0,0,.3)}',
'.rm-disc-card:active{transform:scale(.97)}',
'.rm-disc-card-art{width:175px;height:175px;overflow:hidden;background:var(--rm-bg-surface);position:relative}',
'.rm-disc-card-art img{width:100%;height:100%;object-fit:cover;transition:transform .3s ease}',
'.rm-disc-card:hover .rm-disc-card-art img{transform:scale(1.06)}',
'.rm-disc-card-art i{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:36px;color:rgba(255,255,255,.2)}',
'.rm-disc-card-art .rm-disc-play-overlay{position:absolute;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .2s}',
'.rm-disc-card:hover .rm-disc-play-overlay{opacity:1}',
'.rm-disc-play-overlay i{font-size:32px;color:#fff;filter:drop-shadow(0 2px 8px rgba(0,0,0,.5))}',
'.rm-disc-card-body{padding:12px 14px}',
'.rm-disc-card-title{font-size:13px;font-weight:600;color:var(--rm-text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.rm-disc-card-meta{font-size:11px;color:rgba(255,255,255,.4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:4px}',
'.rm-disc-badge{position:absolute;top:8px;left:8px;font-size:9px;font-weight:700;padding:3px 8px;border-radius:10px;backdrop-filter:blur(6px);letter-spacing:.5px}',
'.rm-disc-badge-radio{background:rgba(var(--rm-accent-rgb),.85);color:#000}',
'.rm-disc-badge-pod{background:rgba(100,100,255,.85);color:var(--rm-text)}',
'.rm-disc-badge-music{background:rgba(255,60,60,.85);color:var(--rm-text)}',
'.rm-disc-badge-rec{background:rgba(255,180,50,.9);color:#000}',
'.rm-disc-empty{padding:20px;text-align:center;color:var(--rm-text-muted);font-size:13px}',
/* Recommendation section */
'.rm-disc-rec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}',
'.rm-disc-rec-item{display:flex;align-items:center;gap:12px;padding:10px 14px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.04);border-radius:12px;cursor:pointer;transition:all .2s}',
'.rm-disc-rec-item:hover{background:rgba(255,255,255,.1);transform:translateX(4px)}',
'.rm-disc-rec-art{width:52px;height:52px;border-radius:8px;overflow:hidden;flex-shrink:0;background:var(--rm-bg-surface)}',
'.rm-disc-rec-art img{width:100%;height:100%;object-fit:cover}',
'.rm-disc-rec-art i{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:18px;color:rgba(255,255,255,.3)}',
'.rm-disc-rec-info{flex:1;min-width:0}',
'.rm-disc-rec-title{font-size:13px;font-weight:600;color:var(--rm-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-disc-rec-meta{font-size:11px;color:rgba(255,255,255,.4);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-main{flex:1;display:flex;flex-direction:column;overflow:hidden;background:var(--rm-bg)}',
'.rm-toolbar{display:flex;align-items:center;gap:10px;padding:12px 20px;background:var(--rm-bg-elevated);border-bottom:1px solid rgba(255,255,255,.06);flex-wrap:wrap}',
'.rm-search{flex:1;min-width:180px;padding:10px 16px;border:none;border-radius:24px;background:rgba(255,255,255,.08);color:var(--rm-text);font-size:13px;outline:none;transition:background .2s}',
'.rm-search:focus{background:rgba(255,255,255,.14);box-shadow:0 0 0 2px rgba(var(--rm-accent-rgb),.3)}',
'.rm-search::placeholder{color:rgba(255,255,255,.4)}',
'.rm-select{padding:8px 12px;border:1px solid rgba(255,255,255,.1);border-radius:20px;background:rgba(255,255,255,.06);color:var(--rm-text);font-size:12px;outline:none;cursor:pointer;min-width:100px}',
'.rm-select:focus{border-color:var(--rm-accent)}',
'.rm-select option{background:var(--rm-bg-surface);color:var(--rm-text)}',
'.rm-content{flex:1;overflow-y:auto;padding:20px;scrollbar-width:thin;scrollbar-color:var(--rm-text-dim) transparent}',

/* ── station / podcast cards ─────────────────────────── */
'.rm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}',
'.rm-card{display:flex;align-items:center;gap:12px;padding:10px 12px;background:rgba(255,255,255,.04);border:none;border-radius:8px;cursor:pointer;transition:all .2s}',
'.rm-card:hover{background:rgba(255,255,255,.1);transform:translateY(-1px)}',
'.rm-card.rm-playing{background:rgba(var(--rm-accent-rgb),.12);box-shadow:inset 3px 0 0 var(--rm-accent)}',
'.rm-card.rm-buffering{opacity:.7}',
'.rm-card.rm-buffering .rm-card-icon::after{content:"";position:absolute;inset:0;border-radius:10px;border:2px solid transparent;border-top-color:var(--rm-accent);animation:rm-spin .8s linear infinite}',
'@keyframes rm-spin{to{transform:rotate(360deg)}}',
'.rm-card-icon{width:48px;height:48px;border-radius:8px;background:var(--rm-bg-surface);display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;position:relative}',
'.rm-card-icon img{width:100%;height:100%;object-fit:cover;border-radius:8px}',
'.rm-card-icon i{font-size:20px;color:rgba(255,255,255,.4)}',
'.rm-letter-icon{width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--rm-text);font-weight:700;font-size:20px;border-radius:8px}',
'.rm-card-info{flex:1;min-width:0}',
'.rm-card-name{font-weight:600;color:var(--rm-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}',
'.rm-card-meta{font-size:11px;color:rgba(255,255,255,.5);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-card-actions{display:flex;gap:4px;flex-shrink:0}',
'.rm-card-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:14px;border-radius:50%;transition:all .12s}',
'.rm-card-btn:hover{color:var(--rm-accent);background:rgba(var(--rm-accent-rgb),.1)}',
'.rm-card-btn.rm-fav-active{color:var(--rm-accent)}',
'.rm-card-codec{font-size:9px;padding:1px 5px;border-radius:4px;background:rgba(255,255,255,.06);color:rgba(255,255,255,.4);font-weight:600;letter-spacing:.5px}',

/* ── chips ─────────────────────────── */
'.rm-chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}',
'.rm-chip{padding:6px 14px;border-radius:20px;background:rgba(255,255,255,.06);border:none;color:rgba(255,255,255,.8);font-size:12px;cursor:pointer;transition:all .15s;white-space:nowrap}',
'.rm-chip:hover{background:rgba(255,255,255,.12)}',
'.rm-chip.active{background:var(--rm-accent);color:var(--rm-text)}',

/* ── Player bar (Spotify-style) ────────────────────── */
'.rm-player{display:flex;align-items:center;gap:14px;padding:10px 20px;background:var(--rm-bg-elevated);border-top:1px solid rgba(255,255,255,.06);min-height:68px}',
'.rm-player.rm-buffering .rm-player-art::after{content:"";position:absolute;inset:-2px;border-radius:8px;border:2px solid transparent;border-top-color:var(--rm-accent);animation:rm-spin .8s linear infinite}',
'.rm-player-art{width:50px;height:50px;border-radius:6px;background:var(--rm-bg-surface);display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.5);position:relative;cursor:pointer}',
'.rm-player-art img{width:100%;height:100%;object-fit:cover}',
'.rm-player-art .rm-letter-icon{font-size:18px}',
'.rm-player-art i{font-size:18px;color:rgba(255,255,255,.4)}',
'.rm-player-info{flex:1;min-width:0;cursor:pointer}',
'.rm-player-name{font-weight:600;font-size:13px;color:var(--rm-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-player-meta{font-size:11px;color:rgba(255,255,255,.5);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}',
'.rm-player-controls{display:flex;align-items:center;gap:4px}',
'.rm-player-btn{background:none;border:none;color:rgba(255,255,255,.8);font-size:16px;cursor:pointer;padding:8px;border-radius:50%;transition:all .12s;line-height:1}',
'.rm-player-btn:hover{color:var(--rm-text);transform:scale(1.08)}',
'.rm-player-btn.rm-mode-active{color:var(--rm-accent)}',
'.rm-player-btn.rm-btn-play{font-size:20px;width:40px;height:40px;display:flex;align-items:center;justify-content:center;background:var(--rm-accent);color:#000;border-radius:50%;box-shadow:0 2px 8px rgba(var(--rm-accent-rgb),.3);position:relative;overflow:visible}',
'.rm-player-btn.rm-btn-play:hover{background:var(--rm-accent-hover);transform:scale(1.06)}',
/* Loading state: Spotify-style progress ring around play button — signals NAS is loading */
'.rm-player-btn.rm-btn-play.rm-loading::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:2px solid transparent;border-top-color:var(--rm-accent);border-right-color:rgba(var(--rm-accent-rgb),.4);animation:rm-spin .7s linear infinite;pointer-events:none}',
'.rm-cast-btn{font-size:15px;transition:color .2s;display:none}',
'.rm-cast-btn.rm-casting{color:var(--rm-accent);animation:rm-cast-pulse 2s ease-in-out infinite}',
'@keyframes rm-cast-pulse{0%,100%{opacity:1}50%{opacity:.5}}',
'.rm-autoplay-prompt{display:flex;align-items:center;justify-content:center;gap:10px;padding:12px 20px;background:linear-gradient(135deg,var(--rm-accent),var(--rm-accent-active));color:#000;font-weight:700;font-size:14px;cursor:pointer;border:none;width:100%;border-top:none;animation:rm-autoplay-pulse 1.5s ease-in-out infinite}',
'@keyframes rm-autoplay-pulse{0%,100%{opacity:1}50%{opacity:.8}}',
'.rm-autoplay-prompt:hover{background:linear-gradient(135deg,var(--rm-accent-hover),var(--rm-accent))}',
'.rm-autoplay-prompt i{font-size:20px}',
'.rm-nas-spinup{position:absolute;top:0;left:0;right:0;bottom:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;background:rgba(15,23,42,.88);z-index:20;border-radius:8px;pointer-events:none}',
'.rm-nas-spinup-icon{font-size:32px;animation:rm-nas-spin 2s linear infinite}',
'.rm-nas-spinup-text{font-size:13px;color:rgba(255,255,255,.7);text-align:center;line-height:1.5;max-width:220px}',
'@keyframes rm-nas-spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}',

'.rm-vol-wrap{display:flex;align-items:center;gap:6px}',
'.rm-vol-wrap i{font-size:13px;color:rgba(255,255,255,.5)}',
'.rm-vol-slider{width:80px;accent-color:var(--rm-accent);height:4px}',
'.rm-player-eq{display:flex;align-items:flex-end;gap:2px;height:18px;margin-left:4px}',
'.rm-player-eq span{width:3px;background:var(--rm-accent);border-radius:1px;animation:rm-eq .6s ease-in-out infinite alternate}',
'.rm-player-eq span:nth-child(1){animation-delay:0s;height:6px}',
'.rm-player-eq span:nth-child(2){animation-delay:.15s;height:12px}',
'.rm-player-eq span:nth-child(3){animation-delay:.3s;height:8px}',
'.rm-player-eq span:nth-child(4){animation-delay:.45s;height:14px}',
'.rm-player-eq span:nth-child(5){animation-delay:.1s;height:10px}',
'@keyframes rm-eq{0%{height:4px}100%{height:18px}}',

/* podcast episode list */
'.rm-ep-list{display:flex;flex-direction:column;gap:8px}',
'.rm-ep-item{display:flex;align-items:center;gap:12px;padding:12px;background:rgba(255,255,255,.04);border:none;border-radius:8px;cursor:pointer;transition:background .15s}',
'.rm-ep-item:hover{background:rgba(255,255,255,.08)}',
'.rm-ep-item.rm-playing{background:rgba(var(--rm-accent-rgb),.1)}',
'.rm-ep-play{font-size:16px;color:var(--rm-accent);width:32px;text-align:center;flex-shrink:0}',
'.rm-ep-info{flex:1;min-width:0}',
'.rm-ep-title{font-weight:600;color:var(--rm-text);margin-bottom:2px}',
'.rm-ep-meta{font-size:11px;color:rgba(255,255,255,.5)}',

/* podcast detail header */
'.rm-pod-header{display:flex;gap:20px;margin-bottom:24px;align-items:flex-start}',
'.rm-pod-art{width:140px;height:140px;border-radius:8px;object-fit:cover;flex-shrink:0;box-shadow:0 4px 20px rgba(0,0,0,.5)}',
'.rm-pod-details{flex:1;min-width:0}',
'.rm-pod-title{font-size:20px;font-weight:700;color:var(--rm-text);margin-bottom:4px}',
'.rm-pod-author{font-size:13px;color:rgba(255,255,255,.5);margin-bottom:6px}',
'.rm-pod-desc{font-size:12px;color:var(--rm-text-secondary);line-height:1.5;max-height:60px;overflow:hidden}',
'.rm-pod-sub-btn{margin-top:8px;padding:6px 16px;border-radius:20px;border:1px solid var(--rm-accent);background:transparent;color:var(--rm-accent);font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}',
'.rm-pod-sub-btn:hover{background:var(--rm-accent);color:#000}',
'.rm-pod-sub-btn.subscribed{background:var(--rm-accent);color:#000}',

/* empty state */
'.rm-empty{text-align:center;padding:60px 20px;color:rgba(255,255,255,.4)}',
'.rm-empty i{font-size:48px;margin-bottom:12px;display:block;opacity:.3}',
'.rm-empty p{font-size:14px}',

/* ── music tracks ─────────────────────────── */
'.rm-track{display:flex;align-items:center;gap:12px;padding:8px 12px;background:rgba(255,255,255,.03);border:none;border-radius:6px;cursor:pointer;transition:all .15s}',
'.rm-track:hover{background:rgba(255,255,255,.08)}',
'.rm-track.rm-playing{background:rgba(var(--rm-accent-rgb),.1)}',
'.rm-track-thumb{width:48px;height:48px;border-radius:6px;object-fit:cover;flex-shrink:0;background:var(--rm-bg-surface)}',
'.rm-track-info{flex:1;min-width:0}',
'.rm-track-title{font-weight:600;color:var(--rm-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}',
'.rm-track-meta{font-size:11px;color:rgba(255,255,255,.4);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-track-dur{font-size:11px;color:rgba(255,255,255,.4);flex-shrink:0;font-variant-numeric:tabular-nums}',
'.rm-track-actions{display:flex;gap:2px;flex-shrink:0}',
'.rm-track-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:13px;border-radius:50%;transition:all .12s}',
'.rm-track-btn:hover{color:var(--rm-accent);background:rgba(var(--rm-accent-rgb),.1)}',

/* local music search */
'.rm-local-search-wrap{padding:8px 12px 0}',
'.rm-local-search-box{display:flex;align-items:center;gap:8px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:7px 12px;margin-bottom:12px}',
'.rm-local-search-box input{flex:1;background:none;border:none;outline:none;color:var(--rm-text);font-size:13px;min-width:0}',
'.rm-local-search-box input::placeholder{color:var(--rm-text-muted)}',

/* seekbar */
'.rm-seekbar{display:none;align-items:center;gap:8px;padding:0 20px;height:20px;flex-shrink:0;background:var(--rm-bg-elevated)}',
'.rm-seekbar.visible{display:flex}',
'.rm-seek-time{font-size:10px;color:rgba(255,255,255,.4);font-variant-numeric:tabular-nums;min-width:36px}',
'.rm-seek-time.right{text-align:right}',
'.rm-seek-track{flex:1;height:4px;background:rgba(255,255,255,.1);border-radius:2px;position:relative;cursor:pointer}',
'.rm-seek-fill{height:100%;background:var(--rm-accent);border-radius:2px;position:absolute;left:0;top:0;pointer-events:none;transition:width .1s}',
'.rm-seek-thumb{width:12px;height:12px;border-radius:50%;background:var(--rm-text);position:absolute;top:50%;transform:translate(-50%,-50%);cursor:pointer;opacity:0;transition:opacity .15s}',
'.rm-seekbar:hover .rm-seek-thumb{opacity:1}',

/* music queue panel */
'.rm-queue{margin-top:16px}',
'.rm-queue-title{font-size:12px;font-weight:600;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}',
'.rm-queue-list{display:flex;flex-direction:column;gap:2px}',
'.rm-queue-item{display:flex;align-items:center;gap:10px;padding:6px 10px;border-radius:6px;cursor:pointer;transition:background .12s;font-size:12px}',
'.rm-queue-item:hover{background:rgba(255,255,255,.06)}',
'.rm-queue-item.rm-playing{color:var(--rm-accent);font-weight:600}',
'.rm-queue-item-idx{width:20px;text-align:center;color:var(--rm-text-muted);flex-shrink:0}',
'.rm-queue-item-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:rgba(255,255,255,.8)}',
'.rm-queue-item.rm-playing .rm-queue-item-title{color:var(--rm-accent)}',
'.rm-queue-item-dur{color:var(--rm-text-muted);flex-shrink:0}',
'.rm-queue-item-rm{background:none;border:none;color:var(--rm-text-muted);cursor:pointer;padding:2px 4px;font-size:11px;transition:color .12s}',
'.rm-queue-item-rm:hover{color:var(--rm-error)}',

/* playlists */
'.rm-pl-header{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}',
'.rm-pl-header h3{margin:0;font-size:18px;color:var(--rm-text);flex:1;font-weight:700}',
'.rm-pl-create{display:flex;gap:8px;align-items:center}',
'.rm-pl-create input{padding:6px 12px;border:1px solid rgba(255,255,255,.1);border-radius:20px;background:rgba(255,255,255,.06);color:var(--rm-text);font-size:13px;outline:none}',
'.rm-pl-create button{padding:6px 14px;border-radius:20px;background:var(--rm-accent);color:#000;border:none;cursor:pointer;font-size:12px;font-weight:600;transition:filter .12s}',
'.rm-pl-create button:hover{filter:brightness(1.1)}',
'.rm-pl-card{display:flex;align-items:center;gap:12px;padding:12px;background:rgba(255,255,255,.04);border:none;border-radius:8px;cursor:pointer;transition:all .2s}',
'.rm-pl-card:hover{background:rgba(255,255,255,.08)}',
'.rm-pl-icon{width:48px;height:48px;border-radius:8px;background:linear-gradient(135deg,var(--rm-accent),var(--rm-accent-active));display:flex;align-items:center;justify-content:center;color:#000;font-size:18px;flex-shrink:0;overflow:hidden}',
'.rm-pl-info{flex:1;min-width:0}',
'.rm-pl-name{font-weight:600;color:var(--rm-text);font-size:14px}',
'.rm-pl-meta{font-size:11px;color:rgba(255,255,255,.4);margin-top:2px}',
'.rm-pl-actions{display:flex;gap:4px}',
'.rm-pl-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:13px;border-radius:50%;transition:all .12s}',
'.rm-pl-btn:hover{color:var(--rm-error);background:rgba(239,68,68,.1)}',

/* add-to-playlist modal */
'.rm-modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(4px)}',
'.rm-modal{background:var(--rm-bg-surface);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:20px;min-width:280px;max-width:400px;max-height:60vh;overflow-y:auto}',
'.rm-modal h4{margin:0 0 12px;font-size:15px;color:var(--rm-text)}',
'.rm-modal-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:6px;cursor:pointer;transition:background .12s;font-size:13px;color:rgba(255,255,255,.8)}',
'.rm-modal-item:hover{background:rgba(255,255,255,.08)}',
'.rm-modal-item i{color:var(--rm-accent);width:16px;text-align:center}',
'.rm-modal-close{margin-top:12px;padding:6px 16px;border-radius:20px;background:none;border:1px solid rgba(255,255,255,.15);color:rgba(255,255,255,.7);cursor:pointer;font-size:12px;width:100%;transition:all .12s}',
'.rm-modal-close:hover{border-color:var(--rm-text-muted);color:var(--rm-text)}',

/* install banner */
'.rm-install-banner{text-align:center;padding:40px 20px;max-width:400px;margin:0 auto}',
'.rm-install-banner i{font-size:48px;color:var(--rm-accent);margin-bottom:12px;display:block}',
'.rm-install-banner p{font-size:14px;color:rgba(255,255,255,.5);margin-bottom:16px}',
'.rm-install-btn{padding:10px 24px;border-radius:24px;background:var(--rm-accent);color:#000;border:none;cursor:pointer;font-size:14px;font-weight:700;transition:all .15s}',
'.rm-install-btn:hover{background:var(--rm-accent-hover);transform:scale(1.02)}',
'.rm-install-btn:disabled{opacity:.5;cursor:wait}',

/* download button */
'.rm-dl-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:14px;border-radius:50%;transition:all .12s;flex-shrink:0}',
'.rm-dl-btn:hover{color:var(--rm-accent);background:rgba(var(--rm-accent-rgb),.1)}',
'.rm-dl-btn.rm-downloading{color:var(--rm-accent);animation:rm-pulse 1.2s infinite}',
'.rm-dl-btn.rm-downloaded{color:var(--rm-accent)}',
'@keyframes rm-pulse{0%,100%{opacity:1}50%{opacity:.4}}',
'.rm-dl-toast{position:fixed;bottom:80px;right:16px;background:var(--rm-bg-surface);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:10px 16px;font-size:12px;color:var(--rm-text);box-shadow:0 4px 20px rgba(0,0,0,.5);z-index:9998;display:flex;align-items:center;gap:8px;max-width:300px}',
'.rm-dl-toast i{color:var(--rm-accent);font-size:14px}',

/* ── Offline Archive button (rm-arch-btn) ──────────────────── */
/* Wrapper: relative + fixed size so SVG ring is always aligned */
'.rm-arch-btn{position:relative;background:none;border:none;cursor:pointer;padding:4px;border-radius:50%;transition:background .12s;flex-shrink:0;width:30px;height:30px;display:flex;align-items:center;justify-content:center}',
'.rm-arch-btn:hover{background:rgba(255,255,255,.08)}',
/* SVG progress ring — hidden by default */
'.rm-arch-ring{position:absolute;inset:0;width:100%;height:100%;transform:rotate(-90deg);pointer-events:none;opacity:0;transition:opacity .2s}',
'.rm-arch-btn.rm-arch-loading .rm-arch-ring{opacity:1}',
'.rm-arch-ring circle{stroke-dasharray:87.96;stroke-dashoffset:87.96;transition:stroke-dashoffset .4s ease}',
/* Icon colour per state */
'.rm-arch-icon{font-size:13px;color:rgba(255,255,255,.4);transition:color .15s,transform .15s}',
'.rm-arch-btn:hover .rm-arch-icon{color:rgba(255,255,255,.7)}',
/* State: loading */
'.rm-arch-btn.rm-arch-loading .rm-arch-icon{color:var(--rm-accent);animation:rm-pulse 1.2s infinite}',
/* State: archived on NAS */
'.rm-arch-btn.rm-arch-nas .rm-arch-icon{color:var(--rm-accent)}',
/* State: archived on NAS + cached on phone (brightest) */
'.rm-arch-btn.rm-arch-phone .rm-arch-icon{color:var(--rm-accent);filter:drop-shadow(0 0 4px rgba(var(--rm-accent-rgb),.7))}',
/* Error state */
'.rm-arch-btn.rm-arch-error .rm-arch-icon{color:var(--rm-error)}',

/* local music folder chips */
'.rm-folder-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}',
'.rm-folder-chip{display:inline-flex;align-items:center;gap:4px;padding:5px 10px;border-radius:16px;background:rgba(255,255,255,.06);border:none;font-size:11px;color:rgba(255,255,255,.8);cursor:default}',
'.rm-folder-chip .rm-chip-remove{cursor:pointer;opacity:.5;margin-left:2px;font-size:10px}',
'.rm-folder-chip .rm-chip-remove:hover{opacity:1;color:var(--rm-error)}',
'.rm-add-folder-btn{padding:5px 12px;border-radius:16px;background:none;border:1px dashed rgba(255,255,255,.15);color:rgba(255,255,255,.4);font-size:11px;cursor:pointer;display:inline-flex;align-items:center;gap:4px}',
'.rm-add-folder-btn:hover{border-color:var(--rm-accent);color:var(--rm-accent)}',

/* ── Now Playing overlay ───────────────────────── */
'.rm-np-overlay{position:absolute;inset:0;z-index:100;display:flex;flex-direction:column;overflow:hidden;background:var(--rm-bg);padding-bottom:max(0px,env(safe-area-inset-bottom))}',
'.rm-np-minimized{transform:translateY(100%);pointer-events:none;opacity:0}',
/* Background: smooth colour transition between tracks */
'.rm-np-bg{position:absolute;inset:-40px;background-size:cover;background-position:center;filter:blur(40px) brightness(.25) saturate(1.4);z-index:0;transition:opacity 300ms ease}',
'.rm-np-inner{position:relative;z-index:1;flex:1;display:flex;flex-direction:column;align-items:center;padding:max(56px,calc(env(safe-area-inset-top,0px) + 48px)) 24px 24px;gap:16px;overflow-y:auto;-webkit-overflow-scrolling:touch}',
'.rm-np-inner::before{content:"";flex:1 1 0;min-height:0;pointer-events:none}',
'.rm-np-inner::after{content:"";flex:2 1 0;min-height:0;pointer-events:none}',
'.rm-np-close{position:absolute;top:max(50px,calc(env(safe-area-inset-top,0px) + 12px));left:12px;background:rgba(255,255,255,.08);border:none;color:var(--rm-text);font-size:18px;cursor:pointer;padding:8px 12px;border-radius:50%;z-index:2;backdrop-filter:blur(8px);transition:background .15s}',
'.rm-np-close:hover{background:rgba(255,255,255,.15)}',
/* Fixed aspect-ratio art box — never causes layout shift */
'.rm-np-art{width:min(260px,55vw);height:min(260px,55vw);aspect-ratio:1/1;border-radius:8px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,.6);flex-shrink:0;background:var(--rm-bg-surface);display:flex;align-items:center;justify-content:center;position:relative}',
'@media(max-height:600px){.rm-np-art{width:min(180px,45vw);height:min(180px,45vw)}}',
/* Art image cross-fade via opacity */
'.rm-np-art img{width:100%;height:100%;object-fit:cover;transition:opacity 200ms ease;position:absolute;inset:0}',
'.rm-np-art img.rm-art-loading{opacity:0}',
'.rm-np-art img.rm-art-loaded{opacity:1}',
'.rm-np-art .rm-letter-icon{font-size:64px;width:100%;height:100%}',
'.rm-np-art i{font-size:64px;color:var(--rm-text-muted)}',
/* Skeleton shimmer — shown while artwork fetches from NAS */
'.rm-np-art.rm-skeleton::before{content:"";position:absolute;inset:0;background:linear-gradient(90deg,var(--rm-bg-surface) 25%,var(--rm-border) 50%,var(--rm-bg-surface) 75%);background-size:200% 100%;animation:rm-skeleton-sweep 1.2s infinite}',
'@keyframes rm-skeleton-sweep{0%{background-position:200% 0}100%{background-position:-200% 0}}',
/* ── Loading Skeletons ─────────────────────────────── */
'.rm-skel-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;padding:8px 0}',
'.rm-skel-card{height:64px;border-radius:10px;background:var(--rm-bg-surface);position:relative;overflow:hidden}',
'.rm-skel-card::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent 25%,var(--rm-border) 50%,transparent 75%);background-size:200% 100%;animation:rm-skeleton-sweep 1.2s infinite}',
'.rm-skel-track{height:52px;border-radius:8px;background:var(--rm-bg-surface);position:relative;overflow:hidden;margin-bottom:4px}',
'.rm-skel-track::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent 25%,var(--rm-border) 50%,transparent 75%);background-size:200% 100%;animation:rm-skeleton-sweep 1.2s infinite}',
'.rm-np-info{text-align:center;max-width:320px;width:100%}',
/* Title + meta fade on track swap */
'.rm-np-title{font-size:22px;font-weight:700;color:var(--rm-text);margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:opacity 150ms ease}',
'.rm-np-meta{font-size:13px;color:rgba(255,255,255,.5);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:opacity 150ms ease}',
'.rm-np-title.rm-fading,.rm-np-meta.rm-fading{opacity:0}',
'.rm-np-seek{display:flex;align-items:center;gap:10px;width:100%;max-width:320px}',
'.rm-np-seek .rm-seek-time{color:rgba(255,255,255,.4);font-size:11px;min-width:38px}',
'.rm-np-seek .rm-seek-track{flex:1;height:4px;background:rgba(255,255,255,.12);border-radius:2px;position:relative;cursor:pointer}',
'.rm-np-seek .rm-seek-fill{height:100%;background:var(--rm-accent);border-radius:2px;position:absolute;left:0;top:0}',
'.rm-np-seek .rm-seek-thumb{width:14px;height:14px;border-radius:50%;background:var(--rm-text);position:absolute;top:50%;transform:translate(-50%,-50%);cursor:pointer}',
'.rm-np-controls{display:flex;align-items:center;gap:20px;touch-action:manipulation}',
'.rm-np-btn{background:none;border:none;color:rgba(255,255,255,.7);font-size:22px;cursor:pointer;padding:10px;border-radius:50%;transition:all .12s;touch-action:manipulation;-webkit-tap-highlight-color:transparent}',
'.rm-np-btn.rm-mode-active{color:var(--rm-accent)}',
'.rm-np-btn:hover{color:var(--rm-text);transform:scale(1.1)}',
'.rm-np-btn.rm-np-play{width:64px;height:64px;font-size:26px;background:var(--rm-accent);color:#000;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 20px rgba(var(--rm-accent-rgb),.3);position:relative;overflow:visible}',
'.rm-np-btn.rm-np-play:hover{background:var(--rm-accent-hover);transform:scale(1.06)}',
'.rm-np-btn.rm-np-play.rm-loading::after{content:"";position:absolute;inset:-5px;border-radius:50%;border:3px solid transparent;border-top-color:var(--rm-accent);border-right-color:rgba(var(--rm-accent-rgb),.4);animation:rm-spin .7s linear infinite;pointer-events:none}',
'.rm-btn-disabled{opacity:.35!important;pointer-events:none!important;cursor:default!important}',
'.rm-np-actions{display:flex;gap:10px;margin-top:4px;flex-wrap:wrap;justify-content:center;touch-action:manipulation}',
'.rm-np-action{background:rgba(255,255,255,.06);border:none;color:rgba(255,255,255,.5);font-size:13px;cursor:pointer;padding:8px 16px;border-radius:20px;transition:all .12s;display:flex;align-items:center;gap:6px;touch-action:manipulation;-webkit-tap-highlight-color:transparent}',
'.rm-np-action:hover{background:rgba(255,255,255,.12);color:var(--rm-text)}',
'.rm-np-action.rm-lyrics-active{background:rgba(var(--rm-accent-rgb),.15);color:var(--rm-accent)}',
'.rm-np-count{font-size:11px;color:var(--rm-text-muted);margin-top:2px}',
'.rm-np-vis{display:block;width:100%;max-width:260px;height:48px;border-radius:6px;opacity:.85}',

/* ── Lyrics panel ── */
'.rm-lyrics-panel{display:none;width:100%;max-height:40vh;overflow-y:auto;padding:16px 8px;text-align:center;font-size:15px;line-height:1.8;color:rgba(255,255,255,.75);white-space:pre-line;-webkit-overflow-scrolling:touch;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.15) transparent}',
'.rm-lyrics-panel.rm-lyrics-visible{display:block}',
'.rm-lyrics-panel .rm-lyrics-loading{color:var(--rm-text-muted);font-style:italic}',
'.rm-lyrics-panel .rm-lyrics-empty{color:var(--rm-text-muted);font-style:italic}',
/* now-playing queue panel */
'.rm-np-queue{display:none;width:100%;max-height:40vh;overflow-y:auto;padding:8px 0;-webkit-overflow-scrolling:touch;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.15) transparent}',
'.rm-np-queue.rm-np-queue-visible{display:block}',
'.rm-np-queue-header{display:flex;align-items:center;justify-content:space-between;padding:0 8px 8px;font-size:13px;color:rgba(255,255,255,.5);font-weight:600}',
'.rm-np-q-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer;transition:background .12s;min-height:44px}',
'.rm-np-q-item:active{background:rgba(255,255,255,.08)}',
'.rm-np-q-item.rm-q-current{background:rgba(var(--rm-accent-rgb),.12)}',
'.rm-np-q-item-idx{width:22px;text-align:center;color:var(--rm-text-muted);font-size:12px;flex-shrink:0}',
'.rm-np-q-item.rm-q-current .rm-np-q-item-idx{color:var(--rm-accent)}',
'.rm-np-q-item-art{width:36px;height:36px;border-radius:4px;object-fit:cover;flex-shrink:0;background:var(--rm-bg-surface)}',
'.rm-np-q-item-info{flex:1;min-width:0;overflow:hidden}',
'.rm-np-q-item-title{font-size:13px;color:rgba(255,255,255,.85);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.rm-np-q-item.rm-q-current .rm-np-q-item-title{color:var(--rm-accent);font-weight:600}',
'.rm-np-q-item-meta{font-size:11px;color:var(--rm-text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',

/* ── Similar artists panel (NP) ── */
'.rm-np-similar{width:100%;padding:12px 0;-webkit-overflow-scrolling:touch}',
'.rm-np-similar-header{display:flex;align-items:center;gap:8px;padding:0 8px 10px;font-size:13px;color:rgba(255,255,255,.5);font-weight:600}',
'.rm-np-similar-scroll{display:flex;gap:12px;overflow-x:auto;padding:0 8px 8px;-webkit-overflow-scrolling:touch;scrollbar-width:none}',
'.rm-np-similar-scroll::-webkit-scrollbar{display:none}',
'.rm-np-similar-card{flex:0 0 110px;display:flex;flex-direction:column;align-items:center;gap:6px;cursor:pointer;padding:8px 4px;border-radius:12px;transition:background .12s}',
'.rm-np-similar-card:active{background:rgba(255,255,255,.08)}',
'.rm-np-similar-card img{width:80px;height:80px;border-radius:50%;object-fit:cover;background:rgba(255,255,255,.06);border:2px solid rgba(255,255,255,.08)}',
'.rm-np-similar-card .rm-nps-name{font-size:12px;color:rgba(255,255,255,.8);text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100px}',
'.rm-np-similar-card .rm-nps-fans{font-size:10px;color:var(--rm-text-muted)}',
'.rm-np-similar-loading{text-align:center;padding:16px;color:var(--rm-text-muted);font-size:13px}',

/* ── Lock screen ── */
'.rm-lock-overlay{position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;background:#000;display:flex;flex-direction:column;align-items:center;justify-content:center;touch-action:none;user-select:none;-webkit-user-select:none}',
'.rm-lock-art{width:180px;height:180px;border-radius:16px;overflow:hidden;background:rgba(255,255,255,.06);display:flex;align-items:center;justify-content:center;font-size:64px;color:rgba(255,255,255,.15);margin-bottom:24px}',
'.rm-lock-art img{width:100%;height:100%;object-fit:cover}',
'.rm-lock-title{font-size:18px;font-weight:700;color:var(--rm-text);text-align:center;max-width:80vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.rm-lock-meta{font-size:13px;color:rgba(255,255,255,.4);margin-top:4px;text-align:center}',
'.rm-lock-icon{font-size:40px;color:rgba(255,255,255,.1);margin-bottom:40px}',
'.rm-lock-hint{position:absolute;bottom:60px;left:0;right:0;text-align:center;color:rgba(255,255,255,.2);font-size:13px;animation:rm-lock-pulse 2s ease-in-out infinite}',
'.rm-lock-hint i{margin-right:6px;font-size:16px}',
'@keyframes rm-lock-pulse{0%,100%{opacity:.2}50%{opacity:.5}}',

/* ── Horizontal scroll section (Most Played, etc.) ── */
'.rm-section-title{font-size:18px;font-weight:700;color:var(--rm-text);margin:20px 0 12px;display:flex;align-items:center;gap:10px}',
'.rm-section-title i{color:var(--rm-accent);font-size:16px}',
'.rm-hscroll{display:flex;overflow-x:auto;gap:14px;padding-bottom:8px;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;scrollbar-width:none}',
'.rm-hscroll::-webkit-scrollbar{display:none}',
'.rm-hcard{scroll-snap-align:start;min-width:150px;max-width:170px;flex-shrink:0;cursor:pointer;transition:all .15s;padding:10px;background:rgba(255,255,255,.04);border-radius:8px}',
'.rm-hcard:hover{background:rgba(255,255,255,.08);transform:translateY(-3px)}',
'.rm-hcard-art{width:130px;height:130px;border-radius:6px;overflow:hidden;background:var(--rm-bg-surface);margin-bottom:8px;position:relative;box-shadow:0 4px 16px rgba(0,0,0,.3)}',
'.rm-hcard-art img{width:100%;height:100%;object-fit:cover}',
'.rm-hcard-art .rm-letter-icon{width:100%;height:100%;font-size:40px}',
'.rm-hcard-art i{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:32px;color:var(--rm-text-muted)}',
'.rm-hcard-badge{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.7);color:var(--rm-accent);font-size:10px;padding:2px 6px;border-radius:10px;backdrop-filter:blur(4px);font-weight:600}',
'.rm-hcard-title{font-size:13px;font-weight:600;color:var(--rm-text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.rm-hcard-meta{font-size:11px;color:rgba(255,255,255,.4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}',

/* mobile bottom tab bar */
'.rm-mobile-nav{display:none;background:#000;border-top:1px solid var(--rm-border);padding:0 0 44px;gap:0;position:relative;z-index:45}',
'.rm-mobile-nav .rm-mnav-btn{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;flex:1;padding:10px 4px 8px;border:none;border-radius:0;background:none;color:rgba(255,255,255,.45);font-size:11px;white-space:nowrap;cursor:pointer;transition:color .15s;min-width:0;line-height:1}',
'.rm-mobile-nav .rm-mnav-btn.active{color:var(--rm-accent);font-weight:600}',
'.rm-mobile-nav .rm-mnav-btn i{font-size:24px;display:block;margin-bottom:2px}',
'.rm-more-sheet{display:none;position:absolute;bottom:100%;left:0;right:0;background:var(--rm-bg-elevated);border-top:1px solid rgba(255,255,255,.1);border-radius:16px 16px 0 0;padding:16px 12px 12px;z-index:50;box-shadow:0 -8px 30px rgba(0,0,0,.6)}',
'.rm-more-sheet.open{display:block}',
'.rm-more-sheet-handle{width:36px;height:4px;background:rgba(255,255,255,.2);border-radius:2px;margin:0 auto 14px}',
'.rm-more-sheet-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}',
'.rm-more-btn{display:flex;flex-direction:column;align-items:center;gap:6px;padding:16px 4px;border:none;border-radius:12px;background:rgba(255,255,255,.05);color:rgba(255,255,255,.7);font-size:11px;cursor:pointer;transition:all .15s;line-height:1.2}',
'.rm-more-btn:active{background:rgba(var(--rm-accent-rgb),.15);color:var(--rm-accent);transform:scale(.95)}',
'.rm-more-btn i{font-size:22px;color:rgba(255,255,255,.5);transition:color .15s}',
'.rm-more-btn:active i{color:var(--rm-accent)}',

/* per-track action bottom sheet */
'.rm-tsheet-overlay{position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.5);backdrop-filter:blur(4px)}',
'.rm-tsheet{position:fixed;bottom:0;left:0;right:0;z-index:9999;background:var(--rm-bg-elevated);border-radius:20px 20px 0 0;padding:12px 0 env(safe-area-inset-bottom,0);box-shadow:0 -8px 40px rgba(0,0,0,.7);transform:translateY(100%);transition:transform .25s cubic-bezier(.25,1,.5,1)}',
'.rm-tsheet.open{transform:translateY(0)}',
'.rm-tsheet-handle{width:40px;height:4px;background:rgba(255,255,255,.2);border-radius:2px;margin:0 auto 8px}',
'.rm-tsheet-title{padding:4px 16px 12px;font-size:13px;font-weight:600;color:var(--rm-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid rgba(255,255,255,.07);margin-bottom:4px}',
'.rm-tsheet-row{display:flex;align-items:center;gap:14px;padding:14px 20px;cursor:pointer;transition:background .12s}',
'.rm-tsheet-row:hover{background:rgba(255,255,255,.06)}',
'.rm-tsheet-row:active{background:rgba(var(--rm-accent-rgb),.12)}',
'.rm-tsheet-row i{width:20px;text-align:center;font-size:16px;color:rgba(255,255,255,.55)}',
'.rm-tsheet-row span{font-size:14px;color:rgba(255,255,255,.85)}',
'.rm-tsheet-row.danger i,.rm-tsheet-row.danger span{color:var(--rm-error)}',
'.rm-tsheet-row.disabled{opacity:.4;pointer-events:none}',
'.rm-tsheet-progress{font-size:11px;color:var(--rm-accent);margin-left:auto;font-weight:600}',

/* "..." button on tracks */
'.rm-track-more{background:none;border:none;color:var(--rm-text-muted);font-size:16px;padding:8px;cursor:pointer;flex-shrink:0;border-radius:8px;transition:color .12s;line-height:1}',
'.rm-track-more:hover{color:rgba(255,255,255,.7);background:rgba(255,255,255,.06)}',
'.rm-track-more.rm-downloading{color:var(--rm-accent);animation:rm-pulse 1s infinite}',

/* playlist edit mode */
'.rm-pl-edit-mode .rm-track{cursor:grab}',
'.rm-pl-edit-mode .rm-track:active{cursor:grabbing}',
'.rm-track.rm-dnd-over{box-shadow:inset 0 2px 0 var(--rm-accent)}',
'.rm-track.rm-dnd-dragging{opacity:.4}',
'.rm-pl-edit-btn{background:none;border:1px solid rgba(255,255,255,.2);color:var(--rm-text-secondary);font-size:12px;padding:6px 14px;border-radius:20px;cursor:pointer;transition:all .15s}',
'.rm-pl-edit-btn.active{background:var(--rm-accent);color:#000;border-color:var(--rm-accent)}',

/* sleep timer dropdown */
'.rm-sleep-dropdown{position:absolute;bottom:100%;left:50%;transform:translateX(-50%);background:var(--rm-bg-surface);border-radius:12px;padding:6px 0;min-width:180px;box-shadow:0 8px 32px rgba(0,0,0,.6);z-index:10;display:none}',
'.rm-sleep-dropdown.open{display:block}',
'.rm-sleep-option{padding:10px 16px;font-size:13px;color:rgba(255,255,255,.8);cursor:pointer;transition:background .1s;display:flex;align-items:center;justify-content:space-between}',
'.rm-sleep-option:hover{background:rgba(255,255,255,.08)}',
'.rm-sleep-option.active{color:var(--rm-accent);font-weight:600}',
'.rm-sleep-option .rm-sleep-check{font-size:11px}',
'.rm-np-action.rm-sleep-active{background:rgba(var(--rm-accent-rgb),.15);color:var(--rm-accent)}',
'.rm-sleep-remaining{font-size:10px;color:var(--rm-accent);margin-left:4px}',

/* playback speed button */
'.rm-speed-btn{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.15);color:var(--rm-text-secondary);font-size:12px;font-weight:700;cursor:pointer;padding:4px 10px;border-radius:12px;transition:all .12s;min-width:38px;text-align:center}',
'.rm-speed-btn:hover{background:rgba(255,255,255,.12);color:var(--rm-text)}',
'.rm-speed-btn.rm-speed-changed{color:var(--rm-accent);border-color:rgba(var(--rm-accent-rgb),.4)}',

/* section transition */
'.rm-content{transition:opacity .15s ease}',
'.rm-content.rm-fade-out{opacity:0}',

/* synced lyrics */
'.rm-lyrics-line{padding:4px 0;transition:all .25s ease;opacity:.35;transform:scale(.95)}',
'.rm-lyrics-line.rm-lyr-active{opacity:1;color:var(--rm-text);font-weight:600;font-size:17px;transform:scale(1)}',
'.rm-lyrics-line.rm-lyr-near{opacity:.6}',

/* queue drag & drop */
'.rm-np-q-item-drag{width:20px;text-align:center;color:rgba(255,255,255,.2);font-size:14px;cursor:grab;flex-shrink:0;touch-action:none}',
'.rm-np-q-item-drag:active{cursor:grabbing}',
'.rm-np-q-item.rm-q-dragging{opacity:.4;background:rgba(var(--rm-accent-rgb),.08)}',
'.rm-np-q-item.rm-q-drag-over{box-shadow:inset 0 -2px 0 var(--rm-accent)}',

/* podcast episode progress */
'.rm-ep-progress{width:100%;height:3px;background:rgba(255,255,255,.08);border-radius:2px;margin-top:4px;overflow:hidden}',
'.rm-ep-progress-bar{height:100%;background:var(--rm-accent);border-radius:2px;transition:width .3s}',
'.rm-ep-done{color:var(--rm-accent);font-size:11px;margin-left:auto;flex-shrink:0}',

/* crossfade setting */
'.rm-crossfade-wrap{display:flex;align-items:center;gap:12px;padding:12px 0}',
'.rm-crossfade-slider{flex:1;height:4px;-webkit-appearance:none;appearance:none;background:rgba(255,255,255,.15);border-radius:2px;outline:none}',
'.rm-crossfade-slider::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--rm-accent);cursor:pointer}',
'.rm-crossfade-val{color:rgba(255,255,255,.5);font-size:12px;min-width:28px;text-align:right}',

/* exit fullscreen toggle (mobile only) */
'.rm-exit-fs{display:none;position:absolute;top:8px;right:8px;z-index:200;width:40px;height:40px;border-radius:50%;background:rgba(0,0,0,.55);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.12);color:rgba(255,255,255,.7);font-size:15px;cursor:pointer;align-items:center;justify-content:center;transition:all .2s}',
'.rm-exit-fs:active{transform:scale(.9);background:rgba(0,0,0,.8)}',

/* responsive — mobile-first touch-friendly overrides */
'@media(max-width:768px){.rm-wrap{height:100%;max-height:100%}.rm-sidebar{display:none}.rm-mobile-nav{display:flex;order:10}.rm-exit-fs{display:flex}.rm-main{min-height:0}.rm-toolbar{padding:10px 12px;flex-shrink:0}.rm-content{padding:12px;min-height:0;flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch}.rm-grid{grid-template-columns:1fr}.rm-search{padding:12px 16px;font-size:14px;border-radius:12px}.rm-select{padding:10px 14px;font-size:13px;min-height:44px}.rm-card{padding:12px;gap:12px;min-height:60px}.rm-card-btn{padding:10px;font-size:16px;min-width:44px;min-height:44px;display:flex;align-items:center;justify-content:center}.rm-chip{padding:10px 16px;font-size:13px;min-height:40px;display:inline-flex;align-items:center}.rm-track{padding:10px 12px;min-height:56px}.rm-track-thumb{width:44px;height:44px}.rm-track-btn{padding:10px;min-width:44px;min-height:44px;font-size:15px;display:flex;align-items:center;justify-content:center}.rm-dl-btn{padding:10px;min-width:44px;min-height:44px;display:flex;align-items:center;justify-content:center}.rm-player{padding:8px 12px;gap:10px;min-height:56px;flex-shrink:0;cursor:pointer}.rm-vol-wrap{display:none}.rm-player-art{width:44px;height:44px;min-width:44px}.rm-player-info{min-width:0;flex:1}.rm-player-name{font-size:13px}.rm-player-meta{font-size:11px}.rm-player-controls{gap:2px;flex-shrink:0}#rm-shuffle-btn{display:none}#rm-repeat-btn{display:none}.rm-player-btn{padding:8px;font-size:16px;min-width:40px;min-height:40px;display:flex;align-items:center;justify-content:center}.rm-player-btn.rm-btn-play{width:40px;height:40px;font-size:18px;min-width:40px}.rm-player-eq{display:none}.rm-seekbar{padding:0 12px;height:20px}.rm-seek-time{font-size:10px;min-width:32px}.rm-seek-track{height:6px}.rm-seek-thumb{width:16px;height:16px;opacity:1}.rm-pod-header{flex-direction:column;align-items:center;text-align:center;gap:16px}.rm-pod-art{width:120px;height:120px}.rm-pod-sub-btn{padding:10px 24px;font-size:14px;min-height:44px}.rm-ep-item{padding:14px 12px;min-height:56px}.rm-ep-play{font-size:20px;width:44px}.rm-hcard{min-width:140px;padding:10px}.rm-hcard-art{width:120px;height:120px}.rm-section-title{font-size:16px;margin:16px 0 10px}.rm-np-art{width:min(260px,65vw);height:min(260px,65vw)}.rm-np-title{font-size:20px}.rm-np-meta{font-size:14px}.rm-np-btn{font-size:24px;padding:12px;min-width:48px;min-height:48px;display:flex;align-items:center;justify-content:center}.rm-np-btn.rm-np-play{width:72px;height:72px;font-size:28px}.rm-np-close{padding:12px 16px;min-width:48px;min-height:48px}.rm-np-action{padding:10px 18px;font-size:13px;min-height:44px}.rm-np-seek .rm-seek-track{height:6px}.rm-np-seek .rm-seek-thumb{width:16px;height:16px;opacity:1}.rm-pl-card{padding:12px;min-height:56px}.rm-pl-btn{padding:10px;min-width:44px;min-height:44px;display:flex;align-items:center;justify-content:center}.rm-pl-create input{padding:10px 14px;font-size:14px;min-height:44px}.rm-pl-create button{padding:10px 18px;font-size:13px;min-height:44px}.rm-queue-item{padding:10px 12px;min-height:48px;font-size:13px}.rm-queue-item-rm{padding:10px;min-width:44px;min-height:44px;font-size:14px;display:flex;align-items:center;justify-content:center}.rm-folder-chip{padding:8px 12px;font-size:12px;min-height:36px}.rm-add-folder-btn{padding:8px 14px;font-size:12px;min-height:36px}.rm-modal{min-width:min(320px,90vw);padding:20px}.rm-modal-item{padding:12px;min-height:48px;font-size:14px}.rm-modal-close{padding:12px;font-size:14px;min-height:48px}.rm-install-btn{padding:14px 28px;font-size:15px;min-height:48px}}',
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
  <div class="rm-sidebar" id="rm-sidebar"></div>
  <div class="rm-main">
    <div class="rm-mobile-nav" id="rm-mobile-nav">
      <button class="rm-mnav-btn active" data-section="most-played"><i class="fas fa-fire"></i><span>${t('Home')}</span></button>
      <button class="rm-mnav-btn" data-section="music"><i class="fab fa-youtube"></i><span>${t('Muzyka')}</span></button>
      <button class="rm-mnav-btn" data-section="radio"><i class="fas fa-broadcast-tower"></i><span>${t('Radio')}</span></button>
      <button class="rm-mnav-btn" data-section="podcasts"><i class="fas fa-podcast"></i><span>${t('Podcasty')}</span></button>
      <button class="rm-mnav-btn" data-section="more"><i class="fas fa-ellipsis-h"></i><span>${t('Więcej')}</span></button>
      <div class="rm-more-sheet" id="rm-more-sheet">
        <div class="rm-more-sheet-handle"></div>
        <div class="rm-more-sheet-grid">
          <button class="rm-more-btn" data-section="local"><i class="fas fa-folder-open"></i><span>${t('Lokalne')}</span></button>
          <button class="rm-more-btn" data-section="local-audiobooks"><i class="fas fa-book-reader"></i><span>${t('Audiobooki lok.')}</span></button>
          <button class="rm-more-btn" data-section="playlists"><i class="fas fa-list"></i><span>${t('Playlisty')}</span></button>
          <button class="rm-more-btn" data-section="queue"><i class="fas fa-list-ol"></i><span>${t('Kolejka')}</span></button>
          <button class="rm-more-btn" data-section="history"><i class="fas fa-history"></i><span>${t('Historia')}</span></button>
          <button class="rm-more-btn" data-section="audiobooks"><i class="fas fa-book-open"></i><span>${t('Audiobooki')}</span></button>
          <button class="rm-more-btn" id="rm-add-homescreen"><i class="fas fa-plus-square"></i><span>${t('Skrót')}</span></button>
        </div>
      </div>
    </div>
    <div class="rm-toolbar" id="rm-toolbar"></div>
    <div class="rm-content" id="rm-content"></div>
    <div class="rm-seekbar" id="rm-seekbar">
      <span class="rm-seek-time" id="rm-seek-cur">0:00</span>
      <div class="rm-seek-track" id="rm-seek-track">
        <div class="rm-seek-fill" id="rm-seek-fill"></div>
        <div class="rm-seek-thumb" id="rm-seek-thumb"></div>
      </div>
      <span class="rm-seek-time right" id="rm-seek-dur">0:00</span>
    </div>
    <div class="rm-player" id="rm-player" style="display:none">
      <div class="rm-player-art" id="rm-player-art"><i class="fas fa-music"></i></div>
      <div class="rm-player-info">
        <div class="rm-player-name" id="rm-player-name"></div>
        <div class="rm-player-meta" id="rm-player-meta"></div>
      </div>
      <div class="rm-player-eq" id="rm-player-eq" style="display:none"><span></span><span></span><span></span><span></span><span></span></div>
      <div class="rm-player-controls">
        <button class="rm-player-btn" id="rm-shuffle-btn" title="${t('Losowo')}"><i class="fas fa-random"></i></button>
        <button class="rm-player-btn" id="rm-prev-btn" title="${t('Poprzednia')}"><i class="fas fa-step-backward"></i></button>
        <button class="rm-player-btn rm-btn-play" id="rm-play-pause"><i class="fas fa-play"></i></button>
        <button class="rm-player-btn" id="rm-next-btn" title="${t('Następna')}"><i class="fas fa-step-forward"></i></button>
        <button class="rm-player-btn" id="rm-repeat-btn" title="${t('Powtarzaj')}"><i class="fas fa-redo"></i></button>
        <button class="rm-player-btn rm-cast-btn" id="rm-cast-btn" title="Chromecast"><i class="fab fa-chromecast"></i></button>
      </div>
      <div class="rm-vol-wrap">
        <i class="fas fa-volume-up"></i>
        <input type="range" class="rm-vol-slider" id="rm-vol" min="0" max="100" value="80">
      </div>
    </div>
  </div>
</div>
<button class="rm-exit-fs" id="rm-exit-fs" title="${t('Tryb okienkowy')}"><i class="fas fa-compress"></i></button>`;

            // Fullscreen mode on mobile — hides window chrome & taskbar
            const _isMobile = window.matchMedia('(max-width: 768px)').matches;
            let _isFullscreen = false;
            function _enterFullscreen() {
                document.body.classList.add('app-fullscreen-active');
                _isFullscreen = true;
                const btn = body.querySelector('#rm-exit-fs');
                if (btn) { btn.innerHTML = '<i class="fas fa-compress"></i>'; btn.title = t('Tryb okienkowy'); }
            }
            function _exitFullscreen() {
                document.body.classList.remove('app-fullscreen-active');
                _isFullscreen = false;
                const btn = body.querySelector('#rm-exit-fs');
                if (btn) { btn.innerHTML = '<i class="fas fa-expand"></i>'; btn.title = t('Pełny ekran'); }
            }
            body.querySelector('#rm-exit-fs').onclick = () => {
                if (_isFullscreen) _exitFullscreen();
                else _enterFullscreen();
            };
            if (_isMobile) _enterFullscreen();

            // Sidebar navigation is wired in _renderSidebar() below

            // Mobile bottom tab bar
            body.querySelectorAll('#rm-mobile-nav > .rm-mnav-btn').forEach(btn => {
                btn.onclick = () => {
                    if (btn.dataset.section === 'more') {
                        const sheet = body.querySelector('#rm-more-sheet');
                        if (sheet) sheet.classList.toggle('open');
                        return;
                    }
                    _syncMobileNav(btn.dataset.section);
                    body.querySelectorAll('.rm-sidebar-item').forEach(e => e.classList.toggle('active', e.dataset.section === btn.dataset.section));
                    activeSection = btn.dataset.section;
                    loadSection(activeSection);
                };
            });

            // "More" sheet section buttons
            body.querySelectorAll('.rm-more-btn[data-section]').forEach(btn => {
                btn.onclick = () => {
                    const section = btn.dataset.section;
                    _syncMobileNav(section);
                    body.querySelectorAll('.rm-sidebar-item').forEach(e => e.classList.toggle('active', e.dataset.section === section));
                    activeSection = section;
                    loadSection(section);
                };
            });

            // "Add to Home Screen" shortcut button
            const addHsBtn = body.querySelector('#rm-add-homescreen');
            if (addHsBtn) addHsBtn.onclick = () => {
                const sheet = body.querySelector('#rm-more-sheet');
                if (sheet) sheet.classList.remove('open');
                _showAddToHomescreenPrompt();
            };

            // Close "More" sheet on outside tap
            document.addEventListener('mousedown', (e) => {
                const sheet = body.querySelector('#rm-more-sheet');
                const moreBtn = body.querySelector('#rm-mobile-nav > .rm-mnav-btn[data-section="more"]');
                if (sheet && sheet.classList.contains('open') && !sheet.contains(e.target) && (!moreBtn || !moreBtn.contains(e.target))) {
                    sheet.classList.remove('open');
                }
            });
            document.addEventListener('touchstart', (e) => {
                const sheet = body.querySelector('#rm-more-sheet');
                const moreBtn = body.querySelector('#rm-mobile-nav > .rm-mnav-btn[data-section="more"]');
                if (sheet && sheet.classList.contains('open') && !sheet.contains(e.target) && (!moreBtn || !moreBtn.contains(e.target))) {
                    sheet.classList.remove('open');
                }
            }, { passive: true });

            // Player controls — optimistic toggle: icon flips instantly, reverts if play() rejects
            const playPauseBtn = body.querySelector('#rm-play-pause');
            playPauseBtn.onclick = () => {
                if (!_audio) return;
                // Restored state: audio src not yet loaded — reinitialise from saved track
                if (!_audio.src && _playing) { playAudio(_playing); return; }
                // When casting, route to Chromecast — don't touch local (muted) audio
                if (_castTogglePlayPause()) return;
                const npBtn = _npOverlay?.querySelector('#rm-np-playpause');
                if (_audio.paused) {
                    playPauseBtn.innerHTML = '<i class="fas fa-pause"></i>';
                    if (npBtn) npBtn.innerHTML = '<i class="fas fa-pause"></i>';
                    _showEq(true);
                    _audio.play().catch(err => {
                        if (err.name !== 'AbortError') {
                            playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
                            if (npBtn) npBtn.innerHTML = '<i class="fas fa-play"></i>';
                            _showEq(false);
                        }
                    });
                } else {
                    _audio.pause();
                    playPauseBtn.innerHTML = '<i class="fas fa-play"></i>';
                    if (npBtn) npBtn.innerHTML = '<i class="fas fa-play"></i>';
                    _showEq(false);
                }
            };
            body.querySelector('#rm-prev-btn').onclick = () => _skipStation(-1);
            body.querySelector('#rm-next-btn').onclick = () => _skipStation(1);

            // Cast button
            body.querySelector('#rm-cast-btn').onclick = () => _toggleCast();

            // Repeat: off → repeat all → repeat one → off
            function _syncRepeatBtn() {
                const btn = body.querySelector('#rm-repeat-btn');
                if (!btn) return;
                btn.classList.toggle('rm-mode-active', _repeatMode > 0);
                btn.innerHTML = _repeatMode === 2 ? '<i class="fas fa-redo"></i><span style="font-size:9px;position:absolute;font-weight:700">1</span>' : '<i class="fas fa-redo"></i>';
                btn.style.position = _repeatMode === 2 ? 'relative' : '';
                btn.title = [t('Powtarzaj'), t('Powtarzaj wszystko'), t('Powtarzaj jeden')][_repeatMode];
            }
            body.querySelector('#rm-repeat-btn').onclick = () => { _repeatMode = (_repeatMode + 1) % 3; _syncRepeatBtn(); };
            _syncRepeatBtn();

            // Shuffle
            function _syncShuffleBtn() {
                const btn = body.querySelector('#rm-shuffle-btn');
                if (btn) btn.classList.toggle('rm-mode-active', _shuffle);
            }
            body.querySelector('#rm-shuffle-btn').onclick = () => { _shuffle = !_shuffle; _syncShuffleBtn(); };
            _syncShuffleBtn();

            body.querySelector('#rm-vol').oninput = (e) => {
                const vol = e.target.value / 100;
                if (_isCasting && _castPlayer && _castController) {
                    // Route volume to Chromecast receiver, not local audio
                    _castPlayer.volumeLevel = vol;
                    _castController.setVolumeLevel();
                } else if (_audio) {
                    _audio.volume = vol;
                }
            };

            // Click player bar to open Now Playing overlay
            body.querySelector('#rm-player-art').onclick = () => _showNowPlaying();
            body.querySelector('#rm-player .rm-player-info').onclick = () => _showNowPlaying();

            // Seekbar interaction
            const seekTrack = body.querySelector('#rm-seek-track');
            seekTrack.onclick = (e) => {
                if (!_audio || !isFinite(_audio.duration)) return;
                const rect = seekTrack.getBoundingClientRect();
                const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                _audio.currentTime = pct * _audio.duration;
            };

            _renderSidebar();
            loadSection('most-played');

            // Pre-load liked songs so NP heart works immediately
            api('/radio-music/music/liked').then(d => { _likedSongs = d.items || []; }).catch(() => {});

            // Initialize Google Cast SDK (wrapped so failures don't break the app)
            try { _initCast(); } catch(e) { _cl('error', 'Cast init failed', { error: e.message }); }

            // Initialize offline archive manager (SocketIO listeners + SW readiness)
            try { _initArchive(); } catch(e) { _cl('error', 'Archive init failed', { error: e.message }); }

            // Subscribe store → auto-refresh queue highlights when track changes via Next/Prev/Cast
            _rmStore.subscribe(() => {
                _refreshQueueHighlight();
                // Refresh NP overlay queue — always (even if minimized), so it's ready on expand
                if (_renderNpQueueFn) _renderNpQueueFn();
            });

            // Restore previous playback state (paused, showing last track)
            _restoreAndShowLastTrack(body);
            _loadCrossfadeSetting();
            _loadEpProgress();
            _loadEqSettings();

            // Android back button: prevent exiting the app
            // Push sentinel history entry so the first back press fires popstate instead of navigating away
            if (!history.state?.rmApp) history.pushState({ rmApp: true }, '');
            window.addEventListener('popstate', _onPopState, true);
            window.addEventListener('keydown', _onKeyDown);

            // Register Periodic Background Sync to refresh station/music catalog every 24h
            if ('serviceWorker' in navigator && 'periodicSync' in ServiceWorkerRegistration.prototype) {
                navigator.serviceWorker.ready.then(async (reg) => {
                    try {
                        await reg.periodicSync.register('rm-catalog-refresh', { minInterval: 24 * 60 * 60 * 1000 });
                        _cl('debug', 'Periodic Background Sync registered');
                    } catch(e) { _cl('debug', 'PeriodicSync not allowed', { msg: e.message }); }
                });
            }
        },
        onClose() {
            _savePlaybackState();
            stopPlayback();
            _hideLockScreen();
            window.removeEventListener('popstate', _onPopState, true);
            window.removeEventListener('keydown', _onKeyDown);
            document.removeEventListener('visibilitychange', _onVisWakeLock);
            document.removeEventListener('visibilitychange', _onVisFocusLoss);
            if (_onDeviceChange && navigator.mediaDevices) {
                navigator.mediaDevices.removeEventListener('devicechange', _onDeviceChange);
            }
            _activePolls.forEach(p => clearInterval(p));
            _activePolls = [];
            document.body.classList.remove('app-fullscreen-active');
            if (_bc) { _bc.close(); _bc = null; }
        },
    });
    setTimeout(() => { if (typeof toggleMaximize === 'function') toggleMaximize('radio-music'); }, 50);

    // ── Dynamic Sidebar with Drag & Drop reordering ───────────────────────────

    const _MORE_SECTIONS = ['local','local-audiobooks','playlists','queue','history'];

    function _syncMobileNav(section) {
        if (!bodyEl) return;
        bodyEl.querySelectorAll('#rm-mobile-nav > .rm-mnav-btn').forEach(b => {
            if (b.dataset.section === 'more') {
                b.classList.toggle('active', _MORE_SECTIONS.includes(section));
            } else {
                b.classList.toggle('active', b.dataset.section === section);
            }
        });
        const sheet = bodyEl.querySelector('#rm-more-sheet');
        if (sheet) sheet.classList.remove('open');
    }

    function _navTo(section) {
        bodyEl.querySelectorAll('.rm-sidebar-item').forEach(e => e.classList.toggle('active', e.dataset.section === section));
        _syncMobileNav(section);
        activeSection = section;
        const content = bodyEl.querySelector('#rm-content');
        if (content) {
            content.classList.add('rm-fade-out');
            setTimeout(() => { loadSection(section); content.classList.remove('rm-fade-out'); }, 150);
        } else {
            loadSection(section);
        }
    }

    function _renderSidebar() {
        const sidebar = bodyEl.querySelector('#rm-sidebar');
        if (!sidebar) return;

        // Load saved group order (array of group IDs)
        let order;
        try { order = JSON.parse(localStorage.getItem('rm_menu_order') || 'null'); } catch(_) { order = null; }
        const defaultOrder = _SIDEBAR_GROUPS.map(g => g.id);
        const groupOrder = Array.isArray(order) ? order : defaultOrder;

        // Build ordered group list (filter out unknowns, append any new groups at end)
        const groupMap = Object.fromEntries(_SIDEBAR_GROUPS.map(g => [g.id, g]));
        const orderedGroups = [
            ...groupOrder.filter(id => groupMap[id]).map(id => groupMap[id]),
            ..._SIDEBAR_GROUPS.filter(g => !groupOrder.includes(g.id)),
        ];

        sidebar.innerHTML = '';
        let editMode = false;

        // Edit button in sidebar header
        const editBtn = document.createElement('button');
        editBtn.className = 'rm-sidebar-edit-btn';
        editBtn.style.cssText = 'margin:12px 12px 0;display:block;text-align:right';
        editBtn.innerHTML = '<i class="fas fa-sliders-h"></i> ' + t('Edytuj');
        editBtn.onclick = () => { editMode = !editMode; sidebar.classList.toggle('rm-sidebar-edit-mode', editMode); };
        sidebar.appendChild(editBtn);

        // Done button (shown in edit mode)
        const doneBtn = document.createElement('button');
        doneBtn.className = 'rm-sidebar-done-btn';
        doneBtn.textContent = t('✓ Gotowe');
        doneBtn.onclick = () => { editMode = false; sidebar.classList.remove('rm-sidebar-edit-mode'); };
        sidebar.appendChild(doneBtn);

        let dragSrc = null;

        orderedGroups.forEach(group => {
            const groupEl = document.createElement('div');
            groupEl.className = 'rm-sidebar-group';
            groupEl.dataset.groupId = group.id;

            // Group label row with drag handle
            const labelRow = document.createElement('div');
            labelRow.className = 'rm-sidebar-label';
            labelRow.innerHTML = `<span>${t(group.label)}</span>`;

            const handle = document.createElement('span');
            handle.className = 'rm-sidebar-drag-handle';
            handle.innerHTML = '<i class="fas fa-grip-vertical"></i>';
            labelRow.appendChild(handle);

            groupEl.appendChild(labelRow);

            // Group items
            group.items.forEach(item => {
                const el = document.createElement('div');
                el.className = 'rm-sidebar-item' + (item.key === activeSection ? ' active' : '');
                el.dataset.section = item.key;
                el.innerHTML = `<i class="${item.icon}"></i> ${t(item.label)}`;
                el.onclick = () => { if (!editMode) _navTo(item.key); };
                groupEl.appendChild(el);
            });

            // HTML5 Drag & Drop — only active when in edit mode
            // Guard: ignore drags starting within 20px of left/right edge (Motorola system gestures)
            groupEl.setAttribute('draggable', 'true');

            groupEl.addEventListener('dragstart', (e) => {
                if (!editMode) { e.preventDefault(); return; }
                // Motorola edge guard: ignore if drag starts near left (<20px) or right (>win-20px) edge
                if (e.clientX < 20 || e.clientX > window.innerWidth - 20) { e.preventDefault(); return; }
                dragSrc = groupEl;
                groupEl.classList.add('rm-dnd-dragging');
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', group.id);
            });

            groupEl.addEventListener('dragend', () => {
                groupEl.classList.remove('rm-dnd-dragging');
                sidebar.querySelectorAll('.rm-dnd-over').forEach(el => el.classList.remove('rm-dnd-over'));
            });

            groupEl.addEventListener('dragover', (e) => {
                if (!editMode || !dragSrc || dragSrc === groupEl) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                sidebar.querySelectorAll('.rm-dnd-over').forEach(el => el.classList.remove('rm-dnd-over'));
                groupEl.classList.add('rm-dnd-over');
            });

            groupEl.addEventListener('dragleave', () => {
                groupEl.classList.remove('rm-dnd-over');
            });

            groupEl.addEventListener('drop', (e) => {
                if (!editMode || !dragSrc || dragSrc === groupEl) return;
                e.preventDefault();
                groupEl.classList.remove('rm-dnd-over');
                // Reorder DOM
                const groups = [...sidebar.querySelectorAll('.rm-sidebar-group')];
                const srcIdx = groups.indexOf(dragSrc);
                const dstIdx = groups.indexOf(groupEl);
                if (srcIdx < dstIdx) groupEl.after(dragSrc);
                else groupEl.before(dragSrc);
                // Save new order to localStorage
                const newOrder = [...sidebar.querySelectorAll('.rm-sidebar-group')].map(el => el.dataset.groupId);
                localStorage.setItem('rm_menu_order', JSON.stringify(newOrder));
                dragSrc = null;
            });

            sidebar.appendChild(groupEl);
        });
    }

    function _syncSidebarActive(section) {
        bodyEl.querySelectorAll('.rm-sidebar-item').forEach(e => e.classList.toggle('active', e.dataset.section === section));
    }

    function loadSection(section) {
        const toolbar = bodyEl.querySelector('#rm-toolbar');
        const content = bodyEl.querySelector('#rm-content');
        toolbar.innerHTML = '';
        content.innerHTML = '';
        _syncSidebarActive(section);
        // Detect slow NAS disk wake (>3s response): show indicator, remove when content populates
        clearTimeout(_nasSpinTimer);
        _nasSpinTimer = setTimeout(() => {
            if (content.children.length === 0) {
                const el = document.createElement('div');
                el.className = 'rm-nas-spinup';
                el.innerHTML = '<span class="rm-nas-spinup-icon">⚙️</span>'
                    + '<span class="rm-nas-spinup-text">NAS budzi dyski…<br>Proszę czekać</span>';
                content.style.position = 'relative';
                content.appendChild(el);
                const obs = new MutationObserver(() => {
                    el.remove();
                    clearTimeout(_nasSpinTimer);
                    obs.disconnect();
                });
                obs.observe(content, { childList: true });
            }
        }, 3000);

        switch(section) {
            case 'radio': loadRadio(toolbar, content); break;
            case 'favorites': loadFavorites(content); break;
            case 'countries': loadCountries(toolbar, content); break;
            case 'tags': loadTags(content); break;
            case 'podcasts': loadPodcasts(toolbar, content); break;
            case 'subscriptions': loadSubscriptions(content); break;
            case 'music': loadMusic(toolbar, content); break;
            case 'local': loadLocal(toolbar, content); break;
            case 'local-audiobooks': loadLocalAudiobooks(toolbar, content); break;
            case 'playlists': loadPlaylists(toolbar, content); break;
            case 'most-played': loadMostPlayed(content); break;
            case 'queue': loadQueue(content); break;
            case 'history': loadHistory(content); break;
            case 'audiobooks': loadAudiobooks(toolbar, content); break;
            case 'discovery': loadDiscovery(toolbar, content); break;
            case 'pod-queue': loadPodQueue(content); break;
            case 'settings': loadSettings(content); break;
            case 'recently-added': loadRecentlyAdded(content); break;
            case 'artists': loadArtists(content); break;
            case 'search': loadUnifiedSearch(toolbar, content); break;
        }
    }

    /* ── Radio Browse ───────────────────────────────── */

    async function loadRadio(toolbar, content) {
        const RADIO_COUNTRIES = [{code:'',name:t('Wszystkie kraje')},{code:'PL',name:'Polska'},{code:'US',name:'USA'},{code:'GB',name:'UK'},
            {code:'DE',name:'Niemcy'},{code:'FR',name:'Francja'},{code:'ES',name:'Hiszpania'},{code:'IT',name:'Włochy'},
            {code:'BR',name:'Brazylia'},{code:'CA',name:'Kanada'},{code:'AU',name:'Australia'},{code:'JP',name:'Japonia'},
            {code:'SE',name:'Szwecja'},{code:'NL',name:'Holandia'},{code:'CZ',name:'Czechy'},{code:'UA',name:'Ukraina'}];
        const RADIO_TAGS = [
            {tag:'',label:t('Wszystkie gatunki')},{tag:'pop',label:'Pop'},{tag:'rock',label:'Rock'},{tag:'news',label:'News'},
            {tag:'classical',label:'Classical'},{tag:'jazz',label:'Jazz'},{tag:'electronic',label:'Electronic'},
            {tag:'hip hop',label:'Hip-Hop'},{tag:'ambient',label:'Ambient'},{tag:'metal',label:'Metal'},
            {tag:'country',label:'Country'},{tag:'dance',label:'Dance'},{tag:'talk',label:'Talk'},
            {tag:'80s',label:'80s'},{tag:'90s',label:'90s'},{tag:'oldies',label:'Oldies'},
            {tag:'chillout',label:'Chillout'},{tag:'reggae',label:'Reggae'},{tag:'latin',label:'Latin'},
        ];
        let _radioCountry = localStorage.getItem('rm-radio-country') || '', _radioTag = localStorage.getItem('rm-radio-tag') || '';

        toolbar.innerHTML = `<input class="rm-search" id="rm-radio-search" placeholder="${t('Szukaj stacji radiowych...')}" autofocus>`
            + `<div style="display:flex;gap:6px;flex-wrap:wrap">`
            + `<select class="rm-select" id="rm-radio-country" style="flex:1;min-width:120px">${RADIO_COUNTRIES.map(c => '<option value="'+c.code+'"'+(c.code===_radioCountry?' selected':'')+'>'+escH(c.name)+'</option>').join('')}</select>`
            + `<select class="rm-select" id="rm-radio-tag" style="flex:1;min-width:120px">${RADIO_TAGS.map(t => '<option value="'+t.tag+'"'+(t.tag===_radioTag?' selected':'')+'>'+escH(t.label)+'</option>').join('')}</select>`
            + `</div>`;

        content.innerHTML = '<div id="rm-radio-favs"></div><div id="rm-radio-results"><div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div></div>';

        // Load favorites inline at top
        const favsContainer = content.querySelector('#rm-radio-favs');
        const favData = await api('/radio-music/radio/favorites');
        _favorites = favData.items || [];
        if (_favorites.length) {
            favsContainer.innerHTML = '<div class="rm-section-title"><i class="fas fa-heart"></i> ' + t('Ulubione') + '</div><div class="rm-hscroll" id="rm-radio-favs-scroll"></div>';
            const scroll = favsContainer.querySelector('#rm-radio-favs-scroll');
            _favorites.forEach((s, i) => {
                const artHtml = s.favicon ? '<img src="' + escH(s.favicon) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-broadcast-tower\\\'></i>\'">' : '<i class="fas fa-broadcast-tower"></i>';
                const card = document.createElement('div');
                card.className = 'rm-hcard';
                card.innerHTML = '<div class="rm-hcard-art">' + artHtml + '</div>'
                    + '<div class="rm-hcard-title">' + escH(s.name) + '</div>'
                    + '<div class="rm-hcard-meta">' + escH([s.country, s.tags].filter(Boolean).join(' · ').substring(0, 30)) + '</div>';
                card.onclick = () => playStation(s);
                scroll.appendChild(card);
            });
        }

        const resultsContainer = content.querySelector('#rm-radio-results');

        function _doSearch() {
            const q = bodyEl.querySelector('#rm-radio-search').value.trim();
            if (q) { favsContainer.style.display = 'none'; searchRadio(q, _radioCountry, _radioTag, resultsContainer); }
            else { favsContainer.style.display = ''; loadTopRadio(_radioCountry, _radioTag, resultsContainer); }
        }

        const searchInput = bodyEl.querySelector('#rm-radio-search');
        let debounce;
        searchInput.onkeyup = () => { clearTimeout(debounce); debounce = setTimeout(_doSearch, 400); };

        bodyEl.querySelector('#rm-radio-country').onchange = (e) => { _radioCountry = e.target.value; localStorage.setItem('rm-radio-country', _radioCountry); _doSearch(); };
        bodyEl.querySelector('#rm-radio-tag').onchange = (e) => { _radioTag = e.target.value; localStorage.setItem('rm-radio-tag', _radioTag); _doSearch(); };

        loadTopRadio(_radioCountry, _radioTag, resultsContainer);
    }

    async function loadTopRadio(country, tag, content) {
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        if (!country && !tag) {
            const data = await api('/radio-music/radio/top?limit=50');
            if (!content?.isConnected) return;
            if (data.items && data.items.length) renderStations(data.items, content);
            else content.innerHTML = '<div class="rm-empty"><i class="fas fa-broadcast-tower"></i><p>' + t('Brak stacji') + '</p></div>';
        } else {
            let url = '/radio-music/radio/search?limit=50';
            if (country) url += '&country=' + country;
            if (tag) url += '&tag=' + encodeURIComponent(tag);
            const data = await api(url);
            if (!content?.isConnected) return;
            if (data.items && data.items.length) renderStations(data.items, content);
            else content.innerHTML = '<div class="rm-empty"><i class="fas fa-broadcast-tower"></i><p>' + t('Brak stacji') + '</p></div>';
        }
    }

    async function searchRadio(q, country, tag, content) {
        content.innerHTML = _skeletonGrid(6);
        let url = '/radio-music/radio/search?q=' + encodeURIComponent(q);
        if (country) url += '&country=' + country;
        if (tag) url += '&tag=' + encodeURIComponent(tag);
        const data = await api(url);
        if (data.items && data.items.length) {
            renderStations(data.items, content);
            if (data.hasMore) _setupInfiniteScroll(content, url, data.items.length, 50);
        } else {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-search"></i><p>' + t('Brak wyników') + '</p></div>';
        }
    }

    function _setupInfiniteScroll(container, baseUrl, loaded, limit) {
        const sentinel = document.createElement('div');
        sentinel.className = 'rm-load-more';
        sentinel.style.cssText = 'text-align:center;padding:16px;color:var(--rm-text-muted);font-size:12px';
        sentinel.textContent = t('Przewiń, aby załadować więcej...');
        container.appendChild(sentinel);
        let offset = loaded;
        let loading = false;
        const observer = new IntersectionObserver(async (entries) => {
            if (!entries[0].isIntersecting || loading) return;
            loading = true;
            sentinel.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            const url = baseUrl + '&offset=' + offset + '&limit=' + limit;
            const data = await api(url);
            const items = data.items || [];
            if (items.length) {
                sentinel.remove();
                renderStations(items, container, true);
                offset += items.length;
                if (data.hasMore) {
                    container.appendChild(sentinel);
                    sentinel.textContent = t('Przewiń, aby załadować więcej...');
                }
            } else {
                sentinel.textContent = t('To wszystkie wyniki');
                observer.disconnect();
            }
            loading = false;
        }, { rootMargin: '200px' });
        observer.observe(sentinel);
    }

    function renderStations(stations, container, append) {
        // Track visible stations for prev/next navigation
        if (!append) _recentStations = stations;
        else _recentStations = _recentStations.concat(stations);
        let grid;
        if (append) {
            grid = container.querySelector('#rm-stations-grid');
            if (!grid) { append = false; }
        }
        if (!append) {
            container.innerHTML = '<div class="rm-grid" id="rm-stations-grid"></div>';
            grid = container.querySelector('#rm-stations-grid');
        }
        stations.forEach(s => {
            const isFav = _favorites.some(f => f.uuid === s.uuid);
            const isPlaying = _playing && _playing.uuid === s.uuid;
            const altCount = (s.alt_urls || []).length;
            const codecTag = s.codec ? '<span class="rm-card-codec">' + escH(s.codec) + (s.bitrate ? ' ' + s.bitrate + 'k' : '') + '</span>' : '';
            const fallbackTag = altCount ? ' <span class="rm-card-codec">' + (altCount+1) + ' src</span>' : '';
            const card = document.createElement('div');
            card.className = 'rm-card' + (isPlaying ? ' rm-playing' : '');
            card._stationUuid = s.uuid;
            if (s.url) card.dataset.url = s.url;
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
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-heart"></i><p>' + t('Brak ulubionych stacji') + '</p><button class="rm-chip" style="margin-top:12px" id="rm-fav-browse">' + t('Przeglądaj stacje') + '</button></div>';
            content.querySelector('#rm-fav-browse')?.addEventListener('click', () => _navTo('most-played'));
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

    async function toggleLikedSong(track) {
        const isLiked = _likedSongs.some(s => s.url === track.url);
        const t = { name: track.name, url: track.url, meta: track.meta || '', image: track.image || track.thumbnail || '', type: 'music' };
        const data = await api('/radio-music/music/liked', {
            method: 'POST',
            body: { action: isLiked ? 'remove' : 'add', track: t }
        });
        _likedSongs = data.items || [];
    }

    /* ── Countries ─────────────────────────────────── */

    async function loadCountries(toolbar, content) {
        toolbar.innerHTML = `<input class="rm-search" id="rm-country-search" placeholder="${t('Szukaj krajów...')}">`;
        content.innerHTML = _skeletonGrid(6);

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
        content.innerHTML = _skeletonGrid(6);
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

    /* ── Podcasts Browse ──────────────────────────── */

    async function loadPodcasts(toolbar, content) {
        let _podCountry = localStorage.getItem('rm-pod-country') || 'pl', _podGenre = '';

        toolbar.innerHTML = `
            <input class="rm-search" id="rm-pod-search" placeholder="${t('Szukaj podcastów...')}" autofocus>
            <select class="rm-select" id="rm-pod-country">${_POD_COUNTRIES.map(c => '<option value="'+c.code+'"'+(c.code===_podCountry?' selected':'')+'>'+escH(c.name)+'</option>').join('')}</select>`;

        content.innerHTML = '<div id="rm-pod-subs"></div><div class="rm-chips" id="rm-pod-genres"></div><div id="rm-pod-results"></div>';

        // Load subscriptions inline at top
        const subsContainer = content.querySelector('#rm-pod-subs');
        const subData = await api('/radio-music/podcasts/subscriptions');
        _subscriptions = subData.items || [];
        if (_subscriptions.length) {
            subsContainer.innerHTML = '<div class="rm-section-title"><i class="fas fa-rss"></i> ' + t('Moje subskrypcje') + '</div><div class="rm-hscroll" id="rm-pod-subs-scroll"></div>';
            const scroll = subsContainer.querySelector('#rm-pod-subs-scroll');
            _subscriptions.forEach(p => {
                const artHtml = (p.artwork || p.image)
                    ? '<img src="' + escH(p.artwork || p.image) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-podcast\\\'></i>\'">'
                    : '<i class="fas fa-podcast"></i>';
                const card = document.createElement('div');
                card.className = 'rm-hcard';
                card.innerHTML = '<div class="rm-hcard-art">' + artHtml + '</div>'
                    + '<div class="rm-hcard-title">' + escH(p.name || p.title) + '</div>'
                    + '<div class="rm-hcard-meta">' + escH(p.artist || p.author || '') + '</div>';
                card.onclick = () => openPodcast(p);
                scroll.appendChild(card);
            });
        }

        // Genre chips
        const chipsEl = content.querySelector('#rm-pod-genres');
        _POD_GENRES.forEach(g => {
            const chip = document.createElement('span');
            chip.className = 'rm-chip' + (g.key === '' ? ' active' : '');
            chip.textContent = t(g.label);
            chip.dataset.genre = g.key;
            chip.onclick = () => {
                chipsEl.querySelectorAll('.rm-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                _podGenre = g.key;
                loadTopPodcasts(_podCountry, _podGenre, content.querySelector('#rm-pod-results'));
            };
            chipsEl.appendChild(chip);
        });

        // Country selector
        bodyEl.querySelector('#rm-pod-country').onchange = (e) => {
            _podCountry = e.target.value;
            localStorage.setItem('rm-pod-country', _podCountry);
            loadTopPodcasts(_podCountry, _podGenre, content.querySelector('#rm-pod-results'));
        };

        // Search
        const searchInput = bodyEl.querySelector('#rm-pod-search');
        let debounce;
        searchInput.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                if (searchInput.value.trim()) {
                    subsContainer.style.display = 'none';
                    chipsEl.style.display = 'none';
                    searchPodcasts(searchInput.value, content.querySelector('#rm-pod-results'));
                } else {
                    subsContainer.style.display = '';
                    chipsEl.style.display = '';
                    loadTopPodcasts(_podCountry, _podGenre, content.querySelector('#rm-pod-results'));
                }
            }, 500);
        };

        // Load top by default
        loadTopPodcasts(_podCountry, _podGenre, content.querySelector('#rm-pod-results'));
    }

    async function loadTopPodcasts(country, genre, container) {
        container.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        let url = '/radio-music/podcasts/top?country=' + country + '&limit=30';
        if (genre) url += '&genre=' + genre;
        const data = await api(url);
        if (!data.items || !data.items.length) {
            container.innerHTML = '<div class="rm-empty"><i class="fas fa-podcast"></i><p>' + t('Brak podcastów') + '</p></div>';
            return;
        }
        container.innerHTML = '<div class="rm-grid"></div>';
        const grid = container.querySelector('.rm-grid');
        data.items.forEach(p => {
            const card = document.createElement('div');
            card.className = 'rm-card';
            card.innerHTML = `
                <div class="rm-card-icon">${p.artwork ? '<img src="' + escH(p.artwork) + '">' : '<i class="fas fa-podcast"></i>'}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(p.name)}</div>
                    <div class="rm-card-meta">${escH(p.artist)}${p.genre ? ' · ' + escH(p.genre) : ''}</div>
                </div>`;
            card.onclick = async () => {
                // Top charts don't include feed_url — need lookup
                if (!p.feed_url && p.id) {
                    const lookup = await api('/radio-music/podcasts/lookup?id=' + p.id);
                    if (lookup.feed_url) {
                        p.feed_url = lookup.feed_url;
                        p.count = lookup.count;
                    }
                }
                openPodcast(p);
            };
            grid.appendChild(card);
        });
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
        content.innerHTML = _skeletonTracks(5);

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
            const pct = _getEpProgressPct(ep.audio_url);
            const prog = _epProgress[ep.audio_url];
            const doneIcon = prog && prog.done ? '<i class="fas fa-check-circle" style="color:var(--rm-accent);margin-right:6px"></i>' : '';
            html += `<div class="rm-ep-item" data-url="${escH(ep.audio_url)}">
                <div class="rm-ep-play"><i class="fas fa-play-circle"></i></div>
                <div class="rm-ep-info">
                    <div class="rm-ep-title">${doneIcon}${escH(ep.title)}</div>
                    <div class="rm-ep-meta">${escH([ep.pub_date, ep.duration_fmt].filter(Boolean).join(' · '))}</div>
                    ${pct > 0 ? '<div class="rm-ep-progress"><div class="rm-ep-progress-bar" style="width:' + pct + '%"></div></div>' : ''}
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

        // Episode play / queue — build full episode queue for prev/next navigation
        const allEpItems = [];
        content.querySelectorAll('.rm-ep-item').forEach(el => {
            const url = el.dataset.url;
            const title = el.querySelector('.rm-ep-title').textContent;
            allEpItems.push({
                name: title,
                url: url,
                type: 'podcast',
                _podcast: true,
                meta: pod.title || podcast.name,
                image: pod.image || podcast.artwork || '',
            });
        });
        content.querySelectorAll('.rm-ep-item').forEach((el, i) => {
            el.onclick = () => {
                _podQueue = allEpItems.slice();
                _podQueueIdx = i;
                playAudio(_podQueue[i]);
            };
        });
    }

    /* ── Subscriptions ─────────────────────────────── */

    async function loadSubscriptions(content) {
        const [subData, adData] = await Promise.all([
            api('/radio-music/podcasts/subscriptions'),
            api('/radio-music/podcasts/autodownload')
        ]);
        _subscriptions = subData.items || [];
        const adFeeds = (adData.feeds || {});

        if (!_subscriptions.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-rss"></i><p>' + t('Brak subskrypcji') + '</p></div>';
            return;
        }
        content.innerHTML = '<div class="rm-grid"></div>';
        const grid = content.querySelector('.rm-grid');
        _subscriptions.forEach(p => {
            const feedUrl = p.feed_url || '';
            const adEntry = adFeeds[feedUrl];
            const adEnabled = adEntry && adEntry.enabled;
            const card = document.createElement('div');
            card.className = 'rm-card';
            card.innerHTML = `
                <div class="rm-card-icon">${p.artwork || p.image ? '<img src="' + escH(p.artwork || p.image) + '">' : '<i class="fas fa-podcast"></i>'}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(p.name || p.title)}</div>
                    <div class="rm-card-meta">${escH(p.artist || p.author || '')}</div>
                </div>
                <button class="rm-pl-btn rm-sub-autod" title="${t('Auto-pobieranie')}" data-feed="${escH(feedUrl)}"
                    style="color:${adEnabled ? 'var(--rm-accent)' : 'var(--rm-text-muted)'}">
                    <i class="fas fa-cloud-download-alt"></i>
                </button>`;
            card.querySelector('.rm-sub-autod').onclick = async (e) => {
                e.stopPropagation();
                const btn = e.currentTarget;
                const nowEnabled = btn.style.color.includes('accent');
                const res = await api('/radio-music/podcasts/autodownload', { method: 'POST', body: { feed_url: feedUrl, enabled: !nowEnabled }});
                if (res.ok) {
                    btn.style.color = !nowEnabled ? 'var(--rm-accent)' : 'var(--rm-text-muted)';
                    toast((!nowEnabled ? t('Auto-pobieranie włączone') : t('Auto-pobieranie wyłączone')), 'success');
                }
            };
            card.onclick = (e) => { if (!e.target.closest('.rm-sub-autod')) openPodcast(p); };
            grid.appendChild(card);
        });
    }

    /* ── Music (YouTube / yt-dlp) ─────────────────── */

    async function loadMusic(toolbar, content) {
        // Check yt-dlp dependency first
        if (_ytdlpReady === null) {
            const deps = await api('/radio-music/music/check-deps');
            _ytdlpReady = deps.ready || false;
        }

        if (!_ytdlpReady) {
            toolbar.innerHTML = '';
            content.innerHTML = `<div class="rm-install-banner">
                <i class="fab fa-youtube"></i>
                <p>${t('Do odtwarzania muzyki z YouTube wymagany jest yt-dlp.')}</p>
                <button class="rm-install-btn" id="rm-install-ytdlp"><i class="fas fa-download"></i> ${t('Zainstaluj yt-dlp')}</button>
            </div>`;
            content.querySelector('#rm-install-ytdlp').onclick = async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Instalowanie...');
                const res = await api('/radio-music/music/install-deps', { method: 'POST' });
                if (res.ready) {
                    _ytdlpReady = true;
                    toast(t('yt-dlp zainstalowane!'), 'success');
                    loadMusic(toolbar, content);
                } else {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-download"></i> ' + t('Zainstaluj yt-dlp');
                    toast(res.error || t('Instalacja nie powiodła się'), 'error');
                }
            };
            return;
        }

        toolbar.innerHTML = `<input class="rm-search" id="rm-music-search" placeholder="${t('Szukaj muzyki na YouTube...')}" autofocus>`;

        // Genre chips
        content.innerHTML = '<div class="rm-chips" id="rm-music-genres"></div><div id="rm-music-results"></div>';
        const chipsEl = content.querySelector('#rm-music-genres');
        _MUSIC_GENRES.forEach(g => {
            const chip = document.createElement('span');
            chip.className = 'rm-chip';
            chip.textContent = t(g.label);
            chip.onclick = () => {
                chipsEl.querySelectorAll('.rm-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                searchMusic(g.q, content.querySelector('#rm-music-results'));
            };
            chipsEl.appendChild(chip);
        });

        const searchInput = bodyEl.querySelector('#rm-music-search');
        let debounce;
        searchInput.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                const q = searchInput.value.trim();
                if (q) {
                    chipsEl.querySelectorAll('.rm-chip').forEach(c => c.classList.remove('active'));
                    searchMusic(q, content.querySelector('#rm-music-results'));
                }
            }, 500);
        };

        // Show "Hity" by default
        const firstChip = chipsEl.querySelector('.rm-chip');
        if (firstChip) { firstChip.click(); }
    }

    async function searchMusic(q, container) {
        container.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i><p>' + t('Szukam...') + '</p></div>';
        const data = await api('/radio-music/music/search?q=' + encodeURIComponent(q) + '&limit=20');
        if (data.error) {
            container.innerHTML = '<div class="rm-empty"><i class="fas fa-exclamation-triangle"></i><p>' + escH(data.error) + '</p></div>';
            return;
        }
        if (!data.items || !data.items.length) {
            container.innerHTML = '<div class="rm-empty"><i class="fas fa-search"></i><p>' + t('Brak wyników') + '</p></div>';
            return;
        }
        renderMusicResults(data.items, container);
    }

    function renderMusicResults(tracks, container, dlFolder) {
        container.innerHTML = '<div style="display:flex;flex-direction:column;gap:6px" id="rm-tracks-list"></div>';
        const list = container.querySelector('#rm-tracks-list');
        // Pre-load archive status for all visible tracks
        _loadArchiveBatch(tracks.map(tr => tr.url).filter(Boolean));
        tracks.forEach((tr, idx) => {
            const isPlaying = _playing && _playing.id === tr.id;
            const el = document.createElement('div');
            el.className = 'rm-track' + (isPlaying ? ' rm-playing' : '');
            if (tr.url) el.dataset.url = tr.url;
            // Store metadata for archive use
            if (tr.url) _archiveDb[tr.url] = _archiveDb[tr.url] || {};
            if (tr.url && tr.title) (_archiveDb[tr.url] || {}).title = tr.title;
            el.innerHTML = `
                <img class="rm-track-thumb" src="${escH(tr.thumbnail)}" loading="lazy" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 48 48%22><rect fill=%22%231a1a2e%22 width=%2248%22 height=%2248%22/><text x=%2224%22 y=%2230%22 fill=%22%23666%22 text-anchor=%22middle%22 font-size=%2220%22>♪</text></svg>'">
                <div class="rm-track-info">
                    <div class="rm-track-title">${escH(tr.title)}</div>
                    <div class="rm-track-meta">${escH(tr.channel)}</div>
                </div>
                <span class="rm-track-dur">${escH(tr.duration_fmt)}</span>
                <div class="rm-track-actions">
                    ${_archiveBtnHtml(tr.url)}
                    <button class="rm-track-more" title="${t('Opcje')}"><i class="fas fa-ellipsis-v"></i></button>
                </div>`;
            el.onclick = (e) => {
                if (e.target.closest('.rm-arch-btn') || e.target.closest('.rm-track-more')) return;
                // F-02 playContext: clicking any track loads full folder as queue context
                playContext(tracks.map(t => t), idx);
            };
            el.querySelector('.rm-track-more').onclick = (e) => {
                e.stopPropagation();
                _showTrackSheet({
                    name: tr.title, url: tr.url, type: 'music',
                    meta: tr.channel, image: tr.thumbnail, source: 'youtube',
                });
            };
            const archBtn = el.querySelector('.rm-arch-btn');
            if (archBtn) archBtn.onclick = (e) => {
                e.stopPropagation();
                // Store track metadata for archive start
                if (tr.url) {
                    _archiveDb[tr.url] = {
                        ..._archiveDb[tr.url] || {},
                        title: tr.title, artist: tr.channel, thumbnail: tr.thumbnail
                    };
                }
                _onArchiveBtnClick(tr.url, archBtn);
            };
            list.appendChild(el);
        });
    }

    function playMusicTrack(tr) {
        // Queue entry from history list — route via original item
        if (tr._histItem) {
            const it = tr._histItem;
            if (it.type === 'local' || it.type === 'music' || it.type === 'podcast') {
                playAudio(it);
            } else {
                playStation(it);
            }
            return;
        }
        if (tr.source === 'local') {
            playAudio({
                name: tr.title, type: 'local', path: tr.url,
                url: '/api/radio-music/local/stream?path=' + encodeURIComponent(tr.url) + '&token=' + (NAS.token || ''),
                meta: tr.channel,
            });
            return;
        }
        playAudio({
            id: tr.id,
            name: tr.title,
            url: tr.url,
            type: 'music',
            meta: tr.channel,
            image: tr.thumbnail,
            duration: tr.duration,
            source: tr.source || 'youtube',
        });
    }

    /* ── Download ───────────────────────────────────── */

    async function _downloadTrack(track, btnEl, folder) {
        if (btnEl) { btnEl.classList.add('rm-downloading'); btnEl.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }
        const body = {
            url: track.url,
            title: track.title || track.name,
            artist: track.artist || track.meta || track.channel || '',
            thumbnail: track.thumbnail || track.image || '',
            duration: track.duration || 0,
            source: track.source || 'youtube',
            type: track.type || 'music',
        };
        if (folder) body.folder = folder;
        const data = await api('/radio-music/music/download', {
            method: 'POST',
            body: body,
        });
        if (data.error) {
            toast(data.error, 'error');
            if (btnEl) { btnEl.classList.remove('rm-downloading'); btnEl.innerHTML = '<i class="fas fa-download"></i>'; }
            return;
        }
        toast(t('Pobieranie rozpoczęte: ') + (track.title || track.name), 'success');
        // Poll for completion
        const jobId = data.job_id;
        const _poll = setInterval(async () => {
            const st = await api('/radio-music/music/downloads');
            const job = (st.jobs || {})[jobId];
            if (!job) { clearInterval(_poll); _activePolls = _activePolls.filter(p => p !== _poll); return; }
            if (job.status === 'done') {
                clearInterval(_poll); _activePolls = _activePolls.filter(p => p !== _poll);
                toast(t('Pobrano: ') + (track.title || track.name) + ' → Various Artists', 'success');
                if (btnEl) { btnEl.classList.remove('rm-downloading'); btnEl.classList.add('rm-downloaded'); btnEl.innerHTML = '<i class="fas fa-check"></i>'; }
            } else if (job.status === 'error') {
                clearInterval(_poll); _activePolls = _activePolls.filter(p => p !== _poll);
                toast(t('Błąd pobierania: ') + (job.error || ''), 'error');
                if (btnEl) { btnEl.classList.remove('rm-downloading'); btnEl.innerHTML = '<i class="fas fa-download"></i>'; }
            }
        }, 2000);
        _activePolls.push(_poll);
    }

    async function _downloadPlaylist(name, tracks) {
        const data = await api('/radio-music/music/download-playlist', {
            method: 'POST',
            body: { name, tracks },
        });
        if (data.error) { toast(data.error, 'error'); return; }
        toast(t('Pobieranie playlisty rozpoczęte: ') + name + ' (' + tracks.length + ' ' + t('utworów') + ')', 'success');
        const jobId = data.job_id;
        const _poll = setInterval(async () => {
            const st = await api('/radio-music/music/downloads');
            const job = (st.jobs || {})[jobId];
            if (!job) { clearInterval(_poll); _activePolls = _activePolls.filter(p => p !== _poll); return; }
            if (job.status === 'done' || job.status === 'done_partial') {
                clearInterval(_poll); _activePolls = _activePolls.filter(p => p !== _poll);
                const msg = job.status === 'done'
                    ? t('Playlista pobrana: ') + name
                    : t('Playlista pobrana częściowo: ') + name + (job.error ? ' — ' + job.error : '');
                toast(msg, job.status === 'done' ? 'success' : 'warning');
            } else if (job.status === 'error') {
                clearInterval(_poll); _activePolls = _activePolls.filter(p => p !== _poll);
                toast(t('Błąd pobierania: ') + (job.error || ''), 'error');
            }
        }, 3000);
        _activePolls.push(_poll);
    }

    /* ── Local Music ───────────────────────────────── */

    async function loadLocal(toolbar, content) {
        toolbar.innerHTML = '';
        content.innerHTML = _skeletonTracks(5);

        // Load folders config
        const foldersData = await api('/radio-music/local/folders');
        const folders = foldersData.items || [];

        // Toolbar: folder chips + add button
        let toolHtml = '<div class="rm-folder-chips">';
        folders.forEach(f => {
            const name = f.path.split('/').pop() || f.path;
            toolHtml += '<span class="rm-folder-chip' + (f.exists ? '' : ' rm-chip-missing') + '" data-path="' + escH(f.path) + '">'
                + '<i class="fas fa-folder' + (f.exists ? '' : '-times') + '" style="font-size:10px;margin-right:2px"></i> '
                + escH(name)
                + (f.removable ? ' <span class="rm-chip-remove" data-remove="' + escH(f.path) + '">×</span>' : '')
                + '</span>';
        });
        toolHtml += '<button class="rm-add-folder-btn" id="rm-add-folder"><i class="fas fa-plus"></i> ' + t('Dodaj folder') + '</button>';
        toolHtml += '</div>';
        toolbar.innerHTML = toolHtml;

        // Wire folder remove
        toolbar.querySelectorAll('.rm-chip-remove').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                const path = btn.dataset.remove;
                await api('/radio-music/local/folders', { method: 'POST', body: { action: 'remove', path } });
                loadLocal(toolbar, content);
            };
        });

        // Wire add folder
        toolbar.querySelector('#rm-add-folder').onclick = () => {
            const path = prompt(t('Podaj ścieżkę do folderu z muzyką:'), '/home/');
            if (!path) return;
            api('/radio-music/local/folders', { method: 'POST', body: { action: 'add', path } }).then(res => {
                if (res.error) toast(res.error, 'error');
                else loadLocal(toolbar, content);
            });
        };

        // Scan for audio files
        const scanData = await api('/radio-music/local/scan');
        const items = scanData.items || [];

        if (!items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-folder-open"></i><p>'
                + t('Brak plików audio w skonfigurowanych folderach') + '</p><p style="font-size:12px;color:var(--text-muted)">'
                + t('Dodaj foldery powyżej lub pobierz muzykę z YouTube') + '</p></div>';
            return;
        }

        // Group by first subdirectory (artist/album level), same as audiobooks
        const byFolder = {};
        items.forEach(it => {
            const parts = (it.relative || it.filename).split('/');
            const group = parts.length > 1 ? parts[0] : (it.folder.split('/').pop() || 'Muzyka');
            if (!byFolder[group]) byFolder[group] = [];
            byFolder[group].push(it);
        });

        // Precompute search string for each item
        items.forEach(file => {
            const metaParts = [];
            if (file.artist) metaParts.push(file.artist);
            if (file.album) metaParts.push(file.album);
            if (file.year) metaParts.push(file.year);
            if (file.genre) metaParts.push(file.genre);
            if (!metaParts.length) metaParts.push(file.filename);
            file._meta = metaParts.join(' · ');
            file._search = (file.name + ' ' + file._meta).toLowerCase();
        });

        // Search bar + list wrapper
        const searchWrap = document.createElement('div');
        searchWrap.className = 'rm-local-search-wrap';
        searchWrap.innerHTML = '<div class="rm-local-search-box">'
            + '<i class="fas fa-search" style="color:var(--text-muted);font-size:13px"></i>'
            + '<input id="rm-local-search" type="text" placeholder="' + t('Szukaj w bibliotece…') + '" autocomplete="off">'
            + '<span id="rm-local-count" style="color:var(--text-muted);font-size:12px;white-space:nowrap">' + items.length + ' ' + t('plików') + '</span>'
            + '</div>';
        content.innerHTML = '';
        content.appendChild(searchWrap);
        const listWrap = document.createElement('div');
        listWrap.id = 'rm-local-list-wrap';
        content.appendChild(listWrap);

        function _buildLocalTrackEl(file, folder) {
            const artUrl = file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '';
            const durStr = file.duration ? _fmtSecs(file.duration) : '';
            const el = document.createElement('div');
            el.className = 'rm-track rm-local-track';
            el.innerHTML = (artUrl
                ? '<img class="rm-track-thumb" src="' + escH(artUrl) + '" loading="lazy" onerror="this.outerHTML=\'<div class=\\\'rm-track-thumb\\\' style=\\\'display:flex;align-items:center;justify-content:center;background:#1a1a2e\\\'><i class=\\\'fas fa-music\\\' style=\\\'color:var(--text-muted)\\\'></i></div>\'">'
                : '<div class="rm-track-thumb" style="display:flex;align-items:center;justify-content:center;background:var(--rm-bg-surface)"><i class="fas fa-music" style="color:var(--text-muted)"></i></div>')
                + '<div class="rm-track-info"><div class="rm-track-title">' + escH(file.name) + '</div>'
                + '<div class="rm-track-meta">' + escH(file._meta) + '</div></div>'
                + (durStr ? '<span class="rm-track-dur">' + durStr + '</span>' : '')
                + '<div class="rm-track-actions">'
                + '<button class="rm-track-btn" title="' + t('Playlista') + '"><i class="fas fa-list-ul"></i></button>'
                + '<button class="rm-track-btn rm-add-queue-btn" title="' + t('Kolejka') + '"><i class="fas fa-plus"></i></button>'
                + '<button class="rm-track-btn rm-local-del-btn" title="' + t('Usuń plik') + '" style="color:rgba(239,68,68,.5)"><i class="fas fa-trash-alt"></i></button>'
                + '</div>';
            const localItem = {
                name: file.name, type: 'local', path: file.path,
                url: '/api/radio-music/local/stream?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || ''),
                meta: file._meta, image: '',
            };
            el.onclick = (e) => {
                if (e.target.closest('.rm-track-btn')) return;
                const folderFiles = byFolder[folder] || [file];
                const idx = folderFiles.indexOf(file);
                _musicQueue = folderFiles.slice(idx >= 0 ? idx : 0).map(f => {
                    const fArt = f.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(f.path) + '&token=' + (NAS.token || '') : '';
                    return { id: f.path, title: f.name, channel: f._meta || f.filename, url: f.path, thumbnail: fArt, duration: f.duration || 0, duration_fmt: f.duration ? _fmtSecs(f.duration) : '', source: 'local' };
                });
                _musicQueueIdx = 0;
                const fileArt = file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '';
                playAudio({ name: file.name, type: 'local', path: file.path, url: localItem.url, meta: file._meta, image: fileArt });
            };
            el.querySelector('.rm-add-queue-btn').onclick = (e) => {
                e.stopPropagation();
                const fArt = file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '';
                _musicQueue.push({ id: file.path, title: file.name, channel: file._meta, url: file.path, thumbnail: fArt, duration: file.duration || 0, duration_fmt: file.duration ? _fmtSecs(file.duration) : '', source: 'local' });
                toast(t('Dodano do kolejki: ') + file.name, 'success');
            };
            const plBtn = el.querySelector('.rm-track-btn[title="' + t('Playlista') + '"]');
            if (plBtn) plBtn.onclick = (e) => { e.stopPropagation(); _showAddToPlaylistModal(localItem); };
            const delBtn = el.querySelector('.rm-local-del-btn');
            if (delBtn) delBtn.onclick = async (e) => {
                e.stopPropagation();
                if (!confirm(t('Usunąć plik?') + '\n' + file.filename)) return;
                const res = await api('/radio-music/local/file', { method: 'DELETE', body: { path: file.path } });
                if (res.error) { toast(res.error, 'error'); return; }
                // Remove from in-memory lists and re-render
                const idx = items.indexOf(file);
                if (idx >= 0) items.splice(idx, 1);
                const bfArr = byFolder[folder];
                if (bfArr) {
                    const bi = bfArr.indexOf(file);
                    if (bi >= 0) bfArr.splice(bi, 1);
                    if (!bfArr.length) delete byFolder[folder];
                }
                _applyLocalFilter(content.querySelector('#rm-local-search')?.value || '');
                toast(t('Usunięto: ') + file.name, 'success');
            };
            return el;
        }

        // Progressive batch render — 80 items/frame to avoid blocking main thread
        // True virtual scroll — only renders visible items + buffer in DOM
        // TRACK_H / HEADER_H must match the rm-track and rm-section-title CSS heights
        const VSCROLL_TRACK_H = 68;  // rm-track: 64px + 4px gap
        const VSCROLL_HEADER_H = 52; // rm-section-title: margin+content
        const VSCROLL_BUFFER_PX = 400;
        let _vsScrollHandler = null;

        function _renderEntry(entry) {
            if (entry.isHeader) {
                const h = document.createElement('div');
                h.className = 'rm-section-title';
                h.style.display = 'flex';
                h.style.alignItems = 'center';
                h.style.justifyContent = 'space-between';
                const label = document.createElement('span');
                label.innerHTML = '<i class="fas fa-folder"></i> ' + escH(entry.name)
                    + ' <span style="font-size:11px;color:var(--text-muted);font-weight:400">(' + entry.count + ')</span>';
                h.appendChild(label);
                if (entry.folderPath) {
                    const delFolderBtn = document.createElement('button');
                    delFolderBtn.className = 'rm-track-btn rm-local-del-btn';
                    delFolderBtn.title = t('Usuń folder');
                    delFolderBtn.style.cssText = 'color:rgba(239,68,68,.5);flex-shrink:0';
                    delFolderBtn.innerHTML = '<i class="fas fa-folder-minus"></i>';
                    delFolderBtn.onclick = async (e) => {
                        e.stopPropagation();
                        const groupKey = entry.groupKey;
                        const folderFiles = byFolder[groupKey] || [];
                        if (!confirm(t('Usunąć folder i') + ' ' + folderFiles.length + ' ' + t('plików?') + '\n' + entry.folderPath)) return;
                        const res = await api('/radio-music/local/folder', { method: 'DELETE', body: { path: entry.folderPath } });
                        if (res.error) { toast(res.error, 'error'); return; }
                        // Remove all files in this group from in-memory lists
                        folderFiles.forEach(f => {
                            const idx = items.indexOf(f);
                            if (idx >= 0) items.splice(idx, 1);
                        });
                        delete byFolder[groupKey];
                        _applyLocalFilter(content.querySelector('#rm-local-search')?.value || '');
                        toast(t('Usunięto folder: ') + entry.name, 'success');
                    };
                    h.appendChild(delFolderBtn);
                }
                return h;
            }
            return _buildLocalTrackEl(entry.file, entry.folder);
        }

        function _initVirtualScroll(entries) {
            // Cleanup previous scroll listener
            if (_vsScrollHandler) { content.removeEventListener('scroll', _vsScrollHandler); _vsScrollHandler = null; }

            // Compute cumulative heights
            const heights = entries.map(e => e.isHeader ? VSCROLL_HEADER_H : VSCROLL_TRACK_H);
            const cumH = [0];
            heights.forEach(h => cumH.push(cumH[cumH.length - 1] + h));
            const totalH = cumH[entries.length];

            listWrap.style.position = 'relative';
            listWrap.style.height = totalH + 'px';

            const pool = new Map(); // index → rendered element

            function update() {
                const listTop = listWrap.offsetTop;
                const scroll = content.scrollTop;
                const viewH = content.clientHeight;
                const visStart = Math.max(0, scroll - listTop - VSCROLL_BUFFER_PX);
                const visEnd = scroll - listTop + viewH + VSCROLL_BUFFER_PX;

                // Binary search for first visible entry
                let lo = 0, hi = entries.length - 1;
                while (lo < hi) { const mid = (lo + hi) >> 1; if (cumH[mid + 1] <= visStart) lo = mid + 1; else hi = mid; }
                const first = Math.max(0, lo);
                let last = lo;
                while (last < entries.length - 1 && cumH[last + 1] < visEnd) last++;
                last = Math.min(entries.length - 1, last);

                // Remove out-of-range items
                for (const [i, el] of pool) {
                    if (i < first || i > last) { el.remove(); pool.delete(i); }
                }
                // Insert in-range items
                for (let i = first; i <= last; i++) {
                    if (pool.has(i)) continue;
                    const el = _renderEntry(entries[i]);
                    el.style.position = 'absolute';
                    el.style.top = cumH[i] + 'px';
                    el.style.left = '0'; el.style.right = '0';
                    listWrap.appendChild(el);
                    pool.set(i, el);
                }
            }

            _vsScrollHandler = update;
            content.addEventListener('scroll', update, { passive: true });
            update();
        }

        let _localSearchTimer = null;
        function _applyLocalFilter(query) {
            listWrap.innerHTML = '';
            const q = query.toLowerCase().trim();
            const entries = [];
            let count = 0;
            if (q) {
                items.filter(f => f._search.includes(q)).forEach(f => {
                    entries.push({ file: f, folder: f.folder });
                    count++;
                });
            } else {
                const sortedGroups = Object.entries(byFolder).sort(([a], [b]) => a.localeCompare(b));
                for (const [group, files] of sortedGroups) {
                    const sample = files[0];
                    const parts = (sample.relative || sample.filename).split('/');
                    const realFolderPath = parts.length > 1 ? (sample.folder + '/' + group) : sample.folder;
                    entries.push({ isHeader: true, name: group, count: files.length, folderPath: realFolderPath, groupKey: group });
                    files.forEach(f => { entries.push({ file: f, folder: group }); count++; });
                }
            }
            const countEl = content.querySelector('#rm-local-count');
            if (countEl) countEl.textContent = count + ' ' + t('plików');
            _initVirtualScroll(entries);
        }

        const searchInput = content.querySelector('#rm-local-search');
        searchInput.addEventListener('input', () => {
            clearTimeout(_localSearchTimer);
            _localSearchTimer = setTimeout(() => _applyLocalFilter(searchInput.value), 200);
        });

        _applyLocalFilter('');
    }

    /* ── Local Audiobooks ──────────────────────────────── */

    async function loadLocalAudiobooks(toolbar, content) {
        toolbar.innerHTML = '';
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';

        const scanData = await api('/radio-music/local/scan?scope=audiobooks');
        const items = scanData.items || [];

        if (!items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-book-reader"></i><p>'
                + t('Brak pobranych audiobooków') + '</p><p style="font-size:12px;color:var(--text-muted)">'
                + t('Pobierz audiobooki z sekcji Audiobooki (YouTube)') + '</p></div>';
            return;
        }

        // Group by folder (artist/uploader)
        const byFolder = {};
        items.forEach(it => {
            const rel = it.relative || it.filename;
            const parts = rel.split('/');
            const group = parts.length > 1 ? parts[0] : it.folder.split('/').pop() || 'Audiobooki';
            if (!byFolder[group]) byFolder[group] = [];
            byFolder[group].push(it);
        });

        let html = '';
        for (const [folder, files] of Object.entries(byFolder)) {
            html += '<div class="rm-section-title"><i class="fas fa-book-open"></i> ' + escH(folder)
                + ' <span style="font-size:11px;color:var(--text-muted);font-weight:400">(' + files.length + ')</span></div>';
            html += '<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:16px">';
            files.forEach((file, i) => {
                const artUrl = file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '';
                const metaParts = [];
                if (file.artist) metaParts.push(file.artist);
                if (file.album) metaParts.push(file.album);
                if (!metaParts.length) metaParts.push(file.filename);
                const durStr = file.duration ? _fmtSecs(file.duration) : '';
                const isLocalPlaying = _playing && _playing.path && _playing.path === file.path;
                html += '<div class="rm-track rm-lab-track' + (isLocalPlaying ? ' rm-playing' : '') + '" data-group="' + escH(folder) + '" data-idx="' + i + '" data-url="' + escH(file.path) + '">'
                    + (artUrl
                        ? '<img class="rm-track-thumb" src="' + escH(artUrl) + '" loading="lazy" onerror="this.outerHTML=\'<div class=\\\'rm-track-thumb\\\' style=\\\'display:flex;align-items:center;justify-content:center;background:#1a1a2e\\\'><i class=\\\'fas fa-book\\\' style=\\\'color:var(--text-muted)\\\'></i></div>\'">'
                        : '<div class="rm-track-thumb" style="display:flex;align-items:center;justify-content:center;background:var(--rm-bg-surface)"><i class="fas fa-book" style="color:var(--text-muted)"></i></div>')
                    + '<div class="rm-track-info"><div class="rm-track-title">' + escH(file.name) + '</div>'
                    + '<div class="rm-track-meta">' + escH(metaParts.join(' · ')) + '</div></div>'
                    + (durStr ? '<span class="rm-track-dur">' + durStr + '</span>' : '')
                    + '<div class="rm-track-actions">'
                    + '<button class="rm-track-btn rm-add-queue-btn" title="' + t('Kolejka') + '"><i class="fas fa-plus"></i></button>'
                    + '</div></div>';
            });
            html += '</div>';
        }
        content.innerHTML = html;

        // Wire clicks
        content.querySelectorAll('.rm-lab-track').forEach(el => {
            const group = el.dataset.group;
            const idx = parseInt(el.dataset.idx);
            const file = byFolder[group][idx];
            el.onclick = (e) => {
                if (e.target.closest('.rm-track-btn')) return;
                const groupFiles = byFolder[group];
                _musicQueue = groupFiles.slice(idx).map(f => {
                    const fArt = f.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(f.path) + '&token=' + (NAS.token || '') : '';
                    return {
                        id: f.path, title: f.name, channel: [f.artist, f.album].filter(Boolean).join(' · ') || f.filename,
                        url: f.path, thumbnail: fArt, duration: f.duration || 0, duration_fmt: f.duration ? _fmtSecs(f.duration) : '',
                        source: 'local',
                    };
                });
                _musicQueueIdx = 0;
                const fileArt = file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '';
                playAudio({
                    name: file.name, type: 'local', path: file.path,
                    url: '/api/radio-music/local/stream?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || ''),
                    meta: [file.artist, file.album].filter(Boolean).join(' · ') || file.filename,
                    image: fileArt,
                });
            };
            el.querySelector('.rm-add-queue-btn').onclick = (e) => {
                e.stopPropagation();
                const fArt = file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '';
                _musicQueue.push({
                    id: file.path, title: file.name, channel: [file.artist, file.album].filter(Boolean).join(' · ') || file.filename,
                    url: file.path, thumbnail: fArt, duration: file.duration || 0, duration_fmt: file.duration ? _fmtSecs(file.duration) : '',
                    source: 'local',
                });
                toast(t('Dodano do kolejki: ') + file.name, 'success');
            };
        });
    }

    /* ── Playlists ──────────────────────────────────── */

    async function _loadPlaylists() {
        const data = await api('/radio-music/playlists');
        _playlists = data.items || [];
    }

    async function loadPlaylists(toolbar, content) {
        toolbar.innerHTML = '';
        content.innerHTML = _skeletonTracks(5);
        await _loadPlaylists();

        let html = '<div class="rm-pl-header"><h3>' + t('Moje playlisty') + '</h3>'
            + '<div class="rm-pl-create"><input id="rm-pl-name" placeholder="' + t('Nazwa playlisty...') + '">'
            + '<button id="rm-pl-create-btn"><i class="fas fa-plus"></i> ' + t('Utwórz') + '</button>'
            + '<button id="rm-pl-import-btn" title="' + t('Importuj M3U') + '"><i class="fas fa-file-import"></i></button>'
            + '<input type="file" id="rm-pl-import-file" accept=".m3u,.m3u8" style="display:none">'
            + '</div></div>';

        if (!_playlists.length) {
            html += '<div class="rm-empty"><i class="fas fa-list"></i><p>' + t('Brak playlist. Utwórz pierwszą!') + '</p></div>';
        } else {
            html += '<div style="display:flex;flex-direction:column;gap:8px">';
            _playlists.forEach(pl => {
                const coverHtml = _playlistCoverHtml(pl, 48);
                html += `<div class="rm-pl-card" data-plid="${escH(pl.id)}">
                    ${coverHtml}
                    <div class="rm-pl-info">
                        <div class="rm-pl-name">${escH(pl.name)}</div>
                        <div class="rm-pl-meta">${pl.tracks.length} ${t('utworów')}</div>
                    </div>
                    <div class="rm-pl-actions">
                        <button class="rm-pl-btn rm-pl-play" title="${t('Odtwórz')}"><i class="fas fa-play"></i></button>
                        <button class="rm-pl-btn rm-pl-export" title="${t('Eksportuj M3U')}"><i class="fas fa-file-export"></i></button>
                        <button class="rm-pl-btn rm-pl-del" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                    </div>
                </div>`;
            });
            html += '</div>';
        }
        content.innerHTML = html;

        // Create playlist
        content.querySelector('#rm-pl-create-btn').onclick = async () => {
            const name = content.querySelector('#rm-pl-name').value.trim();
            if (!name) return;
            await api('/radio-music/playlists', { method: 'POST', body: { name } });
            loadPlaylists(toolbar, content);
        };
        content.querySelector('#rm-pl-name').onkeyup = (e) => {
            if (e.key === 'Enter') content.querySelector('#rm-pl-create-btn').click();
        };

        // Import M3U
        content.querySelector('#rm-pl-import-btn').onclick = () => content.querySelector('#rm-pl-import-file').click();
        content.querySelector('#rm-pl-import-file').onchange = async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const form = new FormData();
            form.append('file', file);
            const res = await fetch('/api/radio-music/playlists/import', {
                method: 'POST', body: form,
                headers: { 'Authorization': 'Bearer ' + (NAS.token || '') }
            }).then(r => r.json());
            if (res.ok) { toast(t('Zaimportowano: ') + (res.playlist?.name || ''), 'success'); loadPlaylists(toolbar, content); }
            else toast(res.error || t('Błąd importu'), 'error');
        };

        // Click handlers
        content.querySelectorAll('.rm-pl-card').forEach(card => {
            const plId = card.dataset.plid;
            card.onclick = (e) => {
                if (e.target.closest('.rm-pl-play') || e.target.closest('.rm-pl-del') || e.target.closest('.rm-pl-export')) return;
                openPlaylist(plId, content);
            };
            const playBtn = card.querySelector('.rm-pl-play');
            if (playBtn) playBtn.onclick = (e) => {
                e.stopPropagation();
                const pl = _playlists.find(p => p.id === plId);
                if (pl && pl.tracks.length) {
                    _musicQueue = pl.tracks.map(t => ({...t, _plItem: true}));
                    _musicQueueIdx = 0;
                    _playTrackFromPlaylist(pl.tracks[0]);
                    toast(t('Odtwarzam: ') + pl.name, 'success');
                }
            };
            const exportBtn = card.querySelector('.rm-pl-export');
            if (exportBtn) exportBtn.onclick = (e) => {
                e.stopPropagation();
                window.open('/api/radio-music/playlists/' + plId + '/export?token=' + (NAS.token || ''), '_blank');
            };
            const delBtn = card.querySelector('.rm-pl-del');
            if (delBtn) delBtn.onclick = async (e) => {
                e.stopPropagation();
                await api('/radio-music/playlists/' + plId, { method: 'DELETE' });
                loadPlaylists(toolbar, content);
            };
        });
    }

    async function openPlaylist(plId, content) {
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        const data = await api('/radio-music/playlists/' + plId);
        if (data.error) { content.innerHTML = '<div class="rm-empty"><p>' + escH(data.error) + '</p></div>'; return; }
        const pl = data.playlist;
        let editMode = false;
        let currentTracks = pl.tracks.slice(); // mutable copy for DnD

        function _renderTracks() {
            const trackList = content.querySelector('#rm-pl-tracklist');
            if (!trackList) return;
            trackList.innerHTML = '';
            currentTracks.forEach((tr, idx) => {
                const icon = tr.type === 'radio' ? 'fa-broadcast-tower' : tr.type === 'podcast' ? 'fa-podcast' : 'fa-music';
                const isPlaying = _playing && _playing.url && _playing.url === tr.url;
                const el = document.createElement('div');
                el.className = 'rm-track' + (isPlaying ? ' rm-playing' : '');
                el.dataset.idx = idx;
                el.dataset.url = tr.url || '';
                if (editMode) el.setAttribute('draggable', 'true');
                el.innerHTML = `${tr.image || tr.thumbnail ? '<img class="rm-track-thumb" src="' + escH(tr.image || tr.thumbnail) + '" loading="lazy">' : '<div class="rm-track-thumb" style="display:flex;align-items:center;justify-content:center"><i class="fas ' + icon + '" style="color:var(--text-muted)"></i></div>'}
                    <div class="rm-track-info">
                        <div class="rm-track-title">${escH(tr.name || tr.title)}</div>
                        <div class="rm-track-meta">${escH(tr.meta || tr.channel || '')} ${tr.type ? '<span class="rm-card-codec">' + tr.type + '</span>' : ''}</div>
                    </div>
                    ${tr.duration_fmt ? '<span class="rm-track-dur">' + escH(tr.duration_fmt) + '</span>' : ''}
                    <button class="rm-track-more" title="${t('Opcje')}"><i class="fas fa-ellipsis-v"></i></button>`;
                el.onclick = (e) => {
                    if (e.target.closest('.rm-track-more')) return;
                    if (editMode) return;
                    _musicQueue = currentTracks.slice(idx).map(t => ({...t, _plItem: true}));
                    _musicQueueIdx = 0;
                    _playTrackFromPlaylist(currentTracks[idx]);
                };
                el.querySelector('.rm-track-more').onclick = (e) => {
                    e.stopPropagation();
                    _showTrackSheet(tr, {
                        inPlaylist: plId,
                        trackIdx: idx,
                        onRemoved: () => openPlaylist(plId, content),
                    });
                };
                // DnD reorder
                el.addEventListener('dragstart', (e) => {
                    el.classList.add('rm-dnd-dragging');
                    e.dataTransfer.effectAllowed = 'move';
                    e.dataTransfer.setData('text/plain', idx);
                });
                el.addEventListener('dragend', () => el.classList.remove('rm-dnd-dragging'));
                el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('rm-dnd-over'); });
                el.addEventListener('dragleave', () => el.classList.remove('rm-dnd-over'));
                el.addEventListener('drop', async (e) => {
                    e.preventDefault();
                    el.classList.remove('rm-dnd-over');
                    const fromIdx = parseInt(e.dataTransfer.getData('text/plain'));
                    const toIdx = parseInt(el.dataset.idx);
                    if (isNaN(fromIdx) || fromIdx === toIdx) return;
                    const moved = currentTracks.splice(fromIdx, 1)[0];
                    currentTracks.splice(toIdx, 0, moved);
                    _renderTracks();
                    // Persist new order
                    await api('/radio-music/playlists/' + plId, { method: 'PUT', body: { tracks: currentTracks } });
                });
                trackList.appendChild(el);
            });
        }

        let html = '<div class="rm-pl-header">'
            + '<button class="rm-pl-btn" id="rm-pl-back" style="font-size:16px;color:var(--text-primary)"><i class="fas fa-arrow-left"></i></button>'
            + '<h3>' + escH(pl.name) + '</h3>'
            + '<button class="rm-pl-btn rm-pl-play-all" title="' + t('Odtwórz wszystko') + '" style="color:var(--accent);font-size:16px"><i class="fas fa-play"></i></button>'
            + '<button class="rm-pl-edit-btn" id="rm-pl-edit-toggle" title="' + t('Edytuj kolejność') + '"><i class="fas fa-sort"></i></button>'
            + '<button class="rm-dl-btn rm-pl-dl-all" title="' + t('Pobierz playlistę') + '" style="font-size:16px"><i class="fas fa-download"></i></button>'
            + '</div>';

        if (!pl.tracks.length) {
            html += '<div class="rm-empty"><i class="fas fa-music"></i><p>' + t('Playlista jest pusta') + '</p><p style="font-size:12px;margin-top:4px">' + t('Dodaj utwory z sekcji Muzyka, Radio lub Podcasty') + '</p></div>';
        } else {
            html += '<div id="rm-pl-tracklist" style="display:flex;flex-direction:column;gap:4px"></div>';
        }
        content.innerHTML = html;
        if (pl.tracks.length) _renderTracks();

        content.querySelector('#rm-pl-back').onclick = () => {
            const toolbar = bodyEl.querySelector('#rm-toolbar');
            loadPlaylists(toolbar, content);
        };

        const editToggle = content.querySelector('#rm-pl-edit-toggle');
        if (editToggle) editToggle.onclick = () => {
            editMode = !editMode;
            editToggle.classList.toggle('active', editMode);
            editToggle.title = editMode ? t('Gotowe') : t('Edytuj kolejność');
            content.querySelector('#rm-pl-tracklist')?.classList.toggle('rm-pl-edit-mode', editMode);
            _renderTracks();
        };

        const playAllBtn = content.querySelector('.rm-pl-play-all');
        if (playAllBtn) playAllBtn.onclick = () => {
            if (currentTracks.length) {
                _musicQueue = currentTracks.map(t => ({...t, _plItem: true}));
                _musicQueueIdx = 0;
                _playTrackFromPlaylist(currentTracks[0]);
            }
        };

        const dlAllBtn = content.querySelector('.rm-pl-dl-all');
        if (dlAllBtn) dlAllBtn.onclick = () => {
            const musicTracks = currentTracks.filter(t => t.type === 'music' && t.url);
            if (!musicTracks.length) {
                toast(t('Brak utworów do pobrania (tylko YouTube)'), 'warning');
                return;
            }
            _downloadPlaylist(pl.name, musicTracks);
            dlAllBtn.classList.add('rm-downloading');
            dlAllBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        };
    }

    function _playTrackFromPlaylist(tr) {
        if (tr.type === 'radio') {
            playStation(tr);
        } else {
            // Playlist items already have {name, url, type, meta, image} — playAudio format
            playAudio(tr);
        }
    }

    function _showTrackSheet(track, opts = {}) {
        /* opts: { inPlaylist: plId, trackIdx: number, onRemoved: fn } */
        const archEntry = track.url ? (_archiveDb[track.url] || null) : null;
        const archKey = archEntry?.key || null;
        const isYT = track.type === 'music';
        const isDone = archEntry?.status === 'done';
        const isDling = archEntry?.status === 'downloading';

        const overlay = document.createElement('div');
        overlay.className = 'rm-tsheet-overlay';
        const sheet = document.createElement('div');
        sheet.className = 'rm-tsheet';

        const rows = [
            { icon: 'fa-play-circle', label: t('Odtwórz'), action: 'play' },
            { icon: 'fa-step-forward', label: t('Odtwórz jako następny'), action: 'play-next' },
            { icon: 'fa-list-ol', label: t('Dodaj do kolejki'), action: 'queue' },
            { icon: 'fa-list', label: t('Dodaj do playlisty'), action: 'playlist' },
        ];
        if (isYT && !isDone && !isDling) {
            rows.push({ icon: 'fa-cloud-download-alt', label: t('Pobierz do folderu Muzyka'), action: 'archive' });
        }
        if (isYT && isDling) {
            rows.push({ icon: 'fa-cloud-download-alt', label: t('Pobieranie…'), action: 'archive', extra: (archEntry.progress || 0) + '%', disabled: true });
        }
        if (isDone && archKey) {
            rows.push({ icon: 'fa-download', label: t('Zapisz plik na dysku'), action: 'download' });
        }
        if (opts.inPlaylist != null) {
            rows.push({ icon: 'fa-trash', label: t('Usuń z playlisty'), action: 'remove', danger: true });
        }

        sheet.innerHTML = `<div class="rm-tsheet-handle"></div>
            <div class="rm-tsheet-title">${escH(track.name || track.title || '?')}</div>
            ${rows.map(r => `<div class="rm-tsheet-row${r.danger ? ' danger' : ''}${r.disabled ? ' disabled' : ''}" data-action="${r.action}">
                <i class="fas ${r.icon}"></i><span>${escH(r.label)}</span>
                ${r.extra ? `<span class="rm-tsheet-progress">${escH(r.extra)}</span>` : ''}
            </div>`).join('')}`;

        document.body.appendChild(overlay);
        document.body.appendChild(sheet);
        requestAnimationFrame(() => sheet.classList.add('open'));

        const close = () => {
            sheet.classList.remove('open');
            setTimeout(() => { overlay.remove(); sheet.remove(); }, 260);
        };
        overlay.onclick = close;

        sheet.querySelectorAll('.rm-tsheet-row[data-action]').forEach(row => {
            row.onclick = async () => {
                const action = row.dataset.action;
                close();
                if (action === 'play') {
                    if (track.type === 'radio') playStation(track);
                    else playAudio(track);
                } else if (action === 'play-next') {
                    const insertIdx = Math.min(_musicQueueIdx + 1, _musicQueue.length);
                    _musicQueue.splice(insertIdx, 0, track);
                    toast(t('Następny w kolejce: ') + (track.name || track.title || ''), 'success');
                    if (_renderNpQueueFn) _renderNpQueueFn();
                } else if (action === 'queue') {
                    _musicQueue.push(track);
                    toast(t('Dodano do kolejki'), 'success');
                } else if (action === 'playlist') {
                    _showAddToPlaylistModal(track);
                } else if (action === 'archive') {
                    _onArchiveBtnClick(track.url, null);
                } else if (action === 'download' && archKey) {
                    const a = document.createElement('a');
                    a.href = `/api/radio-music/archive/download/${archKey}?token=${NAS.token || ''}`;
                    a.download = (track.name || track.title || archKey) + '.mp3';
                    document.body.appendChild(a); a.click(); a.remove();
                } else if (action === 'remove' && opts.inPlaylist != null) {
                    await api('/radio-music/playlists/' + opts.inPlaylist + '/tracks/' + opts.trackIdx, { method: 'DELETE' });
                    if (opts.onRemoved) opts.onRemoved();
                }
            };
        });
    }

    function _showAddToPlaylistModal(track) {
        const overlay = document.createElement('div');
        overlay.className = 'rm-modal-overlay';
        overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

        const modal = document.createElement('div');
        modal.className = 'rm-modal';

        const render = () => {
            let html = '<h4>' + t('Dodaj do playlisty') + '</h4>';
            // "Create new" row
            html += `<div class="rm-modal-item rm-modal-new-pl" data-action="new"><i class="fas fa-plus-circle" style="color:var(--rm-accent)"></i> <span>${t('Utwórz nową playlistę')}</span></div>`;
            // Inline create form (hidden by default)
            html += `<div class="rm-modal-create-form" style="display:none;padding:8px 0 4px">
                <input class="rm-modal-new-input" placeholder="${t('Nazwa playlisty...')}" style="width:100%;box-sizing:border-box;padding:8px 10px;border-radius:8px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);color:var(--rm-text);font-size:13px;outline:none">
                <div style="display:flex;gap:8px;margin-top:8px">
                    <button class="rm-modal-create-confirm" style="flex:1;padding:7px;border-radius:20px;background:var(--rm-accent);color:var(--rm-bg);border:none;cursor:pointer;font-size:12px;font-weight:700">${t('Utwórz i dodaj')}</button>
                    <button class="rm-modal-create-cancel" style="flex:1;padding:7px;border-radius:20px;background:none;border:1px solid rgba(255,255,255,.15);color:rgba(255,255,255,.7);cursor:pointer;font-size:12px">${t('Anuluj')}</button>
                </div>
            </div>`;
            if (_playlists.length) {
                html += '<div style="margin:10px 0 4px;font-size:11px;color:rgba(255,255,255,.3);text-transform:uppercase;letter-spacing:.05em">' + t('Istniejące playlisty') + '</div>';
                _playlists.forEach(pl => {
                    html += `<div class="rm-modal-item" data-plid="${escH(pl.id)}"><i class="fas fa-list"></i> ${escH(pl.name)} <span style="color:var(--text-muted);margin-left:auto;font-size:11px">${pl.tracks.length}</span></div>`;
                });
            }
            html += '<button class="rm-modal-close">' + t('Anuluj') + '</button>';
            modal.innerHTML = html;

            // Toggle create form
            modal.querySelector('.rm-modal-new-pl').onclick = () => {
                const form = modal.querySelector('.rm-modal-create-form');
                const isOpen = form.style.display !== 'none';
                form.style.display = isOpen ? 'none' : 'block';
                if (!isOpen) modal.querySelector('.rm-modal-new-input').focus();
            };

            // Create & add
            const doCreate = async () => {
                const input = modal.querySelector('.rm-modal-new-input');
                const name = input.value.trim();
                if (!name) { input.focus(); return; }
                const res = await api('/radio-music/playlists', { method: 'POST', body: { name } });
                if (res.error) { toast(res.error, 'error'); return; }
                await _loadPlaylists();
                const newPl = _playlists.find(p => p.name === name);
                if (newPl) {
                    await api('/radio-music/playlists/' + newPl.id + '/tracks', { method: 'POST', body: { track } });
                    overlay.remove();
                    toast(t('Utwórzono i dodano do: ') + name, 'success');
                }
            };
            modal.querySelector('.rm-modal-create-confirm').onclick = doCreate;
            modal.querySelector('.rm-modal-new-input').onkeydown = (e) => { if (e.key === 'Enter') doCreate(); };
            modal.querySelector('.rm-modal-create-cancel').onclick = () => {
                modal.querySelector('.rm-modal-create-form').style.display = 'none';
            };

            modal.querySelector('.rm-modal-close').onclick = () => overlay.remove();
            modal.querySelectorAll('.rm-modal-item[data-plid]').forEach(el => {
                el.onclick = async () => {
                    const plId = el.dataset.plid;
                    await api('/radio-music/playlists/' + plId + '/tracks', { method: 'POST', body: { track } });
                    overlay.remove();
                    toast(t('Dodano do: ') + _playlists.find(p => p.id === plId)?.name, 'success');
                    await _loadPlaylists();
                };
            });
        };

        render();
        overlay.appendChild(modal);
        bodyEl.appendChild(overlay);
    }

    /* ── Queue ──────────────────────────────────────── */

    // Update rm-playing highlight inside the queue panel without full re-render
    function _refreshQueueHighlight() {
        if (!_queueContent || !_queueContent.isConnected) { _queueContent = null; return; }
        const items = _queueContent.querySelectorAll('.rm-queue-item');
        items.forEach((el, idx) => {
            const isCurrent = idx === _musicQueueIdx;
            el.className = 'rm-queue-item' + (isCurrent ? ' rm-playing' : '');
            const idxSpan = el.querySelector('.rm-queue-item-idx');
            if (idxSpan) idxSpan.innerHTML = isCurrent ? '<i class="fas fa-volume-up"></i>' : String(idx + 1);
        });
        const cur = _queueContent.querySelector('.rm-queue-item.rm-playing');
        if (cur) cur.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    function loadQueue(content) {
        _queueContent = content;
        if (!_musicQueue.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-list-ol"></i><p>' + t('Kolejka jest pusta') + '</p><p style="font-size:12px;margin-top:8px">' + t('Kliknij + przy utworze aby dodać do kolejki') + '</p></div>';
            return;
        }
        content.innerHTML = '<div class="rm-queue"><div class="rm-queue-title">' + t('Kolejka odtwarzania') + ' (' + _musicQueue.length + ')</div><div class="rm-queue-list" id="rm-queue-list"></div></div>';
        const list = content.querySelector('#rm-queue-list');
        _musicQueue.forEach((tr, idx) => {
            const isCurrent = idx === _musicQueueIdx;
            const el = document.createElement('div');
            el.className = 'rm-queue-item' + (isCurrent ? ' rm-playing' : '');
            el.innerHTML = `
                <span class="rm-queue-item-idx">${isCurrent ? '<i class="fas fa-volume-up"></i>' : (idx + 1)}</span>
                <span class="rm-queue-item-title">${escH(tr.title)}</span>
                <span class="rm-queue-item-dur">${escH(tr.duration_fmt || '')}</span>
                <button class="rm-queue-item-rm" title="${t('Usuń')}"><i class="fas fa-times"></i></button>`;
            el.onclick = (e) => {
                if (e.target.closest('.rm-queue-item-rm')) return;
                _musicQueueIdx = idx;
                playMusicTrack(tr);
                loadQueue(content);
            };
            el.querySelector('.rm-queue-item-rm').onclick = (e) => {
                e.stopPropagation();
                _musicQueue.splice(idx, 1);
                if (idx < _musicQueueIdx) _musicQueueIdx--;
                else if (idx === _musicQueueIdx) _musicQueueIdx = -1;
                loadQueue(content);
            };
            list.appendChild(el);
        });
    }

    /* ── Most Played ──────────────────────────────── */

    async function loadMostPlayed(content) {
        content.innerHTML = _skeletonTracks(5);
        const data = await api('/radio-music/most-played?limit=60');
        const allItems = data.items || [];
        // Music/local only for "Najczęściej grane"
        const items = allItems.filter(it => it.type === 'music' || it.type === 'local');
        if (!items.length && !allItems.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-fire"></i><p>' + t('Zacznij słuchać, aby zobaczyć najczęściej grane') + '</p></div>';
            return;
        }
        let html = '';
        if (items.length) {
            html += '<div class="rm-section-title"><i class="fas fa-fire"></i> ' + t('Najczęściej grane') + '</div>';
            html += '<div class="rm-hscroll">';
            items.slice(0, 30).forEach((item, i) => {
                const art = item.image || item.thumbnail || item.favicon || '';
                const title = item.name || item.title || '';
                if (!title) return;
                const artHtml = art
                    ? '<img src="' + escH(art) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">'
                    : '<i class="fas fa-music"></i>';
                const badge = item.play_count > 1 ? '<span class="rm-hcard-badge">' + item.play_count + '×</span>' : '';
                html += '<div class="rm-hcard" data-idx="' + i + '">'
                    + '<div class="rm-hcard-art">' + artHtml + badge + '</div>'
                    + '<div class="rm-hcard-title">' + escH(title) + '</div>'
                    + '<div class="rm-hcard-meta">' + escH(item.meta || item.channel || item.country || '') + '</div>'
                    + '</div>';
            });
            html += '</div>';
        }

        // "Ostatnio grane" — music/local only
        const allRecent = (await api('/radio-music/history')).items || [];
        const recent = allRecent.filter(it => (it.type === 'music' || it.type === 'local') && (it.name || it.title));
        if (recent.length) {
            html += '<div class="rm-section-title" style="margin-top:20px"><i class="fas fa-history"></i> ' + t('Ostatnio grane') + '</div>';
            html += '<div class="rm-hscroll">';
            recent.slice(0, 20).forEach((item, i) => {
                const art = item.image || item.thumbnail || item.favicon || '';
                const artHtml = art
                    ? '<img src="' + escH(art) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">'
                    : '<i class="fas fa-music"></i>';
                html += '<div class="rm-hcard rm-hcard-recent" data-ridx="' + i + '">'
                    + '<div class="rm-hcard-art">' + artHtml + '</div>'
                    + '<div class="rm-hcard-title">' + escH(item.name || item.title) + '</div>'
                    + '<div class="rm-hcard-meta">' + escH(item.meta || item.channel || item.country || '') + '</div>'
                    + '</div>';
            });
            html += '</div>';
        }

        // Recently played radios
        const recentRadios = allRecent.filter(it => it.type === 'radio' || (!it.type && it.uuid));
        if (recentRadios.length) {
            html += '<div class="rm-section-title" style="margin-top:20px"><i class="fas fa-broadcast-tower"></i> ' + t('Ostatnio słuchane radia') + '</div>';
            html += '<div class="rm-hscroll">';
            recentRadios.slice(0, 20).forEach((item, i) => {
                const artHtml = _stationIconHtml(item);
                html += '<div class="rm-hcard rm-hcard-radio" data-radidx="' + i + '">'
                    + '<div class="rm-hcard-art">' + artHtml + '</div>'
                    + '<div class="rm-hcard-title">' + escH(item.name) + '</div>'
                    + '<div class="rm-hcard-meta">' + escH(item.country || item.meta || '') + '</div>'
                    + '</div>';
            });
            html += '</div>';
        }

        // Recently played podcasts
        const recentPods = allRecent.filter(it => it.type === 'podcast');
        if (recentPods.length) {
            html += '<div class="rm-section-title" style="margin-top:20px"><i class="fas fa-podcast"></i> ' + t('Ostatnie podcasty') + '</div>';
            html += '<div class="rm-hscroll">';
            recentPods.slice(0, 20).forEach((item, i) => {
                const artHtml = (item.image || item.favicon)
                    ? '<img src="' + escH(item.image || item.favicon) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-podcast\\\'></i>\'">'
                    : '<i class="fas fa-podcast"></i>';
                html += '<div class="rm-hcard rm-hcard-pod" data-podidx="' + i + '">'
                    + '<div class="rm-hcard-art">' + artHtml + '</div>'
                    + '<div class="rm-hcard-title">' + escH(item.name) + '</div>'
                    + '<div class="rm-hcard-meta">' + escH(item.meta || '') + '</div>'
                    + '</div>';
            });
            html += '</div>';
        }

        // Playlists
        await _loadPlaylists();
        if (_playlists.length) {
            html += '<div class="rm-section-title" style="margin-top:20px"><i class="fas fa-list"></i> ' + t('Playlisty') + '</div>';
            html += '<div class="rm-hscroll">';
            _playlists.forEach((pl, i) => {
                const coverHtml = _playlistCoverHtml(pl, 120);
                html += '<div class="rm-hcard rm-hcard-pl" data-plidx="' + i + '">'
                    + '<div class="rm-hcard-art">' + coverHtml + '</div>'
                    + '<div class="rm-hcard-title">' + escH(pl.name) + '</div>'
                    + '<div class="rm-hcard-meta">' + pl.tracks.length + ' ' + t('utworów') + '</div>'
                    + '</div>';
            });
            html += '</div>';
        }

        content.innerHTML = html || '<div class="rm-empty"><i class="fas fa-fire"></i><p>' + t('Zacznij słuchać, aby zobaczyć najczęściej grane') + '</p></div>';

        // Wire up clicks — pass full list so next/prev navigates the list
        content.querySelectorAll('.rm-hcard[data-idx]').forEach(card => {
            const idx = parseInt(card.dataset.idx);
            card.onclick = () => _playHistoryItem(items[idx], items, idx);
        });
        content.querySelectorAll('.rm-hcard-recent[data-ridx]').forEach(card => {
            const idx = parseInt(card.dataset.ridx);
            card.onclick = () => _playHistoryItem(recent[idx], recent, idx);
        });
        content.querySelectorAll('.rm-hcard-radio[data-radidx]').forEach(card => {
            const idx = parseInt(card.dataset.radidx);
            card.onclick = () => _playHistoryItem(recentRadios[idx], recentRadios, idx);
        });
        content.querySelectorAll('.rm-hcard-pod[data-podidx]').forEach(card => {
            const idx = parseInt(card.dataset.podidx);
            card.onclick = async () => {
                const item = recentPods[idx];
                const podName = item.meta || item.name || '';
                // Switch to podcasts section
                bodyEl.querySelectorAll('.rm-sidebar-item, .rm-mnav-btn').forEach(b => b.classList.remove('active'));
                activeSection = 'podcasts';
                loadSection('podcasts');
                // Try subscriptions first (has feed_url)
                const sub = _subscriptions.find(s => s.name === podName || s.title === podName);
                if (sub && sub.feed_url) {
                    openPodcast(sub);
                    return;
                }
                // Search iTunes for the podcast
                const res = await api('/radio-music/podcasts/search?q=' + encodeURIComponent(podName));
                const found = (res.items || [])[0];
                if (found && found.feed_url) {
                    openPodcast(found);
                } else {
                    toast(t('Nie znaleziono podcastu'), 'error');
                }
            };
        });
        content.querySelectorAll('.rm-hcard-pl[data-plidx]').forEach(card => {
            const idx = parseInt(card.dataset.plidx);
            card.onclick = () => {
                const pl = _playlists[idx];
                if (pl && pl.tracks.length) {
                    _musicQueue = pl.tracks.map(t => ({...t, _plItem: true}));
                    _musicQueueIdx = 0;
                    _playTrackFromPlaylist(pl.tracks[0]);
                    toast(t('Odtwarzam: ') + pl.name, 'success');
                } else {
                    // Open playlists section to show this playlist
                    bodyEl.querySelectorAll('.rm-sidebar-item, .rm-mnav-btn').forEach(b => b.classList.remove('active'));
                    activeSection = 'playlists';
                    loadSection('playlists');
                }
            };
        });
    }

    /* ── History ────────────────────────────────────── */

    async function loadHistory(content) {
        const data = await api('/radio-music/history');
        const items = data.items || [];
        if (!items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-history"></i><p>' + t('Brak historii odtwarzania') + '</p><p style="font-size:12px;margin-top:4px">' + t('Zacznij słuchać — historia pojawi się tutaj') + '</p></div>';
            return;
        }
        content.innerHTML = '<div class="rm-grid"></div>';
        const grid = content.querySelector('.rm-grid');
        items.forEach((item, idx) => {
            const card = document.createElement('div');
            card.className = 'rm-card';
            const icon = item.type === 'podcast' ? 'fa-podcast' : 'fa-broadcast-tower';
            card.innerHTML = `
                <div class="rm-card-icon">${(item.type === 'radio' || (!item.type && item.uuid)) ? _stationIconHtml(item) : (item.image || item.favicon ? '<img src="' + escH(item.image || item.favicon) + '">' : '<i class="fas ' + icon + '"></i>')}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(item.name)}</div>
                    <div class="rm-card-meta">${escH(item.meta || item.country || '')}</div>
                </div>`;
            card.onclick = () => _playHistoryItem(item, items, idx);
            grid.appendChild(card);
        });
    }

    /* ── Discovery (Personalized) ─────────────────── */

    async function loadDiscovery(toolbar, content) {
        const COUNTRY_MAP = [
            {code:'PL',name:'Polska'},{code:'US',name:'USA'},{code:'GB',name:'UK'},
            {code:'DE',name:'Niemcy'},{code:'FR',name:'Francja'},{code:'ES',name:'Hiszpania'},
            {code:'IT',name:'Włochy'},{code:'BR',name:'Brazylia'},{code:'SE',name:'Szwecja'},
            {code:'NL',name:'Holandia'},{code:'CZ',name:'Czechy'},{code:'UA',name:'Ukraina'},
            {code:'JP',name:'Japonia'},{code:'AU',name:'Australia'},{code:'CA',name:'Kanada'},
        ];

        let country = _detectCountry();
        if (!COUNTRY_MAP.find(c => c.code === country)) country = 'PL';
        const countryName = COUNTRY_MAP.find(c => c.code === country)?.name || country;

        toolbar.innerHTML = '';
        content.innerHTML = '<div style="padding:40px;text-align:center;color:rgba(255,255,255,.4)"><i class="fas fa-compass fa-spin"></i> ' + t('Ładowanie odkryć…') + '</div>';

        // Fetch personalized data + generic fallbacks in parallel
        const [recoData, radioData, podData] = await Promise.allSettled([
            api('/radio-music/recommendations?country=' + country),
            api('/radio-music/radio/search?country=' + country + '&limit=12&order=clickcount&reverse=true'),
            api('/radio-music/podcasts/top?country=' + country.toLowerCase() + '&limit=8'),
        ]);
        if (!content?.isConnected) return;

        const reco = recoData.status === 'fulfilled' ? recoData.value : {};
        const stations = (radioData.status === 'fulfilled' ? (radioData.value?.items || []) : []).slice(0, 8);
        const pods = (podData.status === 'fulfilled' ? (podData.value?.items || []) : []).slice(0, 6);
        const hasPersonal = reco.has_data;

        content.innerHTML = '';

        // ── Hero banner ──
        const hero = document.createElement('div');
        hero.className = 'rm-disc-hero';
        const greetHour = new Date().getHours();
        const greeting = greetHour < 6 ? t('Dobrej nocy') : greetHour < 12 ? t('Dzień dobry') : greetHour < 18 ? t('Cześć') : t('Dobry wieczór');
        const subtitle = hasPersonal ? t('Oto co dla Ciebie mamy') : t('Odkryj muzykę i radio z') + ' ' + escH(countryName);
        hero.innerHTML = `
            <div class="rm-disc-hero-bg"></div>
            <div class="rm-disc-hero-content">
                <h2>${greeting} 🎵</h2>
                <p>${subtitle}</p>
                <div class="rm-disc-hero-chips" id="rm-disc-hero-chips"></div>
            </div>`;
        content.appendChild(hero);

        // Hero chips: personalized tags or generic genres
        const heroChips = hero.querySelector('#rm-disc-hero-chips');
        const chipSources = (reco.top_tags || []).length
            ? (reco.top_tags || []).slice(0, 6).map(tag => ({ label: tag.charAt(0).toUpperCase() + tag.slice(1), q: tag, icon: 'fa-heart', isTag: true }))
            : [
                { label: 'Pop', q: 'pop hits ' + (new Date().getFullYear()), icon: 'fa-star' },
                { label: 'Rock', q: 'rock classics best', icon: 'fa-guitar' },
                { label: 'Chill', q: 'lofi chill beats relax', icon: 'fa-cloud-moon' },
                { label: 'Hip-Hop', q: 'hip hop rap new', icon: 'fa-microphone-alt' },
                { label: 'Electronic', q: 'electronic dance EDM', icon: 'fa-bolt' },
                { label: 'Jazz', q: 'jazz smooth saxophone', icon: 'fa-wine-glass-alt' },
            ];
        chipSources.forEach(g => {
            const chip = document.createElement('span');
            chip.className = 'rm-disc-hero-chip';
            chip.innerHTML = `<i class="fas ${g.icon}"></i> ${escH(g.label)}`;
            chip.onclick = async () => {
                chip.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + escH(g.label);
                if (g.isTag) {
                    // Play radio stations with this tag
                    const d = await api('/radio-music/radio/search?tag=' + encodeURIComponent(g.q) + '&limit=30');
                    const stns = d.items || [];
                    if (stns.length) {
                        playStation(stns[Math.floor(Math.random() * Math.min(stns.length, 10))]);
                        _recentStations = stns;
                        toast(g.label + ' — ' + t('Odtwarzam radio'), 'success');
                    } else { toast(t('Brak wyników'), 'info'); }
                } else {
                    const d = await api('/radio-music/music/search?q=' + encodeURIComponent(g.q) + '&limit=20');
                    const tracks = d.items || [];
                    if (tracks.length) {
                        _musicQueue = tracks; _musicQueueIdx = 0;
                        playAudio(tracks[0]);
                        toast(g.label + ' — ' + t('Odtwarzam') + ' ' + tracks.length + ' ' + t('utworów'), 'success');
                    } else { toast(t('Brak wyników'), 'info'); }
                }
                chip.innerHTML = `<i class="fas ${g.icon}"></i> ${escH(g.label)}`;
            };
            heroChips.appendChild(chip);
        });

        // ── Helper: build a discovery card ──
        function _discCard(opts) {
            const card = document.createElement('div');
            card.className = 'rm-disc-card';
            const artHtml = opts.artSrc
                ? `<img src="${escH(opts.artSrc)}" loading="lazy" onerror="this.outerHTML='<i class=\\'fas ${opts.fallbackIcon || 'fa-music'}\\'></i>'">`
                : (opts.stationHtml || `<i class="fas ${opts.fallbackIcon || 'fa-music'}"></i>`);
            card.innerHTML = `<div class="rm-disc-card-art">${artHtml}<div class="rm-disc-play-overlay"><i class="fas fa-play"></i></div>`
                + (opts.badge ? `<span class="rm-disc-badge ${opts.badgeClass || ''}">${opts.badge}</span>` : '') + `</div>`
                + `<div class="rm-disc-card-body"><div class="rm-disc-card-title">${escH(opts.title || '')}</div>`
                + `<div class="rm-disc-card-meta">${escH(opts.meta || '')}</div></div>`;
            if (opts.onclick) card.onclick = opts.onclick;
            return card;
        }

        // ── PERSONALIZED SECTIONS ──

        // 1. "Bo lubisz [tag]" — radio stations from user's favorite tags
        const tagRadios = reco.tag_radios || {};
        const tagNames = Object.keys(tagRadios).filter(t => tagRadios[t].length > 0);
        tagNames.forEach(tag => {
            const tagStations = tagRadios[tag];
            const sec = document.createElement('div');
            sec.className = 'rm-disc-section';
            const tagLabel = tag.charAt(0).toUpperCase() + tag.slice(1);
            sec.innerHTML = `<div class="rm-disc-title"><i class="fas fa-heart"></i> ${t('Bo lubisz')} ${escH(tagLabel)}</div>`;
            const carousel = document.createElement('div');
            carousel.className = 'rm-disc-carousel';
            tagStations.forEach(s => {
                carousel.appendChild(_discCard({
                    stationHtml: _stationIconHtml(s),
                    fallbackIcon: 'fa-broadcast-tower',
                    badge: 'LIVE', badgeClass: 'rm-disc-badge-radio',
                    title: s.name,
                    meta: (s.tags || '').split(',')[0] || s.country || 'Radio',
                    onclick: () => playStation(s)
                }));
            });
            sec.appendChild(carousel);
            content.appendChild(sec);
        });

        // 2. "Podobni do [artist]" — Deezer-based artist recommendations
        const artistRecs = reco.artist_recs || [];
        if (artistRecs.length) {
            // Group by "because" artist
            const grouped = {};
            artistRecs.forEach(a => {
                if (!grouped[a.because]) grouped[a.because] = [];
                grouped[a.because].push(a);
            });
            Object.entries(grouped).forEach(([because, artists]) => {
                const sec = document.createElement('div');
                sec.className = 'rm-disc-section';
                sec.innerHTML = `<div class="rm-disc-title"><i class="fas fa-wand-magic-sparkles"></i> ${t('Podobni do')} ${escH(because)}</div>`;
                const carousel = document.createElement('div');
                carousel.className = 'rm-disc-carousel';
                artists.forEach(a => {
                    carousel.appendChild(_discCard({
                        artSrc: a.picture,
                        fallbackIcon: 'fa-user-circle',
                        badge: '✦', badgeClass: 'rm-disc-badge-rec',
                        title: a.name,
                        meta: t('Artysta'),
                        onclick: async () => {
                            toast(t('Szukam muzyki') + ': ' + a.name, 'info');
                            const d = await api('/radio-music/music/search?q=' + encodeURIComponent(a.name) + '&limit=20');
                            const tracks = d.items || [];
                            if (tracks.length) {
                                _musicQueue = tracks; _musicQueueIdx = 0;
                                playAudio(tracks[0]);
                            } else { toast(t('Brak wyników'), 'info'); }
                        }
                    }));
                });
                sec.appendChild(carousel);
                content.appendChild(sec);
            });
        }

        // 3. "Teraz grane — podobne" — if something is playing, show similar
        if (_playing && (_playing.type === 'music' || _playing.type === 'local')) {
            const artist = (_playing.meta || _playing.channel || '').trim();
            if (artist) {
                const simSec = document.createElement('div');
                simSec.className = 'rm-disc-section';
                simSec.innerHTML = `<div class="rm-disc-title"><i class="fas fa-headphones"></i> ${t('Podobni do')} ${escH(artist)}</div>`
                    + `<div class="rm-disc-carousel" id="rm-disc-now-similar"><div class="rm-disc-empty"><i class="fas fa-spinner fa-spin"></i></div></div>`;
                content.appendChild(simSec);

                api('/radio-music/similar-artists?artist=' + encodeURIComponent(artist) + '&limit=6').then(d => {
                    const carousel = simSec.querySelector('#rm-disc-now-similar');
                    if (!carousel?.isConnected) return;
                    const items = d.items || [];
                    if (!items.length) { simSec.remove(); return; }
                    carousel.innerHTML = '';
                    items.forEach(a => {
                        carousel.appendChild(_discCard({
                            artSrc: a.picture,
                            fallbackIcon: 'fa-user-circle',
                            badge: '✦', badgeClass: 'rm-disc-badge-rec',
                            title: a.name,
                            meta: a.fans ? (a.fans > 1000 ? Math.round(a.fans/1000) + 'k ' + t('fanów') : a.fans + ' ' + t('fanów')) : t('Artysta'),
                            onclick: async () => {
                                toast(t('Szukam muzyki') + ': ' + a.name, 'info');
                                const d2 = await api('/radio-music/music/search?q=' + encodeURIComponent(a.name) + '&limit=20');
                                const tracks = d2.items || [];
                                if (tracks.length) { _musicQueue = tracks; _musicQueueIdx = 0; playAudio(tracks[0]); }
                                else { toast(t('Brak wyników'), 'info'); }
                            }
                        }));
                    });
                }).catch(() => simSec.remove());
            }
        }

        // ── GENERIC SECTIONS (always shown, below personalized) ──

        // 4. Top Radio
        if (stations.length) {
            const sec = document.createElement('div');
            sec.className = 'rm-disc-section';
            sec.innerHTML = `<div class="rm-disc-title"><i class="fas fa-broadcast-tower"></i> ${t('Top Radio')} — ${escH(countryName)}<span class="rm-disc-seeall" id="rm-disc-radio-all">${t('Zobacz więcej')} →</span></div>`;
            const carousel = document.createElement('div');
            carousel.className = 'rm-disc-carousel';
            stations.forEach(s => {
                carousel.appendChild(_discCard({
                    stationHtml: _stationIconHtml(s),
                    fallbackIcon: 'fa-broadcast-tower',
                    badge: 'LIVE', badgeClass: 'rm-disc-badge-radio',
                    title: s.name,
                    meta: (s.tags || '').split(',')[0] || 'Radio',
                    onclick: () => playStation(s)
                }));
            });
            sec.appendChild(carousel);
            content.appendChild(sec);
            sec.querySelector('#rm-disc-radio-all')?.addEventListener('click', () => _navTo('radio'));
        }

        // 5. Top Podcasts — with actual openPodcast on click
        if (pods.length) {
            const sec = document.createElement('div');
            sec.className = 'rm-disc-section';
            sec.innerHTML = `<div class="rm-disc-title"><i class="fas fa-podcast"></i> ${t('Top Podcasty')}<span class="rm-disc-seeall" id="rm-disc-pod-all">${t('Zobacz więcej')} →</span></div>`;
            const carousel = document.createElement('div');
            carousel.className = 'rm-disc-carousel';
            pods.forEach(p => {
                carousel.appendChild(_discCard({
                    artSrc: p.artwork || p.artwork_url,
                    fallbackIcon: 'fa-podcast',
                    badge: 'POD', badgeClass: 'rm-disc-badge-pod',
                    title: p.name || p.title || '',
                    meta: p.artist || p.artist_name || p.author || '',
                    onclick: async () => {
                        // Lookup feed URL via Apple ID, then open podcast
                        if (p.feed_url) { openPodcast(p); return; }
                        if (p.id) {
                            toast(t('Ładowanie podcastu…'), 'info');
                            const lookup = await api('/radio-music/podcasts/lookup?id=' + p.id);
                            if (lookup.feed_url) {
                                openPodcast({ ...p, feed_url: lookup.feed_url, artwork: lookup.artwork || p.artwork });
                            } else { toast(t('Nie znaleziono feedu'), 'error'); }
                        } else { _navTo('podcasts'); }
                    }
                }));
            });
            sec.appendChild(carousel);
            content.appendChild(sec);
            sec.querySelector('#rm-disc-pod-all')?.addEventListener('click', () => _navTo('podcasts'));
        }

        // 6. Trending Music (lazy)
        const countryGenreMap = { PL:'polish music polskie', DE:'german music deutsch', FR:'french music chanson', ES:'spanish music pop espanol', IT:'italian music', BR:'brazilian music MPB', JP:'japanese pop music J-pop', SE:'swedish pop', NL:'dutch music', CZ:'czech music', UA:'ukrainian music', AU:'australian music', CA:'canadian indie' };
        const musicQ = countryGenreMap[country] || 'top hits ' + (new Date().getFullYear());

        const musicSec = document.createElement('div');
        musicSec.className = 'rm-disc-section';
        musicSec.innerHTML = `<div class="rm-disc-title"><i class="fas fa-fire"></i> ${t('Trending Music')}<span class="rm-disc-seeall" id="rm-disc-music-all">${t('Szukaj muzyki')} →</span></div>`
            + `<div class="rm-disc-carousel" id="rm-disc-music-carousel"><div class="rm-disc-empty"><i class="fas fa-spinner fa-spin"></i></div></div>`;
        content.appendChild(musicSec);
        musicSec.querySelector('#rm-disc-music-all')?.addEventListener('click', () => _navTo('music'));

        api('/radio-music/music/search?q=' + encodeURIComponent(musicQ) + '&limit=10').then(d => {
            const carousel = musicSec.querySelector('#rm-disc-music-carousel');
            if (!carousel?.isConnected) return;
            const tracks = (d.items || []).slice(0, 8);
            if (!tracks.length) { carousel.innerHTML = '<div class="rm-disc-empty">' + t('Brak wyników') + '</div>'; return; }
            carousel.innerHTML = '';
            tracks.forEach(tr => {
                carousel.appendChild(_discCard({
                    artSrc: tr.image || tr.thumbnail,
                    fallbackIcon: 'fa-music',
                    badge: '♪', badgeClass: 'rm-disc-badge-music',
                    title: tr.name || tr.title || '',
                    meta: tr.artist || tr.meta || '',
                    onclick: () => playAudio(tr)
                }));
            });
        }).catch(() => {});

        if (!stations.length && !pods.length && !hasPersonal) {
            content.innerHTML += '<div class="rm-disc-empty" style="padding:60px 20px"><i class="fas fa-compass" style="font-size:40px;margin-bottom:16px;display:block"></i>'
                + t('Brak danych dla wybranego kraju.') + '<br><br>'
                + '<button class="rm-chip" onclick="this.closest(\'.rm-content\').innerHTML=\'\'">' + t('Spróbuj inny kraj') + '</button></div>';
        }
    }

    /* ── Podcast Episode Queue ─────────────────────────── */

    function loadPodQueue(content) {
        content.innerHTML = '';
        const title = document.createElement('div');
        title.className = 'rm-section-title';
        title.innerHTML = '<i class="fas fa-list-ul"></i> ' + t('Kolejka odcinków');
        content.appendChild(title);

        if (!_podQueue.length) {
            content.innerHTML += '<div class="rm-empty"><i class="fas fa-podcast"></i> ' + t('Kolejka jest pusta') + '</div>';
            return;
        }

        _podQueue.forEach((ep, idx) => {
            const row = document.createElement('div');
            row.className = 'rm-queue-item' + (idx === _podQueueIdx ? ' rm-queue-active' : '');
            row.innerHTML = `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escH(ep.name||ep.title||'')}</span>`
                + `<button class="rm-queue-item-rm" title="${t('Usuń')}"><i class="fas fa-times"></i></button>`;
            row.onclick = () => { _podQueueIdx = idx; playAudio(ep); loadPodQueue(content); };
            row.querySelector('.rm-queue-item-rm').onclick = (e) => {
                e.stopPropagation();
                _podQueue.splice(idx, 1);
                if (_podQueueIdx >= idx && _podQueueIdx > 0) _podQueueIdx--;
                loadPodQueue(content);
            };
            content.appendChild(row);
        });

        const clearBtn = document.createElement('button');
        clearBtn.className = 'rm-chip';
        clearBtn.style.margin = '16px 0';
        clearBtn.innerHTML = '<i class="fas fa-trash"></i> ' + t('Wyczyść kolejkę');
        clearBtn.onclick = () => { _podQueue = []; _podQueueIdx = -1; loadPodQueue(content); };
        content.appendChild(clearBtn);
    }

    /* ── Recently Added (Local) ────────────────────── */

    async function loadRecentlyAdded(content) {
        content.innerHTML = _skeletonTracks(5);
        const scanData = await api('/radio-music/local/scan');
        const items = (scanData.items || []).slice(); // copy before sorting
        if (!items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-clock"></i><p>'
                + t('Brak plików audio') + '</p><p style="font-size:12px;color:rgba(255,255,255,.4)">'
                + t('Dodaj foldery w sekcji Lokalna muzyka') + '</p>'
                + '<button class="rm-chip" style="margin-top:12px" id="rm-ra-go-local"><i class="fas fa-folder-open"></i> ' + t('Lokalna muzyka') + '</button></div>';
            content.querySelector('#rm-ra-go-local')?.addEventListener('click', () => _navTo('local'));
            return;
        }
        items.sort((a, b) => (b.modified || 0) - (a.modified || 0));
        const recent = items.slice(0, 50);
        let html = '<div class="rm-section-title"><i class="fas fa-clock"></i> ' + t('Ostatnio dodane') + '</div>';
        recent.forEach(file => {
            const artUrl = file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '';
            const durStr = file.duration ? _fmtSecs(file.duration) : '';
            const metaParts = [];
            if (file.artist) metaParts.push(file.artist);
            if (file.album) metaParts.push(file.album);
            const meta = metaParts.join(' · ') || file.filename;
            html += '<div class="rm-track rm-local-track" data-path="' + escH(file.path) + '">'
                + (artUrl ? '<img class="rm-track-thumb" src="' + escH(artUrl) + '" onerror="this.style.display=\'none\'">' : '<div class="rm-track-thumb" style="background:rgba(255,255,255,.06);display:flex;align-items:center;justify-content:center"><i class="fas fa-music" style="color:rgba(255,255,255,.2)"></i></div>')
                + '<div class="rm-track-info"><div class="rm-track-name">' + escH(file.name) + '</div>'
                + '<div class="rm-track-meta">' + escH(meta) + (durStr ? ' · ' + durStr : '') + '</div></div>'
                + '<button class="rm-track-btn rm-track-play"><i class="fas fa-play"></i></button>'
                + '</div>';
        });
        content.innerHTML = html;
        content.querySelectorAll('.rm-track.rm-local-track').forEach(el => {
            const path = el.dataset.path;
            const file = recent.find(f => f.path === path);
            if (!file) return;
            const playItem = {
                name: file.name, path: file.path, type: 'local',
                image: file.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || '') : '',
                meta: file.artist || file.filename, folder: file.folder
            };
            el.querySelector('.rm-track-play').onclick = (e) => { e.stopPropagation(); playAudio(playItem); };
            el.onclick = () => playAudio(playItem);
        });
    }

    /* ── Settings ───────────────────────────────────── */

    function loadSettings(content) {
        const cfSec = Math.round(_crossfadeDuration / 1000);
        let html = `
            <div class="rm-section-title"><i class="fas fa-cog"></i> ${t('Ustawienia')}</div>
            <div style="max-width:480px;display:flex;flex-direction:column;gap:24px;padding:8px 0">
                <div>
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                        <span style="font-size:14px;color:rgba(255,255,255,.85)">${t('Przenikanie (crossfade)')}</span>
                        <span id="rm-cf-val" style="font-size:13px;color:var(--rm-accent);min-width:28px;text-align:right">${cfSec}s</span>
                    </div>
                    <input type="range" id="rm-cf-slider" class="rm-crossfade-slider" min="0" max="12" step="1" value="${cfSec}">
                    <div style="display:flex;justify-content:space-between;font-size:11px;color:rgba(255,255,255,.35);margin-top:4px">
                        <span>${t('Wył.')}</span><span>12s</span>
                    </div>
                </div>
                <div>
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                        <span style="font-size:14px;color:rgba(255,255,255,.85)">${t('Prędkość odtwarzania')}</span>
                        <span style="font-size:13px;color:rgba(255,255,255,.5)">${(_audio?.playbackRate || 1).toFixed(1)}x</span>
                    </div>
                    <div style="font-size:12px;color:rgba(255,255,255,.4)">${t('Zmień przyciskiem prędkości w panelu odtwarzacza')}</div>
                </div>
                <div>
                    <button class="rm-chip" id="rm-clear-ep-progress" style="margin-top:8px"><i class="fas fa-trash"></i> ${t('Wyczyść postępy podcastów')}</button>
                </div>
            </div>`;
        // Equalizer section
        html += _renderEqSection();
        content.innerHTML = html;
        content.querySelector('#rm-cf-slider').oninput = (e) => {
            const sec = parseInt(e.target.value, 10);
            content.querySelector('#rm-cf-val').textContent = sec + 's';
            _saveCrossfadeSetting(sec * 1000);
        };
        content.querySelector('#rm-clear-ep-progress').onclick = () => {
            _epProgress = {};
            _saveEpProgress();
            toast(t('Postępy podcastów wyczyszczone'), 'success');
        };
        _wireEqHandlers(content);
    }

    /* ── Unified Search ────────────────────────────── */

    async function loadUnifiedSearch(toolbar, content) {
        toolbar.innerHTML = `<input class="rm-search" id="rm-usearch" placeholder="${t('Szukaj wszędzie...')}" autofocus>`;
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-search"></i><p>' + t('Wpisz, aby szukać w radiu, podcastach i lokalnej muzyce') + '</p></div>';
        const inp = toolbar.querySelector('#rm-usearch');
        let debounce;
        inp.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                const q = inp.value.trim();
                if (q.length >= 2) _doUnifiedSearch(q, content);
            }, 400);
        };
    }

    async function _doUnifiedSearch(q, content) {
        content.innerHTML = _skeletonTracks(6);
        const data = await api('/radio-music/search/all?q=' + encodeURIComponent(q) + '&limit=8');
        if (data.error) { content.innerHTML = '<div class="rm-empty"><p>' + escH(data.error) + '</p></div>'; return; }
        let html = '';
        // Radio results
        if (data.radio && data.radio.length) {
            html += '<div class="rm-section-title"><i class="fas fa-broadcast-tower"></i> ' + t('Radio') + ' (' + data.radio.length + ')</div>';
            html += '<div class="rm-grid rm-usearch-radio">';
            data.radio.forEach(s => {
                const isFav = _favorites.some(f => f.uuid === s.uuid);
                html += `<div class="rm-card rm-usearch-item" data-type="radio" data-uuid="${escH(s.uuid||'')}">
                    <div class="rm-card-icon">${_stationIconHtml(s)}</div>
                    <div class="rm-card-info"><div class="rm-card-name">${escH(s.name)}</div>
                    <div class="rm-card-meta">${escH([s.country, s.tags].filter(Boolean).join(' · '))}</div></div>
                    <div class="rm-card-actions"><button class="rm-card-btn rm-fav-btn ${isFav?'rm-fav-active':''}" title="${t('Ulubione')}"><i class="fas fa-heart"></i></button></div>
                </div>`;
            });
            html += '</div>';
        }
        // Podcast results
        if (data.podcasts && data.podcasts.length) {
            html += '<div class="rm-section-title"><i class="fas fa-podcast"></i> ' + t('Podcasty') + ' (' + data.podcasts.length + ')</div>';
            html += '<div class="rm-grid rm-usearch-podcasts">';
            data.podcasts.forEach(p => {
                html += `<div class="rm-card rm-usearch-item" data-type="podcast" data-feed="${escH(p.feed_url||'')}">
                    <div class="rm-card-icon">${p.artwork ? '<img src="' + escH(p.artwork) + '">' : '<i class="fas fa-podcast"></i>'}</div>
                    <div class="rm-card-info"><div class="rm-card-name">${escH(p.name)}</div>
                    <div class="rm-card-meta">${escH(p.artist||'')}</div></div>
                </div>`;
            });
            html += '</div>';
        }
        // Local results
        if (data.local && data.local.length) {
            html += '<div class="rm-section-title"><i class="fas fa-folder-open"></i> ' + t('Lokalna muzyka') + ' (' + data.local.length + ')</div>';
            data.local.forEach(f => {
                const artUrl = f.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(f.path) + '&token=' + (NAS.token || '') : '';
                const meta = [f.artist, f.album].filter(Boolean).join(' · ') || f.filename;
                html += `<div class="rm-track rm-usearch-item rm-local-track" data-type="local" data-path="${escH(f.path)}">
                    ${artUrl ? '<img class="rm-track-thumb" src="' + escH(artUrl) + '" loading="lazy">' : '<div class="rm-track-thumb" style="background:rgba(255,255,255,.06);display:flex;align-items:center;justify-content:center"><i class="fas fa-music" style="color:rgba(255,255,255,.2)"></i></div>'}
                    <div class="rm-track-info"><div class="rm-track-name">${escH(f.name)}</div>
                    <div class="rm-track-meta">${escH(meta)}</div></div>
                    <button class="rm-track-btn rm-track-play"><i class="fas fa-play"></i></button>
                </div>`;
            });
        }
        if (!html) {
            html = '<div class="rm-empty"><i class="fas fa-search"></i><p>' + t('Brak wyników') + '</p></div>';
        }
        content.innerHTML = html;

        // Wire up click handlers
        content.querySelectorAll('.rm-usearch-item').forEach(el => {
            const type = el.dataset.type;
            if (type === 'radio') {
                const s = data.radio.find(r => r.uuid === el.dataset.uuid);
                if (s) {
                    el.onclick = (e) => { if (!e.target.closest('.rm-fav-btn')) playStation(s); };
                    const favBtn = el.querySelector('.rm-fav-btn');
                    if (favBtn) favBtn.onclick = (e) => { e.stopPropagation(); toggleFavorite(s); };
                }
            } else if (type === 'podcast') {
                const p = data.podcasts.find(r => r.feed_url === el.dataset.feed);
                if (p) el.onclick = () => openPodcast(p);
            } else if (type === 'local') {
                const f = data.local.find(r => r.path === el.dataset.path);
                if (f) {
                    const item = {
                        name: f.name, path: f.path, type: 'local',
                        image: f.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(f.path) + '&token=' + (NAS.token || '') : '',
                        meta: f.artist || f.filename, folder: f.folder
                    };
                    el.onclick = () => playAudio(item);
                    const playBtn = el.querySelector('.rm-track-play');
                    if (playBtn) playBtn.onclick = (e) => { e.stopPropagation(); playAudio(item); };
                }
            }
        });
    }

    /* ── Artist / Album Browser ────────────────────── */

    async function loadArtists(content) {
        content.innerHTML = _skeletonGrid(8);
        const scanData = await api('/radio-music/local/scan');
        const items = scanData.items || [];
        if (!items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-user-circle"></i><p>'
                + t('Brak plików audio') + '</p><button class="rm-chip" style="margin-top:12px" id="rm-art-go-local"><i class="fas fa-folder-open"></i> '
                + t('Dodaj foldery') + '</button></div>';
            content.querySelector('#rm-art-go-local')?.addEventListener('click', () => _navTo('local'));
            return;
        }
        // Group by artist
        const artistMap = {};
        items.forEach(f => {
            const artist = (f.artist || '').trim() || t('Nieznany artysta');
            if (!artistMap[artist]) artistMap[artist] = [];
            artistMap[artist].push(f);
        });
        const artists = Object.keys(artistMap).sort((a, b) => a.localeCompare(b, undefined, {sensitivity: 'base'}));
        let html = '<div class="rm-section-title"><i class="fas fa-user-circle"></i> ' + t('Artyści') + ' (' + artists.length + ')</div>';
        html += '<div class="rm-grid">';
        artists.forEach(artist => {
            const tracks = artistMap[artist];
            const albums = new Set(tracks.map(t => (t.album || '').trim()).filter(Boolean));
            const artTrack = tracks.find(t => t.has_art);
            const artUrl = artTrack ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(artTrack.path) + '&token=' + (NAS.token || '') : '';
            html += `<div class="rm-card rm-artist-card" data-artist="${escH(artist)}">
                <div class="rm-card-icon">${artUrl ? '<img src="' + escH(artUrl) + '" loading="lazy">' : '<i class="fas fa-user-circle"></i>'}</div>
                <div class="rm-card-info">
                    <div class="rm-card-name">${escH(artist)}</div>
                    <div class="rm-card-meta">${tracks.length} ${t('utworów')}${albums.size ? ' · ' + albums.size + ' ' + t('albumów') : ''}</div>
                </div>
            </div>`;
        });
        html += '</div>';
        content.innerHTML = html;
        content.querySelectorAll('.rm-artist-card').forEach(el => {
            el.onclick = () => _openArtist(el.dataset.artist, artistMap[el.dataset.artist], content);
        });
    }

    function _openArtist(artistName, tracks, content) {
        // Group by album
        const albumMap = {};
        tracks.forEach(f => {
            const album = (f.album || '').trim() || t('Bez albumu');
            if (!albumMap[album]) albumMap[album] = [];
            albumMap[album].push(f);
        });
        // Sort albums, then tracks within each album by track number
        const albumNames = Object.keys(albumMap).sort((a, b) => a.localeCompare(b, undefined, {sensitivity: 'base'}));
        let html = '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">'
            + '<button class="rm-chip" id="rm-art-back"><i class="fas fa-arrow-left"></i> ' + t('Artyści') + '</button>'
            + '<span style="font-size:18px;font-weight:700">' + escH(artistName) + '</span>'
            + '<button class="rm-chip rm-artist-playall" style="margin-left:auto"><i class="fas fa-play"></i> ' + t('Odtwórz wszystko') + '</button>'
            + '</div>';
        albumNames.forEach(album => {
            let albumTracks = albumMap[album];
            albumTracks.sort((a, b) => {
                const ta = parseInt(a.track) || 999;
                const tb = parseInt(b.track) || 999;
                return ta - tb;
            });
            const artTrack = albumTracks.find(t => t.has_art);
            const artUrl = artTrack ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(artTrack.path) + '&token=' + (NAS.token || '') : '';
            const yearStr = albumTracks[0].year ? ' · ' + albumTracks[0].year : '';
            html += '<div class="rm-section-title" style="display:flex;align-items:center;gap:8px">'
                + (artUrl ? '<img src="' + escH(artUrl) + '" style="width:32px;height:32px;border-radius:4px;object-fit:cover" loading="lazy">' : '')
                + '<span>' + escH(album) + yearStr + '</span></div>';
            albumTracks.forEach((f, idx) => {
                const durStr = f.duration ? _fmtSecs(f.duration) : '';
                const trackNum = f.track ? '<span style="min-width:24px;color:var(--rm-text-muted);font-size:12px">' + escH(f.track) + '</span>' : '';
                html += `<div class="rm-track rm-artist-track" data-path="${escH(f.path)}" data-album="${escH(album)}" data-idx="${idx}">
                    ${trackNum}
                    <div class="rm-track-info"><div class="rm-track-name">${escH(f.name)}</div>
                    <div class="rm-track-meta">${durStr}</div></div>
                    <button class="rm-track-btn rm-track-play"><i class="fas fa-play"></i></button>
                </div>`;
            });
        });
        content.innerHTML = html;
        content.querySelector('#rm-art-back').onclick = () => loadArtists(content);

        // Build full artist queue for playback
        const allTracks = albumNames.flatMap(a => albumMap[a]);
        const allQueue = allTracks.map(f => ({
            name: f.name, path: f.path, type: 'local',
            image: f.has_art ? '/api/radio-music/local/artwork?path=' + encodeURIComponent(f.path) + '&token=' + (NAS.token || '') : '',
            meta: f.artist || f.filename, folder: f.folder
        }));

        content.querySelector('.rm-artist-playall').onclick = () => {
            if (!allQueue.length) return;
            _musicQueue = allQueue;
            _musicQueueIdx = 0;
            playAudio(allQueue[0]);
            toast(t('Odtwarzam: ') + artistName, 'success');
        };

        content.querySelectorAll('.rm-artist-track').forEach(el => {
            const path = el.dataset.path;
            const globalIdx = allTracks.findIndex(f => f.path === path);
            el.onclick = (e) => {
                if (e.target.closest('.rm-track-play')) { e.stopPropagation(); }
                _musicQueue = allQueue;
                _musicQueueIdx = globalIdx >= 0 ? globalIdx : 0;
                playAudio(allQueue[_musicQueueIdx]);
            };
            const playBtn = el.querySelector('.rm-track-play');
            if (playBtn) playBtn.onclick = (e) => {
                e.stopPropagation();
                _musicQueue = allQueue;
                _musicQueueIdx = globalIdx >= 0 ? globalIdx : 0;
                playAudio(allQueue[_musicQueueIdx]);
            };
        });
    }

    /* ── 5-Band Equalizer ──────────────────────────── */

    let _eqEnabled = false;
    let _eqCtx = null;        // AudioContext
    let _eqSource = null;     // MediaElementSourceNode
    let _eqFilters = [];      // BiquadFilterNode[]
    let _eqBands = [60, 230, 910, 3600, 14000];
    let _eqGains = [0, 0, 0, 0, 0];
    const _EQ_PRESETS = {
        'Flat': [0, 0, 0, 0, 0],
        'Bass Boost': [6, 4, 0, 0, 0],
        'Treble Boost': [0, 0, 0, 4, 6],
        'Rock': [4, 2, -1, 3, 4],
        'Vocal': [-2, 0, 4, 3, 1],
        'Dance': [5, 3, 0, 2, 4],
        'Acoustic': [3, 1, 0, 2, 3],
    };

    function _initEq() {
        if (_eqCtx) return;
        try {
            _eqCtx = new (window.AudioContext || window.webkitAudioContext)();
        } catch(e) { return; }
        _eqFilters = _eqBands.map((freq, i) => {
            const f = _eqCtx.createBiquadFilter();
            f.type = i === 0 ? 'lowshelf' : i === _eqBands.length - 1 ? 'highshelf' : 'peaking';
            f.frequency.value = freq;
            f.gain.value = _eqGains[i];
            if (f.type === 'peaking') f.Q.value = 1.4;
            return f;
        });
        // Chain filters
        for (let i = 0; i < _eqFilters.length - 1; i++) {
            _eqFilters[i].connect(_eqFilters[i + 1]);
        }
        _eqFilters[_eqFilters.length - 1].connect(_eqCtx.destination);
    }

    function _connectEq() {
        if (!_eqCtx || !_audio || !_eqEnabled) return;
        try {
            if (_eqSource) { try { _eqSource.disconnect(); } catch(_) {} }
            _eqSource = _eqCtx.createMediaElementSource(_audio);
            _eqSource.connect(_eqFilters[0]);
        } catch(e) {
            // MediaElementSource can only be created once per element
        }
    }

    function _disconnectEq() {
        if (_eqSource) {
            try { _eqSource.disconnect(); _eqSource.connect(_eqCtx.destination); } catch(_) {}
        }
    }

    function _setEqGain(bandIdx, val) {
        _eqGains[bandIdx] = val;
        if (_eqFilters[bandIdx]) _eqFilters[bandIdx].gain.value = val;
        localStorage.setItem('rm_eq_gains', JSON.stringify(_eqGains));
    }

    function _loadEqSettings() {
        try {
            const saved = JSON.parse(localStorage.getItem('rm_eq_gains') || 'null');
            if (Array.isArray(saved) && saved.length === 5) _eqGains = saved;
            _eqEnabled = localStorage.getItem('rm_eq_enabled') === '1';
        } catch(_) {}
    }

    function _renderEqSection() {
        const labels = ['60', '230', '910', '3.6k', '14k'];
        let html = '<div class="rm-section-title"><i class="fas fa-sliders-h"></i> ' + t('Equalizer') + '</div>';
        html += '<div style="max-width:480px;padding:8px 0">';
        // Enable toggle
        html += '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">'
            + '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:var(--rm-text-secondary)">'
            + '<input type="checkbox" id="rm-eq-toggle" ' + (_eqEnabled ? 'checked' : '') + ' style="accent-color:var(--rm-accent);width:18px;height:18px">'
            + t('Włącz equalizer') + '</label></div>';
        // Presets
        html += '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px">';
        Object.keys(_EQ_PRESETS).forEach(name => {
            html += '<button class="rm-chip rm-eq-preset" data-preset="' + name + '">' + name + '</button>';
        });
        html += '</div>';
        // Sliders
        html += '<div style="display:flex;gap:16px;justify-content:center;padding:8px 0">';
        _eqBands.forEach((freq, i) => {
            html += '<div style="display:flex;flex-direction:column;align-items:center;gap:4px">'
                + '<span id="rm-eq-val-' + i + '" style="font-size:11px;color:var(--rm-accent);min-width:28px;text-align:center">' + (_eqGains[i] > 0 ? '+' : '') + _eqGains[i] + 'dB</span>'
                + '<input type="range" class="rm-eq-slider" data-band="' + i + '" min="-12" max="12" step="1" value="' + _eqGains[i] + '" '
                + 'style="writing-mode:vertical-lr;direction:rtl;height:120px;width:28px;accent-color:var(--rm-accent)">'
                + '<span style="font-size:11px;color:var(--rm-text-muted)">' + labels[i] + '</span></div>';
        });
        html += '</div></div>';
        return html;
    }

    function _wireEqHandlers(content) {
        content.querySelector('#rm-eq-toggle').onchange = (e) => {
            _eqEnabled = e.target.checked;
            localStorage.setItem('rm_eq_enabled', _eqEnabled ? '1' : '0');
            if (_eqEnabled) { _initEq(); _connectEq(); _eqFilters.forEach((f, i) => f.gain.value = _eqGains[i]); }
            else { _disconnectEq(); }
        };
        content.querySelectorAll('.rm-eq-slider').forEach(slider => {
            slider.oninput = (e) => {
                const band = parseInt(e.target.dataset.band);
                const val = parseInt(e.target.value);
                _setEqGain(band, val);
                const lbl = content.querySelector('#rm-eq-val-' + band);
                if (lbl) lbl.textContent = (val > 0 ? '+' : '') + val + 'dB';
            };
        });
        content.querySelectorAll('.rm-eq-preset').forEach(btn => {
            btn.onclick = () => {
                const gains = _EQ_PRESETS[btn.dataset.preset];
                if (!gains) return;
                gains.forEach((g, i) => {
                    _setEqGain(i, g);
                    const slider = content.querySelector('.rm-eq-slider[data-band="' + i + '"]');
                    if (slider) slider.value = g;
                    const lbl = content.querySelector('#rm-eq-val-' + i);
                    if (lbl) lbl.textContent = (g > 0 ? '+' : '') + g + 'dB';
                });
                content.querySelectorAll('.rm-eq-preset').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
            };
        });
    }

    /* ── Audiobooks for Kids ───────────────────────── */

    async function loadAudiobooks(toolbar, content) {
        if (_ytdlpReady === null) {
            const deps = await api('/radio-music/music/check-deps');
            _ytdlpReady = deps.ready || false;
        }
        if (!_ytdlpReady) {
            content.innerHTML = `<div class="rm-install-banner">
                <i class="fas fa-book-open"></i>
                <p>${t('Do audiobooków wymagany jest yt-dlp.')}</p>
                <button class="rm-install-btn" id="rm-install-ytdlp-ab"><i class="fas fa-download"></i> ${t('Zainstaluj yt-dlp')}</button>
            </div>`;
            content.querySelector('#rm-install-ytdlp-ab').onclick = async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Instalowanie...');
                const res = await api('/radio-music/music/install-deps', { method: 'POST' });
                if (res.ready) { _ytdlpReady = true; toast(t('Gotowe!'), 'success'); loadAudiobooks(toolbar, content); }
                else { btn.disabled = false; btn.innerHTML = '<i class="fas fa-download"></i> ' + t('Zainstaluj yt-dlp'); toast(res.error || t('Błąd'), 'error'); }
            };
            return;
        }

        toolbar.innerHTML = `<input class="rm-search" id="rm-ab-search" placeholder="${t('Szukaj audiobooków...')}" autofocus>`;
        content.innerHTML = '<div class="rm-chips" id="rm-ab-cats"></div><div id="rm-ab-results"></div>';

        const chipsEl = content.querySelector('#rm-ab-cats');
        _AUDIOBOOK_CATEGORIES.forEach(cat => {
            const chip = document.createElement('span');
            chip.className = 'rm-chip';
            chip.textContent = t(cat.label);
            chip.onclick = () => {
                chipsEl.querySelectorAll('.rm-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                searchAudiobooks(cat.q, content.querySelector('#rm-ab-results'));
            };
            chipsEl.appendChild(chip);
        });

        const searchInput = toolbar.querySelector('#rm-ab-search');
        let debounce;
        searchInput.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                const q = searchInput.value.trim();
                if (q) {
                    chipsEl.querySelectorAll('.rm-chip').forEach(c => c.classList.remove('active'));
                    searchAudiobooks(q + ' audiobook', content.querySelector('#rm-ab-results'));
                }
            }, 500);
        };

        // Show first category by default
        const first = chipsEl.querySelector('.rm-chip');
        if (first) first.click();
    }

    async function searchAudiobooks(q, container) {
        container.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i><p>' + t('Szukam...') + '</p></div>';
        const data = await api('/radio-music/music/search?q=' + encodeURIComponent(q) + '&limit=20');
        if (data.error) {
            container.innerHTML = '<div class="rm-empty"><i class="fas fa-exclamation-triangle"></i><p>' + escH(data.error) + '</p></div>';
            return;
        }
        if (!data.items || !data.items.length) {
            container.innerHTML = '<div class="rm-empty"><i class="fas fa-book-open"></i><p>' + t('Brak wyników') + '</p></div>';
            return;
        }
        renderMusicResults(data.items, container, 'Audiobooks');
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
            homepage: station.homepage || '',
            uuid: station.uuid,
        });
    }

    // Route a history/most-played item to the correct playback function.
    // If listItems & listIdx provided, build a queue from the list so next/prev works.
    function _playHistoryItem(item, listItems, listIdx) {
        // Fix type from URL pattern (corrupted history entries)
        if (!item.type || item.type === 'radio') {
            if (item.url && item.url.includes('/local/stream')) item.type = 'local';
            else if (item.url && (item.url.includes('youtube.com/') || item.url.includes('youtu.be/'))) item.type = 'music';
        }

        // Build queue from list context so prev/next navigates the list
        if (listItems && listItems.length > 1) {
            _musicQueue = listItems.map(it => _historyItemToQueueEntry(it));
            _musicQueueIdx = typeof listIdx === 'number' ? listIdx : 0;
        } else {
            _musicQueue = [_historyItemToQueueEntry(item)];
            _musicQueueIdx = 0;
        }

        if (item.type === 'local' || item.type === 'music' || item.type === 'podcast') {
            playAudio(item);
        } else {
            playStation(item);
        }
    }

    // Convert a history item into a queue-compatible entry for playMusicTrack
    function _historyItemToQueueEntry(it) {
        // Fix type
        if (!it.type || it.type === 'radio') {
            if (it.url && it.url.includes('/local/stream')) it.type = 'local';
            else if (it.url && (it.url.includes('youtube.com/') || it.url.includes('youtu.be/'))) it.type = 'music';
        }
        return {
            title: it.name || it.title || '', channel: it.meta || it.channel || '',
            url: it.url || '', thumbnail: it.image || it.favicon || '',
            source: it.type === 'local' ? 'local' : undefined,
            _histItem: it,  // keep original for playback routing
        };
    }

    // Advance to next track in queue (used by onended, Cast media ended, and error recovery)
    function _advanceQueue() {
        if (_advanceLock) return false;
        if (!_musicQueue.length || _musicQueueIdx < 0) return false;
        _advanceLock = true;
        setTimeout(() => { _advanceLock = false; }, 500);
        _cl('debug', 'advanceQueue', { from: _musicQueueIdx, queueLen: _musicQueue.length, shuffle: _shuffle, repeat: _repeatMode });

        let nextIdx;
        if (_shuffle) {
            if (_musicQueue.length === 1) nextIdx = 0;
            else { do { nextIdx = Math.floor(Math.random() * _musicQueue.length); } while (nextIdx === _musicQueueIdx); }
        } else {
            nextIdx = _musicQueueIdx + 1;
        }
        if (nextIdx < _musicQueue.length) {
            _musicQueueIdx = nextIdx;
            const nxt = _musicQueue[nextIdx];
            nxt._plItem ? _playTrackFromPlaylist(nxt) : playMusicTrack(nxt);
            return true;
        }
        if (_repeatMode === 1 && _musicQueue.length > 0) {
            _musicQueueIdx = _shuffle ? Math.floor(Math.random() * _musicQueue.length) : 0;
            const nxt = _musicQueue[_musicQueueIdx];
            nxt._plItem ? _playTrackFromPlaylist(nxt) : playMusicTrack(nxt);
            return true;
        }
        _advanceLock = false;
        return false;
    }

    // Advance podcast episode queue
    function _advancePodQueue() {
        if (!_podQueue.length || _podQueueIdx < 0) return false;
        const nextIdx = _podQueueIdx + 1;
        if (nextIdx < _podQueue.length) {
            _podQueueIdx = nextIdx;
            playAudio(_podQueue[nextIdx]);
            return true;
        }
        return false;
    }

    function playAudio(item) {
        // F-01 OPTIMISTIC STATE: update UI within <50ms before any audio events
        // User sees pause icon + loading ring immediately — no perceived lag
        const _optPlayBtn = bodyEl?.querySelector('#rm-play-pause');
        if (_optPlayBtn) {
            _optPlayBtn.innerHTML = '<i class="fas fa-pause"></i>';
            _optPlayBtn.classList.add('rm-loading');
        }
        const _optPlayer = bodyEl?.querySelector('#rm-player');
        if (_optPlayer) _optPlayer.style.display = 'flex';
        const _optName = bodyEl?.querySelector('#rm-player-name');
        if (_optName) _optName.textContent = item.name || '';

        // Safety: reset stuck _isCasting if no real Cast session exists
        if (_isCasting) {
            let realSession = null;
            try { realSession = window.cast && cast.framework ? cast.framework.CastContext.getInstance().getCurrentSession() : null; } catch(e) {}
            if (!realSession) {
                _cl('warning', 'playAudio: _isCasting was true but no real Cast session — resetting');
                _isCasting = false; _castSession = null; _castQueueActive = false;
                _syncCastBtnUi(false);
            }
        }
        _cl('info', 'playAudio', { name: item?.name, type: item?.type, isCasting: _isCasting, hasPath: !!item?.path, queueLen: _musicQueue.length, queueIdx: _musicQueueIdx });
        if (_audio) {
            _audio.onended = null; _audio.onerror = null;
            _audio.onplay = null; _audio.onpause = null;
            _audio.ontimeupdate = null; _audio.onloadedmetadata = null;
            _audio.onwaiting = null; _audio.onplaying = null; _audio.onstalled = null;
            _audio.pause(); _audio.src = ''; _audio.load(); // release media resource
        }
        if (_hlsInstance) { try { _hlsInstance.destroy(); } catch (_) {} _hlsInstance = null; }
        // F-04 RAM cleanup: immediately release preload buffer on every new play
        if (_preloadAudio) {
            _preloadAudio.oncanplaythrough = null;
            _preloadAudio.src = '';
            _preloadAudio.load();
            _preloadAudio = null;
        }
        _clearSeek();
        _audio = new Audio();
        _audio.volume = _isCasting ? 0 : (bodyEl.querySelector('#rm-vol')?.value || 80) / 100;
        if (_playbackRate !== 1) _audio.playbackRate = _playbackRate;
        _playing = item;
        _seekLocked = true; // unlock on onplay/oncanplay — prevents seekbar jumping to 0

        // Connect EQ if enabled
        if (_eqEnabled && _eqCtx) {
            try {
                _eqSource = _eqCtx.createMediaElementSource(_audio);
                _eqSource.connect(_eqFilters[0]);
            } catch(_) {}
        }

        // Reset reconnect state and preload on each new playback
        clearTimeout(_radioRetryTimer); _radioRetryTimer = null; _radioRetries = 0;
        if (_preloadAudio) { _preloadAudio.src = ''; _preloadAudio = null; }

        // Build ordered list of URLs to try (primary + fallbacks)
        const isMusic = item.type === 'music';
        const isLocal = item.type === 'local';
        const isPodcast = item.type === 'podcast';
        const isRadio = !isMusic && !isLocal && !isPodcast;

        // Context-Aware Queue:
        // Radio — save current music queue, don't touch _musicQueue
        // Podcast — use separate _podQueue
        // Music/local — restore saved music queue if returning from radio
        if (isRadio) {
            if (_musicQueue.length > 0 && _savedMusicQueue === null) {
                _savedMusicQueue = { queue: _musicQueue.slice(), idx: _musicQueueIdx };
            }
        } else if (isMusic || isLocal) {
            if (_savedMusicQueue !== null) {
                _musicQueue = _savedMusicQueue.queue;
                _musicQueueIdx = _savedMusicQueue.idx;
                _savedMusicQueue = null;
            }
        }

        // For local files, always refresh the token (stored URL may have stale token)
        if (isLocal && item.path) {
            item.url = '/api/radio-music/local/stream?path=' + encodeURIComponent(item.path) + '&token=' + (NAS.token || '');
        } else if (isLocal && item.url && item.url.includes('/local/stream')) {
            const m = item.url.match(/[?&]path=([^&]+)/);
            if (m) item.url = '/api/radio-music/local/stream?path=' + m[1] + '&token=' + (NAS.token || '');
        }

        const urls = isMusic ? [item.url] : [item.url, ...(item.alt_urls || [])];
        let urlIdx = 0;
        let hasPlayed = false;

        // ── Buffering state helpers ──
        function _setBuffering(on) {
            _isBuffering = on;
            // Safety net: auto-release buffering after 20s to prevent permanently stuck controls
            clearTimeout(_bufferingSafetyTimer);
            if (on) {
                _bufferingSafetyTimer = setTimeout(() => {
                    if (_isBuffering) { _setBuffering(false); _cl('warning', 'Buffering safety timeout — auto-released after 20s'); }
                }, 20000);
            }
            const player = bodyEl.querySelector('#rm-player');
            if (player) player.classList.toggle('rm-buffering', on);
            // F-01: progress ring on play button signals NAS loading to user
            const playBtn = bodyEl.querySelector('#rm-play-pause');
            if (playBtn) playBtn.classList.toggle('rm-loading', on);
            // Sync loading ring to NP overlay play button too
            const npPlayBtn = _npOverlay?.querySelector('#rm-np-playpause');
            if (npPlayBtn) npPlayBtn.classList.toggle('rm-loading', on);
            // Disable skip buttons while buffering so rapid taps don't skip past the loading track
            [
                bodyEl.querySelector('#rm-prev-btn'),
                bodyEl.querySelector('#rm-next-btn'),
                _npOverlay?.querySelector('#rm-np-prev'),
                _npOverlay?.querySelector('#rm-np-next'),
            ].forEach(btn => { if (btn) btn.classList.toggle('rm-btn-disabled', on); });
            bodyEl.querySelectorAll('.rm-card, .rm-track').forEach(c => c.classList.remove('rm-buffering'));
            if (on && item.uuid) {
                bodyEl.querySelectorAll('.rm-card').forEach(c => {
                    if (c._stationUuid === item.uuid) c.classList.add('rm-buffering');
                });
            }
            const meta = bodyEl.querySelector('#rm-player-meta');
            if (meta) meta.textContent = on ? t('Buforowanie…') : (item.meta || '');
        }

        function tryUrl(idx) {
            if (idx >= urls.length) {
                // All network URLs failed — try NAS archive as last resort (Fallback Logic)
                const archEntry = isMusic && item.url ? _archiveDb[item.url] : null;
                if (archEntry && archEntry.status === 'done' && archEntry.key) {
                    const archSrc = `/api/radio-music/archive/file/${archEntry.key}?token=${NAS.token || ''}`;
                    _cl('info', 'YouTube failed — falling back to NAS archive', { key: archEntry.key });
                    _audio.src = archSrc;
                    _audio.play().catch(() => {});
                    // Subtle icon change to indicate using archived version
                    _refreshArchiveBtn(item.url);
                    return;
                }
                _cl('error', 'All URLs failed', { name: item?.name, type: item?.type, urlCount: urls.length });
                toast(t('Nie udało się odtworzyć żadnego źródła'), 'error');
                _showEq(false);
                _setBuffering(false);
                setTimeout(() => _advanceQueue(), 800);
                return;
            }
            let src;
            // Source priority: NAS archive (if done) > YouTube proxy > fallback URLs
            const archEntry = isMusic && item.url ? _archiveDb[item.url] : null;
            if (idx === 0 && archEntry && archEntry.status === 'done' && archEntry.key) {
                // Silently play from NAS — faster and doesn't consume YouTube quota
                src = `/api/radio-music/archive/file/${archEntry.key}?token=${NAS.token || ''}`;
                _cl('debug', 'Playing from NAS archive', { key: archEntry.key });
            } else if (isLocal) {
                src = item.url;
            } else if (isMusic) {
                src = '/api/radio-music/music/stream?url=' + encodeURIComponent(urls[idx])
                    + '&token=' + (NAS.token || '');
            } else {
                src = '/api/radio-music/radio/proxy?url=' + encodeURIComponent(urls[idx])
                    + '&token=' + (NAS.token || '');
            }

            _cl('debug', 'tryUrl(' + idx + '/' + urls.length + ')', { src: src?.substring(0, 120) });
            _audio.src = src;
            _audio.play().catch(err => {
                if (err.name === 'NotAllowedError') {
                    _cl('warning', 'Autoplay blocked — showing tap-to-play', { name: item?.name });
                    // If we've seen a successful play before in this browser, use silent click-to-play
                    if (localStorage.getItem('rm_autoplay_ok')) {
                        _setBuffering(false);
                        document.addEventListener('click', () => {
                            _audio?.play().then(() => _setBuffering(false)).catch(() => {});
                        }, { once: true });
                    } else {
                        _showAutoplayPrompt();
                    }
                } else if (err.name === 'AbortError') {
                    // Normal: src changed while play() was pending (e.g. fast track switch) — ignore silently
                } else {
                    _cl('warning', 'play() rejected', { idx, error: err?.message, src: src?.substring(0, 80) });
                    tryUrl(idx + 1);
                }
            });
        }

        function _showAutoplayPrompt() {
            _setBuffering(false);
            // Remove any existing prompt
            bodyEl.querySelector('#rm-autoplay-prompt')?.remove();
            const prompt = document.createElement('button');
            prompt.id = 'rm-autoplay-prompt';
            prompt.className = 'rm-autoplay-prompt';
            prompt.innerHTML = '<i class="fas fa-play-circle"></i> ' + t('Dotknij aby odtworzyć');
            prompt.onclick = () => {
                prompt.remove();
                _setBuffering(true);
                _audio?.play().then(() => _setBuffering(false)).catch(() => {});
            };
            // Insert above player bar
            const player = bodyEl.querySelector('#rm-player');
            if (player) player.insertAdjacentElement('beforebegin', prompt);
        }

        _audio.onplay = () => {
            hasPlayed = true;
            localStorage.setItem('rm_autoplay_ok', '1');
            _acquireWakeLock();
            _seekLocked = false; // unlock seekbar — audio has started
            // Notify other tabs to pause (master-tab coordination)
            if (_bc) _bc.postMessage({ type: 'playing', tabId: _tabId });
            _setBuffering(false);
            clearTimeout(_radioRetryTimer); _radioRetryTimer = null;
            bodyEl.querySelector('#rm-autoplay-prompt')?.remove(); // clear tap-to-play if shown
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-pause"></i>';
            _npOverlay?.querySelector('#rm-np-playpause')?.innerHTML && (_npOverlay.querySelector('#rm-np-playpause').innerHTML = '<i class="fas fa-pause"></i>');
            _showEq(!isMusic && !isLocal);
            _updateSeekbar();
            _savePlaybackState();
            _updateMediaSession();
            if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
            _cl('info', 'Audio playing', { name: item?.name, volume: _audio?.volume });
        };
        _audio.onwaiting = () => _setBuffering(true);
        _audio.onplaying = () => { _setBuffering(false); clearTimeout(_radioRetryTimer); _radioRetryTimer = null; };
        _audio.onpause = () => {
            _releaseWakeLock();
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';
            _npOverlay?.querySelector('#rm-np-playpause')?.innerHTML && (_npOverlay.querySelector('#rm-np-playpause').innerHTML = '<i class="fas fa-play"></i>');
            _showEq(false);
            _savePlaybackState();
            if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
            // E-12: Audio Focus Loss detection — if pause was NOT user-initiated
            // (hasPlayed=true means we were actually playing), show subtle resume hint
            if (hasPlayed && document.visibilityState === 'hidden') {
                // System took audio focus (another app started playing) — nothing to do,
                // user will resume from lock screen MediaSession controls
                _cl('info', 'Audio paused by system (audio focus loss / hidden page)');
            }
        };
        _audio.onstalled = () => {
            if (!isRadio || !hasPlayed) return;
            _setBuffering(true);
            clearTimeout(_radioRetryTimer);
            _radioRetryTimer = setTimeout(() => {
                if (!_audio || _playing !== item) return;
                _cl('info', 'Radio stalled — reconnecting', { name: item?.name });
                _audio.load();
                _audio.play().catch(() => {});
            }, 5000);
        };
        _audio.onerror = () => {
            const code = _audio?.error?.code;
            const msg = _audio?.error?.message || '';
            _cl('error', 'Audio error', { code, msg, hasPlayed, urlIdx, name: item?.name, isRadio });
            clearTimeout(_radioRetryTimer); _radioRetryTimer = null;

            // MEDIA_ERR_SRC_NOT_SUPPORTED (4) — try hls.js for live streams / m3u8 redirects
            if (code === 4 && typeof Hls !== 'undefined' && Hls.isSupported() && !_audio._hlsAttempted) {
                const currentSrc = _audio.src;
                _audio._hlsAttempted = true;
                _cl('info', 'Trying HLS.js fallback', { src: currentSrc?.substring(0, 80) });
                if (_hlsInstance) { try { _hlsInstance.destroy(); } catch (_) {} _hlsInstance = null; }
                const hls = new Hls({ enableWorker: false });
                _hlsInstance = hls;
                hls.loadSource(currentSrc);
                hls.attachMedia(_audio);
                hls.on(Hls.Events.MANIFEST_PARSED, () => {
                    _audio.play().catch(err => {
                        if (err.name !== 'AbortError') tryUrl(urlIdx + 1);
                    });
                });
                hls.on(Hls.Events.ERROR, (ev, data) => {
                    if (data.fatal) {
                        _cl('warning', 'HLS.js fatal error', { type: data.type, details: data.details });
                        hls.destroy(); _hlsInstance = null;
                        tryUrl(urlIdx + 1);
                    }
                });
                return;
            }

            if (!hasPlayed) {
                urlIdx++;
                tryUrl(urlIdx);
            } else if (isRadio && _radioRetries < 3) {
                _radioRetries++;
                const delay = _radioRetries * 3000;
                _setBuffering(true);
                const meta = bodyEl.querySelector('#rm-player-meta');
                if (meta) meta.textContent = t('Łączenie {n}/{max}…', { n: _radioRetries, max: 3 });
                _cl('info', 'Radio error — retry ' + _radioRetries + '/3 in ' + delay + 'ms', { name: item?.name });
                _radioRetryTimer = setTimeout(() => {
                    if (!_audio || _playing !== item) return;
                    tryUrl(urlIdx);
                }, delay);
            } else {
                _showEq(false);
                _setBuffering(false);
                if (isRadio) {
                    const meta = bodyEl.querySelector('#rm-player-meta');
                    if (meta) meta.textContent = t('Błąd połączenia');
                    toast(t('Nie można połączyć ze stacją. Spróbuj ponownie.'), 'error');
                } else {
                    setTimeout(() => _advanceQueue(), 800);
                }
            }
        };
        let _endedHandled = false;
        _audio.onended = () => {
            if (_endedHandled) return;
            _endedHandled = true;
            _cl('info', 'Audio ended', { name: item?.name, isCasting: _isCasting });
            _showEq(false);
            _clearSeek();

            // Sleep timer — end of track mode: stop playback
            if (_onTrackEndedSleepCheck()) {
                bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';
                return;
            }

            // Repeat one — replay current track
            if (_repeatMode === 2) {
                _endedHandled = false;
                _audio.currentTime = 0;
                _audio.play().then(() => _showEq(true)).catch(() => {});
                return;
            }

            // Podcast — advance pod queue; music — advance music queue
            if (isPodcast) { if (_advancePodQueue()) return; }
            else { if (_advanceQueue()) return; }
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';
        };
        _audio.ontimeupdate = () => {
            // Throttle seekbar DOM updates to max 4/s (audio currentTime fires up to 20/s)
            const now = Date.now();
            if (now - _seekThrottleTs >= 250) {
                _seekThrottleTs = now;
                _updateSeekbar();
                // Podcast episode progress tracking (throttled with seekbar)
                if (_playing && _playing._podcast && _audio.duration > 0) {
                    _updateEpProgress(_playing.url || _playing.stream_url, _audio.currentTime, _audio.duration);
                }
                // Update Media Session position state for iOS control center scrubbing
                if ('mediaSession' in navigator && navigator.mediaSession.setPositionState && _audio
                        && isFinite(_audio.duration) && _audio.duration > 0) {
                    try { navigator.mediaSession.setPositionState({ duration: _audio.duration, playbackRate: 1, position: _audio.currentTime }); }
                    catch(e) {}
                }
            }
            // F-03 GAPLESS: preload at 90% of track (or 30s remaining, whichever first)
            // On NAS this gives ~8-12s for HDD spin-up before the track ends
            if ((isMusic || isLocal) && !_preloadAudio && _audio && isFinite(_audio.duration) && _audio.duration > 0) {
                const remaining = _audio.duration - _audio.currentTime;
                const pct = _audio.currentTime / _audio.duration;
                const shouldPreload = remaining < 30 || pct >= 0.9;
                if (shouldPreload && remaining > 0) {
                    const nextIdx = _musicQueueIdx + 1;
                    const nextItem = _musicQueue[nextIdx];
                    if (nextItem) {
                        const nextSrc = nextItem.type === 'local'
                            ? (nextItem.url || ('/api/radio-music/local/stream?path=' + encodeURIComponent(nextItem.path || '') + '&token=' + (NAS.token || '')))
                            : (nextItem.url ? '/api/radio-music/music/stream?url=' + encodeURIComponent(nextItem.url) + '&token=' + (NAS.token || '') : null);
                        if (nextSrc) {
                            _preloadAudio = new Audio();
                            _preloadAudio.preload = 'auto';
                            _preloadAudio.volume = 0; // silent until crossfade starts
                            _preloadAudio.src = nextSrc;
                            const targetVol = _audio.volume;
                            // When preload is buffered enough AND we're in final 5%, crossfade immediately
                            _preloadAudio.oncanplaythrough = () => {
                                if (!_preloadAudio || _playing !== item) return;
                                const pctNow = _audio.currentTime / _audio.duration;
                                if (pctNow >= 0.95) {
                                    _cl('info', 'Gapless crossfade triggered at ' + Math.round(pctNow * 100) + '%', { next: nextItem.name });
                                    _crossfade(_audio, _preloadAudio, targetVol, _crossfadeDuration, () => {
                                        // After crossfade, officially switch to next track
                                        _audio.onended?.(); // trigger queue advance
                                    });
                                }
                            };
                            _cl('debug', 'Preloading next track at ' + Math.round(pct * 100) + '%', { name: nextItem.name });
                        }
                    }
                }
            }
            // Fallback: detect track finished (onended may not fire for proxied streams)
            if (!_endedHandled && _audio && isFinite(_audio.duration) && _audio.duration > 1
                && _audio.currentTime >= _audio.duration - 0.5) {
                _endedHandled = true;
                setTimeout(() => {
                    // Double-check: audio truly stopped (not just buffering near end)
                    if (_audio && (_audio.ended || _audio.paused || _audio.currentTime >= _audio.duration - 0.5)) {
                        _showEq(false); _clearSeek();
                        if (_repeatMode === 2) {
                            _endedHandled = false;
                            _audio.currentTime = 0;
                            _audio.play().then(() => _showEq(true)).catch(() => {});
                            return;
                        }
                        if (_advanceQueue()) return;
                        bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';
                    } else { _endedHandled = false; }
                }, 1500);
            }
        };
        _audio.onloadedmetadata = () => {
            _updateSeekbar(); _savePlaybackState();
            // Restore podcast episode position
            if (item._podcast && _audio.duration > 0) {
                const prog = _epProgress[item.url || item.stream_url];
                if (prog && !prog.done && prog.pos > 5 && prog.pos < prog.dur - 5) {
                    _audio.currentTime = prog.pos;
                }
            }
        };

        // Periodic save of playback position
        if (_saveStateInterval) clearInterval(_saveStateInterval);
        _saveStateInterval = setInterval(_savePlaybackState, 30000); // save every 30s (was 5s)

        // Update player bar — show immediately with buffering indicator
        const player = bodyEl.querySelector('#rm-player');
        player.style.display = 'flex';
        bodyEl.querySelector('#rm-player-name').textContent = item.name;
        _setBuffering(true);  // show "Buforowanie…" until audio plays

        // Player art — use thumbnail for music, logo cascade for radio
        const art = bodyEl.querySelector('#rm-player-art');
        if (isMusic || item.type === 'local') {
            const artSrc = item.image || item.thumbnail || '';
            art.innerHTML = artSrc
                ? '<img src="' + escH(artSrc) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">'
                : '<i class="fas fa-music"></i>';
        } else {
            // Radio / podcast — use station icon with logo manifest lookup
            const _fItem = { name: item.name, favicon: item.image || item.favicon, homepage: item.homepage || '', url: item.url, stationuuid: item.uuid || item.stationuuid || '' };
            art.innerHTML = _stationIconHtml(_fItem);
        }

        // Save to history
        api('/radio-music/history', { method: 'POST', body: { item } });

        // Sync Now Playing overlay — in-place update regardless of minimized state.
        // The overlay is ONLY destroyed when the user explicitly closes it (swipe/button).
        if (_npOverlay) {
            _updateNowPlayingContent(item); // smooth crossfade, works whether open or minimized
        }

        // Highlight the matching row in the list and scroll it into view
        _highlightPlayingTrack(item);
        // Update shared state store (subscribers like queue view react instantly)
        _rmStore.set({ currentTrack: item, currentTrackIndex: _musicQueueIdx });

        // Start playback with fallback chain
        tryUrl(0);

        // If casting, send current track to Chromecast
        if (_isCasting) _castLoadCurrentTrack();
    }

    // Update player bar UI without starting playback (for Cast queue sync)
    function _updatePlayerBar(item) {
        if (!item || !bodyEl) return;
        const nameEl = bodyEl.querySelector('#rm-player-name');
        if (nameEl) nameEl.textContent = item.name || item.title || '';
        const metaEl = bodyEl.querySelector('#rm-player-meta');
        if (metaEl) metaEl.textContent = item.meta || item.channel || '';
        const art = bodyEl.querySelector('#rm-player-art');
        if (art) {
            const img = item.image || item.thumbnail;
            if (img) {
                art.innerHTML = '<img src="' + escH(img) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">';
            } else {
                art.innerHTML = '<i class="fas fa-music"></i>';
            }
        }
        _savePlaybackState();
    }

    // Highlight the currently playing element in the list and scroll it into view.
    // Uses data-url attributes (set during render) to find the matching element.
    function _highlightPlayingTrack(item) {
        if (!bodyEl) return;
        bodyEl.querySelectorAll('.rm-card, .rm-ep-item, .rm-track').forEach(c => c.classList.remove('rm-playing'));
        if (!item) return;

        let el = null;
        // 1. Match by URL (music YT, playlist track, podcast)
        if (item.url) {
            el = bodyEl.querySelector(`[data-url="${CSS.escape(item.url)}"]`);
        }
        // 2. Local files: item.url is the full stream URL but data-url stores the file path
        if (!el && item.path) {
            el = bodyEl.querySelector(`[data-url="${CSS.escape(item.path)}"]`);
        }
        // 3. Radio card: match by station UUID (JS property, not a DOM attribute)
        if (!el && item.uuid) {
            el = Array.from(bodyEl.querySelectorAll('.rm-card')).find(c => c._stationUuid === item.uuid);
        }
        if (el) {
            el.classList.add('rm-playing');
            el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
    }

    function _skipStation(dir) {
        // Block while buffering — prevents skipping past a track that's still loading
        if (_isBuffering) return;
        // Debounce rapid taps (1500ms cooldown) — prevents queued animations on fast Next/Next
        const now = Date.now();
        if (now - _prevNextTs < 1500) return;
        _prevNextTs = now;

        // Podcast queue has priority when a podcast is playing
        if (_playing && _playing._podcast && _podQueue.length > 0) {
            let nextIdx = _podQueueIdx + dir;
            if (nextIdx >= 0 && nextIdx < _podQueue.length) {
                _podQueueIdx = nextIdx;
                playAudio(_podQueue[nextIdx]);
            } else if (_repeatMode === 1 && _podQueue.length > 0) {
                _podQueueIdx = dir > 0 ? 0 : _podQueue.length - 1;
                playAudio(_podQueue[_podQueueIdx]);
            }
            return;
        }

        // Queue has priority (music tracks, local files, or history list items)
        if (_musicQueue.length > 0 && _musicQueueIdx >= 0) {
            let nextIdx;
            if (_shuffle) {
                if (_musicQueue.length === 1) { nextIdx = 0; }
                else {
                    do { nextIdx = Math.floor(Math.random() * _musicQueue.length); } while (nextIdx === _musicQueueIdx);
                }
            } else {
                nextIdx = _musicQueueIdx + dir;
            }
            if (nextIdx >= 0 && nextIdx < _musicQueue.length) {
                _musicQueueIdx = nextIdx;
                const nxt = _musicQueue[nextIdx];
                nxt._plItem ? _playTrackFromPlaylist(nxt) : playMusicTrack(nxt);
            } else if (_repeatMode === 1 && _musicQueue.length > 0) {
                _musicQueueIdx = dir > 0 ? 0 : _musicQueue.length - 1;
                const nxt = _musicQueue[_musicQueueIdx];
                nxt._plItem ? _playTrackFromPlaylist(nxt) : playMusicTrack(nxt);
            }
            return;
        }
        if (!_playing || !_recentStations.length) return;
        const idx = _recentStations.findIndex(s => s.uuid === _playing.uuid || s.name === _playing.name);
        let next;
        if (_shuffle) {
            if (_recentStations.length === 1) { next = 0; }
            else {
                do { next = Math.floor(Math.random() * _recentStations.length); } while (next === idx);
            }
        } else {
            next = idx + dir;
            if (next < 0) next = _recentStations.length - 1;
            if (next >= _recentStations.length) next = 0;
        }
        playStation(_recentStations[next]);
    }

    async function _acquireWakeLock() {
        if (!('wakeLock' in navigator)) return;
        try {
            if (_wakeLock) return; // already held
            _wakeLock = await navigator.wakeLock.request('screen');
            _wakeLock.addEventListener('release', () => { _wakeLock = null; });
            _cl('debug', 'WakeLock acquired');
        } catch(e) { _cl('debug', 'WakeLock failed', { msg: e.message }); }
    }

    function _releaseWakeLock() {
        if (_wakeLock) { _wakeLock.release(); _wakeLock = null; }
    }

    // Named handlers for cleanup in onClose
    function _onVisWakeLock() {
        if (document.visibilityState === 'visible' && _audio && !_audio.paused) {
            _acquireWakeLock();
        }
    }
    document.addEventListener('visibilitychange', _onVisWakeLock);

    // E-11: Bluetooth / audio output device disconnect — auto-pause to avoid music
    // blaring from phone speaker when headphones are pulled out in public
    let _onDeviceChange = null;
    if ('mediaDevices' in navigator && 'enumerateDevices' in navigator.mediaDevices) {
        let _lastOutputCount = 0;
        navigator.mediaDevices.enumerateDevices().then(devs => {
            _lastOutputCount = devs.filter(d => d.kind === 'audiooutput').length;
        }).catch(() => {});

        _onDeviceChange = async () => {
            if (!_audio || _audio.paused) return;
            try {
                const devs = await navigator.mediaDevices.enumerateDevices();
                const outputs = devs.filter(d => d.kind === 'audiooutput');
                if (outputs.length < _lastOutputCount) {
                    _audio.pause();
                    _cl('info', 'Audio paused: output device removed (Bluetooth disconnect)');
                    _showSystemInterruptToast(t('Słuchawki odłączone — wstrzymano'));
                }
                _lastOutputCount = outputs.length;
            } catch(e) { _cl('debug', 'devicechange check failed', { msg: e.message }); }
        };
        navigator.mediaDevices.addEventListener('devicechange', _onDeviceChange);
    }

    // E-12: Audio focus loss — another app/tab takes audio focus.
    // Web has no explicit AudioFocus API; we use two signals:
    // 1. MediaSession 'pause' action fired by Android system (already wired above via onpause)
    // 2. Page visibility hidden while playing (tab backgrounded by another media app)
    function _onVisFocusLoss() {
        if (document.visibilityState === 'hidden') {
            const audioRef = _audio;
            if (!audioRef || audioRef.paused) return;
            audioRef._hiddenAt = Date.now();
        } else if (document.visibilityState === 'visible' && _audio) {
            if (_audio.paused && _audio._hiddenAt) {
                const wasPausedExternally = (Date.now() - _audio._hiddenAt) < 60000;
                if (wasPausedExternally) {
                    _showSystemInterruptToast(t('Przerwano przez system — dotknij aby wznowić'), true);
                }
                _audio._hiddenAt = null;
            }
        }
    }
    document.addEventListener('visibilitychange', _onVisFocusLoss);

    // Toast for system-initiated pause events (BT disconnect, audio focus loss)
    function _showSystemInterruptToast(msg, withResumeBtn = false) {
        if (!bodyEl) return;
        const existing = bodyEl.querySelector('#rm-sys-toast');
        if (existing) existing.remove();
        const toast = document.createElement('div');
        toast.id = 'rm-sys-toast';
        toast.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:rgba(30,30,30,.95);color:#fff;padding:12px 20px;border-radius:24px;font-size:13px;z-index:9999;display:flex;align-items:center;gap:12px;box-shadow:0 4px 20px rgba(0,0,0,.5);backdrop-filter:blur(12px);max-width:90vw;text-align:center';
        toast.innerHTML = '<i class="fas fa-pause-circle" style="color:var(--rm-accent)"></i><span>' + msg + '</span>';
        if (withResumeBtn) {
            const btn = document.createElement('button');
            btn.style.cssText = 'background:var(--rm-accent);color:var(--rm-bg);border:none;border-radius:16px;padding:6px 14px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap';
            btn.textContent = t('▶ Wznów');
            btn.onclick = () => { _audio?.play().catch(() => {}); toast.remove(); };
            toast.appendChild(btn);
        }
        bodyEl.appendChild(toast);
        setTimeout(() => toast.remove(), withResumeBtn ? 8000 : 3000);
    }

    function stopPlayback() {
        _savePlaybackState();
        _clearSleepTimer();
        _stopLyricsSync();
        if (_saveStateInterval) { clearInterval(_saveStateInterval); _saveStateInterval = null; }
        clearTimeout(_radioRetryTimer); _radioRetryTimer = null; _radioRetries = 0;
        if (_preloadAudio) { _preloadAudio.src = ''; _preloadAudio = null; }
        if (_castSession) { try { _castSession.endSession(true); } catch(e) {} }
        _castSession = null;
        _isCasting = false;
        _castQueueActive = false;
        _syncCastBtnUi(false);
        if (_audio) {
            _audio.pause();
            _audio.src = ''; _audio.load(); // release media resource
            _audio = null;
        }
        _playing = null;
        _clearSeek();
        _hideNowPlaying();
        _releaseWakeLock();
        if ('mediaSession' in navigator) {
            navigator.mediaSession.metadata = null;
            navigator.mediaSession.playbackState = 'none';
        }
        const player = bodyEl?.querySelector('#rm-player');
        if (player) { player.style.display = 'none'; player.classList.remove('rm-buffering'); }
        bodyEl?.querySelectorAll('.rm-card.rm-buffering').forEach(c => c.classList.remove('rm-buffering'));
        _showEq(false);
    }

    function _showEq(show) {
        const eq = bodyEl?.querySelector('#rm-player-eq');
        if (eq) eq.style.display = show ? 'flex' : 'none';
    }

    /* ── Sleep Timer ───────────────────────────────────── */
    const _SLEEP_PRESETS = [
        { label: '15 min', mins: 15 },
        { label: '30 min', mins: 30 },
        { label: '45 min', mins: 45 },
        { label: '60 min', mins: 60 },
        { label: '90 min', mins: 90 },
    ];

    function _setSleepTimer(mins) {
        _clearSleepTimer();
        if (!mins) return;
        _sleepMode = 'time';
        _sleepEnd = Date.now() + mins * 60000;
        _sleepTimer = setTimeout(() => {
            if (_audio) { _audio.pause(); }
            toast(t('Wyłącznik czasowy — zatrzymano odtwarzanie'), 'info');
            _clearSleepTimer();
        }, mins * 60000);
        _syncSleepUi();
    }

    function _setSleepEndOfTrack() {
        _clearSleepTimer();
        _sleepMode = 'track';
        _sleepEnd = -1;
        _syncSleepUi();
    }

    function _clearSleepTimer() {
        if (_sleepTimer) { clearTimeout(_sleepTimer); _sleepTimer = null; }
        _sleepEnd = 0;
        _sleepMode = '';
        _syncSleepUi();
    }

    function _onTrackEndedSleepCheck() {
        if (_sleepMode === 'track') {
            _clearSleepTimer();
            toast(t('Wyłącznik czasowy — zatrzymano po utworze'), 'info');
            return true;
        }
        return false;
    }

    function _syncSleepUi() {
        const btn = bodyEl?.querySelector('#rm-np-sleep');
        if (!btn) return;
        if (_sleepEnd || _sleepMode) {
            btn.classList.add('rm-sleep-active');
            if (_sleepMode === 'track') {
                btn.innerHTML = '<i class="fas fa-moon"></i> ' + t('Po utworze');
            } else if (_sleepEnd > 0) {
                const mins = Math.ceil((_sleepEnd - Date.now()) / 60000);
                btn.innerHTML = '<i class="fas fa-moon"></i> ' + mins + ' min';
            }
        } else {
            btn.classList.remove('rm-sleep-active');
            btn.innerHTML = '<i class="fas fa-moon"></i> ' + t('Timer');
        }
    }

    function _showSleepDropdown(anchorBtn) {
        let dd = bodyEl?.querySelector('.rm-sleep-dropdown');
        if (dd && dd.classList.contains('open')) { dd.classList.remove('open'); return; }
        if (!dd) {
            dd = document.createElement('div');
            dd.className = 'rm-sleep-dropdown';
            anchorBtn.style.position = 'relative';
            anchorBtn.appendChild(dd);
        }
        let html = '';
        _SLEEP_PRESETS.forEach(p => {
            const active = _sleepMode === 'time' && _sleepEnd > 0 && Math.abs(Math.ceil((_sleepEnd - Date.now()) / 60000) - p.mins) < 2;
            html += `<div class="rm-sleep-option${active ? ' active' : ''}" data-mins="${p.mins}">${p.label}${active ? ' <span class="rm-sleep-check">✓</span>' : ''}</div>`;
        });
        const trackActive = _sleepMode === 'track';
        html += `<div class="rm-sleep-option${trackActive ? ' active' : ''}" data-action="track">${t('Po bieżącym utworze')}${trackActive ? ' <span class="rm-sleep-check">✓</span>' : ''}</div>`;
        if (_sleepEnd || _sleepMode) {
            html += `<div class="rm-sleep-option" data-action="off" style="color:var(--rm-error)">${t('Wyłącz timer')}</div>`;
        }
        dd.innerHTML = html;
        dd.classList.add('open');
        dd.querySelectorAll('.rm-sleep-option').forEach(opt => {
            opt.onclick = (e) => {
                e.stopPropagation();
                dd.classList.remove('open');
                if (opt.dataset.action === 'off') { _clearSleepTimer(); }
                else if (opt.dataset.action === 'track') { _setSleepEndOfTrack(); }
                else { _setSleepTimer(parseInt(opt.dataset.mins, 10)); }
            };
        });
        const closeDd = (e) => { if (!dd.contains(e.target)) { dd.classList.remove('open'); document.removeEventListener('click', closeDd, true); } };
        setTimeout(() => document.addEventListener('click', closeDd, true), 0);
    }

    /* ── Playback Speed ────────────────────────────────── */
    const _SPEED_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2];

    function _cycleSpeed() {
        const idx = _SPEED_STEPS.indexOf(_playbackRate);
        _playbackRate = _SPEED_STEPS[(idx + 1) % _SPEED_STEPS.length];
        if (_audio) _audio.playbackRate = _playbackRate;
        _syncSpeedUi();
    }

    function _syncSpeedUi() {
        const btn = bodyEl?.querySelector('#rm-np-speed');
        if (!btn) return;
        btn.textContent = _playbackRate === 1 ? '1x' : _playbackRate + 'x';
        btn.classList.toggle('rm-speed-changed', _playbackRate !== 1);
    }

    /* ── Keyboard Shortcuts ────────────────────────────── */
    function _onKeyDown(e) {
        if (!bodyEl || !bodyEl.isConnected) return;
        const tag = e.target.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;

        switch (e.key) {
            case ' ':
                e.preventDefault();
                if (_audio) { _audio.paused ? _audio.play().catch(() => {}) : _audio.pause(); }
                break;
            case 'ArrowRight':
                if (_audio && _audio.duration && isFinite(_audio.duration)) { _audio.currentTime = Math.min(_audio.duration, _audio.currentTime + 10); }
                break;
            case 'ArrowLeft':
                if (_audio && _audio.duration) { _audio.currentTime = Math.max(0, _audio.currentTime - 10); }
                break;
            case 'ArrowUp':
                e.preventDefault();
                if (_audio) { _audio.volume = Math.min(1, _audio.volume + 0.05); const v = bodyEl.querySelector('#rm-vol'); if (v) v.value = _audio.volume; }
                break;
            case 'ArrowDown':
                e.preventDefault();
                if (_audio) { _audio.volume = Math.max(0, _audio.volume - 0.05); const v = bodyEl.querySelector('#rm-vol'); if (v) v.value = _audio.volume; }
                break;
            case 'n': case 'N':
                _skipStation(1);
                break;
            case 'p': case 'P':
                _skipStation(-1);
                break;
            case 'm': case 'M':
                if (_audio) { _audio.muted = !_audio.muted; }
                break;
            case 'Escape': {
                const np = bodyEl.querySelector('.rm-np-overlay');
                if (np) _hideNowPlaying();
                const dd = bodyEl.querySelector('.rm-sleep-dropdown.open');
                if (dd) dd.classList.remove('open');
                break;
            }
        }
    }

    /* ── Synced Lyrics (LRC) ───────────────────────────── */
    function _parseLrc(lrc) {
        if (!lrc) return null;
        const lines = [];
        lrc.split('\n').forEach(line => {
            const m = line.match(/^\[(\d{2}):(\d{2})\.(\d{2,3})\]\s*(.*)/);
            if (m) {
                const ms = parseInt(m[1]) * 60000 + parseInt(m[2]) * 1000 + parseInt(m[3].padEnd(3, '0'));
                lines.push({ time: ms, text: m[4] });
            }
        });
        return lines.length > 3 ? lines : null;
    }

    function _renderSyncedLyrics(panel, lines) {
        panel.innerHTML = lines.map((l, i) =>
            '<div class="rm-lyrics-line" data-idx="' + i + '">' + escH(l.text || '♪') + '</div>'
        ).join('');
    }

    function _startLyricsSync(panel) {
        _stopLyricsSync();
        if (!_syncedLyrics || !_audio) return;
        _lyrSyncInterval = setInterval(() => {
            if (!_audio || _audio.paused) return;
            const ms = _audio.currentTime * 1000;
            let active = 0;
            for (let i = _syncedLyrics.length - 1; i >= 0; i--) {
                if (_syncedLyrics[i].time <= ms) { active = i; break; }
            }
            panel.querySelectorAll('.rm-lyrics-line').forEach((el, i) => {
                el.classList.toggle('rm-lyr-active', i === active);
                el.classList.toggle('rm-lyr-near', i === active - 1 || i === active + 1);
            });
            const activeEl = panel.querySelector('.rm-lyr-active');
            if (activeEl) activeEl.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }, 300);
    }

    function _stopLyricsSync() {
        if (_lyrSyncInterval) { clearInterval(_lyrSyncInterval); _lyrSyncInterval = null; }
        _syncedLyrics = null;
    }

    /* ── Queue Drag & Drop ─────────────────────────────── */
    function _setupQueueDnD(panel) {
        let dragIdx = -1;
        panel.addEventListener('pointerdown', (e) => {
            const handle = e.target.closest('.rm-np-q-item-drag');
            if (!handle) return;
            const item = handle.closest('.rm-np-q-item');
            if (!item) return;
            dragIdx = parseInt(item.dataset.idx, 10);
            if (isNaN(dragIdx)) return;
            item.classList.add('rm-q-dragging');
            item.setPointerCapture(e.pointerId);

            const onMove = (ev) => {
                const el = document.elementFromPoint(ev.clientX, ev.clientY)?.closest('.rm-np-q-item');
                panel.querySelectorAll('.rm-np-q-item').forEach(q => q.classList.remove('rm-q-drag-over'));
                if (el && el !== item) el.classList.add('rm-q-drag-over');
            };
            const onUp = (ev) => {
                item.classList.remove('rm-q-dragging');
                item.releasePointerCapture(ev.pointerId);
                panel.removeEventListener('pointermove', onMove);
                panel.removeEventListener('pointerup', onUp);
                const target = document.elementFromPoint(ev.clientX, ev.clientY)?.closest('.rm-np-q-item');
                panel.querySelectorAll('.rm-np-q-item').forEach(q => q.classList.remove('rm-q-drag-over'));
                if (!target) return;
                const dropIdx = parseInt(target.dataset.idx, 10);
                if (isNaN(dropIdx) || dropIdx === dragIdx) return;
                // Reorder _musicQueue
                const [moved] = _musicQueue.splice(dragIdx, 1);
                _musicQueue.splice(dropIdx, 0, moved);
                // Fix current index
                if (_musicQueueIdx === dragIdx) _musicQueueIdx = dropIdx;
                else if (dragIdx < _musicQueueIdx && dropIdx >= _musicQueueIdx) _musicQueueIdx--;
                else if (dragIdx > _musicQueueIdx && dropIdx <= _musicQueueIdx) _musicQueueIdx++;
                if (_renderNpQueueFn) _renderNpQueueFn();
            };
            panel.addEventListener('pointermove', onMove);
            panel.addEventListener('pointerup', onUp);
        });
    }

    /* ── Podcast Episode Progress ──────────────────────── */
    function _loadEpProgress() {
        try { _epProgress = JSON.parse(localStorage.getItem('rm_ep_progress') || '{}'); } catch(_) { _epProgress = {}; }
    }

    function _saveEpProgress() {
        try { localStorage.setItem('rm_ep_progress', JSON.stringify(_epProgress)); } catch(_) {}
    }

    function _updateEpProgress(url, pos, dur) {
        if (!url || !dur) return;
        const done = pos / dur > 0.95;
        _epProgress[url] = { pos: Math.floor(pos), dur: Math.floor(dur), done };
        _saveEpProgress();
    }

    function _getEpProgressPct(url) {
        const p = _epProgress[url];
        if (!p || !p.dur) return 0;
        return p.done ? 100 : Math.floor(p.pos / p.dur * 100);
    }

    /* ── Crossfade User Setting ────────────────────────── */
    function _loadCrossfadeSetting() {
        try {
            const v = parseInt(localStorage.getItem('rm_crossfade_ms'), 10);
            if (v >= 0 && v <= 12000) _crossfadeDuration = v;
        } catch(_) {}
    }

    function _saveCrossfadeSetting(ms) {
        _crossfadeDuration = ms;
        try { localStorage.setItem('rm_crossfade_ms', String(ms)); } catch(_) {}
    }

    /* ── Chromecast / Google Cast SDK ──────────────────── */

    function _initCast() {
        function _setupCastFramework() {
            _castAvail = true;
            try {
                const ctx = cast.framework.CastContext.getInstance();
                ctx.setOptions({
                    receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
                    autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
                });
                if (window._rmCastSessionCb) {
                    try { ctx.removeEventListener(cast.framework.CastContextEventType.SESSION_STATE_CHANGED, window._rmCastSessionCb); } catch(e) {}
                }
                window._rmCastSessionCb = _onCastSessionChanged;
                ctx.addEventListener(cast.framework.CastContextEventType.SESSION_STATE_CHANGED, _onCastSessionChanged);
                // Show/hide cast button based on device availability (C-01)
                ctx.addEventListener(cast.framework.CastContextEventType.CAST_STATE_CHANGED, _onCastStateChanged);

                _castPlayer = new cast.framework.RemotePlayer();
                _castController = new cast.framework.RemotePlayerController(_castPlayer);
                if (window._rmCastPlayerCb) {
                    try { _castController.removeEventListener(cast.framework.RemotePlayerEventType.PLAYER_STATE_CHANGED, window._rmCastPlayerCb); } catch(e) {}
                }
                window._rmCastPlayerCb = _onCastPlayerStateChanged;
                _castController.addEventListener(cast.framework.RemotePlayerEventType.PLAYER_STATE_CHANGED, _onCastPlayerStateChanged);

                // Seek bar sync — update PWA seekbar from Cast position (remote control feel)
                _castController.addEventListener(cast.framework.RemotePlayerEventType.CURRENT_TIME_CHANGED, _onCastTimeChanged);
                // Volume sync — physical TV remote / receiver volume reflected in PWA slider
                _castController.addEventListener(cast.framework.RemotePlayerEventType.VOLUME_LEVEL_CHANGED, _onCastVolumeChanged);
                // Pause/play sync — if paused from Cast receiver UI, update PWA button
                _castController.addEventListener(cast.framework.RemotePlayerEventType.IS_PAUSED_CHANGED, _onCastPausedChanged);

                _cl('info', 'Cast SDK initialized, _castAvail=true');
            } catch (e) {
                _cl('error', 'Cast framework init error', { error: e.message || String(e) });
                _castAvail = false;
            }
        }

        window['__onGCastApiAvailable'] = function(isAvailable) {
            _cl('info', 'Cast API available: ' + isAvailable);
            if (!isAvailable) { _castAvail = false; return; }
            _setupCastFramework();
        };
        // Fetch NAS LAN IP — Chromecast needs the real IP, not localhost/hostname
        api('/radio-music/cast-info').then(d => {
            if (d?.lan_origin) {
                _castLanOrigin = d.lan_origin;
                _cl('info', 'Cast LAN origin: ' + _castLanOrigin);
            }
        }).catch(() => {});
        if (!document.querySelector('script[src*="cast_sender"]')) {
            const s = document.createElement('script');
            s.src = 'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1';
            s.async = true;
            s.onerror = () => _cl('error', 'Cast SDK script failed to load (CSP or network)');
            document.head.appendChild(s);
            _cl('info', 'Loading Cast SDK script...');
        } else if (window.cast && cast.framework) {
            _cl('info', 'Cast SDK already loaded, running setup');
            _setupCastFramework();
        } else {
            _cl('warning', 'Cast script tag exists but cast.framework not available');
        }
    }

    function _onCastPlayerStateChanged() {
        if (!_isCasting || !_castPlayer) return;
        _cl('debug', 'Cast player state: ' + _castPlayer.playerState);
        if (_castPlayer.playerState === 'IDLE') {
            const session = cast.framework.CastContext.getInstance().getCurrentSession();
            const media = session?.getMediaSession();
            const reason = media?.idleReason || 'unknown';
            _cl('info', 'Cast IDLE, reason: ' + reason);
            if (reason === 'FINISHED') {
                _advanceQueue();
            }
        }
    }

    // C-01: Show/hide cast button based on whether Cast devices are present on the network
    function _onCastStateChanged(event) {
        const state = event.castState;
        const hasDevices = state !== cast.framework.CastState.NO_DEVICES_AVAILABLE;
        const btn = bodyEl?.querySelector('#rm-cast-btn');
        if (btn) btn.style.display = hasDevices ? '' : 'none';
        _cl('debug', 'Cast state changed: ' + state + ', hasDevices=' + hasDevices);
    }

    // Seekbar sync: Cast position → PWA seekbar (fires ~1Hz from Cast SDK)
    function _onCastTimeChanged() {
        if (!_isCasting || !_castPlayer) return;
        const cur = _castPlayer.currentTime;
        const dur = _castPlayer.duration;
        if (!isFinite(dur) || dur <= 0) return;
        const pct = (cur / dur) * 100;
        const seekbar = bodyEl?.querySelector('#rm-seekbar');
        if (!seekbar) return;
        seekbar.classList.add('visible');
        const fill = seekbar.querySelector('#rm-seek-fill');
        const thumb = seekbar.querySelector('#rm-seek-thumb');
        if (fill) fill.style.width = pct + '%';
        if (thumb) thumb.style.left = pct + '%';
        const curEl = seekbar.querySelector('#rm-seek-cur');
        const durEl = seekbar.querySelector('#rm-seek-dur');
        if (curEl) curEl.textContent = _fmtTime(cur);
        if (durEl) durEl.textContent = _fmtTime(dur);
        // Sync NP overlay seekbar too
        const npBar = _npOverlay?.querySelector('#rm-np-seek-fill');
        if (npBar) npBar.style.width = pct + '%';
        const npCur = _npOverlay?.querySelector('#rm-np-cur');
        if (npCur) npCur.textContent = _fmtTime(cur);
    }

    // Volume sync: Cast receiver volume → PWA volume slider
    function _onCastVolumeChanged() {
        if (!_isCasting || !_castPlayer) return;
        const vol = _castPlayer.volumeLevel;
        const slider = bodyEl?.querySelector('#rm-vol');
        if (slider) {
            slider.value = Math.round(vol * 100);
            slider.dispatchEvent(new Event('input', { bubbles: false })); // update icon only
        }
        _cl('debug', 'Cast volume: ' + Math.round(vol * 100));
    }

    // Pause sync: Cast receiver pause state → PWA play/pause button
    function _onCastPausedChanged() {
        if (!_isCasting || !_castPlayer) return;
        const paused = _castPlayer.isPaused;
        const btn = bodyEl?.querySelector('#rm-play-pause');
        if (btn) btn.innerHTML = paused
            ? '<i class="fas fa-play"></i>'
            : '<i class="fas fa-pause"></i>';
        const npBtn = _npOverlay?.querySelector('#rm-np-playpause');
        if (npBtn) npBtn.innerHTML = paused
            ? '<i class="fas fa-play"></i>'
            : '<i class="fas fa-pause"></i>';
        if ('mediaSession' in navigator) {
            navigator.mediaSession.playbackState = paused ? 'paused' : 'playing';
        }
    }

    function _onCastSessionChanged(event) {
        const state = event.sessionState;
        _cl('info', 'Cast session: ' + state);
        if (state === cast.framework.SessionState.SESSION_STARTED ||
            state === cast.framework.SessionState.SESSION_RESUMED) {
            _castSession = cast.framework.CastContext.getInstance().getCurrentSession();
            _isCasting = true;
            _syncCastBtnUi(true);
            toast(t('Połączono z Chromecast'), 'success');
            const deviceName = _castSession?.getCastDevice?.()?.friendlyName || '?';
            _cl('info', 'Cast connected to: ' + deviceName);
            // Suppress browser MediaSession so Android shows only ONE Cast volume slider (not two)
            if ('mediaSession' in navigator) {
                navigator.mediaSession.playbackState = 'none';
            }
            _castLoadCurrentTrack();
        } else if (state === cast.framework.SessionState.SESSION_ENDED) {
            // C-05: capture Cast position so we can resume locally from same spot
            const resumeAt = _castPlayer?.currentTime || 0;
            const wasPlaying = _castPlayer && !_castPlayer.isPaused;
            _castSession = null;
            _isCasting = false;
            _castQueueActive = false;
            _syncCastBtnUi(false);
            // Restore browser MediaSession — user is back to local playback
            _updateMediaSession();
            if (_audio) {
                _audio.volume = _preCastVolume;
                if (_playing?.type !== 'radio' && resumeAt > 1) {
                    _audio.currentTime = resumeAt;
                }
                if (wasPlaying) {
                    _audio.play().catch(() => {});
                    toast(t('Odtwarzanie wróciło na urządzenie'), 'info');
                }
            }
            _cl('info', 'Cast disconnected, resumed locally at ' + Math.round(resumeAt) + 's');
        }
    }

    async function _castLoadCurrentTrack() {
        if (window.cast && cast.framework) {
            _castSession = cast.framework.CastContext.getInstance().getCurrentSession();
        }
        if (!_castSession || !_playing) {
            _cl('warning', 'Cast loadTrack skipped: session=' + !!_castSession + ' playing=' + !!_playing);
            if (!_castSession && _isCasting) {
                _cl('warning', 'Resetting stuck _isCasting from _castLoadCurrentTrack');
                _isCasting = false; _castQueueActive = false;
                _syncCastBtnUi(false);
                if (_audio) _audio.volume = (bodyEl.querySelector('#rm-vol')?.value || 80) / 100;
            }
            return;
        }

        let mediaUrl, ct;

        if (_playing.type === 'music' && _playing.url && !_playing.url.startsWith('/api/')) {
            try {
                const data = await api('/radio-music/music/direct-url?url=' + encodeURIComponent(_playing.url));
                if (data.audio_url) {
                    mediaUrl = data.audio_url;
                    ct = data.content_type || 'audio/mp4';
                }
            } catch (e) { _cl('error', 'Cast direct-url resolve failed', { error: e.message }); }
        }

        if (!mediaUrl && _playing.type === 'local' && _playing.path) {
            mediaUrl = _castLanOrigin + '/api/radio-music/local/stream?path='
                + encodeURIComponent(_playing.path) + '&token=' + (NAS.token || '');
            ct = 'audio/mpeg';
        }

        if (!mediaUrl && _playing.type === 'radio' && _playing.url) {
            mediaUrl = _playing.url;
            ct = 'audio/mpeg';
        }

        if (!mediaUrl && _audio?.src) {
            mediaUrl = _audio.src;
            ct = 'audio/mpeg';
        }
        if (!mediaUrl) {
            _cl('warning', 'Cast loadTrack: no URL resolved', { type: _playing.type, name: _playing.name });
            return;
        }

        _cl('info', 'Cast loadMedia', { url: mediaUrl.substring(0, 100), type: ct, track: _playing.name });
        const mediaInfo = new chrome.cast.media.MediaInfo(mediaUrl, ct);
        // BUFFERED stream type: Android recognises it as music (not phone call), shows single Cast volume slider
        mediaInfo.streamType = chrome.cast.media.StreamType.BUFFERED;
        mediaInfo.metadata = new chrome.cast.media.MusicTrackMediaMetadata();
        if (_playing) {
            mediaInfo.metadata.title = _playing.name || '';
            mediaInfo.metadata.artist = _playing.meta || '';
            if (_playing.image) {
                const imgUrl = _playing.image.startsWith('http') ? _playing.image : (_castLanOrigin + _playing.image);
                mediaInfo.metadata.images = [new chrome.cast.Image(imgUrl)];
            }
        }
        const request = new chrome.cast.media.LoadRequest(mediaInfo);
        request.autoplay = true;
        if (_audio && _audio.currentTime > 1) request.currentTime = _audio.currentTime;

        try {
            await _castSession.loadMedia(request);
            _cl('info', 'Cast loadMedia SUCCESS: ' + (_playing.name || '?'));
            _preCastVolume = (bodyEl.querySelector('#rm-vol')?.value || 80) / 100;
            if (_audio) _audio.volume = 0;
        } catch (err) {
            _cl('error', 'Cast loadMedia FAILED', { error: err?.message || String(err), track: _playing.name });
            toast(t('Cast: nie udało się załadować utworu'), 'error');
        }
    }

    // Resolve a track to an absolute URL accessible by Chromecast
    async function _resolveCastMediaUrl(tr) {
        if (tr.type === 'music' && tr.url && !tr.url.startsWith('/api/')) {
            try {
                const data = await api('/radio-music/music/direct-url?url=' + encodeURIComponent(tr.url));
                if (data.audio_url) return { url: data.audio_url, ct: data.content_type || 'audio/mp4' };
            } catch (e) {}
            // Fallback: proxy URL via NAS LAN IP (Chromecast must reach real IP, not localhost)
            return {
                url: _castLanOrigin + '/api/radio-music/music/stream?url=' + encodeURIComponent(tr.url) + '&token=' + (NAS.token || ''),
                ct: 'audio/mp4'
            };
        }
        if (tr.type === 'local') {
            const path = tr.path || (tr.url?.match(/[?&]path=([^&]+)/)?.[1]);
            if (path) {
                return {
                    url: _castLanOrigin + '/api/radio-music/local/stream?path=' + path + '&token=' + (NAS.token || ''),
                    ct: 'audio/mpeg'
                };
            }
        }
        if (tr.type === 'radio' && tr.url) {
            return { url: tr.url, ct: 'audio/mpeg' };
        }
        return null;
    }

    // Load full playlist queue on Chromecast (plays autonomously even when phone sleeps)
    async function _castLoadQueue(startIdx) {
        if (!_castSession) return _castLoadCurrentTrack();
        const session = _castSession.getSessionObj ? _castSession.getSessionObj() : null;
        if (!session || !session.queueLoad || _musicQueue.length <= 1) {
            return _castLoadCurrentTrack();
        }

        // Resolve URLs for all queue items in parallel
        const urlResults = await Promise.all(_musicQueue.map(tr => _resolveCastMediaUrl(tr)));

        const items = [];
        urlResults.forEach((resolved, i) => {
            if (!resolved) return;
            const tr = _musicQueue[i];
            const mediaInfo = new chrome.cast.media.MediaInfo(resolved.url, resolved.ct);
            mediaInfo.metadata = new chrome.cast.media.MusicTrackMediaMetadata();
            mediaInfo.metadata.title = tr.name || tr.title || '';
            mediaInfo.metadata.artist = tr.meta || tr.channel || '';
            const img = tr.image || tr.thumbnail;
            if (img) {
                const imgUrl = img.startsWith('http') ? img : (_castLanOrigin + img);
                mediaInfo.metadata.images = [new chrome.cast.Image(imgUrl)];
            }
            items.push(new chrome.cast.media.QueueItem(mediaInfo));
        });

        if (!items.length) return _castLoadCurrentTrack();

        const queueRequest = new chrome.cast.media.QueueLoadRequest(items);
        queueRequest.startIndex = startIdx ?? _musicQueueIdx ?? 0;
        queueRequest.repeatMode = _repeatMode === 1 ? chrome.cast.media.RepeatMode.ALL
            : _repeatMode === 2 ? chrome.cast.media.RepeatMode.SINGLE
            : chrome.cast.media.RepeatMode.OFF;

        try {
            await new Promise((resolve, reject) => session.queueLoad(queueRequest, resolve, reject));
            _castQueueActive = true;
            _preCastVolume = (bodyEl.querySelector('#rm-vol')?.value || 80) / 100;
            if (_audio) _audio.volume = 0;
        } catch (err) {
            console.warn('Cast queueLoad error:', err);
            _castQueueActive = false;
            return _castLoadCurrentTrack();
        }
    }

    // Unified Cast play/pause toggle — use explicit play()/pause() via RemoteMediaClient
    // instead of controlling local _audio when Chromecast is active.
    // Returns true if Cast handled the action (caller should skip local audio control).
    function _castTogglePlayPause() {
        if (!_isCasting || !_castPlayer || !_castController) return false;
        const paused = _castPlayer.isPaused;
        // Optimistic UI update — don't wait for Cast ACK
        const icon = paused ? '<i class="fas fa-pause"></i>' : '<i class="fas fa-play"></i>';
        const mainBtn = bodyEl?.querySelector('#rm-play-pause');
        const npBtn = _npOverlay?.querySelector('#rm-np-playpause');
        if (mainBtn) mainBtn.innerHTML = icon;
        if (npBtn) npBtn.innerHTML = icon;
        _showEq(paused);
        if ('mediaSession' in navigator) {
            navigator.mediaSession.playbackState = paused ? 'playing' : 'paused';
        }
        // Send command to Chromecast receiver
        _castController.playOrPause();
        return true;
    }

    function _toggleCast() {
        _cl('info', 'Cast toggle', { castAvail: _castAvail, isCasting: _isCasting, hasSession: !!_castSession });
        if (!_audio) {
            toast(t('Najpierw włącz muzykę'), 'info');
            return;
        }
        if (_isCasting && _castSession) {
            _cl('info', 'Disconnecting Cast');
            _castSession.endSession(true);
            _isCasting = false;
            _castSession = null;
            _castQueueActive = false;
            _syncCastBtnUi(false);
            if (_audio) _audio.volume = _preCastVolume;
            return;
        }
        if (_castAvail) {
            _cl('info', 'Requesting Cast session via SDK');
            cast.framework.CastContext.getInstance().requestSession().catch(err => {
                _cl('error', 'Cast requestSession failed', { error: String(err) });
                if (err !== 'cancel') toast(t('Nie udało się połączyć z Chromecast'), 'error');
            });
            return;
        }
        // Fallback: Remote Playback API (limited — no auto-advance)
        _cl('warning', 'Cast SDK not available, trying Remote Playback API fallback');
        if (_audio.remote) {
            _audio.remote.prompt().catch(err => {
                if (err.name === 'NotAllowedError') return;
                _cl('warning', 'Remote Playback API failed', { error: err.message });
                toast(t('Chromecast niedostępny — użyj Chrome'), 'info');
            });
            return;
        }
        _cl('warning', 'No Cast method available');
        toast(t('Chromecast niedostępny — użyj Chrome'), 'info');
    }

    function _syncCastBtnUi(casting) {
        const btn = bodyEl?.querySelector('#rm-cast-btn');
        if (btn) btn.classList.toggle('rm-casting', casting);
        const npBtn = _npOverlay?.querySelector('#rm-np-cast');
        if (npBtn) npBtn.classList.toggle('rm-lyrics-active', casting);
    }

    function _updateSeekbar() {
        if (!_audio) return;
        if (_seekLocked) return; // don't jump to 0 during track switch
        const seekbar = bodyEl?.querySelector('#rm-seekbar');
        if (!seekbar) return;
        const dur = _audio.duration;
        const cur = _audio.currentTime;
        const seekable = isFinite(dur) && dur > 0;

        seekbar.classList.toggle('visible', seekable);
        if (!seekable) return;

        const pct = (cur / dur) * 100;
        const fill = seekbar.querySelector('#rm-seek-fill');
        const thumb = seekbar.querySelector('#rm-seek-thumb');
        if (fill) fill.style.width = pct + '%';
        if (thumb) thumb.style.left = pct + '%';
        const curEl = seekbar.querySelector('#rm-seek-cur');
        const durEl = seekbar.querySelector('#rm-seek-dur');
        if (curEl) curEl.textContent = _fmtTime(cur);
        if (durEl) durEl.textContent = _fmtTime(dur);
    }

    function _clearSeek() {
        if (_seekInterval) { clearInterval(_seekInterval); _seekInterval = null; }
        const seekbar = bodyEl?.querySelector('#rm-seekbar');
        if (seekbar) seekbar.classList.remove('visible');
    }

    function _updateMediaSession() {
        if (!('mediaSession' in navigator) || !_playing) return;
        const item = _playing;
        // Build artwork array with multiple sizes for best lock screen rendering (min 512x512 on Motorola)
        const artwork = [];
        if (item.image) {
            // Add explicit size variants — browsers pick the largest available
            artwork.push({ src: item.image, sizes: '512x512', type: 'image/jpeg' });
            artwork.push({ src: item.image, sizes: '256x256', type: 'image/jpeg' });
            artwork.push({ src: item.image, sizes: '96x96',  type: 'image/jpeg' });
        } else {
            // Fallback: NAS icon — always 512x512 so lock screen is never blank
            artwork.push({ src: '/img/icon-512.png', sizes: '512x512', type: 'image/png' });
        }
        navigator.mediaSession.metadata = new MediaMetadata({
            title: item.name || '',
            artist: item.meta || 'EthOS Radio & Music',
            album: item.type === 'radio' ? t('Radio Live') : (item.album || ''),
            artwork,
        });
        navigator.mediaSession.setActionHandler('play', () => {
            if (_isCasting) {
                if (_castPlayer?.isPaused) _castController?.playOrPause();
                return;
            }
            _audio?.play();
        });
        navigator.mediaSession.setActionHandler('pause', () => {
            if (_isCasting) {
                if (!_castPlayer?.isPaused) _castController?.playOrPause();
                return;
            }
            _audio?.pause();
        });
        navigator.mediaSession.setActionHandler('stop', () => stopPlayback());
        // Podcast queue uses _advancePodQueue, music uses _advanceQueue
        const isPod = _playing?.type === 'podcast';
        navigator.mediaSession.setActionHandler('nexttrack',
            (isPod ? _podQueue.length > 1 : _musicQueue.length > 1)
                ? () => { isPod ? _advancePodQueue() : _advanceQueue(); }
                : null);
        navigator.mediaSession.setActionHandler('previoustrack',
            (isPod ? _podQueueIdx > 0 : _musicQueue.length > 1)
                ? () => { isPod ? _prevPodQueue() : _changeStation(-1); }
                : null);
        const canSeek = item.type !== 'radio';
        navigator.mediaSession.setActionHandler('seekto', canSeek ? (d) => {
            if (_audio && isFinite(_audio.duration)) { _audio.currentTime = d.seekTime; }
        } : null);
        navigator.mediaSession.setActionHandler('seekbackward', canSeek ? (d) => {
            if (_audio) _audio.currentTime = Math.max(0, _audio.currentTime - (d.seekOffset || 10));
        } : null);
        navigator.mediaSession.setActionHandler('seekforward', canSeek ? (d) => {
            if (_audio) _audio.currentTime = Math.min(_audio.duration || Infinity, _audio.currentTime + (d.seekOffset || 10));
        } : null);
    }

    // Go back one episode in pod queue
    function _prevPodQueue() {
        if (_podQueueIdx > 0) { _podQueueIdx--; playAudio(_podQueue[_podQueueIdx]); }
    }

    /**
     * F-02 playContext — Spotify-like: click any track and the whole list becomes
     * the queue. The clicked track plays first, rest queues up silently.
     * @param {Array}  items    Full array of track objects (music/local type)
     * @param {number} startIdx Index of the track the user clicked
     */
    function playContext(items, startIdx = 0) {
        if (!items || !items.length) return;
        const idx = Math.max(0, Math.min(startIdx, items.length - 1));
        // Ensure every item has type set — items from music search may lack it
        const normalised = items.map(it => {
            if (!it.type) {
                // Infer type: YouTube/archive → music, local path → local
                const isYt = it.source === 'youtube' || (it.url && (it.url.includes('youtube.com') || it.url.includes('youtu.be')));
                return { ...it, type: isYt ? 'music' : (it.path ? 'local' : 'music') };
            }
            return it;
        });
        _musicQueue = normalised.slice();
        _musicQueueIdx = idx;
        _cl('info', 'playContext', { total: normalised.length, startIdx: idx, name: normalised[idx]?.name });
        playAudio(normalised[idx]);
    }

    /**
     * F-03 _crossfade — Double-buffer crossfade between two HTMLAudioElement instances.
     * Uses rAF for smooth volume ramp on the main thread.
     * @param {HTMLAudioElement} outAudio  Currently playing element (fades to 0)
     * @param {HTMLAudioElement} inAudio   Preloaded next element (fades to targetVol)
     * @param {number}           targetVol Final volume for inAudio (0–1)
     * @param {number}           durationMs Crossfade duration in ms (default 1500)
     * @param {Function}         onDone   Called after crossfade completes
     */
    function _crossfade(outAudio, inAudio, targetVol, durationMs = 1500, onDone) {
        if (!outAudio || !inAudio) { onDone?.(); return; }
        const startTime = performance.now();
        const startVol = outAudio.volume;
        inAudio.volume = 0;
        inAudio.play().catch(e => {
            _cl('warning', 'Crossfade inAudio.play failed', { error: e.message });
            onDone?.();
        });
        function tick(now) {
            const t = Math.min(1, (now - startTime) / durationMs);
            // Ease in/out for smoother transition
            const eased = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
            if (outAudio && !outAudio.paused) outAudio.volume = Math.max(0, startVol * (1 - eased));
            if (inAudio) inAudio.volume = Math.min(targetVol, targetVol * eased);
            if (t < 1) {
                requestAnimationFrame(tick);
            } else {
                try { outAudio.pause(); outAudio.src = ''; outAudio.load(); } catch(e) {}
                if (inAudio) inAudio.volume = targetVol;
                onDone?.();
            }
        }
        requestAnimationFrame(tick);
    }

    function _fmtTime(s) {
        if (!isFinite(s)) return '0:00';
        s = Math.floor(s);
        if (s >= 3600) return Math.floor(s / 3600) + ':' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
        return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    }

    /* ── Offline Archive ──────────────────────────────────────────────────── */

    /** Build archive button HTML for a YT music track. */
    function _archiveBtnHtml(url) {
        const entry = _archiveDb[url] || {};
        const st = entry.status || 'none';
        let iconClass = 'fa-cloud-arrow-down'; // default: not archived
        let btnClass = '';
        let title = t('Archiwizuj na NAS');
        if (st === 'downloading') { iconClass = 'fa-cloud'; btnClass = 'rm-arch-loading'; title = t('Pobieranie...') + ' ' + (entry.progress || 0) + '%'; }
        else if (st === 'done' && entry.phoneCache) { iconClass = 'fa-mobile-screen-button'; btnClass = 'rm-arch-phone'; title = t('Na NAS i na telefonie'); }
        else if (st === 'done') { iconClass = 'fa-cloud-check'; btnClass = 'rm-arch-nas'; title = t('Zarchiwizowano na NAS'); }
        else if (st === 'error') { iconClass = 'fa-cloud-exclamation'; btnClass = 'rm-arch-error'; title = entry.error || t('Błąd archiwizacji'); }

        const circumference = 87.96;
        const offset = st === 'downloading'
            ? circumference - (circumference * (entry.progress || 0) / 100)
            : circumference;

        return `<button class="rm-arch-btn ${btnClass}" data-arch-url="${escH(url)}" title="${escH(title)}">
            <i class="fas ${iconClass} rm-arch-icon"></i>
            <svg class="rm-arch-ring" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
                <circle cx="18" cy="18" r="14" fill="none" stroke="#1DB954" stroke-width="2.5"
                    stroke-dasharray="${circumference}" stroke-dashoffset="${offset}" stroke-linecap="round"/>
            </svg>
        </button>`;
    }

    /** Update archive button DOM element to reflect current _archiveDb state. */
    function _refreshArchiveBtn(url) {
        bodyEl.querySelectorAll(`.rm-arch-btn[data-arch-url="${CSS.escape(url)}"]`).forEach(btn => {
            const entry = _archiveDb[url] || {};
            const st = entry.status || 'none';
            btn.classList.remove('rm-arch-loading', 'rm-arch-nas', 'rm-arch-phone', 'rm-arch-error');
            const icon = btn.querySelector('.rm-arch-icon');
            if (!icon) return;
            const ring = btn.querySelector('.rm-arch-ring circle');
            if (st === 'downloading') {
                btn.classList.add('rm-arch-loading');
                btn.title = t('Pobieranie...') + ' ' + (entry.progress || 0) + '%';
                icon.className = 'fas fa-cloud rm-arch-icon';
                if (ring) {
                    const c = 87.96;
                    ring.style.strokeDashoffset = c - (c * (entry.progress || 0) / 100);
                }
            } else if (st === 'done' && entry.phoneCache) {
                btn.classList.add('rm-arch-phone');
                btn.title = t('Na NAS i na telefonie — dotknij aby usunąć');
                icon.className = 'fas fa-mobile-screen-button rm-arch-icon';
            } else if (st === 'done') {
                btn.classList.add('rm-arch-nas');
                btn.title = t('Zarchiwizowano na NAS — dotknij aby zapisać na telefon');
                icon.className = 'fas fa-cloud-check rm-arch-icon';
            } else if (st === 'error') {
                btn.classList.add('rm-arch-error');
                btn.title = entry.error || t('Błąd archiwizacji');
                icon.className = 'fas fa-cloud-exclamation rm-arch-icon';
            } else {
                btn.title = t('Archiwizuj na NAS');
                icon.className = 'fas fa-cloud-arrow-down rm-arch-icon';
            }
        });
    }

    /** Handle archive button click — cycles: none→NAS | NAS→phone | phone→delete menu */
    async function _onArchiveBtnClick(url, btnEl) {
        const entry = _archiveDb[url] || {};
        const st = entry.status || 'none';

        if (st === 'none' || st === 'error') {
            // Start NAS archive
            _archiveDb[url] = { ...(entry || {}), status: 'downloading', progress: 0 };
            _refreshArchiveBtn(url);
            const res = await api('/radio-music/archive/start', {
                method: 'POST',
                body: { url, title: entry.title || url, artist: entry.artist || '', thumbnail: entry.thumbnail || '' }
            });
            if (res.error) {
                _archiveDb[url] = { ..._archiveDb[url], status: 'error', error: res.error };
                _refreshArchiveBtn(url);
                toast(res.error, 'error');
            } else {
                _archiveDb[url] = { ..._archiveDb[url], key: res.key, status: res.status };
                _refreshArchiveBtn(url);
                if (res.status === 'done') toast(t('Już zarchiwizowane!'), 'success');
            }
            return;
        }

        if (st === 'downloading') {
            toast(t('Pobieranie w toku...') + ' ' + (entry.progress || 0) + '%', 'info');
            return;
        }

        if (st === 'done' && !entry.phoneCache) {
            // Cache to phone via SW
            if (!navigator.serviceWorker?.controller) {
                toast(t('Service Worker niedostępny'), 'error'); return;
            }
            toast(t('Zapisuję na telefon...'), 'info');
            const archiveUrl = `/api/radio-music/archive/file/${entry.key}?token=${NAS.token || ''}`;
            navigator.serviceWorker.controller.postMessage({
                type: 'RM_CACHE_AUDIO', key: entry.key, url: archiveUrl
            });
            // Response comes via SW message event in _initArchive()
            return;
        }

        if (st === 'done' && entry.phoneCache) {
            // Show delete menu
            _showArchiveDeleteMenu(url, entry, btnEl);
        }
    }

    function _showArchiveDeleteMenu(url, entry, anchor) {
        const existing = bodyEl.querySelector('.rm-arch-menu');
        if (existing) existing.remove();
        const menu = document.createElement('div');
        menu.className = 'rm-arch-menu';
        menu.style.cssText = 'position:fixed;background:#1e1e1e;border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:4px 0;z-index:9999;min-width:180px;box-shadow:0 4px 24px rgba(0,0,0,.5);font-size:13px;color:#fff';
        menu.innerHTML = `
            <button class="rm-arch-menu-item" data-action="del-phone"><i class="fas fa-mobile-screen-button" style="color:var(--rm-warning);width:18px"></i> ${t('Usuń z telefonu')}</button>
            <button class="rm-arch-menu-item" data-action="del-nas"><i class="fas fa-trash" style="color:var(--rm-error);width:18px"></i> ${t('Usuń z NAS')}</button>
            <button class="rm-arch-menu-item" data-action="cancel"><i class="fas fa-times" style="color:rgba(255,255,255,.4);width:18px"></i> ${t('Anuluj')}</button>`;
        const rect = anchor.getBoundingClientRect();
        menu.style.left = Math.min(rect.right, window.innerWidth - 190) + 'px';
        menu.style.top = (rect.bottom + 6) + 'px';
        menu.querySelectorAll('.rm-arch-menu-item').forEach(btn => {
            btn.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 14px;background:none;border:none;color:#fff;cursor:pointer;width:100%;text-align:left';
            btn.onmouseenter = () => { btn.style.background = 'rgba(255,255,255,.06)'; };
            btn.onmouseleave = () => { btn.style.background = ''; };
        });
        menu.querySelector('[data-action="del-phone"]').onclick = () => {
            menu.remove();
            navigator.serviceWorker?.controller?.postMessage({ type: 'RM_UNCACHE_AUDIO', key: entry.key });
            _archiveDb[url] = { ..._archiveDb[url], phoneCache: false };
            _refreshArchiveBtn(url);
            toast(t('Usunięto z telefonu'), 'success');
        };
        menu.querySelector('[data-action="del-nas"]').onclick = async () => {
            menu.remove();
            if (!entry.key) return;
            await api('/radio-music/archive/delete', { method: 'POST', body: { key: entry.key } });
            navigator.serviceWorker?.controller?.postMessage({ type: 'RM_UNCACHE_AUDIO', key: entry.key });
            delete _archiveDb[url];
            _refreshArchiveBtn(url);
            toast(t('Usunięto archiwum'), 'success');
        };
        menu.querySelector('[data-action="cancel"]').onclick = () => menu.remove();
        document.body.appendChild(menu);
        setTimeout(() => document.addEventListener('click', () => menu.remove(), { once: true }), 10);
    }

    /** Load archive status for an array of YT URLs (batch). */
    async function _loadArchiveBatch(urls) {
        if (!urls || !urls.length) return;
        const toFetch = urls.filter(u => u && !_archiveDb[u]);
        if (!toFetch.length) return;
        try {
            const res = await api('/radio-music/archive/batch', { method: 'POST', body: { urls: toFetch } });
            if (res.results) {
                for (const [url, info] of Object.entries(res.results)) {
                    if (info.status !== 'none') {
                        _archiveDb[url] = info;
                    }
                }
            }
        } catch (_) {}
    }

    /** Wire SocketIO listeners for archive progress/done/error events. */
    function _initArchive() {
        // Listen for SW messages (cache done/error)
        if (navigator.serviceWorker) {
            navigator.serviceWorker.ready.then(() => { _swReady = true; });
            navigator.serviceWorker.addEventListener('message', (evt) => {
                const { type, key } = evt.data || {};
                if (type === 'RM_CACHE_DONE' && key) {
                    // Find URL by key in _archiveDb
                    for (const [url, entry] of Object.entries(_archiveDb)) {
                        if (entry.key === key) {
                            _archiveDb[url] = { ...entry, phoneCache: true };
                            _refreshArchiveBtn(url);
                            break;
                        }
                    }
                    toast(t('Zapisano na telefon!'), 'success');
                }
                if (type === 'RM_CACHE_ERROR') {
                    toast(t('Błąd zapisu na telefon'), 'error');
                }
            });
        }

        // SocketIO events from backend
        if (NAS.socket) {
            NAS.socket.on('rm_archive_progress', (d) => {
                if (!d.url) return;
                _archiveDb[d.url] = { ..._archiveDb[d.url] || {}, key: d.key, status: 'downloading', progress: d.progress };
                _refreshArchiveBtn(d.url);
            });
            NAS.socket.on('rm_archive_done', (d) => {
                if (!d.url) return;
                _archiveDb[d.url] = { ..._archiveDb[d.url] || {}, key: d.key, status: 'done', progress: 100 };
                _refreshArchiveBtn(d.url);
                toast('✅ ' + (d.title || t('Utwór')) + ' — ' + t('zarchiwizowano na NAS'), 'success');
                // If this track is currently playing from YT, silently switch source to NAS
                if (_playing && _playing.url === d.url && _audio) {
                    const nasUrl = `/api/radio-music/archive/file/${d.key}?token=${NAS.token || ''}`;
                    const pos = _audio.currentTime;
                    const wasPlaying = !_audio.paused;
                    _audio.src = nasUrl;
                    _audio.currentTime = pos;
                    if (wasPlaying) _audio.play().catch(() => {});
                }
            });
            NAS.socket.on('rm_archive_error', (d) => {
                if (!d.url) return;
                _archiveDb[d.url] = { ..._archiveDb[d.url] || {}, status: 'error', error: d.error };
                _refreshArchiveBtn(d.url);
                toast(t('Błąd archiwizacji: ') + (d.error || ''), 'error');
            });
        }
    }

    /* ── Now Playing Overlay ──────────────────────── */

    /**
     * In-place NP overlay content update — no DOM rebuild, no layout shift.
     * Crossfades art (200ms) and fades title/meta (150ms) simultaneously.
     * Called when a new track starts while overlay is already open.
     */
    function _updateNowPlayingContent(item) {
        if (!_npOverlay || !item) return;

        const titleEl = _npOverlay.querySelector('.rm-np-title');
        const metaEl  = _npOverlay.querySelector('.rm-np-meta');
        const artEl   = _npOverlay.querySelector('#rm-np-art');
        const bgEl    = _npOverlay.querySelector('.rm-np-bg');

        // Step 1: fade out title + meta
        if (titleEl) titleEl.classList.add('rm-fading');
        if (metaEl)  metaEl.classList.add('rm-fading');

        // Step 2: skeleton art while new image loads (if >100ms)
        if (artEl) artEl.classList.add('rm-skeleton');

        // Fade out background
        if (bgEl) bgEl.style.opacity = '0';

        setTimeout(() => {
            // Step 3: swap text content while invisible
            if (titleEl) { titleEl.textContent = item.name || ''; titleEl.classList.remove('rm-fading'); }
            if (metaEl)  { metaEl.textContent  = item.meta || ''; metaEl.classList.remove('rm-fading'); }

            // Step 4: swap background
            if (bgEl) {
                const bgUrl = item.image || item.favicon || '';
                bgEl.style.backgroundImage = bgUrl ? `url('${escH(bgUrl)}')` : '';
                bgEl.style.opacity = '1';
            }

            // Step 5: crossfade art image
            if (artEl) {
                const isMusic = item.type === 'music' || item.type === 'local';
                const newSrc = item.image || item.thumbnail || null;

                if (isMusic && newSrc) {
                    // Pre-load new image; show skeleton until ready
                    const newImg = document.createElement('img');
                    newImg.className = 'rm-art-loading';
                    newImg.style.cssText = 'width:100%;height:100%;object-fit:cover;position:absolute;inset:0;opacity:0;transition:opacity 200ms ease';
                    let skeletonTimer = setTimeout(() => {
                        artEl.classList.add('rm-skeleton'); // show shimmer if slow
                    }, 100);
                    newImg.onload = () => {
                        clearTimeout(skeletonTimer);
                        artEl.classList.remove('rm-skeleton');
                        // Fade out old content, fade in new image
                        const oldChildren = [...artEl.children];
                        artEl.appendChild(newImg);
                        requestAnimationFrame(() => {
                            newImg.style.opacity = '1';
                            setTimeout(() => {
                                oldChildren.forEach(c => c.remove());
                                newImg.style.position = '';
                                newImg.style.inset = '';
                                newImg.className = 'rm-art-loaded';
                            }, 220);
                        });
                    };
                    newImg.onerror = () => {
                        clearTimeout(skeletonTimer);
                        artEl.classList.remove('rm-skeleton');
                        artEl.innerHTML = '<i class="fas fa-music"></i>';
                    };
                    newImg.src = newSrc;
                } else {
                    artEl.classList.remove('rm-skeleton');
                    if (newSrc) {
                        artEl.innerHTML = `<img src="${escH(newSrc)}" style="width:100%;height:100%;object-fit:cover" onerror="this.outerHTML='<i class=\\'fas fa-music\\'></i>'">`;
                    } else if (isMusic) {
                        artEl.innerHTML = '<i class="fas fa-music"></i>';
                    } else {
                        // Radio / podcast — station icon with logo manifest
                        const _fItem = { name: item.name, favicon: item.image || item.favicon, homepage: item.homepage || '', url: item.url, stationuuid: item.uuid || item.stationuuid || '' };
                        artEl.innerHTML = _stationIconHtml(_fItem);
                    }
                }
            }

            // Step 6: reset seekbar to 0 state cleanly (no jump — _seekLocked handles the live bar)
            const fill = _npOverlay.querySelector('#rm-np-fill');
            const cur  = _npOverlay.querySelector('#rm-np-cur');
            const dur  = _npOverlay.querySelector('#rm-np-dur');
            if (fill) fill.style.width = '0%';
            if (cur)  cur.textContent = '0:00';
            if (dur)  dur.textContent = '0:00';

            // Step 7: clear stale lyrics from previous track
            _stopLyricsSync();
            const lyrPanel = _npOverlay.querySelector('#rm-np-lyrics-panel');
            if (lyrPanel) { lyrPanel.innerHTML = ''; lyrPanel.classList.remove('rm-lyrics-visible'); }
            const lyrBtn = _npOverlay.querySelector('#rm-np-lyrics');
            if (lyrBtn) lyrBtn.classList.remove('rm-lyrics-active');

            // Step 8: sync NP favorite and download buttons to new track
            if (_npSyncFav) _npSyncFav();
            if (_npSyncDownload) _npSyncDownload();
            if (_npReloadSimilar) _npReloadSimilar();

            // Step 9: update MediaSession with new track
            _updateMediaSession();

        }, 160); // wait for fade-out transition to complete
    }

    function _showNowPlaying() {
        if (!_playing) return;

        // If overlay exists and is just minimized (same track), re-expand it — no DOM rebuild
        if (_npOverlay && _npOverlay.classList.contains('rm-np-minimized')) {
            _npOverlay.classList.remove('rm-np-minimized');
            _npOverlay.style.transform = '';
            // WAAPI slide-up: element at final position for hit-testing, visual-only animation
            _npOverlay.animate([
                { transform: 'translateY(100%)', opacity: 0 },
                { transform: 'translateY(0)', opacity: 1 }
            ], { duration: 380, easing: 'cubic-bezier(0.32,0.72,0,1)' });
            if (!history.state?.rmNpOpen) history.pushState({ rmNpOpen: true }, '');
            localStorage.setItem('rm_np_open', '1');
            const visCvs = _npOverlay.querySelector('#rm-np-vis');
            if (visCvs && _audio) _startVisualizer(visCvs);
            _npUpdateLoop();
            // Refresh queue panel if it's visible — content may be stale from a playlist switch
            // that happened while the overlay was minimized
            if (_renderNpQueueFn) {
                const qp = _npOverlay.querySelector('#rm-np-queue-panel');
                if (qp?.classList.contains('rm-np-queue-visible')) _renderNpQueueFn();
            }
            return;
        }

        _hideNowPlaying();
        let item = _playing;
        const isMusic = item.type === 'music';
        const isPodcast = item.type === 'podcast';
        const hasSeek = isMusic || isPodcast;
        const bgUrl = item.image || item.favicon || '';

        const ov = document.createElement('div');
        ov.className = 'rm-np-overlay';
        ov.innerHTML = `
            <div class="rm-np-bg" style="background-image:url('${escH(bgUrl)}')"></div>
            <button class="rm-np-close"><i class="fas fa-chevron-down"></i></button>
            <div class="rm-np-inner">
                <div class="rm-np-art" id="rm-np-art"></div>
                <div class="rm-np-info">
                    <div class="rm-np-title">${escH(item.name)}</div>
                    <div class="rm-np-meta">${escH(item.meta || '')}</div>
                </div>
                ${hasSeek ? `
                <div class="rm-np-seek">
                    <span class="rm-seek-time" id="rm-np-cur">0:00</span>
                    <div class="rm-seek-track" id="rm-np-track">
                        <div class="rm-seek-fill" id="rm-np-fill"></div>
                        <div class="rm-seek-thumb" id="rm-np-thumb"></div>
                    </div>
                    <span class="rm-seek-time right" id="rm-np-dur">0:00</span>
                </div>` : '<canvas id="rm-np-vis" class="rm-np-vis" width="260" height="48"></canvas>'}
                <div class="rm-np-controls">
                    <button class="rm-np-btn" id="rm-np-shuffle" title="${t('Losowo')}"><i class="fas fa-random"></i></button>
                    <button class="rm-np-btn" id="rm-np-prev"><i class="fas fa-step-backward"></i></button>
                    <button class="rm-np-btn rm-np-play" id="rm-np-playpause"><i class="fas ${_audio && !_audio.paused ? 'fa-pause' : 'fa-play'}"></i></button>
                    <button class="rm-np-btn" id="rm-np-next"><i class="fas fa-step-forward"></i></button>
                    <button class="rm-np-btn" id="rm-np-repeat" title="${t('Powtarzaj')}"><i class="fas fa-redo"></i></button>
                </div>
                <div class="rm-np-actions">
                    <button class="rm-np-action" id="rm-np-sleep"><i class="fas fa-moon"></i> ${t('Timer')}</button>
                    <button class="rm-speed-btn" id="rm-np-speed">${_playbackRate === 1 ? '1x' : _playbackRate + 'x'}</button>
                    <button class="rm-np-action" id="rm-np-queue-btn"><i class="fas fa-list-ol"></i> ${t('Kolejka')}</button>
                    <button class="rm-np-action" id="rm-np-lyrics"><i class="fas fa-align-left"></i> ${t('Tekst')}</button>
                    <button class="rm-np-action" id="rm-np-addpl"><i class="fas fa-plus"></i> ${t('Playlista')}</button>
                    <button class="rm-np-action rm-np-cast-action" id="rm-np-cast"><i class="fab fa-chromecast"></i> Chromecast</button>
                    <button class="rm-np-action" id="rm-np-fav"><i class="fas fa-heart"></i> ${t('Ulubione')}</button>
                    <button class="rm-np-action" id="rm-np-download"><i class="fas fa-cloud-arrow-down"></i> ${t('Pobierz')}</button>
                </div>
                <div class="rm-lyrics-panel" id="rm-np-lyrics-panel"></div>
                <div class="rm-np-queue" id="rm-np-queue-panel"></div>
                <div class="rm-np-similar" id="rm-np-similar-panel"></div>
            </div>`;

        // Art image
        const artContainer = ov.querySelector('#rm-np-art');
        if (isMusic && item.image) {
            artContainer.innerHTML = '<img src="' + escH(item.image) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">';
        } else if (item.image || item.favicon) {
            artContainer.innerHTML = '<img src="' + escH(item.image || item.favicon) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-broadcast-tower\\\'></i>\'">';
        } else {
            const letter = (item.name || '?')[0].toUpperCase();
            const hue = (item.name || '').split('').reduce((h, c) => h + c.charCodeAt(0), 0) % 360;
            artContainer.innerHTML = '<div class="rm-letter-icon" style="background:hsl(' + hue + ',50%,35%);display:flex;align-items:center;justify-content:center;font-size:64px;color:#fff">' + letter + '</div>';
        }

        // Close handler — minimize (CSS slide-down), not destroy
        ov.querySelector('.rm-np-close').onclick = () => _minimizeNowPlaying();

        // Favorite button — toggle radio favorite or liked song
        const favBtn = ov.querySelector('#rm-np-fav');
        function _syncNpFav() {
            if (!favBtn || !_playing) return;
            const isFav = _playing.type === 'radio'
                ? _favorites.some(f => f.uuid === (_playing.uuid || _playing.stationuuid))
                : _likedSongs.some(s => s.url === _playing.url);
            favBtn.classList.toggle('rm-lyrics-active', isFav);
            favBtn.innerHTML = isFav
                ? '<i class="fas fa-heart" style="color:var(--rm-accent)"></i> ' + t('Ulubione')
                : '<i class="far fa-heart"></i> ' + t('Ulubione');
        }
        if (favBtn) {
            favBtn.onclick = async () => {
                if (!_playing) return;
                if (_playing.type === 'radio') {
                    await toggleFavorite(_playing);
                } else {
                    await toggleLikedSong(_playing);
                }
                _syncNpFav();
            };
            _syncNpFav();
        }

        // Download/Archive button — trigger NAS archive for music tracks
        const dlBtn = ov.querySelector('#rm-np-download');
        function _syncNpDownload() {
            if (!dlBtn || !_playing) return;
            const isMusic = _playing.type === 'music';
            if (!isMusic) { dlBtn.style.display = 'none'; return; }
            dlBtn.style.display = '';
            const entry = _archiveDb[_playing.url] || {};
            const st = entry.status || 'none';
            if (st === 'done') {
                dlBtn.innerHTML = '<i class="fas fa-cloud-check" style="color:var(--rm-accent)"></i> ' + t('Pobrano');
                dlBtn.classList.add('rm-lyrics-active');
            } else if (st === 'downloading') {
                dlBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + (entry.progress || 0) + '%';
            } else {
                dlBtn.innerHTML = '<i class="fas fa-cloud-arrow-down"></i> ' + t('Pobierz');
                dlBtn.classList.remove('rm-lyrics-active');
            }
        }
        if (dlBtn) {
            dlBtn.onclick = () => {
                if (!_playing || !_playing.url) return;
                _onArchiveBtnClick(_playing.url, dlBtn);
                setTimeout(_syncNpDownload, 500);
            };
            _syncNpDownload();
        }
        // Expose sync functions for track changes
        _npSyncFav = _syncNpFav;
        _npSyncDownload = _syncNpDownload;

        // Sleep timer button
        const sleepBtn = ov.querySelector('#rm-np-sleep');
        if (sleepBtn) sleepBtn.onclick = (e) => { e.stopPropagation(); _showSleepDropdown(sleepBtn); };
        _syncSleepUi();

        // Playback speed button
        const speedBtn = ov.querySelector('#rm-np-speed');
        if (speedBtn) speedBtn.onclick = (e) => { e.stopPropagation(); _cycleSpeed(); };
        _syncSpeedUi();

        // Swipe-down to dismiss: ONLY on the art area (avoids conflict with buttons & scrolling)
        let _swStartY = 0, _swActive = false, _swMoving = false;
        const _npArt = ov.querySelector('#rm-np-art');
        _npArt.addEventListener('touchstart', (e) => {
            const t = e.touches[0];
            if (_npSeekDragging) return;
            _swStartY = t.clientY;
            _swActive = true;
            _swMoving = false;
        }, { passive: true });
        _npArt.addEventListener('touchmove', (e) => {
            if (!_swActive) return;
            const dy = Math.max(0, e.touches[0].clientY - _swStartY);
            if (!_swMoving) {
                if (dy < 12) return;
                _swMoving = true;
            }
            ov.style.transform = `translateY(${dy}px)`;
        }, { passive: true });
        _npArt.addEventListener('touchend', (e) => {
            if (!_swActive) return;
            _swActive = false;
            if (!_swMoving) return;
            const dy = e.changedTouches[0].clientY - _swStartY;
            if (dy > 100) {
                _minimizeNowPlaying();
            } else {
                // Snap back to position via WAAPI
                const curDy = Math.max(0, dy);
                ov.style.transform = '';
                ov.animate([
                    { transform: `translateY(${curDy}px)` },
                    { transform: 'translateY(0)' }
                ], { duration: 200, easing: 'ease-out' });
            }
        }, { passive: true });

        // Controls — optimistic: icon flips instantly, reverts only if play() rejects
        ov.querySelector('#rm-np-playpause').onclick = () => {
            if (!_audio) return;
            // Restored state: audio src not yet loaded — reinitialise from saved track
            if (!_audio.src && _playing) { playAudio(_playing); return; }
            // When casting, route to Chromecast — don't touch local (muted) audio
            if (_castTogglePlayPause()) return;
            const btn = ov.querySelector('#rm-np-playpause');
            const miniBtn = bodyEl?.querySelector('#rm-play-pause');
            if (_audio.paused) {
                // Optimistic: show Pause immediately
                btn.innerHTML = '<i class="fas fa-pause"></i>';
                if (miniBtn) miniBtn.innerHTML = '<i class="fas fa-pause"></i>';
                _audio.play().catch(err => {
                    if (err.name !== 'AbortError') { // AbortError = another src change interrupted, not a real failure
                        btn.innerHTML = '<i class="fas fa-play"></i>';
                        if (miniBtn) miniBtn.innerHTML = '<i class="fas fa-play"></i>';
                    }
                });
            } else {
                btn.innerHTML = '<i class="fas fa-play"></i>';
                if (miniBtn) miniBtn.innerHTML = '<i class="fas fa-play"></i>';
                _audio.pause();
            }
        };
        ov.querySelector('#rm-np-prev').onclick = () => _skipStation(-1);
        ov.querySelector('#rm-np-next').onclick = () => _skipStation(1);

        // Repeat/Shuffle in Now Playing
        function _syncNpRepeat() {
            const btn = ov.querySelector('#rm-np-repeat');
            if (!btn) return;
            btn.classList.toggle('rm-mode-active', _repeatMode > 0);
            btn.innerHTML = _repeatMode === 2 ? '<i class="fas fa-redo"></i><span style="font-size:9px;position:absolute;font-weight:700">1</span>' : '<i class="fas fa-redo"></i>';
            btn.style.position = _repeatMode === 2 ? 'relative' : '';
        }
        function _syncNpShuffle() {
            const btn = ov.querySelector('#rm-np-shuffle');
            if (btn) btn.classList.toggle('rm-mode-active', _shuffle);
        }
        ov.querySelector('#rm-np-repeat').onclick = () => {
            _repeatMode = (_repeatMode + 1) % 3;
            _syncNpRepeat();
            // Sync main player btn
            const mainBtn = bodyEl.querySelector('#rm-repeat-btn');
            if (mainBtn) {
                mainBtn.classList.toggle('rm-mode-active', _repeatMode > 0);
                mainBtn.innerHTML = _repeatMode === 2 ? '<i class="fas fa-redo"></i><span style="font-size:9px;position:absolute;font-weight:700">1</span>' : '<i class="fas fa-redo"></i>';
                mainBtn.style.position = _repeatMode === 2 ? 'relative' : '';
            }
        };
        ov.querySelector('#rm-np-shuffle').onclick = () => {
            _shuffle = !_shuffle;
            _syncNpShuffle();
            const mainBtn = bodyEl.querySelector('#rm-shuffle-btn');
            if (mainBtn) mainBtn.classList.toggle('rm-mode-active', _shuffle);
        };
        _syncNpRepeat();
        _syncNpShuffle();

        // Add to playlist — use live _playing
        ov.querySelector('#rm-np-addpl').onclick = () => {
            if (typeof _showAddToPlaylistModal === 'function' && _playing) _showAddToPlaylistModal(_playing);
        };

        // Lyrics toggle — always uses live _playing, not stale closure item
        ov.querySelector('#rm-np-lyrics').onclick = async () => {
            const lyrBtn = ov.querySelector('#rm-np-lyrics');
            const panel = ov.querySelector('#rm-np-lyrics-panel');
            // Close queue if open
            queuePanel.classList.remove('rm-np-queue-visible');
            queueBtn.classList.remove('rm-lyrics-active');

            if (panel.classList.contains('rm-lyrics-visible')) {
                panel.classList.remove('rm-lyrics-visible');
                lyrBtn.classList.remove('rm-lyrics-active');
                return;
            }
            lyrBtn.classList.add('rm-lyrics-active');
            panel.classList.add('rm-lyrics-visible');
            panel.innerHTML = '<div class="rm-lyrics-loading"><i class="fas fa-spinner fa-spin"></i> ' + t('Szukam tekstu...') + '</div>';

            const curItem = _playing;
            if (!curItem) { panel.innerHTML = '<div class="rm-lyrics-empty"><i class="fas fa-music"></i> ' + t('Nic nie gra') + '</div>'; return; }

            // Parse artist/title from name — YouTube titles are often "Artist - Title (Official Video)"
            let rawName = curItem.name || '';
            // Strip common YouTube suffixes
            let cleanName = rawName.replace(/\s*[\(\[](official\s*(video|audio|music\s*video|lyric\s*video|visualizer)|lyrics?|teledysk|audio|video|clip|hd|hq|4k|remastered|live)[\)\]]/gi, '').trim();

            let title = cleanName;
            let artist = '';
            if (cleanName.includes(' - ')) {
                const parts = cleanName.split(' - ');
                artist = parts[0].trim();
                title = parts.slice(1).join(' - ').trim();
            }
            // If no artist from name, try meta (YouTube channel)
            if (!artist && curItem.meta) artist = curItem.meta;

            // Try search with parsed artist+title first
            let data = await api('/radio-music/lyrics?title=' + encodeURIComponent(title) + '&artist=' + encodeURIComponent(artist));
            // Bail if track changed during fetch
            if (_playing !== curItem) return;
            // Fallback: try with just the clean name if not found
            if (!data.lyrics && title !== cleanName) {
                data = await api('/radio-music/lyrics?title=' + encodeURIComponent(cleanName) + '&artist=');
                if (_playing !== curItem) return;
            }
            if (data.lyrics) {
                _stopLyricsSync();
                const parsed = _parseLrc(data.syncedLyrics || '');
                if (parsed) {
                    _syncedLyrics = parsed;
                    _renderSyncedLyrics(panel, parsed);
                    _startLyricsSync(panel);
                } else {
                    panel.textContent = data.lyrics;
                }
            } else {
                panel.innerHTML = '<div class="rm-lyrics-empty"><i class="fas fa-music"></i> ' + t('Nie znaleziono tekstu') + '</div>';
            }
        };

        // Queue panel toggle
        const queueBtn = ov.querySelector('#rm-np-queue-btn');
        const queuePanel = ov.querySelector('#rm-np-queue-panel');
        function _renderNpQueue() {
            if (!_musicQueue.length) {
                queuePanel.innerHTML = '<div style="text-align:center;padding:20px;color:rgba(255,255,255,.3);font-size:13px"><i class="fas fa-list"></i> ' + t('Kolejka jest pusta') + '</div>';
                return;
            }
            let html = '<div class="rm-np-queue-header"><span>' + t('Kolejka') + ' (' + _musicQueue.length + ')</span></div>';
            _musicQueue.forEach((tr, idx) => {
                const isCurrent = idx === _musicQueueIdx;
                const name = tr.name || tr.title || '';
                const meta = tr.meta || tr.channel || '';
                const art = tr.image || tr.thumbnail || tr.favicon || '';
                html += '<div class="rm-np-q-item' + (isCurrent ? ' rm-q-current' : '') + '" data-idx="' + idx + '">'
                    + '<span class="rm-np-q-item-drag"><i class="fas fa-grip-vertical"></i></span>'
                    + '<span class="rm-np-q-item-idx">' + (isCurrent ? '<i class="fas fa-volume-up"></i>' : (idx + 1)) + '</span>'
                    + (art ? '<img class="rm-np-q-item-art" src="' + escH(art) + '" onerror="this.style.display=\'none\'">' : '')
                    + '<div class="rm-np-q-item-info"><div class="rm-np-q-item-title">' + escH(name) + '</div>'
                    + (meta ? '<div class="rm-np-q-item-meta">' + escH(meta) + '</div>' : '')
                    + '</div></div>';
            });
            queuePanel.innerHTML = html;
            // Scroll current into view
            const cur = queuePanel.querySelector('.rm-q-current');
            if (cur) cur.scrollIntoView({ block: 'center', behavior: 'smooth' });
            // Click to jump — Action Proxy: same as clicking the track in the list
            queuePanel.querySelectorAll('.rm-np-q-item').forEach(el => {
                el.onclick = () => {
                    const idx = parseInt(el.dataset.idx, 10);
                    if (isNaN(idx) || idx === _musicQueueIdx) return;
                    _musicQueueIdx = idx;
                    const tr = _musicQueue[idx];
                    // Overlay stays open — only content changes (R-02: no _hideNowPlaying here)
                    if (tr._plItem) {
                        _playTrackFromPlaylist(tr);
                    } else {
                        playMusicTrack(tr);
                    }
                    // Refresh NP queue panel highlight in-place
                    _renderNpQueue();
                };
            });
        }
        queueBtn.onclick = () => {
            // Close lyrics if open
            const lyrPanel = ov.querySelector('#rm-np-lyrics-panel');
            if (lyrPanel) lyrPanel.classList.remove('rm-lyrics-visible');
            ov.querySelector('#rm-np-lyrics').classList.remove('rm-lyrics-active');

            if (queuePanel.classList.contains('rm-np-queue-visible')) {
                queuePanel.classList.remove('rm-np-queue-visible');
                queueBtn.classList.remove('rm-lyrics-active');
                return;
            }
            queueBtn.classList.add('rm-lyrics-active');
            queuePanel.classList.add('rm-np-queue-visible');
            _renderNpQueue();
        };
        _setupQueueDnD(queuePanel);

        // Similar artists panel (lazy-loaded from Deezer)
        const simBtn = ov.querySelector('#rm-np-similar-btn');
        const simPanel = ov.querySelector('#rm-np-similar-panel');
        let _simLoaded = false;
        let _simArtist = '';
        let _simArtists = []; // cached list for queuing
        function _loadSimilarArtists() {
            const artist = (item.meta || item.artist || '').trim();
            if (!artist) {
                simPanel.innerHTML = '';
                return;
            }
            if (_simLoaded && _simArtist === artist) return;
            _simLoaded = true;
            _simArtist = artist;
            simPanel.innerHTML = '<div class="rm-np-similar-loading"><i class="fas fa-spinner fa-spin"></i> ' + t('Szukam podobnych...') + '</div>';
            api('/radio-music/similar-artists?artist=' + encodeURIComponent(artist)).then(data => {
                if (!data || !data.artists || !data.artists.length) {
                    simPanel.innerHTML = '';
                    return;
                }
                _simArtists = data.artists;
                let html = '<div class="rm-np-similar-header"><i class="fas fa-users"></i> ' + t('Podobni do') + ' ' + escH(artist) + '</div>';
                html += '<div class="rm-np-similar-scroll">';
                data.artists.forEach((a, idx) => {
                    const fans = a.nb_fan ? (a.nb_fan > 1000000 ? (a.nb_fan / 1000000).toFixed(1) + 'M' : a.nb_fan > 1000 ? Math.round(a.nb_fan / 1000) + 'K' : a.nb_fan) : '';
                    const pic = a.picture_medium || a.picture || '';
                    html += '<div class="rm-np-similar-card" data-name="' + escH(a.name) + '" data-idx="' + idx + '">'
                        + (pic ? '<img src="' + escH(pic) + '" loading="lazy" onerror="this.style.display=\'none\'">' : '<div style="width:80px;height:80px;border-radius:50%;background:rgba(255,255,255,.06);display:flex;align-items:center;justify-content:center;font-size:28px;color:rgba(255,255,255,.15)"><i class="fas fa-user"></i></div>')
                        + '<span class="rm-nps-name">' + escH(a.name) + '</span>'
                        + (fans ? '<span class="rm-nps-fans"><i class="fas fa-heart" style="font-size:8px;margin-right:2px"></i>' + fans + '</span>' : '')
                        + '</div>';
                });
                html += '</div>';
                simPanel.innerHTML = html;
                simPanel.querySelectorAll('.rm-np-similar-card').forEach(card => {
                    card.onclick = async () => {
                        const name = card.dataset.name;
                        const clickedIdx = parseInt(card.dataset.idx, 10);
                        if (!name) return;
                        card.style.opacity = '.5';
                        // Search for the clicked artist's music
                        const data = await api('/radio-music/music/search?q=' + encodeURIComponent(name) + '&limit=5');
                        if (!data || !data.items || !data.items.length) {
                            card.style.opacity = '1';
                            return;
                        }
                        // Play the first track
                        const first = data.items[0];
                        _musicQueue = data.items.map(tr => ({
                            id: tr.id, name: tr.title, url: tr.url, type: 'music',
                            meta: tr.channel, image: tr.thumbnail,
                            duration: tr.duration, source: tr.source || 'youtube',
                        }));
                        _musicQueueIdx = 0;
                        // Also queue tracks from remaining similar artists (background)
                        const remaining = _simArtists.filter((_, i) => i !== clickedIdx).slice(0, 6);
                        _queueSimilarArtists(remaining);
                        playAudio(_musicQueue[0]);
                    };
                });
            });
        }
        // Lazy queue: fetch one track per remaining similar artist and append to queue
        async function _queueSimilarArtists(artists) {
            for (const a of artists) {
                try {
                    const d = await api('/radio-music/music/search?q=' + encodeURIComponent(a.name) + '&limit=2');
                    if (d && d.items && d.items.length) {
                        const tr = d.items[0];
                        _musicQueue.push({
                            id: tr.id, name: tr.title, url: tr.url, type: 'music',
                            meta: tr.channel, image: tr.thumbnail,
                            duration: tr.duration, source: tr.source || 'youtube',
                        });
                        if (_renderNpQueueFn) _renderNpQueueFn();
                    }
                } catch (_) {}
            }
        }
        // Auto-load on open (for music/podcast tracks that have an artist)
        if (item.meta || item.artist) _loadSimilarArtists();
        // Expose for track-change refresh
        _npReloadSimilar = () => {
            _simLoaded = false;
            item = _playing; // update closure ref to new track
            _loadSimilarArtists();
        };

        // Cast button in NP overlay
        const npCastBtn = ov.querySelector('#rm-np-cast');
        if (npCastBtn) {
            npCastBtn.onclick = () => _toggleCast();
            if (_isCasting) npCastBtn.classList.add('rm-lyrics-active');
        }

        // Seekbar for overlay
        if (hasSeek) {
            const npTrack = ov.querySelector('#rm-np-track');
            const _seekTo = (pct) => {
                if (_audio && isFinite(_audio.duration)) {
                    _audio.currentTime = pct * _audio.duration;
                }
            };
            npTrack.onclick = (e) => {
                const rect = npTrack.getBoundingClientRect();
                _seekTo(Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)));
            };

            // Touch drag for seek thumb
            const npThumb = ov.querySelector('#rm-np-thumb');
            npThumb.addEventListener('touchstart', () => { _npSeekDragging = true; }, { passive: true });
            document.addEventListener('touchmove', (e) => {
                if (!_npSeekDragging) return;
                const rect = npTrack.getBoundingClientRect();
                _seekTo(Math.max(0, Math.min(1, (e.touches[0].clientX - rect.left) / rect.width)));
            }, { passive: true });
            document.addEventListener('touchend', () => { _npSeekDragging = false; }, { passive: true });
        }

        // Insert overlay — element at final position immediately (for hit-testing),
        // visual entrance via WAAPI so buttons are responsive from the start
        const wrap = bodyEl.querySelector('.rm-wrap');
        if (wrap) wrap.appendChild(ov);
        _npOverlay = ov;

        // WAAPI slide-up + optional FLIP art transition
        const miniArt = bodyEl.querySelector('#rm-player-art');
        const fullArt = ov.querySelector('#rm-np-art');
        ov.animate([
            { transform: 'translateY(100%)', opacity: 0 },
            { transform: 'translateY(0)', opacity: 1 }
        ], { duration: 380, easing: 'cubic-bezier(0.32,0.72,0,1)' });
        if (miniArt && fullArt) {
            requestAnimationFrame(() => {
                const from = miniArt.getBoundingClientRect();
                const to = fullArt.getBoundingClientRect();
                const dx = from.left - to.left;
                const dy = from.top - to.top;
                const sx = from.width / to.width;
                const sy = from.height / to.height;
                fullArt.animate([
                    { transform: `translate(${dx}px,${dy}px) scale(${sx},${sy})`, opacity: 0.7 },
                    { transform: 'translate(0,0) scale(1)', opacity: 1 }
                ], { duration: 340, easing: 'cubic-bezier(0.4,0,0.2,1)' });
            });
        }

        // Expose _renderNpQueue so the store subscriber can refresh it on Next/Prev
        _renderNpQueueFn = _renderNpQueue;

        // Push history state so Android back minimizes overlay instead of navigating away
        if (!history.state?.rmNpOpen) history.pushState({ rmNpOpen: true }, '');
        localStorage.setItem('rm_np_open', '1');

        // Auto-expand queue if there are queued tracks
        if (_musicQueue.length > 0) {
            queueBtn.classList.add('rm-lyrics-active');
            queuePanel.classList.add('rm-np-queue-visible');
            _renderNpQueue();
        }

        // Start visualizer for live radio (canvas), seekbar loop for music
        const visCvs = ov.querySelector('#rm-np-vis');
        if (visCvs && _audio) _startVisualizer(visCvs);
        _npUpdateLoop();
    }

    function _hideNowPlaying() {
        _stopVisualizer();
        if (_npOverlay) {
            _npOverlay.remove();
            _npOverlay = null;
        }
        _renderNpQueueFn = null;
        _npSyncFav = null;
        _npSyncDownload = null;
        _npReloadSimilar = null;
        localStorage.removeItem('rm_np_open');
    }

    // Minimize overlay to mini player (WAAPI slide-down, DOM kept alive)
    function _minimizeNowPlaying() {
        if (!_npOverlay || _npMinimizing) return;
        _npMinimizing = true;
        _stopVisualizer();
        const curTransform = _npOverlay.style.transform || 'translateY(0)';
        _npOverlay.style.transform = '';
        _npOverlay.animate([
            { transform: curTransform, opacity: 1 },
            { transform: 'translateY(100%)', opacity: 0 }
        ], { duration: 300, easing: 'cubic-bezier(0.4, 0, 1, 1)' }).onfinish = () => {
            _npOverlay.classList.add('rm-np-minimized');
            _npMinimizing = false;
        };
        localStorage.removeItem('rm_np_open');
        if (history.state?.rmNpOpen) history.back();
    }

    // Android back button handler — intercepts popstate to minimize overlay,
    // navigate between sections, and prevent exiting the app
    function _onPopState() {
        if (_npMinimizing) return;

        // 1. NP overlay open → minimize it
        if (_npOverlay && !_npOverlay.classList.contains('rm-np-minimized')) {
            _npMinimizing = true;
            _stopVisualizer();
            _npOverlay.style.transform = '';
            _npOverlay.animate([
                { transform: 'translateY(0)', opacity: 1 },
                { transform: 'translateY(100%)', opacity: 0 }
            ], { duration: 300, easing: 'cubic-bezier(0.4, 0, 1, 1)' }).onfinish = () => {
                _npOverlay.classList.add('rm-np-minimized');
                _npMinimizing = false;
            };
            localStorage.removeItem('rm_np_open');
            // Re-push sentinel so next back press is also caught
            if (!history.state?.rmApp) history.pushState({ rmApp: true }, '');
            return;
        }

        // 2. "More" sheet open → close it
        if (bodyEl) {
            const sheet = bodyEl.querySelector('#rm-more-sheet');
            if (sheet && sheet.classList.contains('open')) {
                sheet.classList.remove('open');
                if (!history.state?.rmApp) history.pushState({ rmApp: true }, '');
                return;
            }
        }

        // 3. Not on home section → go back to home
        if (activeSection !== 'most-played') {
            _navTo('most-played');
            if (!history.state?.rmApp) history.pushState({ rmApp: true }, '');
            return;
        }

        // 4. Already on home → re-push sentinel to prevent app exit
        if (!history.state?.rmApp) history.pushState({ rmApp: true }, '');
    }

    function _startVisualizer(canvas) {
        _stopVisualizer();
        try {
            // Create AudioContext once; reuse across plays
            if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            // Connect current _audio element (only if not already connected)
            if (!_audioSource || _audioSource.mediaElement !== _audio) {
                try { if (_audioSource) _audioSource.disconnect(); } catch(e) {}
                _audioSource = _audioCtx.createMediaElementSource(_audio);
                _analyser = _audioCtx.createAnalyser();
                _analyser.fftSize = 64;
                _audioSource.connect(_analyser);
                _analyser.connect(_audioCtx.destination);
            }
            if (_audioCtx.state === 'suspended') _audioCtx.resume();
        } catch(e) {
            _cl('warning', 'AudioContext visualizer error', { err: e?.message });
            canvas.style.display = 'none';
            return;
        }
        const ctx = canvas.getContext('2d');
        const buf = new Uint8Array(_analyser.frequencyBinCount);
        const W = canvas.width, H = canvas.height;
        const BAR_COUNT = 24, BAR_GAP = 2;
        const barW = Math.floor((W - BAR_GAP * (BAR_COUNT - 1)) / BAR_COUNT);

        function draw() {
            _visRafId = requestAnimationFrame(draw);
            _analyser.getByteFrequencyData(buf);
            ctx.clearRect(0, 0, W, H);
            const step = Math.floor(buf.length / BAR_COUNT);
            for (let i = 0; i < BAR_COUNT; i++) {
                const val = buf[i * step] / 255;
                const bH = Math.max(3, val * H);
                const x = i * (barW + BAR_GAP);
                const g = ctx.createLinearGradient(0, H, 0, H - bH);
                g.addColorStop(0, '#1DB954');
                g.addColorStop(1, '#4ade80');
                ctx.fillStyle = g;
                ctx.beginPath();
                ctx.roundRect(x, H - bH, barW, bH, 2);
                ctx.fill();
            }
        }
        draw();
    }

    function _stopVisualizer() {
        if (_visRafId) { cancelAnimationFrame(_visRafId); _visRafId = null; }
    }

    function _showLockScreen() {
        if (_lockOverlay) return;
        const item = _playing;
        if (!item) return;

        const ov = document.createElement('div');
        ov.className = 'rm-lock-overlay';
        const artSrc = item.image || item.favicon || '';
        ov.innerHTML = `
            <div class="rm-lock-icon"><i class="fas fa-lock"></i></div>
            <div class="rm-lock-art">${artSrc ? '<img src="' + escH(artSrc) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">' : '<i class="fas fa-music"></i>'}</div>
            <div class="rm-lock-title">${escH(item.name)}</div>
            <div class="rm-lock-meta">${escH(item.meta || '')}</div>
            <div class="rm-lock-hint"><i class="fas fa-chevron-up"></i> ${t('Przesuń w górę aby odblokować')}</div>`;

        // Block ALL touch events except our swipe detector
        ov.addEventListener('touchstart', (e) => e.preventDefault(), { passive: false });
        ov.addEventListener('touchmove', (e) => e.preventDefault(), { passive: false });
        ov.addEventListener('click', (e) => e.stopPropagation(), true);

        // Swipe-up to unlock — requires deliberate 120px+ upward swipe
        let startY = 0;
        let tracking = false;
        ov.addEventListener('touchstart', (e) => {
            startY = e.touches[0].clientY;
            tracking = true;
        });
        ov.addEventListener('touchend', (e) => {
            if (!tracking) return;
            tracking = false;
            const dy = startY - e.changedTouches[0].clientY;
            if (dy > 120) _hideLockScreen();
        });

        document.body.appendChild(ov);
        _lockOverlay = ov;
    }

    function _hideLockScreen() {
        if (_lockOverlay) {
            _lockOverlay.remove();
            _lockOverlay = null;
        }
    }

    function _showAddToHomescreenPrompt() {
        // Open dedicated install page — it has its own manifest so Chrome
        // treats Radio & Music as a separate installable PWA
        window.open(window.location.origin + '/music.html', '_blank');
    }

    function _npUpdateLoop() {
        if (!_npOverlay || !_audio) return;
        // Skip DOM updates when overlay is minimized (invisible)
        if (_npOverlay.classList.contains('rm-np-minimized')) {
            setTimeout(() => _npUpdateLoop(), 500);
            return;
        }
        const dur = _audio.duration;
        const cur = _audio.currentTime;
        if (isFinite(dur) && dur > 0) {
            const pct = (cur / dur) * 100;
            const fill = _npOverlay.querySelector('#rm-np-fill');
            const thumb = _npOverlay.querySelector('#rm-np-thumb');
            const curEl = _npOverlay.querySelector('#rm-np-cur');
            const durEl = _npOverlay.querySelector('#rm-np-dur');
            if (fill) fill.style.width = pct + '%';
            if (thumb) thumb.style.left = pct + '%';
            if (curEl) curEl.textContent = _fmtTime(cur);
            if (durEl) durEl.textContent = _fmtTime(dur);
        }
        // Sync play/pause icon
        const ppBtn = _npOverlay.querySelector('#rm-np-playpause');
        if (ppBtn && _audio) {
            ppBtn.innerHTML = '<i class="fas ' + (_audio.paused ? 'fa-play' : 'fa-pause') + '"></i>';
        }
        // 4fps is enough for time display (changes every second) — avoids 60fps DOM thrash
        setTimeout(() => _npUpdateLoop(), 250);
    }

    function _syncNowPlaying() {
        // Update overlay info if track changed
        if (!_npOverlay || !_playing) return;
        _npOverlay.querySelector('.rm-np-title').textContent = _playing.name;
        _npOverlay.querySelector('.rm-np-meta').textContent = _playing.meta || '';
        const bgUrl = _playing.image || _playing.favicon || '';
        _npOverlay.querySelector('.rm-np-bg').style.backgroundImage = "url('" + escH(bgUrl) + "')";
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
