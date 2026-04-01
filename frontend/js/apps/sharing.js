/* ═══════════════════════════════════════════════════════════
   ${t('EthOS — Udostępnianie (Multi-protocol file sharing)')}
   Samba · NFS · DLNA · WebDAV · SFTP · FTP
   Tabs only appear for protocols installed via App Store.
   ═══════════════════════════════════════════════════════════ */

AppRegistry['sharing'] = function (appDef) {
    createWindow('sharing', {
        title: t('Udostępnianie'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 820,
        height: 560,
        onRender: (body) => renderSharingApp(body),
        onClose: () => { /* no intervals/sockets to clean */ },
    });
};

/* ── helpers ────────────────────────────────── */
function _shBadge(ok, label) {
    if (ok === null) return `<span class="fm-badge shr-badge-loading">${label || '…'}</span>`;
    if (ok === 'off') return `<span class="fm-badge shr-badge-warn">${label || t('Wyłączony')}</span>`;
    return ok
        ? `<span class="fm-badge fm-badge-green">${label || t('Aktywny')}</span>`
        : `<span class="fm-badge shr-badge-err">${label || t('Nieaktywny')}</span>`;
}

function _shEsc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function _shEscAttr(s) {
    return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* All 6 protocols — order matters for sidebar */
const _SH_ALL_PROTOS = [
    { id: 'samba',  pkgId: 'sharing-samba',  icon: 'fa-windows',      label: 'Samba' },
    { id: 'nfs',    pkgId: 'sharing-nfs',    icon: 'fa-network-wired', label: 'NFS' },
    { id: 'dlna',   pkgId: 'sharing-dlna',   icon: 'fa-photo-video',  label: 'DLNA' },
    { id: 'webdav', pkgId: 'sharing-webdav', icon: 'fa-globe',        label: 'WebDAV' },
    { id: 'sftp',   pkgId: 'sharing-sftp',   icon: 'fa-lock',         label: 'SFTP' },
    { id: 'ftp',    pkgId: 'sharing-ftp',    icon: 'fa-upload',       label: 'FTP' },
];

/* ── main render ────────────────────────────── */
async function renderSharingApp(body) {
    const $ = (s) => body.querySelector(s);

    /* Show loading state */
    body.innerHTML = `<div class="shr-loading"><i class="fas fa-spinner fa-spin shr-spin-icon"></i>${t('Ładowanie…')}</div>`;

    /* Fetch installed packages to determine which tabs to show */
    let installedProtos;
    try {
        const pkgs = await api('/ethos-packages');
        const installed = new Set(pkgs.filter(p => p.installed).map(p => p.id));
        installedProtos = _SH_ALL_PROTOS.filter(p => installed.has(p.pkgId));
    } catch (e) {
        installedProtos = _SH_ALL_PROTOS; /* fallback: show all */
    }

    /* Empty state — no protocols installed */
    if (!installedProtos.length) {
        body.innerHTML = `
        <div class="shr-empty">
            <i class="fas fa-share-alt shr-empty-icon"></i>
            <h3 class="shr-empty-title">${t('Brak zainstalowanych protokołów')}</h3>
            <p class="shr-empty-text">
                ${t('Zainstaluj protokoły udostępniania (Samba, NFS, DLNA, WebDAV, SFTP, FTP) w')}
                <a href="#" id="sh-goto-store" class="shr-link">${t('App Store')}</a>.
            </p>
        </div>`;
        const link = $('#sh-goto-store');
        if (link) link.onclick = (e) => { e.preventDefault(); if (typeof openApp === 'function') openApp('app-store'); };
        return;
    }

    /* ── Layout: left sidebar + right panel ──
       body IS the .window-body (flex:1; overflow:auto).
       We make it a flex-row container directly so sidebar + panel
       sit side by side without needing height:100% on a wrapper. */
    body.style.cssText = 'display:flex;flex-direction:row;overflow:hidden;padding:0';
    body.innerHTML = `
        <div class="sh-sidebar shr-sidebar">
            ${installedProtos.map(p => `
                <button class="sh-tab shr-tab-btn" data-tab="${p.id}"><i class="fas ${p.icon} shr-tab-icon"></i><span>${p.label}</span></button>
            `).join('')}
        </div>
        <div id="sh-panel" class="shr-panel"></div>`;

    let activeTab = null;

    function switchTab(id) {
        activeTab = id;
        body.querySelectorAll('.sh-tab').forEach(b => {
            const active = b.dataset.tab === id;
            b.style.borderLeftColor = active ? '#6366f1' : 'transparent';
            b.style.color = active ? 'var(--text)' : 'var(--text-muted)';
            b.style.fontWeight = active ? '600' : '400';
            b.style.background = active ? 'rgba(99,102,241,.06)' : 'none';
        });
        const render = { samba: renderSamba, nfs: renderNFS, dlna: renderDLNA, webdav: renderWebDAV, sftp: renderSFTP, ftp: renderFTP };
        (render[id] || render.samba)($('#sh-panel'));
    }

    body.querySelectorAll('.sh-tab').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
    switchTab(installedProtos[0].id);

    /* ══════════════════════════════════════════
       SAMBA tab
       ══════════════════════════════════════════ */
    function renderSamba(panel) {
        const st = { shares: [] };
        panel.innerHTML = `
        <div class="shr-header">
            <span class="shr-title"><i class="fab fa-windows shr-icon-accent"></i>Samba</span>
            <span id="sh-smb-status"></span>
            <div class="shr-spacer"></div>
            <button class="fm-toolbar-btn btn-green" id="sh-smb-add"><i class="fas fa-plus"></i> ${t('Dodaj')}</button>
            <button class="fm-toolbar-btn" id="sh-smb-ref" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
        </div>
        <div id="sh-smb-list"></div>
        <div id="sh-smb-form" style="display:none"></div>
        <div class="shr-pw-section">
            <h4 class="shr-form-title"><i class="fas fa-key shr-icon-warn"></i> ${t('Hasło Samba')}</h4>
            <div class="shr-pw-row">
                <input type="text" id="sh-smb-pwu" class="fm-input" placeholder="${t('Użytkownik')}" style="width:140px">
                <input type="password" id="sh-smb-pwp" class="fm-input" placeholder="${t('Hasło')}" style="width:160px">
                <button class="fm-toolbar-btn btn-green" id="sh-smb-pwb"><i class="fas fa-save"></i> ${t('Ustaw')}</button>
            </div>
        </div>`;

        async function load() {
            try {
                const [shares, status] = await Promise.all([api('/storage/samba/shares'), api('/storage/samba/status')]);
                st.shares = shares || [];
                panel.querySelector('#sh-smb-status').innerHTML = _shBadge(status.running);
                renderList();
            } catch (e) { panel.querySelector('#sh-smb-list').innerHTML = `<div class="shr-error">${t('Błąd')}: ${e.message}</div>`; }
        }

        function renderList() {
            const w = panel.querySelector('#sh-smb-list');
            if (!st.shares.length) { w.innerHTML = `<div class="shr-empty-msg">${t('Brak udziałów')}</div>`; return; }
            w.innerHTML = `<table class="shr-table">
                <thead><tr class="shr-thead-row">
                    <th class="shr-th">${t('Nazwa')}</th><th class="shr-th">${t('Ścieżka')}</th>
                    <th class="shr-td-center">${t('Gość')}</th><th class="shr-td-center">${t('Zapis')}</th><th></th>
                </tr></thead><tbody>${st.shares.map(s => `<tr class="shr-tr">
                    <td class="shr-td-name">${_shEsc(s.name)}</td>
                    <td class="shr-td-sec">${_shEsc(s.path)}</td>
                    <td class="shr-td-center">${s.guest_ok ? '<i class="fas fa-check shr-check-ok"></i>' : '<i class="fas fa-times shr-check-no"></i>'}</td>
                    <td class="shr-td-center">${s.writable ? '<i class="fas fa-check shr-check-ok"></i>' : '<i class="fas fa-times shr-check-no"></i>'}</td>
                    <td class="shr-td-actions">
                        <button class="fm-toolbar-btn btn-sm sh-sma" data-name="${_shEscAttr(s.name)}" title="${t('Uprawnienia')}"><i class="fas fa-shield-alt"></i></button>
                        <button class="fm-toolbar-btn btn-sm sh-sme" data-name="${_shEscAttr(s.name)}" data-path="${_shEscAttr(s.path)}" data-guest="${s.guest_ok}" data-writable="${s.writable}"><i class="fas fa-pen"></i></button>
                        <button class="fm-toolbar-btn btn-sm btn-red sh-smd" data-name="${_shEscAttr(s.name)}"><i class="fas fa-trash"></i></button>
                    </td></tr>`).join('')}</tbody></table>`;
            w.querySelectorAll('.sh-sma').forEach(b => b.onclick = () => showAclEditor(b.dataset.name));
            w.querySelectorAll('.sh-sme').forEach(b => b.onclick = () => showForm({ name: b.dataset.name, path: b.dataset.path, guest_ok: b.dataset.guest === 'true', writable: b.dataset.writable === 'true' }));
            w.querySelectorAll('.sh-smd').forEach(b => b.onclick = async () => { if (!await confirmDialog(t('Usunąć udział'), t('Usunąć') + ` "${b.dataset.name}"?`)) return; await api('/storage/samba/share', { method: 'DELETE', body: { name: b.dataset.name } }); toast(t('Usunięto'), 'success'); load(); });
        }

        function showForm(s) {
            const f = panel.querySelector('#sh-smb-form');
            f.style.display = '';
            f.innerHTML = `<div class="shr-form-section">
                <h4 class="shr-form-title">${s ? t('Edytuj') : t('Nowy udział')}</h4>
                <div class="shr-form-row">
                    <input type="text" id="sh-sf-n" class="fm-input" placeholder="${t('Nazwa')}" value="${s?.name || ''}" style="width:140px" ${s ? 'readonly' : ''}>
                    <div class="shr-input-group"><input type="text" id="sh-sf-p" class="fm-input" placeholder="${t('Ścieżka')}" value="${s?.path || ''}" style="width:200px;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn shr-input-group-btn" id="sh-sf-browse" title="${t('Przeglądaj')}"><i class="fas fa-folder-open"></i></button></div>
                    <label class="shr-checkbox-label"><input type="checkbox" id="sh-sf-g" ${!s || s.guest_ok ? 'checked' : ''}> ${t('Gość')}</label>
                    <label class="shr-checkbox-label"><input type="checkbox" id="sh-sf-w" ${!s || s.writable ? 'checked' : ''}> ${t('Zapis')}</label>
                    <button class="fm-toolbar-btn btn-green" id="sh-sf-ok"><i class="fas fa-save"></i></button>
                    <button class="fm-toolbar-btn" id="sh-sf-x"><i class="fas fa-times"></i></button>
                </div></div>`;
            panel.querySelector('#sh-sf-browse').onclick = () => openDirPicker(panel.querySelector('#sh-sf-p').value || '/home', t('Wybierz folder'), p => { panel.querySelector('#sh-sf-p').value = p; });
            panel.querySelector('#sh-sf-ok').onclick = async () => {
                const n = panel.querySelector('#sh-sf-n').value.trim(), p = panel.querySelector('#sh-sf-p').value.trim();
                if (!n || !p) { toast(t('Podaj nazwę i ścieżkę'), 'warning'); return; }
                try {
                    const resp = await api('/storage/samba/share', { method: 'POST', body: { name: n, path: p, guest_ok: panel.querySelector('#sh-sf-g').checked, writable: panel.querySelector('#sh-sf-w').checked } });
                    if (resp.error) { toast(t('Błąd: ') + resp.error, 'error'); return; }
                    toast(t('Udział zapisany'), 'success'); f.style.display = 'none'; load();
                } catch (e) { toast(t('Błąd zapisu: ') + (e.message || 'nieznany'), 'error'); }
            };
            panel.querySelector('#sh-sf-x').onclick = () => { f.style.display = 'none'; };
        }

        async function showAclEditor(shareName) {
            let acl, users, groups;
            try {
                const [aclRes, usersRes, groupsRes] = await Promise.all([
                    api('/storage/samba/share/acl?name=' + encodeURIComponent(shareName)),
                    api('/users/list'),
                    api('/users/groups'),
                ]);
                acl = aclRes.acl || { users: {}, groups: {} };
                users = (Array.isArray(usersRes) ? usersRes : []).map(u => u.username);
                groups = (groupsRes.groups || []).map(g => g.name);
            } catch (e) { toast(t('Błąd ładowania ACL'), 'error'); return; }

            const permOpts = (current) => ['rw', 'ro', 'none'].map(p =>
                `<option value="${p}" ${current === p ? 'selected' : ''}>${p === 'rw' ? t('Odczyt/Zapis') : p === 'ro' ? t('Tylko odczyt') : t('Brak dostępu')}</option>`
            ).join('');

            let userRows = users.map(u => `<tr><td style="padding:4px 8px">${_shEsc(u)}</td><td style="padding:4px"><select class="fm-input acl-u" data-name="${_shEscAttr(u)}" style="width:160px">${permOpts(acl.users[u] || '')}<option value="" ${!acl.users[u] ? 'selected' : ''}>—</option></select></td></tr>`).join('');
            let groupRows = groups.map(g => `<tr><td style="padding:4px 8px">@${_shEsc(g)}</td><td style="padding:4px"><select class="fm-input acl-g" data-name="${_shEscAttr(g)}" style="width:160px">${permOpts(acl.groups[g] || '')}<option value="" ${!acl.groups[g] ? 'selected' : ''}>—</option></select></td></tr>`).join('');

            showModal(t('Uprawnienia — ') + shareName, `
                <div style="max-height:400px;overflow-y:auto">
                    <h4 style="margin:0 0 8px;font-size:13px;color:var(--text-secondary)"><i class="fas fa-user"></i> ${t('Użytkownicy')}</h4>
                    <table style="width:100%;margin-bottom:12px">${userRows || '<tr><td style="color:var(--text-muted);padding:8px">' + t('Brak użytkowników') + '</td></tr>'}</table>
                    <h4 style="margin:0 0 8px;font-size:13px;color:var(--text-secondary)"><i class="fas fa-users"></i> ${t('Grupy')}</h4>
                    <table style="width:100%">${groupRows || '<tr><td style="color:var(--text-muted);padding:8px">' + t('Brak grup') + '</td></tr>'}</table>
                    <p style="font-size:11px;color:var(--text-muted);margin-top:8px"><i class="fas fa-info-circle"></i> ${t('Puste = domyślnie (wszyscy mogą). Ustaw jawnie aby ograniczyć.')}</p>
                </div>
            `, [
                { label: t('Anuluj'), class: 'secondary' },
                { label: t('Resetuj ACL'), class: 'warning', action: async () => {
                    if (!await confirmDialog(t('Usunąć wszystkie uprawnienia dla tego udziału?'))) return;
                    await api('/storage/samba/share/acl', { method: 'DELETE', body: { name: shareName } });
                    toast(t('ACL usunięte'), 'success');
                }},
                { label: t('Zapisz'), class: 'primary', action: async (modal) => {
                    const newAcl = { users: {}, groups: {} };
                    modal.querySelectorAll('.acl-u').forEach(sel => { if (sel.value) newAcl.users[sel.dataset.name] = sel.value; });
                    modal.querySelectorAll('.acl-g').forEach(sel => { if (sel.value) newAcl.groups[sel.dataset.name] = sel.value; });
                    try {
                        const r = await api('/storage/samba/share/acl', { method: 'PUT', body: { name: shareName, ...newAcl } });
                        if (r.error) { toast(r.error, 'error'); return; }
                        toast(t('Uprawnienia zapisane'), 'success');
                    } catch (e) { toast(e.message, 'error'); }
                }}
            ]);
        }

        panel.querySelector('#sh-smb-add').onclick = () => showForm(null);
        panel.querySelector('#sh-smb-ref').onclick = () => load();
        panel.querySelector('#sh-smb-pwb').onclick = async () => {
            const u = panel.querySelector('#sh-smb-pwu').value.trim(), p = panel.querySelector('#sh-smb-pwp').value;
            if (!u || !p) { toast(t('Podaj użytkownika i hasło'), 'warning'); return; }
            try {
                const resp = await api('/storage/samba/password', { method: 'POST', body: { username: u, password: p } });
                if (resp.error) { toast(t('Błąd: ') + resp.error, 'error'); return; }
                toast(t('Hasło ustawione'), 'success'); panel.querySelector('#sh-smb-pwp').value = '';
            } catch(e) { toast(t('Błąd ustawiania hasła: ') + e.message, 'error'); }
        };
        load();
    }

    /* ══════════════════════════════════════════
       NFS tab
       ══════════════════════════════════════════ */
    function renderNFS(panel) {
        panel.innerHTML = `
        <div class="shr-header">
            <span class="shr-title"><i class="fas fa-network-wired shr-icon-accent"></i>NFS</span>
            <span id="sh-nfs-st"></span>
            <div class="shr-spacer"></div>
            <button class="fm-toolbar-btn btn-green" id="sh-nfs-add"><i class="fas fa-plus"></i> ${t('Dodaj eksport')}</button>
            <button class="fm-toolbar-btn" id="sh-nfs-ref"><i class="fas fa-sync-alt"></i></button>
        </div>
        <p class="shr-desc">${t('NFS — szybkie udostępnianie dla klientów Linux/Mac. Idealne do montowania katalogów na wielu maszynach.')}</p>
        <div id="sh-nfs-list"></div>
        <div id="sh-nfs-form" style="display:none"></div>`;

        async function load() {
            try {
                const [exports, status] = await Promise.all([api('/storage/nfs/exports'), api('/storage/nfs/status')]);
                panel.querySelector('#sh-nfs-st').innerHTML = _shBadge(status.running);
                renderList(exports.exports || []);
            } catch (e) { panel.querySelector('#sh-nfs-list').innerHTML = `<div class="shr-error">${t('Błąd')}: ${_shEsc(e.message)}</div>`; }
        }

        function renderList(exports) {
            const w = panel.querySelector('#sh-nfs-list');
            if (!exports.length) { w.innerHTML = `<div class="shr-empty-msg">${t('Brak eksportów NFS')}</div>`; return; }
            w.innerHTML = `<table class="shr-table">
                <thead><tr class="shr-thead-row">
                    <th class="shr-th">${t('Ścieżka')}</th><th class="shr-th">${t('Klienci / opcje')}</th><th></th>
                </tr></thead><tbody>${exports.map(e => `<tr class="shr-tr">
                    <td class="shr-td-name">${_shEsc(e.path)}</td>
                    <td class="shr-td-sec">${_shEsc(e.clients)}</td>
                    <td class="shr-td-actions"><button class="fm-toolbar-btn btn-sm btn-red sh-nfsd" data-path="${_shEscAttr(e.path)}"><i class="fas fa-trash"></i></button></td>
                </tr>`).join('')}</tbody></table>`;
            w.querySelectorAll('.sh-nfsd').forEach(b => b.onclick = async () => {
                if (!await confirmDialog(t('Usunąć eksport'), t('Usunąć eksport') + ` "${b.dataset.path}"?`)) return;
                await api('/storage/nfs/export', { method: 'DELETE', body: { path: b.dataset.path } });
                toast(t('Usunięto'), 'success'); load();
            });
        }

        function showForm() {
            const f = panel.querySelector('#sh-nfs-form');
            f.style.display = '';
            f.innerHTML = `<div class="shr-form-section">
                <h4 class="shr-form-title">${t('Nowy eksport NFS')}</h4>
                <div class="shr-form-row">
                    <div class="shr-input-group"><input type="text" id="sh-nf-p" class="fm-input" placeholder="${t('Ścieżka np. /home/media')}" style="width:200px;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn shr-input-group-btn" id="sh-nf-browse" title="${t('Przeglądaj')}"><i class="fas fa-folder-open"></i></button></div>
                    <input type="text" id="sh-nf-n" class="fm-input" placeholder="${t('Sieć np. 192.168.1.0/24 lub *')}" value="*" style="width:180px">
                    <button class="fm-toolbar-btn btn-green" id="sh-nf-ok"><i class="fas fa-save"></i></button>
                    <button class="fm-toolbar-btn" id="sh-nf-x"><i class="fas fa-times"></i></button>
                </div></div>`;
            panel.querySelector('#sh-nf-browse').onclick = () => openDirPicker('/home', t('Wybierz folder do eksportu'), p => { panel.querySelector('#sh-nf-p').value = p; });
            panel.querySelector('#sh-nf-ok').onclick = async () => {
                const p = panel.querySelector('#sh-nf-p').value.trim(), n = panel.querySelector('#sh-nf-n').value.trim();
                if (!p) { toast(t('Podaj ścieżkę'), 'warning'); return; }
                await api('/storage/nfs/export', { method: 'POST', body: { path: p, network: n } });
                toast(t('Dodano'), 'success'); f.style.display = 'none'; load();
            };
            panel.querySelector('#sh-nf-x').onclick = () => { f.style.display = 'none'; };
        }

        panel.querySelector('#sh-nfs-add').onclick = () => showForm();
        panel.querySelector('#sh-nfs-ref').onclick = () => load();
        load();
    }

    /* ══════════════════════════════════════════
       DLNA tab
       ══════════════════════════════════════════ */
    function renderDLNA(panel) {
        panel.innerHTML = `
        <div class="shr-header">
            <span class="shr-title"><i class="fas fa-photo-video shr-icon-accent"></i>DLNA / UPnP</span>
            <span id="sh-dlna-st"></span>
            <div class="shr-spacer"></div>
            <span id="sh-dlna-actions"></span>
            <button class="fm-toolbar-btn" id="sh-dlna-ref" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
        </div>
        <div id="sh-dlna-stats" class="shr-dlna-stats" style="display:none">
            <span class="shr-dlna-stat"><i class="fas fa-film"></i> <span id="sh-dlna-fcount">0</span> ${t('plików')}</span>
            <span class="shr-dlna-stat"><i class="fas fa-network-wired"></i> ${t('Port')}: <span id="sh-dlna-port">8200</span></span>
            <span class="shr-dlna-stat"><i class="fas fa-signature"></i> <span id="sh-dlna-fname">—</span></span>
        </div>
        <p class="shr-desc">${t('DLNA streamuje media (filmy, muzykę, zdjęcia) do Smart TV, konsol i innych urządzeń w sieci.')}</p>
        <div id="sh-dlna-install" style="display:none">
            <div class="shr-empty-msg">
                <p>MiniDLNA ${t('nie jest zainstalowany')}.</p>
                <button class="fm-toolbar-btn btn-green" id="sh-dlna-install-btn"><i class="fas fa-download"></i> ${t('Zainstaluj MiniDLNA')}</button>
            </div>
        </div>
        <div id="sh-dlna-cfg" style="display:none"></div>`;

        let availableDrives = [];

        async function loadStatus() {
            try {
                const data = await api('/dlna/status');
                if (!data.installed) {
                    panel.querySelector('#sh-dlna-st').innerHTML = _shBadge(false, t('Nie zainstalowano'));
                    panel.querySelector('#sh-dlna-install').style.display = '';
                    panel.querySelector('#sh-dlna-cfg').style.display = 'none';
                    panel.querySelector('#sh-dlna-stats').style.display = 'none';
                    panel.querySelector('#sh-dlna-actions').innerHTML = '';
                    return;
                }
                panel.querySelector('#sh-dlna-install').style.display = 'none';
                panel.querySelector('#sh-dlna-cfg').style.display = '';
                if (data.running) {
                    panel.querySelector('#sh-dlna-st').innerHTML = _shBadge(true);
                    panel.querySelector('#sh-dlna-stats').style.display = '';
                    panel.querySelector('#sh-dlna-fcount').textContent = data.file_count || 0;
                    panel.querySelector('#sh-dlna-port').textContent = data.port || 8200;
                    panel.querySelector('#sh-dlna-fname').textContent = data.friendly_name || '—';
                    panel.querySelector('#sh-dlna-actions').innerHTML =
                        `<button class="fm-toolbar-btn btn-sm btn-red" id="sh-dlna-stop"><i class="fas fa-stop"></i> ${t('Zatrzymaj')}</button>`;
                    panel.querySelector('#sh-dlna-stop').onclick = async () => {
                        await api('/dlna/stop', { method: 'POST' });
                        toast(t('DLNA zatrzymany'), 'success'); loadStatus();
                    };
                } else {
                    panel.querySelector('#sh-dlna-st').innerHTML = _shBadge(false);
                    panel.querySelector('#sh-dlna-stats').style.display = 'none';
                    panel.querySelector('#sh-dlna-actions').innerHTML =
                        `<button class="fm-toolbar-btn btn-sm btn-green" id="sh-dlna-start"><i class="fas fa-play"></i> ${t('Uruchom')}</button>`;
                    panel.querySelector('#sh-dlna-start').onclick = async () => {
                        await api('/dlna/start', { method: 'POST' });
                        toast(t('DLNA uruchomiony'), 'success'); loadStatus();
                    };
                }
            } catch (e) {
                panel.querySelector('#sh-dlna-st').innerHTML = _shBadge(false, t('Błąd'));
            }
        }

        async function loadConfig() {
            try {
                const data = await api('/dlna/config');
                availableDrives = data.available_drives || [];
                renderCfg(data);
            } catch { /* handled by loadStatus */ }
        }

        function _dlnaEsc(s) {
            const d = document.createElement('div');
            d.textContent = s;
            return d.innerHTML;
        }

        function renderCfg(config) {
            const w = panel.querySelector('#sh-dlna-cfg');
            const mediaDirs = config.media_dirs || [];

            // Parse selected dirs into a map: path → type prefix string
            const selectedMap = {};
            mediaDirs.forEach(d => {
                const m = d.match(/^([AVP]+),(.+)$/);
                if (m) selectedMap[m[2]] = m[1];
                else selectedMap[d] = '';
            });

            let drivesHtml = '';
            if (availableDrives.length) {
                drivesHtml = availableDrives.map(drive => {
                    const isSel = drive in selectedMap;
                    const types = selectedMap[drive] || 'AVP';
                    return `<div class="shr-dir-row shr-dlna-drive">
                        <label class="shr-dlna-drv-check">
                            <input type="checkbox" class="sh-dlna-drv-cb" data-drive="${_dlnaEsc(drive)}" ${isSel ? 'checked' : ''}>
                            <span>${_dlnaEsc(drive)}</span>
                        </label>
                        <span class="shr-dlna-types" data-drive-types="${_dlnaEsc(drive)}">
                            <button class="shr-dlna-type-tag ${types.includes('V') ? 'active' : ''}" data-type="V" title="${t('Wideo')}">V</button>
                            <button class="shr-dlna-type-tag ${types.includes('A') ? 'active' : ''}" data-type="A" title="${t('Audio')}">A</button>
                            <button class="shr-dlna-type-tag ${types.includes('P') ? 'active' : ''}" data-type="P" title="${t('Zdjęcia')}">P</button>
                        </span>
                    </div>`;
                }).join('');
            }

            // Custom dirs (those not in availableDrives)
            const customDirs = Object.keys(selectedMap).filter(p => !availableDrives.includes(p));

            w.innerHTML = `
            <div class="shr-dlna-form">
                <div class="shr-form-row-inline">
                    <label class="shr-label">${t('Nazwa serwera')}:</label>
                    <input type="text" id="sh-dlna-name" class="fm-input" value="${_dlnaEsc(config.friendly_name || 'EthOS Media Server')}" style="width:200px;margin-left:8px" maxlength="64">
                </div>
                <div class="shr-form-row-inline">
                    <label class="shr-label">${t('Port')}:</label>
                    <input type="number" id="sh-dlna-port-in" class="fm-input" value="${config.port || 8200}" min="1024" max="65535" style="width:100px;margin-left:8px">
                </div>
                <label class="shr-label">${t('Katalogi z mediami')}:</label>
                ${drivesHtml ? `<div class="shr-dlna-drives">${drivesHtml}</div>` : `<div class="shr-empty-msg" style="padding:6px 0">${t('Brak wykrytych dysków')}</div>`}
                <div id="sh-dlna-custom" class="shr-dirs">${customDirs.map(d => {
                    const types = selectedMap[d] || 'AVP';
                    return `<div class="shr-dir-row">
                        <div class="shr-input-group" style="flex:1"><input type="text" class="fm-input sh-dlna-cdir" value="${_dlnaEsc(d)}" style="flex:1;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn sh-dlna-br shr-input-group-btn" title="${t('Przeglądaj')}"><i class="fas fa-folder-open"></i></button></div>
                        <span class="shr-dlna-types shr-dlna-ctypes">
                            <button class="shr-dlna-type-tag ${types.includes('V') ? 'active' : ''}" data-type="V" title="${t('Wideo')}">V</button>
                            <button class="shr-dlna-type-tag ${types.includes('A') ? 'active' : ''}" data-type="A" title="${t('Audio')}">A</button>
                            <button class="shr-dlna-type-tag ${types.includes('P') ? 'active' : ''}" data-type="P" title="${t('Zdjęcia')}">P</button>
                        </span>
                        <button class="fm-toolbar-btn btn-sm btn-red sh-dlna-rmcdir"><i class="fas fa-minus"></i></button>
                    </div>`;
                }).join('')}</div>
                <div class="shr-btn-row" style="margin-top:4px">
                    <button class="fm-toolbar-btn" id="sh-dlna-adddir"><i class="fas fa-plus"></i> ${t('Dodaj katalog')}</button>
                </div>
                <div class="shr-form-row-inline">
                    <label class="shr-dlna-toggle-label">
                        <input type="checkbox" id="sh-dlna-inotify" ${config.inotify !== false ? 'checked' : ''}>
                        inotify — ${t('wykrywaj nowe pliki automatycznie')}
                    </label>
                </div>
                <div class="shr-btn-row">
                    <button class="fm-toolbar-btn btn-green" id="sh-dlna-save"><i class="fas fa-save"></i> ${t('Zapisz')}</button>
                    <button class="fm-toolbar-btn" id="sh-dlna-rescan"><i class="fas fa-sync-alt"></i> ${t('Pełne skanowanie')}</button>
                </div>
            </div>`;

            // Type tag toggles
            w.querySelectorAll('.shr-dlna-type-tag').forEach(btn => {
                btn.onclick = () => btn.classList.toggle('active');
            });

            // Browse buttons for custom dirs
            function _bindBrowse(btn) {
                btn.onclick = () => {
                    const inp = btn.parentElement.querySelector('.sh-dlna-cdir');
                    openDirPicker(inp.value || '/home', t('Wybierz katalog mediów'), p => { inp.value = p; });
                };
            }
            w.querySelectorAll('.sh-dlna-br').forEach(_bindBrowse);

            // Remove custom dir
            w.querySelectorAll('.sh-dlna-rmcdir').forEach(b => b.onclick = () => b.closest('.shr-dir-row').remove());

            // Add custom dir
            panel.querySelector('#sh-dlna-adddir').onclick = () => {
                const row = document.createElement('div');
                row.className = 'shr-dir-row';
                row.innerHTML = `<div class="shr-input-group" style="flex:1"><input type="text" class="fm-input sh-dlna-cdir" placeholder="/home/media" style="flex:1;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn sh-dlna-br shr-input-group-btn" title="${t('Przeglądaj')}"><i class="fas fa-folder-open"></i></button></div>
                    <span class="shr-dlna-types shr-dlna-ctypes">
                        <button class="shr-dlna-type-tag active" data-type="V" title="${t('Wideo')}">V</button>
                        <button class="shr-dlna-type-tag active" data-type="A" title="${t('Audio')}">A</button>
                        <button class="shr-dlna-type-tag active" data-type="P" title="${t('Zdjęcia')}">P</button>
                    </span>
                    <button class="fm-toolbar-btn btn-sm btn-red sh-dlna-rmcdir"><i class="fas fa-minus"></i></button>`;
                _bindBrowse(row.querySelector('.sh-dlna-br'));
                row.querySelector('.sh-dlna-rmcdir').onclick = () => row.remove();
                row.querySelectorAll('.shr-dlna-type-tag').forEach(btn => { btn.onclick = () => btn.classList.toggle('active'); });
                panel.querySelector('#sh-dlna-custom').appendChild(row);
            };

            // Collect all media dirs (drives + custom) with type prefixes
            function collectMediaDirs() {
                const dirs = [];
                // Checked auto-detected drives
                w.querySelectorAll('.sh-dlna-drv-cb').forEach(cb => {
                    if (!cb.checked) return;
                    const drive = cb.dataset.drive;
                    const tc = w.querySelector(`[data-drive-types="${CSS.escape(drive)}"]`);
                    let types = '';
                    if (tc) tc.querySelectorAll('.shr-dlna-type-tag.active').forEach(t => { types += t.dataset.type; });
                    dirs.push(types && types !== 'AVP' ? `${types},${drive}` : drive);
                });
                // Custom directory entries
                panel.querySelectorAll('#sh-dlna-custom .shr-dir-row').forEach(row => {
                    const p = row.querySelector('.sh-dlna-cdir')?.value?.trim();
                    if (!p) return;
                    const tc = row.querySelector('.shr-dlna-ctypes');
                    let types = '';
                    if (tc) tc.querySelectorAll('.shr-dlna-type-tag.active').forEach(t => { types += t.dataset.type; });
                    dirs.push(types && types !== 'AVP' ? `${types},${p}` : p);
                });
                return dirs;
            }

            // Save
            panel.querySelector('#sh-dlna-save').onclick = async () => {
                const btn = panel.querySelector('#sh-dlna-save');
                btn.disabled = true;
                btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Zapisywanie…')}`;
                try {
                    const payload = {
                        friendly_name: panel.querySelector('#sh-dlna-name').value.trim() || 'EthOS Media Server',
                        port: parseInt(panel.querySelector('#sh-dlna-port-in').value, 10) || 8200,
                        media_dirs: collectMediaDirs(),
                        inotify: panel.querySelector('#sh-dlna-inotify').checked,
                    };
                    const data = await api('/dlna/config', { method: 'PUT', body: payload });
                    if (data.success) {
                        toast(t('Konfiguracja DLNA zapisana'), 'success');
                        loadStatus(); loadConfig();
                    } else {
                        toast(data.error || t('Nie udało się zapisać'), 'error');
                    }
                } catch (e) { toast(`${t('Błąd')}: ${e.message}`, 'error'); }
                finally { btn.disabled = false; btn.innerHTML = `<i class="fas fa-save"></i> ${t('Zapisz')}`; }
            };

            // Rescan
            panel.querySelector('#sh-dlna-rescan').onclick = async () => {
                const btn = panel.querySelector('#sh-dlna-rescan');
                btn.disabled = true;
                btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Skanowanie…')}`;
                try {
                    const data = await api('/dlna/rescan', { method: 'POST' });
                    if (data.success) {
                        toast(t('Skanowanie rozpoczęte'), 'success');
                        setTimeout(loadStatus, 3000);
                    } else {
                        toast(data.error || t('Skanowanie nie powiodło się'), 'error');
                    }
                } catch (e) { toast(`${t('Błąd')}: ${e.message}`, 'error'); }
                finally { btn.disabled = false; btn.innerHTML = `<i class="fas fa-sync-alt"></i> ${t('Pełne skanowanie')}`; }
            };
        }

        // Install button
        panel.querySelector('#sh-dlna-install-btn').onclick = async () => {
            const btn = panel.querySelector('#sh-dlna-install-btn');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Instalowanie…')}`;
            try {
                const data = await api('/dlna/install', { method: 'POST' });
                if (data.success) {
                    toast(t('MiniDLNA zainstalowano pomyślnie'), 'success');
                    loadStatus(); loadConfig();
                } else {
                    toast(data.error || t('Instalacja nie powiodła się'), 'error');
                }
            } catch (e) { toast(`${t('Błąd')}: ${e.message}`, 'error'); }
            finally { btn.disabled = false; btn.innerHTML = `<i class="fas fa-download"></i> ${t('Zainstaluj MiniDLNA')}`; }
        };

        panel.querySelector('#sh-dlna-ref').onclick = () => { loadStatus(); loadConfig(); };
        loadStatus();
        loadConfig();
    }

    /* ══════════════════════════════════════════
       WebDAV tab
       ══════════════════════════════════════════ */
    function renderWebDAV(panel) {
        panel.innerHTML = `
        <div class="shr-header">
            <span class="shr-title"><i class="fas fa-globe shr-icon-accent"></i>WebDAV</span>
            <span id="sh-dav-st"></span>
            <div class="shr-spacer"></div>
            <button class="fm-toolbar-btn btn-green" id="sh-dav-add"><i class="fas fa-plus"></i> ${t('Dodaj')}</button>
            <button class="fm-toolbar-btn" id="sh-dav-ref"><i class="fas fa-sync-alt"></i></button>
        </div>
        <p class="shr-desc">${t('WebDAV — dostęp do plików przez HTTP. Działa z Windows Explorer, macOS Finder, i aplikacjami mobilnymi.')}</p>
        <div id="sh-dav-list"></div>
        <div id="sh-dav-form" style="display:none"></div>`;

        async function load() {
            try {
                const [status, shares] = await Promise.all([api('/storage/webdav/status'), api('/storage/webdav/shares').catch(() => ({ shares: [], port: 8888 }))]);
                panel.querySelector('#sh-dav-st').innerHTML = _shBadge(status.running, status.running ? `Port ${shares.port}` : null);
                renderList(shares.shares || [], shares.port);
            } catch (e) { panel.querySelector('#sh-dav-list').innerHTML = `<div class="shr-error">${t('Błąd')}: ${_shEsc(e.message)}</div>`; }
        }

        function renderList(shares, port) {
            const w = panel.querySelector('#sh-dav-list');
            if (!shares.length) { w.innerHTML = `<div class="shr-empty-msg">${t('Brak udziałów WebDAV')}</div>`; return; }
            w.innerHTML = `<table class="shr-table">
                <thead><tr class="shr-thead-row">
                    <th class="shr-th">URL</th><th class="shr-th">${t('Ścieżka')}</th><th></th>
                </tr></thead><tbody>${shares.map(s => `<tr class="shr-tr">
                    <td class="shr-td-name">:${port}${_shEsc(s.url_path)}</td>
                    <td class="shr-td-sec">${_shEsc(s.fs_path)}</td>
                    <td class="shr-td-actions"><button class="fm-toolbar-btn btn-sm btn-red sh-davd" data-url="${_shEscAttr(s.url_path)}"><i class="fas fa-trash"></i></button></td>
                </tr>`).join('')}</tbody></table>`;
            w.querySelectorAll('.sh-davd').forEach(b => b.onclick = async () => {
                if (!await confirmDialog(t('Usunąć udział WebDAV'), t('Usunąć udział WebDAV?'))) return;
                await api('/storage/webdav/share', { method: 'DELETE', body: { url_path: b.dataset.url } }); toast(t('Usunięto'), 'success'); load();
            });
        }

        function showForm() {
            const f = panel.querySelector('#sh-dav-form');
            f.style.display = '';
            f.innerHTML = `<div class="shr-form-section">
                <h4 class="shr-form-title">${t('Nowy udział WebDAV')}</h4>
                <div class="shr-form-row">
                    <div class="shr-input-group"><input type="text" id="sh-dv-p" class="fm-input" placeholder="${t('Ścieżka np. /home/share')}" style="width:200px;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn shr-input-group-btn" id="sh-dv-browse" title="${t('Przeglądaj')}"><i class="fas fa-folder-open"></i></button></div>
                    <input type="text" id="sh-dv-u" class="fm-input" placeholder="${t('URL np. /share')}" style="width:140px">
                    <input type="text" id="sh-dv-un" class="fm-input" placeholder="${t('Login (opcja)')}" style="width:120px">
                    <input type="password" id="sh-dv-pw" class="fm-input" placeholder="${t('Hasło (opcja)')}" style="width:120px">
                    <button class="fm-toolbar-btn btn-green" id="sh-dv-ok"><i class="fas fa-save"></i></button>
                    <button class="fm-toolbar-btn" id="sh-dv-x"><i class="fas fa-times"></i></button>
                </div></div>`;
            panel.querySelector('#sh-dv-browse').onclick = () => openDirPicker('/home', t('Wybierz folder WebDAV'), p => { panel.querySelector('#sh-dv-p').value = p; });
            panel.querySelector('#sh-dv-ok').onclick = async () => {
                const p = panel.querySelector('#sh-dv-p').value.trim();
                if (!p) { toast(t('Podaj ścieżkę'), 'warning'); return; }
                await api('/storage/webdav/share', { method: 'POST', body: {
                    path: p,
                    url_path: panel.querySelector('#sh-dv-u').value.trim(),
                    username: panel.querySelector('#sh-dv-un').value.trim(),
                    password: panel.querySelector('#sh-dv-pw').value,
                }});
                toast(t('Dodano'), 'success'); f.style.display = 'none'; load();
            };
            panel.querySelector('#sh-dv-x').onclick = () => { f.style.display = 'none'; };
        }

        panel.querySelector('#sh-dav-add').onclick = () => showForm();
        panel.querySelector('#sh-dav-ref').onclick = () => load();
        load();
    }

    /* ══════════════════════════════════════════
       SFTP tab
       ══════════════════════════════════════════ */
    function renderSFTP(panel) {
        panel.innerHTML = `
        <div class="shr-header">
            <span class="shr-title"><i class="fas fa-lock shr-icon-accent"></i>SFTP</span>
            <span id="sh-sftp-st"></span>
            <div class="shr-spacer"></div>
            <button class="fm-toolbar-btn" id="sh-sftp-ref"><i class="fas fa-sync-alt"></i></button>
        </div>
        <p class="shr-desc">${t('SFTP — bezpieczny transfer plików przez SSH. Szyfrowany, nie wymaga dodatkowych usług.')}</p>
        <div id="sh-sftp-toggle" style="margin-bottom:14px"></div>
        <div id="sh-sftp-users"></div>`;

        async function load() {
            try {
                const [status, users] = await Promise.all([api('/storage/sftp/status'), api('/storage/sftp/users')]);
                panel.querySelector('#sh-sftp-st').innerHTML = _shBadge(status.running && status.sftp_enabled);

                const toggleEl = panel.querySelector('#sh-sftp-toggle');
                toggleEl.innerHTML = `<label class="shr-toggle">
                    <input type="checkbox" id="sh-sftp-en" ${status.sftp_enabled ? 'checked' : ''}>
                    <span class="shr-toggle-text">${t('SFTP włączony')}</span>
                </label>`;
                panel.querySelector('#sh-sftp-en').onchange = async (e) => {
                    await api('/storage/sftp/toggle', { method: 'POST', body: { enable: e.target.checked } });
                    toast(e.target.checked ? t('SFTP włączony') : t('SFTP wyłączony'), 'success'); load();
                };

                const w = panel.querySelector('#sh-sftp-users');
                const ul = users.users || [];
                if (!ul.length) { w.innerHTML = `<div class="shr-muted">${t('Brak użytkowników')}</div>`; return; }
                w.innerHTML = `<label class="shr-label">${t('Użytkownicy z dostępem SFTP')}:</label>
                <table class="shr-table" style="margin-top:6px">
                    <thead><tr class="shr-thead-row">
                        <th class="shr-th">${t('Użytkownik')}</th><th class="shr-th">${t('Katalog domowy')}</th>
                    </tr></thead><tbody>${ul.map(u => `<tr class="shr-tr">
                        <td class="shr-td-name">${_shEsc(u.username)}</td>
                        <td class="shr-td-sec">${_shEsc(u.home)}</td>
                    </tr>`).join('')}</tbody></table>`;
            } catch (e) { panel.querySelector('#sh-sftp-users').innerHTML = `<div class="shr-error">${t('Błąd')}: ${_shEsc(e.message)}</div>`; }
        }

        panel.querySelector('#sh-sftp-ref').onclick = () => load();
        load();
    }

    /* ══════════════════════════════════════════
       FTP tab
       ══════════════════════════════════════════ */
    function renderFTP(panel) {
        panel.innerHTML = `
        <div class="shr-header">
            <span class="shr-title"><i class="fas fa-upload shr-icon-accent"></i>FTP</span>
            <span id="sh-ftp-st"></span>
            <div class="shr-spacer"></div>
            <button class="fm-toolbar-btn" id="sh-ftp-ref"><i class="fas fa-sync-alt"></i></button>
        </div>
        <p class="shr-desc">${t('FTP — klasyczny protokół transferu. Dla starszych urządzeń i kamer IP. Użyj SFTP jeśli możliwe.')}</p>
        <div id="sh-ftp-ctl"></div>`;

        async function load() {
            try {
                const status = await api('/storage/ftp/status');
                const el = panel.querySelector('#sh-ftp-st');
                const ctl = panel.querySelector('#sh-ftp-ctl');

                el.innerHTML = _shBadge(status.running);
                ctl.innerHTML = `<label class="shr-toggle">
                    <input type="checkbox" id="sh-ftp-en" ${status.running ? 'checked' : ''}>
                    <span class="shr-toggle-text">${t('Serwer FTP włączony')}</span>
                </label>
                <p class="shr-note">${t('Użytkownicy systemowi logują się do swoich katalogów domowych.')}</p>`;
                panel.querySelector('#sh-ftp-en').onchange = async (e) => {
                    await api('/storage/ftp/toggle', { method: 'POST', body: { enable: e.target.checked } });
                    toast(e.target.checked ? t('FTP włączony') : t('FTP wyłączony'), 'success'); load();
                };
            } catch (e) { panel.querySelector('#sh-ftp-ctl').innerHTML = `<div class="shr-error">${t('Błąd')}: ${_shEsc(e.message)}</div>`; }
        }

        panel.querySelector('#sh-ftp-ref').onclick = () => load();
        load();
    }
}
