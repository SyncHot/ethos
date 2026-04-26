/* ═══════════════════════════════════════════════════════════
   EthOS — Users & Groups Management
   User/group CRUD + app privilege management
   ═══════════════════════════════════════════════════════════ */

AppRegistry['users'] = function (appDef) {

    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('users', level, msg, details) : console.log('[users]', msg, details || '');

    const body = document.createElement('div');
    body.className = 'usr-app';

    body.innerHTML = `
        <div class="usr-sidebar">
            <div class="usr-sidebar-logo">
                <i class="fas fa-users-cog"></i>
                <span>${t('Użytkownicy')}</span>
            </div>
            <nav class="usr-nav">
                <button class="usr-nav-btn active" data-tab="users">
                    <i class="fas fa-user"></i><span>${t('Użytkownicy')}</span>
                </button>
                <button class="usr-nav-btn" data-tab="groups">
                    <i class="fas fa-users"></i><span>${t('Grupy')}</span>
                </button>
                <button class="usr-nav-btn" data-tab="privileges">
                    <i class="fas fa-key"></i><span>${t('Uprawnienia')}</span>
                </button>
                <button class="usr-nav-btn" data-tab="ldap">
                    <i class="fas fa-sitemap"></i><span>LDAP / AD</span>
                </button>
            </nav>
        </div>
        <div class="usr-main">
            <!-- Users Tab -->
            <div class="usr-tab active" id="usr-tab-users">
                <div class="usr-header">
                    <h2>${t('Użytkownicy systemowi')}</h2>
                    <div class="usr-header-actions">
                        <button class="btn btn-primary" id="usr-btn-add-user">
                            <i class="fas fa-user-plus"></i> ${t('Dodaj użytkownika')}
                        </button>
                    </div>
                </div>
                <div class="usr-list" id="usr-user-list"></div>
            </div>

            <!-- Groups Tab -->
            <div class="usr-tab" id="usr-tab-groups">
                <div class="usr-header">
                    <h2>${t('Grupy')}</h2>
                    <div class="usr-header-actions">
                        <button class="btn btn-primary" id="usr-btn-add-group">
                            <i class="fas fa-plus"></i> ${t('Utwórz grupę')}
                        </button>
                    </div>
                </div>
                <div class="usr-list" id="usr-group-list"></div>
            </div>

            <!-- Privileges Tab -->
            <div class="usr-tab" id="usr-tab-privileges">
                <div class="usr-header">
                    <h2>${t('Uprawnienia — Aplikacje dla grup')}</h2>
                    <p class="usr-header-sub">${t('Wybierz, które aplikacje są dostępne członkom danej grupy.')}</p>
                </div>
                <div class="usr-priv-grid" id="usr-priv-grid"></div>
                <div class="usr-priv-actions" id="usr-priv-actions" style="display:none">
                    <button class="btn btn-primary" id="usr-priv-save">
                        <i class="fas fa-save"></i> ${t('Zapisz uprawnienia')}
                    </button>
                </div>
            </div>

            <!-- LDAP / Active Directory Tab -->
            <div class="usr-tab" id="usr-tab-ldap">
                <div class="usr-header">
                    <h2>LDAP / Active Directory</h2>
                    <p class="usr-header-sub">${t('Centralne zarządzanie użytkownikami przez LDAP lub Active Directory')}</p>
                </div>
                <div id="usr-ldap-content"></div>
            </div>
        </div>
    `;

    createWindow('users', {
        title: appDef.name || t('Użytkownicy'),
        icon: appDef.icon || 'fa-users-cog',
        iconColor: appDef.color || '#ec4899',
        width: 1000,
        height: 650,
        content: body.outerHTML,
        onRender: init,
    });

    function init(container) {
    const root = container;

    /* ─── Tab switching ─── */
    const tabs = root.querySelectorAll('.usr-nav-btn');
    tabs.forEach(btn => {
        btn.addEventListener('click', () => {
            tabs.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            root.querySelectorAll('.usr-tab').forEach(t => t.classList.remove('active'));
            root.querySelector(`#usr-tab-${btn.dataset.tab}`).classList.add('active');
            if (btn.dataset.tab === 'users') loadUsers();
            else if (btn.dataset.tab === 'groups') loadGroups();
            else if (btn.dataset.tab === 'privileges') loadPrivileges();
            else if (btn.dataset.tab === 'ldap') loadLdap();
        });
    });

    /* ─── Password strength meter ─── */
    function setupPasswordStrength(inputId, containerId, policy) {
        // Defer to next tick so modal DOM is ready
        setTimeout(() => {
            const inp = document.getElementById(inputId);
            const container = document.getElementById(containerId);
            if (!inp || !container) return;
            inp.addEventListener('input', () => {
                const pw = inp.value;
                if (!pw) { container.innerHTML = ''; return; }
                let score = 0, msgs = [];
                const minLen = policy.min_length || 8;
                if (pw.length >= minLen) score++; else msgs.push(t('Min.') + ` ${minLen} ` + t('znaków'));
                if (/[A-Z]/.test(pw)) score++; else if (policy.require_uppercase) msgs.push(t('Wielka litera'));
                if (/[a-z]/.test(pw)) score++; else if (policy.require_lowercase) msgs.push(t('Mała litera'));
                if (/[0-9]/.test(pw)) score++; else if (policy.require_digit) msgs.push(t('Cyfra'));
                if (/[^A-Za-z0-9]/.test(pw)) score++; else if (policy.require_special) msgs.push(t('Znak specjalny'));
                if (pw.length >= minLen + 4) score++;
                const levels = ['#ef4444', '#f59e0b', '#f59e0b', '#10b981', '#10b981', '#10b981'];
                const labels = [t('Bardzo słabe'), t('Słabe'), t('Średnie'), t('Dobre'), t('Silne'), t('Bardzo silne')];
                const idx = Math.min(score, levels.length - 1);
                const pct = Math.min(100, (score / 5) * 100);
                container.innerHTML = `
                    <div style="height:4px;background:var(--bg-hover);border-radius:2px;margin:4px 0">
                        <div style="height:100%;width:${pct}%;background:${levels[idx]};border-radius:2px;transition:.3s"></div>
                    </div>
                    <div style="font-size:11px;color:${levels[idx]}">${labels[idx]}${msgs.length ? ' — ' + msgs.join(', ') : ''}</div>`;
            });
        }, 50);
    }

    /* ═══════ USERS ═══════ */
    const userList = root.querySelector('#usr-user-list');

    async function loadUsers() {
        userList.innerHTML = `<div class="usr-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        try {
            const [users, quotaRes] = await Promise.all([api('/users/list'), api('/users/quotas').catch(() => ({ available: false }))]);
            const quotas = quotaRes.quotas || {};
            if (!users.length) { userList.innerHTML = `<div class="usr-empty">${t('Brak użytkowników')}</div>`; return; }
            userList.innerHTML = '';
            users.forEach(u => {
                const card = document.createElement('div');
                card.className = 'usr-card';
                const isAdmin = u.role === 'admin';
                const isFamily = u.role === 'family';
                const isNasos = u.nasos_user;
                const q = quotas[u.username];
                const quotaHtml = q && q.limit_gb > 0 ? `<div class="usr-quota"><div class="usr-quota-bar"><div class="usr-quota-fill" style="width:${Math.min(q.percent,100)}%;background:${q.percent > 90 ? '#ef4444' : q.percent > 70 ? '#f59e0b' : '#10b981'}"></div></div><span class="usr-quota-text">${q.used_gb} / ${q.limit_gb} GB (${q.percent}%)</span></div>` : quotaRes.available ? `<span class="usr-quota-text" style="color:var(--text-muted);font-size:11px">${t('Bez limitu')}</span>` : '';
                card.innerHTML = `
                    <div class="usr-card-avatar ${isAdmin ? 'admin' : ''}">
                        <i class="fas ${u.username === 'root' ? 'fa-crown' : isAdmin ? 'fa-user-shield' : isFamily ? 'fa-house-user' : 'fa-user'}"></i>
                    </div>
                    <div class="usr-card-info">
                        <div class="usr-card-name">
                            ${u.username}
                            ${isAdmin ? '<span class="usr-badge admin">Admin</span>' : isFamily ? `<span class="usr-badge family">${t('Rodzina')}</span>` : `<span class="usr-badge user">${t('Użytkownik')}</span>`}
                            ${isNasos ? '<span class="usr-badge nasos"><i class="fas fa-server"></i> EthOS</span>' : ''}
                        </div>
                        <div class="usr-card-detail">
                            <span>UID: ${u.uid}</span>
                            <span>Shell: ${u.shell || '/bin/bash'}</span>
                            <span>Home: ${u.home || '—'}</span>
                        </div>
                        ${quotaHtml}
                        <div class="usr-card-groups">
                            ${(u.groups || []).map(g => `<span class="usr-group-tag">${g}</span>`).join('')}
                        </div>
                    </div>
                    <div class="usr-card-actions">
                        <button class="usr-icon-btn" title="${t('Zmień hasło')}" data-action="password"><i class="fas fa-key"></i></button>
                        ${quotaRes.available ? `<button class="usr-icon-btn" title="${t('Quota')}" data-action="quota"><i class="fas fa-chart-pie"></i></button>` : ''}
                        <button class="usr-icon-btn" title="${t('Edytuj grupy')}" data-action="edit-groups"><i class="fas fa-users"></i></button>
                        ${u.username !== 'root' ? `<button class="usr-icon-btn danger" title="${t('Usuń')}" data-action="delete"><i class="fas fa-trash"></i></button>` : ''}
                    </div>
                `;
                card.querySelector('[data-action="password"]')?.addEventListener('click', () => changePassword(u));
                card.querySelector('[data-action="quota"]')?.addEventListener('click', () => setQuota(u, q));
                card.querySelector('[data-action="edit-groups"]')?.addEventListener('click', () => editUserGroups(u));
                card.querySelector('[data-action="delete"]')?.addEventListener('click', () => deleteUser(u));
                userList.appendChild(card);
            });
        } catch (err) {
            userList.innerHTML = `<div class="usr-error"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd')}: ${err.message}</div>`;
        }
    }

    root.querySelector('#usr-btn-add-user').addEventListener('click', async () => {
        let policy = {};
        try { const r = await api('/users/password-policy'); policy = r.policy || {}; } catch {}
        showModal(t('Nowy użytkownik'), `
            <div class="usr-form">
                <label>${t('Nazwa użytkownika')}</label>
                <input type="text" id="usr-new-username" placeholder="${t('np. jan')}" class="usr-input">
                <label>${t('Hasło')}</label>
                <input type="password" id="usr-new-password" placeholder="${t('Hasło')}" class="usr-input">
                <div id="usr-pw-strength" class="usr-pw-strength"></div>
                <label>${t('Powłoka')}</label>
                <input type="text" id="usr-new-shell" value="/bin/bash" class="usr-input">
                <label class="usr-checkbox-label">
                    <input type="checkbox" id="usr-new-create-home" checked>
                    ${t('Utwórz katalog domowy')}
                </label>
            </div>
        `, [
            { label: t('Anuluj'), class: 'secondary' },
            {
                label: t('Utwórz'), class: 'primary', action: async (modal) => {
                    const username = modal.querySelector('#usr-new-username').value.trim();
                    const password = modal.querySelector('#usr-new-password').value;
                    const shell = modal.querySelector('#usr-new-shell').value.trim();
                    const createHome = modal.querySelector('#usr-new-create-home').checked;
                    if (!username) { toast(t('Podaj nazwę użytkownika'), 'error'); return; }
                    if (!password) { toast(t('Podaj hasło'), 'error'); return; }
                    try {
                        const res = await api('/users/create', {
                            method: 'POST',
                            body: { username, password, shell, create_home: createHome }
                        });
                        if (res.error) { toast(res.error, 'error'); return; }
                        toast(t('Utworzono użytkownika') + ` ${username}`, 'success');
                        loadUsers();
                    } catch (e) { toast(e.message, 'error'); }
                }
            }
        ]);
        setupPasswordStrength('usr-new-password', 'usr-pw-strength', policy);
    });

    async function changePassword(user) {
        let policy = {};
        try { const r = await api('/users/password-policy'); policy = r.policy || {}; } catch {}
        showModal(t('Zmień hasło — ') + user.username, `
            <div class="usr-form">
                <label>${t('Nowe hasło')}</label>
                <input type="password" id="usr-chpw" placeholder="${t('Nowe hasło')}" class="usr-input">
                <div id="usr-chpw-strength" class="usr-pw-strength"></div>
                <label>${t('Powtórz hasło')}</label>
                <input type="password" id="usr-chpw2" placeholder="${t('Powtórz hasło')}" class="usr-input">
            </div>
        `, [
            { label: t('Anuluj'), class: 'secondary' },
            {
                label: t('Zmień'), class: 'primary', action: async (modal) => {
                    const pw = modal.querySelector('#usr-chpw').value;
                    const pw2 = modal.querySelector('#usr-chpw2').value;
                    if (!pw) { toast(t('Podaj hasło'), 'error'); return; }
                    if (pw !== pw2) { toast(t('Hasła nie są identyczne'), 'error'); return; }
                    try {
                        const res = await api('/users/update', {
                            method: 'POST',
                            body: { username: user.username, password: pw }
                        });
                        if (res.error) { toast(res.error, 'error'); return; }
                        toast(t('Hasło zmienione'), 'success');
                    } catch (e) { toast(e.message, 'error'); }
                }
            }
        ]);
        setupPasswordStrength('usr-chpw', 'usr-chpw-strength', policy);
    }

    async function setQuota(user, currentQuota) {
        const currentLimit = currentQuota?.limit_gb || 0;
        showModal(t('Quota — ') + user.username, `
            <div class="usr-form">
                <label>${t('Limit miejsca (GB)')}</label>
                <input type="number" id="usr-quota-val" class="usr-input" min="0" max="100000" step="1" value="${currentLimit}" placeholder="${t('0 = bez limitu')}">
                <p style="font-size:11px;color:var(--text-muted);margin-top:4px">${t('Ustaw 0 aby usunąć limit. Wymaga btrfs na partycji danych.')}</p>
                ${currentQuota && currentQuota.used_gb ? `<p style="font-size:12px;margin-top:8px">${t('Aktualnie zajęte')}: <strong>${currentQuota.used_gb} GB</strong></p>` : ''}
            </div>
        `, [
            { label: t('Anuluj'), class: 'secondary' },
            {
                label: t('Zapisz'), class: 'primary', action: async (modal) => {
                    const val = parseFloat(modal.querySelector('#usr-quota-val').value) || 0;
                    try {
                        const res = await api('/users/quotas', { method: 'PUT', body: { username: user.username, limit_gb: val } });
                        if (res.error) { toast(res.error, 'error'); return; }
                        toast(t('Quota zapisana'), 'success');
                        loadUsers();
                    } catch (e) { toast(e.message, 'error'); }
                }
            }
        ]);
    }

    async function editUserGroups(user) {
        try {
            const allGroups = await api('/users/groups');
            const userGs = new Set(user.groups || []);
            const html = `
                <div class="usr-form">
                    <p>${t('Grupy użytkownika')} <strong>${user.username}</strong>:</p>
                    <div class="usr-group-checkboxes">
                        ${allGroups.map(g => `
                            <label class="usr-checkbox-label">
                                <input type="checkbox" value="${g.name}" ${userGs.has(g.name) ? 'checked' : ''}>
                                ${g.name} <small class="usr-muted">(${g.members?.length || 0} ${t('członków)')}</small>
                            </label>
                        `).join('')}
                    </div>
                </div>
            `;
            showModal(t('Grupy') + ' — ' + user.username, html, [
                { label: t('Anuluj'), class: 'secondary' },
                {
                    label: t('Zapisz'), class: 'primary', action: async (modal) => {
                        const checks = modal.querySelectorAll('.usr-group-checkboxes input[type="checkbox"]');
                        const selected = [...checks].filter(c => c.checked).map(c => c.value);
                        // Find adds and removes
                        const toAdd = selected.filter(g => !userGs.has(g));
                        const toRemove = [...userGs].filter(g => !selected.includes(g));
                        try {
                            for (const g of toAdd) {
                                await api('/users/groups/members', { method: 'POST', body: { group: g, username: user.username, action: 'add' } });
                            }
                            for (const g of toRemove) {
                                await api('/users/groups/members', { method: 'POST', body: { group: g, username: user.username, action: 'remove' } });
                            }
                            toast(t('Grupy zaktualizowane'), 'success');
                            loadUsers();
                        } catch (e) { toast(e.message, 'error'); }
                    }
                }
            ]);
        } catch (e) { toast(e.message, 'error'); }
    }

    async function deleteUser(user) {
        confirmDialog(t('Usunąć użytkownika') + ` <strong>${user.username}</strong>?<br><small>${t('Katalog domowy zostanie zachowany.')}</small>`, async () => {
            try {
                const res = await api('/users/delete', { method: 'POST', body: { username: user.username } });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Usunięto') + ` ${user.username}`, 'success');
                loadUsers();
            } catch (e) { toast(e.message, 'error'); }
        });
    }

    /* ═══════ GROUPS ═══════ */
    const groupList = root.querySelector('#usr-group-list');

    async function loadGroups() {
        groupList.innerHTML = `<div class="usr-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        try {
            const groups = await api('/users/groups');
            if (!groups.length) { groupList.innerHTML = `<div class="usr-empty">${t('Brak grup')}</div>`; return; }
            groupList.innerHTML = '';
            groups.forEach(g => {
                const card = document.createElement('div');
                card.className = 'usr-card group-card';
                const hasPriv = g.app_privileges && g.app_privileges.length > 0;
                card.innerHTML = `
                    <div class="usr-card-avatar group">
                        <i class="fas fa-users"></i>
                    </div>
                    <div class="usr-card-info">
                        <div class="usr-card-name">
                            ${g.name}
                            ${g.gid !== undefined ? `<span class="usr-muted">GID: ${g.gid}</span>` : ''}
                            ${hasPriv ? '<span class="usr-badge priv"><i class="fas fa-key"></i> ' + t('Uprawnienia') + '</span>' : ''}
                        </div>
                        <div class="usr-card-members">
                            ${(g.members || []).length ? 
                                `<span class="usr-muted">${t('Członkowie')}:</span> ${g.members.map(m => `<span class="usr-group-tag">${m}</span>`).join('')}`
                                : `<span class="usr-muted">${t('Brak członków')}</span>`}
                        </div>
                        ${hasPriv ? `<div class="usr-card-privs"><span class="usr-muted">${t('Aplikacje')}:</span> ${g.app_privileges.map(a => `<span class="usr-app-tag">${a}</span>`).join('')}</div>` : ''}
                    </div>
                    <div class="usr-card-actions">
                        <button class="usr-icon-btn" title="${t('Zarządzaj członkami')}" data-action="members"><i class="fas fa-user-edit"></i></button>
                        <button class="usr-icon-btn danger" title="${t('Usuń grupę')}" data-action="delete"><i class="fas fa-trash"></i></button>
                    </div>
                `;
                card.querySelector('[data-action="members"]').addEventListener('click', () => manageMembers(g));
                card.querySelector('[data-action="delete"]').addEventListener('click', () => deleteGroup(g));
                groupList.appendChild(card);
            });
        } catch (err) {
            groupList.innerHTML = `<div class="usr-error"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd')}: ${err.message}</div>`;
        }
    }

    root.querySelector('#usr-btn-add-group').addEventListener('click', () => {
        showModal(t('Nowa grupa'), `
            <div class="usr-form">
                <label>${t('Nazwa grupy')}</label>
                <input type="text" id="usr-new-groupname" placeholder="${t('np. multimedia')}" class="usr-input">
            </div>
        `, [
            { label: t('Anuluj'), class: 'secondary' },
            {
                label: t('Utwórz'), class: 'primary', action: async (modal) => {
                    const name = modal.querySelector('#usr-new-groupname').value.trim();
                    if (!name) { toast(t('Podaj nazwę grupy'), 'error'); return; }
                    try {
                        const res = await api('/users/groups/create', { method: 'POST', body: { name } });
                        if (res.error) { toast(res.error, 'error'); return; }
                        toast(t('Utworzono grupę') + ` ${name}`, 'success');
                        loadGroups();
                    } catch (e) { toast(e.message, 'error'); }
                }
            }
        ]);
    });

    async function manageMembers(group) {
        try {
            const users = await api('/users/list');
            const memberSet = new Set(group.members || []);
            showModal(t('Członkowie grupy') + ` — ${group.name}`, `
                <div class="usr-form">
                    <div class="usr-group-checkboxes">
                        ${users.map(u => `
                            <label class="usr-checkbox-label">
                                <input type="checkbox" value="${u.username}" ${memberSet.has(u.username) ? 'checked' : ''}>
                                ${u.username}
                            </label>
                        `).join('')}
                    </div>
                </div>
            `, [
                { label: t('Anuluj'), class: 'secondary' },
                {
                    label: t('Zapisz'), class: 'primary', action: async (modal) => {
                        const checks = modal.querySelectorAll('.usr-group-checkboxes input[type="checkbox"]');
                        const selected = new Set([...checks].filter(c => c.checked).map(c => c.value));
                        try {
                            for (const u of selected) {
                                if (!memberSet.has(u)) {
                                    await api('/users/groups/members', { method: 'POST', body: { group: group.name, username: u, action: 'add' } });
                                }
                            }
                            for (const u of memberSet) {
                                if (!selected.has(u)) {
                                    await api('/users/groups/members', { method: 'POST', body: { group: group.name, username: u, action: 'remove' } });
                                }
                            }
                            toast(t('Członkowie zaktualizowani'), 'success');
                            loadGroups();
                        } catch (e) { toast(e.message, 'error'); }
                    }
                }
            ]);
        } catch (e) { toast(e.message, 'error'); }
    }

    async function deleteGroup(group) {
        confirmDialog(t('Usunąć grupę') + ` <strong>${group.name}</strong>?`, async () => {
            try {
                const res = await api('/users/groups/delete', { method: 'POST', body: { name: group.name } });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Usunięto grupę') + ` ${group.name}`, 'success');
                loadGroups();
            } catch (e) { toast(e.message, 'error'); }
        });
    }

    /* ═══════ PRIVILEGES ═══════ */
    const privGrid = root.querySelector('#usr-priv-grid');
    const privActions = root.querySelector('#usr-priv-actions');
    let privData = {}; // group -> Set of app ids

    async function loadPrivileges() {
        privGrid.innerHTML = `<div class="usr-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        try {
            const [groups, privileges, apps] = await Promise.all([
                api('/users/groups'),
                api('/users/privileges'),
                api('/apps')
            ]);
            // Filter out admin-only apps and users app itself
            const availableApps = apps.filter(a => !a.admin_only);

            if (!groups.length) {
                privGrid.innerHTML = `<div class="usr-empty">${t('Brak grup — utwórz grupę w zakładce Grupy')}</div>`;
                privActions.style.display = 'none';
                return;
            }

            privData = {};
            groups.forEach(g => {
                privData[g.name] = new Set(privileges[g.name] || []);
            });

            privGrid.innerHTML = `
                <table class="usr-priv-table">
                    <thead>
                        <tr>
                            <th class="usr-priv-group-col">${t('Grupa')}</th>
                            ${availableApps.map(a => `<th class="usr-priv-app-col" title="${a.name}"><i class="fas ${a.icon}" style="color:${a.color}"></i><br><small>${a.name}</small></th>`).join('')}
                        </tr>
                    </thead>
                    <tbody>
                        ${groups.map(g => {
                            const isAdmin = g.name === 'ethos-admin' || g.name === 'sudo';
                            return `<tr>
                                <td class="usr-priv-group-name">${g.name} <small class="usr-muted">(${(g.members||[]).length})</small>${isAdmin ? ` <small style="color:var(--accent)">${t('pełny dostęp')}</small>` : ''}</td>
                                ${availableApps.map(a => `
                                    <td class="usr-priv-cell">
                                        <input type="checkbox" data-group="${g.name}" data-app="${a.id}"
                                            ${isAdmin || privData[g.name]?.has(a.id) ? 'checked' : ''}
                                            ${isAdmin ? 'disabled' : ''}>
                                    </td>
                                `).join('')}
                            </tr>`;
                        }).join('')}
                    </tbody>
                </table>
            `;
            privActions.style.display = 'flex';
        } catch (err) {
            privGrid.innerHTML = `<div class="usr-error"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd:')} ${err.message}</div>`;
            privActions.style.display = 'none';
        }
    }

    root.querySelector('#usr-priv-save').addEventListener('click', async () => {
        const checks = privGrid.querySelectorAll('input[type="checkbox"]');
        const newPriv = {};
        checks.forEach(cb => {
            const grp = cb.dataset.group;
            const app = cb.dataset.app;
            if (!newPriv[grp]) newPriv[grp] = [];
            if (cb.checked) newPriv[grp].push(app);
        });
        try {
            const res = await api('/users/privileges', { method: 'POST', body: newPriv });
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Uprawnienia zapisane'), 'success');
            // Reload to confirm saved state
            loadPrivileges();
        } catch (e) { toast(e.message, 'error'); }
    });

    /* ─── LDAP / AD Tab ─── */
    async function loadLdap() {
        const wrap = root.querySelector('#usr-ldap-content');
        if (!wrap) return;

        // Check if LDAP app is installed
        let status;
        try {
            status = await api('/ldap/status');
        } catch (e) {
            wrap.innerHTML = `
                <div class="usr-ldap-not-installed">
                    <i class="fas fa-info-circle" style="font-size:2rem;color:var(--accent)"></i>
                    <h3>${t('LDAP / AD nie jest zainstalowany')}</h3>
                    <p>${t('Zainstaluj aplikację LDAP / Active Directory z App Store, aby włączyć integrację.')}</p>
                </div>`;
            return;
        }
        if (status && status.error && status.error.includes('not installed')) {
            wrap.innerHTML = `
                <div class="usr-ldap-not-installed">
                    <i class="fas fa-info-circle" style="font-size:2rem;color:var(--accent)"></i>
                    <h3>${t('LDAP / AD nie jest zainstalowany')}</h3>
                    <p>${t('Zainstaluj aplikację LDAP / Active Directory z App Store, aby włączyć integrację.')}</p>
                </div>`;
            return;
        }

        // Load config
        let cfg;
        try {
            cfg = await api('/ldap/config');
            if (cfg.error) { wrap.innerHTML = `<div class="usr-error">${cfg.error}</div>`; return; }
            cfg = cfg.config || {};
        } catch (e) {
            wrap.innerHTML = `<div class="usr-error">${e.message}</div>`;
            return;
        }

        const en = cfg.enabled ? 'checked' : '';
        const ssl = cfg.use_ssl ? 'checked' : '';
        const tls = cfg.use_starttls ? 'checked' : '';

        wrap.innerHTML = `
            <div class="usr-ldap-form">
                <div class="usr-ldap-toggle-row">
                    <label class="usr-ldap-toggle">
                        <input type="checkbox" id="ldap-enabled" ${en}>
                        <span class="usr-toggle-slider"></span>
                    </label>
                    <span class="usr-ldap-toggle-label">${t('Włącz LDAP / Active Directory')}</span>
                    <span class="usr-ldap-status" id="ldap-status-badge"></span>
                </div>

                <div class="usr-ldap-section">
                    <h4><i class="fas fa-server"></i> ${t('Serwer')}</h4>
                    <div class="usr-ldap-grid">
                        <label>${t('Adres serwera')}</label>
                        <input type="text" id="ldap-server" class="fm-input" value="${cfg.server || ''}" placeholder="ldap.example.com">

                        <label>${t('Port')}</label>
                        <input type="number" id="ldap-port" class="fm-input" value="${cfg.port || 389}" style="width:100px">

                        <label>SSL</label>
                        <label class="usr-ldap-toggle"><input type="checkbox" id="ldap-ssl" ${ssl}><span class="usr-toggle-slider"></span></label>

                        <label>StartTLS</label>
                        <label class="usr-ldap-toggle"><input type="checkbox" id="ldap-starttls" ${tls}><span class="usr-toggle-slider"></span></label>
                    </div>
                </div>

                <div class="usr-ldap-section">
                    <h4><i class="fas fa-link"></i> ${t('Bind (konto serwisowe)')}</h4>
                    <div class="usr-ldap-grid">
                        <label>Bind DN</label>
                        <input type="text" id="ldap-bind-dn" class="fm-input" value="${cfg.bind_dn || ''}" placeholder="cn=admin,dc=example,dc=com">

                        <label>${t('Hasło')}</label>
                        <input type="password" id="ldap-bind-pw" class="fm-input" value="${cfg.bind_password || ''}" placeholder="••••••••">
                    </div>
                </div>

                <div class="usr-ldap-section">
                    <h4><i class="fas fa-search"></i> ${t('Wyszukiwanie użytkowników')}</h4>
                    <div class="usr-ldap-grid">
                        <label>Base DN</label>
                        <input type="text" id="ldap-base-dn" class="fm-input" value="${cfg.base_dn || ''}" placeholder="dc=example,dc=com">

                        <label>${t('Filtr')}</label>
                        <input type="text" id="ldap-filter" class="fm-input" value="${cfg.user_filter || '(&(objectClass=person)(sAMAccountName={username}))'}" placeholder="(&(objectClass=person)(sAMAccountName={username}))">

                        <label>${t('Atrybut login')}</label>
                        <input type="text" id="ldap-attr-user" class="fm-input" value="${cfg.user_attr || 'sAMAccountName'}" style="width:200px">
                    </div>
                </div>

                <div class="usr-ldap-section">
                    <h4><i class="fas fa-users-cog"></i> ${t('Mapowanie ról (grupy LDAP → EthOS)')}</h4>
                    <div class="usr-ldap-grid">
                        <label>${t('Grupy admin')}</label>
                        <input type="text" id="ldap-grp-admin" class="fm-input" value="${(cfg.admin_groups || []).join(', ')}" placeholder="Domain Admins, IT-Admins">

                        <label>${t('Grupy użytkowników')}</label>
                        <input type="text" id="ldap-grp-user" class="fm-input" value="${(cfg.user_groups || []).join(', ')}" placeholder="Domain Users">

                        <label>${t('Grupy rodzina')}</label>
                        <input type="text" id="ldap-grp-family" class="fm-input" value="${(cfg.family_groups || []).join(', ')}" placeholder="Family">
                    </div>
                </div>

                <div class="usr-ldap-actions">
                    <button class="btn btn-primary" id="ldap-save"><i class="fas fa-save"></i> ${t('Zapisz')}</button>
                    <button class="usr-btn" id="ldap-test"><i class="fas fa-plug"></i> ${t('Test połączenia')}</button>
                    <button class="usr-btn" id="ldap-sync"><i class="fas fa-sync-alt"></i> ${t('Synchronizuj teraz')}</button>
                    <button class="usr-btn danger" id="ldap-remove" style="margin-left:auto"><i class="fas fa-trash"></i> ${t('Usuń konfigurację')}</button>
                </div>
                <div id="ldap-test-result" class="usr-ldap-test-result" style="display:none"></div>
            </div>
        `;

        // Update status badge
        const badge = wrap.querySelector('#ldap-status-badge');
        if (status && status.enabled) {
            badge.innerHTML = `<span class="usr-badge-ok"><i class="fas fa-check-circle"></i> ${t('Aktywny')}</span>`;
        } else {
            badge.innerHTML = `<span class="usr-badge-off"><i class="fas fa-minus-circle"></i> ${t('Wyłączony')}</span>`;
        }

        // SSL toggle auto-adjusts port
        wrap.querySelector('#ldap-ssl').addEventListener('change', (e) => {
            const portInput = wrap.querySelector('#ldap-port');
            if (e.target.checked) {
                portInput.value = 636;
                wrap.querySelector('#ldap-starttls').checked = false;
            } else {
                portInput.value = 389;
            }
        });

        // Save
        wrap.querySelector('#ldap-save').addEventListener('click', async () => {
            const body = {
                enabled: wrap.querySelector('#ldap-enabled').checked,
                server: wrap.querySelector('#ldap-server').value.trim(),
                port: parseInt(wrap.querySelector('#ldap-port').value) || 389,
                use_ssl: wrap.querySelector('#ldap-ssl').checked,
                use_starttls: wrap.querySelector('#ldap-starttls').checked,
                bind_dn: wrap.querySelector('#ldap-bind-dn').value.trim(),
                bind_password: wrap.querySelector('#ldap-bind-pw').value,
                base_dn: wrap.querySelector('#ldap-base-dn').value.trim(),
                user_filter: wrap.querySelector('#ldap-filter').value.trim(),
                user_attr: wrap.querySelector('#ldap-attr-user').value.trim(),
                admin_groups: wrap.querySelector('#ldap-grp-admin').value.split(',').map(s => s.trim()).filter(Boolean),
                user_groups: wrap.querySelector('#ldap-grp-user').value.split(',').map(s => s.trim()).filter(Boolean),
                family_groups: wrap.querySelector('#ldap-grp-family').value.split(',').map(s => s.trim()).filter(Boolean),
            };
            try {
                const res = await api('/ldap/config', { method: 'PUT', body });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Konfiguracja LDAP zapisana'), 'success');
                loadLdap();
            } catch (e) { toast(e.message, 'error'); }
        });

        // Test connection
        wrap.querySelector('#ldap-test').addEventListener('click', async () => {
            const resultDiv = wrap.querySelector('#ldap-test-result');
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Testowanie połączenia...')}`;
            try {
                const res = await api('/ldap/test', { method: 'POST' });
                if (res.error) {
                    resultDiv.innerHTML = `<i class="fas fa-times-circle" style="color:var(--danger)"></i> ${res.error}`;
                } else if (res.ok) {
                    resultDiv.innerHTML = `<i class="fas fa-check-circle" style="color:var(--success)"></i> ${t('Połączenie udane!')} ${res.user_count != null ? t('{count} użytkowników znaleziono', { count: res.user_count }) : ''}`;
                }
            } catch (e) {
                resultDiv.innerHTML = `<i class="fas fa-times-circle" style="color:var(--danger)"></i> ${e.message}`;
            }
        });

        // Sync
        wrap.querySelector('#ldap-sync').addEventListener('click', async () => {
            try {
                const res = await api('/ldap/sync', { method: 'POST' });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Synchronizacja zakończona: {synced} użytkowników', { synced: res.synced || 0 }), 'success');
            } catch (e) { toast(e.message, 'error'); }
        });

        // Remove
        wrap.querySelector('#ldap-remove').addEventListener('click', async () => {
            if (!confirm(t('Czy na pewno chcesz usunąć konfigurację LDAP?'))) return;
            try {
                const res = await api('/ldap/config', { method: 'DELETE' });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Konfiguracja LDAP usunięta'), 'success');
                loadLdap();
            } catch (e) { toast(e.message, 'error'); }
        });
    }

    /* ─── Initial load ─── */
    loadUsers();

    } // end init
};
