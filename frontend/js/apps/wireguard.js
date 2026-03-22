/* ═══════════════════════════════════════════════════════════
   EthOS — WireGuard VPN Manager
   Zarządzanie serwerem VPN: peery, QR kody, status
   ═══════════════════════════════════════════════════════════ */

AppRegistry['wireguard'] = function (appDef) {
    function esc(str) {
        if (typeof str !== 'string') return str;
        return str.replace(/&/g, '&amp;')
                  .replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;')
                  .replace(/'/g, '&#039;');
    }

    const win = createWindow('wireguard', {
        title: 'VPN (WireGuard)',
        icon: 'fa-shield-halved',
        iconColor: '#7c3aed',
        width: 820,
        height: 640,
        resizable: true,
        maximizable: true
    });

    const body = win.body;
    body.style.cssText = 'display:flex;flex-direction:column;background:var(--bg-default);color:var(--text-default);padding:20px;gap:16px;overflow-y:auto';

    body.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
            <div>
                <h2 style="margin:0 0 4px 0;display:flex;align-items:center;gap:10px">
                    <i class="fas fa-shield-halved" style="color:#7c3aed"></i>
                    WireGuard VPN
                </h2>
                <p style="margin:0;opacity:0.65;font-size:13px">Bezpieczny dostęp do sieci domowej z dowolnego miejsca</p>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
                <span id="wg-status-badge" style="font-size:12px;padding:3px 10px;border-radius:20px;background:var(--bg-surface);border:1px solid var(--border)">Ładowanie...</span>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px">
                    <span>VPN</span>
                    <div id="wg-toggle-wrap" style="position:relative;width:44px;height:24px">
                        <input type="checkbox" id="wg-toggle" style="opacity:0;width:0;height:0;position:absolute">
                        <span id="wg-toggle-track" style="position:absolute;inset:0;border-radius:12px;background:#555;transition:background 0.2s;cursor:pointer"></span>
                        <span id="wg-toggle-thumb" style="position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:transform 0.2s;pointer-events:none"></span>
                    </div>
                </label>
            </div>
        </div>

        <div id="wg-info-bar" style="background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:13px;display:none">
            <span style="opacity:0.7">Endpoint DDNS: </span>
            <span id="wg-endpoint" style="font-family:monospace;font-weight:600"></span>
            <span style="opacity:0.7;margin-left:16px">Port: </span>
            <span id="wg-port" style="font-family:monospace;font-weight:600"></span>
        </div>

        <div style="display:flex;align-items:center;justify-content:space-between">
            <h3 style="margin:0;font-size:15px">Peery (urządzenia)</h3>
            <button id="wg-add-btn" class="app-btn app-btn-accent" style="font-size:13px">
                <i class="fas fa-plus"></i> Dodaj urządzenie
            </button>
        </div>

        <div id="wg-peers-list" style="display:flex;flex-direction:column;gap:10px">
            <div style="opacity:0.5;text-align:center;padding:20px">Ładowanie...</div>
        </div>

        <!-- Add Peer Modal -->
        <div id="wg-add-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;align-items:center;justify-content:center">
            <div style="background:var(--bg-surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:380px;max-width:95vw">
                <h3 style="margin:0 0 16px 0">Dodaj urządzenie</h3>
                <label style="display:block;margin-bottom:8px;font-size:13px;opacity:0.8">Nazwa urządzenia</label>
                <input id="wg-peer-name" type="text" class="app-input" placeholder="np. Telefon, Laptop" style="width:100%;margin-bottom:16px;box-sizing:border-box">
                <div style="display:flex;gap:10px;justify-content:flex-end">
                    <button id="wg-add-cancel" class="app-btn">Anuluj</button>
                    <button id="wg-add-confirm" class="app-btn app-btn-accent"><i class="fas fa-check"></i> Generuj</button>
                </div>
            </div>
        </div>

        <!-- QR/Config Modal -->
        <div id="wg-qr-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9999;align-items:center;justify-content:center">
            <div style="background:var(--bg-surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto">
                <h3 style="margin:0 0 4px 0" id="wg-qr-title">Konfiguracja peera</h3>
                <p style="margin:0 0 16px 0;font-size:13px;opacity:0.7">Zeskanuj QR kodem lub pobierz plik .conf</p>
                <div id="wg-qr-img-wrap" style="text-align:center;margin-bottom:16px">
                    <img id="wg-qr-img" style="max-width:220px;border-radius:8px;border:4px solid #fff" src="" alt="QR code">
                    <div id="wg-qr-missing" style="display:none;opacity:0.5;font-size:13px;padding:20px">Brak QR (qrencode niedostępny)</div>
                </div>
                <textarea id="wg-conf-text" readonly style="width:100%;height:160px;font-family:monospace;font-size:11px;background:var(--bg-default);color:var(--text-default);border:1px solid var(--border);border-radius:6px;padding:8px;box-sizing:border-box;resize:vertical"></textarea>
                <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px">
                    <button id="wg-qr-download" class="app-btn app-btn-accent"><i class="fas fa-download"></i> Pobierz .conf</button>
                    <button id="wg-qr-close" class="app-btn">Zamknij</button>
                </div>
            </div>
        </div>
    `;

    const statusBadge = body.querySelector('#wg-status-badge');
    const toggle = body.querySelector('#wg-toggle');
    const toggleTrack = body.querySelector('#wg-toggle-track');
    const toggleThumb = body.querySelector('#wg-toggle-thumb');
    const infoBar = body.querySelector('#wg-info-bar');
    const peersList = body.querySelector('#wg-peers-list');
    const addBtn = body.querySelector('#wg-add-btn');
    const addModal = body.querySelector('#wg-add-modal');
    const qrModal = body.querySelector('#wg-qr-modal');

    function setToggleUI(active) {
        toggle.checked = active;
        toggleTrack.style.background = active ? '#7c3aed' : '#555';
        toggleThumb.style.transform = active ? 'translateX(20px)' : 'translateX(0)';
    }

    function fmtBytes(b) {
        if (!b) return '0 B';
        if (b < 1024) return b + ' B';
        if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
        if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' MB';
        return (b / 1024 / 1024 / 1024).toFixed(2) + ' GB';
    }

    function fmtHandshake(ts) {
        if (!ts) return 'Nigdy';
        const diff = Math.floor(Date.now() / 1000) - ts;
        if (diff < 120) return 'Przed chwilą';
        if (diff < 3600) return Math.floor(diff / 60) + ' min temu';
        if (diff < 86400) return Math.floor(diff / 3600) + ' godz. temu';
        return Math.floor(diff / 86400) + ' dni temu';
    }

    function renderPeers(peers) {
        if (!peers || peers.length === 0) {
            peersList.innerHTML = `<div style="opacity:0.5;text-align:center;padding:30px;border:1px dashed var(--border);border-radius:8px">
                Brak peerów. Kliknij „Dodaj urządzenie" aby wygenerować pierwszą konfigurację.
            </div>`;
            return;
        }
        peersList.innerHTML = peers.map(p => {
            const active = p.status === 'active';
            const dot = active ? '#22c55e' : '#6b7280';
            const name = esc(p.Name || p.PublicKey.slice(0, 12) + '…');
            return `
            <div style="background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
                <div style="display:flex;align-items:center;gap:10px;min-width:0">
                    <span style="width:10px;height:10px;border-radius:50%;background:${dot};flex-shrink:0"></span>
                    <div style="min-width:0">
                        <div style="font-weight:600;font-size:14px">${name}</div>
                        <div style="font-family:monospace;font-size:11px;opacity:0.6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.PublicKey)}</div>
                        <div style="font-size:12px;opacity:0.7;margin-top:2px">
                            ${active ? `↓ ${fmtBytes(p.transfer_rx)} ↑ ${fmtBytes(p.transfer_tx)} · ostatnie połączenie: ${fmtHandshake(p.latest_handshake)}` : 'Nieaktywny'}
                        </div>
                    </div>
                </div>
                <button class="app-btn app-btn-sm wg-delete-btn" data-key="${esc(p.PublicKey)}" data-name="${name}" style="color:#ef4444;flex-shrink:0">
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
                statusBadge.textContent = 'wireguard-tools nie zainstalowane';
                statusBadge.style.color = '#ef4444';
                setToggleUI(false);
                toggle.disabled = true;
                peersList.innerHTML = `<div style="opacity:0.6;padding:20px">WireGuard nie jest zainstalowany w systemie.</div>`;
                return;
            }
            const active = r.active;
            statusBadge.textContent = active ? 'Aktywny' : 'Nieaktywny';
            statusBadge.style.background = active ? 'rgba(124,58,237,0.15)' : 'var(--bg-surface)';
            statusBadge.style.color = active ? '#7c3aed' : 'var(--text-muted)';
            statusBadge.style.borderColor = active ? '#7c3aed' : 'var(--border)';
            setToggleUI(active);
            if (r.hostname) {
                infoBar.style.display = 'block';
                body.querySelector('#wg-endpoint').textContent = r.hostname;
                body.querySelector('#wg-port').textContent = r.port || 51820;
            }
            renderPeers(r.peers);
        } catch (e) {
            statusBadge.textContent = 'Błąd';
            peersList.innerHTML = `<div style="color:#ef4444;padding:10px">Błąd: ${esc(String(e))}</div>`;
        }
    }

    toggle.addEventListener('change', async () => {
        const enable = toggle.checked;
        setToggleUI(enable);
        statusBadge.textContent = enable ? 'Uruchamianie…' : 'Zatrzymywanie…';
        toggle.disabled = true;
        try {
            await api('/wireguard/toggle', { method: 'POST', body: { enable } });
            await loadStatus();
        } catch (e) {
            showNotification('Błąd: ' + e.message, 'error');
            await loadStatus();
        } finally {
            toggle.disabled = false;
        }
    });

    // Click on the track also toggles
    body.querySelector('#wg-toggle-track').addEventListener('click', () => toggle.click());

    // Add peer flow
    addBtn.addEventListener('click', () => {
        body.querySelector('#wg-peer-name').value = '';
        addModal.style.display = 'flex';
        setTimeout(() => body.querySelector('#wg-peer-name').focus(), 50);
    });
    body.querySelector('#wg-add-cancel').addEventListener('click', () => { addModal.style.display = 'none'; });
    body.querySelector('#wg-peer-name').addEventListener('keydown', e => { if (e.key === 'Enter') body.querySelector('#wg-add-confirm').click(); });

    body.querySelector('#wg-add-confirm').addEventListener('click', async () => {
        const name = body.querySelector('#wg-peer-name').value.trim() || 'Urządzenie';
        addModal.style.display = 'none';
        statusBadge.textContent = 'Generowanie…';
        try {
            const r = await api('/wireguard/peer', { method: 'POST', body: { name } });
            showQR(name, r.config, r.qr_code);
            await loadStatus();
        } catch (e) {
            showNotification('Błąd dodawania peera: ' + e.message, 'error');
            await loadStatus();
        }
    });

    // QR modal
    let _currentConf = '';
    let _currentPeerName = '';

    function showQR(name, conf, qrB64) {
        _currentConf = conf;
        _currentPeerName = name;
        body.querySelector('#wg-qr-title').textContent = 'Konfiguracja: ' + name;
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
        qrModal.style.display = 'flex';
    }

    body.querySelector('#wg-qr-close').addEventListener('click', () => { qrModal.style.display = 'none'; });
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
        if (!confirm(`Usunąć peera "${name}"?`)) return;
        try {
            await api('/wireguard/peer/' + encodeURIComponent(pubKey), { method: 'DELETE' });
            await loadStatus();
        } catch (e) {
            showNotification('Błąd usuwania: ' + e.message, 'error');
        }
    }

    loadStatus();
};
