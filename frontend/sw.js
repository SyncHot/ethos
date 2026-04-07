// EthOS Service Worker v2
// Bump CACHE_VERSION when deploying static asset changes.
// Versioned JS/CSS (?v=N in index.html) auto-invalidate on next load.
const CACHE_VERSION = 'v3';
const STATIC_CACHE = 'ethos-static-' + CACHE_VERSION;
const RUNTIME_CACHE = 'ethos-runtime-' + CACHE_VERSION;
// Persistent offline audio cache — never purged by version bump
const RM_OFFLINE_CACHE = 'rm-offline-v1';

// Pre-cache: critical shell assets (versioned URLs from index.html ?v=N)
const PRECACHE_ASSETS = [
    '/index.html',
    '/offline.html',
    '/music.html',
    '/manifest.json',
    '/manifest-music.json',
    '/img/icon-192.png',
    '/img/icon-512.png',
    '/img/icon-music-192.png',
    '/img/icon-music-512.png',
    '/css/style.css?v=3',
    '/css/apps.css?v=4',
    '/js/i18n.js?v=2',
    '/js/desktop.js?v=11',
    '/js/apps.js?v=11',
    '/js/apps/radio_music.js?v=18',
    '/js/apps/video_station.js?v=3',
    '/js/apps/dashboard.js?v=1',
    '/js/apps/storage.js?v=9',
    '/js/apps/resources.js?v=3',
    '/js/apps/backup.js?v=3',
    '/js/apps/terminal.js?v=2',
    '/js/apps/packages.js?v=3',
    '/js/apps/users.js?v=2',
    '/js/apps/network.js?v=1',
    '/js/apps/gallery.js?v=2',
    '/js/apps/photos_ai.js?v=1',
    '/js/apps/docker-manager.js?v=1',
    '/js/apps/aichat.js?v=2',
    '/js/apps/updates.js?v=2',
    '/js/apps/firewall.js?v=3',
    '/js/apps/wireguard.js?v=2',
    '/js/apps/domains.js?v=3',
    '/js/apps/stickynotes.js?v=2',
    '/js/apps/familyhub.js?v=1',
    '/js/apps/websites.js?v=2',
    '/js/apps/naslink.js?v=1',
    '/js/apps/ssh.js?v=1',
    '/js/apps/surveillance.js?v=1',
    '/js/apps/printer.js?v=3',
    '/js/apps/builder.js?v=2',
    '/js/apps/services.js?v=1',
    '/js/apps/downloads.js?v=1',
    '/js/apps/duplicates.js?v=1',
    '/js/apps/tickets.js?v=1',
    '/js/apps/cron.js?v=1',
    '/js/apps/antivirus.js?v=1',
    '/js/apps/ups.js?v=1',
    '/js/apps/power.js?v=1',
    '/js/apps/rollback.js?v=1',
    '/js/apps/notifications.js?v=1',
    '/js/apps/doc-anonymizer.js?v=6',
    '/js/apps/med-assistant.js?v=2',
    '/js/apps/fail2ban.js?v=1',
    '/js/apps/vm-manager.js?v=1',
    '/js/apps/flasher.js?v=2',
    '/js/apps/cloud-backup.js?v=1',
    '/js/apps/security_advisor.js?v=1',
    '/js/apps/remote-log.js?v=1',
];

// Never intercept these — always hit network (streams, API, auth)
const BYPASS_PATTERNS = [
    /^\/api\//,
    /^\/hls\//,
    /^\/local\/stream/,
    /^\/music\/stream/,
    /^\/music\/file/,
    /^\/radio-music\/radio\/proxy/,
    /^\/socket\.io/,
    /^\/cdn-cgi\//,
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(STATIC_CACHE)
            .then(cache => cache.addAll(PRECACHE_ASSETS.map(url => new Request(url, { cache: 'reload' }))))
            .then(() => self.skipWaiting())
            .catch(err => {
                // Don't fail install if some optional assets are missing
                console.warn('[SW] Precache partial failure:', err);
                return self.skipWaiting();
            })
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys
                    .filter(k => k !== STATIC_CACHE && k !== RUNTIME_CACHE && k !== RM_OFFLINE_CACHE)
                    .map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Archive file: serve from offline cache if available (offline-first for cached tracks)
    if (url.pathname.includes('/api/radio-music/archive/file/')) {
        const cacheKey = url.pathname; // stable key without token query param
        event.respondWith(
            caches.open(RM_OFFLINE_CACHE).then(cache =>
                cache.match(cacheKey).then(cached => {
                    if (cached) return cached;
                    // Not in cache — fetch from network and store for future offline use
                    return fetch(event.request).then(resp => {
                        if (resp.ok) cache.put(cacheKey, resp.clone());
                        return resp;
                    });
                })
            )
        );
        return;
    }

    // Skip non-GET, cross-origin, and bypass patterns
    if (event.request.method !== 'GET') return;
    if (url.origin !== self.location.origin) return;
    if (BYPASS_PATTERNS.some(p => p.test(url.pathname))) return;

    // Navigation requests: stale-while-revalidate
    // Serve cached instantly (fast load), fetch update in background.
    // Fallback to offline.html when completely offline.
    if (event.request.mode === 'navigate') {
        event.respondWith(
            caches.match(event.request, { ignoreSearch: false }).then(cached => {
                const networkFetch = fetch(event.request).then(response => {
                    if (response.ok) {
                        caches.open(RUNTIME_CACHE).then(c => c.put(event.request, response.clone()));
                    }
                    return response;
                }).catch(() => null);

                // Return cached immediately; or wait for network; fallback offline.html
                if (cached) {
                    networkFetch.catch(() => {});
                    return cached;
                }
                return networkFetch.then(r => r || caches.match('/offline.html'));
            })
        );
        return;
    }

    // Static assets: Cache-First (versioned with ?v=N so stale entries don't conflict)
    event.respondWith(
        caches.match(event.request).then(cached => {
            if (cached) return cached;
            return fetch(event.request).then(response => {
                if (response.ok) {
                    const clone = response.clone();
                    caches.open(RUNTIME_CACHE).then(c => c.put(event.request, clone));
                }
                return response;
            }).catch(() => {
                if (event.request.destination === 'document') return caches.match('/offline.html');
            });
        })
    );
});

// ── Offline Audio Cache Messages ─────────────────────────────
// RM_CACHE_AUDIO: cache an archive file to rm-offline-v1 for offline playback
// RM_CACHE_STATUS: check if a key is cached
// RM_UNCACHE_AUDIO: remove a cached file
self.addEventListener('message', (event) => {
    const { type, key, url } = event.data || {};
    if (!type) return;

    if (type === 'RM_CACHE_AUDIO' && url && key) {
        const cacheKey = '/api/radio-music/archive/file/' + key;
        event.waitUntil(
            caches.open(RM_OFFLINE_CACHE).then(cache =>
                fetch(url, { credentials: 'include' })
                    .then(resp => {
                        if (resp.ok) {
                            return cache.put(cacheKey, resp).then(() => {
                                event.source?.postMessage({ type: 'RM_CACHE_DONE', key });
                            });
                        }
                        throw new Error('fetch failed: ' + resp.status);
                    })
                    .catch(err => {
                        event.source?.postMessage({ type: 'RM_CACHE_ERROR', key, error: err.message });
                    })
            )
        );
        return;
    }

    if (type === 'RM_CACHE_STATUS' && key) {
        const cacheKey = '/api/radio-music/archive/file/' + key;
        event.waitUntil(
            caches.open(RM_OFFLINE_CACHE).then(cache =>
                cache.match(cacheKey).then(cached => {
                    event.source?.postMessage({ type: 'RM_CACHE_STATUS_RESULT', key, cached: !!cached });
                })
            )
        );
        return;
    }

    if (type === 'RM_UNCACHE_AUDIO' && key) {
        const cacheKey = '/api/radio-music/archive/file/' + key;
        event.waitUntil(
            caches.open(RM_OFFLINE_CACHE).then(cache => cache.delete(cacheKey))
        );
        return;
    }
});

self.addEventListener('push', (event) => {
    const data = event.data ? event.data.json() : {};
    const title = data.title || 'EthOS';
    const options = {
        body: data.body || 'Nowe powiadomienie',
        icon: '/img/icon-192.png',
        badge: '/img/icon-192.png',
        data: data.url || '/'
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
            if (clientList.length > 0) {
                let client = clientList[0];
                for (let i = 0; i < clientList.length; i++) {
                    if (clientList[i].focused) {
                        client = clientList[i];
                    }
                }
                return client.focus();
            }
            return clients.openWindow(event.notification.data);
        })
    );
});

// Periodic Background Sync — refresh radio station list and music catalog
// Registered by app with tag "rm-catalog-refresh" (24h interval)
self.addEventListener("periodicsync", (event) => {
    if (event.tag === "rm-catalog-refresh") {
        event.waitUntil(
            caches.open(STATIC_CACHE).then(async (cache) => {
                const urls = ["/api/radio/stations", "/api/music/recent"];
                for (const url of urls) {
                    try {
                        const resp = await fetch(url, { credentials: "include" });
                        if (resp.ok) await cache.put(url, resp);
                    } catch (_) { /* offline - skip */ }
                }
            })
        );
    }
});
