/* ═══════════════════════════════════════════════════════════
   EthOS — WireGuard VPN Manager
   ═══════════════════════════════════════════════════════════ */

AppRegistry['wireguard'] = function (appDef) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('wireguard', level, msg, details) : console.log('[wireguard]', msg, details || '');

    function esc(str) {
        if (typeof str !== 'string') return str;
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    }

    const win = createWindow('wireguard', {
        title: 'VPN (WireGuard)',
        icon: 'fa-shield-halved',
        iconColor: '#7c3aed',
        width: 820,
        height: 600,
        resizable: true,
        maximizable: true
    });

    const body = win.body;
    body.style.cssText = 'display:flex;flex-direction:column;height:100%;overflow:hidden;background:var(--window-bg)';

    body.innerHTML = `
        <!-- Status bar -->
        <div class="pwr-status-bar">
            <div class="pwr-status-chip" id="wg-state-chip">
                <i class="fas fa-shield-halved" style="color:#7c3aed"></i>
                <span>${t('Ładowanie...')}</span>
            </div>
            <div class="pwr-status-chip" id="wg-endpoint-chip" style="display:none">
                <i class="fas fa-globe" style="color:#3b82f6"></i>
                <span id="wg-endpoint-text"></span>
            </div>
        </div>

        <!-- Scrollable body -->
        <div class="pwr-scroll">

            <!-- VPN Toggle card -->
            <div class="pwr-card">
                <div class="pwr-card-header">
                    <i class="fas fa-shield-halved" style="color:#7c3aed"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">WireGuard VPN</div>
                        <div class="pwr-card-sub">${t('Bezpieczny dostęp do sieci domowej z dowolnego miejsca')}</div>
                    </div>
                    <label class="pwr-toggle" title="${t('Włącz / wyłącz VPN')}">
                        <input type="checkbox" id="wg-toggle">
                        <span class="pwr-toggle-track"><span class="pwr-toggle-thumb"></span></span>
                    </label>
                </div>
                <div id="wg-port-row" class="pwr-info-row" style="display:none">
                    <span class="pwr-info-label"><i class="fas fa-plug"></i> Port</span>
                    <span class="pwr-info-val" id="wg-port-val"></span>
                </div>
            </div>

            <!-- Peers card -->
            <div class="pwr-card">
                <div class="pwr-card-header">
                    <i class="fas fa-laptop-mobile" style="color:#3b82f6"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">${t('Urządzenia (Peery)')}</div>
                        <div class="pwr-card-sub">${t('Klienci VPN z dostępem do sieci')}</div>
                    </div>
                    <button id="wg-add-btn" class="app-btn app-btn-accent app-btn-sm">
                        <i class="fas fa-plus"></i> ${t('Dodaj')}
                    </button>
                </div>
                <div id="wg-peers-list" style="padding:0 0 4px 0"></div>
            </div>

        </div><!-- end .pwr-scroll -->

        <!-- Add Peer overlay (inside window) -->
        <div id="wg-add-overlay" class="pwr-overlay" style="position:absolute">
            <div class="pwr-dialog">
                <div class="pwr-dialog-header">
                    <i class="fas fa-plus" style="color:#7c3aed"></i>
                    <span>${t('Dodaj urządzenie')}</span>
                    <button class="pwr-icon-btn" id="wg-add-x"><i class="fas fa-times"></i></button>
                </div>
                <div class="pwr-dialog-body">
                    <div>
                        <label style="display:block;font-size:12px;color:var(--text-muted);margin-bottom:6px">${t('Nazwa urządzenia')}</label>
                        <input id="wg-peer-name" type="text" class="app-input" placeholder="${t('np. Telefon, Laptop')}" style="width:100%;box-sizing:border-box">
                    </div>
                </div>
                <div class="pwr-dialog-footer">
                    <button class="pwr-btn-ghost" id="wg-add-cancel">${t('Anuluj')}</button>
                    <button class="pwr-btn-primary" id="wg-add-confirm" style="background:#7c3aed"><i class="fas fa-key"></i> ${t('Generuj')}</button>
                </div>
            </div>
        </div>

        <!-- QR/Config overlay -->
        <div id="wg-qr-overlay" class="pwr-overlay" style="position:absolute">
            <div class="pwr-dialog" style="width:480px">
                <div class="pwr-dialog-header">
                    <i class="fas fa-qrcode" style="color:#7c3aed"></i>
                    <span id="wg-qr-title">${t('Konfiguracja')}</span>
                    <button class="pwr-icon-btn" id="wg-qr-x"><i class="fas fa-times"></i></button>
                </div>
                <div class="pwr-dialog-body">
                    <div style="text-align:center">
                        <img id="wg-qr-img" style="max-width:200px;border-radius:8px;border:4px solid #fff;display:none" src="" alt="QR">
                        <div id="wg-qr-missing" style="display:none;color:var(--text-muted);font-size:13px;padding:16px">
                            <i class="fas fa-triangle-exclamation" style="color:#f59e0b"></i> ${t('Brak QR (qrencode niedostępny)')}
                        </div>
                    </div>
                    <div>
                        <label style="display:block;font-size:12px;color:var(--text-muted);margin-bottom:6px">${t('Plik konfiguracyjny')}</label>
                        <textarea id="wg-conf-text" readonly style="width:100%;height:150px;font-family:monospace;font-size:11px;background:var(--bg-default);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:8px;box-sizing:border-box;resize:vertical"></textarea>
                    </div>
                </div>
                <div class="pwr-dialog-footer">
                    <button class="pwr-btn-ghost" id="wg-qr-close">${t('Zamknij')}</button>
                    <button class="pwr-btn-primary" id="wg-qr-download" style="background:#7c3aed"><i class="fas fa-download"></i> ${t('Pobierz .conf')}</button>
                </div>
            </div>
        </div>
    `;

    const toggle       = body.querySelector('#wg-toggle');
    const stateChip    = body.querySelector('#wg-state-chip');
    const endpointChip = body.querySelector('#wg-endpoint-chip');
    const portRow      = body.querySelector('#wg-port-row');
    const peersList    = body.querySelector('#wg-peers-list');
    const addOverlay   = body.querySelector('#wg-add-overlay');
    const qrOverlay    = body.querySelector('#wg-qr-overlay');

    function fmtBytes(b) {
        if (!b) return '0 B';
        if (b < 1024) return b + ' B';
        if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
        if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
        return (b / 1073741824).toFixed(2) + ' GB';
    }

    function fmtHandshake(ts) {
        if (!ts) return t('Nigdy');
        const diff = Math.floor(Date.now() / 1000) - ts;
        if (diff < 120) return t('Przed chwilą');
        if (diff < 3600) return Math.floor(diff / 60) + ' min temu';
        if (diff < 86400) return Math.floor(diff / 3600) + ' godz. temu';
        return Math.floor(diff / 86400) + ' dni temu';
    }

    function renderPeers(peers) {
        if (!peers || peers.length === 0) {
            peersList.innerHTML = `<div class="pwr-empty"><i class="fas fa-mobile-screen-button"></i> ${t('Brak urządzeń. Kliknij „Dodaj" aby wygenerować pierwszą konfigurację.')}</div>`;
            return;
        }
        peersList.innerHTML = peers.map(p => {
            const active = p.status === 'active';
            const name = esc(p.Name || p.PublicKey.slice(0, 12) + '…');
            return `
            <div class="pwr-info-row" style="flex-wrap:wrap;gap:8px;">
                <span class="pwr-badge ${active ? 'pwr-badge-green' : 'pwr-badge-gray'}" style="flex-shrink:0">
                    ${active ? t('Online') : t('Offline')}
                </span>
                <div style="flex:1;min-width:0">
                    <div style="font-size:13px;font-weight:600;color:var(--text-primary)">${name}</div>
                    <div style="font-family:monospace;font-size:10px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.PublicKey)}</div>
                    ${active ? `<div style="font-size:11px;color:var(--text-muted)">↓ ${fmtBytes(p.transfer_rx)} ↑ ${fmtBytes(p.transfer_tx)} · ${fmtHandshake(p.latest_handshake)}</div>` : ''}
                </div>
                <button class="pwr-icon-btn wg-delete-btn" data-key="${esc(p.PublicKey)}" data-name="${name}" title="${t('Usuń')}" style="color:var(--danger,#ef4444)">
                    <i class="fas fa-trash"></i>
                </button>
            </div>`;
        }).join('');

        peersList.querySelectorAll('.wg-delete-btn').forEach(btn => {
            btn.addEventListener('click', () => deletePeer(btn.dataset.key, btn.dataset.name));
        });
    }

    async function loadStatus() {
        try {
            const r = await api('/wireguard/status');
            if (!r.installed) {
                stateChip.innerHTML = `<i class="fas fa-circle-xmark" style="color:#ef4444"></i> <span>${t('WireGuard nie zainstalowany')}</span>`;
                toggle.disabled = true;
                peersList.innerHTML = `<div class="pwr-empty"><i class="fas fa-circle-xmark" style="color:#ef4444"></i> ${t('Zainstaluj WireGuard z App Store.')}</div>`;
                return;
            }
            const active = r.active;
            toggle.checked = active;
            stateChip.innerHTML = `<i class="fas fa-shield-halved" style="color:${active ? '#7c3aed' : 'var(--text-muted)'}"></i> <span style="color:${active ? '#7c3aed' : 'var(--text-muted)'}">${active ? t('Aktywny') : t('Nieaktywny')}</span>`;

            if (r.hostname) {
                endpointChip.style.display = 'flex';
                body.querySelector('#wg-endpoint-text').textContent = r.hostname + ':' + (r.port || 51820);
                portRow.style.display = 'flex';
                body.querySelector('#wg-port-val').textContent = r.port || 51820;
            } else {
                endpointChip.style.display = 'none';
                portRow.style.display = 'none';
            }
            renderPeers(r.peers);
        } catch (e) {
            stateChip.innerHTML = `<i class="fas fa-triangle-exclamation" style="color:#f59e0b"></i> <span>${t('Błąd')}</span>`;
            peersList.innerHTML = `<div class="pwr-empty" style="color:var(--danger,#ef4444)"><i class="fas fa-triangle-exclamation"></i> ${esc(String(e))}</div>`;
        }
    }

    toggle.addEventListener('change', async () => {
        const enable = toggle.checked;
        stateChip.innerHTML = `<i class="fas fa-spinner fa-spin" style="color:#7c3aed"></i> <span>${enable ? t('Uruchamianie…') : t('Zatrzymywanie…')}</span>`;
        toggle.disabled = true;
        try {
            await api('/wireguard/toggle', { method: 'POST', body: { enable } });
            await loadStatus();
        } catch (e) {
            showNotification(t('Błąd:') + ' ' + e.message, 'error');
            await loadStatus();
        } finally {
            toggle.disabled = false;
        }
    });

    // Add peer
    function openAddModal() {
        body.querySelector('#wg-peer-name').value = '';
        addOverlay.classList.add('visible');
        setTimeout(() => body.querySelector('#wg-peer-name').focus(), 50);
    }
    function closeAddModal() { addOverlay.classList.remove('visible'); }

    body.querySelector('#wg-add-btn').addEventListener('click', openAddModal);
    body.querySelector('#wg-add-cancel').addEventListener('click', closeAddModal);
    body.querySelector('#wg-add-x').addEventListener('click', closeAddModal);
    body.querySelector('#wg-peer-name').addEventListener('keydown', e => { if (e.key === 'Enter') body.querySelector('#wg-add-confirm').click(); });
    addOverlay.addEventListener('click', e => { if (e.target === addOverlay) closeAddModal(); });

    body.querySelector('#wg-add-confirm').addEventListener('click', async () => {
        const name = body.querySelector('#wg-peer-name').value.trim() || t('Urządzenie');
        closeAddModal();
        stateChip.innerHTML = `<i class="fas fa-spinner fa-spin" style="color:#7c3aed"></i> <span>${t('Generowanie…')}</span>`;
        try {
            const r = await api('/wireguard/peer', { method: 'POST', body: { name } });
            showQR(name, r.config, r.qr_code);
            await loadStatus();
        } catch (e) {
            showNotification(t('Błąd dodawania peera:') + ' ' + e.message, 'error');
            await loadStatus();
        }
    });

    // QR modal
    let _currentConf = '', _currentPeerName = '';

    function showQR(name, conf, qrB64) {
        _currentConf = conf;
        _currentPeerName = name;
        body.querySelector('#wg-qr-title').textContent = name;
        body.querySelector('#wg-conf-text').value = conf;
        const img = body.querySelector('#wg-qr-img');
        const missing = body.querySelector('#wg-qr-missing');
        if (qrB64) {
            img.src = 'data:image/png;base64,' + qrB64;
            img.style.display = 'block';
            missing.style.display = 'none';
        } else {
            img.style.display = 'none';
            missing.style.display = 'block';
        }
        qrOverlay.classList.add('visible');
    }

    body.querySelector('#wg-qr-close').addEventListener('click', () => qrOverlay.classList.remove('visible'));
    body.querySelector('#wg-qr-x').addEventListener('click', () => qrOverlay.classList.remove('visible'));
    qrOverlay.addEventListener('click', e => { if (e.target === qrOverlay) qrOverlay.classList.remove('visible'); });

    body.querySelector('#wg-qr-download').addEventListener('click', () => {
        const blob = new Blob([_currentConf], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = (_currentPeerName || 'peer').replace(/[^a-zA-Z0-9_-]/g, '_') + '.conf';
        a.click();
        URL.revokeObjectURL(url);
    });

    async function deletePeer(pubKey, name) {
        if (!await confirmDialog(t('Usunąć peera') + ` "${name}"?`)) return;
        try {
            await api('/wireguard/peer/' + encodeURIComponent(pubKey), { method: 'DELETE' });
            await loadStatus();
        } catch (e) {
            showNotification(t('Błąd usuwania:') + ' ' + e.message, 'error');
        }
    }

    loadStatus();
};
