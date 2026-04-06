/* eslint-disable */
/**
 * Radio & Music — internet radio, podcasts, and music player.
 * CSS prefix: rm-
 */
AppRegistry['radio-music'] = function(appDef, launchOpts) {

    let bodyEl, activeSection = 'most-played', _audio = null, _playing = null;
    let _favorites = [], _subscriptions = [], _countries = [], _tags = [];
    let _recentStations = [];  // for prev/next navigation
    let _musicQueue = [];      // music track queue
    let _musicQueueIdx = -1;   // index of currently playing track in queue
    let _ytdlpReady = null;    // null = unknown, true/false
    let _seekInterval = null;  // interval for updating seekbar
    let _playlists = [];       // user's playlists

    const escH = s => s ? s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';

    // Deterministic color from station name (for letter-avatar fallback)
    const _COLORS = ['#ef4444','#f97316','#f59e0b','#22c55e','#14b8a6','#3b82f6','#6366f1','#a855f7','#ec4899','#06b6d4'];
    function _stationColor(name) { let h=0; for(let i=0;i<name.length;i++) h=((h<<5)-h)+name.charCodeAt(i); return _COLORS[Math.abs(h)%_COLORS.length]; }
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

    // Multi-layer logo: Google favicon (128px, from homepage domain) → Radio Browser favicon → letter avatar
    // Google service returns high-quality logos; Radio Browser favicons are often tiny/broken
    function _stationIconHtml(s) {
        const letter = _stationInitial(s.name);
        const bg = _stationColor(s.name);
        const letterFallback = '<span class="rm-letter-icon" style="display:none;background:' + bg + '">' + escH(letter) + '</span>';
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
/* ── Spotify-inspired layout ─────────────────────────── */
'.rm-wrap{display:flex;height:100%;overflow:hidden;font-size:13px;background:#121212;color:#fff;border-radius:0}',
'.rm-sidebar{width:220px;min-width:220px;background:#000;display:flex;flex-direction:column;overflow-y:auto;padding:8px 0;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.1) transparent}',
'.rm-sidebar-item{padding:10px 20px;cursor:pointer;display:flex;align-items:center;gap:10px;color:rgba(255,255,255,.65);font-size:13px;transition:all .15s;border-radius:0;border-left:3px solid transparent}',
'.rm-sidebar-item:hover{color:#fff;background:rgba(255,255,255,.05)}',
'.rm-sidebar-item.active{color:#1DB954;border-left-color:#1DB954;background:rgba(29,185,84,.08);font-weight:600}',
'.rm-sidebar-item i{width:18px;text-align:center;font-size:14px}',
'.rm-sidebar-label{padding:20px 20px 6px;font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:rgba(255,255,255,.35);font-weight:700}',
'.rm-main{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#121212}',
'.rm-toolbar{display:flex;align-items:center;gap:10px;padding:12px 20px;background:#181818;border-bottom:1px solid rgba(255,255,255,.06);flex-wrap:wrap}',
'.rm-search{flex:1;min-width:180px;padding:10px 16px;border:none;border-radius:24px;background:rgba(255,255,255,.08);color:#fff;font-size:13px;outline:none;transition:background .2s}',
'.rm-search:focus{background:rgba(255,255,255,.14);box-shadow:0 0 0 2px rgba(29,185,84,.3)}',
'.rm-search::placeholder{color:rgba(255,255,255,.4)}',
'.rm-select{padding:8px 12px;border:1px solid rgba(255,255,255,.1);border-radius:20px;background:rgba(255,255,255,.06);color:#fff;font-size:12px;outline:none;cursor:pointer;min-width:100px}',
'.rm-select:focus{border-color:#1DB954}',
'.rm-select option{background:#282828;color:#fff}',
'.rm-content{flex:1;overflow-y:auto;padding:20px;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.1) transparent}',

/* ── station / podcast cards ─────────────────────────── */
'.rm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}',
'.rm-card{display:flex;align-items:center;gap:12px;padding:10px 12px;background:rgba(255,255,255,.04);border:none;border-radius:8px;cursor:pointer;transition:all .2s}',
'.rm-card:hover{background:rgba(255,255,255,.1);transform:translateY(-1px)}',
'.rm-card.rm-playing{background:rgba(29,185,84,.12);box-shadow:inset 3px 0 0 #1DB954}',
'.rm-card.rm-buffering{opacity:.7}',
'.rm-card.rm-buffering .rm-card-icon::after{content:"";position:absolute;inset:0;border-radius:10px;border:2px solid transparent;border-top-color:#1DB954;animation:rm-spin .8s linear infinite}',
'@keyframes rm-spin{to{transform:rotate(360deg)}}',
'.rm-card-icon{width:48px;height:48px;border-radius:8px;background:#282828;display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;position:relative}',
'.rm-card-icon img{width:100%;height:100%;object-fit:cover;border-radius:8px}',
'.rm-card-icon i{font-size:20px;color:rgba(255,255,255,.4)}',
'.rm-letter-icon{width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:20px;border-radius:8px}',
'.rm-card-info{flex:1;min-width:0}',
'.rm-card-name{font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}',
'.rm-card-meta{font-size:11px;color:rgba(255,255,255,.5);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-card-actions{display:flex;gap:4px;flex-shrink:0}',
'.rm-card-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:14px;border-radius:50%;transition:all .12s}',
'.rm-card-btn:hover{color:#1DB954;background:rgba(29,185,84,.1)}',
'.rm-card-btn.rm-fav-active{color:#1DB954}',
'.rm-card-codec{font-size:9px;padding:1px 5px;border-radius:4px;background:rgba(255,255,255,.06);color:rgba(255,255,255,.4);font-weight:600;letter-spacing:.5px}',

/* ── chips ─────────────────────────── */
'.rm-chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}',
'.rm-chip{padding:6px 14px;border-radius:20px;background:rgba(255,255,255,.06);border:none;color:rgba(255,255,255,.8);font-size:12px;cursor:pointer;transition:all .15s;white-space:nowrap}',
'.rm-chip:hover{background:rgba(255,255,255,.12)}',
'.rm-chip.active{background:#1DB954;color:#fff}',

/* ── Player bar (Spotify-style) ────────────────────── */
'.rm-player{display:flex;align-items:center;gap:14px;padding:10px 20px;background:#181818;border-top:1px solid rgba(255,255,255,.06);min-height:68px}',
'.rm-player.rm-buffering .rm-player-art::after{content:"";position:absolute;inset:-2px;border-radius:8px;border:2px solid transparent;border-top-color:#1DB954;animation:rm-spin .8s linear infinite}',
'.rm-player-art{width:50px;height:50px;border-radius:6px;background:#282828;display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.5);position:relative;cursor:pointer}',
'.rm-player-art img{width:100%;height:100%;object-fit:cover}',
'.rm-player-art .rm-letter-icon{font-size:18px}',
'.rm-player-art i{font-size:18px;color:rgba(255,255,255,.4)}',
'.rm-player-info{flex:1;min-width:0;cursor:pointer}',
'.rm-player-name{font-weight:600;font-size:13px;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-player-meta{font-size:11px;color:rgba(255,255,255,.5);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}',
'.rm-player-controls{display:flex;align-items:center;gap:4px}',
'.rm-player-btn{background:none;border:none;color:rgba(255,255,255,.8);font-size:16px;cursor:pointer;padding:8px;border-radius:50%;transition:all .12s;line-height:1}',
'.rm-player-btn:hover{color:#fff;transform:scale(1.08)}',
'.rm-player-btn.rm-btn-play{font-size:20px;width:40px;height:40px;display:flex;align-items:center;justify-content:center;background:#1DB954;color:#000;border-radius:50%;box-shadow:0 2px 8px rgba(29,185,84,.3)}',
'.rm-player-btn.rm-btn-play:hover{background:#1ed760;transform:scale(1.06)}',
'.rm-vol-wrap{display:flex;align-items:center;gap:6px}',
'.rm-vol-wrap i{font-size:13px;color:rgba(255,255,255,.5)}',
'.rm-vol-slider{width:80px;accent-color:#1DB954;height:4px}',
'.rm-player-eq{display:flex;align-items:flex-end;gap:2px;height:18px;margin-left:4px}',
'.rm-player-eq span{width:3px;background:#1DB954;border-radius:1px;animation:rm-eq .6s ease-in-out infinite alternate}',
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
'.rm-ep-item.rm-playing{background:rgba(29,185,84,.1)}',
'.rm-ep-play{font-size:16px;color:#1DB954;width:32px;text-align:center;flex-shrink:0}',
'.rm-ep-info{flex:1;min-width:0}',
'.rm-ep-title{font-weight:600;color:#fff;margin-bottom:2px}',
'.rm-ep-meta{font-size:11px;color:rgba(255,255,255,.5)}',

/* podcast detail header */
'.rm-pod-header{display:flex;gap:20px;margin-bottom:24px;align-items:flex-start}',
'.rm-pod-art{width:140px;height:140px;border-radius:8px;object-fit:cover;flex-shrink:0;box-shadow:0 4px 20px rgba(0,0,0,.5)}',
'.rm-pod-details{flex:1;min-width:0}',
'.rm-pod-title{font-size:20px;font-weight:700;color:#fff;margin-bottom:4px}',
'.rm-pod-author{font-size:13px;color:rgba(255,255,255,.5);margin-bottom:6px}',
'.rm-pod-desc{font-size:12px;color:rgba(255,255,255,.6);line-height:1.5;max-height:60px;overflow:hidden}',
'.rm-pod-sub-btn{margin-top:8px;padding:6px 16px;border-radius:20px;border:1px solid #1DB954;background:transparent;color:#1DB954;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}',
'.rm-pod-sub-btn:hover{background:#1DB954;color:#000}',
'.rm-pod-sub-btn.subscribed{background:#1DB954;color:#000}',

/* empty state */
'.rm-empty{text-align:center;padding:60px 20px;color:rgba(255,255,255,.4)}',
'.rm-empty i{font-size:48px;margin-bottom:12px;display:block;opacity:.3}',
'.rm-empty p{font-size:14px}',

/* ── music tracks ─────────────────────────── */
'.rm-track{display:flex;align-items:center;gap:12px;padding:8px 12px;background:rgba(255,255,255,.03);border:none;border-radius:6px;cursor:pointer;transition:all .15s}',
'.rm-track:hover{background:rgba(255,255,255,.08)}',
'.rm-track.rm-playing{background:rgba(29,185,84,.1)}',
'.rm-track-thumb{width:48px;height:48px;border-radius:6px;object-fit:cover;flex-shrink:0;background:#282828}',
'.rm-track-info{flex:1;min-width:0}',
'.rm-track-title{font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}',
'.rm-track-meta{font-size:11px;color:rgba(255,255,255,.4);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.rm-track-dur{font-size:11px;color:rgba(255,255,255,.4);flex-shrink:0;font-variant-numeric:tabular-nums}',
'.rm-track-actions{display:flex;gap:2px;flex-shrink:0}',
'.rm-track-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:13px;border-radius:50%;transition:all .12s}',
'.rm-track-btn:hover{color:#1DB954;background:rgba(29,185,84,.1)}',

/* seekbar */
'.rm-seekbar{display:none;align-items:center;gap:8px;padding:0 20px;height:20px;flex-shrink:0;background:#181818}',
'.rm-seekbar.visible{display:flex}',
'.rm-seek-time{font-size:10px;color:rgba(255,255,255,.4);font-variant-numeric:tabular-nums;min-width:36px}',
'.rm-seek-time.right{text-align:right}',
'.rm-seek-track{flex:1;height:4px;background:rgba(255,255,255,.1);border-radius:2px;position:relative;cursor:pointer}',
'.rm-seek-fill{height:100%;background:#1DB954;border-radius:2px;position:absolute;left:0;top:0;pointer-events:none;transition:width .1s}',
'.rm-seek-thumb{width:12px;height:12px;border-radius:50%;background:#fff;position:absolute;top:50%;transform:translate(-50%,-50%);cursor:pointer;opacity:0;transition:opacity .15s}',
'.rm-seekbar:hover .rm-seek-thumb{opacity:1}',

/* music queue panel */
'.rm-queue{margin-top:16px}',
'.rm-queue-title{font-size:12px;font-weight:600;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}',
'.rm-queue-list{display:flex;flex-direction:column;gap:2px}',
'.rm-queue-item{display:flex;align-items:center;gap:10px;padding:6px 10px;border-radius:6px;cursor:pointer;transition:background .12s;font-size:12px}',
'.rm-queue-item:hover{background:rgba(255,255,255,.06)}',
'.rm-queue-item.rm-playing{color:#1DB954;font-weight:600}',
'.rm-queue-item-idx{width:20px;text-align:center;color:rgba(255,255,255,.35);flex-shrink:0}',
'.rm-queue-item-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:rgba(255,255,255,.8)}',
'.rm-queue-item.rm-playing .rm-queue-item-title{color:#1DB954}',
'.rm-queue-item-dur{color:rgba(255,255,255,.35);flex-shrink:0}',
'.rm-queue-item-rm{background:none;border:none;color:rgba(255,255,255,.3);cursor:pointer;padding:2px 4px;font-size:11px;transition:color .12s}',
'.rm-queue-item-rm:hover{color:#ef4444}',

/* playlists */
'.rm-pl-header{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}',
'.rm-pl-header h3{margin:0;font-size:18px;color:#fff;flex:1;font-weight:700}',
'.rm-pl-create{display:flex;gap:8px;align-items:center}',
'.rm-pl-create input{padding:6px 12px;border:1px solid rgba(255,255,255,.1);border-radius:20px;background:rgba(255,255,255,.06);color:#fff;font-size:13px;outline:none}',
'.rm-pl-create button{padding:6px 14px;border-radius:20px;background:#1DB954;color:#000;border:none;cursor:pointer;font-size:12px;font-weight:600;transition:filter .12s}',
'.rm-pl-create button:hover{filter:brightness(1.1)}',
'.rm-pl-card{display:flex;align-items:center;gap:12px;padding:12px;background:rgba(255,255,255,.04);border:none;border-radius:8px;cursor:pointer;transition:all .2s}',
'.rm-pl-card:hover{background:rgba(255,255,255,.08)}',
'.rm-pl-icon{width:48px;height:48px;border-radius:8px;background:linear-gradient(135deg,#1DB954,#1a8f42);display:flex;align-items:center;justify-content:center;color:#000;font-size:18px;flex-shrink:0}',
'.rm-pl-info{flex:1;min-width:0}',
'.rm-pl-name{font-weight:600;color:#fff;font-size:14px}',
'.rm-pl-meta{font-size:11px;color:rgba(255,255,255,.4);margin-top:2px}',
'.rm-pl-actions{display:flex;gap:4px}',
'.rm-pl-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:13px;border-radius:50%;transition:all .12s}',
'.rm-pl-btn:hover{color:#ef4444;background:rgba(239,68,68,.1)}',

/* add-to-playlist modal */
'.rm-modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(4px)}',
'.rm-modal{background:#282828;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:20px;min-width:280px;max-width:400px;max-height:60vh;overflow-y:auto}',
'.rm-modal h4{margin:0 0 12px;font-size:15px;color:#fff}',
'.rm-modal-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:6px;cursor:pointer;transition:background .12s;font-size:13px;color:rgba(255,255,255,.8)}',
'.rm-modal-item:hover{background:rgba(255,255,255,.08)}',
'.rm-modal-item i{color:#1DB954;width:16px;text-align:center}',
'.rm-modal-close{margin-top:12px;padding:6px 16px;border-radius:20px;background:none;border:1px solid rgba(255,255,255,.15);color:rgba(255,255,255,.7);cursor:pointer;font-size:12px;width:100%;transition:all .12s}',
'.rm-modal-close:hover{border-color:rgba(255,255,255,.3);color:#fff}',

/* install banner */
'.rm-install-banner{text-align:center;padding:40px 20px;max-width:400px;margin:0 auto}',
'.rm-install-banner i{font-size:48px;color:#1DB954;margin-bottom:12px;display:block}',
'.rm-install-banner p{font-size:14px;color:rgba(255,255,255,.5);margin-bottom:16px}',
'.rm-install-btn{padding:10px 24px;border-radius:24px;background:#1DB954;color:#000;border:none;cursor:pointer;font-size:14px;font-weight:700;transition:all .15s}',
'.rm-install-btn:hover{background:#1ed760;transform:scale(1.02)}',
'.rm-install-btn:disabled{opacity:.5;cursor:wait}',

/* download button */
'.rm-dl-btn{background:none;border:none;color:rgba(255,255,255,.4);cursor:pointer;padding:6px;font-size:14px;border-radius:50%;transition:all .12s;flex-shrink:0}',
'.rm-dl-btn:hover{color:#1DB954;background:rgba(29,185,84,.1)}',
'.rm-dl-btn.rm-downloading{color:#1DB954;animation:rm-pulse 1.2s infinite}',
'.rm-dl-btn.rm-downloaded{color:#1DB954}',
'@keyframes rm-pulse{0%,100%{opacity:1}50%{opacity:.4}}',
'.rm-dl-toast{position:fixed;bottom:80px;right:16px;background:#282828;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:10px 16px;font-size:12px;color:#fff;box-shadow:0 4px 20px rgba(0,0,0,.5);z-index:9998;display:flex;align-items:center;gap:8px;max-width:300px}',
'.rm-dl-toast i{color:#1DB954;font-size:14px}',

/* local music folder chips */
'.rm-folder-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}',
'.rm-folder-chip{display:inline-flex;align-items:center;gap:4px;padding:5px 10px;border-radius:16px;background:rgba(255,255,255,.06);border:none;font-size:11px;color:rgba(255,255,255,.8);cursor:default}',
'.rm-folder-chip .rm-chip-remove{cursor:pointer;opacity:.5;margin-left:2px;font-size:10px}',
'.rm-folder-chip .rm-chip-remove:hover{opacity:1;color:#ef4444}',
'.rm-add-folder-btn{padding:5px 12px;border-radius:16px;background:none;border:1px dashed rgba(255,255,255,.15);color:rgba(255,255,255,.4);font-size:11px;cursor:pointer;display:inline-flex;align-items:center;gap:4px}',
'.rm-add-folder-btn:hover{border-color:#1DB954;color:#1DB954}',

/* ── Now Playing overlay ───────────────────────── */
'.rm-np-overlay{position:absolute;inset:0;z-index:100;display:flex;flex-direction:column;overflow:hidden}',
'.rm-np-bg{position:absolute;inset:-40px;background-size:cover;background-position:center;filter:blur(40px) brightness(.25) saturate(1.4);z-index:0}',
'.rm-np-inner{position:relative;z-index:1;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;gap:16px;overflow-y:auto}',
'.rm-np-close{position:absolute;top:12px;left:12px;background:rgba(255,255,255,.08);border:none;color:#fff;font-size:18px;cursor:pointer;padding:8px 12px;border-radius:50%;z-index:2;backdrop-filter:blur(8px);transition:background .15s}',
'.rm-np-close:hover{background:rgba(255,255,255,.15)}',
'.rm-np-art{width:260px;height:260px;border-radius:8px;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,.6);flex-shrink:0;background:#282828;display:flex;align-items:center;justify-content:center}',
'.rm-np-art img{width:100%;height:100%;object-fit:cover}',
'.rm-np-art .rm-letter-icon{font-size:64px;width:100%;height:100%}',
'.rm-np-art i{font-size:64px;color:rgba(255,255,255,.3)}',
'.rm-np-info{text-align:center;max-width:320px;width:100%}',
'.rm-np-title{font-size:22px;font-weight:700;color:#fff;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.rm-np-meta{font-size:13px;color:rgba(255,255,255,.5);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.rm-np-seek{display:flex;align-items:center;gap:10px;width:100%;max-width:320px}',
'.rm-np-seek .rm-seek-time{color:rgba(255,255,255,.4);font-size:11px;min-width:38px}',
'.rm-np-seek .rm-seek-track{flex:1;height:4px;background:rgba(255,255,255,.12);border-radius:2px;position:relative;cursor:pointer}',
'.rm-np-seek .rm-seek-fill{height:100%;background:#1DB954;border-radius:2px;position:absolute;left:0;top:0}',
'.rm-np-seek .rm-seek-thumb{width:14px;height:14px;border-radius:50%;background:#fff;position:absolute;top:50%;transform:translate(-50%,-50%);cursor:pointer}',
'.rm-np-controls{display:flex;align-items:center;gap:20px}',
'.rm-np-btn{background:none;border:none;color:rgba(255,255,255,.7);font-size:22px;cursor:pointer;padding:10px;border-radius:50%;transition:all .12s}',
'.rm-np-btn:hover{color:#fff;transform:scale(1.1)}',
'.rm-np-btn.rm-np-play{width:64px;height:64px;font-size:26px;background:#1DB954;color:#000;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 20px rgba(29,185,84,.3)}',
'.rm-np-btn.rm-np-play:hover{background:#1ed760;transform:scale(1.06)}',
'.rm-np-actions{display:flex;gap:10px;margin-top:4px}',
'.rm-np-action{background:rgba(255,255,255,.06);border:none;color:rgba(255,255,255,.5);font-size:13px;cursor:pointer;padding:8px 16px;border-radius:20px;transition:all .12s;display:flex;align-items:center;gap:6px}',
'.rm-np-action:hover{background:rgba(255,255,255,.12);color:#fff}',
'.rm-np-count{font-size:11px;color:rgba(255,255,255,.3);margin-top:2px}',

/* ── Horizontal scroll section (Most Played, etc.) ── */
'.rm-section-title{font-size:18px;font-weight:700;color:#fff;margin:20px 0 12px;display:flex;align-items:center;gap:10px}',
'.rm-section-title i{color:#1DB954;font-size:16px}',
'.rm-hscroll{display:flex;overflow-x:auto;gap:14px;padding-bottom:8px;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;scrollbar-width:none}',
'.rm-hscroll::-webkit-scrollbar{display:none}',
'.rm-hcard{scroll-snap-align:start;min-width:150px;max-width:170px;flex-shrink:0;cursor:pointer;transition:all .15s;padding:10px;background:rgba(255,255,255,.04);border-radius:8px}',
'.rm-hcard:hover{background:rgba(255,255,255,.08);transform:translateY(-3px)}',
'.rm-hcard-art{width:130px;height:130px;border-radius:6px;overflow:hidden;background:#282828;margin-bottom:8px;position:relative;box-shadow:0 4px 16px rgba(0,0,0,.3)}',
'.rm-hcard-art img{width:100%;height:100%;object-fit:cover}',
'.rm-hcard-art .rm-letter-icon{width:100%;height:100%;font-size:40px}',
'.rm-hcard-art i{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:32px;color:rgba(255,255,255,.3)}',
'.rm-hcard-badge{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.7);color:#1DB954;font-size:10px;padding:2px 6px;border-radius:10px;backdrop-filter:blur(4px);font-weight:600}',
'.rm-hcard-title{font-size:13px;font-weight:600;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.rm-hcard-meta{font-size:11px;color:rgba(255,255,255,.4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}',

/* mobile nav bar */
'.rm-mobile-nav{display:none;overflow-x:auto;white-space:nowrap;background:#000;border-bottom:1px solid rgba(255,255,255,.06);padding:6px 8px;gap:4px;-webkit-overflow-scrolling:touch;scrollbar-width:none}',
'.rm-mobile-nav::-webkit-scrollbar{display:none}',
'.rm-mobile-nav .rm-mnav-btn{display:inline-flex;align-items:center;gap:5px;padding:7px 12px;border:none;border-radius:20px;background:rgba(255,255,255,.06);color:rgba(255,255,255,.7);font-size:12px;white-space:nowrap;cursor:pointer;flex-shrink:0;transition:all .12s}',
'.rm-mobile-nav .rm-mnav-btn.active{background:#1DB954;color:#000;font-weight:600}',
'.rm-mobile-nav .rm-mnav-btn i{font-size:11px}',

/* responsive */
'@media(max-width:768px){.rm-wrap{height:100%;max-height:100%}.rm-sidebar{display:none}.rm-mobile-nav{display:flex}.rm-main{min-height:0}.rm-grid{grid-template-columns:1fr}.rm-pod-header{flex-direction:column;align-items:center;text-align:center}.rm-pod-art{width:100px;height:100px}.rm-toolbar{padding:8px 10px;flex-shrink:0}.rm-content{padding:10px;min-height:0}.rm-seekbar{padding:0 10px;height:16px}.rm-seek-time{font-size:9px;min-width:30px}.rm-player{padding:6px 8px;gap:6px;min-height:auto;flex-shrink:0;cursor:pointer}.rm-vol-wrap{display:none}.rm-player-art{width:32px;height:32px;min-width:32px}.rm-player-info{min-width:0;flex:1}.rm-player-name{font-size:12px}.rm-player-meta{font-size:10px}.rm-player-controls{gap:0;flex-shrink:0}.rm-player-btn{padding:4px;font-size:13px}.rm-player-btn.rm-btn-play{width:30px;height:30px;font-size:15px;min-width:30px}.rm-player-eq{display:none}.rm-track-thumb{width:40px;height:40px}.rm-np-art{width:200px;height:200px}.rm-np-title{font-size:18px}.rm-hcard{min-width:130px;padding:8px}.rm-hcard-art{width:114px;height:114px}.rm-section-title{font-size:16px}}',
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
    <div class="rm-sidebar-label">${t('Muzyka')}</div>
    <div class="rm-sidebar-item" data-section="music"><i class="fab fa-youtube"></i> ${t('Szukaj')}</div>
    <div class="rm-sidebar-item" data-section="local"><i class="fas fa-folder-open"></i> ${t('Lokalna muzyka')}</div>
    <div class="rm-sidebar-item" data-section="playlists"><i class="fas fa-list"></i> ${t('Playlisty')}</div>
    <div class="rm-sidebar-item" data-section="queue"><i class="fas fa-list-ol"></i> ${t('Kolejka')}</div>
    <div class="rm-sidebar-label">${t('Radio')}</div>
    <div class="rm-sidebar-item" data-section="radio"><i class="fas fa-broadcast-tower"></i> ${t('Przeglądaj')}</div>
    <div class="rm-sidebar-item" data-section="favorites"><i class="fas fa-heart"></i> ${t('Ulubione')}</div>
    <div class="rm-sidebar-item" data-section="countries"><i class="fas fa-globe"></i> ${t('Kraje')}</div>
    <div class="rm-sidebar-item" data-section="tags"><i class="fas fa-tags"></i> ${t('Gatunki')}</div>
    <div class="rm-sidebar-label">${t('Podcasty')}</div>
    <div class="rm-sidebar-item" data-section="podcasts"><i class="fas fa-podcast"></i> ${t('Szukaj')}</div>
    <div class="rm-sidebar-item" data-section="subscriptions"><i class="fas fa-rss"></i> ${t('Subskrypcje')}</div>
    <div class="rm-sidebar-label">${t('Inne')}</div>
    <div class="rm-sidebar-item active" data-section="most-played"><i class="fas fa-fire"></i> ${t('Najczęściej grane')}</div>
    <div class="rm-sidebar-item" data-section="history"><i class="fas fa-history"></i> ${t('Historia')}</div>
  </div>
  <div class="rm-main">
    <div class="rm-mobile-nav" id="rm-mobile-nav">
      <button class="rm-mnav-btn" data-section="music"><i class="fab fa-youtube"></i> ${t('Muzyka')}</button>
      <button class="rm-mnav-btn" data-section="local"><i class="fas fa-folder-open"></i> ${t('Lokalne')}</button>
      <button class="rm-mnav-btn" data-section="playlists"><i class="fas fa-list"></i> ${t('Playlisty')}</button>
      <button class="rm-mnav-btn" data-section="queue"><i class="fas fa-list-ol"></i> ${t('Kolejka')}</button>
      <button class="rm-mnav-btn" data-section="radio"><i class="fas fa-broadcast-tower"></i> ${t('Radio')}</button>
      <button class="rm-mnav-btn" data-section="favorites"><i class="fas fa-heart"></i> ${t('Ulubione')}</button>
      <button class="rm-mnav-btn" data-section="countries"><i class="fas fa-globe"></i> ${t('Kraje')}</button>
      <button class="rm-mnav-btn" data-section="tags"><i class="fas fa-tags"></i> ${t('Gatunki')}</button>
      <button class="rm-mnav-btn" data-section="podcasts"><i class="fas fa-podcast"></i> ${t('Podcasty')}</button>
      <button class="rm-mnav-btn" data-section="subscriptions"><i class="fas fa-rss"></i> ${t('Subskrypcje')}</button>
      <button class="rm-mnav-btn active" data-section="most-played"><i class="fas fa-fire"></i> ${t('Top')}</button>
      <button class="rm-mnav-btn" data-section="history"><i class="fas fa-history"></i> ${t('Historia')}</button>
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

            loadSection('most-played');
        },
        onClose() {
            stopPlayback();
        },
    });
    setTimeout(() => { if (typeof toggleMaximize === 'function') toggleMaximize('radio-music'); }, 50);

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
            case 'music': loadMusic(toolbar, content); break;
            case 'local': loadLocal(toolbar, content); break;
            case 'playlists': loadPlaylists(toolbar, content); break;
            case 'most-played': loadMostPlayed(content); break;
            case 'queue': loadQueue(content); break;
            case 'history': loadHistory(content); break;
        }
    }

    /* ── Radio Browse ───────────────────────────────── */

    async function loadRadio(toolbar, content) {
        const RADIO_COUNTRIES = [{code:'',name:t('Wszystkie kraje')},{code:'PL',name:'Polska'},{code:'US',name:'USA'},{code:'GB',name:'UK'},
            {code:'DE',name:'Niemcy'},{code:'FR',name:'Francja'},{code:'ES',name:'Hiszpania'},{code:'IT',name:'Włochy'},
            {code:'BR',name:'Brazylia'},{code:'CA',name:'Kanada'},{code:'AU',name:'Australia'},{code:'JP',name:'Japonia'},
            {code:'SE',name:'Szwecja'},{code:'NL',name:'Holandia'},{code:'CZ',name:'Czechy'},{code:'UA',name:'Ukraina'}];
        let _radioCountry = '';

        toolbar.innerHTML = `<input class="rm-search" id="rm-radio-search" placeholder="${t('Szukaj stacji radiowych...')}" autofocus>`
            + `<select class="rm-select" id="rm-radio-country">${RADIO_COUNTRIES.map(c => '<option value="'+c.code+'">'+escH(c.name)+'</option>').join('')}</select>`;

        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';

        const searchInput = bodyEl.querySelector('#rm-radio-search');
        let debounce;
        searchInput.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                const q = searchInput.value.trim();
                if (q) searchRadio(q, _radioCountry, content);
                else loadTopRadio(_radioCountry, content);
            }, 400);
        };

        bodyEl.querySelector('#rm-radio-country').onchange = (e) => {
            _radioCountry = e.target.value;
            const q = searchInput.value.trim();
            if (q) searchRadio(q, _radioCountry, content);
            else loadTopRadio(_radioCountry, content);
        };

        // Load top stations by default
        loadTopRadio('', content);
    }

    async function loadTopRadio(country, content) {
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        let url = '/radio-music/radio/top?limit=50';
        if (country) url = '/radio-music/radio/search?country=' + country + '&limit=50';
        const data = await api(url);
        if (data.items && data.items.length) renderStations(data.items, content);
        else content.innerHTML = '<div class="rm-empty"><i class="fas fa-broadcast-tower"></i><p>' + t('Brak stacji') + '</p></div>';
    }

    async function searchRadio(q, country, content) {
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        let url = '/radio-music/radio/search?q=' + encodeURIComponent(q);
        if (country) url += '&country=' + country;
        const data = await api(url);
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
            card._stationUuid = s.uuid;
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

    /* ── Podcasts Browse ──────────────────────────── */

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

    async function loadPodcasts(toolbar, content) {
        let _podCountry = 'pl', _podGenre = '';

        toolbar.innerHTML = `
            <input class="rm-search" id="rm-pod-search" placeholder="${t('Szukaj podcastów...')}" autofocus>
            <select class="rm-select" id="rm-pod-country">${_POD_COUNTRIES.map(c => '<option value="'+c.code+'"'+(c.code==='pl'?' selected':'')+'>'+escH(c.name)+'</option>').join('')}</select>`;

        // Genre chips + results area
        content.innerHTML = '<div class="rm-chips" id="rm-pod-genres"></div><div id="rm-pod-results"></div>';
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
            loadTopPodcasts(_podCountry, _podGenre, content.querySelector('#rm-pod-results'));
        };

        // Search
        const searchInput = bodyEl.querySelector('#rm-pod-search');
        let debounce;
        searchInput.onkeyup = () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                if (searchInput.value.trim()) {
                    searchPodcasts(searchInput.value, content.querySelector('#rm-pod-results'));
                } else {
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

    /* ── Music (YouTube / yt-dlp) ─────────────────── */

    const _MUSIC_GENRES = [
        {q:'top hits 2024 2025', label:'🔥 Hity'},
        {q:'pop music', label:'Pop'}, {q:'rock music', label:'Rock'},
        {q:'hip hop rap', label:'Hip-Hop'}, {q:'electronic dance music', label:'Electronic'},
        {q:'r&b soul music', label:'R&B'}, {q:'jazz music', label:'Jazz'},
        {q:'classical music', label:'Klasyczna'}, {q:'reggae music', label:'Reggae'},
        {q:'metal music', label:'Metal'}, {q:'indie alternative', label:'Indie'},
        {q:'polish music polskie', label:'🇵🇱 Polskie'},
    ];

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

    function renderMusicResults(tracks, container) {
        container.innerHTML = '<div style="display:flex;flex-direction:column;gap:6px" id="rm-tracks-list"></div>';
        const list = container.querySelector('#rm-tracks-list');
        tracks.forEach((tr, idx) => {
            const isPlaying = _playing && _playing.id === tr.id;
            const el = document.createElement('div');
            el.className = 'rm-track' + (isPlaying ? ' rm-playing' : '');
            el.innerHTML = `
                <img class="rm-track-thumb" src="${escH(tr.thumbnail)}" loading="lazy" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 48 48%22><rect fill=%22%231a1a2e%22 width=%2248%22 height=%2248%22/><text x=%2224%22 y=%2230%22 fill=%22%23666%22 text-anchor=%22middle%22 font-size=%2220%22>♪</text></svg>'">
                <div class="rm-track-info">
                    <div class="rm-track-title">${escH(tr.title)}</div>
                    <div class="rm-track-meta">${escH(tr.channel)}</div>
                </div>
                <span class="rm-track-dur">${escH(tr.duration_fmt)}</span>
                <div class="rm-track-actions">
                    <button class="rm-dl-btn" title="${t('Pobierz')}"><i class="fas fa-download"></i></button>
                    <button class="rm-track-btn" title="${t('Playlista')}"><i class="fas fa-list-ul"></i></button>
                    <button class="rm-track-btn rm-add-queue-btn" title="${t('Dodaj do kolejki')}"><i class="fas fa-plus"></i></button>
                </div>`;
            el.onclick = (e) => {
                if (e.target.closest('.rm-add-queue-btn') || e.target.closest('.rm-dl-btn') || e.target.closest('.rm-track-btn')) return;
                _musicQueue = tracks.slice(idx);
                _musicQueueIdx = 0;
                playMusicTrack(tr);
            };
            el.querySelector('.rm-add-queue-btn').onclick = (e) => {
                e.stopPropagation();
                _musicQueue.push(tr);
                toast(t('Dodano do kolejki: ') + tr.title, 'success');
            };
            el.querySelector('.rm-dl-btn').onclick = (e) => {
                e.stopPropagation();
                _downloadTrack(tr, e.currentTarget);
            };
            el.querySelector('.rm-track-btn[title="' + t('Playlista') + '"]').onclick = (e) => {
                e.stopPropagation();
                _showAddToPlaylistModal({
                    name: tr.title, url: tr.url, type: 'music',
                    meta: tr.channel, image: tr.thumbnail,
                });
            };
            list.appendChild(el);
        });
    }

    function playMusicTrack(tr) {
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

    async function _downloadTrack(track, btnEl) {
        if (btnEl) { btnEl.classList.add('rm-downloading'); btnEl.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; }
        const data = await api('/radio-music/music/download', {
            method: 'POST',
            body: { url: track.url, title: track.title || track.name },
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
            if (!job) { clearInterval(_poll); return; }
            if (job.status === 'done') {
                clearInterval(_poll);
                toast(t('Pobrano: ') + (track.title || track.name), 'success');
                if (btnEl) { btnEl.classList.remove('rm-downloading'); btnEl.classList.add('rm-downloaded'); btnEl.innerHTML = '<i class="fas fa-check"></i>'; }
            } else if (job.status === 'error') {
                clearInterval(_poll);
                toast(t('Błąd pobierania: ') + (job.error || ''), 'error');
                if (btnEl) { btnEl.classList.remove('rm-downloading'); btnEl.innerHTML = '<i class="fas fa-download"></i>'; }
            }
        }, 2000);
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
            if (!job) { clearInterval(_poll); return; }
            if (job.status === 'done' || job.status === 'done_partial') {
                clearInterval(_poll);
                const msg = job.status === 'done'
                    ? t('Playlista pobrana: ') + name
                    : t('Playlista pobrana częściowo: ') + name + (job.error ? ' — ' + job.error : '');
                toast(msg, job.status === 'done' ? 'success' : 'warning');
            } else if (job.status === 'error') {
                clearInterval(_poll);
                toast(t('Błąd pobierania: ') + (job.error || ''), 'error');
            }
        }, 3000);
    }

    /* ── Local Music ───────────────────────────────── */

    async function loadLocal(toolbar, content) {
        toolbar.innerHTML = '';
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';

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

        // Group by folder
        const byFolder = {};
        items.forEach(it => {
            if (!byFolder[it.folder]) byFolder[it.folder] = [];
            byFolder[it.folder].push(it);
        });

        let html = '';
        for (const [folder, files] of Object.entries(byFolder)) {
            const folderName = folder.split('/').pop() || folder;
            html += '<div class="rm-section-title"><i class="fas fa-folder"></i> ' + escH(folderName)
                + ' <span style="font-size:11px;color:var(--text-muted);font-weight:400">(' + files.length + ')</span></div>';
            html += '<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:16px">';
            files.forEach((file, i) => {
                const sizeMB = (file.size / 1048576).toFixed(1);
                html += '<div class="rm-track rm-local-track" data-folder="' + escH(folder) + '" data-idx="' + i + '">'
                    + '<div class="rm-track-thumb" style="display:flex;align-items:center;justify-content:center;background:#1a1a2e"><i class="fas fa-music" style="color:var(--text-muted)"></i></div>'
                    + '<div class="rm-track-info"><div class="rm-track-title">' + escH(file.name) + '</div>'
                    + '<div class="rm-track-meta">' + escH(file.filename) + ' · ' + sizeMB + ' MB</div></div>'
                    + '<div class="rm-track-actions">'
                    + '<button class="rm-track-btn" title="' + t('Playlista') + '"><i class="fas fa-list-ul"></i></button>'
                    + '<button class="rm-track-btn rm-add-queue-btn" title="' + t('Kolejka') + '"><i class="fas fa-plus"></i></button>'
                    + '</div></div>';
            });
            html += '</div>';
        }
        content.innerHTML = html;

        // Wire clicks
        content.querySelectorAll('.rm-local-track').forEach(el => {
            const folder = el.dataset.folder;
            const idx = parseInt(el.dataset.idx);
            const file = byFolder[folder][idx];
            const localItem = {
                name: file.name, type: 'local', path: file.path,
                url: '/api/radio-music/local/stream?path=' + encodeURIComponent(file.path) + '&token=' + (NAS.token || ''),
                meta: file.filename,
            };
            el.onclick = (e) => {
                if (e.target.closest('.rm-track-btn')) return;
                // Play and set rest of folder as queue
                const folderFiles = byFolder[folder];
                _musicQueue = folderFiles.slice(idx).map(f => ({
                    id: f.path, title: f.name, channel: f.filename,
                    url: f.path, thumbnail: '', duration: 0, duration_fmt: '',
                    source: 'local',
                }));
                _musicQueueIdx = 0;
                playAudio(localItem);
            };
            el.querySelector('.rm-add-queue-btn').onclick = (e) => {
                e.stopPropagation();
                _musicQueue.push({
                    id: file.path, title: file.name, channel: file.filename,
                    url: file.path, thumbnail: '', duration: 0, duration_fmt: '',
                    source: 'local',
                });
                toast(t('Dodano do kolejki: ') + file.name, 'success');
            };
            const plBtn = el.querySelector('.rm-track-btn[title="' + t('Playlista') + '"]');
            if (plBtn) plBtn.onclick = (e) => {
                e.stopPropagation();
                _showAddToPlaylistModal(localItem);
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
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        await _loadPlaylists();

        let html = '<div class="rm-pl-header"><h3>' + t('Moje playlisty') + '</h3>'
            + '<div class="rm-pl-create"><input id="rm-pl-name" placeholder="' + t('Nazwa playlisty...') + '">'
            + '<button id="rm-pl-create-btn"><i class="fas fa-plus"></i> ' + t('Utwórz') + '</button></div></div>';

        if (!_playlists.length) {
            html += '<div class="rm-empty"><i class="fas fa-list"></i><p>' + t('Brak playlist. Utwórz pierwszą!') + '</p></div>';
        } else {
            html += '<div style="display:flex;flex-direction:column;gap:8px">';
            _playlists.forEach(pl => {
                html += `<div class="rm-pl-card" data-plid="${escH(pl.id)}">
                    <div class="rm-pl-icon"><i class="fas fa-music"></i></div>
                    <div class="rm-pl-info">
                        <div class="rm-pl-name">${escH(pl.name)}</div>
                        <div class="rm-pl-meta">${pl.tracks.length} ${t('utworów')}</div>
                    </div>
                    <div class="rm-pl-actions">
                        <button class="rm-pl-btn rm-pl-play" title="${t('Odtwórz')}"><i class="fas fa-play"></i></button>
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

        // Click handlers
        content.querySelectorAll('.rm-pl-card').forEach(card => {
            const plId = card.dataset.plid;
            card.onclick = (e) => {
                if (e.target.closest('.rm-pl-play') || e.target.closest('.rm-pl-del')) return;
                openPlaylist(plId, content);
            };
            const playBtn = card.querySelector('.rm-pl-play');
            if (playBtn) playBtn.onclick = (e) => {
                e.stopPropagation();
                const pl = _playlists.find(p => p.id === plId);
                if (pl && pl.tracks.length) {
                    _musicQueue = pl.tracks.slice();
                    _musicQueueIdx = 0;
                    _playTrackFromPlaylist(pl.tracks[0]);
                    toast(t('Odtwarzam: ') + pl.name, 'success');
                }
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

        let html = '<div class="rm-pl-header">'
            + '<button class="rm-pl-btn" id="rm-pl-back" style="font-size:16px;color:var(--text-primary)"><i class="fas fa-arrow-left"></i></button>'
            + '<h3>' + escH(pl.name) + '</h3>'
            + '<button class="rm-pl-btn rm-pl-play-all" title="' + t('Odtwórz wszystko') + '" style="color:var(--accent);font-size:16px"><i class="fas fa-play"></i></button>'
            + '<button class="rm-dl-btn rm-pl-dl-all" title="' + t('Pobierz playlistę') + '" style="font-size:16px"><i class="fas fa-download"></i></button>'
            + '</div>';

        if (!pl.tracks.length) {
            html += '<div class="rm-empty"><i class="fas fa-music"></i><p>' + t('Playlista jest pusta') + '</p><p style="font-size:12px;margin-top:4px">' + t('Dodaj utwory z sekcji Muzyka, Radio lub Podcasty') + '</p></div>';
        } else {
            html += '<div style="display:flex;flex-direction:column;gap:4px">';
            pl.tracks.forEach((tr, idx) => {
                const icon = tr.type === 'radio' ? 'fa-broadcast-tower' : tr.type === 'podcast' ? 'fa-podcast' : 'fa-music';
                html += `<div class="rm-track" data-idx="${idx}">
                    ${tr.image || tr.thumbnail ? '<img class="rm-track-thumb" src="' + escH(tr.image || tr.thumbnail) + '" loading="lazy">' : '<div class="rm-track-thumb" style="display:flex;align-items:center;justify-content:center"><i class="fas ' + icon + '" style="color:var(--text-muted)"></i></div>'}
                    <div class="rm-track-info">
                        <div class="rm-track-title">${escH(tr.name || tr.title)}</div>
                        <div class="rm-track-meta">${escH(tr.meta || tr.channel || '')} ${tr.type ? '<span class="rm-card-codec">' + tr.type + '</span>' : ''}</div>
                    </div>
                    ${tr.duration_fmt ? '<span class="rm-track-dur">' + escH(tr.duration_fmt) + '</span>' : ''}
                    <button class="rm-track-btn rm-pl-trk-rm" title="${t('Usuń')}"><i class="fas fa-times"></i></button>
                </div>`;
            });
            html += '</div>';
        }
        content.innerHTML = html;

        content.querySelector('#rm-pl-back').onclick = () => {
            const toolbar = bodyEl.querySelector('#rm-toolbar');
            loadPlaylists(toolbar, content);
        };

        const playAllBtn = content.querySelector('.rm-pl-play-all');
        if (playAllBtn) playAllBtn.onclick = () => {
            if (pl.tracks.length) {
                _musicQueue = pl.tracks.slice();
                _musicQueueIdx = 0;
                _playTrackFromPlaylist(pl.tracks[0]);
            }
        };

        const dlAllBtn = content.querySelector('.rm-pl-dl-all');
        if (dlAllBtn) dlAllBtn.onclick = () => {
            const musicTracks = pl.tracks.filter(t => t.type === 'music' && t.url);
            if (!musicTracks.length) {
                toast(t('Brak utworów do pobrania (tylko YouTube)'), 'warning');
                return;
            }
            _downloadPlaylist(pl.name, musicTracks);
            dlAllBtn.classList.add('rm-downloading');
            dlAllBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        };

        content.querySelectorAll('.rm-track').forEach(el => {
            const idx = parseInt(el.dataset.idx);
            el.onclick = (e) => {
                if (e.target.closest('.rm-pl-trk-rm')) return;
                _musicQueue = pl.tracks.slice(idx);
                _musicQueueIdx = 0;
                _playTrackFromPlaylist(pl.tracks[idx]);
            };
            el.querySelector('.rm-pl-trk-rm').onclick = async (e) => {
                e.stopPropagation();
                await api('/radio-music/playlists/' + plId + '/tracks/' + idx, { method: 'DELETE' });
                openPlaylist(plId, content);
            };
        });
    }

    function _playTrackFromPlaylist(tr) {
        if (tr.type === 'radio') {
            playStation(tr);
        } else if (tr.type === 'music') {
            playMusicTrack(tr);
        } else if (tr.type === 'local') {
            playAudio(tr);
        } else {
            playAudio(tr);
        }
    }

    function _showAddToPlaylistModal(track) {
        // Show a simple modal to pick a playlist
        const overlay = document.createElement('div');
        overlay.className = 'rm-modal-overlay';
        overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

        let html = '<div class="rm-modal"><h4>' + t('Dodaj do playlisty') + '</h4>';
        if (!_playlists.length) {
            html += '<p style="color:var(--text-muted);font-size:13px">' + t('Brak playlist. Utwórz playlistę w sekcji Playlisty.') + '</p>';
        } else {
            _playlists.forEach(pl => {
                html += `<div class="rm-modal-item" data-plid="${escH(pl.id)}"><i class="fas fa-list"></i> ${escH(pl.name)} <span style="color:var(--text-muted);margin-left:auto;font-size:11px">${pl.tracks.length}</span></div>`;
            });
        }
        html += '<button class="rm-modal-close">' + t('Anuluj') + '</button></div>';
        overlay.innerHTML = html;

        overlay.querySelector('.rm-modal-close').onclick = () => overlay.remove();
        overlay.querySelectorAll('.rm-modal-item').forEach(el => {
            el.onclick = async () => {
                const plId = el.dataset.plid;
                await api('/radio-music/playlists/' + plId + '/tracks', { method: 'POST', body: { track } });
                overlay.remove();
                toast(t('Dodano do: ') + _playlists.find(p => p.id === plId)?.name, 'success');
                await _loadPlaylists();
            };
        });

        bodyEl.appendChild(overlay);
    }

    /* ── Queue ──────────────────────────────────────── */

    function loadQueue(content) {
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
        content.innerHTML = '<div class="rm-empty"><i class="fas fa-spinner fa-spin"></i></div>';
        const data = await api('/radio-music/most-played?limit=30');
        const items = data.items || [];
        if (!items.length) {
            content.innerHTML = '<div class="rm-empty"><i class="fas fa-fire"></i><p>' + t('Zacznij słuchać, aby zobaczyć najczęściej grane') + '</p></div>';
            return;
        }
        let html = '<div class="rm-section-title"><i class="fas fa-fire"></i> ' + t('Najczęściej grane') + '</div>';
        html += '<div class="rm-hscroll">';
        items.forEach((item, i) => {
            const artHtml = (item.image || item.favicon)
                ? '<img src="' + escH(item.image || item.favicon) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">'
                : '<i class="fas ' + (item.type === 'podcast' ? 'fa-podcast' : item.type === 'music' ? 'fa-music' : 'fa-broadcast-tower') + '"></i>';
            const badge = item.play_count > 1 ? '<span class="rm-hcard-badge">' + item.play_count + '×</span>' : '';
            html += '<div class="rm-hcard" data-idx="' + i + '">'
                + '<div class="rm-hcard-art">' + artHtml + badge + '</div>'
                + '<div class="rm-hcard-title">' + escH(item.name) + '</div>'
                + '<div class="rm-hcard-meta">' + escH(item.meta || item.country || '') + '</div>'
                + '</div>';
        });
        html += '</div>';

        // Also show recent grid below
        html += '<div class="rm-section-title" style="margin-top:20px"><i class="fas fa-history"></i> ' + t('Ostatnio grane') + '</div>';
        html += '<div class="rm-hscroll">';
        const recent = (await api('/radio-music/history')).items || [];
        recent.slice(0, 20).forEach((item, i) => {
            const artHtml = (item.image || item.favicon)
                ? '<img src="' + escH(item.image || item.favicon) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">'
                : '<i class="fas ' + (item.type === 'podcast' ? 'fa-podcast' : item.type === 'music' ? 'fa-music' : 'fa-broadcast-tower') + '"></i>';
            html += '<div class="rm-hcard rm-hcard-recent" data-ridx="' + i + '">'
                + '<div class="rm-hcard-art">' + artHtml + '</div>'
                + '<div class="rm-hcard-title">' + escH(item.name) + '</div>'
                + '<div class="rm-hcard-meta">' + escH(item.meta || item.country || '') + '</div>'
                + '</div>';
        });
        html += '</div>';
        content.innerHTML = html;

        // Wire up clicks
        content.querySelectorAll('.rm-hcard[data-idx]').forEach(card => {
            card.onclick = () => {
                const it = items[parseInt(card.dataset.idx)];
                if (it.type === 'music') playMusicTrack(it);
                else if (it.type === 'podcast') playAudio(it);
                else playStation(it);
            };
        });
        content.querySelectorAll('.rm-hcard-recent[data-ridx]').forEach(card => {
            card.onclick = () => {
                const it = recent[parseInt(card.dataset.ridx)];
                if (it.type === 'music') playMusicTrack(it);
                else if (it.type === 'podcast') playAudio(it);
                else playStation(it);
            };
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
            homepage: station.homepage || '',
            uuid: station.uuid,
        });
    }

    function playAudio(item) {
        if (_audio) { _audio.pause(); _audio.src = ''; }
        _clearSeek();
        _audio = new Audio();
        _audio.volume = (bodyEl.querySelector('#rm-vol')?.value || 80) / 100;
        _playing = item;

        // Build ordered list of URLs to try (primary + fallbacks)
        const isMusic = item.type === 'music';
        const isLocal = item.type === 'local';
        const urls = isMusic ? [item.url] : [item.url, ...(item.alt_urls || [])];
        let urlIdx = 0;
        let hasPlayed = false;

        // ── Buffering state helpers ──
        function _setBuffering(on) {
            const player = bodyEl.querySelector('#rm-player');
            if (player) player.classList.toggle('rm-buffering', on);
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
                toast(t('Nie udało się odtworzyć żadnego źródła'), 'error');
                _showEq(false);
                _setBuffering(false);
                return;
            }
            let src;
            if (isLocal) {
                // Local files already have a full URL with token
                src = item.url;
            } else if (isMusic) {
                src = '/api/radio-music/music/stream?url=' + encodeURIComponent(urls[idx])
                    + '&token=' + (NAS.token || '');
            } else {
                src = '/api/radio-music/radio/proxy?url=' + encodeURIComponent(urls[idx])
                    + '&token=' + (NAS.token || '');
            }

            _audio.src = src;
            _audio.play().catch(err => {
                console.warn('Play error:', err?.message, 'url:', urls[idx]);
                tryUrl(idx + 1);
            });
        }

        _audio.onplay = () => {
            hasPlayed = true;
            _setBuffering(false);
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-pause"></i>';
            _showEq(!isMusic && !isLocal);
            _updateSeekbar();
        };
        _audio.onwaiting = () => _setBuffering(true);
        _audio.onplaying = () => _setBuffering(false);
        _audio.onpause = () => {
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';
            _showEq(false);
        };
        _audio.onerror = () => {
            if (!hasPlayed) {
                urlIdx++;
                tryUrl(urlIdx);
            } else {
                toast(t('Strumień przerwany'), 'error');
                _showEq(false);
                _setBuffering(false);
            }
        };
        _audio.onended = () => {
            _showEq(false);
            _clearSeek();
            // Auto-play next in queue
            if (_musicQueue.length && _musicQueueIdx >= 0) {
                _musicQueueIdx++;
                if (_musicQueueIdx < _musicQueue.length) {
                    playMusicTrack(_musicQueue[_musicQueueIdx]);
                    return;
                }
            }
            bodyEl.querySelector('#rm-play-pause').innerHTML = '<i class="fas fa-play"></i>';
        };
        _audio.ontimeupdate = () => _updateSeekbar();
        _audio.onloadedmetadata = () => _updateSeekbar();

        // Update player bar — show immediately with buffering indicator
        const player = bodyEl.querySelector('#rm-player');
        player.style.display = 'flex';
        bodyEl.querySelector('#rm-player-name').textContent = item.name;
        _setBuffering(true);  // show "Buforowanie…" until audio plays

        // Player art — use thumbnail for music, logo cascade for radio
        const art = bodyEl.querySelector('#rm-player-art');
        if (isMusic && item.image) {
            art.innerHTML = '<img src="' + escH(item.image) + '" onerror="this.outerHTML=\'<i class=\\\'fas fa-music\\\'></i>\'">';
        } else {
            const _fItem = { name: item.name, favicon: item.image, homepage: item.homepage || '', url: item.url };
            art.innerHTML = _stationIconHtml(_fItem);
        }

        // Save to history
        api('/radio-music/history', { method: 'POST', body: { item } });

        // Sync Now Playing overlay if open
        if (_npOverlay) {
            _hideNowPlaying();
            setTimeout(() => _showNowPlaying(), 100);
        }

        // Highlight playing card/track
        bodyEl.querySelectorAll('.rm-card, .rm-ep-item, .rm-track').forEach(c => c.classList.remove('rm-playing'));

        // Start playback with fallback chain
        tryUrl(0);
    }

    function _skipStation(dir) {
        // Music queue has priority
        if (_playing && _playing.type === 'music' && _musicQueue.length > 0) {
            const nextIdx = _musicQueueIdx + dir;
            if (nextIdx >= 0 && nextIdx < _musicQueue.length) {
                _musicQueueIdx = nextIdx;
                playMusicTrack(_musicQueue[nextIdx]);
            }
            return;
        }
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
        _clearSeek();
        _hideNowPlaying();
        const player = bodyEl?.querySelector('#rm-player');
        if (player) { player.style.display = 'none'; player.classList.remove('rm-buffering'); }
        bodyEl?.querySelectorAll('.rm-card.rm-buffering').forEach(c => c.classList.remove('rm-buffering'));
        _showEq(false);
    }

    function _showEq(show) {
        const eq = bodyEl?.querySelector('#rm-player-eq');
        if (eq) eq.style.display = show ? 'flex' : 'none';
    }

    function _updateSeekbar() {
        if (!_audio) return;
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

    function _fmtTime(s) {
        if (!isFinite(s)) return '0:00';
        s = Math.floor(s);
        if (s >= 3600) return Math.floor(s / 3600) + ':' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
        return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    }

    /* ── Now Playing Overlay ──────────────────────── */

    let _npOverlay = null;
    let _npSeekDragging = false;

    function _showNowPlaying() {
        if (!_playing) return;
        _hideNowPlaying();
        const item = _playing;
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
                </div>` : '<div class="rm-np-count"><i class="fas fa-signal"></i> ' + t('Transmisja na żywo') + '</div>'}
                <div class="rm-np-controls">
                    <button class="rm-np-btn" id="rm-np-prev"><i class="fas fa-step-backward"></i></button>
                    <button class="rm-np-btn rm-np-play" id="rm-np-playpause"><i class="fas ${_audio && !_audio.paused ? 'fa-pause' : 'fa-play'}"></i></button>
                    <button class="rm-np-btn" id="rm-np-next"><i class="fas fa-step-forward"></i></button>
                </div>
                <div class="rm-np-actions">
                    <button class="rm-np-action" id="rm-np-addpl"><i class="fas fa-plus"></i> ${t('Playlista')}</button>
                    <button class="rm-np-action" id="rm-np-fav"><i class="fas fa-heart"></i> ${t('Ulubione')}</button>
                    <button class="rm-np-action" id="rm-np-close2"><i class="fas fa-times"></i> ${t('Zamknij')}</button>
                </div>
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

        // Close handlers
        ov.querySelector('.rm-np-close').onclick = () => _hideNowPlaying();
        ov.querySelector('#rm-np-close2').onclick = () => _hideNowPlaying();

        // Swipe down to close
        let touchStartY = 0;
        ov.addEventListener('touchstart', (e) => { touchStartY = e.touches[0].clientY; }, { passive: true });
        ov.addEventListener('touchend', (e) => {
            const dy = e.changedTouches[0].clientY - touchStartY;
            if (dy > 80) _hideNowPlaying();
        }, { passive: true });

        // Controls
        ov.querySelector('#rm-np-playpause').onclick = () => {
            if (!_audio) return;
            if (_audio.paused) {
                _audio.play();
                ov.querySelector('#rm-np-playpause').innerHTML = '<i class="fas fa-pause"></i>';
            } else {
                _audio.pause();
                ov.querySelector('#rm-np-playpause').innerHTML = '<i class="fas fa-play"></i>';
            }
        };
        ov.querySelector('#rm-np-prev').onclick = () => _skipStation(-1);
        ov.querySelector('#rm-np-next').onclick = () => _skipStation(1);

        // Add to playlist
        ov.querySelector('#rm-np-addpl').onclick = () => {
            if (typeof _showAddToPlaylistModal === 'function') _showAddToPlaylistModal(item);
        };

        // Favorite toggle
        ov.querySelector('#rm-np-fav').onclick = async () => {
            const favBtn = ov.querySelector('#rm-np-fav');
            if (item.type === 'radio' && item.uuid) {
                await api('/radio-music/radio/favorite', { method: 'POST', body: { station: item } });
                favBtn.innerHTML = '<i class="fas fa-heart" style="color:#e74c3c"></i> ' + t('Dodano');
            } else {
                await api('/radio-music/history', { method: 'POST', body: { item } });
                favBtn.innerHTML = '<i class="fas fa-check"></i> ' + t('OK');
            }
        };

        // Seekbar for overlay
        if (hasSeek) {
            const npTrack = ov.querySelector('#rm-np-track');
            npTrack.onclick = (e) => {
                if (!_audio || !isFinite(_audio.duration)) return;
                const rect = npTrack.getBoundingClientRect();
                const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                _audio.currentTime = pct * _audio.duration;
            };

            // Touch drag for seek thumb
            const npThumb = ov.querySelector('#rm-np-thumb');
            npThumb.addEventListener('touchstart', () => { _npSeekDragging = true; }, { passive: true });
            document.addEventListener('touchmove', (e) => {
                if (!_npSeekDragging || !_audio || !isFinite(_audio.duration)) return;
                const rect = npTrack.getBoundingClientRect();
                const pct = Math.max(0, Math.min(1, (e.touches[0].clientX - rect.left) / rect.width));
                _audio.currentTime = pct * _audio.duration;
            }, { passive: true });
            document.addEventListener('touchend', () => { _npSeekDragging = false; }, { passive: true });
        }

        // Insert overlay into the window wrap
        const wrap = bodyEl.querySelector('.rm-wrap');
        if (wrap) wrap.appendChild(ov);
        _npOverlay = ov;

        // Start overlay seekbar updates
        _npUpdateLoop();
    }

    function _hideNowPlaying() {
        if (_npOverlay) {
            _npOverlay.remove();
            _npOverlay = null;
        }
    }

    function _npUpdateLoop() {
        if (!_npOverlay || !_audio) return;
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
        requestAnimationFrame(() => _npUpdateLoop());
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
