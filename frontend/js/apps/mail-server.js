/* ═══════════════════════════════════════════════════════════
   EthOS — Mail Server (Postfix + Dovecot)
   ═══════════════════════════════════════════════════════════ */

AppRegistry['mail-server'] = function (appDef) {
    const PREFIX = 'ms';
    function esc(s) {
        if (!s && s !== 0) return '';
        const d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    const win = createWindow('mail-server', {
        title: t('Serwer Poczty'),
        icon: appDef.icon || 'fa-envelope',
        iconColor: appDef.color || '#3b82f6',
        width: 900,
        height: 650,
        resizable: true,
        maximizable: true,
    });
    const body = win.body;
    body.style.cssText = 'display:flex;flex-direction:column;height:100%;overflow:hidden;background:var(--window-bg)';

    let _status = null;
    let _currentTab = 'dashboard';

    // ─── DNS Provider guides ──────────────────────────────
    const DNS_GUIDES = {
        'OVH': {
            url: 'https://docs.ovh.com/pl/domains/hosting_www_jak_edytowac_strefe_dns/',
            steps: [
                t('Zaloguj się do panelu OVH → Web Cloud → Domeny'),
                t('Wybierz domenę → zakładka "Strefa DNS"'),
                t('Kliknij "Dodaj wpis" i wybierz typ rekordu'),
                t('Wklej wartość z poniższej tabeli i zapisz'),
            ]
        },
        'Cloudflare': {
            url: 'https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/',
            steps: [
                t('Zaloguj się do Cloudflare Dashboard'),
                t('Wybierz domenę → DNS → Records'),
                t('Kliknij "Add record"'),
                t('Wybierz typ, wklej nazwę i wartość, zapisz'),
            ]
        },
        'GoDaddy': {
            url: 'https://www.godaddy.com/help/manage-dns-records-680',
            steps: [
                t('Zaloguj się do GoDaddy → My Products → DNS'),
                t('Wybierz domenę'),
                t('Kliknij "Add" przy typie rekordu'),
                t('Wklej wartości i zapisz'),
            ]
        },
        'home.pl': {
            url: 'https://pomoc.home.pl/baza-wiedzy/jak-edytowac-strefe-dns-domeny',
            steps: [
                t('Panel klienta → Domeny → Zarządzaj'),
                t('Zakładka "DNS / Nameservers"'),
                t('Dodaj nowy rekord wybranego typu'),
                t('Wklej wartości i zatwierdź'),
            ]
        },
    };

    const RELAY_PRESETS = {
        'Gmail': { host: 'smtp.gmail.com', port: 587, note: t('Wymaga "App Password" w ustawieniach Google (2FA musi być włączone).') },
        'Mailgun': { host: 'smtp.mailgun.org', port: 587, note: t('Darmowe 5000 maili/miesiąc. Zarejestruj się na mailgun.com.') },
        'SendGrid': { host: 'smtp.sendgrid.net', port: 587, note: t('Darmowe 100 maili/dzień. Użyj "apikey" jako username.') },
        'OVH': { host: 'ssl0.ovh.net', port: 587, note: t('Użyj danych swojego konta OVH email.') },
    };

    // ─── Main render ─────────────────────────────────────
    async function init() {
        body.innerHTML = `<div class="${PREFIX}-loading" style="display:flex;align-items:center;justify-content:center;height:100%;gap:10px">
            <i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}
        </div>`;

        try {
            _status = await api('/mail-server/status');
            if (_status.error) { body.innerHTML = `<div style="padding:20px;color:var(--error)">${esc(_status.error)}</div>`; return; }
        } catch (e) {
            body.innerHTML = `<div style="padding:20px;color:var(--error)">${t('Błąd połączenia z serwerem.')}</div>`;
            return;
        }

        if (!_status.installed) { renderInstallScreen(); return; }
        if (!_status.configured) { renderWizard(); return; }
        renderDashboard();
    }

    // ─── Install screen ──────────────────────────────────
    function renderInstallScreen() {
        body.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:20px;padding:40px">
            <i class="fas fa-envelope" style="font-size:48px;color:#3b82f6"></i>
            <h2 style="margin:0;color:var(--text-primary)">${t('Serwer Poczty')}</h2>
            <p style="color:var(--text-secondary);text-align:center;max-width:400px">
                ${t('Zainstaluj Postfix + Dovecot + OpenDKIM aby uruchomić własny serwer pocztowy. Użytkownicy będą mogli korzystać z dowolnego klienta (Thunderbird, Outlook, Apple Mail).')}
            </p>
            <button class="btn btn-primary" id="${PREFIX}-install-btn">
                <i class="fas fa-download"></i> ${t('Zainstaluj')}
            </button>
            <div id="${PREFIX}-install-progress" style="display:none;width:100%;max-width:400px">
                <div class="set-progress-bar" style="height:8px;border-radius:4px;overflow:hidden;background:var(--bg-tertiary)">
                    <div id="${PREFIX}-progress-fill" style="height:100%;width:0;background:#3b82f6;transition:width .3s"></div>
                </div>
                <div id="${PREFIX}-progress-msg" style="text-align:center;margin-top:8px;color:var(--text-secondary);font-size:13px"></div>
            </div>
        </div>`;

        body.querySelector(`#${PREFIX}-install-btn`).onclick = async () => {
            const btn = body.querySelector(`#${PREFIX}-install-btn`);
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Instalowanie...')}`;
            body.querySelector(`#${PREFIX}-install-progress`).style.display = 'block';

            if (NAS.socket) {
                NAS.socket.on('mail_server_install', (d) => {
                    const fill = body.querySelector(`#${PREFIX}-progress-fill`);
                    const msg = body.querySelector(`#${PREFIX}-progress-msg`);
                    if (fill) fill.style.width = d.percent + '%';
                    if (msg) msg.textContent = d.message || '';
                    if (d.stage === 'done') {
                        NAS.socket.off('mail_server_install');
                        setTimeout(() => init(), 1000);
                    }
                    if (d.stage === 'error') {
                        NAS.socket.off('mail_server_install');
                        btn.disabled = false;
                        btn.innerHTML = `<i class="fas fa-download"></i> ${t('Spróbuj ponownie')}`;
                        if (msg) msg.style.color = 'var(--error)';
                    }
                });
            }
            await api('/mail-server/install', { method: 'POST' });
        };
    }

    // ─── Setup Wizard ────────────────────────────────────
    function renderWizard() {
        body.innerHTML = `
        <div style="max-width:520px;margin:30px auto;padding:0 20px">
            <h2 style="margin:0 0 8px;color:var(--text-primary)"><i class="fas fa-magic" style="color:#3b82f6"></i> ${t('Kreator konfiguracji')}</h2>
            <p style="color:var(--text-secondary);margin:0 0 24px">${t('Skonfiguruj serwer poczty w 4 prostych krokach.')}</p>

            <div class="${PREFIX}-form-group" style="margin-bottom:16px">
                <label style="color:var(--text-secondary);font-size:13px;display:block;margin-bottom:4px">
                    ${t('Hostname serwera poczty')}
                    <span style="color:var(--text-muted);font-size:11px">(${t('np. mail.twojadomena.pl')})</span>
                </label>
                <input type="text" id="${PREFIX}-wiz-hostname" class="input" placeholder="mail.example.com"
                    style="width:100%;padding:8px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            </div>

            <div class="${PREFIX}-form-group" style="margin-bottom:16px">
                <label style="color:var(--text-secondary);font-size:13px;display:block;margin-bottom:4px">
                    ${t('Domena email')}
                    <span style="color:var(--text-muted);font-size:11px">(${t('np. twojadomena.pl')})</span>
                </label>
                <input type="text" id="${PREFIX}-wiz-domain" class="input" placeholder="example.com"
                    style="width:100%;padding:8px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            </div>

            <div class="${PREFIX}-form-group" style="margin-bottom:16px">
                <label style="color:var(--text-secondary);font-size:13px;display:block;margin-bottom:4px">
                    ${t('Pierwszy adres email')}
                </label>
                <input type="text" id="${PREFIX}-wiz-email" class="input" placeholder="admin"
                    style="width:100%;padding:8px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
                <div id="${PREFIX}-wiz-email-preview" style="color:var(--text-muted);font-size:12px;margin-top:2px"></div>
            </div>

            <div class="${PREFIX}-form-group" style="margin-bottom:24px">
                <label style="color:var(--text-secondary);font-size:13px;display:block;margin-bottom:4px">
                    ${t('Hasło do konta email')}
                </label>
                <input type="password" id="${PREFIX}-wiz-password" class="input" placeholder="••••••••"
                    style="width:100%;padding:8px 12px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            </div>

            <div id="${PREFIX}-wiz-error" style="display:none;padding:10px;border-radius:6px;background:var(--error-bg);color:var(--error);margin-bottom:16px;font-size:13px"></div>

            <button class="btn btn-primary" id="${PREFIX}-wiz-submit" style="width:100%;padding:10px">
                <i class="fas fa-rocket"></i> ${t('Skonfiguruj i uruchom')}
            </button>
        </div>`;

        // Live preview of email
        const domainInp = body.querySelector(`#${PREFIX}-wiz-domain`);
        const emailInp = body.querySelector(`#${PREFIX}-wiz-email`);
        const preview = body.querySelector(`#${PREFIX}-wiz-email-preview`);
        const updatePreview = () => {
            const d = domainInp.value.trim();
            const e = emailInp.value.trim();
            preview.textContent = d && e ? `→ ${e}@${d}` : '';
        };
        domainInp.addEventListener('input', updatePreview);
        emailInp.addEventListener('input', updatePreview);

        body.querySelector(`#${PREFIX}-wiz-submit`).onclick = async () => {
            const btn = body.querySelector(`#${PREFIX}-wiz-submit`);
            const errDiv = body.querySelector(`#${PREFIX}-wiz-error`);
            errDiv.style.display = 'none';
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Konfigurowanie...')}`;

            const data = {
                hostname: body.querySelector(`#${PREFIX}-wiz-hostname`).value.trim(),
                domain: domainInp.value.trim(),
                email: emailInp.value.trim(),
                password: body.querySelector(`#${PREFIX}-wiz-password`).value,
            };

            const res = await api('/mail-server/setup', { method: 'POST', body: data });
            if (res.error) {
                errDiv.textContent = res.error;
                errDiv.style.display = 'block';
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-rocket"></i> ${t('Skonfiguruj i uruchom')}`;
                return;
            }

            toast(t('Serwer poczty skonfigurowany!'), 'success');
            init();
        };
    }

    // ─── Dashboard (main view after setup) ────────────────
    function renderDashboard() {
        body.innerHTML = `
        <div class="${PREFIX}-tabs" style="display:flex;border-bottom:1px solid var(--border-color);padding:0 12px;background:var(--bg-secondary);gap:0">
            ${['dashboard','accounts','domains','aliases','relay','logs'].map(t_id => `
                <button class="${PREFIX}-tab ${_currentTab===t_id?PREFIX+'-tab-active':''}" data-tab="${t_id}"
                    style="padding:10px 16px;background:none;border:none;cursor:pointer;color:${_currentTab===t_id?'var(--accent)':'var(--text-secondary)'};
                    border-bottom:2px solid ${_currentTab===t_id?'var(--accent)':'transparent'};font-size:13px;font-weight:500;transition:all .15s">
                    ${t_id === 'dashboard' ? '<i class="fas fa-tachometer-alt"></i> '+t('Panel') : ''}
                    ${t_id === 'accounts' ? '<i class="fas fa-users"></i> '+t('Konta') : ''}
                    ${t_id === 'domains' ? '<i class="fas fa-globe"></i> '+t('Domeny') : ''}
                    ${t_id === 'aliases' ? '<i class="fas fa-share"></i> '+t('Aliasy') : ''}
                    ${t_id === 'relay' ? '<i class="fas fa-paper-plane"></i> '+t('Relay') : ''}
                    ${t_id === 'logs' ? '<i class="fas fa-file-alt"></i> '+t('Logi') : ''}
                </button>
            `).join('')}
        </div>
        <div id="${PREFIX}-tab-content" style="flex:1;overflow:auto;padding:16px"></div>`;

        body.querySelectorAll(`.${PREFIX}-tab`).forEach(btn => {
            btn.onclick = () => {
                _currentTab = btn.dataset.tab;
                renderDashboard();
            };
        });

        const content = body.querySelector(`#${PREFIX}-tab-content`);
        if (_currentTab === 'dashboard') renderDashboardTab(content);
        else if (_currentTab === 'accounts') renderAccountsTab(content);
        else if (_currentTab === 'domains') renderDomainsTab(content);
        else if (_currentTab === 'aliases') renderAliasesTab(content);
        else if (_currentTab === 'relay') renderRelayTab(content);
        else if (_currentTab === 'logs') renderLogsTab(content);
    }

    // ─── Dashboard tab ───────────────────────────────────
    async function renderDashboardTab(el) {
        _status = await api('/mail-server/status');
        const s = _status;

        const svcBadge = (running) => running
            ? `<span style="color:#16a34a"><i class="fas fa-circle" style="font-size:8px"></i> ${t('Działa')}</span>`
            : `<span style="color:#dc2626"><i class="fas fa-circle" style="font-size:8px"></i> ${t('Zatrzymany')}</span>`;

        el.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px">
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">Postfix (SMTP)</div>
                <div style="font-size:15px">${svcBadge(s.postfix_running)}</div>
            </div>
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">Dovecot (IMAP)</div>
                <div style="font-size:15px">${svcBadge(s.dovecot_running)}</div>
            </div>
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">OpenDKIM</div>
                <div style="font-size:15px">${svcBadge(s.opendkim_running)}</div>
            </div>
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">${t('Kolejka')}</div>
                <div style="font-size:15px;color:var(--text-primary)">${s.queue_count || 0}</div>
            </div>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px">
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">${t('Hostname')}</div>
                <div style="font-size:14px;color:var(--text-primary);word-break:break-all">${esc(s.hostname)}</div>
            </div>
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">${t('Domeny / Konta / Aliasy')}</div>
                <div style="font-size:14px;color:var(--text-primary)">${s.domain_count || 0} / ${s.account_count || 0} / ${s.alias_count || 0}</div>
            </div>
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">${t('Użycie dysku')}</div>
                <div style="font-size:14px;color:var(--text-primary)">${esc(s.data_size)}</div>
            </div>
            <div class="${PREFIX}-card" style="padding:16px;border-radius:8px;background:var(--bg-tertiary);border:1px solid var(--border-color)">
                <div style="color:var(--text-muted);font-size:12px;margin-bottom:6px">${t('Relay SMTP')}</div>
                <div style="font-size:14px;color:var(--text-primary)">${s.relay_enabled ? esc(s.relay_host) : t('Wyłączony')}</div>
            </div>
        </div>

        <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn btn-sm" id="${PREFIX}-svc-restart"><i class="fas fa-redo"></i> ${t('Restartuj usługi')}</button>
            <button class="btn btn-sm" id="${PREFIX}-svc-stop"><i class="fas fa-stop"></i> ${t('Zatrzymaj')}</button>
            <button class="btn btn-sm" id="${PREFIX}-svc-start"><i class="fas fa-play"></i> ${t('Uruchom')}</button>
            <button class="btn btn-sm" id="${PREFIX}-flush-queue"><i class="fas fa-broom"></i> ${t('Wyczyść kolejkę')}</button>
            <button class="btn btn-sm ${PREFIX}-test-btn" style="margin-left:auto"><i class="fas fa-paper-plane"></i> ${t('Wyślij test')}</button>
        </div>`;

        el.querySelector(`#${PREFIX}-svc-restart`).onclick = () => svcAction('restart');
        el.querySelector(`#${PREFIX}-svc-stop`).onclick = () => svcAction('stop');
        el.querySelector(`#${PREFIX}-svc-start`).onclick = () => svcAction('start');
        el.querySelector(`#${PREFIX}-flush-queue`).onclick = async () => {
            await api('/mail-server/queue/flush', { method: 'POST' });
            toast(t('Kolejka wyczyszczona'), 'success');
        };
        el.querySelector(`.${PREFIX}-test-btn`).onclick = () => showTestDialog();
    }

    async function svcAction(action) {
        const res = await api(`/mail-server/service/${action}`, { method: 'POST' });
        if (res.ok) toast(t('Usługi: ') + action, 'success');
        else toast(res.errors ? res.errors.join(', ') : t('Błąd'), 'error');
        renderDashboard();
    }

    function showTestDialog() {
        const html = `
        <div style="padding:20px;max-width:400px">
            <h3 style="margin:0 0 16px">${t('Wyślij testowy email')}</h3>
            <label style="font-size:13px;color:var(--text-secondary)">${t('Od')}</label>
            <input type="email" id="${PREFIX}-test-from" class="input" style="width:100%;margin-bottom:10px;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            <label style="font-size:13px;color:var(--text-secondary)">${t('Do')}</label>
            <input type="email" id="${PREFIX}-test-to" class="input" style="width:100%;margin-bottom:16px;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            <button class="btn btn-primary" id="${PREFIX}-test-send-btn" style="width:100%">${t('Wyślij')}</button>
        </div>`;
        const dlg = createWindow('mail-test', { title: t('Test Email'), width: 440, height: 260, modal: true });
        dlg.body.innerHTML = html;
        dlg.body.querySelector(`#${PREFIX}-test-send-btn`).onclick = async () => {
            const from = dlg.body.querySelector(`#${PREFIX}-test-from`).value;
            const to = dlg.body.querySelector(`#${PREFIX}-test-to`).value;
            const res = await api('/mail-server/test-send', { method: 'POST', body: { from, to } });
            if (res.ok) { toast(res.message || t('Wysłano'), 'success'); closeWindow('mail-test'); }
            else toast(res.error || t('Błąd'), 'error');
        };
    }

    // ─── Accounts tab ────────────────────────────────────
    async function renderAccountsTab(el) {
        const data = await api('/mail-server/accounts');
        const items = data.items || [];

        el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h3 style="margin:0;color:var(--text-primary)">${t('Konta email')} (${items.length})</h3>
            <button class="btn btn-primary btn-sm" id="${PREFIX}-add-account"><i class="fas fa-plus"></i> ${t('Dodaj konto')}</button>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead>
                <tr style="border-bottom:1px solid var(--border-color);text-align:left">
                    <th style="padding:8px;color:var(--text-muted)">Email</th>
                    <th style="padding:8px;color:var(--text-muted)">${t('Użycie')}</th>
                    <th style="padding:8px;color:var(--text-muted)">${t('Quota')}</th>
                    <th style="padding:8px;color:var(--text-muted)">${t('Status')}</th>
                    <th style="padding:8px;color:var(--text-muted)"></th>
                </tr>
            </thead>
            <tbody>
                ${items.map(a => {
                    const usedMB = (a.used_bytes / 1048576).toFixed(1);
                    const pct = a.quota_mb > 0 ? Math.min(100, (a.used_bytes / (a.quota_mb * 1048576) * 100)).toFixed(0) : 0;
                    return `<tr style="border-bottom:1px solid var(--border-color)">
                        <td style="padding:8px;color:var(--text-primary)">${esc(a.email)}</td>
                        <td style="padding:8px;color:var(--text-secondary)">${usedMB} MB (${pct}%)</td>
                        <td style="padding:8px;color:var(--text-secondary)">${a.quota_mb} MB</td>
                        <td style="padding:8px">${a.enabled
                            ? '<span style="color:#16a34a">'+t('Aktywne')+'</span>'
                            : '<span style="color:#dc2626">'+t('Wyłączone')+'</span>'}</td>
                        <td style="padding:8px;text-align:right">
                            <button class="btn btn-xs ${PREFIX}-toggle-acc" data-email="${esc(a.email)}" data-enabled="${a.enabled}" title="${t('Przełącz')}">
                                <i class="fas fa-${a.enabled?'pause':'play'}"></i>
                            </button>
                            <button class="btn btn-xs ${PREFIX}-del-acc" data-email="${esc(a.email)}" title="${t('Usuń')}">
                                <i class="fas fa-trash" style="color:#dc2626"></i>
                            </button>
                        </td>
                    </tr>`;
                }).join('')}
                ${items.length === 0 ? `<tr><td colspan="5" style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak kont')}</td></tr>` : ''}
            </tbody>
        </table>`;

        el.querySelector(`#${PREFIX}-add-account`).onclick = () => showAccountDialog(el);
        el.querySelectorAll(`.${PREFIX}-del-acc`).forEach(btn => {
            btn.onclick = async () => {
                if (!confirm(t('Usunąć konto') + ' ' + btn.dataset.email + '?')) return;
                await api(`/mail-server/accounts/${encodeURIComponent(btn.dataset.email)}`, { method: 'DELETE' });
                renderAccountsTab(el);
            };
        });
        el.querySelectorAll(`.${PREFIX}-toggle-acc`).forEach(btn => {
            btn.onclick = async () => {
                await api(`/mail-server/accounts/${encodeURIComponent(btn.dataset.email)}`, {
                    method: 'PUT', body: { enabled: btn.dataset.enabled === '0' }
                });
                renderAccountsTab(el);
            };
        });
    }

    function showAccountDialog(parentEl) {
        const html = `<div style="padding:20px">
            <h3 style="margin:0 0 16px">${t('Nowe konto email')}</h3>
            <label style="font-size:13px;color:var(--text-secondary)">Email</label>
            <input type="email" id="${PREFIX}-new-email" class="input" placeholder="user@domain.com"
                style="width:100%;margin-bottom:10px;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            <label style="font-size:13px;color:var(--text-secondary)">${t('Hasło')}</label>
            <input type="password" id="${PREFIX}-new-pass" class="input"
                style="width:100%;margin-bottom:10px;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            <label style="font-size:13px;color:var(--text-secondary)">${t('Quota (MB)')}</label>
            <input type="number" id="${PREFIX}-new-quota" class="input" value="1024"
                style="width:100%;margin-bottom:16px;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            <button class="btn btn-primary" id="${PREFIX}-save-account" style="width:100%">${t('Utwórz')}</button>
        </div>`;
        const dlg = createWindow('mail-new-acc', { title: t('Nowe konto'), width: 400, height: 320, modal: true });
        dlg.body.innerHTML = html;
        dlg.body.querySelector(`#${PREFIX}-save-account`).onclick = async () => {
            const res = await api('/mail-server/accounts', { method: 'POST', body: {
                email: dlg.body.querySelector(`#${PREFIX}-new-email`).value,
                password: dlg.body.querySelector(`#${PREFIX}-new-pass`).value,
                quota_mb: parseInt(dlg.body.querySelector(`#${PREFIX}-new-quota`).value) || 1024,
            }});
            if (res.ok) { toast(t('Konto utworzone'), 'success'); closeWindow('mail-new-acc'); renderAccountsTab(parentEl); }
            else toast(res.error || t('Błąd'), 'error');
        };
    }

    // ─── Domains tab ─────────────────────────────────────
    async function renderDomainsTab(el) {
        const data = await api('/mail-server/domains');
        const items = data.items || [];

        el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h3 style="margin:0;color:var(--text-primary)">${t('Domeny')} (${items.length})</h3>
            <button class="btn btn-primary btn-sm" id="${PREFIX}-add-domain"><i class="fas fa-plus"></i> ${t('Dodaj domenę')}</button>
        </div>
        <div id="${PREFIX}-domains-list"></div>`;

        const list = el.querySelector(`#${PREFIX}-domains-list`);
        for (const d of items) {
            const div = document.createElement('div');
            div.style.cssText = 'border:1px solid var(--border-color);border-radius:8px;padding:16px;margin-bottom:12px;background:var(--bg-tertiary)';
            div.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <span style="font-size:15px;font-weight:600;color:var(--text-primary)">${esc(d.domain)}</span>
                    <div>
                        <span style="color:var(--text-muted);font-size:12px;margin-right:8px">${d.account_count} ${t('kont')}</span>
                        <button class="btn btn-xs ${PREFIX}-show-dns" data-domain="${esc(d.domain)}"><i class="fas fa-globe"></i> DNS</button>
                        <button class="btn btn-xs ${PREFIX}-del-domain" data-domain="${esc(d.domain)}"><i class="fas fa-trash" style="color:#dc2626"></i></button>
                    </div>
                </div>
                <div class="${PREFIX}-dns-panel" data-domain="${esc(d.domain)}" style="display:none"></div>`;
            list.appendChild(div);
        }

        if (items.length === 0) {
            list.innerHTML = `<div style="text-align:center;padding:30px;color:var(--text-muted)">${t('Brak domen')}</div>`;
        }

        el.querySelector(`#${PREFIX}-add-domain`).onclick = async () => {
            const domain = prompt(t('Podaj domenę (np. example.com):'));
            if (!domain) return;
            const res = await api('/mail-server/domains', { method: 'POST', body: { domain } });
            if (res.ok) { toast(t('Domena dodana'), 'success'); renderDomainsTab(el); }
            else toast(res.error || t('Błąd'), 'error');
        };

        el.querySelectorAll(`.${PREFIX}-del-domain`).forEach(btn => {
            btn.onclick = async () => {
                if (!confirm(t('Usunąć domenę') + ' ' + btn.dataset.domain + '? ' + t('Usunie to też wszystkie konta i aliasy!'))) return;
                await api(`/mail-server/domains/${encodeURIComponent(btn.dataset.domain)}`, { method: 'DELETE' });
                renderDomainsTab(el);
            };
        });

        el.querySelectorAll(`.${PREFIX}-show-dns`).forEach(btn => {
            btn.onclick = () => showDnsPanel(btn.dataset.domain, el);
        });
    }

    async function showDnsPanel(domain, parentEl) {
        const panel = parentEl.querySelector(`.${PREFIX}-dns-panel[data-domain="${domain}"]`);
        if (!panel) return;
        if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }

        panel.style.display = 'block';
        panel.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}`;

        const data = await api(`/mail-server/domains/${encodeURIComponent(domain)}/dns`);
        const records = data.items || [];

        let html = `
        <div style="margin:12px 0 8px">
            <div style="font-size:13px;font-weight:600;color:var(--text-primary);margin-bottom:4px">
                <i class="fas fa-info-circle" style="color:#3b82f6"></i> ${t('Dodaj te rekordy DNS u swojego dostawcy domeny:')}
            </div>
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">
                <i class="fas fa-lightbulb" style="color:#f59e0b"></i>
                ${t('Kolumna „Nazwa w DNS" pokazuje co wpisać w polu Name/Host u providera.')}
                ${t('{at} oznacza domenę główną (root).', { at: '<code style="background:var(--bg-tertiary);padding:1px 5px;border-radius:3px;font-size:11px">@</code>' })}
            </div>
            <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
                ${Object.keys(DNS_GUIDES).map(p => `<button class="btn btn-xs ${PREFIX}-dns-guide" data-provider="${p}">${p}</button>`).join('')}
            </div>
            <div id="${PREFIX}-dns-guide-steps" style="display:none;margin-bottom:12px;padding:10px;border-radius:6px;background:var(--bg-secondary);font-size:12px"></div>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:8px">
            <thead><tr style="border-bottom:1px solid var(--border-color)">
                <th style="padding:6px;text-align:left;color:var(--text-muted)">${t('Typ')}</th>
                <th style="padding:6px;text-align:left;color:var(--text-muted)">${t('Nazwa w DNS')}</th>
                <th style="padding:6px;text-align:left;color:var(--text-muted)">${t('Wartość')}</th>
                <th style="padding:6px;text-align:left;color:var(--text-muted)">${t('Co to jest?')}</th>
                <th style="padding:6px"></th>
            </tr></thead>
            <tbody>${records.map(r => `
                <tr style="border-bottom:1px solid var(--border-color)">
                    <td style="padding:6px"><span style="background:#3b82f6;color:#fff;padding:2px 6px;border-radius:3px;font-size:11px;font-weight:600">${esc(r.type)}</span></td>
                    <td style="padding:6px;color:var(--text-primary);word-break:break-all;max-width:160px">
                        <code style="background:var(--bg-tertiary);padding:2px 6px;border-radius:3px;font-weight:600">${esc(r.dns_name || r.name)}</code>
                        <div style="font-size:10px;color:var(--text-muted);margin-top:2px">${esc(r.name)}</div>
                    </td>
                    <td style="padding:6px;color:var(--text-primary);word-break:break-all;max-width:250px;font-family:monospace;font-size:11px">${esc(r.value)}</td>
                    <td style="padding:6px;color:var(--text-muted);font-size:11px">${esc(r.description)}</td>
                    <td style="padding:6px"><button class="btn btn-xs ${PREFIX}-copy-dns" data-value="${esc(r.value).replace(/"/g,'&quot;')}" title="${t('Kopiuj')}"><i class="fas fa-copy"></i></button></td>
                </tr>
            `).join('')}</tbody>
        </table>`;

        panel.innerHTML = html;

        panel.querySelectorAll(`.${PREFIX}-copy-dns`).forEach(btn => {
            btn.onclick = () => {
                navigator.clipboard.writeText(btn.dataset.value);
                toast(t('Skopiowano'), 'success');
            };
        });

        panel.querySelectorAll(`.${PREFIX}-dns-guide`).forEach(btn => {
            btn.onclick = () => {
                const guide = DNS_GUIDES[btn.dataset.provider];
                const stepsDiv = panel.querySelector(`#${PREFIX}-dns-guide-steps`);
                stepsDiv.style.display = 'block';
                stepsDiv.innerHTML = `
                    <div style="font-weight:600;margin-bottom:6px">${btn.dataset.provider} — ${t('instrukcja')}:</div>
                    <ol style="margin:0;padding-left:20px">${guide.steps.map(s => `<li style="margin-bottom:4px">${s}</li>`).join('')}</ol>
                    <a href="${guide.url}" target="_blank" style="color:#3b82f6;font-size:11px;margin-top:6px;display:inline-block">
                        <i class="fas fa-external-link-alt"></i> ${t('Pełna dokumentacja')}
                    </a>`;
            };
        });
    }

    // ─── Aliases tab ─────────────────────────────────────
    async function renderAliasesTab(el) {
        const data = await api('/mail-server/aliases');
        const items = data.items || [];

        el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <h3 style="margin:0;color:var(--text-primary)">${t('Aliasy / Przekierowania')} (${items.length})</h3>
            <button class="btn btn-primary btn-sm" id="${PREFIX}-add-alias"><i class="fas fa-plus"></i> ${t('Dodaj alias')}</button>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="border-bottom:1px solid var(--border-color)">
                <th style="padding:8px;text-align:left;color:var(--text-muted)">${t('Źródło')}</th>
                <th style="padding:8px;text-align:left;color:var(--text-muted)">→</th>
                <th style="padding:8px;text-align:left;color:var(--text-muted)">${t('Cel')}</th>
                <th style="padding:8px"></th>
            </tr></thead>
            <tbody>${items.map(a => `
                <tr style="border-bottom:1px solid var(--border-color)">
                    <td style="padding:8px;color:var(--text-primary)">${esc(a.source)}</td>
                    <td style="padding:8px;color:var(--text-muted)">→</td>
                    <td style="padding:8px;color:var(--text-primary)">${esc(a.destination)}</td>
                    <td style="padding:8px;text-align:right">
                        <button class="btn btn-xs ${PREFIX}-del-alias" data-id="${a.id}"><i class="fas fa-trash" style="color:#dc2626"></i></button>
                    </td>
                </tr>`).join('')}
                ${items.length === 0 ? `<tr><td colspan="4" style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak aliasów')}</td></tr>` : ''}
            </tbody>
        </table>`;

        el.querySelector(`#${PREFIX}-add-alias`).onclick = async () => {
            const source = prompt(t('Źródło (np. info@domain.com):'));
            if (!source) return;
            const dest = prompt(t('Cel (np. admin@domain.com):'));
            if (!dest) return;
            const res = await api('/mail-server/aliases', { method: 'POST', body: { source, destination: dest } });
            if (res.ok) { toast(t('Alias dodany'), 'success'); renderAliasesTab(el); }
            else toast(res.error || t('Błąd'), 'error');
        };

        el.querySelectorAll(`.${PREFIX}-del-alias`).forEach(btn => {
            btn.onclick = async () => {
                if (!confirm(t('Usunąć ten alias?'))) return;
                await api(`/mail-server/aliases/${btn.dataset.id}`, { method: 'DELETE' });
                renderAliasesTab(el);
            };
        });
    }

    // ─── Relay tab ───────────────────────────────────────
    async function renderRelayTab(el) {
        const relay = await api('/mail-server/relay');

        el.innerHTML = `
        <div style="max-width:500px">
            <h3 style="margin:0 0 8px;color:var(--text-primary)">${t('SMTP Relay')}</h3>
            <p style="color:var(--text-secondary);font-size:13px;margin:0 0 16px">
                ${t('Jeśli Twój ISP blokuje port 25, skonfiguruj relay przez zewnętrzny serwer SMTP (np. Gmail, Mailgun).')}
            </p>

            <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px">
                ${Object.entries(RELAY_PRESETS).map(([name, preset]) => `
                    <button class="btn btn-xs ${PREFIX}-relay-preset" data-host="${preset.host}" data-port="${preset.port}" data-note="${esc(preset.note)}">${name}</button>
                `).join('')}
            </div>
            <div id="${PREFIX}-relay-note" style="display:none;padding:8px;border-radius:6px;background:var(--bg-secondary);font-size:12px;color:var(--text-secondary);margin-bottom:12px"></div>

            <label style="display:flex;align-items:center;gap:8px;margin-bottom:12px;cursor:pointer">
                <input type="checkbox" id="${PREFIX}-relay-enabled" ${relay.enabled ? 'checked' : ''}>
                <span style="color:var(--text-primary);font-size:14px">${t('Włącz relay')}</span>
            </label>
            <div class="${PREFIX}-form-group" style="margin-bottom:10px">
                <label style="font-size:13px;color:var(--text-secondary)">${t('Serwer SMTP')}</label>
                <input type="text" id="${PREFIX}-relay-host" class="input" value="${esc(relay.host || '')}" placeholder="smtp.gmail.com"
                    style="width:100%;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            </div>
            <div class="${PREFIX}-form-group" style="margin-bottom:10px">
                <label style="font-size:13px;color:var(--text-secondary)">${t('Port')}</label>
                <input type="number" id="${PREFIX}-relay-port" class="input" value="${relay.port || 587}"
                    style="width:100px;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            </div>
            <div class="${PREFIX}-form-group" style="margin-bottom:10px">
                <label style="font-size:13px;color:var(--text-secondary)">${t('Użytkownik')}</label>
                <input type="text" id="${PREFIX}-relay-user" class="input" value="${esc(relay.username || '')}"
                    style="width:100%;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            </div>
            <div class="${PREFIX}-form-group" style="margin-bottom:16px">
                <label style="font-size:13px;color:var(--text-secondary)">${t('Hasło')}</label>
                <input type="password" id="${PREFIX}-relay-pass" class="input" value="${esc(relay.password || '')}"
                    style="width:100%;padding:6px 10px;border-radius:6px;border:1px solid var(--border-color);background:var(--input-bg);color:var(--text-primary)">
            </div>
            <button class="btn btn-primary" id="${PREFIX}-relay-save" style="width:100%"><i class="fas fa-save"></i> ${t('Zapisz')}</button>
        </div>`;

        el.querySelectorAll(`.${PREFIX}-relay-preset`).forEach(btn => {
            btn.onclick = () => {
                el.querySelector(`#${PREFIX}-relay-host`).value = btn.dataset.host;
                el.querySelector(`#${PREFIX}-relay-port`).value = btn.dataset.port;
                el.querySelector(`#${PREFIX}-relay-enabled`).checked = true;
                const note = el.querySelector(`#${PREFIX}-relay-note`);
                note.textContent = btn.dataset.note;
                note.style.display = 'block';
            };
        });

        el.querySelector(`#${PREFIX}-relay-save`).onclick = async () => {
            const res = await api('/mail-server/relay', { method: 'PUT', body: {
                enabled: el.querySelector(`#${PREFIX}-relay-enabled`).checked,
                host: el.querySelector(`#${PREFIX}-relay-host`).value,
                port: parseInt(el.querySelector(`#${PREFIX}-relay-port`).value) || 587,
                username: el.querySelector(`#${PREFIX}-relay-user`).value,
                password: el.querySelector(`#${PREFIX}-relay-pass`).value,
            }});
            if (res.ok) toast(t('Relay zapisany'), 'success');
            else toast(res.error || t('Błąd'), 'error');
        };
    }

    // ─── Logs tab ────────────────────────────────────────
    async function renderLogsTab(el) {
        el.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <h3 style="margin:0;color:var(--text-primary)">${t('Logi poczty')}</h3>
            <button class="btn btn-sm" id="${PREFIX}-refresh-logs"><i class="fas fa-sync"></i></button>
        </div>
        <pre id="${PREFIX}-log-content" style="flex:1;overflow:auto;padding:12px;border-radius:6px;background:var(--bg-tertiary);
            font-size:11px;font-family:monospace;color:var(--text-secondary);white-space:pre-wrap;word-break:break-all;
            border:1px solid var(--border-color);max-height:calc(100vh - 250px)">
            ${t('Ładowanie...')}
        </pre>`;

        async function loadLogs() {
            const data = await api('/mail-server/logs?lines=200');
            const logEl = el.querySelector(`#${PREFIX}-log-content`);
            if (logEl) logEl.textContent = (data.items || []).join('\n') || t('Brak logów');
        }
        loadLogs();
        el.querySelector(`#${PREFIX}-refresh-logs`).onclick = loadLogs;
    }

    // ─── Init ────────────────────────────────────────────
    init();
};
