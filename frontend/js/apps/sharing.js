/* ═══════════════════════════════════════════════════════════
   EthOS — Udostępnianie (Multi-protocol file sharing)
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
                    <td class="shr-td-name">${s.name}</td>
                    <td class="shr-td-sec">${s.path}</td>
                    <td class="shr-td-center">${s.guest_ok ? '<i class="fas fa-check shr-check-ok"></i>' : '<i class="fas fa-times shr-check-no"></i>'}</td>
                    <td class="shr-td-center">${s.writable ? '<i class="fas fa-check shr-check-ok"></i>' : '<i class="fas fa-times shr-check-no"></i>'}</td>
                    <td class="shr-td-actions">
                        <button class="fm-toolbar-btn btn-sm sh-sme" data-name="${s.name}" data-path="${s.path}" data-guest="${s.guest_ok}" data-writable="${s.writable}"><i class="fas fa-pen"></i></button>
                        <button class="fm-toolbar-btn btn-sm btn-red sh-smd" data-name="${s.name}"><i class="fas fa-trash"></i></button>
                    </td></tr>`).join('')}</tbody></table>`;
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
            } catch (e) { panel.querySelector('#sh-nfs-list').innerHTML = `<div class="shr-error">${e.message}</div>`; }
        }

        function renderList(exports) {
            const w = panel.querySelector('#sh-nfs-list');
            if (!exports.length) { w.innerHTML = `<div class="shr-empty-msg">${t('Brak eksportów NFS')}</div>`; return; }
            w.innerHTML = `<table class="shr-table">
                <thead><tr class="shr-thead-row">
                    <th class="shr-th">${t('Ścieżka')}</th><th class="shr-th">${t('Klienci / opcje')}</th><th></th>
                </tr></thead><tbody>${exports.map(e => `<tr class="shr-tr">
                    <td class="shr-td-name">${e.path}</td>
                    <td class="shr-td-sec">${e.clients}</td>
                    <td class="shr-td-actions"><button class="fm-toolbar-btn btn-sm btn-red sh-nfsd" data-path="${e.path}"><i class="fas fa-trash"></i></button></td>
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
                    <div class="shr-input-group"><input type="text" id="sh-nf-p" class="fm-input" placeholder="${t('Ścieżka np. /home/media')}" style="width:200px;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn shr-input-group-btn" id="sh-nf-browse" title="Przeglądaj"><i class="fas fa-folder-open"></i></button></div>
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
            <button class="fm-toolbar-btn" id="sh-dlna-rescan" title="${t('Reskan')}"><i class="fas fa-sync-alt"></i> ${t('Reskan')}</button>
            <button class="fm-toolbar-btn" id="sh-dlna-ref"><i class="fas fa-sync-alt"></i></button>
        </div>
        <p class="shr-desc">${t('DLNA streamuje media (filmy, muzykę, zdjęcia) do Smart TV, konsol i innych urządzeń w sieci.')}</p>
        <div id="sh-dlna-cfg"></div>`;

        async function load() {
            try {
                const [status, config] = await Promise.all([api('/storage/dlna/status'), api('/storage/dlna/config').catch(() => ({ dirs: [], friendly_name: 'EthOS' }))]);
                panel.querySelector('#sh-dlna-st').innerHTML = _shBadge(status.running);
                renderCfg(config);
            } catch (e) { panel.querySelector('#sh-dlna-cfg').innerHTML = `<div class="shr-error">${e.message}</div>`; }
        }

        function renderCfg(config) {
            const w = panel.querySelector('#sh-dlna-cfg');
            const dirs = config.dirs || [];
            w.innerHTML = `
            <div style="margin-bottom:10px">
                <label class="shr-label">${t('Nazwa urządzenia')}:</label>
                <input type="text" id="sh-dlna-name" class="fm-input" value="${config.friendly_name || 'EthOS'}" style="width:200px;margin-left:8px">
            </div>
            <label class="shr-label">${t('Katalogi z mediami')}:</label>
            <div id="sh-dlna-dirs" class="shr-dirs">${dirs.map((d, i) => `<div class="shr-dir-row">
                <div class="shr-input-group" style="flex:1"><input type="text" class="fm-input sh-dlna-dir" value="${d}" style="flex:1;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn sh-dlna-br shr-input-group-btn" title="Przeglądaj"><i class="fas fa-folder-open"></i></button></div>
                <button class="fm-toolbar-btn btn-sm btn-red sh-dlna-rm" data-i="${i}"><i class="fas fa-minus"></i></button>
            </div>`).join('')}</div>
            <div class="shr-btn-row">
                <button class="fm-toolbar-btn" id="sh-dlna-adddir"><i class="fas fa-plus"></i> ${t('Dodaj katalog')}</button>
                <button class="fm-toolbar-btn btn-green" id="sh-dlna-save"><i class="fas fa-save"></i> ${t('Zapisz')}</button>
            </div>`;

            function _dlnaBrowseBind(btn) {
                btn.onclick = () => {
                    const inp = btn.parentElement.querySelector('.sh-dlna-dir');
                    openDirPicker(inp.value || '/home', t('Wybierz katalog mediów'), p => { inp.value = p; });
                };
            }
            w.querySelectorAll('.sh-dlna-br').forEach(_dlnaBrowseBind);

            panel.querySelector('#sh-dlna-adddir').onclick = () => {
                const d = document.createElement('div');
                d.className = 'shr-dir-row';
                d.innerHTML = `<div class="shr-input-group" style="flex:1"><input type="text" class="fm-input sh-dlna-dir" placeholder="/home/media" style="flex:1;border-radius:6px 0 0 6px" readonly><button class="fm-toolbar-btn sh-dlna-br shr-input-group-btn" title="Przeglądaj"><i class="fas fa-folder-open"></i></button></div><button class="fm-toolbar-btn btn-sm btn-red sh-dlna-rmx"><i class="fas fa-minus"></i></button>`;
                _dlnaBrowseBind(d.querySelector('.sh-dlna-br'));
                d.querySelector('.sh-dlna-rmx').onclick = () => d.remove();
                panel.querySelector('#sh-dlna-dirs').appendChild(d);
            };
            w.querySelectorAll('.sh-dlna-rm').forEach(b => b.onclick = () => b.parentElement.remove());
            panel.querySelector('#sh-dlna-save').onclick = async () => {
                const ds = [...panel.querySelectorAll('.sh-dlna-dir')].map(i => i.value.trim()).filter(Boolean);
                const name = panel.querySelector('#sh-dlna-name').value.trim() || 'EthOS';
                await api('/storage/dlna/config', { method: 'POST', body: { dirs: ds, friendly_name: name } });
                toast(t('Zapisano i zrestartowano DLNA'), 'success'); load();
            };
        }

        panel.querySelector('#sh-dlna-rescan').onclick = async () => { await api('/storage/dlna/rescan', { method: 'POST' }); toast(t('Reskan rozpoczęty'), 'success'); };
        panel.querySelector('#sh-dlna-ref').onclick = () => load();
        load();
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
            } catch (e) { panel.querySelector('#sh-dav-list').innerHTML = `<div class="shr-error">${e.message}</div>`; }
        }

        function renderList(shares, port) {
            const w = panel.querySelector('#sh-dav-list');
            if (!shares.length) { w.innerHTML = `<div class="shr-empty-msg">${t('Brak udziałów WebDAV')}</div>`; return; }
            w.innerHTML = `<table class="shr-table">
                <thead><tr class="shr-thead-row">
                    <th class="shr-th">URL</th><th class="shr-th">${t('Ścieżka')}</th><th></th>
                </tr></thead><tbody>${shares.map(s => `<tr class="shr-tr">
                    <td class="shr-td-name">:${port}${s.url_path}</td>
                    <td class="shr-td-sec">${s.fs_path}</td>
                    <td class="shr-td-actions"><button class="fm-toolbar-btn btn-sm btn-red sh-davd" data-url="${s.url_path}"><i class="fas fa-trash"></i></button></td>
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
                        <td class="shr-td-name">${u.username}</td>
                        <td class="shr-td-sec">${u.home}</td>
                    </tr>`).join('')}</tbody></table>`;
            } catch (e) { panel.querySelector('#sh-sftp-users').innerHTML = `<div class="shr-error">${e.message}</div>`; }
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
            } catch (e) { panel.querySelector('#sh-ftp-ctl').innerHTML = `<div class="shr-error">${e.message}</div>`; }
        }

        panel.querySelector('#sh-ftp-ref').onclick = () => load();
        load();
    }
}
