/* ═══════════════════════════════════════════════════════════
   EthOS — Firewall Manager (UFW)
   Manage firewall rules, quick presets, and banned IPs
   ═══════════════════════════════════════════════════════════ */

AppRegistry['firewall'] = function (appDef) {
    const win = createWindow('firewall', {
        title: 'Firewall',
        icon: 'fa-fire',
        iconColor: '#e05d44',
        width: 900,
        height: 700,
        resizable: true,
        maximizable: true
    });

    const body = win.body;
    body.style.display = 'flex';
    body.style.flexDirection = 'column';
    body.style.background = 'var(--bg-default)';
    body.style.color = 'var(--text-default)';
    body.style.padding = '0'; // No padding on body, padding in content
    body.style.overflow = 'hidden';

    // ─── Header ───
    const header = document.createElement('div');
    header.style.padding = '20px';
    header.style.borderBottom = '1px solid var(--border)';
    header.style.display = 'flex';
    header.style.justifyContent = 'space-between';
    header.style.alignItems = 'center';
    header.style.background = 'var(--bg-surface)';

    const titleArea = document.createElement('div');
    titleArea.innerHTML = `
        <h2 style="margin:0;display:flex;align-items:center;gap:10px">
            <i class="fas fa-shield-alt" style="color:#e05d44"></i>
            Firewall (UFW)
        </h2>
        <div id="fw-status-text" style="margin-top:5px;font-size:0.9em;opacity:0.8">
            ${t('Ładowanie statusu...')}
        </div>
    `;

    const toggleArea = document.createElement('div');
    toggleArea.innerHTML = `
        <label class="switch">
            <input type="checkbox" id="fw-toggle">
            <span class="slider round"></span>
        </label>
    `;
    header.appendChild(titleArea);
    header.appendChild(toggleArea);
    body.appendChild(header);

    // ─── Tabs ───
    const tabsContainer = document.createElement('div');
    tabsContainer.style.display = 'flex';
    tabsContainer.style.borderBottom = '1px solid var(--border)';
    tabsContainer.style.background = 'var(--bg-surface)';

    const tabRules = document.createElement('button');
    tabRules.className = 'tab-btn active';
    tabRules.textContent = t('Reguły');
    tabRules.onclick = () => switchTab('rules');

    const tabBanned = document.createElement('button');
    tabBanned.className = 'tab-btn';
    tabBanned.textContent = 'Zablokowane IP (Fail2Ban)';
    tabBanned.onclick = () => switchTab('banned');

    tabsContainer.appendChild(tabRules);
    tabsContainer.appendChild(tabBanned);
    body.appendChild(tabsContainer);

    // ─── Content Areas ───
    const contentArea = document.createElement('div');
    contentArea.style.flex = '1';
    contentArea.style.overflowY = 'auto';
    contentArea.style.padding = '20px';
    contentArea.style.position = 'relative';
    body.appendChild(contentArea);

    // ─── Tab: Rules ───
    const rulesView = document.createElement('div');
    rulesView.style.display = 'flex';
    rulesView.style.flexDirection = 'column';
    rulesView.style.gap = '20px';

    // Quick Actions
    const quickActions = document.createElement('div');
    quickActions.className = 'card'; // Assuming card class exists or standard styling
    quickActions.style.padding = '15px';
    quickActions.style.background = 'var(--bg-surface)';
    quickActions.style.border = '1px solid var(--border)';
    quickActions.style.borderRadius = '8px';
    quickActions.innerHTML = `
        <h3 style="margin-top:0;font-size:1rem;margin-bottom:10px">Szybkie akcje</h3>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
            <button class="app-btn app-btn-sm" onclick="app_firewall_addRule('22', 'tcp', 'SSH')"><i class="fas fa-terminal"></i> Allow SSH</button>
            <button class="app-btn app-btn-sm" onclick="app_firewall_addRule('139,445', 'tcp', 'Samba')"><i class="fas fa-network-wired"></i> Allow Samba</button>
            <button class="app-btn app-btn-sm" onclick="app_firewall_addRule('32400', 'tcp', 'Plex')"><i class="fas fa-play-circle"></i> Allow Plex</button>
            <button class="app-btn app-btn-sm" onclick="app_firewall_addRule('80,443', 'tcp', 'Web')"><i class="fas fa-globe"></i> Allow HTTP/HTTPS</button>
            <button class="app-btn app-btn-sm app-btn-secondary" onclick="app_firewall_resetDefaults()"><i class="fas fa-undo"></i> ${t('Resetuj do domyślnych')}</button>
        </div>
    `;
    rulesView.appendChild(quickActions);

    // Custom Rule
    const customRule = document.createElement('div');
    customRule.className = 'card';
    customRule.style.padding = '15px';
    customRule.style.background = 'var(--bg-surface)';
    customRule.style.border = '1px solid var(--border)';
    customRule.style.borderRadius = '8px';
    customRule.innerHTML = `
        <h3 style="margin-top:0;font-size:1rem;margin-bottom:10px">${t('Dodaj własną regułę')}</h3>
        <div style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
            <div style="flex:1;min-width:100px">
                <label style="display:block;font-size:0.8rem;opacity:0.7">Port(y)</label>
                <input type="text" id="fw-custom-port" class="app-input" placeholder="np. 8080 lub 8000:8100">
            </div>
            <div style="width:100px">
                <label style="display:block;font-size:0.8rem;opacity:0.7">${t('Protokół')}</label>
                <select id="fw-custom-proto" class="app-select">
                    <option value="tcp">TCP</option>
                    <option value="udp">UDP</option>
                    <option value="">Both</option>
                </select>
            </div>
            <div style="flex:1;min-width:120px">
                <label style="display:block;font-size:0.8rem;opacity:0.7">${t('Źródło IP (opcjonalne)')}</label>
                <input type="text" id="fw-custom-ip" class="app-input" placeholder="np. 192.168.1.100 lub 'any'">
            </div>
            <button class="app-btn app-btn-primary" id="fw-add-custom-btn"><i class="fas fa-plus"></i> Dodaj</button>
        </div>
    `;
    rulesView.appendChild(customRule);

    // Rules List
    const rulesList = document.createElement('div');
    rulesList.innerHTML = `
        <h3 style="margin-top:0;font-size:1rem;margin-bottom:10px">${t('Aktywne reguły')}</h3>
        <div id="fw-rules-table-container" style="background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;overflow:hidden">
            <table style="width:100%;border-collapse:collapse">
                <thead style="background:var(--bg-base);border-bottom:1px solid var(--border)">
                    <tr>
                        <th style="padding:10px;text-align:left">ID</th>
                        <th style="padding:10px;text-align:left">${t('Port / Usługa')}</th>
                        <th style="padding:10px;text-align:left">Akcja</th>
                        <th style="padding:10px;text-align:left">Kierunek</th>
                        <th style="padding:10px;text-align:left">${t('Źródło')}</th>
                        <th style="padding:10px;text-align:right">Opcje</th>
                    </tr>
                </thead>
                <tbody id="fw-rules-tbody">
                    <tr><td colspan="6" style="padding:20px;text-align:center">${t('Ładowanie...')}</td></tr>
                </tbody>
            </table>
        </div>
    `;
    rulesView.appendChild(rulesList);

    // ─── Tab: Banned ───
    const bannedView = document.createElement('div');
    bannedView.style.display = 'none'; // Hidden by default
    bannedView.style.flexDirection = 'column';
    bannedView.style.gap = '20px';
    bannedView.innerHTML = `
        <div class="card" style="padding:15px;background:var(--bg-surface);border:1px solid var(--border);border-radius:8px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                <h3 style="margin:0;font-size:1rem">Zablokowane adresy IP (Fail2Ban)</h3>
                <button class="app-btn app-btn-sm" id="fw-refresh-banned"><i class="fas fa-sync-alt"></i> ${t('Odśwież')}</button>
            </div>
            <div id="fw-banned-list">
                ${t('Ładowanie...')}
            </div>
        </div>
    `;

    contentArea.appendChild(rulesView);
    contentArea.appendChild(bannedView);

    // ─── Helper CSS for Tabs ───
    const style = document.createElement('style');
    style.textContent = `
        .tab-btn {
            padding: 10px 20px;
            background: transparent;
            border: none;
            border-bottom: 2px solid transparent;
            color: var(--text-default);
            cursor: pointer;
            font-weight: 500;
            opacity: 0.7;
            transition: all 0.2s;
        }
        .tab-btn:hover { opacity: 1; background: rgba(255,255,255,0.05); }
        .tab-btn.active {
            border-bottom-color: #e05d44;
            opacity: 1;
            color: #e05d44;
        }
        .switch {
            position: relative;
            display: inline-block;
            width: 40px;
            height: 20px;
        }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider {
            position: absolute;
            cursor: pointer;
            top: 0; left: 0; right: 0; bottom: 0;
            background-color: #ccc;
            transition: .4s;
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 16px;
            width: 16px;
            left: 2px;
            bottom: 2px;
            background-color: white;
            transition: .4s;
        }
        input:checked + .slider { background-color: #e05d44; }
        input:focus + .slider { box-shadow: 0 0 1px #e05d44; }
        input:checked + .slider:before { transform: translateX(20px); }
        .slider.round { border-radius: 34px; }
        .slider.round:before { border-radius: 50%; }
    `;
    body.appendChild(style);

    // ─── Logic ───

    // Tab Switching
    function switchTab(tab) {
        if (tab === 'rules') {
            rulesView.style.display = 'flex';
            bannedView.style.display = 'none';
            tabRules.classList.add('active');
            tabBanned.classList.remove('active');
            loadRules();
        } else {
            rulesView.style.display = 'none';
            bannedView.style.display = 'flex';
            tabRules.classList.remove('active');
            tabBanned.classList.add('active');
            loadBanned();
        }
    }

    // Load Rules & Status
    async function loadRules() {
        const statusText = document.getElementById('fw-status-text');
        const toggle = document.getElementById('fw-toggle');
        const tbody = document.getElementById('fw-rules-tbody');

        // Prevent triggering toggle event
        toggle.onclick = null;

        try {
            const res = await api('/firewall/status');

            // Update Header Status
            if (res.status === 'active') {
                statusText.innerHTML = '<span style="color:#2ecc71">● Aktywny</span>';
                toggle.checked = true;
            } else {
                statusText.innerHTML = '<span style="color:#ef4444">● Nieaktywny</span>';
                toggle.checked = false;
            }

            // Bind Toggle
            toggle.onclick = async (e) => {
                e.preventDefault(); // Don't switch yet
                const newState = !toggle.checked; // Current state is technically already switched in UI? No, e.preventDefault stops it?
                // Wait, onclick happens after change? Usually input type checkbox uses onchange.
                // Let's use logic: if checked, we want to uncheck (disable).

                // Better approach:
                // Checkbox state is *before* the click unless we prevent default?
                // Actually, let's just use the current visual state relative to data.

                const enable = !toggle.checked; // If it was checked, we clicked to uncheck

                // Confirm action
                if (!confirm(`${t('Czy na pewno chcesz')} ${enable ? t('włączyć') : t('wyłączyć')} firewall?`)) {
                    toggle.checked = !enable; // Revert visual
                    return;
                }

                // Optimistic UI? No, wait for result.
                try {
                    const tRes = await api('/firewall/toggle', {
                        method: 'POST',
                        body: { enable: enable }
                    });
                    if (tRes.success) {
                        toast(tRes.message, 'success');
                        loadRules();
                    } else {
                        toast(tRes.error || t('Błąd zmiany statusu'), 'error');
                        toggle.checked = !enable; // Revert
                    }
                } catch (err) {
                    toast(err.message, 'error');
                    toggle.checked = !enable;
                }
            };
            // Need to re-bind onchange actually, creating onclick logic on checkbox is tricky.
            // Let's just reset onclick to handle it properly.
            toggle.onclick = null;
            toggle.onchange = async function() {
                const enable = this.checked;
                // Revert immediately to wait for confirmation/api
                this.checked = !enable;

                if (!confirm(`${t('Czy na pewno chcesz')} ${enable ? t('włączyć') : t('wyłączyć')} firewall?`)) return;

                try {
                    const tRes = await api('/firewall/toggle', {
                        method: 'POST',
                        body: { enable: enable }
                    });
                    if (tRes.success) {
                        toast(tRes.message, 'success');
                        this.checked = enable;
                        loadRules(); // Refresh status text
                    } else {
                        toast(tRes.error || t('Błąd'), 'error');
                    }
                } catch (err) {
                    toast(err.message, 'error');
                }
            };


            // Render Rules
            tbody.innerHTML = '';
            if (!res.rules || res.rules.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="padding:20px;text-align:center;opacity:0.6">' + t('Brak reguł lub firewall nieaktywny') + '</td></tr>';
            } else {
                res.rules.forEach(rule => {
                    const tr = document.createElement('tr');
                    tr.style.borderBottom = '1px solid var(--border-subtle)';

                    const actionColor = rule.action.includes('ALLOW') ? '#2ecc71' : '#ef4444';

                    tr.innerHTML = `
                        <td style="padding:10px">${rule.id}</td>
                        <td style="padding:10px;font-family:monospace">${rule.to}</td>
                        <td style="padding:10px;color:${actionColor};font-weight:bold">${rule.action}</td>
                        <td style="padding:10px">${rule.direction}</td>
                        <td style="padding:10px;font-family:monospace">${rule.from}</td>
                        <td style="padding:10px;text-align:right">
                            <button class="app-btn app-btn-xs app-btn-danger delete-rule-btn" data-id="${rule.id}">
                                <i class="fas fa-trash"></i>
                            </button>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });

                // Bind delete buttons
                tbody.querySelectorAll('.delete-rule-btn').forEach(btn => {
                    btn.onclick = () => deleteRule(btn.dataset.id);
                });
            }

        } catch (e) {
            console.error(e);
            tbody.innerHTML = `<tr><td colspan="6" style="padding:20px;text-align:center;color:var(--text-error)">${t('Błąd:')} ${e.message}</td></tr>`;
        }
    }

    // Add Rule Helper
    window.app_firewall_addRule = async (port, proto, name) => {
        if (!confirm(`${t('Dodać regułę dla')} ${name || port}?`)) return;
        try {
            const res = await api('/firewall/rules', {
                method: 'POST',
                body: { action: 'add', port, proto }
            });
            if (res.success) {
                toast('Reguła dodana', 'success');
                loadRules();
            } else {
                toast(res.error, 'error');
            }
        } catch (e) {
            toast(e.message, 'error');
        }
    };

    // Reset Defaults
    window.app_firewall_resetDefaults = async () => {
        if (!confirm(t('UWAGA: To usunie wszystkie obecne reguły i przywróci domyślne (SSH, Web, Samba, Plex). Kontynuować?'))) return;
        try {
            const res = await api('/firewall/rules', {
                method: 'POST',
                body: { action: 'reset_defaults' }
            });
            if (res.success) {
                toast('Przywrócono domyślne reguły', 'success');
                loadRules();
            } else {
                toast(res.error, 'error');
            }
        } catch (e) {
            toast(e.message, 'error');
        }
    };

    // Delete Rule
    async function deleteRule(id) {
        if (!confirm(`${t('Czy na pewno usunąć regułę #')}${id}?`)) return;
        try {
            const res = await api('/firewall/rules', {
                method: 'POST',
                body: { action: 'delete', id: id }
            });
            if (res.success) {
                toast('Reguła usunięta', 'success');
                loadRules();
            } else {
                toast(res.error, 'error');
            }
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    // Custom Add Logic
    document.getElementById('fw-add-custom-btn').onclick = async () => {
        const port = document.getElementById('fw-custom-port').value.trim();
        const proto = document.getElementById('fw-custom-proto').value;
        const ip = document.getElementById('fw-custom-ip').value.trim();

        if (!port) {
            toast('Podaj port', 'error');
            return;
        }

        try {
            const res = await api('/firewall/rules', {
                method: 'POST',
                body: {
                    action: 'add',
                    port: port,
                    proto: proto || null,
                    from: ip || 'any'
                }
            });
            if (res.success) {
                toast('Reguła dodana', 'success');
                // Clear inputs
                document.getElementById('fw-custom-port').value = '';
                document.getElementById('fw-custom-ip').value = '';
                loadRules();
            } else {
                toast(res.error, 'error');
            }
        } catch (e) {
            toast(e.message, 'error');
        }
    };

    // Load Banned IPs
    async function loadBanned() {
        const list = document.getElementById('fw-banned-list');
        list.innerHTML = t('Ładowanie...');

        try {
            const res = await api('/firewall/banned');
            list.innerHTML = '';

            if (res.error) {
                list.innerHTML = `<div style="color:var(--text-error)">${t('Błąd:')} ${res.error}</div>`;
                return;
            }

            if (!res.jails || res.jails.length === 0) {
                list.innerHTML = '<div style="opacity:0.6;padding:10px">' + t('Brak aktywnych więzień Fail2Ban.') + '</div>';
                return;
            }

            res.jails.forEach(jail => {
                const jailDiv = document.createElement('div');
                jailDiv.style.marginBottom = '15px';
                jailDiv.innerHTML = `
                    <h4 style="margin:0 0 5px 0;font-size:0.95rem;color:var(--text-accent)">
                        <i class="fas fa-lock"></i> ${jail.name}
                    </h4>
                `;

                if (jail.banned_ips && jail.banned_ips.length > 0) {
                    const ul = document.createElement('ul');
                    ul.style.listStyle = 'none';
                    ul.style.padding = '0';
                    ul.style.margin = '0';
                    ul.style.background = 'var(--bg-base)';
                    ul.style.borderRadius = '6px';
                    ul.style.border = '1px solid var(--border-subtle)';

                    jail.banned_ips.forEach(ip => {
                        const li = document.createElement('li');
                        li.style.padding = '8px 10px';
                        li.style.borderBottom = '1px solid var(--border-subtle)';
                        li.style.display = 'flex';
                        li.style.justifyContent = 'space-between';
                        li.style.alignItems = 'center';
                        li.innerHTML = `
                            <span style="font-family:monospace">${ip}</span>
                            <button class="app-btn app-btn-xs app-btn-danger unban-btn" data-jail="${jail.name}" data-ip="${ip}">
                                Unban
                            </button>
                        `;
                        ul.appendChild(li);
                    });
                    // remove last border
                    if (ul.lastChild) ul.lastChild.style.borderBottom = 'none';

                    jailDiv.appendChild(ul);
                } else {
                    const empty = document.createElement('div');
                    empty.textContent = 'Brak zablokowanych IP.';
                    empty.style.opacity = '0.6';
                    empty.style.fontSize = '0.85rem';
                    empty.style.paddingLeft = '10px';
                    jailDiv.appendChild(empty);
                }

                list.appendChild(jailDiv);
            });

            // Bind unban
            list.querySelectorAll('.unban-btn').forEach(btn => {
                btn.onclick = async () => {
                    const jail = btn.dataset.jail;
                    const ip = btn.dataset.ip;
                    if (!confirm(`${t('Odblokować IP')} ${ip}?`)) return;

                    try {
                        const uRes = await api('/firewall/unban', {
                            method: 'POST',
                            body: { jail, ip }
                        });
                        if (uRes.success) {
                            toast(`Odblokowano ${ip}`, 'success');
                            loadBanned();
                        } else {
                            toast(uRes.error, 'error');
                        }
                    } catch (e) {
                        toast(e.message, 'error');
                    }
                };
            });

        } catch (e) {
            console.error(e);
            list.innerHTML = `<div style="color:var(--text-error)">${t('Błąd:')} ${e.message}</div>`;
        }
    }

    document.getElementById('fw-refresh-banned').onclick = loadBanned;

    // Initial load
    loadRules();
};
