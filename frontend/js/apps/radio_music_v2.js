/* eslint-disable */
/**
 * Radio & Music v2 — Lightweight rewrite
 * CSS prefix: rm2-
 */
AppRegistry['radio-music-v2'] = function(appDef, launchOpts) {

    let bodyEl, activeSection = 'search', _audio = null, _playing = null;
    let _favorites = [], _playlists = [], _queue = [], _queueIdx = -1;
    let _history = [], _repeatMode = 0, _shuffle = false;
    let _countries = [], _tags = [];
    
    const API_BASE = '/api/radio-music-v2';
    
    // Helper: API call wrapper
    const api = (endpoint, options = {}) => {
        return NAS.api(API_BASE + endpoint, options);
    };
    
    // Helper: Toast notification
    const toast = (msg, type = 'info') => {
        if (typeof NAS !== 'undefined' && NAS.toast) {
            NAS.toast(msg, type);
        }
    };
    
    // Helper: Format seconds to MM:SS
    const formatTime = (sec) => {
        if (!sec || isNaN(sec)) return '0:00';
        const m = Math.floor(sec / 60);
        const s = Math.floor(sec % 60);
        return `${m}:${s.toString().padStart(2, '0')}`;
    };
    
    // ── HTML Structure ─────────────────────────────────────────────────
    
    const buildHTML = () => `
        <style>
            .rm2-container {
                display: flex;
                height: 100%;
                background: #121212;
                color: #fff;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            }
            
            /* Sidebar */
            .rm2-sidebar {
                width: 240px;
                background: #000;
                padding: 1rem;
                overflow-y: auto;
                border-right: 1px solid #282828;
            }
            
            .rm2-logo {
                font-size: 1.5rem;
                font-weight: bold;
                margin-bottom: 2rem;
                padding-left: 0.5rem;
            }
            
            .rm2-section {
                margin-bottom: 2rem;
            }
            
            .rm2-section h3 {
                font-size: 0.75rem;
                text-transform: uppercase;
                color: #aaa;
                margin-bottom: 0.75rem;
                padding-left: 0.5rem;
            }
            
            .rm2-section ul {
                list-style: none;
                padding: 0;
                margin: 0;
            }
            
            .rm2-section li {
                padding: 0.75rem;
                cursor: pointer;
                border-radius: 4px;
                transition: background 0.2s;
                display: flex;
                align-items: center;
                gap: 0.75rem;
            }
            
            .rm2-section li:hover { background: #282828; }
            .rm2-section li.active { background: #1db954; color: #fff; }
            
            /* Main Content */
            .rm2-content {
                flex: 1;
                padding: 2rem;
                padding-bottom: 120px;
                overflow-y: auto;
            }
            
            .rm2-header {
                margin-bottom: 2rem;
            }
            
            .rm2-header h1 {
                font-size: 2rem;
                margin-bottom: 1rem;
            }
            
            .rm2-search-bar {
                width: 100%;
                max-width: 600px;
                margin-bottom: 2rem;
            }
            
            .rm2-search-bar input {
                width: 100%;
                padding: 0.75rem 1rem;
                border: none;
                border-radius: 24px;
                background: #fff;
                color: #000;
                font-size: 0.95rem;
            }
            
            /* Grid Layout */
            .rm2-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
                gap: 1.5rem;
            }
            
            .rm2-card {
                background: #181818;
                border-radius: 8px;
                padding: 1rem;
                cursor: pointer;
                transition: background 0.2s, transform 0.2s;
            }
            
            .rm2-card:hover {
                background: #282828;
                transform: translateY(-4px);
            }
            
            .rm2-card img {
                width: 100%;
                aspect-ratio: 1;
                object-fit: cover;
                border-radius: 4px;
                margin-bottom: 0.75rem;
            }
            
            .rm2-card h4 {
                font-size: 0.95rem;
                margin-bottom: 0.5rem;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            
            .rm2-card p {
                font-size: 0.85rem;
                color: #aaa;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            
            /* Player Bar */
            .rm2-player {
                position: fixed;
                bottom: 0;
                left: 0;
                right: 0;
                height: 90px;
                background: #181818;
                border-top: 1px solid #282828;
                display: flex;
                align-items: center;
                padding: 0 1rem;
                z-index: 1000;
                gap: 1rem;
            }
            
            .rm2-player-left {
                display: flex;
                align-items: center;
                gap: 1rem;
                min-width: 200px;
            }
            
            .rm2-cover {
                width: 56px;
                height: 56px;
                border-radius: 4px;
                object-fit: cover;
                background: #282828;
            }
            
            .rm2-track-info {
                flex: 1;
            }
            
            .rm2-title {
                font-size: 0.95rem;
                margin-bottom: 0.25rem;
                font-weight: 500;
            }
            
            .rm2-artist {
                font-size: 0.85rem;
                color: #aaa;
            }
            
            .rm2-player-center {
                flex: 1;
                display: flex;
                flex-direction: column;
                gap: 0.5rem;
            }
            
            .rm2-controls {
                display: flex;
                justify-content: center;
                align-items: center;
                gap: 1rem;
            }
            
            .rm2-controls button {
                background: none;
                border: none;
                color: #fff;
                font-size: 1.25rem;
                cursor: pointer;
                padding: 0.5rem;
                border-radius: 50%;
                transition: background 0.2s;
            }
            
            .rm2-controls button:hover {
                background: #282828;
            }
            
            .rm2-btn-play {
                font-size: 2rem !important;
            }
            
            .rm2-progress-bar {
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }
            
            .rm2-time-current, .rm2-time-total {
                font-size: 0.75rem;
                color: #aaa;
                min-width: 40px;
            }
            
            .rm2-seekbar {
                flex: 1;
                height: 4px;
                appearance: none;
                background: #282828;
                border-radius: 2px;
                outline: none;
            }
            
            .rm2-seekbar::-webkit-slider-thumb {
                appearance: none;
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background: #1db954;
                cursor: pointer;
            }
            
            .rm2-player-right {
                display: flex;
                align-items: center;
                gap: 1rem;
                min-width: 200px;
                justify-content: flex-end;
            }
            
            .rm2-volume {
                width: 100px;
                height: 4px;
                appearance: none;
                background: #282828;
                border-radius: 2px;
                outline: none;
            }
            
            .rm2-volume::-webkit-slider-thumb {
                appearance: none;
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background: #fff;
                cursor: pointer;
            }
            
            /* List View (for history, playlists) */
            .rm2-list {
                background: #181818;
                border-radius: 8px;
                padding: 1rem;
            }
            
            .rm2-list-item {
                display: flex;
                align-items: center;
                padding: 0.75rem;
                border-radius: 4px;
                cursor: pointer;
                transition: background 0.2s;
                gap: 1rem;
            }
            
            .rm2-list-item:hover {
                background: #282828;
            }
            
            .rm2-list-item img {
                width: 48px;
                height: 48px;
                border-radius: 4px;
                object-fit: cover;
            }
            
            .rm2-list-item-info {
                flex: 1;
            }
            
            .rm2-list-item-title {
                font-size: 0.95rem;
                margin-bottom: 0.25rem;
            }
            
            .rm2-list-item-subtitle {
                font-size: 0.85rem;
                color: #aaa;
            }
            
            /* Mobile Responsive */
            @media (max-width: 768px) {
                .rm2-sidebar {
                    display: none;
                }
                
                .rm2-content {
                    padding: 1rem;
                    padding-bottom: 160px;
                }
                
                .rm2-grid {
                    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
                    gap: 1rem;
                }
                
                .rm2-player {
                    height: 120px;
                    flex-wrap: wrap;
                }
                
                .rm2-player-left {
                    width: 100%;
                }
                
                .rm2-player-center {
                    width: 100%;
                }
                
                .rm2-player-right {
                    display: none;
                }
            }
            
            /* Loading Spinner */
            .rm2-loading {
                text-align: center;
                padding: 3rem;
                color: #aaa;
            }
            
            /* Empty State */
            .rm2-empty {
                text-align: center;
                padding: 3rem;
                color: #aaa;
            }
        </style>
        
        <div class="rm2-container">
            <!-- Sidebar -->
            <aside class="rm2-sidebar">
                <div class="rm2-logo">🎵 Radio & Music</div>
                
                <div class="rm2-section">
                    <h3>Library</h3>
                    <ul>
                        <li data-section="search" class="active">
                            <i class="fas fa-search"></i> Search
                        </li>
                        <li data-section="playlists">
                            <i class="fas fa-list"></i> Playlists
                        </li>
                        <li data-section="history">
                            <i class="fas fa-history"></i> History
                        </li>
                    </ul>
                </div>
                
                <div class="rm2-section">
                    <h3>Radio</h3>
                    <ul>
                        <li data-section="radio-top">
                            <i class="fas fa-star"></i> Top Stations
                        </li>
                        <li data-section="radio-favorites">
                            <i class="fas fa-heart"></i> Favorites
                        </li>
                        <li data-section="radio-countries">
                            <i class="fas fa-globe"></i> Countries
                        </li>
                        <li data-section="radio-tags">
                            <i class="fas fa-tags"></i> Genres
                        </li>
                    </ul>
                </div>
                
                <div class="rm2-section">
                    <h3>Podcasts</h3>
                    <ul>
                        <li data-section="podcasts-search">
                            <i class="fas fa-podcast"></i> Search
                        </li>
                        <li data-section="podcasts-favorites">
                            <i class="fas fa-rss"></i> Subscriptions
                        </li>
                    </ul>
                </div>
            </aside>
            
            <!-- Main Content -->
            <main class="rm2-content">
                <div class="rm2-header">
                    <h1 id="rm2-page-title">Search</h1>
                </div>
                
                <div id="rm2-main-content"></div>
            </main>
            
            <!-- Player Bar -->
            <div class="rm2-player">
                <div class="rm2-player-left">
                    <img class="rm2-cover" id="rm2-player-cover" src="" alt="" />
                    <div class="rm2-track-info">
                        <div class="rm2-title" id="rm2-player-title">No track playing</div>
                        <div class="rm2-artist" id="rm2-player-artist"></div>
                    </div>
                </div>
                
                <div class="rm2-player-center">
                    <div class="rm2-controls">
                        <button id="rm2-btn-prev" title="Previous">
                            <i class="fas fa-step-backward"></i>
                        </button>
                        <button id="rm2-btn-play" class="rm2-btn-play" title="Play">
                            <i class="fas fa-play"></i>
                        </button>
                        <button id="rm2-btn-next" title="Next">
                            <i class="fas fa-step-forward"></i>
                        </button>
                        <button id="rm2-btn-shuffle" title="Shuffle">
                            <i class="fas fa-random"></i>
                        </button>
                        <button id="rm2-btn-repeat" title="Repeat">
                            <i class="fas fa-redo"></i>
                        </button>
                    </div>
                    <div class="rm2-progress-bar">
                        <span class="rm2-time-current" id="rm2-time-current">0:00</span>
                        <input type="range" class="rm2-seekbar" id="rm2-seekbar" value="0" min="0" max="100" />
                        <span class="rm2-time-total" id="rm2-time-total">0:00</span>
                    </div>
                </div>
                
                <div class="rm2-player-right">
                    <button id="rm2-btn-queue" title="Queue">
                        <i class="fas fa-list-ol"></i>
                    </button>
                    <input type="range" class="rm2-volume" id="rm2-volume" value="100" min="0" max="100" />
                </div>
            </div>
        </div>
        
        <!-- Hidden audio element -->
        <audio id="rm2-audio"></audio>
    `;
    
    // ── Player Logic ───────────────────────────────────────────────────
    
    const initPlayer = () => {
        _audio = bodyEl.querySelector('#rm2-audio');
        
        // Play/Pause
        bodyEl.querySelector('#rm2-btn-play').addEventListener('click', () => {
            if (_audio.paused) {
                _audio.play();
            } else {
                _audio.pause();
            }
        });
        
        // Update play button icon
        _audio.addEventListener('play', () => {
            bodyEl.querySelector('#rm2-btn-play i').className = 'fas fa-pause';
        });
        
        _audio.addEventListener('pause', () => {
            bodyEl.querySelector('#rm2-btn-play i').className = 'fas fa-play';
        });
        
        // Time update
        _audio.addEventListener('timeupdate', () => {
            const current = _audio.currentTime;
            const duration = _audio.duration;
            
            if (duration > 0) {
                bodyEl.querySelector('#rm2-time-current').textContent = formatTime(current);
                bodyEl.querySelector('#rm2-time-total').textContent = formatTime(duration);
                bodyEl.querySelector('#rm2-seekbar').value = (current / duration) * 100;
            }
        });
        
        // Seek
        bodyEl.querySelector('#rm2-seekbar').addEventListener('input', (e) => {
            const seekTo = (_audio.duration * e.target.value) / 100;
            _audio.currentTime = seekTo;
        });
        
        // Volume
        bodyEl.querySelector('#rm2-volume').addEventListener('input', (e) => {
            _audio.volume = e.target.value / 100;
        });
        
        // Next/Prev
        bodyEl.querySelector('#rm2-btn-next').addEventListener('click', () => playNext());
        bodyEl.querySelector('#rm2-btn-prev').addEventListener('click', () => playPrev());
        
        // Auto-advance on track end
        _audio.addEventListener('ended', () => {
            if (_repeatMode === 2) {
                _audio.currentTime = 0;
                _audio.play();
            } else {
                playNext();
            }
        });
        
        // Repeat mode
        bodyEl.querySelector('#rm2-btn-repeat').addEventListener('click', () => {
            _repeatMode = (_repeatMode + 1) % 3;
            const btn = bodyEl.querySelector('#rm2-btn-repeat');
            if (_repeatMode === 0) {
                btn.style.color = '#fff';
                btn.title = 'Repeat';
            } else if (_repeatMode === 1) {
                btn.style.color = '#1db954';
                btn.title = 'Repeat All';
            } else {
                btn.style.color = '#1db954';
                btn.innerHTML = '<i class="fas fa-redo"></i> <small>1</small>';
                btn.title = 'Repeat One';
            }
        });
        
        // Shuffle
        bodyEl.querySelector('#rm2-btn-shuffle').addEventListener('click', () => {
            _shuffle = !_shuffle;
            const btn = bodyEl.querySelector('#rm2-btn-shuffle');
            btn.style.color = _shuffle ? '#1db954' : '#fff';
        });
    };
    
    const playTrack = (track, addToHistory = true) => {
        _playing = track;
        
        // Update player UI
        bodyEl.querySelector('#rm2-player-title').textContent = track.title || 'Unknown';
        bodyEl.querySelector('#rm2-player-artist').textContent = track.artist || track.country || '';
        bodyEl.querySelector('#rm2-player-cover').src = track.thumbnail || track.favicon || track.artwork || '/img/music-default.png';
        
        // Set audio source
        if (track.type === 'youtube') {
            _audio.src = `${API_BASE}/yt/stream?url=${encodeURIComponent(track.url)}`;
        } else {
            _audio.src = track.url;
        }
        
        _audio.play();
        
        // Add to history
        if (addToHistory) {
            api('/history', {
                method: 'POST',
                body: {
                    type: track.type,
                    title: track.title,
                    data: track
                }
            });
        }
    };
    
    const playNext = () => {
        if (_queue.length === 0) return;
        
        if (_shuffle) {
            _queueIdx = Math.floor(Math.random() * _queue.length);
        } else {
            _queueIdx = (_queueIdx + 1) % _queue.length;
        }
        
        playTrack(_queue[_queueIdx]);
    };
    
    const playPrev = () => {
        if (_queue.length === 0) return;
        
        _queueIdx = (_queueIdx - 1 + _queue.length) % _queue.length;
        playTrack(_queue[_queueIdx]);
    };
    
    const addToQueue = (track) => {
        _queue.push(track);
        toast('Added to queue');
    };
    
    // ── Sections ───────────────────────────────────────────────────────
    
    const renderSearch = () => {
        const html = `
            <div class="rm2-search-bar">
                <input type="text" id="rm2-search-input" placeholder="Search music, radio, podcasts..." />
            </div>
            <div id="rm2-search-results" class="rm2-grid"></div>
        `;
        
        bodyEl.querySelector('#rm2-main-content').innerHTML = html;
        
        const searchInput = bodyEl.querySelector('#rm2-search-input');
        searchInput.addEventListener('input', async (e) => {
            const query = e.target.value.trim();
            if (query.length < 2) return;
            
            // Search YouTube by default
            const results = await api('/yt/search?q=' + encodeURIComponent(query)).then(r => r.json());
            
            const grid = bodyEl.querySelector('#rm2-search-results');
            grid.innerHTML = results.map(v => `
                <div class="rm2-card" onclick="window._rm2_playYoutube('${v.url}', '${v.title.replace(/'/g, "\\'")}', '${v.channel.replace(/'/g, "\\'")}', '${v.thumbnail}')">
                    <img src="${v.thumbnail}" alt="${v.title}" />
                    <h4>${v.title}</h4>
                    <p>${v.channel}</p>
                </div>
            `).join('');
        });
        
        searchInput.focus();
    };
    
    const renderRadioTop = async () => {
        bodyEl.querySelector('#rm2-main-content').innerHTML = '<div class="rm2-loading">Loading top stations...</div>';
        
        const stations = await api('/radio/top?limit=50').then(r => r.json());
        
        const html = `
            <div class="rm2-grid">
                ${stations.map(s => `
                    <div class="rm2-card" onclick="window._rm2_playRadio('${s.url}', '${s.name.replace(/'/g, "\\'")}', '${s.country}', '${s.favicon}')">
                        <img src="${s.favicon || '/img/radio-default.png'}" alt="${s.name}" />
                        <h4>${s.name}</h4>
                        <p>${s.country}</p>
                    </div>
                `).join('')}
            </div>
        `;
        
        bodyEl.querySelector('#rm2-main-content').innerHTML = html;
    };
    
    const renderHistory = async () => {
        bodyEl.querySelector('#rm2-main-content').innerHTML = '<div class="rm2-loading">Loading history...</div>';
        
        _history = await api('/history?limit=50').then(r => r.json());
        
        if (_history.length === 0) {
            bodyEl.querySelector('#rm2-main-content').innerHTML = '<div class="rm2-empty">No playback history yet</div>';
            return;
        }
        
        const html = `
            <div class="rm2-list">
                ${_history.map(item => `
                    <div class="rm2-list-item" onclick="window._rm2_playFromHistory(${item.id})">
                        <img src="${item.data.thumbnail || item.data.favicon || item.data.artwork || '/img/music-default.png'}" />
                        <div class="rm2-list-item-info">
                            <div class="rm2-list-item-title">${item.title}</div>
                            <div class="rm2-list-item-subtitle">${item.data.artist || item.data.country || item.type}</div>
                        </div>
                    </div>
                `).join('')}
            </div>
        `;
        
        bodyEl.querySelector('#rm2-main-content').innerHTML = html;
    };
    
    const renderPlaylists = async () => {
        bodyEl.querySelector('#rm2-main-content').innerHTML = '<div class="rm2-loading">Loading playlists...</div>';
        
        _playlists = await api('/playlists').then(r => r.json());
        
        const html = `
            <button onclick="window._rm2_createPlaylist()" style="margin-bottom: 1rem; padding: 0.75rem 1.5rem; background: #1db954; border: none; border-radius: 24px; color: #fff; cursor: pointer;">
                <i class="fas fa-plus"></i> New Playlist
            </button>
            <div class="rm2-grid">
                ${_playlists.map(p => `
                    <div class="rm2-card" onclick="window._rm2_openPlaylist(${p.id})">
                        <img src="/img/playlist-default.png" alt="${p.name}" />
                        <h4>${p.name}</h4>
                        <p>${p.tracks.length} tracks</p>
                    </div>
                `).join('')}
            </div>
        `;
        
        bodyEl.querySelector('#rm2-main-content').innerHTML = html || '<div class="rm2-empty">No playlists yet</div>';
    };
    
    // ── Global Helper Functions (called from onclick) ─────────────────
    
    window._rm2_playYoutube = (url, title, artist, thumbnail) => {
        const track = { type: 'youtube', url, title, artist, thumbnail };
        playTrack(track);
    };
    
    window._rm2_playRadio = (url, name, country, favicon) => {
        const track = { type: 'radio', url, title: name, country, favicon };
        playTrack(track);
    };
    
    window._rm2_playFromHistory = (itemId) => {
        const item = _history.find(h => h.id === itemId);
        if (item) {
            playTrack(item.data, false);
        }
    };
    
    window._rm2_createPlaylist = async () => {
        const name = prompt('Playlist name:');
        if (!name) return;
        
        const result = await api('/playlists', {
            method: 'POST',
            body: { name, tracks: [] }
        }).then(r => r.json());
        
        toast('Playlist created');
        renderPlaylists();
    };
    
    window._rm2_openPlaylist = async (playlistId) => {
        const playlist = await api(`/playlists/${playlistId}`).then(r => r.json());
        
        // Play all tracks
        _queue = playlist.tracks;
        _queueIdx = 0;
        if (_queue.length > 0) {
            playTrack(_queue[0]);
        }
    };
    
    // ── Navigation ─────────────────────────────────────────────────────
    
    const navigate = (section) => {
        activeSection = section;
        
        // Update sidebar active state
        bodyEl.querySelectorAll('.rm2-section li').forEach(li => {
            li.classList.toggle('active', li.dataset.section === section);
        });
        
        // Update page title
        const titles = {
            'search': 'Search',
            'playlists': 'Playlists',
            'history': 'History',
            'radio-top': 'Top Radio Stations',
            'radio-favorites': 'Favorite Stations',
            'radio-countries': 'Browse by Country',
            'radio-tags': 'Browse by Genre',
            'podcasts-search': 'Search Podcasts',
            'podcasts-favorites': 'Podcast Subscriptions'
        };
        
        bodyEl.querySelector('#rm2-page-title').textContent = titles[section] || 'Radio & Music';
        
        // Render section
        switch (section) {
            case 'search': renderSearch(); break;
            case 'playlists': renderPlaylists(); break;
            case 'history': renderHistory(); break;
            case 'radio-top': renderRadioTop(); break;
            default: bodyEl.querySelector('#rm2-main-content').innerHTML = '<div class="rm2-empty">Coming soon</div>';
        }
    };
    
    // ── Init ───────────────────────────────────────────────────────────
    
    return {
        onOpen: function(win) {
            bodyEl = win.body();
            bodyEl.innerHTML = buildHTML();
            
            // Init player
            initPlayer();
            
            // Sidebar navigation
            bodyEl.querySelectorAll('.rm2-section li').forEach(li => {
                li.addEventListener('click', () => {
                    navigate(li.dataset.section);
                });
            });
            
            // Load initial section
            navigate('search');
        },
        
        onClose: function() {
            if (_audio) {
                _audio.pause();
                _audio.src = '';
            }
            
            // Cleanup global functions
            delete window._rm2_playYoutube;
            delete window._rm2_playRadio;
            delete window._rm2_playFromHistory;
            delete window._rm2_createPlaylist;
            delete window._rm2_openPlaylist;
        }
    };
};
