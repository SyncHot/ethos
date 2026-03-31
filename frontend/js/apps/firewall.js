/* ═══════════════════════════════════════════════════════════
   EthOS — Firewall Manager (UFW)
   Manage firewall rules, quick presets, and banned IPs
   ═══════════════════════════════════════════════════════════ */

AppRegistry['firewall'] = function (appDef) {
    let _lanSubnet = '';
    let _rules = [];

    const win = createWindow('firewall', {
        title: t('Firewall'),
        icon: 'fa-fire',
        iconColor: '#e05d44',
        width: 960,
        height: 700,
        resizable: true,
        maximizable: true
    });

    const body = win.body;
    body.className = 'fw-body';

    body.innerHTML = '<div class="fw-header">' +
        '<div class="fw-header-left">' +
            '<h2 class="fw-title"><i class="fas fa-shield-alt"></i> Firewall (UFW)</h2>' +
            '<div class="fw-status" id="fw-status"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div>' +
        '</div>' +
        '<div class="fw-header-right">' +
            '<span class="fw-toggle-label" id="fw-toggle-label">' + t('Wyłączony') + '</span>' +
            '<label class="fw-switch"><input type="checkbox" id="fw-toggle"><span class="fw-switch-slider"></span></label>' +
        '</div>' +
    '</div>' +
    '<div class="fw-tabs" id="fw-tabs">' +
        '<button class="fw-tab active" data-tab="rules"><i class="fas fa-list-ul"></i> ' + t('Reguły') + '</button>' +
        '<button class="fw-tab" data-tab="banned"><i class="fas fa-ban"></i> ' + t('Zablokowane IP') + '</button>' +
    '</div>' +
    '<div class="fw-content" id="fw-content">' +
        '<div class="fw-panel" id="fw-panel-rules">' +
            '<div class="fw-section">' +
                '<div class="fw-section-title">' + t('Szybkie presety') + '</div>' +
                '<div class="fw-preset-grid" id="fw-preset-grid"></div>' +
            '</div>' +
            '<div class="fw-section">' +
                '<div class="fw-section-title">' + t('Dodaj regułę') + '</div>' +
                '<div class="fw-form-row">' +
                    '<div class="fw-form-group"><label>' + t('Port(y)') + '</label><input type="text" class="app-input" id="fw-port" placeholder="np. 8080, 3000:3100"></div>' +
                    '<div class="fw-form-group fw-form-narrow"><label>' + t('Protokół') + '</label><select class="app-input" id="fw-proto"><option value="tcp">TCP</option><option value="udp">UDP</option><option value="">TCP+UDP</option></select></div>' +
                    '<div class="fw-form-group fw-form-narrow"><label>' + t('Akcja') + '</label><select class="app-input" id="fw-action"><option value="allow">' + t('Allow') + '</option><option value="deny">' + t('Deny') + '</option><option value="limit">' + t('Limit') + '</option></select></div>' +
                    '<div class="fw-form-group"><label>' + t('Dostęp') + '</label><select class="app-input" id="fw-access"><option value="lan">🏠 ' + t('Tylko LAN') + '</option><option value="public">🌐 ' + t('Publiczny') + '</option><option value="custom">📍 ' + t('Własny IP') + '</option></select></div>' +
                    '<div class="fw-form-group fw-form-custom-ip hidden" id="fw-custom-ip-group"><label>' + t('Adres IP / podsieć') + '</label><input type="text" class="app-input" id="fw-custom-ip" placeholder="np. 10.0.0.0/8"></div>' +
                    '<div class="fw-form-group"><label>' + t('Komentarz') + '</label><input type="text" class="app-input" id="fw-comment" placeholder="' + t('np. Serwer WWW') + '"></div>' +
                    '<div class="fw-form-group fw-form-action"><label>&nbsp;</label><button class="app-btn app-btn-primary" id="fw-add-btn"><i class="fas fa-plus"></i> ' + t('Dodaj') + '</button></div>' +
                '</div>' +
                '<div class="fw-form-hint" id="fw-subnet-hint"></div>' +
            '</div>' +
            '<div class="fw-section">' +
                '<div class="fw-section-header">' +
                    '<span class="fw-section-title">' + t('Aktywne reguły') + '</span>' +
                    '<div class="fw-section-actions">' +
                        '<button class="app-btn app-btn-sm" id="fw-refresh-btn"><i class="fas fa-sync-alt"></i> ' + t('Odśwież') + '</button>' +
                        '<button class="app-btn app-btn-sm app-btn-danger" id="fw-reset-btn"><i class="fas fa-undo"></i> ' + t('Resetuj domyślne') + '</button>' +
                    '</div>' +
                '</div>' +
                '<div class="fw-rules-wrap">' +
                    '<table class="fw-rules-table"><thead><tr>' +
                        '<th>#</th><th>' + t('Port / Usługa') + '</th><th>' + t('Akcja') + '</th><th>' + t('Dostęp') + '</th><th>' + t('Źródło') + '</th><th>' + t('Komentarz') + '</th><th></th>' +
                    '</tr></thead><tbody id="fw-rules-tbody"><tr><td colspan="7" class="fw-empty-msg">' + t('Ładowanie...') + '</td></tr></tbody></table>' +
                '</div>' +
            '</div>' +
        '</div>' +
        '<div class="fw-panel hidden" id="fw-panel-banned">' +
            '<div class="fw-section">' +
                '<div class="fw-section-header">' +
                    '<span class="fw-section-title">' + t('Zablokowane adresy IP') + ' (Fail2Ban)</span>' +
                    '<button class="app-btn app-btn-sm" id="fw-refresh-banned"><i class="fas fa-sync-alt"></i> ' + t('Odśwież') + '</button>' +
                '</div>' +
                '<div id="fw-banned-list" class="fw-banned-list"><div class="fw-empty-msg">' + t('Ładowanie...') + '</div></div>' +
            '</div>' +
        '</div>' +
    '</div>';

    // ─── Presets ───
    var PRESETS = [
        { icon: 'fa-server',        name: 'EthOS',        ports: '9000',    proto: 'tcp', access: 'lan',    color: '#38bdf8' },
        { icon: 'fa-terminal',      name: 'SSH',          ports: '22',      proto: 'tcp', access: 'lan',    color: '#22c55e' },
        { icon: 'fa-folder-open',   name: 'Samba',        ports: '139,445', proto: 'tcp', access: 'lan',    color: '#3b82f6' },
        { icon: 'fa-globe',         name: 'HTTP / HTTPS', ports: '80,443',  proto: 'tcp', access: 'lan',    color: '#f59e0b' },
        { icon: 'fa-play-circle',   name: 'Plex',         ports: '32400',   proto: 'tcp', access: 'lan',    color: '#e5a00d' },
        { icon: 'fa-print',         name: 'CUPS',         ports: '631',     proto: 'tcp', access: 'lan',    color: '#8b5cf6' },
        { icon: 'fa-hdd',           name: 'NFS',          ports: '2049',    proto: 'tcp', access: 'lan',    color: '#06b6d4' },
        { icon: 'fa-download',      name: 'FTP',          ports: '21',      proto: 'tcp', access: 'lan',    color: '#f97316' },
        { icon: 'fa-tv',            name: 'DLNA',         ports: '8200',    proto: 'tcp', access: 'lan',    color: '#ec4899' },
    ];

    var presetGrid = document.getElementById('fw-preset-grid');
    PRESETS.forEach(function(p) {
        var btn = document.createElement('button');
        btn.className = 'fw-preset-card';
        btn.innerHTML =
            '<i class="fas ' + p.icon + '" style="color:' + p.color + '"></i>' +
            '<span class="fw-preset-name">' + p.name + '</span>' +
            '<span class="fw-preset-ports">' + p.ports + '/' + p.proto + '</span>' +
            '<span class="fw-access-badge ' + p.access + '">' + (p.access === 'lan' ? '🏠 LAN' : '🌐 Public') + '</span>';
        btn.onclick = function() { addRule(p.ports, p.proto, 'allow', p.access, '', p.name); };
        presetGrid.appendChild(btn);
    });

    // ─── Tabs ───
    document.querySelectorAll('#fw-tabs .fw-tab').forEach(function(tab) {
        tab.onclick = function() {
            document.querySelectorAll('#fw-tabs .fw-tab').forEach(function(t) { t.classList.remove('active'); });
            tab.classList.add('active');
            var target = tab.dataset.tab;
            document.getElementById('fw-panel-rules').classList.toggle('hidden', target !== 'rules');
            document.getElementById('fw-panel-banned').classList.toggle('hidden', target !== 'banned');
            if (target === 'banned') loadBanned();
        };
    });

    // ─── Access type toggle ───
    var accessSel = document.getElementById('fw-access');
    var customIpGroup = document.getElementById('fw-custom-ip-group');
    accessSel.onchange = function() {
        customIpGroup.classList.toggle('hidden', accessSel.value !== 'custom');
    };

    // ─── Toggle ───
    var toggle = document.getElementById('fw-toggle');
    toggle.onchange = async function () {
        var enable = this.checked;
        this.checked = !enable;
        if (!await confirmDialog(t('Czy na pewno chcesz') + ' ' + (enable ? t('włączyć') : t('wyłączyć')) + ' firewall?')) return;
        try {
            var res = await api('/firewall/toggle', { method: 'POST', body: { enable: enable } });
            if (res.ok) {
                toast(enable ? t('Firewall włączony') : t('Firewall wyłączony'), 'success');
                loadStatus();
            } else {
                toast(res.error || t('Błąd'), 'error');
            }
        } catch (e) { toast(e.message, 'error'); }
    };

    // ─── Add Rule ───
    document.getElementById('fw-add-btn').onclick = function() {
        var port = document.getElementById('fw-port').value.trim();
        if (!port) { toast(t('Podaj numer portu'), 'error'); return; }
        addRule(
            port,
            document.getElementById('fw-proto').value,
            document.getElementById('fw-action').value,
            accessSel.value,
            document.getElementById('fw-custom-ip').value.trim(),
            document.getElementById('fw-comment').value.trim()
        );
    };

    async function addRule(port, proto, action, access, customIp, comment) {
        try {
            var res = await api('/firewall/rules', {
                method: 'POST',
                body: {
                    action: 'add',
                    port: port, proto: proto,
                    ufw_action: action,
                    access: access,
                    from: customIp,
                    comment: comment || ''
                }
            });
            if (res.ok) {
                toast(t('Reguła dodana'), 'success');
                document.getElementById('fw-port').value = '';
                document.getElementById('fw-comment').value = '';
                document.getElementById('fw-custom-ip').value = '';
                loadStatus();
            } else {
                toast(res.error || t('Błąd dodawania reguły'), 'error');
            }
        } catch (e) { toast(e.message, 'error'); }
    }

    // ─── Delete Rule ───
    async function deleteRule(id, v6Id) {
        if (!await confirmDialog(t('Czy na pewno usunąć regułę') + ' #' + id + '?')) return;
        try {
            var res = await api('/firewall/rules', {
                method: 'POST',
                body: { action: 'delete', id: id, v6_id: v6Id || null }
            });
            if (res.ok) {
                toast(t('Reguła usunięta'), 'success');
                loadStatus();
            } else {
                toast(res.error || t('Błąd'), 'error');
            }
        } catch (e) { toast(e.message, 'error'); }
    }

    // ─── Reset Defaults ───
    document.getElementById('fw-reset-btn').onclick = async function() {
        if (!await confirmDialog(t('UWAGA: To usunie wszystkie reguły i przywróci domyślne (SSH, EthOS Web — tylko LAN). Kontynuować?'))) return;
        try {
            var res = await api('/firewall/rules', { method: 'POST', body: { action: 'reset_defaults' } });
            if (res.ok) {
                toast(t('Przywrócono domyślne reguły'), 'success');
                loadStatus();
            } else {
                toast(res.error || t('Błąd'), 'error');
            }
        } catch (e) { toast(e.message, 'error'); }
    };

    document.getElementById('fw-refresh-btn').onclick = function() { loadStatus(); };

    // ─── Load Status & Rules ───
    async function loadStatus() {
        var statusEl = document.getElementById('fw-status');
        var toggleLabel = document.getElementById('fw-toggle-label');
        var tbody = document.getElementById('fw-rules-tbody');

        try {
            var res = await api('/firewall/status');
            if (res.error) {
                statusEl.innerHTML = '<span class="fw-dot red"></span> ' + (res.error.includes('not available') ? t('Firewall nie zainstalowany') : t('Błąd'));
                tbody.innerHTML = '<tr><td colspan="7" class="fw-empty-msg">' + res.error + '</td></tr>';
                return;
            }

            var active = res.status === 'active';
            toggle.checked = active;
            toggleLabel.textContent = active ? t('Aktywny') : t('Wyłączony');

            if (active) {
                var def = res.defaults || {};
                statusEl.innerHTML = '<span class="fw-dot green"></span> ' + t('Aktywny') +
                    (def.incoming ? ' &middot; ' + t('Domyślnie') + ': <strong>' + def.incoming + '</strong> ' + t('przychodzące') : '');
            } else {
                statusEl.innerHTML = '<span class="fw-dot red"></span> ' + t('Nieaktywny');
            }

            _rules = res.rules || [];
            renderRules(tbody);
        } catch (e) {
            statusEl.innerHTML = '<span class="fw-dot red"></span> ' + t('Błąd połączenia');
            tbody.innerHTML = '<tr><td colspan="7" class="fw-empty-msg">' + t('Błąd:') + ' ' + e.message + '</td></tr>';
        }
    }

    function renderRules(tbody) {
        tbody.innerHTML = '';
        if (_rules.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="fw-empty-msg">' + t('Brak reguł lub firewall nieaktywny') + '</td></tr>';
            return;
        }
        _rules.forEach(function(rule) {
            var tr = document.createElement('tr');
            var actionCls = rule.action === 'ALLOW' ? 'allow' : (rule.action === 'LIMIT' ? 'limit' : 'deny');
            var accessCls = rule.access || 'public';
            var accessLabel = accessCls === 'lan' ? '🏠 LAN' : (accessCls === 'public' ? '🌐 ' + t('Publiczny') : '📍 ' + t('Własny'));

            tr.innerHTML =
                '<td>' + rule.id + '</td>' +
                '<td class="fw-mono">' + rule.to + '</td>' +
                '<td><span class="fw-action-badge ' + actionCls + '">' + rule.action + '</span></td>' +
                '<td><span class="fw-access-badge ' + accessCls + '">' + accessLabel + '</span></td>' +
                '<td class="fw-mono">' + rule.from + '</td>' +
                '<td class="fw-comment-cell">' + (rule.comment || '') + '</td>' +
                '<td><button class="app-btn app-btn-xs app-btn-danger fw-del-btn" data-id="' + rule.id + '" data-v6="' + (rule.v6_id || '') + '"><i class="fas fa-trash"></i></button></td>';
            tbody.appendChild(tr);
        });
        tbody.querySelectorAll('.fw-del-btn').forEach(function(btn) {
            btn.onclick = function() { deleteRule(parseInt(btn.dataset.id), btn.dataset.v6 ? parseInt(btn.dataset.v6) : null); };
        });
    }

    // ─── Load Banned IPs ───
    async function loadBanned() {
        var list = document.getElementById('fw-banned-list');
        list.innerHTML = '<div class="fw-empty-msg"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div>';

        try {
            var res = await api('/firewall/banned');
            list.innerHTML = '';

            if (res.error) {
                list.innerHTML = '<div class="fw-empty-msg" style="color:#ef4444">' + t('Błąd:') + ' ' + res.error + '</div>';
                return;
            }

            var jails = res.jails || [];
            if (jails.length === 0) {
                list.innerHTML = '<div class="fw-empty-msg"><i class="fas fa-check-circle" style="color:#22c55e"></i> ' + t('Brak aktywnych banów') + '</div>';
                return;
            }

            jails.forEach(function(jail) {
                var jailEl = document.createElement('div');
                jailEl.className = 'fw-jail';

                var stats =
                    '<span class="fw-jail-stat">' + t('Zbanowanych') + ': <strong>' + jail.banned_ips.length + '</strong></span>' +
                    '<span class="fw-jail-stat">' + t('Łącznie') + ': <strong>' + jail.total_banned + '</strong></span>' +
                    '<span class="fw-jail-stat">' + t('Nieudanych') + ': <strong>' + jail.failed + '</strong></span>';

                var ipsHtml = '';
                if (jail.banned_ips.length === 0) {
                    ipsHtml = '<div class="fw-empty-msg" style="padding:8px">' + t('Brak zablokowanych IP') + '</div>';
                } else {
                    ipsHtml = jail.banned_ips.map(function(ip) {
                        return '<div class="fw-ban-item">' +
                            '<span class="fw-mono">' + ip + '</span>' +
                            '<button class="app-btn app-btn-xs app-btn-danger fw-unban-btn" data-jail="' + jail.name + '" data-ip="' + ip + '">Unban</button>' +
                        '</div>';
                    }).join('');
                }

                jailEl.innerHTML =
                    '<div class="fw-jail-header">' +
                        '<span class="fw-jail-name"><i class="fas fa-lock"></i> ' + jail.name + '</span>' +
                        '<div class="fw-jail-stats">' + stats + '</div>' +
                    '</div>' +
                    '<div class="fw-ban-items">' + ipsHtml + '</div>';
                list.appendChild(jailEl);
            });

            list.querySelectorAll('.fw-unban-btn').forEach(function(btn) {
                btn.onclick = async function() {
                    var jail = btn.dataset.jail, ip = btn.dataset.ip;
                    if (!await confirmDialog(t('Odblokować') + ' ' + ip + '?')) return;
                    try {
                        var r = await api('/firewall/unban', { method: 'POST', body: { jail: jail, ip: ip } });
                        if (r.ok) { toast(t('Odblokowano') + ' ' + ip, 'success'); loadBanned(); }
                        else toast(r.error || t('Błąd'), 'error');
                    } catch (e) { toast(e.message, 'error'); }
                };
            });
        } catch (e) {
            list.innerHTML = '<div class="fw-empty-msg" style="color:#ef4444">' + t('Błąd:') + ' ' + e.message + '</div>';
        }
    }

    document.getElementById('fw-refresh-banned').onclick = loadBanned;

    // ─── Load subnet hint ───
    async function loadSubnet() {
        try {
            var res = await api('/firewall/subnet');
            if (res.ok && res.subnet) {
                _lanSubnet = res.subnet;
                document.getElementById('fw-subnet-hint').textContent =
                    t('Twoja sieć LAN') + ': ' + res.subnet;
            }
        } catch (e) { /* ignore */ }
    }

    // ─── Init ───
    loadStatus();
    loadSubnet();
};
