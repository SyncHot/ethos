/* ═══════════════════════════════════════════════════════════
   EthOS — Storage Manager: Sharing
   (Samba, NFS, WebDAV, SFTP, FTP, DLNA)
   ═══════════════════════════════════════════════════════════ */


/* ═══════════════════════════════════════════════════════════
   Section: Sharing (from original sharing.js)
   ═══════════════════════════════════════════════════════════ */
async function _smSharing(el) {
    const body = el;
    const $ = (s) => body.querySelector(s);

    /* Show loading state */
    body.innerHTML = `<div class="shr-loading"><i class="fas fa-spinner fa-spin shr-spin-icon"></i>${t('Ładowanie…')}</div>`;

    /* Fetch installed packages to determine which tabs to show */
    let installedProtos;
    let installedPkgIds = new Set();
    try {
        const pkgs = await api('/ethos-packages');
        installedPkgIds = new Set(pkgs.filter(p => p.installed).map(p => p.id));
        installedProtos = _SH_ALL_PROTOS.filter(p => installedPkgIds.has(p.pkgId));
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
            <button class="sh-tab shr-tab-btn" data-tab="easy-share"><i class="fas fa-magic shr-tab-icon" style="color:var(--accent)"></i><span>${t('Szybki udział')}</span></button>
            <div class="esh-sidebar-sep"></div>
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
        const render = { 'easy-share': (p) => renderEasyShare(p, installedPkgIds), samba: renderSamba, nfs: renderNFS, dlna: renderDLNA, webdav: renderWebDAV, sftp: renderSFTP, ftp: renderFTP };
        (render[id] || render['easy-share'])($('#sh-panel'));
    }

    body.querySelectorAll('.sh-tab').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
    switchTab('easy-share');

    body.querySelectorAll('.sh-tab').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
    switchTab('easy-share');

    /* ══════════════════════════════════════════
       Easy Share Wizard
       ══════════════════════════════════════════ */
    function renderEasyShare(panel, pkgIds) {
        let step = 0;
        const d = { name: '', path: '', devices: new Set(), guest: true, writable: true };
        const host = window.location.hostname;

        const DEVS = [
            { id: 'windows', icon: 'fab fa-windows',    name: 'Windows',        proto: 'Samba (SMB)', pkg: 'sharing-samba',  color: '#0078d4' },
            { id: 'mac',     icon: 'fab fa-apple',      name: 'macOS',          proto: 'Samba (SMB)', pkg: 'sharing-samba',  color: '#888' },
            { id: 'linux',   icon: 'fab fa-linux',      name: 'Linux',          proto: 'NFS',         pkg: 'sharing-nfs',    color: '#e95420' },
            { id: 'mobile',  icon: 'fas fa-mobile-alt', name: t('Telefon'),     proto: 'WebDAV',      pkg: 'sharing-webdav', color: '#10b981' },
            { id: 'tv',      icon: 'fas fa-tv',         name: 'Smart TV',       proto: 'DLNA',        pkg: 'sharing-dlna',   color: '#ef4444' },
        ];

        function stepBar(cur) {
            const labels = [t('Folder'), t('Urządzenia'), t('Dostęp'), t('Gotowe')];
            return `<div class="esh-step-bar">${labels.map((l, i) =>
                `<div class="esh-step ${i === cur ? 'esh-step-active' : ''} ${i < cur ? 'esh-step-done' : ''}">` +
                `<span class="esh-step-num">${i < cur ? '✓' : i + 1}</span><span class="esh-step-label">${l}</span></div>`
            ).join('<div class="esh-step-line"></div>')}</div>`;
        }

        function nav(s, last) {
            let h = '';
            if (s > 0) h += `<button class="fm-toolbar-btn" id="esh-back"><i class="fas fa-arrow-left"></i> ${t('Wstecz')}</button>`;
            h += '<div style="flex:1"></div>';
            h += last
                ? `<button class="fm-toolbar-btn btn-green" id="esh-create"><i class="fas fa-check"></i> ${t('Utwórz udział')}</button>`
                : `<button class="fm-toolbar-btn btn-green" id="esh-next">${t('Dalej')} <i class="fas fa-arrow-right"></i></button>`;
            return `<div class="esh-nav">${h}</div>`;
        }

        function render() { [renderStep0, renderStep1, renderStep2, renderStep3][step](); }

        /* ── Step 0: Name + Folder ── */
        function renderStep0() {
            panel.innerHTML = `${stepBar(0)}<div class="esh-content">
                <h3 class="esh-title">${t('Co chcesz udostępnić?')}</h3>
                <p class="esh-subtitle">${t('Nadaj nazwę i wybierz folder, który ma być widoczny w sieci.')}</p>
                <label class="esh-label">${t('Nazwa udziału')}</label>
                <input type="text" id="esh-name" class="fm-input" value="${_shEscAttr(d.name)}" placeholder="${t('np. Filmy, Zdjęcia, Dokumenty')}" style="max-width:300px">
                <label class="esh-label" style="margin-top:14px">${t('Folder do udostępnienia')}</label>
                <div class="shr-input-group" style="max-width:400px">
                    <input type="text" id="esh-path" class="fm-input" value="${_shEscAttr(d.path)}" placeholder="/home/media" readonly style="border-radius:6px 0 0 6px">
                    <button class="fm-toolbar-btn shr-input-group-btn" id="esh-browse"><i class="fas fa-folder-open"></i></button>
                </div></div>${nav(0)}`;
            panel.querySelector('#esh-browse').onclick = () => {
                openDirPicker(d.path || '/home', t('Wybierz folder'), p => {
                    d.path = p;
                    panel.querySelector('#esh-path').value = p;
                    if (!d.name) {
                        const auto = p.split('/').pop() || '';
                        d.name = auto.charAt(0).toUpperCase() + auto.slice(1);
                        panel.querySelector('#esh-name').value = d.name;
                    }
                });
            };
            panel.querySelector('#esh-next').onclick = () => {
                d.name = panel.querySelector('#esh-name').value.trim();
                if (!d.name || !d.path) { toast(t('Podaj nazwę i wybierz folder'), 'warning'); return; }
                step = 1; render();
            };
        }

        /* ── Step 1: Device selection ── */
        function renderStep1() {
            panel.innerHTML = `${stepBar(1)}<div class="esh-content">
                <h3 class="esh-title">${t('Z jakich urządzeń będziesz korzystać?')}</h3>
                <p class="esh-subtitle">${t('Wybierz urządzenia, które mają widzieć ten folder w sieci.')}</p>
                <div class="esh-devices">${DEVS.map(v => {
                    const ok = pkgIds.has(v.pkg);
                    const sel = d.devices.has(v.id);
                    return `<div class="esh-device-card ${sel ? 'esh-selected' : ''} ${!ok ? 'esh-disabled' : ''}" data-id="${v.id}">
                        <i class="${v.icon} esh-device-icon" style="color:${v.color}"></i>
                        <span class="esh-device-name">${v.name}</span>
                        <span class="esh-device-proto">${v.proto}</span>
                        ${!ok ? `<span class="esh-device-missing">${t('Zainstaluj w App Store')}</span>` : ''}
                    </div>`;
                }).join('')}</div></div>${nav(1)}`;
            panel.querySelectorAll('.esh-device-card:not(.esh-disabled)').forEach(c => {
                c.onclick = () => {
                    const id = c.dataset.id;
                    if (d.devices.has(id)) { d.devices.delete(id); c.classList.remove('esh-selected'); }
                    else { d.devices.add(id); c.classList.add('esh-selected'); }
                };
            });
            panel.querySelector('#esh-back').onclick = () => { step = 0; render(); };
            panel.querySelector('#esh-next').onclick = () => {
                if (!d.devices.size) { toast(t('Wybierz przynajmniej jedno urządzenie'), 'warning'); return; }
                step = 2; render();
            };
        }

        /* ── Step 2: Access settings ── */
        function renderStep2() {
            panel.innerHTML = `${stepBar(2)}<div class="esh-content">
                <h3 class="esh-title">${t('Kto i jak ma mieć dostęp?')}</h3>
                <div class="esh-access-options">
                    <label class="esh-access-card">
                        <input type="checkbox" id="esh-guest" ${d.guest ? 'checked' : ''}>
                        <div class="esh-access-info">
                            <span class="esh-access-name"><i class="fas fa-unlock" style="color:var(--accent)"></i> ${t('Dostęp bez hasła')}</span>
                            <span class="esh-access-desc">${t('Każdy w sieci domowej może przeglądać pliki')}</span>
                        </div>
                    </label>
                    <label class="esh-access-card">
                        <input type="checkbox" id="esh-write" ${d.writable ? 'checked' : ''}>
                        <div class="esh-access-info">
                            <span class="esh-access-name"><i class="fas fa-pen" style="color:var(--warning)"></i> ${t('Pozwól na zapis')}</span>
                            <span class="esh-access-desc">${t('Użytkownicy mogą dodawać, edytować i usuwać pliki')}</span>
                        </div>
                    </label>
                </div></div>${nav(2, true)}`;
            panel.querySelector('#esh-back').onclick = () => { step = 1; render(); };
            panel.querySelector('#esh-create').onclick = async () => {
                d.guest = panel.querySelector('#esh-guest').checked;
                d.writable = panel.querySelector('#esh-write').checked;
                step = 3; render();
                await doCreate();
            };
        }

        /* ── Step 3: Creating + Results ── */
        function renderStep3() {
            panel.innerHTML = `${stepBar(3)}<div class="esh-content">
                <div class="esh-creating" id="esh-spinner"><i class="fas fa-spinner fa-spin"></i> ${t('Tworzenie udziałów...')}</div>
                <div id="esh-results"></div></div>
                <div class="esh-nav"><div style="flex:1"></div>
                    <button class="fm-toolbar-btn btn-green" id="esh-done" style="display:none"><i class="fas fa-check"></i> ${t('Gotowe')}</button>
                </div>`;
            panel.querySelector('#esh-done').onclick = () => { step = 0; d.name = ''; d.path = ''; d.devices.clear(); render(); };
            panel.addEventListener('click', e => {
                const btn = e.target.closest('.esh-copy-btn');
                if (btn && btn.dataset.url) navigator.clipboard.writeText(btn.dataset.url).then(() => toast(t('Skopiowano'), 'success'));
            });
        }

        async function doCreate() {
            const res = {};
            const tasks = [];
            const smb = d.devices.has('windows') || d.devices.has('mac');
            const nfs = d.devices.has('linux');
            const dlna = d.devices.has('tv');
            const dav = d.devices.has('mobile');

            if (smb) tasks.push((async () => {
                try {
                    const r = await api('/storage/samba/share', { method: 'POST', body: { name: d.name, path: d.path, guest_ok: d.guest, writable: d.writable } });
                    res.samba = !r.error; res.sambaErr = r.error;
                } catch (e) { res.samba = false; res.sambaErr = e.message; }
            })());

            if (nfs) tasks.push((async () => {
                try {
                    const opts = d.writable ? 'rw,async,no_subtree_check,no_root_squash,insecure' : 'ro,async,no_subtree_check,insecure';
                    const r = await api('/storage/nfs/export', { method: 'POST', body: { path: d.path, options: opts } });
                    res.nfs = !r.error; res.nfsErr = r.error;
                } catch (e) { res.nfs = false; res.nfsErr = e.message; }
            })());

            if (dlna) tasks.push((async () => {
                try {
                    const cfg = await api('/storage/dlna/config');
                    const dirs = (cfg.dirs || []).map(x => typeof x === 'string' ? x : x.path);
                    if (!dirs.includes(d.path)) dirs.push(d.path);
                    const r = await api('/storage/dlna/config', { method: 'POST', body: { dirs, friendly_name: cfg.friendly_name || 'EthOS Media' } });
                    res.dlna = !r.error; res.dlnaErr = r.error;
                } catch (e) { res.dlna = false; res.dlnaErr = e.message; }
            })());

            if (dav) tasks.push((async () => {
                try {
                    const urlPath = '/' + d.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
                    const r = await api('/storage/webdav/share', { method: 'POST', body: { path: d.path, url_path: urlPath } });
                    res.webdav = !r.error; res.webdavErr = r.error; res.webdavPort = r.port || 8888; res.webdavUrl = urlPath;
                } catch (e) { res.webdav = false; res.webdavErr = e.message; }
            })());

            await Promise.all(tasks);
            showResults(res, smb, nfs, dlna, dav);
        }

        function connCard(icon, label, ok, err, steps, url) {
            return `<div class="esh-conn-card ${ok ? '' : 'esh-conn-err'}">
                <div class="esh-conn-header"><i class="${icon}"></i> ${label} ${ok ? '<i class="fas fa-check-circle" style="color:var(--success);font-size:12px"></i>' : '<i class="fas fa-times-circle" style="color:var(--danger);font-size:12px"></i>'}</div>
                ${ok ? `<div class="esh-conn-steps">
                    ${steps.map((s, i) => `<p class="esh-conn-step"><span class="esh-conn-num">${i + 1}</span> ${s}</p>`).join('')}
                    ${url ? `<div class="esh-conn-url"><code>${_shEsc(url)}</code>
                        <button class="esh-copy-btn" data-url="${_shEscAttr(url)}" title="${t('Kopiuj')}"><i class="fas fa-copy"></i></button></div>` : ''}
                </div>` : `<p class="esh-conn-step" style="color:var(--danger)"><i class="fas fa-exclamation-triangle"></i> ${_shEsc(err || t('Błąd'))}</p>`}
            </div>`;
        }

        function showResults(res, smb, nfs, dlna, dav) {
            const el = panel.querySelector('#esh-results');
            const spinner = panel.querySelector('#esh-spinner');
            if (spinner) spinner.style.display = 'none';
            panel.querySelector('#esh-done').style.display = '';

            let html = `<div style="text-align:center;padding:8px 0"><i class="fas fa-check-circle esh-done-icon"></i>
                <h3 class="esh-title">${t('Udział')} "${_shEsc(d.name)}" ${t('utworzony!')}</h3>
                <p class="esh-subtitle">${t('Połącz się z dowolnego urządzenia:')}</p></div>`;

            if (smb) {
                const smbName = d.name;
                if (d.devices.has('windows')) {
                    const winUrl = `\\\\${host}\\${smbName}`;
                    html += connCard('fab fa-windows', 'Windows', res.samba, res.sambaErr, [
                        t('Otwórz Eksplorator plików') + ' (Win+E)',
                        t('W pasku adresu wpisz:'),
                    ], winUrl);
                }
                if (d.devices.has('mac')) {
                    const macUrl = `smb://${host}/${smbName}`;
                    html += connCard('fab fa-apple', 'macOS', res.samba, res.sambaErr, [
                        'Finder → ' + t('Idź') + ' → ' + t('Połącz z serwerem') + ' (⌘K)',
                        t('Wpisz adres:'),
                    ], macUrl);
                }
            }

            if (nfs) {
                const nfsUrl = `${host}:${d.path}`;
                html += connCard('fab fa-linux', 'Linux', res.nfs, res.nfsErr, [
                    t('W terminalu wpisz:'),
                ], `sudo mount -t nfs ${nfsUrl} /mnt/share`);
            }

            if (dav) {
                const davUrl = `http://${host}:${res.webdavPort || 8888}${res.webdavUrl || '/' + d.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
                html += connCard('fas fa-mobile-alt', t('Telefon / Tablet'), res.webdav, res.webdavErr, [
                    t('Zainstaluj aplikację WebDAV (np. Solid Explorer, Documents)'),
                    t('Dodaj serwer z adresem:'),
                ], davUrl);
            }

            if (dlna) {
                html += `<div class="esh-conn-card ${res.dlna ? '' : 'esh-conn-err'}">
                    <div class="esh-conn-header"><i class="fas fa-tv"></i> Smart TV / DLNA ${res.dlna ? '<i class="fas fa-check-circle" style="color:var(--success);font-size:12px"></i>' : ''}</div>
                    ${res.dlna
                        ? `<p class="esh-conn-step"><span class="esh-conn-num">1</span> ${t('Otwórz aplikację multimedialną na TV (np. „Media", „DLNA")')}</p>
                           <p class="esh-conn-step"><span class="esh-conn-num">2</span> ${t('Serwer EthOS pojawi się automatycznie')}</p>`
                        : `<p class="esh-conn-step" style="color:var(--danger)"><i class="fas fa-exclamation-triangle"></i> ${_shEsc(res.dlnaErr || t('Błąd'))}</p>`}
                </div>`;
            }

            el.innerHTML = html;
        }

        render();
    }

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
        <div id="sh-wsdd-section"></div>`;

        async function load() {
            try {
                const [shares, status, wsdd] = await Promise.all([
                    api('/storage/samba/shares'),
                    api('/storage/samba/status'),
                    api('/storage/samba/wsdd/status').catch(() => null),
                ]);
                st.shares = shares || [];
                panel.querySelector('#sh-smb-status').innerHTML = _shBadge(status.running);
                renderList();
                renderWsdd(wsdd);
            } catch (e) { panel.querySelector('#sh-smb-list').innerHTML = `<div class="shr-error">${t('Błąd')}: ${e.message}</div>`; }
        }

        function renderWsdd(wsdd) {
            const sec = panel.querySelector('#sh-wsdd-section');
            if (!sec) return;
            const running = wsdd && wsdd.running;
            sec.innerHTML = `
                <div class="shr-pw-section">
                    <h4 class="shr-form-title"><i class="fas fa-broadcast-tower shr-icon-accent"></i> ${t('Wykrywanie w sieci Windows')}</h4>
                    <label class="shr-toggle">
                        <input type="checkbox" id="sh-wsdd-en" ${running ? 'checked' : ''}>
                        <span class="shr-toggle-text">WS-Discovery</span>
                    </label>
                    <p class="shr-note">${t('Pozwala Windows 10/11 automatycznie wykryć ten serwer w Eksploratorze plików → Sieć.')}</p>
                </div>`;
            const cb = sec.querySelector('#sh-wsdd-en');
            cb.onchange = async (e) => {
                cb.disabled = true;
                try {
                    const r = await api('/storage/samba/wsdd/toggle', { method: 'POST', body: { enable: e.target.checked } });
                    if (r.error) { toast(r.error, 'error'); cb.checked = !cb.checked; return; }
                    toast(e.target.checked ? t('WS-Discovery włączone') : t('WS-Discovery wyłączone'), 'success');
                    load();
                } catch (err) {
                    toast(err.message, 'error'); cb.checked = !cb.checked;
                } finally { cb.disabled = false; }
            };
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
    return null;
}

/* ═══════════════════════════════════════════════════════════
   Section: Diagnostics (from original diskrepair.js)
   ═══════════════════════════════════════════════════════════ */

function _sto_escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _smDiagnostics(el) {
    const body = el;
    const escHtml = _sto_escHtml;
    const state = {
        logOffset: 0,
        smartData: null,
        fsDetail: null,
        history: [],
    };

    body.innerHTML = `
    <style>
        .dr-wrap { display:flex; height:100%; overflow:hidden; }

        /* ── Sidebar ── */
        .dr-sidebar { width:250px; min-width:250px; background:var(--bg-secondary); border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden; }
        .dr-sidebar-header { display:flex; align-items:center; justify-content:space-between; padding:12px 14px; border-bottom:1px solid var(--border); }
        .dr-sidebar-header span { font-weight:600; font-size:13px; color:var(--text-primary); }
        .dr-disk-list { flex:1; overflow-y:auto; padding:6px; }
        .dr-disk-item { display:flex; align-items:center; gap:10px; padding:10px; border-radius:8px; cursor:pointer; margin-bottom:4px; transition:background .15s; }
        .dr-disk-item:hover { background:var(--bg-card); }
        .dr-disk-item.active { background:var(--accent); color:#fff; }
        .dr-disk-item.active .dr-disk-model { color:#fff; }
        .dr-disk-item.active .dr-disk-size { color:rgba(255,255,255,.7); }
        .dr-disk-item.active .dr-temp-badge { background:rgba(255,255,255,.2); color:#fff; }
        .dr-disk-icon { font-size:22px; width:28px; text-align:center; flex-shrink:0; }
        .dr-disk-meta { flex:1; min-width:0; }
        .dr-disk-model { font-size:12px; font-weight:600; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .dr-disk-size { font-size:11px; color:var(--text-muted); }
        .dr-health-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
        .dr-temp-badge { font-size:10px; padding:2px 6px; border-radius:4px; background:var(--bg-primary); color:var(--text-muted); flex-shrink:0; }

        /* ── Main ── */
        .dr-main { flex:1; display:flex; flex-direction:column; overflow:hidden; }
        .dr-banner { display:none; padding:10px 16px; background:var(--bg-card); border-bottom:1px solid var(--border); }
        .dr-banner.active { display:flex; align-items:center; gap:12px; }
        .dr-banner-info { flex:1; min-width:0; }
        .dr-banner-title { font-size:12px; font-weight:600; color:var(--text-primary); }
        .dr-banner-progress { height:8px; background:var(--bg-primary); border-radius:4px; overflow:hidden; margin-top:6px; }
        .dr-banner-bar { height:100%; background:linear-gradient(90deg, var(--accent), #6366f1); border-radius:4px; transition:width .3s; width:0%; }
        .dr-banner-detail { font-size:11px; color:var(--text-muted); margin-top:4px; }

        /* ── Tabs ── */
        .dr-tabs { display:flex; border-bottom:1px solid var(--border); padding:0 16px; background:var(--bg-secondary); flex-shrink:0; }
        .dr-tab { padding:10px 16px; font-size:12px; font-weight:500; color:var(--text-muted); cursor:pointer; border-bottom:2px solid transparent; transition:color .15s, border-color .15s; white-space:nowrap; }
        .dr-tab:hover { color:var(--text-primary); }
        .dr-tab.active { color:var(--accent); border-bottom-color:var(--accent); }

        /* ── Tab content ── */
        .dr-content { flex:1; overflow-y:auto; padding:16px; }
        .dr-empty { display:flex; align-items:center; justify-content:center; height:100%; color:var(--text-muted); font-size:14px; flex-direction:column; gap:8px; }
        .dr-empty i { font-size:40px; opacity:.4; }

        .dr-card { background:var(--bg-card); border-radius:10px; padding:16px; margin-bottom:14px; }
        .dr-card-title { font-weight:600; font-size:13px; color:var(--text-primary); margin-bottom:10px; display:flex; align-items:center; gap:8px; }
        .dr-card-title i { width:18px; text-align:center; }

        .dr-table { width:100%; border-collapse:collapse; font-size:12px; }
        .dr-table th { text-align:left; font-weight:600; padding:8px 10px; border-bottom:2px solid var(--border); color:var(--text-secondary); font-size:11px; text-transform:uppercase; letter-spacing:.3px; }
        .dr-table td { padding:7px 10px; border-bottom:1px solid var(--border); color:var(--text-primary); }
        .dr-table tr:last-child td { border-bottom:none; }

        .dr-kv { display:grid; grid-template-columns:140px 1fr; gap:6px 12px; font-size:12px; }
        .dr-kv-label { color:var(--text-muted); }
        .dr-kv-value { color:var(--text-primary); font-weight:500; }

        .dr-metrics { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
        .dr-metric { background:var(--bg-card); border-radius:10px; padding:14px 18px; flex:1; min-width:120px; text-align:center; }
        .dr-metric-value { font-size:22px; font-weight:700; color:var(--text-primary); }
        .dr-metric-label { font-size:11px; color:var(--text-muted); margin-top:2px; }

        .dr-health-big { display:flex; align-items:center; gap:12px; padding:14px; background:var(--bg-primary); border-radius:10px; margin-bottom:14px; }
        .dr-health-icon { font-size:32px; }
        .dr-health-text { font-size:15px; font-weight:600; }

        .dr-btn { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:8px 14px; cursor:pointer; font-size:12px; display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
        .dr-btn:hover { filter:brightness(1.1); }
        .dr-btn:disabled { opacity:.5; cursor:not-allowed; filter:none; }
        .dr-btn-sm { padding:5px 10px; font-size:11px; }
        .dr-btn-outline { background:transparent; border:1px solid var(--border); color:var(--text-secondary); }
        .dr-btn-outline:hover { border-color:var(--accent); color:var(--accent); }
        .dr-btn-danger { background:#ef4444; }
        .dr-btn-danger:hover { background:#dc2626; }
        .dr-btn-warn { background:#f59e0b; }
        .dr-btn-warn:hover { background:#d97706; }

        .dr-select { background:var(--bg-primary); border:1px solid var(--border); border-radius:6px; padding:8px 10px; color:var(--text-primary); font-size:12px; }
        .dr-select option { background:var(--bg-primary); color:var(--text-primary); }

        .dr-log { max-height:200px; overflow-y:auto; background:var(--bg-primary); border-radius:8px; padding:8px 10px; margin-top:10px; font-family:monospace; font-size:11px; color:var(--text-muted); }
        .dr-log-line { padding:2px 0; border-bottom:1px solid var(--border); }
        .dr-log-line:last-child { border:none; }
        .dr-log-line.error { color:#ef4444; }
        .dr-log-line.success { color:#10b981; }

        .dr-progress-outer { height:20px; background:var(--bg-primary); border-radius:10px; overflow:hidden; position:relative; margin-top:10px; }
        .dr-progress-inner { height:100%; background:linear-gradient(90deg, var(--accent), #6366f1); border-radius:10px; transition:width .3s; width:0%; }
        .dr-progress-text { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:600; color:#fff; text-shadow:0 1px 2px rgba(0,0,0,.4); }

        .dr-row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
        .dr-warning { background:#fef3c7; color:#92400e; border-radius:8px; padding:10px 14px; font-size:12px; display:flex; align-items:center; gap:8px; margin-bottom:12px; }
        .dr-warning i { color:#f59e0b; }

        .dr-smart-status { display:inline-block; width:8px; height:8px; border-radius:50%; }

        .dr-hist-item { display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid var(--border); font-size:12px; }
        .dr-hist-item:last-child { border:none; }
        .dr-hist-icon { font-size:16px; width:20px; text-align:center; }
        .dr-hist-meta { flex:1; min-width:0; }
        .dr-hist-time { font-size:10px; color:var(--text-muted); white-space:nowrap; }

        .dr-nodata { text-align:center; padding:30px; color:var(--text-muted); font-size:13px; }
    </style>

    <div class="dr-wrap">
        <div class="dr-sidebar">
            <div class="dr-sidebar-header">
                <span><i class="fas fa-hard-drive"></i> Dyski</span>
                <button class="dr-btn dr-btn-sm dr-btn-outline" id="dr-refresh-disks" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dr-disk-list" id="dr-disk-list"></div>
        </div>
        <div class="dr-main">
            <div class="dr-banner" id="dr-banner">
                <i class="fas fa-cog fa-spin" style="color:var(--accent);font-size:18px"></i>
                <div class="dr-banner-info">
                    <div class="dr-banner-title" id="dr-banner-title"></div>
                    <div class="dr-banner-progress"><div class="dr-banner-bar" id="dr-banner-bar"></div></div>
                    <div class="dr-banner-detail" id="dr-banner-detail"></div>
                </div>
                <button class="dr-btn dr-btn-sm dr-btn-danger" id="dr-banner-cancel"><i class="fas fa-stop"></i> Anuluj</button>
            </div>
            <div class="dr-tabs" id="dr-tabs">
                <div class="dr-tab active" data-tab="info"><i class="fas fa-info-circle"></i> Info</div>
                <div class="dr-tab" data-tab="smart"><i class="fas fa-heartbeat"></i> SMART</div>
                <div class="dr-tab" data-tab="schedule"><i class="fas fa-calendar-alt"></i> ${t('Harmonogram')}</div>
                <div class="dr-tab" data-tab="check"><i class="fas fa-search"></i> ${t('Sprawdź')}</div>
                <div class="dr-tab" data-tab="repair"><i class="fas fa-wrench"></i> Naprawa</div>
                <div class="dr-tab" data-tab="history"><i class="fas fa-clock-rotate-left"></i> Historia</div>
            </div>
            <div class="dr-content" id="dr-content">
                <div class="dr-empty"><i class="fas fa-hard-drive"></i><span>Wybierz dysk z listy</span></div>
            </div>
        </div>
    </div>`;

    const $ = s => body.querySelector(s);
    const $$ = s => body.querySelectorAll(s);

    /* ─── Helpers ─── */
    function humanSize(b) {
        if (b == null) return '—';
        const n = Number(b);
        if (isNaN(n)) return String(b);
        if (n >= 1e12) return (n / 1e12).toFixed(1) + ' TB';
        if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
        if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
        return (n / 1024).toFixed(0) + ' KB';
    }

    function healthColor(disk) {
        if (!disk.smart_available) return '#888';
        if (disk.pending_sectors > 0) return '#ef4444';
        if (disk.reallocated_sectors > 0) return '#f59e0b';
        if (disk.smart_healthy === false) return '#ef4444';
        return '#10b981';
    }

    function tempColor(t) {
        if (t == null) return '';
        if (t > 60) return '#ef4444';
        if (t > 50) return '#f59e0b';
        return '#10b981';
    }

    function isRunning() {
        return state.operation && (state.operation.status === 'running' || state.operation.status === 'starting');
    }

    /* ─── Load disks ─── */
    async function loadDisks() {
        try {
            const data = await api('/diskrepair/disks');
            state.disks = data || [];
            renderDiskList();
            if (state.selectedDisk) {
                const still = state.disks.find(d => d.name === state.selectedDisk.name);
                if (still) { state.selectedDisk = still; }
            }
        } catch (e) {
            toast(t('Błąd ładowania dysków: ') + e.message, 'error');
        }
    }

    function renderDiskList() {
        const list = $('#dr-disk-list');
        if (!state.disks.length) {
            list.innerHTML = `<div class="dr-nodata"><i class="fas fa-info-circle"></i> ${t('Nie znaleziono dysków')}</div>`;
            return;
        }
        list.innerHTML = state.disks.map(d => {
            const sel = state.selectedDisk && state.selectedDisk.name === d.name;
            const icon = (d.rotational === false || (d.model && /ssd|nvme/i.test(d.model))) ? 'fa-microchip' : 'fa-hard-drive';
            const hc = healthColor(d);
            const tc = tempColor(d.temperature);
            return `
                <div class="dr-disk-item ${sel ? 'active' : ''}" data-disk="${escHtml(d.name)}">
                    <i class="fas ${icon} dr-disk-icon"></i>
                    <div class="dr-disk-meta">
                        <div class="dr-disk-model">${escHtml(d.model || d.name)}</div>
                        <div class="dr-disk-size">${humanSize(d.size)}</div>
                    </div>
                    ${d.temperature != null ? `<span class="dr-temp-badge" style="color:${sel ? '#fff' : tc}">${d.temperature}°C</span>` : ''}
                    <span class="dr-health-dot" style="background:${hc}" title="${d.smart_healthy === false ? 'FAILING' : d.smart_healthy ? 'OK' : '?'}"></span>
                </div>`;
        }).join('');

        list.querySelectorAll('.dr-disk-item').forEach(el => {
            el.onclick = () => {
                const name = el.dataset.disk;
                const disk = state.disks.find(d => d.name === name);
                if (disk) selectDisk(disk);
            };
        });
    }

    function selectDisk(disk) {
        state.selectedDisk = disk;
        state.smartData = null;
        renderDiskList();
        renderTab();
    }

    /* ─── Tabs ─── */
    $$('.dr-tab').forEach(tab => {
        tab.onclick = () => {
            state.activeTab = tab.dataset.tab;
            $$('.dr-tab').forEach(t => t.classList.toggle('active', t === tab));
            renderTab();
        };
    });

    function renderTab() {
        const content = $('#dr-content');
        if (!state.selectedDisk) {
            content.innerHTML = '<div class="dr-empty"><i class="fas fa-hard-drive"></i><span>Wybierz dysk z listy</span></div>';
            return;
        }
        switch (state.activeTab) {
            case 'info': renderInfoTab(content); break;
            case 'smart': renderSmartTab(content); break;
            case 'schedule': renderScheduleTab(content); break;
            case 'check': renderCheckTab(content); break;
            case 'repair': renderRepairTab(content); break;
            case 'history': renderHistoryTab(content); break;
        }
    }

    /* ─── Tab: Info ─── */
    function renderInfoTab(el) {
        const d = state.selectedDisk;
        const noSmart = !d.smart_available;
        el.innerHTML = `
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-hard-drive"></i> Informacje o dysku</div>
                <div class="dr-kv">
                    <span class="dr-kv-label">${t('Urządzenie')}</span><span class="dr-kv-value">/dev/${escHtml(d.name)}</span>
                    <span class="dr-kv-label">Model</span><span class="dr-kv-value">${escHtml(d.model || '—')}</span>
                    <span class="dr-kv-label">Rozmiar</span><span class="dr-kv-value">${humanSize(d.size)}</span>
                    <span class="dr-kv-label">SMART</span><span class="dr-kv-value">${noSmart ? `<span style="color:#f59e0b">${t('Niedostępny')}</span>` : (d.smart_healthy ? '<span style="color:#10b981">Zdrowy</span>' : '<span style="color:#ef4444">Problemy!</span>')}</span>
                    <span class="dr-kv-label">Temperatura</span><span class="dr-kv-value">${d.temperature != null ? d.temperature + '°C' : '—'}</span>
                    <span class="dr-kv-label">Godziny pracy</span><span class="dr-kv-value">${d.power_on_hours != null ? d.power_on_hours.toLocaleString() + ' h' : '—'}</span>
                    <span class="dr-kv-label">Realokowane sektory</span><span class="dr-kv-value">${d.reallocated_sectors != null ? `<span style="color:${d.reallocated_sectors > 0 ? '#f59e0b' : '#10b981'}">${d.reallocated_sectors}</span>` : '—'}</span>
                    <span class="dr-kv-label">${t('Oczekujące sektory')}</span><span class="dr-kv-value">${d.pending_sectors != null ? `<span style="color:${d.pending_sectors > 0 ? '#ef4444' : '#10b981'}">${d.pending_sectors}</span>` : '—'}</span>
                </div>
            </div>
            ${noSmart ? `<div class="dr-warning"><i class="fas fa-exclamation-triangle"></i> ${t('SMART niedostępny dla tego dysku (dyski USB mogą nie obsługiwać SMART).')}</div>` : ''}
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-table-cells"></i> Partycje</div>
                ${d.partitions && d.partitions.length ? `
                <table class="dr-table">
                    <thead><tr><th>${t('Nazwa')}</th><th>${t('Rozmiar')}</th><th>${t('System plików')}</th><th>${t('Punkt montowania')}</th><th>Status</th><th></th></tr></thead>
                    <tbody id="dr-partitions-body">
                    ${d.partitions.map(p => `
                        <tr>
                            <td>/dev/${escHtml(p.name)}</td>
                            <td>${humanSize(p.size)}</td>
                            <td>${escHtml(p.fstype || '—')}</td>
                            <td>${escHtml(p.mountpoint || '—')}</td>
                            <td>${p.mounted ? '<span style="color:#10b981"><i class="fas fa-circle" style="font-size:8px"></i> Zamontowany</span>' : '<span style="color:var(--text-muted)"><i class="far fa-circle" style="font-size:8px"></i> Niezamontowany</span>'}</td>
                            <td><button class="dr-btn dr-btn-sm dr-btn-outline dr-part-check" data-part="${escHtml(p.name)}" ${isRunning() ? 'disabled' : ''}><i class="fas fa-search"></i> ${t('Sprawdź')}</button></td>
                        </tr>
                    `).join('')}
                    </tbody>
                </table>` : '<div class="dr-nodata">Brak partycji</div>'}
            </div>`;

        el.querySelectorAll('.dr-part-check').forEach(btn => {
            btn.onclick = () => startFsck(btn.dataset.part, false);
        });
    }

    /* ─── Tab: SMART ─── */
    async function renderSmartTab(el) {
        const d = state.selectedDisk;
        if (!d.smart_available) {
            el.innerHTML = `<div class="dr-warning"><i class="fas fa-exclamation-triangle"></i> ${t('SMART niedostępny dla tego dysku (dyski USB mogą nie obsługiwać SMART).')}</div>`;
            return;
        }

        el.innerHTML = `<div class="dr-nodata"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie danych SMART...')}</div>`;

        try {
            const smart = await api(`/diskrepair/smart/${d.name}`);
            state.smartData = smart;
            renderSmartContent(el, smart);
        } catch (e) {
            el.innerHTML = `<div class="dr-warning"><i class="fas fa-times-circle"></i> ${t('Błąd ładowania SMART:')} ${escHtml(e.message)}</div>`;
        }
    }

    function renderSmartContent(el, smart) {
        const healthy = smart.health && /passed|ok/i.test(smart.health);
        el.innerHTML = `
            <div class="dr-health-big">
                <i class="fas ${healthy ? 'fa-check-circle' : 'fa-exclamation-triangle'} dr-health-icon" style="color:${healthy ? '#10b981' : '#ef4444'}"></i>
                <div>
                    <div class="dr-health-text" style="color:${healthy ? '#10b981' : '#ef4444'}">${healthy ? 'SMART: Zdrowy' : 'SMART: Problemy wykryte!'}</div>
                    <div style="font-size:12px;color:var(--text-muted)">${escHtml(smart.health || '')}</div>
                </div>
            </div>
            <div class="dr-metrics">
                <div class="dr-metric">
                    <div class="dr-metric-value" style="color:${tempColor(smart.temperature)}">${smart.temperature != null ? smart.temperature + '°C' : '—'}</div>
                    <div class="dr-metric-label">Temperatura</div>
                </div>
                <div class="dr-metric">
                    <div class="dr-metric-value">${smart.power_on_hours != null ? smart.power_on_hours.toLocaleString() : '—'}</div>
                    <div class="dr-metric-label">Godziny pracy</div>
                </div>
                <div class="dr-metric">
                    <div class="dr-metric-value">${smart.power_cycle_count != null ? smart.power_cycle_count.toLocaleString() : '—'}</div>
                    <div class="dr-metric-label">Cykle zasilania</div>
                </div>
            </div>
            ${smart.attributes && smart.attributes.length ? `
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-list"></i> Atrybuty SMART</div>
                <div style="overflow-x:auto">
                <table class="dr-table">
                    <thead><tr><th>ID</th><th>${t('Nazwa')}</th><th>${t('Wartość')}</th><th>${t('Najgorsza')}</th><th>${t('Próg')}</th><th>Raw</th><th>Status</th></tr></thead>
                    <tbody>
                    ${smart.attributes.map(a => {
                        const failing = a.thresh && a.value && Number(a.value) <= Number(a.thresh);
                        const warn = a.name && /reallocat|pending|uncorrect/i.test(a.name) && Number(a.raw) > 0;
                        const color = failing ? '#ef4444' : warn ? '#f59e0b' : '#10b981';
                        return `<tr>
                            <td>${escHtml(a.id)}</td>
                            <td>${escHtml(a.name)}</td>
                            <td>${escHtml(a.value)}</td>
                            <td>${escHtml(a.worst)}</td>
                            <td>${escHtml(a.thresh)}</td>
                            <td style="font-family:monospace">${escHtml(a.raw)}</td>
                            <td><span class="dr-smart-status" style="background:${color}"></span></td>
                        </tr>`;
                    }).join('')}
                    </tbody>
                </table>
                </div>
            </div>` : ''}
            ${smart.error_log ? `<div class="dr-card"><div class="dr-card-title"><i class="fas fa-exclamation-circle"></i> ${t('Log błędów')}</div><pre style="font-size:11px;color:var(--text-muted);white-space:pre-wrap;margin:0">${escHtml(smart.error_log)}</pre></div>` : ''}
            ${smart.self_test_log ? `<div class="dr-card"><div class="dr-card-title"><i class="fas fa-vial"></i> ${t('Log testów')}</div><pre style="font-size:11px;color:var(--text-muted);white-space:pre-wrap;margin:0">${escHtml(smart.self_test_log)}</pre></div>` : ''}
            <div class="dr-row" style="margin-top:8px">
                <button class="dr-btn" id="dr-smart-short" ${isRunning() ? 'disabled' : ''}><i class="fas fa-bolt"></i> ${t('Test krótki')}</button>
                <button class="dr-btn dr-btn-warn" id="dr-smart-long" ${isRunning() ? 'disabled' : ''}><i class="fas fa-clock"></i> ${t('Test długi')}</button>
            </div>`;

        const shortBtn = el.querySelector('#dr-smart-short');
        const longBtn = el.querySelector('#dr-smart-long');
        if (shortBtn) shortBtn.onclick = () => startSmartTest('short');
        if (longBtn) longBtn.onclick = () => startSmartTest('long');
    }

    async function startSmartTest(type) {
        if (isRunning()) { toast('Inna operacja w toku', 'warning'); return; }
        const d = state.selectedDisk;
        try {
            const r = await api('/diskrepair/smart-test', { method: 'POST', body: { disk: d.name, type } });
            toast(r.message || 'Test SMART uruchomiony', 'success');
        } catch (e) {
            toast(t('Błąd: ') + e.message, 'error');
        }
    }

    /* ─── Tab: Sprawdź (Check) ─── */
    function renderCheckTab(el) {
        const d = state.selectedDisk;
        const parts = (d.partitions || []).filter(p => p.fstype);
        el.innerHTML = `
            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-search"></i> ${t('Sprawdzanie systemu plików (fsck)')}</div>
                ${parts.length ? `
                <div class="dr-row" style="margin-bottom:12px">
                    <select class="dr-select" id="dr-check-part" style="flex:1">
                        <option value="">— ${t('Wybierz partycję')} —</option>
                        ${parts.map(p => `<option value="${escHtml(p.name)}">/dev/${escHtml(p.name)} (${escHtml(p.fstype)}, ${humanSize(p.size)})</option>`).join('')}
                    </select>
                    <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary);cursor:pointer">
                        <input type="checkbox" id="dr-check-repair"> ${t('Napraw (fsck -y)')}
                    </label>
                </div>
                <div class="dr-row">
                    <button class="dr-btn" id="dr-check-start" ${isRunning() ? 'disabled' : ''}><i class="fas fa-play"></i> Rozpocznij sprawdzanie</button>
                </div>
                ` : `<div class="dr-nodata">${t('Brak partycji z systemem plików')}</div>`}
            </div>
            <div id="dr-check-progress" style="display:none">
                <div class="dr-card">
                    <div class="dr-card-title"><i class="fas fa-cog fa-spin"></i> ${t('Postęp')}</div>
                    <div class="dr-progress-outer">
                        <div class="dr-progress-inner" id="dr-check-bar"></div>
                        <div class="dr-progress-text" id="dr-check-pct">0%</div>
                    </div>
                    <div class="dr-log" id="dr-check-log"></div>
                    <div style="margin-top:10px"><button class="dr-btn dr-btn-sm dr-btn-danger" id="dr-check-cancel"><i class="fas fa-stop"></i> Anuluj</button></div>
                </div>
            </div>`;

        const startBtn = el.querySelector('#dr-check-start');
        if (startBtn) {
            startBtn.onclick = () => {
                const part = el.querySelector('#dr-check-part').value;
                const repair = el.querySelector('#dr-check-repair').checked;
                if (!part) { toast(t('Wybierz partycję'), 'warning'); return; }
                startFsck(part, repair);
            };
        }
        const cancelBtn = el.querySelector('#dr-check-cancel');
        if (cancelBtn) cancelBtn.onclick = cancelOperation;

        if (isRunning() && state.operation && (state.operation.operation === 'fsck')) {
            showCheckProgress(el);
        }
    }

    function showCheckProgress(el) {
        const wrap = el.querySelector('#dr-check-progress');
        if (wrap) wrap.style.display = '';
    }

    async function startFsck(partition, repair) {
        if (isRunning()) { toast('Inna operacja w toku', 'warning'); return; }
        try {
            await api('/diskrepair/fsck', { method: 'POST', body: { partition, repair } });
            toast(t('Sprawdzanie rozpoczęte'), 'info');
            state.logOffset = 0;
            startPolling();
        } catch (e) {
            toast(t('Błąd: ') + e.message, 'error');
        }
    }

    /* ─── Tab: Naprawa (Repair) ─── */
    function renderRepairTab(el) {
        const d = state.selectedDisk;
        const parts = (d.partitions || []).filter(p => p.fstype);
        el.innerHTML = `
            <div class="dr-warning"><i class="fas fa-exclamation-triangle"></i> ${t('Operacje naprawcze mogą trwać bardzo długo i mogą uszkodzić dane. Upewnij się, że masz kopię zapasową!')}</div>

            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-search-plus"></i> ${t('Skanowanie uszkodzonych sektorów (badblocks)')}</div>
                <div class="dr-row" style="margin-bottom:10px">
                    <span style="font-size:12px;color:var(--text-secondary)">Dysk:</span>
                    <strong style="font-size:12px;color:var(--text-primary)">/dev/${escHtml(d.name)}</strong>
                    <select class="dr-select" id="dr-bb-mode">
                        <option value="readonly">Tylko odczyt (bezpieczny)</option>
                        <option value="nondestructive">Niedestrukcyjny zapis</option>
                    </select>
                </div>
                <div class="dr-warning"><i class="fas fa-clock"></i> ${t('Badblocks może trwać wiele godzin, w zależności od rozmiaru dysku.')}</div>
                <button class="dr-btn dr-btn-warn" id="dr-bb-start" ${isRunning() ? 'disabled' : ''}><i class="fas fa-play"></i> Rozpocznij skanowanie</button>
            </div>

            <div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-wrench"></i> ${t('Naprawa systemu plików')}</div>
                ${parts.length ? `
                <div class="dr-row" style="margin-bottom:10px">
                    <select class="dr-select" id="dr-repair-part" style="flex:1">
                        <option value="">— ${t('Wybierz partycję')} —</option>
                        ${parts.map(p => `<option value="${escHtml(p.name)}">/dev/${escHtml(p.name)} (${escHtml(p.fstype)}, ${humanSize(p.size)})${p.mounted ? ' [' + t('zamontowany') + ']' : ''}</option>`).join('')}
                    </select>
                    <button class="dr-btn dr-btn-sm dr-btn-outline" id="dr-repair-unmount"><i class="fas fa-eject"></i> Odmontuj</button>
                    <button class="dr-btn dr-btn-danger" id="dr-repair-start" ${isRunning() ? 'disabled' : ''}><i class="fas fa-wrench"></i> Napraw (fsck -y)</button>
                </div>
                ` : `<div class="dr-nodata">${t('Brak partycji z systemem plików')}</div>`}
            </div>

            <div id="dr-repair-progress" style="display:none">
                <div class="dr-card">
                    <div class="dr-card-title"><i class="fas fa-cog fa-spin"></i> ${t('Postęp operacji')}</div>
                    <div class="dr-progress-outer">
                        <div class="dr-progress-inner" id="dr-repair-bar"></div>
                        <div class="dr-progress-text" id="dr-repair-pct">0%</div>
                    </div>
                    <div class="dr-log" id="dr-repair-log"></div>
                    <div style="margin-top:10px"><button class="dr-btn dr-btn-sm dr-btn-danger" id="dr-repair-cancel"><i class="fas fa-stop"></i> Anuluj</button></div>
                </div>
            </div>`;

        const bbStartBtn = el.querySelector('#dr-bb-start');
        if (bbStartBtn) {
            bbStartBtn.onclick = () => {
                const mode = el.querySelector('#dr-bb-mode').value;
                startBadblocks(d.name, mode);
            };
        }

        const repairStartBtn = el.querySelector('#dr-repair-start');
        if (repairStartBtn) {
            repairStartBtn.onclick = () => {
                const part = el.querySelector('#dr-repair-part').value;
                if (!part) { toast(t('Wybierz partycję'), 'warning'); return; }
                startFsck(part, true);
            };
        }

        const unmountBtn = el.querySelector('#dr-repair-unmount');
        if (unmountBtn) {
            unmountBtn.onclick = async () => {
                const part = el.querySelector('#dr-repair-part').value;
                if (!part) { toast(t('Wybierz partycję'), 'warning'); return; }
                try {
                    await api('/diskrepair/unmount', { method: 'POST', body: { partition: part } });
                    toast('Odmontowano /dev/' + part, 'success');
                    await loadDisks();
                    renderTab();
                } catch (e) {
                    toast(t('Błąd odmontowywania: ') + e.message, 'error');
                }
            };
        }

        const repairCancel = el.querySelector('#dr-repair-cancel');
        if (repairCancel) repairCancel.onclick = cancelOperation;

        if (isRunning()) {
            const wrap = el.querySelector('#dr-repair-progress');
            if (wrap) wrap.style.display = '';
        }
    }

    async function startBadblocks(disk, mode) {
        if (isRunning()) { toast('Inna operacja w toku', 'warning'); return; }
        try {
            await api('/diskrepair/badblocks', { method: 'POST', body: { disk, mode } });
            toast(t('Skanowanie badblocks rozpoczęte'), 'info');
            state.logOffset = 0;
            startPolling();
        } catch (e) {
            toast(t('Błąd: ') + e.message, 'error');
        }
    }

    /* ─── Tab: Historia ─── */
    async function renderHistoryTab(el) {
        el.innerHTML = `<div class="dr-nodata"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;
        try {
            const data = await api('/diskrepair/history');
            state.history = data || [];
            if (!state.history.length) {
                el.innerHTML = '<div class="dr-nodata"><i class="fas fa-clock-rotate-left"></i> Brak historii operacji</div>';
                return;
            }
            el.innerHTML = `<div class="dr-card"><div class="dr-card-title"><i class="fas fa-clock-rotate-left"></i> Historia operacji</div>
                ${state.history.slice().reverse().map(h => {
                    const ok = h.success || h.result === 'success';
                    const icon = ok ? 'fa-check-circle' : 'fa-times-circle';
                    const color = ok ? '#10b981' : '#ef4444';
                    const ts = h.timestamp ? new Date(typeof h.timestamp === 'number' && h.timestamp < 1e12 ? h.timestamp * 1000 : h.timestamp).toLocaleString(getLocale()) : '';
                    return `<div class="dr-hist-item">
                        <i class="fas ${icon} dr-hist-icon" style="color:${color}"></i>
                        <div class="dr-hist-meta">
                            <div style="font-weight:500;color:var(--text-primary)">${escHtml(h.operation || h.type || '?')} — ${escHtml(h.disk || h.partition || '')}</div>
                            <div style="font-size:11px;color:var(--text-muted)">${escHtml(h.message || h.detail || '')}</div>
                        </div>
                        <div class="dr-hist-time">${ts}</div>
                    </div>`;
                }).join('')}
            </div>`;
        } catch (e) {
            el.innerHTML = `<div class="dr-warning"><i class="fas fa-times-circle"></i> ${t('Błąd:')} ${escHtml(e.message)}</div>`;
        }
    }

    /* ─── Tab: Schedule (SMART tests) ─── */
    async function renderScheduleTab(el) {
        const d = state.selectedDisk;
        if (!d.smart_available) {
            el.innerHTML = '<div class="dr-nodata">SMART niedostępny dla tego dysku</div>';
            return;
        }
        el.innerHTML = `<div class="dr-nodata"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;

        const [schedRes, resultRes] = await Promise.allSettled([
            api('/storage/smart/schedule'),
            api('/storage/smart/test/result?disk=' + encodeURIComponent(d.name)),
        ]);
        const sched = (schedRes.status === 'fulfilled' ? schedRes.value : {}).tests || [];
        const diskSched = sched.filter(s => s.disk === d.name);
        const testLog = (resultRes.status === 'fulfilled' ? resultRes.value : {});
        const tests = testLog.tests || [];
        const testRunning = testLog.running || false;
        const testProgress = testLog.progress;

        const freqLabel = { daily: t('Codziennie'), weekly: t('Co tydzień'), monthly: t('Co miesiąc') };
        const typeLabel = { short: t('Krótki'), long: t('Długi'), conveyance: 'Conveyance' };

        let html = '';

        // Quick test trigger
        html += `<div class="dr-card">
            <div class="dr-card-title"><i class="fas fa-play-circle"></i> ${t('Uruchom test')}</div>
            ${testRunning ? `<div style="display:flex;align-items:center;gap:10px;padding:8px;background:var(--bg-primary);border-radius:6px;margin-bottom:10px">
                <i class="fas fa-spinner fa-spin" style="color:var(--accent)"></i>
                <span style="font-size:13px">${t('Test w toku')}${testProgress != null ? ' — ' + testProgress + '%' : ''}</span>
            </div>` : `<div style="display:flex;gap:8px;flex-wrap:wrap">
                <button class="dr-btn" data-trigger="short"><i class="fas fa-bolt"></i> ${t('Krótki')} (~2 min)</button>
                <button class="dr-btn" data-trigger="long"><i class="fas fa-clock"></i> ${t('Długi')} (~60 min)</button>
                <button class="dr-btn dr-btn-outline" data-trigger="conveyance"><i class="fas fa-truck"></i> Conveyance (~5 min)</button>
            </div>`}
        </div>`;

        // Scheduled tests for this disk
        html += `<div class="dr-card">
            <div class="dr-card-title"><i class="fas fa-calendar-alt"></i> ${t('Zaplanowane testy')}</div>`;
        if (diskSched.length) {
            html += '<table class="dr-table"><thead><tr><th>Typ</th><th>' + t('Częstotliwość') + '</th><th>Status</th><th></th></tr></thead><tbody>';
            for (const s of diskSched) {
                html += `<tr>
                    <td>${typeLabel[s.type] || s.type}</td>
                    <td>${freqLabel[s.frequency] || s.frequency}</td>
                    <td>${s.enabled ? '<span style="color:#10b981">✓ ' + t('Aktywny') + '</span>' : '<span style="color:var(--text-muted)">' + t('Wyłączony') + '</span>'}</td>
                    <td><button class="dr-btn dr-btn-sm dr-btn-outline" data-delete-sched="${escHtml(s.id)}"><i class="fas fa-trash"></i></button></td>
                </tr>`;
            }
            html += '</tbody></table>';
        } else {
            html += `<div style="color:var(--text-muted);font-size:13px;padding:8px 0">${t('Brak zaplanowanych testów dla tego dysku.')}</div>`;
        }
        html += `<div style="margin-top:10px"><button class="dr-btn dr-btn-sm" id="dr-add-schedule"><i class="fas fa-plus"></i> ${t('Zaplanuj test')}</button></div>`;
        html += '</div>';

        // Test log
        if (tests.length) {
            html += `<div class="dr-card">
                <div class="dr-card-title"><i class="fas fa-list"></i> ${t('Wyniki testów')}</div>
                <table class="dr-table"><thead><tr><th>#</th><th>Typ</th><th>Status</th><th>${t('Czas pracy')}</th></tr></thead><tbody>`;
            for (const t2 of tests.slice(0, 10)) {
                const ok = (t2.status || '').includes('Completed') || (t2.status || '').includes('success');
                const clr = ok ? '#10b981' : (t2.status || '').includes('progress') ? '#f59e0b' : '#ef4444';
                html += `<tr>
                    <td>${t2.num || ''}</td>
                    <td>${t2.type || ''}</td>
                    <td style="color:${clr}">${t2.status || '—'}</td>
                    <td>${t2.lifetime_hours || ''}h</td>
                </tr>`;
            }
            html += '</tbody></table></div>';
        }

        el.innerHTML = html;

        // Test trigger buttons
        el.querySelectorAll('[data-trigger]').forEach(btn => {
            btn.onclick = async () => {
                const ttype = btn.dataset.trigger;
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ...';
                const res = await api('/storage/smart/test', { method: 'POST', body: { disk: d.name, type: ttype } });
                if (res.error) { toast(res.error, 'error'); }
                else { toast(res.message || 'Test started', 'success'); }
                setTimeout(() => renderScheduleTab(el), 2000);
            };
        });

        // Delete schedule
        el.querySelectorAll('[data-delete-sched]').forEach(btn => {
            btn.onclick = async () => {
                const sid = btn.dataset.deleteSched;
                const ok = await confirmDialog(t('Usunąć zaplanowany test?'));
                if (!ok) return;
                const res = await api('/storage/smart/schedule/' + encodeURIComponent(sid), { method: 'DELETE' });
                if (res.error) toast(res.error, 'error');
                else toast(t('Usunięto'), 'success');
                renderScheduleTab(el);
            };
        });

        // Add schedule button
        el.querySelector('#dr-add-schedule')?.addEventListener('click', () => {
            const modal = document.createElement('div');
            modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:10000';
            modal.innerHTML = `<div style="background:var(--bg-secondary);border-radius:12px;padding:24px;width:340px;max-width:90vw">
                <h3 style="margin:0 0 16px;font-size:16px">${t('Zaplanuj test SMART')}</h3>
                <div style="margin-bottom:12px">
                    <label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:4px">${t('Typ testu')}</label>
                    <select id="sched-type" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary)">
                        <option value="short">${t('Krótki')} (~2 min)</option>
                        <option value="long">${t('Długi')} (~60 min)</option>
                        <option value="conveyance">Conveyance (~5 min)</option>
                    </select>
                </div>
                <div style="margin-bottom:16px">
                    <label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:4px">${t('Częstotliwość')}</label>
                    <select id="sched-freq" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary)">
                        <option value="weekly">${t('Co tydzień')} (${t('niedziela')} 3:00)</option>
                        <option value="daily">${t('Codziennie')} (3:00)</option>
                        <option value="monthly">${t('Co miesiąc')} (1. ${t('dzień')} 3:00)</option>
                    </select>
                </div>
                <div style="display:flex;gap:8px;justify-content:flex-end">
                    <button class="dr-btn dr-btn-outline" id="sched-cancel">${t('Anuluj')}</button>
                    <button class="dr-btn" id="sched-save"><i class="fas fa-check"></i> ${t('Zapisz')}</button>
                </div>
            </div>`;
            document.body.appendChild(modal);
            modal.querySelector('#sched-cancel').onclick = () => modal.remove();
            modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
            modal.querySelector('#sched-save').onclick = async () => {
                const res = await api('/storage/smart/schedule', {
                    method: 'POST',
                    body: {
                        disk: d.name,
                        type: modal.querySelector('#sched-type').value,
                        frequency: modal.querySelector('#sched-freq').value,
                    },
                });
                modal.remove();
                if (res.error) toast(res.error, 'error');
                else toast(t('Test zaplanowany'), 'success');
                renderScheduleTab(el);
            };
        });
    }

    /* ─── Operation banner / polling ─── */
    async function cancelOperation() {
        try {
            await api('/diskrepair/cancel', { method: 'POST' });
            toast('Operacja anulowana', 'warning');
        } catch (e) {
            toast(t('Błąd anulowania: ') + e.message, 'error');
        }
    }

    function startPolling() {
        stopPolling();
        state.pollTimer = setInterval(pollStatus, 2000);
        pollStatus();
    }

    function stopPolling() {
        if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    }

    async function pollStatus() {
        try {
            const st = await api(`/diskrepair/status?since=${state.logOffset}`);
            state.operation = st;

            const banner = $('#dr-banner');
            const bannerTitle = $('#dr-banner-title');
            const bannerBar = $('#dr-banner-bar');
            const bannerDetail = $('#dr-banner-detail');
            const bannerCancel = $('#dr-banner-cancel');

            if (st.status === 'running' || st.status === 'starting') {
                banner.classList.add('active');
                bannerTitle.textContent = `${st.operation || 'Operacja'} — ${st.disk || st.partition || ''}`;
                bannerBar.style.width = (st.percent || 0) + '%';

                let detail = st.message || '';
                if (st.percent != null) detail = st.percent + '% ' + detail;
                bannerDetail.textContent = detail;
                bannerCancel.style.display = '';

                updateTabProgress(st);

                for (const line of (st.logs || [])) appendLog(line);
                state.logOffset = st.log_total || state.logOffset;

                disableStartButtons(true);
            } else if (st.status === 'done' || st.status === 'error') {
                for (const line of (st.logs || [])) appendLog(line);
                state.logOffset = st.log_total || state.logOffset;

                const ok = st.status === 'done' || (st.result && st.result === 'success');
                banner.classList.add('active');
                bannerTitle.textContent = ok ? t('Operacja zakończona pomyślnie') : t('Operacja zakończona z błędem');
                bannerBar.style.width = ok ? '100%' : '0%';
                bannerDetail.textContent = st.message || '';
                bannerCancel.style.display = 'none';

                toast(ok ? t('Operacja zakończona') : t('Operacja nie powiodła się'), ok ? 'success' : 'error');
                stopPolling();
                disableStartButtons(false);

                // Dismiss after showing result
                try { await api('/diskrepair/dismiss', { method: 'POST' }); } catch (_) {}
                state.operation = null;

                // Auto-hide banner after 5s
                setTimeout(() => {
                    banner.classList.remove('active');
                    loadDisks();
                }, 5000);
            } else {
                banner.classList.remove('active');
                stopPolling();
                disableStartButtons(false);
                state.operation = null;
            }
        } catch (e) {
            // Keep polling on transient errors
        }
    }

    function updateTabProgress(st) {
        // Update inline progress bars in check/repair tabs
        const bar = body.querySelector('#dr-check-bar') || body.querySelector('#dr-repair-bar');
        const pct = body.querySelector('#dr-check-pct') || body.querySelector('#dr-repair-pct');
        const progressWrap = body.querySelector('#dr-check-progress') || body.querySelector('#dr-repair-progress');

        if (progressWrap) progressWrap.style.display = '';
        if (bar) bar.style.width = (st.percent || 0) + '%';
        if (pct) pct.textContent = (st.percent || 0) + '%';
    }

    function appendLog(line) {
        const logEls = body.querySelectorAll('.dr-log');
        logEls.forEach(logEl => {
            const cls = /error|fail/i.test(line) ? 'error' : /success|pass|ok/i.test(line) ? 'success' : '';
            logEl.innerHTML += `<div class="dr-log-line ${cls}">${escHtml(line)}</div>`;
            logEl.scrollTop = logEl.scrollHeight;
        });
    }

    function disableStartButtons(disabled) {
        body.querySelectorAll('#dr-check-start, #dr-bb-start, #dr-repair-start, #dr-smart-short, #dr-smart-long').forEach(btn => {
            btn.disabled = disabled;
        });
        body.querySelectorAll('.dr-part-check').forEach(btn => { btn.disabled = disabled; });
    }

    /* ─── Banner cancel ─── */
    $('#dr-banner-cancel').onclick = cancelOperation;

    /* ─── Refresh button ─── */
    $('#dr-refresh-disks').onclick = async () => {
        await loadDisks();
        renderTab();
        toast(t('Odświeżono'), 'info');
    };

    /* ─── Check for running operation ─── */
    async function checkRunningOp() {
        try {
            const st = await api('/diskrepair/status?since=0');
            if (st.status === 'running' || st.status === 'starting') {
                state.operation = st;
                state.logOffset = st.log_total || 0;
                startPolling();
            }
        } catch (_) {}
    }

    /* ─── Init ─── */
    loadDisks();
    checkRunningOp();
    return () => { stopPolling(); };
}

/* ═══════════════════════════════════════════════════════════
   Section: Maintenance (scrub, RAID check, TRIM)
   ═══════════════════════════════════════════════════════════ */
function _smMaintenance(el) {
    el.innerHTML = '<div style="padding:20px" id="mnt-root"><div style="color:var(--text-secondary)"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div></div>';
    const root = el.querySelector('#mnt-root');
    let pollTimer = null;
    const escHtml = _sto_escHtml;

    async function load() {
        const [poolsRes, raidRes, statusRes, histRes, rootfsRes, appUsageRes, scrubInfoRes, schedsRes] = await Promise.allSettled([
            api('/storage/pool/list'),
            api('/raid/arrays'),
            api('/storage/maintenance/status'),
            api('/storage/maintenance/history?limit=20'),
            api('/update/rootfs-info'),
            api('/storage/app-usage'),
            api('/storage/maintenance/scrub-info'),
            api('/storage/maintenance/schedules'),
            api('/storage/app-usage'),
        ]);

        const pools = ((poolsRes.status === 'fulfilled' ? poolsRes.value : {}).pools || []);
        const arrays = ((raidRes.status === 'fulfilled' ? raidRes.value : {}).arrays || (Array.isArray(raidRes.value) ? raidRes.value : []));
        const active = ((statusRes.status === 'fulfilled' ? statusRes.value : {}).tasks || []).filter(t2 => t2.status === 'running');
        const history = ((histRes.status === 'fulfilled' ? histRes.value : {}).history || []);
        const rootfs = (rootfsRes.status === 'fulfilled' ? rootfsRes.value : {});
        const appUsageItems = (appUsageRes.status === 'fulfilled' ? appUsageRes.value : {}).items || [];
        const scrubPools = (scrubInfoRes.status === 'fulfilled' ? scrubInfoRes.value : {}).pools || [];
        const schedules = (schedsRes.status === 'fulfilled' ? schedsRes.value : {}).schedules || [];

        const btrfsPools = pools.filter(p => p.fstype === 'btrfs' && p.mounted);
        const raidArrays = Array.isArray(arrays) ? arrays : [];
        // Merge scrub info from btrfs scrub status with pool list
        const scrubByMount = {};
        for (const si of scrubPools) scrubByMount[si.mount] = si;
        // Build schedule lookup: key = "type|target"
        const schedByKey = {};
        for (const s of schedules) schedByKey[s.type + '|' + s.target] = s;

        // Get all mounted non-system mounts for TRIM
        const mountsForTrim = pools.filter(p => p.mounted).map(p => ({ name: p.name, path: p.mount_path }));

        let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:20px">
            <h3 style="margin:0;font-size:16px;font-weight:600"><i class="fas fa-tools" style="color:var(--accent);margin-right:6px"></i>${t('Konserwacja storage')}</h3>
        </div>`;

        // ── Root filesystem / Overlay info card ──
        const fmtMB = mb => mb >= 1024 ? (mb/1024).toFixed(1)+' GB' : mb+' MB';
        if (rootfs.squashfs) {
            const onData = rootfs.overlay_on_data;
            const overlayUsedMB = rootfs.overlay_used_bytes ? Math.round(rootfs.overlay_used_bytes / (1024*1024)) : null;
            const rootFree = rootfs.root_partition_free_mb;
            const dataFree = rootfs.data_free_mb;
            const dataTotal = rootfs.data_total_mb;
            const dockerRoot = rootfs.docker_data_root || '/var/lib/docker';
            const dockerOnData = dockerRoot.startsWith('/mnt/data');
            const verity = rootfs.verity;

            html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px">
                <div style="font-size:13px;font-weight:600;margin-bottom:12px">
                    <i class="fas fa-layer-group" style="color:var(--accent);margin-right:6px"></i>${t('System plików (SquashFS + Overlay)')}
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;font-size:12px">
                    <div style="background:var(--bg-primary);border-radius:8px;padding:10px">
                        <div style="color:var(--text-muted);margin-bottom:4px">${t('Tryb')}</div>
                        <div style="font-weight:600;color:var(--accent)">SquashFS + OverlayFS</div>
                        <div style="color:var(--text-muted);font-size:11px;margin-top:2px">${t('Root tylko do odczytu')}</div>
                    </div>
                    <div style="background:var(--bg-primary);border-radius:8px;padding:10px">
                        <div style="color:var(--text-muted);margin-bottom:4px">${t('Warstwa zapisu (upper)')}</div>
                        <div style="font-weight:600;color:${onData ? 'var(--accent)' : '#eab308'}">
                            ${onData ? '<i class="fas fa-check-circle"></i> '+t('Dysk danych') : '<i class="fas fa-exclamation-triangle"></i> '+t('Partycja root')}
                        </div>
                        <div style="color:var(--text-muted);font-size:11px;margin-top:2px">${onData ? t('Root nigdy się nie zapełni') : t('Same-disk lub brak dysku danych')}</div>
                    </div>
                    ${overlayUsedMB != null ? `<div style="background:var(--bg-primary);border-radius:8px;padding:10px">
                        <div style="color:var(--text-muted);margin-bottom:4px">${t('Rozmiar overlay')}</div>
                        <div style="font-weight:600">${fmtMB(overlayUsedMB)}</div>
                        <div style="color:var(--text-muted);font-size:11px;margin-top:2px">${t('Zmiany od SquashFS')}</div>
                    </div>` : ''}
                    ${rootFree != null ? `<div style="background:var(--bg-primary);border-radius:8px;padding:10px">
                        <div style="color:var(--text-muted);margin-bottom:4px">${t('Root wolne')}</div>
                        <div style="font-weight:600;color:${rootFree < 200 ? '#ef4444' : rootFree < 500 ? '#eab308' : 'var(--text-primary)'}">${fmtMB(rootFree)}</div>
                        <div style="color:var(--text-muted);font-size:11px;margin-top:2px">${t('Partycja Root-A/B')}</div>
                    </div>` : ''}
                    ${dataFree != null ? `<div style="background:var(--bg-primary);border-radius:8px;padding:10px">
                        <div style="color:var(--text-muted);margin-bottom:4px">${t('Dysk danych wolne')}</div>
                        <div style="font-weight:600">${fmtMB(dataFree)} / ${fmtMB(dataTotal)}</div>
                        <div style="color:var(--text-muted);font-size:11px;margin-top:2px">EthOS-Data</div>
                    </div>` : ''}
                    <div style="background:var(--bg-primary);border-radius:8px;padding:10px">
                        <div style="color:var(--text-muted);margin-bottom:4px">Docker data-root</div>
                        <div style="font-weight:600;color:${dockerOnData ? 'var(--accent)' : '#eab308'}" title="${dockerRoot}">
                            ${dockerOnData ? '<i class="fas fa-check-circle"></i> '+t('Dysk danych') : '<i class="fas fa-exclamation-triangle"></i> '+t('Root')}
                        </div>
                        <div style="color:var(--text-muted);font-size:11px;margin-top:2px;word-break:break-all">${dockerRoot}</div>
                    </div>
                    <div style="background:var(--bg-primary);border-radius:8px;padding:10px">
                        <div style="color:var(--text-muted);margin-bottom:4px">dm-verity</div>
                        <div style="font-weight:600;color:${verity ? 'var(--accent)' : 'var(--text-muted)'}">
                            ${verity ? '<i class="fas fa-shield-alt"></i> '+t('Aktywna') : t('Nieaktywna')}
                        </div>
                        <div style="color:var(--text-muted);font-size:11px;margin-top:2px">${t('Weryfikacja integralności')}</div>
                    </div>
                </div>
            </div>`;
        } else if (rootfs.mode) {
            html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px">
                <div style="font-size:13px;font-weight:600;margin-bottom:6px">
                    <i class="fas fa-hdd" style="color:var(--text-muted);margin-right:6px"></i>${t('System plików')}
                </div>
                <div style="font-size:12px;color:var(--text-muted)">${t('Tryb')}: <strong>ext4</strong> — ${t('Standardowy install deweloperski. SquashFS nieaktywny.')}</div>
            </div>`;
        }

        // ── App disk usage card ──
        if (appUsageItems.length > 0) {
            const fmtBytes = b => {
                if (b >= 1073741824) return (b/1073741824).toFixed(1)+' GB';
                if (b >= 1048576) return (b/1048576).toFixed(0)+' MB';
                return (b/1024).toFixed(0)+' KB';
            };
            const maxBytes = appUsageItems[0].bytes;
            html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px">
                <div style="font-size:13px;font-weight:600;margin-bottom:12px">
                    <i class="fas fa-chart-bar" style="color:var(--accent);margin-right:6px"></i>${t('Wykorzystanie miejsca per aplikacja')}
                </div>
                <div style="display:flex;flex-direction:column;gap:8px">`;
            for (const item of appUsageItems) {
                const pct = maxBytes > 0 ? Math.max(2, Math.round(item.bytes * 100 / maxBytes)) : 0;
                const onData = item.partition !== '/';
                html += `<div style="font-size:12px">
                    <div style="display:flex;justify-content:space-between;margin-bottom:3px">
                        <span style="color:var(--text-primary)" title="${item.path}">${item.label}
                            <span style="color:var(--text-muted);font-size:10px;margin-left:4px">${item.partition}</span>
                            ${onData ? '<span style="color:var(--accent);font-size:10px;margin-left:4px">✓ data</span>' : ''}
                        </span>
                        <span style="color:var(--text-secondary);font-weight:600">${fmtBytes(item.bytes)}</span>
                    </div>
                    <div style="background:var(--bg-primary);border-radius:4px;height:6px;overflow:hidden">
                        <div style="width:${pct}%;height:100%;background:${onData ? 'var(--accent)' : '#eab308'};border-radius:4px;transition:width .3s"></div>
                    </div>
                </div>`;
            }
            html += `</div></div>`;
        }

        // ── /tmp cleanup card ──
        {
            const tmpItem = appUsageItems.find(i => i.path === '/tmp');
            const tmpBytes = tmpItem ? tmpItem.bytes : 0;
            const tmpStr = tmpBytes >= 1073741824 ? (tmpBytes/1073741824).toFixed(1)+' GB'
                         : tmpBytes >= 1048576 ? (tmpBytes/1048576).toFixed(0)+' MB'
                         : (tmpBytes/1024).toFixed(0)+' KB';
            const tmpWarn = tmpBytes >= 1073741824;
            html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px${tmpWarn ? ';border-left:3px solid #ef4444' : ''}">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
                    <div style="font-size:13px;font-weight:600">
                        <i class="fas fa-broom" style="color:${tmpWarn ? '#ef4444' : 'var(--accent)'};margin-right:6px"></i>${t('Czyszczenie /tmp')}
                        <span style="font-weight:400;font-size:12px;color:var(--text-muted);margin-left:8px">${t('Zajęte')}: <strong style="color:${tmpWarn ? '#ef4444' : 'var(--text-primary)'}">${tmpStr}</strong></span>
                    </div>
                    <div style="display:flex;gap:6px;align-items:center">
                        <select id="mnt-tmp-age" style="padding:4px 8px;border-radius:6px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary);font-size:12px">
                            <option value="0">${t('Wszystkie pliki')}</option>
                            <option value="60" selected>${t('Starsze niż 1h')}</option>
                            <option value="1440">${t('Starsze niż 24h')}</option>
                            <option value="10080">${t('Starsze niż 7 dni')}</option>
                        </select>
                        <button class="dr-btn dr-btn-sm" id="mnt-clean-tmp"><i class="fas fa-broom"></i> ${t('Wyczyść')}</button>
                    </div>
                </div>
                <div id="mnt-tmp-details" style="font-size:12px;color:var(--text-muted)"></div>
            </div>`;
        }

        // Active tasks with progress bars
        if (active.length) {
            html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px;border-left:3px solid var(--accent)">
                <div style="font-size:13px;font-weight:600;margin-bottom:10px"><i class="fas fa-cog fa-spin" style="color:var(--accent);margin-right:6px"></i>${t('Aktywne zadania')}</div>`;
            for (const task of active) {
                const pct = task.progress != null ? Math.round(task.progress) : null;
                html += `<div style="padding:10px;background:var(--bg-primary);border-radius:6px;margin-bottom:6px;font-size:13px">
                    <div style="display:flex;align-items:center;gap:10px;margin-bottom:${pct != null ? '8' : '0'}px">
                        <i class="fas fa-spinner fa-spin" style="color:var(--accent)"></i>
                        <span style="flex:1"><strong>${task.type}</strong> — ${escHtml(task.target)}</span>
                        ${pct != null ? `<span style="font-weight:600;color:var(--accent)">${pct}%</span>` : ''}
                        ${task.rate ? `<span style="color:var(--text-muted);font-size:11px">${escHtml(task.rate)}</span>` : ''}
                    </div>
                    ${pct != null ? `<div style="background:var(--bg-secondary);border-radius:4px;height:6px;overflow:hidden">
                        <div style="width:${pct}%;height:100%;background:var(--accent);border-radius:4px;transition:width .5s"></div>
                    </div>` : ''}
                </div>`;
            }
            html += '</div>';
        }

        // Btrfs Scrub section — enhanced with last scrub info and schedules
        html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px">
            <div style="font-size:13px;font-weight:700;margin-bottom:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.3px">
                <i class="fas fa-search" style="margin-right:6px"></i>Btrfs Scrub
                <span style="font-weight:400;font-size:11px;text-transform:none;margin-left:8px;color:var(--text-muted)">${t('Weryfikacja integralności danych')}</span>
            </div>`;
        if (btrfsPools.length) {
            for (const p of btrfsPools) {
                const mount = p.mount_path;
                const si = scrubByMount[mount] || {};
                const sched = schedByKey['scrub|' + mount];
                const isRunning = active.some(t2 => t2.type === 'scrub' && t2.target === mount);
                const hasErrors = si.errors && si.errors !== 'no errors found';
                const lastDate = si.started || null;
                const statusColor = isRunning ? 'var(--accent)' : hasErrors ? '#ef4444' : si.status === 'finished' ? '#10b981' : 'var(--text-muted)';
                const statusIcon = isRunning ? 'fa-spinner fa-spin' : hasErrors ? 'fa-exclamation-triangle' : si.status === 'finished' ? 'fa-check-circle' : 'fa-minus-circle';
                const statusText = isRunning ? t('W trakcie...') : hasErrors ? t('Wykryto błędy') : si.status === 'finished' ? t('OK') : si.status === 'aborted' ? t('Przerwany') : t('Nigdy');

                html += `<div style="background:var(--bg-primary);border-radius:8px;padding:14px;margin-bottom:8px;border-left:3px solid ${statusColor}">
                    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                        <i class="fas fa-layer-group" style="color:#3b82f6;width:20px;text-align:center"></i>
                        <span style="flex:1;font-size:13px;font-weight:600">${escHtml(p.name)} <span style="color:var(--text-muted);font-weight:400">${escHtml(mount)}</span></span>
                        ${sched ? `<span style="background:#3b82f620;color:#3b82f6;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600"><i class="fas fa-calendar-check" style="margin-right:3px"></i>${sched.frequency}</span>` : ''}
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;font-size:12px;margin-bottom:10px">
                        <div>
                            <div style="color:var(--text-muted);margin-bottom:2px">Status</div>
                            <div style="color:${statusColor};font-weight:600"><i class="fas ${statusIcon}" style="margin-right:4px"></i>${statusText}</div>
                        </div>
                        <div>
                            <div style="color:var(--text-muted);margin-bottom:2px">${t('Ostatni scrub')}</div>
                            <div style="font-weight:500">${lastDate || '—'}</div>
                        </div>
                        ${si.duration ? `<div>
                            <div style="color:var(--text-muted);margin-bottom:2px">${t('Czas trwania')}</div>
                            <div style="font-weight:500">${si.duration}</div>
                        </div>` : ''}
                        ${si.rate ? `<div>
                            <div style="color:var(--text-muted);margin-bottom:2px">${t('Prędkość')}</div>
                            <div style="font-weight:500">${si.rate}</div>
                        </div>` : ''}
                        ${si.total_str ? `<div>
                            <div style="color:var(--text-muted);margin-bottom:2px">${t('Rozmiar')}</div>
                            <div style="font-weight:500">${si.total_str}</div>
                        </div>` : ''}
                        ${si.errors ? `<div>
                            <div style="color:var(--text-muted);margin-bottom:2px">${t('Błędy')}</div>
                            <div style="font-weight:600;color:${hasErrors ? '#ef4444' : '#10b981'}">${hasErrors ? '<i class="fas fa-exclamation-triangle"></i> ' : '<i class="fas fa-check"></i> '}${escHtml(si.errors)}</div>
                        </div>` : ''}
                    </div>
                    <div style="display:flex;gap:6px;align-items:center">
                        <button class="dr-btn dr-btn-sm" data-scrub="${escHtml(mount)}" ${isRunning ? 'disabled' : ''}>
                            <i class="fas ${isRunning ? 'fa-spinner fa-spin' : 'fa-play'}"></i> ${isRunning ? t('W trakcie...') : t('Uruchom')}
                        </button>
                        ${sched
                            ? `<button class="dr-btn dr-btn-sm dr-btn-outline" data-unsched-scrub="${escHtml(mount)}" title="${t('Usuń harmonogram')}"><i class="fas fa-calendar-times"></i> ${t('Wyłącz')}</button>`
                            : `<button class="dr-btn dr-btn-sm dr-btn-outline" data-sched-scrub="${escHtml(mount)}" title="${t('Zaplanuj automatyczny scrub')}"><i class="fas fa-calendar-plus"></i> ${t('Zaplanuj')}</button>`
                        }
                    </div>
                </div>`;
            }
        } else {
            html += `<div style="color:var(--text-muted);font-size:13px;padding:8px 0">${t('Brak pul btrfs. Scrub dostępny tylko dla btrfs.')}</div>`;
        }
        html += '</div>';

        // RAID Parity Check section
        html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px">
            <div style="font-size:13px;font-weight:700;margin-bottom:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.3px">
                <i class="fas fa-server" style="margin-right:6px"></i>${t('Sprawdzenie RAID')}
            </div>`;
        if (raidArrays.length) {
            for (const a of raidArrays) {
                const name = a.name || a.device;
                const mdName = (name || '').replace('/dev/', '');
                const raidSched = schedByKey['raid-check|' + mdName];
                const isRunning = active.some(t2 => t2.type === 'raid-check' && t2.target === mdName);
                html += `<div style="display:flex;align-items:center;gap:10px;padding:10px;background:var(--bg-primary);border-radius:6px;margin-bottom:6px">
                    <i class="fas fa-server" style="color:#8b5cf6;width:20px;text-align:center"></i>
                    <span style="flex:1;font-size:13px"><strong>${escHtml(mdName)}</strong> <span style="color:var(--text-muted)">${escHtml(a.level || '')} — ${(a.members || []).length} ${t('dysków')}</span></span>
                    ${raidSched ? `<span style="background:#8b5cf620;color:#8b5cf6;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600"><i class="fas fa-calendar-check" style="margin-right:3px"></i>${raidSched.frequency}</span>` : ''}
                    <button class="dr-btn dr-btn-sm" data-raidcheck="${escHtml(mdName)}" ${isRunning ? 'disabled' : ''}><i class="fas ${isRunning ? 'fa-spinner fa-spin' : 'fa-play'}"></i> ${t('Sprawdź')}</button>
                    ${raidSched
                        ? `<button class="dr-btn dr-btn-sm dr-btn-outline" data-unsched-raid="${escHtml(mdName)}"><i class="fas fa-calendar-times"></i></button>`
                        : `<button class="dr-btn dr-btn-sm dr-btn-outline" data-sched-raid="${escHtml(mdName)}"><i class="fas fa-calendar-plus"></i></button>`
                    }
                </div>`;
            }
        } else {
            html += `<div style="color:var(--text-muted);font-size:13px;padding:8px 0">${t('Brak macierzy RAID.')}</div>`;
        }
        html += '</div>';

        // SSD TRIM section
        html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px">
            <div style="font-size:13px;font-weight:700;margin-bottom:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.3px">
                <i class="fas fa-bolt" style="margin-right:6px"></i>SSD TRIM
            </div>`;
        if (mountsForTrim.length) {
            for (const m of mountsForTrim) {
                const trimSched = schedByKey['trim|' + m.path];
                const isRunning = active.some(t2 => t2.type === 'trim' && t2.target === m.path);
                html += `<div style="display:flex;align-items:center;gap:10px;padding:10px;background:var(--bg-primary);border-radius:6px;margin-bottom:6px">
                    <i class="fas fa-hdd" style="color:#f59e0b;width:20px;text-align:center"></i>
                    <span style="flex:1;font-size:13px"><strong>${escHtml(m.name)}</strong> <span style="color:var(--text-muted)">${escHtml(m.path)}</span></span>
                    ${trimSched ? `<span style="background:#f59e0b20;color:#f59e0b;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600"><i class="fas fa-calendar-check" style="margin-right:3px"></i>${trimSched.frequency}</span>` : ''}
                    <button class="dr-btn dr-btn-sm" data-trim="${escHtml(m.path)}" ${isRunning ? 'disabled' : ''}><i class="fas ${isRunning ? 'fa-spinner fa-spin' : 'fa-play'}"></i> TRIM</button>
                    ${trimSched
                        ? `<button class="dr-btn dr-btn-sm dr-btn-outline" data-unsched-trim="${escHtml(m.path)}"><i class="fas fa-calendar-times"></i></button>`
                        : `<button class="dr-btn dr-btn-sm dr-btn-outline" data-sched-trim="${escHtml(m.path)}"><i class="fas fa-calendar-plus"></i></button>`
                    }
                </div>`;
            }
        } else {
            html += `<div style="color:var(--text-muted);font-size:13px;padding:8px 0">${t('Brak zamontowanych pul.')}</div>`;
        }
        html += '</div>';

        // History section
        if (history.length) {
            html += `<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px">
                <div style="font-size:13px;font-weight:700;margin-bottom:12px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.3px">
                    <i class="fas fa-clock-rotate-left" style="margin-right:6px"></i>${t('Historia konserwacji')}
                </div>
                <table style="width:100%;border-collapse:collapse;font-size:12px">
                <thead><tr>
                    <th style="text-align:left;padding:6px 8px;border-bottom:2px solid var(--border-color)">${t('Typ')}</th>
                    <th style="text-align:left;padding:6px 8px;border-bottom:2px solid var(--border-color)">${t('Cel')}</th>
                    <th style="text-align:left;padding:6px 8px;border-bottom:2px solid var(--border-color)">Status</th>
                    <th style="text-align:left;padding:6px 8px;border-bottom:2px solid var(--border-color)">${t('Czas')}</th>
                    <th style="text-align:left;padding:6px 8px;border-bottom:2px solid var(--border-color)">${t('Data')}</th>
                </tr></thead><tbody>`;
            for (const h of history) {
                const ok = h.status === 'completed';
                const clr = ok ? '#10b981' : h.status === 'running' ? 'var(--accent)' : '#ef4444';
                const dur = h.duration_sec ? (h.duration_sec < 60 ? Math.round(h.duration_sec) + 's' : Math.round(h.duration_sec / 60) + ' min') : '—';
                html += `<tr>
                    <td style="padding:6px 8px;border-bottom:1px solid var(--border-color)">${escHtml(h.type)}</td>
                    <td style="padding:6px 8px;border-bottom:1px solid var(--border-color);color:var(--text-secondary)">${escHtml(h.target)}</td>
                    <td style="padding:6px 8px;border-bottom:1px solid var(--border-color);color:${clr}">${h.status === 'completed' ? '✓' : h.status === 'running' ? '⟳' : '✗'} ${h.status}</td>
                    <td style="padding:6px 8px;border-bottom:1px solid var(--border-color)">${dur}</td>
                    <td style="padding:6px 8px;border-bottom:1px solid var(--border-color);color:var(--text-muted)">${h.started ? new Date(h.started).toLocaleString() : '—'}</td>
                </tr>`;
            }
            html += '</tbody></table></div>';
        }

        root.innerHTML = html;

        // Wire up action buttons
        async function startMaint(type, target) {
            const res = await api('/storage/maintenance/start', { method: 'POST', body: { type, target } });
            if (res.error) { toast(res.error, 'error'); return; }
            toast(res.message || t('Zadanie uruchomione'), 'success');
            startPoll();
            setTimeout(load, 2000);
        }

        async function scheduleMaint(type, target) {
            const modal = document.createElement('div');
            modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:10000';
            modal.innerHTML = `<div style="background:var(--bg-secondary);border-radius:12px;padding:24px;width:320px">
                <h3 style="margin:0 0 16px;font-size:16px">${t('Zaplanuj')} ${type}</h3>
                <div style="margin-bottom:16px">
                    <label style="font-size:12px;color:var(--text-secondary);display:block;margin-bottom:4px">${t('Częstotliwość')}</label>
                    <select id="mnt-freq" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border-color);background:var(--bg-primary);color:var(--text-primary)">
                        <option value="weekly">${t('Co tydzień')}</option>
                        <option value="daily">${t('Codziennie')}</option>
                        <option value="monthly">${t('Co miesiąc')}</option>
                    </select>
                </div>
                <div style="display:flex;gap:8px;justify-content:flex-end">
                    <button class="dr-btn dr-btn-outline" id="mnt-cancel">${t('Anuluj')}</button>
                    <button class="dr-btn" id="mnt-save"><i class="fas fa-check"></i> ${t('Zapisz')}</button>
                </div>
            </div>`;
            document.body.appendChild(modal);
            modal.querySelector('#mnt-cancel').onclick = () => modal.remove();
            modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
            modal.querySelector('#mnt-save').onclick = async () => {
                const freq = modal.querySelector('#mnt-freq').value;
                const res = await api('/storage/maintenance/schedule', {
                    method: 'POST', body: { type, target, frequency: freq },
                });
                modal.remove();
                if (res.error) toast(res.error, 'error');
                else toast(res.message || t('Zaplanowano'), 'success');
            };
        }

        root.querySelectorAll('[data-scrub]').forEach(b => b.onclick = () => startMaint('scrub', b.dataset.scrub));
        root.querySelectorAll('[data-raidcheck]').forEach(b => b.onclick = () => startMaint('raid-check', b.dataset.raidcheck));
        root.querySelectorAll('[data-trim]').forEach(b => b.onclick = () => startMaint('trim', b.dataset.trim));
        root.querySelectorAll('[data-sched-scrub]').forEach(b => b.onclick = () => scheduleMaint('scrub', b.dataset.schedScrub));
        root.querySelectorAll('[data-sched-raid]').forEach(b => b.onclick = () => scheduleMaint('raid-check', b.dataset.schedRaid));
        root.querySelectorAll('[data-sched-trim]').forEach(b => b.onclick = () => scheduleMaint('trim', b.dataset.schedTrim));

        // Unschedule buttons
        root.querySelectorAll('[data-unsched-scrub]').forEach(b => b.onclick = async () => {
            const res = await api('/storage/maintenance/schedule', { method: 'DELETE', body: { type: 'scrub', target: b.dataset.unschedScrub } });
            if (res.error) toast(res.error, 'error');
            else { toast(t('Harmonogram usunięty'), 'success'); load(); }
        });
        root.querySelectorAll('[data-unsched-raid]').forEach(b => b.onclick = async () => {
            const res = await api('/storage/maintenance/schedule', { method: 'DELETE', body: { type: 'raid-check', target: b.dataset.unschedRaid } });
            if (res.error) toast(res.error, 'error');
            else { toast(t('Harmonogram usunięty'), 'success'); load(); }
        });
        root.querySelectorAll('[data-unsched-trim]').forEach(b => b.onclick = async () => {
            const res = await api('/storage/maintenance/schedule', { method: 'DELETE', body: { type: 'trim', target: b.dataset.unschedTrim } });
            if (res.error) toast(res.error, 'error');
            else { toast(t('Harmonogram usunięty'), 'success'); load(); }
        });

        // /tmp cleanup: load details + wire button
        const tmpDetailsEl = root.querySelector('#mnt-tmp-details');
        const tmpBtn = root.querySelector('#mnt-clean-tmp');
        const tmpAgeSel = root.querySelector('#mnt-tmp-age');
        if (tmpDetailsEl) {
            api('/storage/clean-tmp').then(info => {
                if (!info || info.error) return;
                const fmtB = b => b >= 1073741824 ? (b/1073741824).toFixed(1)+' GB' : b >= 1048576 ? (b/1048576).toFixed(0)+' MB' : (b/1024).toFixed(0)+' KB';
                const top = (info.items || []).slice(0, 8);
                if (top.length) {
                    tmpDetailsEl.innerHTML = `<div style="margin-bottom:4px">${t('Tmpfs')}: ${fmtB(info.used)} / ${fmtB(info.total)} (${info.pct}%)</div>`
                        + `<div style="display:flex;flex-wrap:wrap;gap:4px">`
                        + top.map(f => `<span style="background:var(--bg-primary);padding:2px 8px;border-radius:4px;font-size:11px" title="${f.name}">${f.is_dir ? '<i class="fas fa-folder" style="margin-right:3px;color:var(--accent)"></i>' : ''}${escHtml(f.name.length > 30 ? f.name.slice(0,27)+'…' : f.name)} <b>${fmtB(f.bytes)}</b></span>`).join('')
                        + `</div>`;
                }
            });
        }
        if (tmpBtn) {
            tmpBtn.onclick = async () => {
                const age = parseInt(tmpAgeSel?.value || '60', 10);
                const label = age === 0 ? t('WSZYSTKIE pliki') : tmpAgeSel.options[tmpAgeSel.selectedIndex].text;
                if (!confirm(t('Czy na pewno wyczyścić /tmp?') + '\n' + label)) return;
                tmpBtn.disabled = true;
                tmpBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Czyszczenie...');
                const res = await api('/storage/clean-tmp', { method: 'POST', body: { max_age_minutes: age } });
                if (res.error) { toast(res.error, 'error'); }
                else {
                    const fmtB = b => b >= 1073741824 ? (b/1073741824).toFixed(1)+' GB' : b >= 1048576 ? (b/1048576).toFixed(0)+' MB' : (b/1024).toFixed(0)+' KB';
                    toast(t('Usunięto') + ` ${res.removed} ` + t('elementów') + `, ${t('zwolniono')} ${fmtB(res.freed)}`, 'success');
                }
                setTimeout(load, 1500);
            };
        }
    }

    function startPoll() {
        stopPoll();
        pollTimer = setInterval(async () => {
            const res = await api('/storage/maintenance/status');
            const tasks = (res.tasks || []).filter(t2 => t2.status === 'running');
            if (tasks.length === 0) { stopPoll(); load(); }
        }, 5000);
    }
    function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

    load();
    return () => stopPoll();
}

/* ═══════════════════════════════════════════════════════════
   Section: SSD Cache (bcache management)
   ═══════════════════════════════════════════════════════════ */
function _smCache(el) {
    el.innerHTML = `<div style="padding:20px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
            <h3 style="margin:0;font-size:16px;font-weight:600;color:var(--text-primary)"><i class="fas fa-bolt" style="color:var(--accent);margin-right:6px"></i>SSD Cache</h3>
            <button class="fm-toolbar-btn btn-sm" id="sc-refresh"><i class="fas fa-sync-alt"></i> ${t('Odśwież')}</button>
        </div>
        <div id="sc-content"><div class="sto-center-lg"><i class="fas fa-spinner fa-spin sto-spinner"></i></div></div>
    </div>`;

    const contentEl = el.querySelector('#sc-content');
    el.querySelector('#sc-refresh').onclick = loadCache;

    async function loadCache() {
        contentEl.innerHTML = '<div class="sto-center-lg"><i class="fas fa-spinner fa-spin sto-spinner"></i></div>';
        try {
            const [statusData, devData] = await Promise.all([
                api('/cache/status'),
                api('/cache/devices'),
            ]);
            const live = (statusData && statusData.live) || [];
            const configured = (statusData && statusData.configured) || [];
            const ssds = (devData && devData.ssds) || [];
            const hdds = (devData && devData.hdds) || [];

            let html = '';

            /* Active caches */
            if (configured.length) {
                html += '<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px;margin-bottom:16px">';
                html += '<h4 style="margin:0 0 12px;font-size:14px;font-weight:600"><i class="fas fa-bolt" style="color:var(--accent);margin-right:6px"></i>' + t('Aktywne cache') + '</h4>';
                for (const c of configured) {
                    const backing = live.flatMap(l => l.backing_devices || []).find(b => b.device === c.backing_device);
                    const hitRate = backing ? (backing.hit_ratio !== null ? backing.hit_ratio + '%' : '—') : '—';
                    const dirty = backing ? (backing.dirty_data || '0') : '0';
                    html += `<div style="display:flex;align-items:center;gap:12px;padding:10px;border:1px solid var(--border-color);border-radius:8px;margin-bottom:6px;flex-wrap:wrap">
                        <div style="flex:1;min-width:200px">
                            <b>${c.cache_device || '?'}</b> <span style="color:var(--text-secondary)">→</span> <b>${c.backing_device || '?'}</b>
                            <span style="margin-left:8px;font-size:12px;color:var(--text-secondary)">${c.mode || '?'}</span>
                            <span style="margin-left:12px;font-size:12px">${t('Trafienia')}: ${hitRate} | ${t('Brudne dane')}: ${dirty}</span>
                        </div>
                        <select class="modal-input sc-mode-sel" data-backing="${c.backing_device}" style="width:140px">
                            ${['writethrough','writeback','writearound'].map(m =>
                                `<option value="${m}" ${m === c.mode ? 'selected' : ''}>${m}</option>`
                            ).join('')}
                        </select>
                        <button class="fm-toolbar-btn btn-sm btn-red sc-detach" data-backing="${c.backing_device}"><i class="fas fa-unlink"></i> ${t('Odłącz')}</button>
                    </div>`;
                }
                html += '</div>';
            }

            /* Create new cache */
            const hasDevices = ssds.length > 0 && hdds.length > 0;
            if (hasDevices) {
                const ssdOpts = ssds.map(s => `<option value="${s.device}">${s.name} — ${s.size} ${s.model || ''}</option>`).join('');
                const hddOpts = hdds.map(h => `<option value="${h.device}">${h.name} — ${h.size} ${h.model || ''}</option>`).join('');
                html += '<div style="background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:16px">';
                html += '<h4 style="margin:0 0 12px;font-size:14px;font-weight:600"><i class="fas fa-plus-circle" style="color:#2ecc71;margin-right:6px"></i>' + t('Nowy cache') + '</h4>';
                html += `<div style="display:flex;gap:12px;align-items:end;flex-wrap:wrap">
                    <div><label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">${t('Dysk SSD (cache)')}</label>
                    <select id="sc-ssd" class="modal-input" style="min-width:160px">${ssdOpts}</select></div>
                    <div><label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">${t('Dysk HDD (backing)')}</label>
                    <select id="sc-hdd" class="modal-input" style="min-width:160px">${hddOpts}</select></div>
                    <div><label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:4px">${t('Tryb')}</label>
                    <select id="sc-mode" class="modal-input"><option value="writethrough">${t('Write-through (bezpieczny)')}</option><option value="writeback">${t('Write-back (szybszy)')}</option><option value="writearound">${t('Write-around')}</option></select></div>
                    <button class="fm-toolbar-btn" id="sc-create"><i class="fas fa-plus"></i> ${t('Utwórz')}</button>
                </div>`;
                html += `<div style="margin-top:10px;padding:8px;background:rgba(239,68,68,.08);border-radius:6px;font-size:12px;color:#e74c3c"><i class="fas fa-exclamation-triangle"></i> <b>${t('UWAGA:')}</b> ${t('Dane na obu dyskach zostaną usunięte!')}</div>`;
                html += '</div>';
            } else if (!configured.length) {
                html = '<div style="text-align:center;padding:40px;color:var(--text-secondary)"><i class="fas fa-info-circle" style="font-size:24px;margin-bottom:8px"></i><div>' + t('Brak dostępnych dysków SSD lub HDD do konfiguracji cache.') + '</div></div>';
            }

            contentEl.innerHTML = html;

            /* Bind event handlers */
            setTimeout(() => {
                el.querySelectorAll('.sc-detach').forEach(btn => {
                    btn.onclick = async () => {
                        const backing = btn.dataset.backing;
                        if (!await confirmDialog(t('Odłączyć SSD cache od') + ' ' + backing + '?')) return;
                        const r = await api('/cache/detach', { method: 'POST', body: { backing_device: backing } });
                        if (r.error) { toast(r.error, 'error'); return; }
                        toast(t('Cache odłączony'), 'success'); loadCache();
                    };
                });
                el.querySelectorAll('.sc-mode-sel').forEach(sel => {
                    sel.onchange = async () => {
                        const r = await api('/cache/mode', { method: 'PUT', body: { backing_device: sel.dataset.backing, mode: sel.value } });
                        if (r.error) { toast(r.error, 'error'); return; }
                        toast(t('Tryb zmieniony: ') + sel.value, 'success');
                    };
                });
                const createBtn = el.querySelector('#sc-create');
                if (createBtn) createBtn.onclick = async () => {
                    const ssd = el.querySelector('#sc-ssd')?.value;
                    const hdd = el.querySelector('#sc-hdd')?.value;
                    const mode = el.querySelector('#sc-mode')?.value;
                    if (!ssd || !hdd) { toast(t('Wybierz oba dyski'), 'warning'); return; }
                    if (ssd === hdd) { toast(t('SSD i HDD muszą być różnymi dyskami'), 'warning'); return; }
                    if (!await confirmDialog(t('Utworzyć SSD cache? Dane na obu dyskach zostaną usunięte!'))) return;
                    const r = await api('/cache/create', { method: 'POST', body: { cache_device: ssd, backing_device: hdd, mode } });
                    if (r.error) { toast(r.error, 'error'); return; }
                    toast(t('SSD Cache utworzony!'), 'success'); loadCache();
                };
            }, 50);
        } catch (e) {
            contentEl.innerHTML = '<div style="color:#e74c3c;padding:20px"><i class="fas fa-exclamation-triangle"></i> ' + (e.message || t('Błąd ładowania')) + '</div>';
        }
    }
    loadCache();
    return null;
}

/* ═══════════════════════════════════════════════════════════
   Section: Storage Pool Wizard
   ═══════════════════════════════════════════════════════════ */
function _smWizard(el, switchSectionFn) {
    const state = {
        step: 0,
        disks: [],
        selectedDisks: [],
        raidLevel: null,
        poolName: 'Storage-1',
        fstype: 'ext4',
        mountPath: '/mnt/Storage-1',
        samba: true,
        sambaName: 'Storage-1',
        creating: false,
    };

    const STEPS = [
        { icon: 'fa-hdd',          label: t('Dyski') },
        { icon: 'fa-shield-alt',   label: t('Ochrona') },
        { icon: 'fa-cog',          label: t('Ustawienia') },
        { icon: 'fa-check-circle', label: t('Potwierdzenie') },
        { icon: 'fa-rocket',       label: t('Tworzenie') },
    ];

    const RAID_INFO = {
        '0':  { label: 'RAID 0 — Stripe',           desc: t('Maksymalna szybkość, brak ochrony. Awaria 1 dysku = utrata wszystkich danych.'), min: 2, icon: 'fa-bolt',       color: '#ef4444' },
        '1':  { label: 'RAID 1 — Mirror',            desc: t('Pełna kopia na każdym dysku. Najlepsza ochrona, ale pojemność = 1 dysk.'), min: 2, icon: 'fa-clone',      color: '#10b981' },
        '5':  { label: 'RAID 5 — Parzystość',        desc: t('Dobry balans: szybkość + ochrona. Wytrzymuje awarię 1 dysku.'), min: 3, icon: 'fa-shield-alt', color: '#3b82f6' },
        '6':  { label: 'RAID 6 — Podwójna parzystość', desc: t('Jak RAID 5, ale wytrzymuje awarię 2 dysków jednocześnie.'), min: 4, icon: 'fa-shield-alt', color: '#8b5cf6' },
        '10': { label: 'RAID 10 — Mirror + Stripe',  desc: t('Najszybszy z ochroną. Mirror + stripe. Wytrzymuje awarię 1 dysku w każdej parze.'), min: 4, icon: 'fa-server',     color: '#f59e0b' },
    };

    function calcCapacity(diskCount, raidLevel, minSizeBytes) {
        if (!raidLevel) return minSizeBytes;
        switch (raidLevel) {
            case '0': return minSizeBytes * diskCount;
            case '1': return minSizeBytes;
            case '5': return minSizeBytes * (diskCount - 1);
            case '6': return minSizeBytes * (diskCount - 2);
            case '10': return minSizeBytes * Math.floor(diskCount / 2);
            default: return minSizeBytes;
        }
    }

    function fmtBytes(b) {
        if (b <= 0) return '0 B';
        const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
        const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), u.length - 1);
        return (b / Math.pow(1024, i)).toFixed(i > 1 ? 1 : 0) + ' ' + u[i];
    }

    function renderSteps() {
        return '<div class="spw-steps">' +
            STEPS.map((s, i) => {
                const cls = i < state.step ? 'spw-step-done' : (i === state.step ? 'spw-step-active' : 'spw-step-pending');
                const icon = i < state.step ? 'fa-check' : s.icon;
                return `<div class="spw-step ${cls}"><i class="fas ${icon}"></i><span>${s.label}</span></div>` +
                    (i < STEPS.length - 1 ? '<div class="spw-connector"></div>' : '');
            }).join('') +
        '</div>';
    }

    function render() {
        let bodyHtml = '';
        switch (state.step) {
            case 0: bodyHtml = renderStep0(); break;
            case 1: bodyHtml = renderStep1(); break;
            case 2: bodyHtml = renderStep2(); break;
            case 3: bodyHtml = renderStep3(); break;
            case 4: bodyHtml = renderStep4(); break;
        }

        el.innerHTML =
            '<div class="spw-wizard">' +
                '<div class="spw-header"><i class="fas fa-magic"></i> ' + t('Kreator puli storage') + '</div>' +
                renderSteps() +
                '<div class="spw-body">' + bodyHtml + '</div>' +
            '</div>';

        attachHandlers();
    }

    // ── Step 0: Select disks ──
    function renderStep0() {
        if (!state.disks.length && !state._loadingDisks && !state._loadError) {
            state._loadingDisks = true;
            api('/storage/pool/available-disks').then(data => {
                state._loadingDisks = false;
                if (data.error) { state._loadError = data.error; }
                else { state.disks = data.disks || []; }
                render();
            }).catch(() => {
                state._loadingDisks = false;
                state._loadError = t('Nie udało się pobrać listy dysków.');
                render();
            });
            return '<div class="spw-loading"><i class="fas fa-spinner fa-spin fa-2x"></i><div>' + t('Szukam dostępnych dysków...') + '</div></div>';
        }

        if (state._loadError) {
            return '<div class="spw-empty"><i class="fas fa-exclamation-triangle fa-2x" style="color:#ef4444"></i>' +
                '<div style="margin-top:12px">' + state._loadError + '</div>' +
                '<div style="margin-top:16px"><button class="spw-btn spw-btn-primary" id="spw-retry-disks"><i class="fas fa-redo"></i> ' + t('Spróbuj ponownie') + '</button></div></div>';
        }

        if (!state.disks.length) {
            return '<div class="spw-empty"><i class="fas fa-exclamation-circle fa-2x" style="color:var(--text-muted)"></i>' +
                '<div style="margin-top:12px">' + t('Brak wolnych dysków do użycia.') + '</div>' +
                '<div style="font-size:12px;color:var(--text-muted);margin-top:4px">' + t('Odmontuj dyski lub dodaj nowe, aby kontynuować.') + '</div></div>';
        }

        let html = '<h3><i class="fas fa-hdd"></i> ' + t('Wybierz dyski') + '</h3>' +
            '<p class="spw-hint">' + t('Zaznacz dyski, które chcesz połączyć w pulę storage. Dane na wybranych dyskach zostaną usunięte.') + '</p>' +
            '<div class="spw-disk-list">';

        state.disks.forEach(d => {
            const selected = state.selectedDisks.includes(d.name);
            const isUsb = d.tran === 'usb' || d.removable;
            const tranIcon = d.tran === 'usb' ? 'fa-usb' : (d.tran === 'nvme' ? 'fa-bolt' : 'fa-hdd');
            const usbWarn = isUsb ? `<div class="spw-usb-warn"><i class="fas fa-exclamation-triangle"></i> ${t('USB — wolniejszy, może zostać odłączony. Nie zalecany do RAID.')}</div>` : '';
            html += `<div class="spw-disk-card ${selected ? 'spw-disk-selected' : ''} ${isUsb ? 'spw-disk-usb' : ''}" data-disk="${d.name}">` +
                `<div class="spw-disk-check"><i class="fas ${selected ? 'fa-check-square' : 'fa-square'}"></i></div>` +
                `<div class="spw-disk-info">` +
                    `<div class="spw-disk-name"><i class="fas ${tranIcon}"></i> /dev/${d.name}</div>` +
                    `<div class="spw-disk-detail">${d.model || t('Nieznany model')} · ${d.size}` +
                    `${d.tran ? ' · ' + d.tran.toUpperCase() : ''}` +
                    `${d.fstype ? ' · ' + d.fstype : ''}` +
                    `${d.label ? ' · "' + d.label + '"' : ''}</div>` +
                    usbWarn +
                `</div>` +
                `<div class="spw-disk-size">${d.size}</div>` +
            `</div>`;
        });

        html += '</div>';

        html += '<div class="spw-actions">' +
            `<button class="spw-btn spw-btn-primary" id="spw-next" ${state.selectedDisks.length === 0 ? 'disabled' : ''}>` +
                t('Dalej') + ' <i class="fas fa-arrow-right"></i></button>' +
            '</div>';

        return html;
    }

    // ── Step 1: RAID level ──
    function renderStep1() {
        if (state.selectedDisks.length === 1) {
            // Skip RAID selection for single disk — jump to step 2
            state.raidLevel = null;
            state.step = 2;
            return renderStep2();
        }

        // Block RAID if any selected disk is USB/removable
        const hasUsb = state.selectedDisks.some(name => {
            const d = state.disks.find(x => x.name === name);
            return d && (d.tran === 'usb' || d.removable);
        });
        if (hasUsb) {
            state.raidLevel = null;
            let html = '<h3><i class="fas fa-exclamation-triangle" style="color:var(--warn)"></i> ' + t('RAID niedostępny') + '</h3>' +
                '<div class="spw-usb-raid-block">' +
                '<p>' + t('Wybrane dyski zawierają dysk USB/wymienny. RAID wymaga stałych, niezawodnych dysków.') + '</p>' +
                '<p>' + t('Aby kontynuować:') + '</p>' +
                '<ul>' +
                '<li>' + t('Odznacz dyski USB i użyj tylko dysków wewnętrznych do RAID') + '</li>' +
                '<li>' + t('Lub utwórz osobną pulę single-disk dla każdego dysku USB') + '</li>' +
                '</ul></div>';

            html += '<div class="spw-actions">' +
                `<button class="spw-btn spw-btn-secondary" id="spw-back"><i class="fas fa-arrow-left"></i> ` + t('Wstecz') + '</button>' +
                '</div>';
            return html;
        }

        const n = state.selectedDisks.length;
        const minSize = Math.min(...state.selectedDisks.map(name => {
            const d = state.disks.find(x => x.name === name);
            return d ? d.size_bytes : 0;
        }));

        let html = '<h3><i class="fas fa-shield-alt"></i> ' + t('Typ ochrony danych') + '</h3>' +
            '<p class="spw-hint">' + t('Wybierz poziom RAID dla') + ' ' + n + ' ' + t('dysków') + '. ' +
            t('RAID używa pojemności najmniejszego dysku') + ' (' + fmtBytes(minSize) + ').</p>' +
            '<div class="spw-raid-list">';

        Object.entries(RAID_INFO).forEach(([level, info]) => {
            const disabled = n < info.min;
            const selected = state.raidLevel === level;
            const capacity = calcCapacity(n, level, minSize);

            html += `<div class="spw-raid-card ${selected ? 'spw-raid-selected' : ''} ${disabled ? 'spw-raid-disabled' : ''}" ` +
                `data-level="${level}" ${disabled ? '' : ''}>` +
                `<div class="spw-raid-icon" style="color:${disabled ? 'var(--text-muted)' : info.color}"><i class="fas ${info.icon}"></i></div>` +
                `<div class="spw-raid-info">` +
                    `<div class="spw-raid-label">${info.label}</div>` +
                    `<div class="spw-raid-desc">${info.desc}</div>` +
                    `${disabled ? '<div class="spw-raid-warn"><i class="fas fa-exclamation-triangle"></i> ' + t('Min.') + ' ' + info.min + ' ' + t('dysków') + '</div>' : ''}` +
                `</div>` +
                `<div class="spw-raid-cap">${disabled ? '—' : fmtBytes(capacity)}</div>` +
            `</div>`;
        });

        html += '</div>';

        html += '<div class="spw-actions">' +
            `<button class="spw-btn spw-btn-secondary" id="spw-back"><i class="fas fa-arrow-left"></i> ` + t('Wstecz') + '</button>' +
            `<button class="spw-btn spw-btn-primary" id="spw-next" ${!state.raidLevel ? 'disabled' : ''}>` +
                t('Dalej') + ' <i class="fas fa-arrow-right"></i></button>' +
            '</div>';

        return html;
    }

    // ── Step 2: Configuration ──
    function renderStep2() {
        let html = '<h3><i class="fas fa-cog"></i> ' + t('Konfiguracja puli') + '</h3>' +

        '<div class="spw-form">' +
            '<div class="spw-field">' +
                '<label>' + t('Nazwa puli') + '</label>' +
                `<input type="text" id="spw-pool-name" class="spw-input" value="${state.poolName}" placeholder="Storage-1">` +
            '</div>' +

            '<div class="spw-field">' +
                '<label>' + t('System plików') + '</label>' +
                '<div class="spw-radio-group">' +
                    `<label class="spw-radio ${state.fstype === 'ext4' ? 'spw-radio-active' : ''}">` +
                        `<input type="radio" name="spw-fs" value="ext4" ${state.fstype === 'ext4' ? 'checked' : ''}> ` +
                        '<strong>ext4</strong> <span class="spw-radio-desc">' + t('Stabilny, szybki, sprawdzony') + '</span></label>' +
                    `<label class="spw-radio ${state.fstype === 'btrfs' ? 'spw-radio-active' : ''}">` +
                        `<input type="radio" name="spw-fs" value="btrfs" ${state.fstype === 'btrfs' ? 'checked' : ''}> ` +
                        '<strong>btrfs</strong> <span class="spw-radio-desc">' + t('Snapshoty, kompresja, nowoczesny') + '</span></label>' +
                '</div>' +
            '</div>' +

            '<div class="spw-field">' +
                '<label>' + t('Punkt montowania') + '</label>' +
                `<input type="text" id="spw-mount" class="spw-input" value="${state.mountPath}" placeholder="/mnt/Storage-1">` +
            '</div>' +

            '<div class="spw-field">' +
                '<label class="spw-checkbox-label">' +
                    `<input type="checkbox" id="spw-samba" ${state.samba ? 'checked' : ''}> ` +
                    '<i class="fab fa-windows" style="margin:0 4px"></i> ' +
                    t('Utwórz udział Samba (sieciowy)') +
                '</label>' +
                `<input type="text" id="spw-samba-name" class="spw-input spw-input-indent" value="${state.sambaName}" ` +
                    `placeholder="${t('Nazwa udziału')}" ${state.samba ? '' : 'disabled'}>` +
            '</div>' +
        '</div>';

        html += '<div class="spw-actions">' +
            `<button class="spw-btn spw-btn-secondary" id="spw-back"><i class="fas fa-arrow-left"></i> ` + t('Wstecz') + '</button>' +
            `<button class="spw-btn spw-btn-primary" id="spw-next">` +
                t('Dalej') + ' <i class="fas fa-arrow-right"></i></button>' +
            '</div>';

        return html;
    }

    // ── Step 3: Confirmation ──
    function renderStep3() {
        const n = state.selectedDisks.length;
        const minSize = Math.min(...state.selectedDisks.map(name => {
            const d = state.disks.find(x => x.name === name);
            return d ? d.size_bytes : 0;
        }));
        const capacity = state.raidLevel ? calcCapacity(n, state.raidLevel, minSize) : minSize;

        let html = '<h3><i class="fas fa-check-circle"></i> ' + t('Podsumowanie') + '</h3>' +

        '<div class="spw-summary">' +
            '<div class="spw-summary-row"><span>' + t('Nazwa puli') + '</span><strong>' + state.poolName + '</strong></div>' +
            '<div class="spw-summary-row"><span>' + t('Dyski') + '</span><strong>' + state.selectedDisks.map(d => '/dev/' + d).join(', ') + '</strong></div>' +
            (state.raidLevel
                ? '<div class="spw-summary-row"><span>' + t('RAID') + '</span><strong>' + RAID_INFO[state.raidLevel].label + '</strong></div>'
                : '<div class="spw-summary-row"><span>' + t('RAID') + '</span><strong>' + t('Brak (1 dysk)') + '</strong></div>') +
            '<div class="spw-summary-row"><span>' + t('System plików') + '</span><strong>' + state.fstype + '</strong></div>' +
            '<div class="spw-summary-row"><span>' + t('Pojemność') + '</span><strong>' + fmtBytes(capacity) + '</strong></div>' +
            '<div class="spw-summary-row"><span>' + t('Punkt montowania') + '</span><strong>' + state.mountPath + '</strong></div>' +
            (state.samba
                ? '<div class="spw-summary-row"><span>' + t('Samba share') + '</span><strong>' + state.sambaName + '</strong></div>'
                : '') +
        '</div>' +

        '<div class="spw-warning"><i class="fas fa-exclamation-triangle"></i> ' +
            t('WSZYSTKIE DANE na wybranych dyskach zostaną bezpowrotnie usunięte!') + '</div>';

        html += '<div class="spw-actions">' +
            `<button class="spw-btn spw-btn-secondary" id="spw-back"><i class="fas fa-arrow-left"></i> ` + t('Wstecz') + '</button>' +
            `<button class="spw-btn spw-btn-danger" id="spw-create"><i class="fas fa-rocket"></i> ` + t('Utwórz pulę') + '</button>' +
            '</div>';

        return html;
    }

    // ── Step 4: Progress ──
    function renderStep4() {
        return '<h3><i class="fas fa-rocket"></i> ' + t('Tworzenie puli...') + '</h3>' +
            '<div class="spw-progress-log" id="spw-log"></div>' +
            '<div id="spw-done-area"></div>';
    }

    function startCreation() {
        state.creating = true;
        state.step = 4;
        render();

        const logEl = el.querySelector('#spw-log');
        const doneEl = el.querySelector('#spw-done-area');

        const payload = {
            disks: state.selectedDisks,
            raid_level: state.selectedDisks.length > 1 ? state.raidLevel : null,
            fstype: state.fstype,
            name: state.poolName,
            mount_path: state.mountPath,
            samba: state.samba,
            samba_name: state.samba ? state.sambaName : '',
        };

        const token = NAS.token || '';
        const url = '/api/storage/pool/create';
        fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + token,
                'X-CSRFToken': NAS.csrfToken || '',
            },
            body: JSON.stringify(payload),
        }).then(response => {
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            function readChunk() {
                reader.read().then(({ done, value }) => {
                    if (done) {
                        state.creating = false;
                        return;
                    }
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop();

                    lines.forEach(line => {
                        if (!line.startsWith('data: ')) return;
                        try {
                            const msg = JSON.parse(line.slice(6));
                            const entry = document.createElement('div');

                            if (msg.type === 'step') {
                                entry.className = 'spw-log-step';
                                entry.innerHTML = '<i class="fas fa-check-circle"></i> ' + msg.message;
                            } else if (msg.type === 'error') {
                                entry.className = 'spw-log-error';
                                entry.innerHTML = '<i class="fas fa-times-circle"></i> ' + msg.message;
                            } else if (msg.type === 'log') {
                                entry.className = 'spw-log-line';
                                entry.textContent = msg.message;
                            } else if (msg.type === 'done') {
                                state.creating = false;
                                if (msg.success) {
                                    doneEl.innerHTML =
                                        '<div class="spw-done-success">' +
                                            '<i class="fas fa-check-circle fa-3x"></i>' +
                                            '<div class="spw-done-title">' + t('Pula gotowa!') + '</div>' +
                                            '<div class="spw-done-subtitle">"' + state.poolName + '" ' + t('została utworzona i jest dostępna.') + '</div>' +
                                            '<div class="spw-actions" style="justify-content:center">' +
                                                '<button class="spw-btn spw-btn-primary" id="spw-go-pools"><i class="fas fa-layer-group"></i> ' + t('Pokaż pulę') + '</button>' +
                                                '<button class="spw-btn spw-btn-secondary" id="spw-go-sharing"><i class="fas fa-share-alt"></i> ' + t('Udostępnij w sieci') + '</button>' +
                                                '<button class="spw-btn spw-btn-secondary" id="spw-new-pool"><i class="fas fa-plus"></i> ' + t('Utwórz kolejną') + '</button>' +
                                            '</div>' +
                                        '</div>';
                                    el.querySelector('#spw-go-pools')?.addEventListener('click', () => switchSectionFn('pools'));
                                    el.querySelector('#spw-go-sharing')?.addEventListener('click', () => switchSectionFn('sharing'));
                                    el.querySelector('#spw-new-pool')?.addEventListener('click', () => {
                                        state.step = 0; state.selectedDisks = []; state.disks = [];
                                        state.raidLevel = null; state.creating = false;
                                        render();
                                    });
                                } else {
                                    doneEl.innerHTML =
                                        '<div class="spw-done-error">' +
                                            '<i class="fas fa-times-circle fa-3x"></i>' +
                                            '<div class="spw-done-title">' + t('Tworzenie nie powiodło się') + '</div>' +
                                            '<div class="spw-actions" style="justify-content:center">' +
                                                '<button class="spw-btn spw-btn-secondary" id="spw-retry"><i class="fas fa-redo"></i> ' + t('Spróbuj ponownie') + '</button>' +
                                            '</div>' +
                                        '</div>';
                                    el.querySelector('#spw-retry')?.addEventListener('click', () => {
                                        state.step = 3; state.creating = false; render();
                                    });
                                }
                                return;
                            }

                            if (logEl) {
                                logEl.appendChild(entry);
                                logEl.scrollTop = logEl.scrollHeight;
                            }
                        } catch (_) {}
                    });

                    readChunk();
                });
            }
            readChunk();
        }).catch(err => {
            state.creating = false;
            if (logEl) {
                const entry = document.createElement('div');
                entry.className = 'spw-log-error';
                entry.textContent = t('Błąd połączenia: ') + err.message;
                logEl.appendChild(entry);
            }
        });
    }

    function attachHandlers() {
        // Retry disk loading
        const retryBtn = el.querySelector('#spw-retry-disks');
        if (retryBtn) retryBtn.addEventListener('click', () => {
            state._loadError = null;
            render();
        });

        // Disk selection
        el.querySelectorAll('.spw-disk-card').forEach(card => {
            card.addEventListener('click', () => {
                const name = card.dataset.disk;
                const idx = state.selectedDisks.indexOf(name);
                if (idx >= 0) state.selectedDisks.splice(idx, 1);
                else state.selectedDisks.push(name);
                render();
            });
        });

        // RAID level selection
        el.querySelectorAll('.spw-raid-card:not(.spw-raid-disabled)').forEach(card => {
            card.addEventListener('click', () => {
                state.raidLevel = card.dataset.level;
                render();
            });
        });

        // Navigation
        const nextBtn = el.querySelector('#spw-next');
        const backBtn = el.querySelector('#spw-back');
        const createBtn = el.querySelector('#spw-create');

        if (nextBtn) nextBtn.addEventListener('click', () => {
            // Read form values on step 2
            if (state.step === 2) {
                const nameEl = el.querySelector('#spw-pool-name');
                const mountEl = el.querySelector('#spw-mount');
                const sambaEl = el.querySelector('#spw-samba');
                const sambaNameEl = el.querySelector('#spw-samba-name');
                if (nameEl) state.poolName = nameEl.value.trim() || 'Storage-1';
                if (mountEl) state.mountPath = mountEl.value.trim() || '/mnt/Storage-1';
                if (sambaEl) state.samba = sambaEl.checked;
                if (sambaNameEl) state.sambaName = sambaNameEl.value.trim() || state.poolName;

                if (!state.poolName) { toast(t('Podaj nazwę puli'), 'error'); return; }
                if (!state.mountPath.startsWith('/')) { toast(t('Ścieżka musi zaczynać się od /'), 'error'); return; }
            }
            state.step++;
            render();
        });

        if (backBtn) backBtn.addEventListener('click', () => {
            if (state.step === 2 && state.selectedDisks.length === 1) {
                state.step = 0;
            } else {
                state.step--;
            }
            render();
        });

        if (createBtn) createBtn.addEventListener('click', () => startCreation());

        // Pool name → auto-update mount path and samba name
        const poolNameInput = el.querySelector('#spw-pool-name');
        if (poolNameInput) {
            poolNameInput.addEventListener('input', () => {
                const name = poolNameInput.value.trim();
                const safeName = name.replace(/[^a-zA-Z0-9_\-]/g, '_') || 'Storage-1';
                const mountEl = el.querySelector('#spw-mount');
                const sambaNameEl = el.querySelector('#spw-samba-name');
                if (mountEl) mountEl.value = '/mnt/' + safeName;
                if (sambaNameEl) sambaNameEl.value = name || 'Storage-1';
                state.poolName = name;
                state.mountPath = '/mnt/' + safeName;
                state.sambaName = name || 'Storage-1';
            });
        }

        // Samba checkbox toggle
        const sambaCheck = el.querySelector('#spw-samba');
        if (sambaCheck) {
            sambaCheck.addEventListener('change', () => {
                const nameInput = el.querySelector('#spw-samba-name');
                if (nameInput) nameInput.disabled = !sambaCheck.checked;
                state.samba = sambaCheck.checked;
            });
        }

        // Filesystem radio
        el.querySelectorAll('input[name="spw-fs"]').forEach(radio => {
            radio.addEventListener('change', () => {
                state.fstype = radio.value;
                el.querySelectorAll('.spw-radio').forEach(r => r.classList.toggle('spw-radio-active', r.querySelector('input').value === state.fstype));
            });
        });
    }

    render();
    return null;
}
