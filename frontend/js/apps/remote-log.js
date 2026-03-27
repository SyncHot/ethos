AppRegistry['remote-log'] = function (appDef) {
    createWindow('remote-log', {
        title: t('Zdalne logi'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 700,
        height: 600,
        onRender: (body) => renderRemoteLog(body),
    });
};

function renderRemoteLog(body) {
    const CSS = `
    <style>
    .rl-wrap{padding:20px;font-size:14px;color:var(--text-primary);overflow-y:auto;height:100%}
    .rl-card{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:20px;margin-bottom:16px}
    .rl-card h3{margin:0 0 14px;font-size:16px;font-weight:600}
    .rl-row{display:flex;align-items:center;gap:12px;margin-bottom:12px}
    .rl-row label{min-width:140px;font-size:13px;color:var(--text-secondary)}
    .rl-input{flex:1;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;padding:8px 12px;color:var(--text-primary);font-size:13px}
    .rl-input:focus{outline:none;border-color:var(--accent)}
    .rl-toggle{position:relative;width:44px;height:24px;cursor:pointer}
    .rl-toggle input{display:none}
    .rl-toggle .slider{position:absolute;inset:0;background:var(--bg-tertiary);border-radius:12px;transition:.3s}
    .rl-toggle input:checked+.slider{background:var(--accent)}
    .rl-toggle .slider::before{content:'';position:absolute;width:18px;height:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
    .rl-toggle input:checked+.slider::before{transform:translateX(20px)}
    .rl-btn{padding:8px 18px;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:filter .2s}
    .rl-btn-primary{background:var(--accent);color:#fff}
    .rl-btn-primary:hover{filter:brightness(1.1)}
    .rl-btn-secondary{background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border-color)}
    .rl-btn:disabled{opacity:.4;cursor:not-allowed}
    .rl-status{display:flex;gap:16px;flex-wrap:wrap;margin-top:8px}
    .rl-stat{background:var(--bg-tertiary);border-radius:8px;padding:10px 14px;text-align:center;min-width:100px}
    .rl-stat .val{font-size:18px;font-weight:700;color:var(--accent)}
    .rl-stat .lbl{font-size:11px;color:var(--text-secondary);margin-top:2px}
    .rl-tag{display:inline-block;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:600;margin:2px}
    .rl-tag-on{background:rgba(16,185,129,.15);color:#10b981}
    .rl-tag-off{background:rgba(239,68,68,.15);color:#ef4444}
    .rl-cats{display:flex;flex-wrap:wrap;gap:6px;flex:1}
    .rl-cat{padding:5px 10px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid var(--border-color);background:var(--bg-tertiary);color:var(--text-secondary);transition:all .2s}
    .rl-cat.active{background:var(--accent);color:#fff;border-color:var(--accent)}
    .rl-msg{padding:10px;border-radius:6px;font-size:13px;margin-top:8px}
    .rl-msg-ok{background:rgba(16,185,129,.1);color:#10b981}
    .rl-msg-err{background:rgba(239,68,68,.1);color:#ef4444}
    .rl-preview{background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;padding:12px;font-family:monospace;font-size:11px;max-height:200px;overflow:auto;white-space:pre-wrap;word-break:break-all;margin-top:8px}
    .rl-id{font-family:monospace;font-size:12px;color:var(--text-secondary);background:var(--bg-tertiary);padding:2px 6px;border-radius:4px}
    </style>`;

    body.innerHTML = CSS + `<div class="rl-wrap">
        <div class="rl-card">
            <h3><i class="fas fa-satellite-dish app-hdr-icon"></i>${t('Zdalne raportowanie logów')}</h3>
            <div id="rl-loading" class="app-empty app-empty--loading">
                <i class="fas fa-spinner fa-spin app-spinner-md"></i>
                <div class="app-mt-sm">${t('Ładowanie konfiguracji...')}</div>
            </div>
            <div id="rl-content" class="hidden"></div>
        </div>
    </div>`;

    let config = {};

    async function load() {
        try {
            config = await api('/remote-log/config');
            render();
        } catch (e) {
            body.querySelector('#rl-loading').innerHTML = `<div class="app-text-error">${t('Błąd:')} ${esc(e.message)}</div>`;
        }
    }

    function render() {
        body.querySelector('#rl-loading').style.display = 'none';
        const content = body.querySelector('#rl-content');
        content.style.display = '';

        const allCats = ['boot','services','system','errors','dmesg'];
        const activeCats = config.log_categories || allCats;
        const lastSend = config.last_send ? new Date(config.last_send * 1000).toLocaleString() : 'nigdy';

        content.innerHTML = `
            <div class="rl-status">
                <div class="rl-stat">
                    <div class="val">${config.enabled ? '<span class="rl-tag rl-tag-on">ON</span>' : '<span class="rl-tag rl-tag-off">OFF</span>'}</div>
                    <div class="lbl">Status</div>
                </div>
                <div class="rl-stat">
                    <div class="val">${config.send_count || 0}</div>
                    <div class="lbl">${t('Wysłano')}</div>
                </div>
                <div class="rl-stat">
                    <div class="val app-text-sm">${esc(lastSend)}</div>
                    <div class="lbl">Ostatnio</div>
                </div>
            </div>
            ${config.last_error ? `<div class="rl-msg rl-msg-err app-mt-md"><i class="fas fa-exclamation-triangle"></i> ${esc(config.last_error)}</div>` : ''}
            <div class="app-mt-lg">
                <div class="rl-row">
                    <label>${t('Włączone')}</label>
                    <label class="rl-toggle"><input type="checkbox" id="rl-enabled" ${config.enabled ? 'checked' : ''}><span class="slider"></span></label>
                </div>
                <div class="rl-row">
                    <label>URL serwera</label>
                    <input class="rl-input" id="rl-url" value="${esc(config.server_url || '')}">
                </div>
                <div class="rl-row">
                    <label>${t('Interwał (min)')}</label>
                    <input class="rl-input app-input-narrow" id="rl-interval" type="number" min="5" value="${config.interval_minutes || 60}">
                </div>
                <div class="rl-row">
                    <label>${t('Wyślij przy starcie')}</label>
                    <label class="rl-toggle"><input type="checkbox" id="rl-boot" ${config.send_on_boot ? 'checked' : ''}><span class="slider"></span></label>
                </div>
                <div class="rl-row">
                    <label>${t('Wyślij przy błędzie')}</label>
                    <label class="rl-toggle"><input type="checkbox" id="rl-error" ${config.send_on_error ? 'checked' : ''}><span class="slider"></span></label>
                </div>
                <div class="rl-row">
                    <label>${t('Kategorie logów')}</label>
                    <div class="rl-cats">
                        ${allCats.map(c => `<div class="rl-cat ${activeCats.includes(c) ? 'active' : ''}" data-cat="${c}">${c}</div>`).join('')}
                    </div>
                </div>
                <div class="rl-row app-mt-xs">
                    <label>Device ID</label>
                    <span class="rl-id">${esc(config.device_id || '?')}</span>
                </div>
            </div>
            <div class="app-actions">
                <button class="rl-btn rl-btn-primary" id="rl-save"><i class="fas fa-save"></i> Zapisz</button>
                <button class="rl-btn rl-btn-secondary" id="rl-send"><i class="fas fa-paper-plane"></i> ${t('Wyślij teraz')}</button>
                <button class="rl-btn rl-btn-secondary" id="rl-preview"><i class="fas fa-eye">${t('Podgląd')}</button>
            </div>
            <div id="rl-feedback"></div>
            <div id="rl-preview-box"></div>
        `;

        // Toggle categories
        content.querySelectorAll('.rl-cat').forEach(el => {
            el.addEventListener('click', () => el.classList.toggle('active'));
        });

        // Save
        content.querySelector('#rl-save').addEventListener('click', async () => {
            const cats = [...content.querySelectorAll('.rl-cat.active')].map(e => e.dataset.cat);
            const btn = content.querySelector('#rl-save');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Zapisuję...')}`;
            try {
                config = await api('/remote-log/config', {
                    method: 'POST',
                    body: {
                        enabled: content.querySelector('#rl-enabled').checked,
                        server_url: content.querySelector('#rl-url').value.trim(),
                        interval_minutes: parseInt(content.querySelector('#rl-interval').value) || 60,
                        send_on_boot: content.querySelector('#rl-boot').checked,
                        send_on_error: content.querySelector('#rl-error').checked,
                        log_categories: cats,
                    }
                });
                content.querySelector('#rl-feedback').innerHTML = '<div class="rl-msg rl-msg-ok app-mt-sm"><i class="fas fa-check"></i> Zapisano</div>';
                setTimeout(() => { try { content.querySelector('#rl-feedback').innerHTML = ''; } catch(e){} }, 3000);
            } catch (e) {
                content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-err app-mt-sm">${esc(e.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-save"></i> Zapisz';
        });

        // Send now
        content.querySelector('#rl-send').addEventListener('click', async () => {
            const btn = content.querySelector('#rl-send');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Wysyłanie...')}`;
            try {
                const r = await api('/remote-log/send', { method: 'POST' });
                if (r.ok) {
                    content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-ok app-mt-sm"><i class="fas fa-check"></i> ${t('Wysłano pomyślnie')}</div>`;
                } else {
                    content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-err app-mt-sm">${esc(r.message)}</div>`;
                }
                setTimeout(() => { try { content.querySelector('#rl-feedback').innerHTML = ''; } catch(e){} }, 5000);
            } catch (e) {
                content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-err app-mt-sm">${esc(e.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-paper-plane"></i> ${t('Wyślij teraz')}`;
        });

        // Preview
        content.querySelector('#rl-preview').addEventListener('click', async () => {
            const box = content.querySelector('#rl-preview-box');
            const btn = content.querySelector('#rl-preview');
            if (box.children.length) { box.innerHTML = ''; return; }
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            try {
                const data = await api('/remote-log/preview');
                box.innerHTML = `<div class="rl-preview">${esc(JSON.stringify(data, null, 2))}</div>`;
            } catch (e) {
                box.innerHTML = `<div class="rl-msg rl-msg-err">${esc(e.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-eye"></i> ${t('Podgląd')}`;
        });
    }

    load();
}

// ═══════════════════════════════════════════════════════════
//  DYNAMIC DNS — MIGRATED to Domains Manager app
//  (domains.js → DDNS tab, backend still in ddns.py)
// ═══════════════════════════════════════════════════════════

/* ═══════════════════════════════════════════════════════════════════
   System Settings App
   ═══════════════════════════════════════════════════════════════════ */

