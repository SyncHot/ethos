/**
 * EthOS — SSH Manager
 * Centralised SSH key + known_hosts management.
 */
AppRegistry['ssh-manager'] = function (appDef, launchOpts) {
    const winId = 'ssh-manager';
    if (WM.windows.has(winId)) return;

    createWindow(winId, {
        title: t('Menedżer SSH'),
        icon: appDef?.icon || 'fa-key',
        iconColor: appDef?.color || '#6366f1',
        width: 820, height: 640,
        minWidth: 600, minHeight: 400,
        onRender: (body) => _sshInit(body),
    });
};

function _sshInit(body) {
    const API = '/ssh';
    let sshKeys = [];
    let tab = 'keys';

    const CSS = `<style>
    .ssh-mgr { display:flex; height:100%; font-family:var(--font-family,Inter,system-ui,sans-serif); color:var(--text,#e2e8f0); }
    .ssh-sidebar { width:190px; min-width:190px; background:var(--bg-sidebar,#0f172a); border-right:1px solid var(--border,#1e293b); display:flex; flex-direction:column; padding:8px 0; }
    .ssh-nav { padding:10px 18px; cursor:pointer; display:flex; align-items:center; gap:10px; font-size:13px; color:var(--text-muted,#94a3b8); transition:.15s; border-left:3px solid transparent; }
    .ssh-nav:hover { background:var(--bg-hover,rgba(255,255,255,.04)); color:var(--text,#e2e8f0); }
    .ssh-nav.active { background:var(--bg-hover,rgba(255,255,255,.06)); color:var(--accent,#6366f1); border-left-color:var(--accent,#6366f1); font-weight:600; }
    .ssh-nav i { width:16px; text-align:center; }
    .ssh-content { flex:1; overflow-y:auto; padding:24px; }

    .ssh-toolbar { display:flex; align-items:center; justify-content:space-between; margin-bottom:18px; }
    .ssh-toolbar h2 { margin:0; font-size:17px; font-weight:700; display:flex; align-items:center; gap:10px; }

    .ssh-btn { padding:8px 16px; border:none; border-radius:8px; cursor:pointer; font-size:12px; font-weight:600; display:inline-flex; align-items:center; gap:6px; background:var(--bg-hover,#334155); color:var(--text,#e2e8f0); transition:.15s; }
    .ssh-btn:hover { filter:brightness(1.15); }
    .ssh-btn.primary { background:var(--accent,#6366f1); color:#fff; }
    .ssh-btn.success { background:#22c55e; color:#fff; }
    .ssh-btn.danger { background:#ef4444; color:#fff; }
    .ssh-btn.sm { padding:5px 10px; font-size:11px; }
    .ssh-btn:disabled { opacity:.5; cursor:not-allowed; }

    .ssh-card { display:flex; align-items:center; gap:14px; padding:14px 16px; background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:10px; margin-bottom:8px; transition:.15s; }
    .ssh-card:hover { border-color:var(--accent,#6366f1); }
    .ssh-card-icon { width:40px; height:40px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:16px; flex-shrink:0; }
    .ssh-card-info { flex:1; min-width:0; }
    .ssh-card-name { font-size:14px; font-weight:600; }
    .ssh-card-meta { font-size:11px; color:var(--text-muted,#94a3b8); margin-top:2px; }
    .ssh-card-actions { display:flex; gap:4px; flex-shrink:0; }

    .ssh-empty { text-align:center; padding:48px 20px; color:var(--text-muted,#94a3b8); }
    .ssh-empty i { font-size:42px; opacity:.3; display:block; margin-bottom:14px; }
    .ssh-empty p { margin:0; font-size:13px; line-height:1.6; }

    .ssh-form { background:var(--bg-card,#1e293b); border:1px solid var(--border,#334155); border-radius:12px; padding:20px; margin-bottom:20px; }
    .ssh-form-title { font-size:14px; font-weight:700; margin:0 0 14px; display:flex; align-items:center; gap:8px; }
    .ssh-form-row { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
    .ssh-form-row label { width:120px; font-size:12px; font-weight:600; color:var(--text-muted,#94a3b8); flex-shrink:0; }
    .ssh-form-row input, .ssh-form-row select { flex:1; padding:8px 12px; background:var(--bg-input,#0f172a); border:1px solid var(--border,#334155); border-radius:8px; color:var(--text,#e2e8f0); font-size:13px; outline:none; }
    .ssh-form-row input:focus, .ssh-form-row select:focus { border-color:var(--accent,#6366f1); }
    .ssh-form-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:14px; }
    .ssh-hint { font-size:11px; color:var(--text-muted,#94a3b8); margin:-4px 0 10px 130px; display:flex; align-items:flex-start; gap:6px; }
    .ssh-hint i { margin-top:1px; }

    .ssh-badge { font-size:10px; padding:1px 7px; border-radius:4px; font-weight:700; }

    .ssh-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,.5); z-index:9999; display:flex; align-items:center; justify-content:center; }
    .ssh-modal { background:var(--bg-card,#1e293b); border-radius:12px; padding:24px; width:560px; max-width:95vw; max-height:90vh; overflow-y:auto; border:1px solid var(--border,#334155); }
    .ssh-modal-header { font-size:15px; font-weight:600; display:flex; align-items:center; gap:8px; margin-bottom:16px; }
    .ssh-modal-footer { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
    </style>`;

    body.innerHTML = CSS + `
    <div class="ssh-mgr">
        <div class="ssh-sidebar">
            <div class="ssh-nav active" data-tab="keys"><i class="fas fa-key"></i> Klucze SSH</div>
            <div class="ssh-nav" data-tab="hosts"><i class="fas fa-shield-alt"></i> Zaufane hosty</div>
        </div>
        <div class="ssh-content" id="ssh-content"></div>
    </div>`;

    const sidebar = body.querySelector('.ssh-sidebar');
    const content = body.querySelector('#ssh-content');

    sidebar.addEventListener('click', e => {
        const n = e.target.closest('.ssh-nav');
        if (!n) return;
        tab = n.dataset.tab;
        sidebar.querySelectorAll('.ssh-nav').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
        renderTab();
    });

    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

    async function renderTab() {
        if (tab === 'keys') await renderKeys();
        else if (tab === 'hosts') await renderHosts();
    }

    // ═══════════════════════════════════════════════════════════════
    //  KEYS TAB
    // ═══════════════════════════════════════════════════════════════

    async function loadKeys() {
        try {
            const r = await api(API + '/keys');
            sshKeys = r.keys || [];
        } catch { sshKeys = []; }
    }

    async function renderKeys() {
        content.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>';
        await loadKeys();

        content.innerHTML = `
            <div class="ssh-toolbar">
                <h2><i class="fas fa-key" style="color:#6366f1"></i> Klucze SSH</h2>
            </div>

            <div class="ssh-form">
                <div class="ssh-form-title"><i class="fas fa-plus-circle" style="color:#22c55e"></i> Wygeneruj nową parę kluczy</div>
                <div class="ssh-form-row">
                    <label>Nazwa klucza</label>
                    <input type="text" id="ssh-gen-name" placeholder="np. moj_nas" maxlength="64">
                </div>
                <div class="ssh-form-row">
                    <label>Typ</label>
                    <select id="ssh-gen-type">
                        <option value="ed25519" selected>Ed25519 (zalecany)</option>
                        <option value="rsa">RSA (4096)</option>
                        <option value="ecdsa">ECDSA</option>
                    </select>
                </div>
                <div class="ssh-form-row">
                    <label>Komentarz</label>
                    <input type="text" id="ssh-gen-comment" placeholder="ethos@hostname">
                </div>
                <div class="ssh-form-actions">
                    <button class="ssh-btn primary" id="ssh-gen-go"><i class="fas fa-plus-circle"></i> Generuj klucz</button>
                </div>
            </div>

            <div id="ssh-keys-list"></div>
        `;

        _renderKeysList();

        // Generate
        content.querySelector('#ssh-gen-go').addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const name = content.querySelector('#ssh-gen-name').value.trim();
            const type = content.querySelector('#ssh-gen-type').value;
            const comment = content.querySelector('#ssh-gen-comment').value.trim();
            if (!name) { toast(t('Podaj nazwę klucza'), 'warning'); return; }
            btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generowanie…';
            try {
                const r = await api(API + '/keys/generate', { method: 'POST', body: { name, type, comment } });
                if (r.error) { toast(r.error, 'error'); }
                else {
                    toast(`Klucz "${name}" wygenerowany!`, 'success');
                    content.querySelector('#ssh-gen-name').value = '';
                    content.querySelector('#ssh-gen-comment').value = '';
                    await loadKeys();
                    _renderKeysList();
                }
            } catch (err) { toast(t('Błąd: ') + (err.message || err), 'error'); }
            btn.disabled = false; btn.innerHTML = '<i class="fas fa-plus-circle"></i> Generuj klucz';
        });
    }

    function _renderKeysList() {
        const list = content.querySelector('#ssh-keys-list');
        if (!list) return;
        if (!sshKeys.length) {
            list.innerHTML = `<div class="ssh-empty"><i class="fas fa-key"></i><p>${t('Brak kluczy SSH.')}<br>${t('Wygeneruj pierwszą parę kluczy powyżej.')}</p></div>`;
            return;
        }
        list.innerHTML = sshKeys.map(k => {
            const dt = k.created ? new Date(k.created).toLocaleString(getLocale()) : '?';
            const short = k.public_key ? (k.public_key.length > 50 ? k.public_key.substring(0, 50) + '…' : k.public_key) : '';
            return `
            <div class="ssh-card">
                <div class="ssh-card-icon" style="background:rgba(99,102,241,.12);color:#6366f1;"><i class="fas fa-key"></i></div>
                <div class="ssh-card-info">
                    <div class="ssh-card-name">
                        ${esc(k.name)}
                        <span class="ssh-badge" style="background:#6366f1;color:#fff;">${(k.type || '?').toUpperCase()}</span>
                    </div>
                    <div class="ssh-card-meta">${dt}${k.comment ? ' · ' + esc(k.comment) : ''}</div>
                    <div class="ssh-card-meta" style="font-family:monospace;font-size:10px;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(k.public_key)}">${esc(short)}</div>
                </div>
                <div class="ssh-card-actions">
                    <button class="ssh-btn sm" data-view="${esc(k.name)}" title="Pokaż klucz publiczny"><i class="fas fa-eye"></i></button>
                    <button class="ssh-btn sm" data-copy="${esc(k.name)}" title="Kopiuj klucz publiczny"><i class="fas fa-copy"></i></button>
                    <button class="ssh-btn sm success" data-deploy="${esc(k.name)}" title="Wdróż na serwer"><i class="fas fa-upload"></i> Wdróż</button>
                    <button class="ssh-btn sm" data-test="${esc(k.name)}" title="Testuj połączenie" style="background:#e67e22;color:#fff;"><i class="fas fa-plug"></i></button>
                    <button class="ssh-btn sm danger" data-del="${esc(k.name)}" title="Usuń klucz"><i class="fas fa-trash"></i></button>
                </div>
            </div>`;
        }).join('');

        // --- Actions ---
        list.addEventListener('click', async e => {
            const btn = e.target.closest('.ssh-btn');
            if (!btn) return;

            if (btn.dataset.view) return _showPubKey(btn.dataset.view);
            if (btn.dataset.copy) return _copyPubKey(btn.dataset.copy);
            if (btn.dataset.deploy) return _deployKey(btn.dataset.deploy);
            if (btn.dataset.test) return _testKey(btn, btn.dataset.test);
            if (btn.dataset.del) return _deleteKey(btn.dataset.del);
        });
    }

    async function _showPubKey(name) {
        const k = sshKeys.find(x => x.name === name);
        if (!k) return;
        const overlay = document.createElement('div');
        overlay.className = 'ssh-overlay';
        overlay.innerHTML = `
            <div class="ssh-modal">
                <div class="ssh-modal-header"><i class="fas fa-key" style="color:#6366f1"></i> Klucz publiczny — ${esc(name)}</div>
                <p style="font-size:12px;color:var(--text-muted);margin:0 0 12px;">Skopiuj ten klucz i wklej do <code>~/.ssh/authorized_keys</code> na zdalnym serwerze, lub użyj "Wdróż".</p>
                <textarea readonly style="width:100%;height:120px;font-family:monospace;font-size:11px;background:var(--bg-input,#0f172a);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px;resize:vertical;">${esc(k.public_key)}</textarea>
                <div class="ssh-modal-footer">
                    <button class="ssh-btn primary" id="ssh-view-copy"><i class="fas fa-copy"></i> Kopiuj</button>
                    <button class="ssh-btn" id="ssh-view-close">OK</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        overlay.querySelector('#ssh-view-close').onclick = () => overlay.remove();
        overlay.querySelector('#ssh-view-copy').onclick = () => {
            const ta = overlay.querySelector('textarea');
            ta.select(); document.execCommand('copy');
            navigator.clipboard?.writeText(ta.value).catch(() => {});
            toast('Klucz skopiowany', 'success');
        };
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    }

    async function _copyPubKey(name) {
        try {
            const r = await api(API + '/keys/' + encodeURIComponent(name) + '/public');
            if (r.public_key) {
                await navigator.clipboard.writeText(r.public_key);
                toast('Klucz publiczny skopiowany', 'success');
            }
        } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
    }

    async function _deployKey(keyName) {
        // Get servers from backup
        let servers = [];
        try { const r = await api('/backup/ssh-servers'); servers = r.servers || []; } catch {}
        const hasServers = servers.length > 0;

        const overlay = document.createElement('div');
        overlay.className = 'ssh-overlay';
        overlay.innerHTML = `
            <div class="ssh-modal">
                <div class="ssh-modal-header"><i class="fas fa-upload" style="color:#22c55e"></i> Wdróż klucz SSH</div>
                <p style="font-size:12px;color:var(--text-muted);margin:0 0 14px;">
                    Klucz publiczny <b>"${esc(keyName)}"</b> zostanie dodany do authorized_keys na serwerze docelowym.
                </p>
                ${hasServers ? `
                <div class="ssh-form-row">
                    <label>Wybierz serwer</label>
                    <select id="ssh-deploy-srv">
                        <option value="">— ręcznie —</option>
                        ${servers.map(s => `<option value="${esc(s.id)}">${esc(s.name || s.host)}</option>`).join('')}
                    </select>
                </div>` : ''}
                <div class="ssh-form-row"><label>Host</label><input type="text" id="ssh-deploy-host" placeholder="192.168.x.x"></div>
                <div class="ssh-form-row"><label>Port</label><input type="number" id="ssh-deploy-port" value="22" min="1" max="65535"></div>
                <div class="ssh-form-row"><label>Użytkownik</label><input type="text" id="ssh-deploy-user" placeholder="root"></div>
                <div class="ssh-form-row"><label>Hasło (jednorazowo)</label><input type="password" id="ssh-deploy-pw"></div>
                <div class="ssh-hint"><i class="fas fa-info-circle"></i> Hasło jest potrzebne tylko do wdrożenia klucza. Po tym logowanie będzie bezhasłowe.</div>
                <div class="ssh-modal-footer">
                    <button class="ssh-btn" id="ssh-deploy-cancel">Anuluj</button>
                    <button class="ssh-btn success" id="ssh-deploy-go"><i class="fas fa-upload"></i> Wdróż</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        // Auto-fill from server select
        const srvSel = overlay.querySelector('#ssh-deploy-srv');
        if (srvSel) {
            srvSel.addEventListener('change', () => {
                const s = servers.find(x => x.id === srvSel.value);
                if (s) {
                    overlay.querySelector('#ssh-deploy-host').value = s.host || '';
                    overlay.querySelector('#ssh-deploy-port').value = s.port || 22;
                    overlay.querySelector('#ssh-deploy-user').value = s.username || '';
                }
            });
        }

        overlay.querySelector('#ssh-deploy-cancel').onclick = () => overlay.remove();
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
        overlay.querySelector('#ssh-deploy-go').onclick = async () => {
            const host = overlay.querySelector('#ssh-deploy-host').value.trim();
            const port = parseInt(overlay.querySelector('#ssh-deploy-port').value) || 22;
            const user = overlay.querySelector('#ssh-deploy-user').value.trim();
            const pw = overlay.querySelector('#ssh-deploy-pw').value;
            if (!host || !user) { toast(t('Podaj host i użytkownika'), 'warning'); return; }
            if (!pw) { toast(t('Hasło wymagane do jednorazowego wdrożenia'), 'warning'); return; }
            const btn = overlay.querySelector('#ssh-deploy-go');
            btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Wdrażanie…')}`;
            try {
                const r = await api(API + '/keys/' + encodeURIComponent(keyName) + '/deploy', {
                    method: 'POST', body: { host, port, username: user, password: pw }
                });
                if (r.error) { toast(t('Błąd: ') + r.error, 'error'); }
                else if (r.already_deployed) { toast(t('Klucz już był wdrożony na tym serwerze'), 'info'); }
                else { toast(t('Klucz wdrożony pomyślnie!'), 'success'); }
                overlay.remove();
            } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
            btn.disabled = false; btn.innerHTML = `<i class="fas fa-upload"></i> ${t('Wdróż')}`;
        };
    }

    async function _testKey(btn, name) {
        let servers = [];
        try { const r = await api('/backup/ssh-servers'); servers = r.servers || []; } catch {}
        if (!servers.length) { toast(t('Brak zapisanych serwerów SSH (dodaj w NASLink lub Kopie zapasowe)'), 'warning'); return; }
        btn.disabled = true; const orig = btn.innerHTML; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        let results = [];
        for (const s of servers) {
            try {
                const r = await api(API + '/keys/test-auth', {
                    method: 'POST', body: { key_name: name, host: s.host, port: s.port || 22, username: s.username }
                });
                results.push((s.name || s.host) + ': ' + (r.success ? '✅ OK (jako ' + r.user + ')' : '❌ ' + (r.error || t('błąd'))));
            } catch (e) { results.push((s.name || s.host) + ': ❌ ' + (e.message || e)); }
        }
        toast(results.join('\n'), results.some(r => r.includes('✅')) ? 'success' : 'error', 8000);
        btn.disabled = false; btn.innerHTML = orig;
    }

    async function _deleteKey(name) {
        if (!confirm(`Usunąć parę kluczy "${name}"?\nSerwery, na których wdrożono klucz publiczny, nadal będą go miały.`)) return;
        try {
            await api(API + '/keys/' + encodeURIComponent(name), { method: 'DELETE' });
            toast(t('Klucz usunięty'), 'success');
            await loadKeys();
            _renderKeysList();
        } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
    }

    // ═══════════════════════════════════════════════════════════════
    //  KNOWN HOSTS TAB
    // ═══════════════════════════════════════════════════════════════

    async function renderHosts() {
        content.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>';

        let entries = [], khPath = '', khUser = '', khHome = '';
        try {
            const r = await api(API + '/known-hosts');
            entries = r.entries || [];
            khPath = r.path || '';
            khUser = r.user || '';
            khHome = r.home || '';
        } catch {}

        content.innerHTML = `
            <div class="ssh-toolbar">
                <h2><i class="fas fa-shield-alt" style="color:#06b6d4"></i> Zaufane hosty <span style="font-size:12px;font-weight:400;color:var(--text-muted)">(known_hosts)</span></h2>
            </div>
            <p style="font-size:12px;color:var(--text-muted);margin:-12px 0 18px;line-height:1.6;">
                Lista hostów, do których ten NAS się łączył przez SSH. Usunięcie wpisu „odcina" zaufanie — przy następnym połączeniu zostaniesz poproszony o potwierdzenie tożsamości hosta.
                ${khPath ? `<br><i class="fas fa-user" style="font-size:10px;"></i> <b>${esc(khUser)}</b> — <span style="font-family:monospace;font-size:11px;">${esc(khPath)}</span>` : ''}
            </p>
            <div id="ssh-hosts-list">
                ${entries.length === 0
                    ? `<div class="ssh-empty"><i class="fas fa-shield-alt"></i><p>${t('Brak wpisów w known_hosts')}</p></div>`
                    : entries.map(e => `
                    <div class="ssh-card" style="border-left:3px solid ${e.server_name ? '#06b6d4' : 'var(--border)'};">
                        <div class="ssh-card-icon" style="background:rgba(${e.server_name ? '6,182,212' : '100,116,139'},.1);color:${e.server_name ? '#06b6d4' : '#64748b'};">
                            <i class="fas fa-fingerprint"></i>
                        </div>
                        <div class="ssh-card-info">
                            <div class="ssh-card-name" style="display:flex;align-items:center;gap:8px;">
                                ${esc(e.host)}
                                ${e.server_name ? `<span class="ssh-badge" style="background:rgba(6,182,212,.12);color:#06b6d4;">${esc(e.server_name)}</span>` : ''}
                            </div>
                            <div class="ssh-card-meta">
                                ${esc(e.key_type)} — ${esc(e.fingerprint || '?')}
                                ${e.hashed ? ' — <i class="fas fa-lock" style="font-size:9px;"></i> zahaszowany' : ''}
                            </div>
                        </div>
                        ${e.host && e.host !== '(zaszyfrowany)'
                            ? `<button class="ssh-btn sm danger" data-rm-host="${esc(e.host)}" title="Usuń zaufanie"><i class="fas fa-unlink"></i> Odetnij</button>`
                            : `<button class="ssh-btn sm danger" data-rm-line="${e.line}" title="Usuń wpis"><i class="fas fa-trash"></i></button>`
                        }
                    </div>
                `).join('')}
            </div>
        `;

        content.querySelector('#ssh-hosts-list').addEventListener('click', async e => {
            const btn = e.target.closest('.ssh-btn');
            if (!btn) return;

            if (btn.dataset.rmHost) {
                if (!confirm(`Usunąć zaufanie do hosta "${btn.dataset.rmHost}"?`)) return;
                btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                try {
                    const r = await api(API + '/known-hosts/remove', { method: 'POST', body: { host: btn.dataset.rmHost } });
                    if (r.error) toast(r.error, 'error'); else { toast(r.message || t('Usunięto'), 'success'); renderHosts(); }
                } catch (err) { toast(t('Błąd: ') + (err.message || err), 'error'); }
            }

            if (btn.dataset.rmLine) {
                if (!confirm(t('Usunąć ten wpis z known_hosts?'))) return;
                btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                try {
                    const r = await api(API + '/known-hosts/remove-line', { method: 'POST', body: { line: parseInt(btn.dataset.rmLine) } });
                    if (r.error) toast(r.error, 'error'); else { toast(t('Wpis usunięty'), 'success'); renderHosts(); }
                } catch (err) { toast(t('Błąd: ') + (err.message || err), 'error'); }
            }
        });
    }

    // Boot
    renderTab();
}
