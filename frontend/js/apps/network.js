/* ═══════════════════════════════════════════════════════════
   EthOS — Network Manager
   LAN / WiFi interface management via nmcli
   ═══════════════════════════════════════════════════════════ */

AppRegistry['network'] = function (appDef) {

    const body = document.createElement('div');
    body.className = 'net-app';

    body.innerHTML = `
        <div class="net-sidebar">
            <div class="net-sidebar-logo">
                <i class="fas fa-network-wired"></i>
                <span>${t('Sieć')}</span>
            </div>
            <nav class="net-nav">
                <button class="net-nav-btn active" data-tab="overview">
                    <i class="fas fa-tachometer-alt"></i><span>${t('Przegląd')}</span>
                </button>
                <button class="net-nav-btn" data-tab="wifi">
                    <i class="fas fa-wifi"></i><span>WiFi</span>
                    <span class="net-badge" id="net-wifi-badge" style="display:none">0</span>
                </button>
                <button class="net-nav-btn" data-tab="saved">
                    <i class="fas fa-bookmark"></i><span>Zapisane sieci</span>
                </button>
                <button class="net-nav-btn" data-tab="hotspot">
                    <i class="fas fa-broadcast-tower"></i><span>Hotspot</span>
                </button>
            </nav>
        </div>
        <div class="net-main">
            <!-- Overview -->
            <div class="net-tab active" id="net-tab-overview">
                <div class="net-header">
                    <h2>Interfejsy sieciowe</h2>
                    <button class="net-btn net-btn-sm" id="net-refresh-ifaces">
                        <i class="fas fa-sync-alt"></i> ${t('Odśwież')}
                    </button>
                </div>
                <div class="net-info-bar" id="net-info-bar"></div>
                <div class="net-iface-list" id="net-iface-list">
                    <div class="net-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>
                </div>
            </div>
            <!-- WiFi -->
            <div class="net-tab" id="net-tab-wifi">
                <div class="net-header">
                    <h2>${t('Dostępne sieci WiFi')}</h2>
                    <button class="net-btn net-btn-primary" id="net-wifi-scan">
                        <i class="fas fa-radar"></i> Skanuj
                    </button>
                </div>
                <div class="net-wifi-status" id="net-wifi-status"></div>
                <div class="net-wifi-list" id="net-wifi-list">
                    <div class="net-placeholder">
                        <i class="fas fa-wifi"></i>
                        <p>${t('Kliknij')} <b>${t('Skanuj')}</b>, ${t('aby wyszukać dostępne sieci WiFi')}</p>
                    </div>
                </div>
            </div>
            <!-- Hotspot -->
            <div class="net-tab" id="net-tab-hotspot">
                <div class="net-header">
                    <h2>Hotspot WiFi</h2>
                </div>
                <div id="net-ap-content">
                    <div class="net-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>
                </div>
            </div>
            <!-- Saved -->
            <div class="net-tab" id="net-tab-saved">
                <div class="net-header">
                    <h2>Zapisane sieci WiFi</h2>
                    <button class="net-btn net-btn-sm" id="net-refresh-saved">
                        <i class="fas fa-sync-alt"></i> ${t('Odśwież')}
                    </button>
                </div>
                <div class="net-saved-list" id="net-saved-list">
                    <div class="net-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>
                </div>
            </div>
        </div>
    `;

    createWindow('network', {
        title: appDef.name || t('Sieć'),
        icon: appDef.icon || 'fa-network-wired',
        iconColor: appDef.color || '#0ea5e9',
        width: 1000,
        height: 650,
        content: body.outerHTML,
        onRender: init,
    });

    /* ───────────────────── init ───────────────────── */
    function init(container) {
        const root = container;

        // Tab navigation
        root.querySelectorAll('.net-nav-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                root.querySelectorAll('.net-nav-btn').forEach(b => b.classList.remove('active'));
                root.querySelectorAll('.net-tab').forEach(t => t.classList.remove('active'));
                btn.classList.add('active');
                const tab = root.querySelector(`#net-tab-${btn.dataset.tab}`);
                if (tab) tab.classList.add('active');
                if (btn.dataset.tab === 'wifi') loadWifiStatus();
                if (btn.dataset.tab === 'saved') loadSaved();
                if (btn.dataset.tab === 'hotspot') loadApStatus();
            });
        });

        // Buttons
        root.querySelector('#net-refresh-ifaces').addEventListener('click', loadInterfaces);
        root.querySelector('#net-wifi-scan').addEventListener('click', scanWifi);
        root.querySelector('#net-refresh-saved').addEventListener('click', loadSaved);

        // Initial load
        loadInterfaces();

        /* ──────────── Interfaces Tab ──────────── */
        async function loadInterfaces() {
            const list = root.querySelector('#net-iface-list');
            const infoBar = root.querySelector('#net-info-bar');
            list.innerHTML = `<div class="net-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;
            try {
                const data = await api('/network/interfaces');
                if (data.error) throw new Error(data.error);

                // Info bar
                infoBar.innerHTML = `
                    <div class="net-info-item">
                        <i class="fas fa-server"></i>
                        <span><b>Host:</b> ${esc(data.hostname)}</span>
                    </div>
                    <div class="net-info-item">
                        <i class="fas fa-route"></i>
                        <span><b>Brama:</b> ${data.gateway ? esc(data.gateway.ip) + ' (' + esc(data.gateway.dev) + ')' : '—'}</span>
                    </div>
                    <div class="net-info-item">
                        <i class="fas fa-globe"></i>
                        <span><b>DNS:</b> ${data.dns.length ? data.dns.map(esc).join(', ') : '—'}</span>
                    </div>
                `;

                if (!data.interfaces.length) {
                    list.innerHTML = `<div class="net-placeholder"><i class="fas fa-ethernet"></i><p>${t('Nie znaleziono interfejsów')}</p></div>`;
                    return;
                }

                list.innerHTML = '';
                data.interfaces.forEach(iface => {
                    const card = document.createElement('div');
                    card.className = `net-iface-card ${iface.state === 'UP' ? 'up' : 'down'}`;

                    const icon = iface.type === 'wifi' ? 'fa-wifi' : 'fa-ethernet';
                    const typeLabel = iface.type === 'wifi' ? 'WiFi' : 'Ethernet';
                    const stateClass = iface.state === 'UP' ? 'net-state-up' : 'net-state-down';
                    const stateText = iface.state === 'UP' ? 'Aktywny' : t('Wyłączony');

                    const ipv4 = iface.addresses.filter(a => a.family === 'inet');
                    const ipv6 = iface.addresses.filter(a => a.family === 'inet6');

                    let wifiLine = '';
                    if (iface.wifi && iface.wifi.connection) {
                        wifiLine = `<div class="net-iface-detail"><i class="fas fa-link"></i> ${t('Połączono z:')} <b>${esc(iface.wifi.connection)}</b></div>`;
                    }

                    let speedLine = '';
                    if (iface.speed && iface.speed > 0) {
                        speedLine = `<div class="net-iface-detail"><i class="fas fa-tachometer-alt"></i> ${t('Prędkość:')} <b>${iface.speed} Mb/s</b></div>`;
                    }

                    card.innerHTML = `
                        <div class="net-iface-header">
                            <div class="net-iface-icon ${iface.type}">
                                <i class="fas ${icon}"></i>
                            </div>
                            <div class="net-iface-title">
                                <h3>${esc(iface.name)}</h3>
                                <span class="net-iface-type">${typeLabel}</span>
                                <span class="net-iface-state ${stateClass}">${stateText}</span>
                            </div>
                            <div class="net-iface-actions">
                                ${iface.state === 'UP'
                                    ? `<button class="net-btn net-btn-sm net-btn-danger" data-action="down" data-iface="${esc(iface.name)}"><i class="fas fa-power-off"></i> ${t('Wyłącz')}</button>`
                                    : `<button class="net-btn net-btn-sm net-btn-success" data-action="up" data-iface="${esc(iface.name)}"><i class="fas fa-power-off"></i> ${t('Włącz')}</button>`
                                }
                            </div>
                        </div>
                        <div class="net-iface-body">
                            <div class="net-iface-details">
                                <div class="net-iface-detail"><i class="fas fa-fingerprint"></i> MAC: <b>${esc(iface.mac)}</b></div>
                                <div class="net-iface-detail"><i class="fas fa-arrows-alt-h"></i> MTU: <b>${iface.mtu}</b></div>
                                ${speedLine}
                                ${wifiLine}
                                ${ipv4.map(a => `<div class="net-iface-detail"><i class="fas fa-map-marker-alt"></i> IPv4: <b>${esc(a.address)}/${a.prefixlen}</b></div>`).join('')}
                                ${ipv6.map(a => `<div class="net-iface-detail"><i class="fas fa-map-marker-alt"></i> IPv6: <b>${esc(a.address)}/${a.prefixlen}</b> <span class="net-scope">${a.scope}</span></div>`).join('')}
                            </div>
                            <div class="net-iface-traffic">
                                <div class="net-traffic-item">
                                    <i class="fas fa-arrow-down"></i>
                                    <span>RX: <b>${formatBytes(iface.rx_bytes)}</b></span>
                                </div>
                                <div class="net-traffic-item">
                                    <i class="fas fa-arrow-up"></i>
                                    <span>TX: <b>${formatBytes(iface.tx_bytes)}</b></span>
                                </div>
                            </div>
                        </div>
                    `;

                    // Up/down actions
                    card.querySelectorAll('[data-action]').forEach(btn => {
                        btn.addEventListener('click', async () => {
                            const action = btn.dataset.action;
                            const name = btn.dataset.iface;
                            btn.disabled = true;
                            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                            try {
                                await api(`/network/interface/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
                                setTimeout(loadInterfaces, 1000);
                            } catch (e) {
                                toast(e.message, 'error');
                                btn.disabled = false;
                                btn.innerHTML = action === 'up' ? `<i class="fas fa-power-off"></i> ${t('Włącz')}` : `<i class="fas fa-power-off"></i> ${t('Wyłącz')}`;
                            }
                        });
                    });

                    list.appendChild(card);
                });

            } catch (e) {
                list.innerHTML = `<div class="net-error"><i class="fas fa-exclamation-triangle"></i> ${esc(e.message)}</div>`;
            }
        }

        /* ──────────── WiFi Tab ──────────── */
        let wifiScanned = false;

        async function loadWifiStatus() {
            const statusEl = root.querySelector('#net-wifi-status');
            try {
                const data = await api('/network/wifi/status');
                if (data.connected) {
                    statusEl.innerHTML = `
                        <div class="net-wifi-conn-info connected">
                            <div>
                                <i class="fas fa-wifi"></i>
                                <span>${t('Połączono z')} <b>${esc(data.connected.ssid)}</b></span>
                                <span class="net-wifi-signal">${signalIcon(data.connected.signal)} ${data.connected.signal}%</span>
                                ${data.ip_address ? `<span class="net-wifi-ip"><i class="fas fa-map-marker-alt"></i> ${esc(data.ip_address)}</span>` : ''}
                            </div>
                            <button class="net-btn net-btn-sm net-btn-danger" id="net-wifi-disconnect">
                                <i class="fas fa-unlink"></i> ${t('Rozłącz')}
                            </button>
                        </div>
                    `;
                    root.querySelector('#net-wifi-disconnect').addEventListener('click', disconnectWifi);
                } else {
                    statusEl.innerHTML = `<div class="net-wifi-conn-info disconnected"><i class="fas fa-wifi"></i> <span>${t('Nie połączono z żadną siecią WiFi')}</span></div>`;
                }
            } catch (e) {
                statusEl.innerHTML = '';
            }
        }

        async function scanWifi() {
            const list = root.querySelector('#net-wifi-list');
            const btn = root.querySelector('#net-wifi-scan');
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Skanowanie...';
            list.innerHTML = '<div class="net-loading"><i class="fas fa-spinner fa-spin"></i> Skanowanie sieci WiFi...</div>';

            try {
                const data = await api('/network/wifi/scan', { method: 'POST' });

                loadWifiStatus();
                wifiScanned = true;

                if (!data.networks.length) {
                    list.innerHTML = `<div class="net-placeholder"><i class="fas fa-wifi"></i><p>${t('Nie znaleziono sieci WiFi w zasięgu')}</p></div>`;
                    return;
                }

                // Update badge
                const badge = root.querySelector('#net-wifi-badge');
                badge.textContent = data.networks.length;
                badge.style.display = 'inline-flex';

                list.innerHTML = '';
                data.networks.forEach(net => {
                    const item = document.createElement('div');
                    item.className = 'net-wifi-item';

                    const secured = net.security && net.security !== '--' && net.security !== '';
                    const lockIcon = secured ? '<i class="fas fa-lock"></i>' : '<i class="fas fa-lock-open" style="opacity:0.4"></i>';

                    item.innerHTML = `
                        <div class="net-wifi-item-info">
                            <div class="net-wifi-item-signal">${signalIcon(net.signal)}</div>
                            <div class="net-wifi-item-details">
                                <div class="net-wifi-item-ssid">${esc(net.ssid)}</div>
                                <div class="net-wifi-item-meta">
                                    ${lockIcon}
                                    <span>${esc(net.security || 'Otwarta')}</span>
                                    <span class="net-wifi-item-sep">•</span>
                                    <span>${esc(net.band)}</span>
                                    <span class="net-wifi-item-sep">•</span>
                                    <span>${net.signal}%</span>
                                    ${net.rate ? `<span class="net-wifi-item-sep">•</span><span>${esc(net.rate)}</span>` : ''}
                                </div>
                            </div>
                            <button class="net-btn net-btn-primary net-btn-sm net-wifi-connect-btn" data-ssid="${esc(net.ssid)}" data-secured="${secured}">
                                <i class="fas fa-plug"></i> ${t('Połącz')}
                            </button>
                        </div>
                    `;

                    item.querySelector('.net-wifi-connect-btn').addEventListener('click', () => {
                        connectWifi(net.ssid, secured);
                    });

                    list.appendChild(item);
                });

            } catch (e) {
                list.innerHTML = `<div class="net-error"><i class="fas fa-exclamation-triangle"></i> ${esc(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-search"></i> Skanuj';
            }
        }

        async function connectWifi(ssid, secured) {
            let password = '';
            if (secured) {
                password = await promptDialog(t('Połącz z siecią ') + ssid, t('Podaj hasło WiFi:'));
                if (!password) return;
            }

            toast(t('Łączenie z ') + ssid + '...', 'info');

            try {
                await api('/network/wifi/connect', { method: 'POST', body: { ssid, password } });
                toast(t('Połączono z siecią ') + ssid, 'success');
                loadWifiStatus();
                loadInterfaces();
            } catch (e) {
                toast(e.message, 'error');
            }
        }

        async function disconnectWifi() {
            try {
                await api('/network/wifi/disconnect', { method: 'POST' });
                toast(t('Rozłączono WiFi'), 'success');
                loadWifiStatus();
                loadInterfaces();
            } catch (e) {
                toast(e.message, 'error');
            }
        }

        /* ──────────── Saved Tab ──────────── */
        async function loadSaved() {
            const list = root.querySelector('#net-saved-list');
            list.innerHTML = `<div class="net-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;
            try {
                const data = await api('/network/wifi/saved');

                if (!data.length) {
                    list.innerHTML = '<div class="net-placeholder"><i class="fas fa-bookmark"></i><p>Brak zapisanych sieci WiFi</p></div>';
                    return;
                }

                list.innerHTML = '';
                data.forEach(conn => {
                    const item = document.createElement('div');
                    item.className = 'net-saved-item';
                    item.innerHTML = `
                        <div class="net-saved-info">
                            <i class="fas fa-wifi"></i>
                            <span class="net-saved-name">${esc(conn.name)}</span>
                        </div>
                        <div class="net-saved-actions">
                            <button class="net-btn net-btn-sm net-btn-danger" data-forget="${esc(conn.name)}">
                                <i class="fas fa-trash"></i> Zapomnij
                            </button>
                        </div>
                    `;
                    item.querySelector('[data-forget]').addEventListener('click', async () => {
                        const ok = await confirmDialog(t('Zapomnij sieć'), `${t('Czy na pewno chcesz zapomnieć sieć "')}${conn.name}"?`);
                        if (!ok) return;
                        try {
                            await api('/network/wifi/forget', { method: 'POST', body: { name: conn.name } });
                            toast(t('Sieć zapomniana'), 'success');
                            loadSaved();
                        } catch (e) {
                            toast(e.message, 'error');
                        }
                    });
                    list.appendChild(item);
                });
            } catch (e) {
                list.innerHTML = `<div class="net-error"><i class="fas fa-exclamation-triangle"></i> ${esc(e.message)}</div>`;
            }
        }

        /* ──────────── Hotspot Tab ──────────── */
        async function loadApStatus() {
            const el = root.querySelector('#net-ap-content');
            el.innerHTML = `<div class="net-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;
            try {
                const data = await api('/network/ap/status');
                const active = data.active;
                el.innerHTML = `
                    <div style="padding:10px 0;">
                        <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px;">
                            <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px;">
                                <div style="width:48px;height:48px;border-radius:14px;background:${active ? 'linear-gradient(135deg,#22c55e,#16a34a)' : 'var(--bg-tertiary)'};display:flex;align-items:center;justify-content:center;">
                                    <i class="fas fa-broadcast-tower" style="font-size:20px;color:${active ? '#fff' : 'var(--text-secondary)'}"></i>
                                </div>
                                <div style="flex:1;">
                                    <div style="font-size:16px;font-weight:700;">${active ? 'Hotspot aktywny' : 'Hotspot nieaktywny'}</div>
                                    <div style="font-size:13px;color:var(--text-secondary);">
                                        ${active ? 'SSID: <b>' + esc(data.ssid) + '</b> &nbsp;|&nbsp; IP: <b>' + esc(data.ip) + '</b> &nbsp;|&nbsp; Klienci: <b>' + data.clients + '</b>' : t('Hotspot tworzy sieć WiFi "ethos" do konfiguracji')}
                                    </div>
                                </div>
                            </div>
                            <div style="display:flex;gap:10px;align-items:center;">
                                ${active
                                    ? `<button class="net-btn net-btn-danger" id="net-ap-stop"><i class="fas fa-stop"></i> ${t('Wyłącz hotspot')}</button>`
                                    : `<button class="net-btn net-btn-primary" id="net-ap-start"><i class="fas fa-play"></i> ${t('Włącz hotspot')}</button>`
                                }
                            </div>
                        </div>
                        <div style="background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.12);border-radius:10px;padding:16px;font-size:13px;line-height:1.7;color:var(--text-secondary);">
                            <div style="font-weight:600;margin-bottom:6px;color:var(--text);"><i class="fas fa-info-circle" style="margin-right:6px;"></i>${t('Jak to działa?')}</div>
                            <ul style="margin:0;padding-left:20px;">
                                <li>${t('Hotspot tworzy otwartą sieć WiFi')} <b>"ethos"</b> (${t('bez hasła')})</li>
                                <li>${t('Połącz się z telefonem lub laptopem do tej sieci')}</li>
                                <li>${t('Otwórz')} <b>http://192.168.42.1:9000</b> ${t('w przeglądarce')}</li>
                                <li>${t('W zakładce')} <b>WiFi</b> ${t('skonfiguruj docelową sieć')}</li>
                                <li>${t('Po połączeniu z WiFi hotspot wyłączy się automatycznie')}</li>
                            </ul>
                        </div>
                    </div>
                `;
                const startBtn = root.querySelector('#net-ap-start');
                const stopBtn = root.querySelector('#net-ap-stop');
                if (startBtn) startBtn.addEventListener('click', () => toggleAp('start'));
                if (stopBtn) stopBtn.addEventListener('click', () => toggleAp('stop'));
            } catch (e) {
                el.innerHTML = `<div class="net-error"><i class="fas fa-exclamation-triangle"></i> ${esc(e.message)}</div>`;
            }
        }

        async function toggleAp(action) {
            const el = root.querySelector('#net-ap-content');
            const label = action === 'start' ? 'Uruchamiam hotspot...' : t('Wyłączam hotspot...');
            el.innerHTML = `<div class="net-loading"><i class="fas fa-spinner fa-spin"></i> ${label}</div>`;
            try {
                const data = await api('/network/ap/' + action, { method: 'POST' });
                if (data.error) throw new Error(data.error);
                toast(action === 'start' ? 'Hotspot uruchomiony' : t('Hotspot wyłączony'), 'success');
                setTimeout(() => {
                    loadApStatus();
                    loadInterfaces();
                }, 1500);
            } catch (e) {
                toast(e.message, 'error');
                loadApStatus();
            }
        }

        /* ──────────── Helpers ──────────── */
        function esc(s) { const d = document.createElement('span'); d.textContent = s; return d.innerHTML; }

        function formatBytes(b) {
            if (!b) return '0 B';
            const u = ['B', 'KB', 'MB', 'GB', 'TB'];
            let i = 0;
            let v = b;
            while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
            return v.toFixed(i > 0 ? 1 : 0) + ' ' + u[i];
        }

        function signalIcon(signal) {
            if (signal >= 75) return '<i class="fas fa-signal net-sig-4"></i>';
            if (signal >= 50) return '<i class="fas fa-signal net-sig-3"></i>';
            if (signal >= 25) return '<i class="fas fa-signal net-sig-2"></i>';
            return '<i class="fas fa-signal net-sig-1"></i>';
        }
    }
};
