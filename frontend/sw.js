const CACHE_NAME = 'ethos-v1';
const ASSETS_TO_CACHE = [
    '/',
    '/index.html',
    '/css/style.css',
    '/css/apps.css',
    '/js/desktop.js',
    '/js/apps.js',
    '/manifest.json',
    '/offline.html',
    '/img/icon-192.png',
    '/img/icon-512.png',
    '/js/apps/storage.js',
    '/js/apps/printer.js',
    '/js/apps/resources.js',
    '/js/apps/backup.js',
    '/js/apps/terminal.js',
    '/js/apps/packages.js',
    '/js/apps/users.js',
    '/js/apps/network.js',
    '/js/apps/duplicates.js',
    '/js/apps/gallery.js',
    '/js/apps/editor.js',
    '/js/apps/code-editor.js',
    '/js/apps/downloads.js',
    '/js/apps/flasher.js',
    '/js/apps/builder.js',
    '/js/apps/updates.js',
    '/js/apps/services.js',
    '/js/apps/surveillance.js',
    '/js/apps/aichat.js',
    '/js/apps/websites.js',
    '/js/apps/naslink.js',
    '/js/apps/ssh.js',
    '/js/apps/domains.js',
    '/js/apps/stickynotes.js',
    '/js/apps/tickets.js',
    '/js/apps/familyhub.js',
    '/js/apps/fail2ban.js',
    '/js/apps/wireguard.js',
    '/js/apps/ups.js',
    '/js/apps/power.js'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS_TO_CACHE);
        })
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );
});

self.addEventListener('fetch', (event) => {
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).catch(() => {
                return caches.match('/offline.html');
            })
        );
        return;
    }

    event.respondWith(
        caches.match(event.request).then((response) => {
            return response || fetch(event.request);
        })
    );
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
