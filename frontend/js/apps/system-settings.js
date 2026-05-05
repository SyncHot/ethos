/* ═══════════════════════════════════════════════════════════
   EthOS — System Settings
   ═══════════════════════════════════════════════════════════ */



AppRegistry['system-settings'] = function (appDef) {
    createWindow('system-settings', {
        title: t('Ustawienia systemowe'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 720,
        height: 640,
        onRender: (body) => renderSystemSettings(body),
    });
};

async function renderSystemSettings(body) {
    const esc = (s) => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

    const style = document.createElement('style');
    style.textContent = `
        .ss-wrap { padding:18px; font-family:var(--font); color:var(--text); overflow-y:auto; height:100%; }

        /* Tabs */
        .ss-tabs { display:flex; gap:2px; background:var(--bg-card,#1e293b); border-radius:10px; padding:3px; margin-bottom:18px; }
        .ss-tab { flex:1; text-align:center; padding:10px 12px; border-radius:8px; font-size:12px; font-weight:500; cursor:pointer;
                   color:var(--text-muted); transition:all .15s; display:flex; align-items:center; justify-content:center; gap:6px; }
        .ss-tab:hover { color:var(--text); }
        .ss-tab.active { background:var(--bg-hover,#334155); color:var(--text); font-weight:600; }
        .ss-tab i { font-size:13px; }

        /* Section */
        .ss-section { display:none; }
        .ss-section.active { display:block; }
        .ss-section-title { font-size:14px; font-weight:600; margin:0 0 14px; display:flex; align-items:center; gap:8px; }
        .ss-section-title i { opacity:.6; font-size:13px; }

        /* Form */
        .ss-group { margin-bottom:18px; }
        .ss-group-title { font-size:12px; font-weight:600; color:var(--text-muted); text-transform:uppercase; letter-spacing:.5px; margin-bottom:10px; }
        .ss-row { display:flex; align-items:center; gap:14px; margin-bottom:12px; }
        .ss-row label { width:160px; font-size:13px; color:var(--text); flex-shrink:0; }
        .ss-row input, .ss-row select { flex:1; padding:8px 12px; border:1px solid var(--border,#334155); border-radius:8px;
                   background:var(--bg-input,#0f172a); color:var(--text); font-size:13px; outline:none; transition:border .15s; }
        .ss-row input:focus, .ss-row select:focus { border-color:#3b82f6; }
        .ss-row input[readonly] { opacity:.6; cursor:default; }
        .ss-hint { font-size:11px; color:var(--text-muted); margin:-6px 0 12px 174px; line-height:1.4; }

        /* Buttons */
        .ss-btn { display:inline-flex; align-items:center; gap:6px; padding:9px 18px; border:none; border-radius:8px;
                   font-size:12px; cursor:pointer; font-weight:500; transition:all .15s; }
        .ss-btn-primary { background:#3b82f6; color:#fff; }
        .ss-btn-primary:hover { background:#2563eb; }
        .ss-btn-danger { background:#991b1b; color:#fff; }
        .ss-btn-danger:hover { background:#7f1d1d; }
        .ss-btn-warn { background:#d97706; color:#fff; }
        .ss-btn-warn:hover { background:#b45309; }
        .ss-btn:disabled { opacity:.5; cursor:not-allowed; }
        .ss-actions { display:flex; gap:10px; margin-top:16px; }

        /* Info cards */
        .ss-info-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
        .ss-info-card { background:var(--bg-card,#1e293b); border-radius:10px; padding:14px 18px; }
        .ss-info-label { font-size:11px; color:var(--text-muted); text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
        .ss-info-value { font-size:15px; font-weight:600; }

        /* Messages */
        .ss-msg { padding:10px 14px; border-radius:8px; font-size:12px; margin-bottom:12px; display:flex; align-items:center; gap:8px; }
        .ss-msg-ok { background:rgba(34,197,94,.12); color:#22c55e; }
        .ss-msg-err { background:rgba(239,68,68,.12); color:#ef4444; }
        .ss-msg-warn { background:rgba(234,179,8,.12); color:#eab308; }

        /* Password strength */
        .ss-pw-strength { height:4px; border-radius:2px; margin:-4px 0 10px 174px; background:var(--border,#334155); overflow:hidden; }
        .ss-pw-strength-bar { height:100%; border-radius:2px; transition:width .3s, background .3s; }

        /* Factory Reset */
        .ss-reset-box { background:rgba(153,27,27,.08); border:1px solid rgba(153,27,27,.25); border-radius:12px; padding:20px; margin-top:4px; }
        .ss-reset-icon { font-size:36px; color:#ef4444; text-align:center; margin-bottom:12px; }
        .ss-reset-title { font-size:15px; font-weight:600; color:#ef4444; text-align:center; margin-bottom:8px; }
        .ss-reset-desc { font-size:12px; color:var(--text-muted); line-height:1.6; margin-bottom:14px; }
        .ss-reset-desc ul { margin:6px 0 0 18px; padding:0; }
        .ss-reset-desc ul li { margin-bottom:3px; }
        .ss-reset-opt { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--text); margin-bottom:12px; }
        .ss-reset-opt input[type=checkbox] { accent-color:#3b82f6; width:16px; height:16px; }
        .ss-reset-confirm { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
        .ss-reset-confirm label { font-size:12px; color:var(--text-muted); white-space:nowrap; }
        .ss-reset-confirm input { flex:1; padding:8px 12px; border:1px solid rgba(153,27,27,.4); border-radius:8px;
                   background:var(--bg-input,#0f172a); color:#ef4444; font-size:13px; font-weight:600; letter-spacing:1px;
                   outline:none; text-align:center; }
        .ss-reset-confirm input:focus { border-color:#ef4444; }
        .ss-reset-confirm input::placeholder { color:rgba(239,68,68,.3); font-weight:400; letter-spacing:0; }

        /* Language selector */
        .ss-lang-select { flex:1; position:relative; }

        /* 2FA Setup */
        .ss-2fa-setup { padding:4px 0; }
        .ss-2fa-step { font-size:13px; font-weight:600; color:var(--text-primary); margin:8px 0 6px; }
        .ss-2fa-qr { text-align:center; padding:8px 0; }
        .ss-2fa-input { width:120px; padding:10px 14px; border:1px solid var(--border,#334155); border-radius:8px;
                   background:var(--bg-input,#0f172a); color:var(--text); font-size:18px; font-weight:600;
                   letter-spacing:4px; text-align:center; outline:none; font-family:monospace; }
        .ss-2fa-input:focus { border-color:#3b82f6; }
        .ss-2fa-input::placeholder { letter-spacing:2px; font-weight:400; opacity:.4; }
        .ss-2fa-backup { display:flex; flex-wrap:wrap; gap:6px; padding:8px 0; }
        .ss-2fa-backup-code { display:inline-block; background:var(--bg-hover,#1e293b); border:1px solid var(--border,#334155);
                   border-radius:6px; padding:5px 10px; font-family:monospace; font-size:13px; font-weight:600;
                   color:var(--text-primary); user-select:all; letter-spacing:1px; }
        .ss-lang-btn { width:100%; padding:8px 12px; border:1px solid var(--border,#334155); border-radius:8px;
                   background:var(--bg-input,#0f172a); color:var(--text); font-size:13px; cursor:pointer;
                   display:flex; align-items:center; gap:8px; transition:border .15s; }
        .ss-lang-btn:hover, .ss-lang-btn.open { border-color:#3b82f6; }
        .ss-lang-btn .ss-lang-flag { font-size:18px; line-height:1; }
        .ss-lang-btn .ss-lang-arrow { margin-left:auto; font-size:10px; opacity:.5; transition:transform .15s; }
        .ss-lang-btn.open .ss-lang-arrow { transform:rotate(180deg); }
        .ss-lang-dropdown { position:absolute; top:calc(100% + 4px); left:0; right:0; background:var(--bg-card,#1e293b);
                   border:1px solid var(--border,#334155); border-radius:10px; max-height:240px; overflow-y:auto;
                   z-index:100; box-shadow:0 8px 24px rgba(0,0,0,.4); display:none; }
        .ss-lang-dropdown.open { display:block; }
        .ss-lang-opt { display:flex; align-items:center; gap:10px; padding:8px 14px; cursor:pointer;
                   font-size:13px; color:var(--text); transition:background .1s; }
        .ss-lang-opt:first-child { border-radius:10px 10px 0 0; }
        .ss-lang-opt:last-child { border-radius:0 0 10px 10px; }
        .ss-lang-opt:hover { background:var(--bg-hover,#334155); }
        .ss-lang-opt.selected { background:rgba(59,130,246,.15); color:#60a5fa; font-weight:600; }
        .ss-lang-opt .ss-lang-flag { font-size:18px; line-height:1; }

        @media (max-width: 640px) {
            .ss-row { flex-direction:column; align-items:stretch; gap:4px; }
            .ss-row label { width:auto; }
            .ss-hint { margin-left:0; }
            .ss-info-grid { grid-template-columns:1fr; }
            .ss-pw-strength { margin-left:0; }
        }

        /* Toggle switch */
        .ss-toggle { position:relative; width:44px; height:24px; flex-shrink:0; }
        .ss-toggle input { opacity:0; width:0; height:0; }
        .ss-toggle-slider { position:absolute; cursor:pointer; top:0; left:0; right:0; bottom:0;
                   background:var(--border,#334155); border-radius:24px; transition:.2s; }
        .ss-toggle-slider:before { content:''; position:absolute; height:18px; width:18px; left:3px; bottom:3px;
                   background:#fff; border-radius:50%; transition:.2s; }
        .ss-toggle input:checked + .ss-toggle-slider { background:#3b82f6; }
        .ss-toggle input:checked + .ss-toggle-slider:before { transform:translateX(20px); }

        /* SSL card */
        .ss-ssl-card { background:var(--bg-card,#1e293b); border-radius:10px; padding:16px 20px; margin-bottom:12px;
                   display:flex; align-items:center; justify-content:space-between; gap:14px; }
        .ss-ssl-status { display:flex; align-items:center; gap:10px; }
        .ss-ssl-dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
        .ss-ssl-info { font-size:12px; color:var(--text-muted); margin-top:2px; }
    `;
    body.appendChild(style);

    const wrap = document.createElement('div');
    wrap.className = 'ss-wrap';
    body.appendChild(wrap);

    let settings = {};
    let timezones = [];

    async function load() {
        try {
            settings = await api('/settings/');
        } catch (e) {
            wrap.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Błąd ładowania:')} ${esc(e.message)}</div>`;
            return;
        }
        render();
    }

    function render() {
        wrap.innerHTML = '';

        // Tabs
        const tabs = [
            { id: 'general', icon: 'fa-cog', label: t('Ogólne') },
            { id: 'network', icon: 'fa-network-wired', label: t('Sieć') },
            { id: 'security', icon: 'fa-shield-alt', label: t('Bezpieczeństwo') },
            { id: 'thermal', icon: 'fa-thermometer-half', label: t('Termika') },
            { id: 'maintenance', icon: 'fa-tools', label: t('Konserwacja') },
        ];
        const tabBar = document.createElement('div');
        tabBar.className = 'ss-tabs';
        tabs.forEach((t, i) => {
            const tab = document.createElement('div');
            tab.className = 'ss-tab' + (i === 0 ? ' active' : '');
            tab.dataset.tab = t.id;
            tab.innerHTML = `<i class="fas ${t.icon}"></i> ${t.label}`;
            tab.addEventListener('click', () => {
                tabBar.querySelectorAll('.ss-tab').forEach(x => x.classList.remove('active'));
                tab.classList.add('active');
                wrap.querySelectorAll('.ss-section').forEach(x => x.classList.remove('active'));
                wrap.querySelector(`[data-section="${t.id}"]`).classList.add('active');
            });
            tabBar.appendChild(tab);
        });
        wrap.appendChild(tabBar);

        // === General Section ===
        const generalHtml = `
            <div class="ss-section active" data-section="general">
                <div class="ss-section-title"><i class="fas fa-cog"></i> ${t('Ustawienia ogólne')}</div>
                <div class="ss-group">
                    <div class="ss-row">
                        <label>${t('Nazwa NAS')}</label>
                        <input type="text" id="ss-nas-name" value="${esc(settings.nas_name)}" maxlength="32" placeholder="EthOS">
                    </div>
                    <div class="ss-hint">${t('Wyświetlana na ekranie logowania i w pasku zadań')}</div>

                    <div class="ss-row">
                        <label>Hostname</label>
                        <input type="text" id="ss-hostname" value="${esc(settings.hostname)}" maxlength="63" placeholder="ethos">
                    </div>
                    <div class="ss-hint">${t('Nazwa sieciowa komputera (tylko litery, cyfry i myślniki)')}</div>

                    <div class="ss-row">
                        <label>${t('Strefa czasowa')}</label>
                        <select id="ss-timezone">
                            <option value="${esc(settings.timezone)}">${esc(settings.timezone)}</option>
                        </select>
                    </div>
                    <div class="ss-hint">
                        <a href="#" id="ss-load-tz" class="app-link-sm">
                            <i class="fas fa-sync-alt"></i> ${t('Załaduj pełną listę stref')}
                        </a>
                    </div>

                    <div class="ss-row">
                        <label>${t('Język interfejsu')}</label>
                        <div class="ss-lang-select">
                            <div class="ss-lang-btn" id="ss-lang-btn">
                                <span class="ss-lang-flag">${(I18n.supportedLangs[I18n.lang]||{}).flag||'🌐'}</span>
                                <span class="ss-lang-name">${(I18n.supportedLangs[I18n.lang]||{}).name||I18n.lang}</span>
                                <i class="fas fa-chevron-down ss-lang-arrow"></i>
                            </div>
                            <div class="ss-lang-dropdown" id="ss-lang-dropdown">
                                ${Object.entries(I18n.supportedLangs).map(([code, info]) =>
                                    `<div class="ss-lang-opt${code === I18n.lang ? ' selected' : ''}" data-lang="${code}">
                                        <span class="ss-lang-flag">${info.flag}</span>
                                        <span>${info.name}</span>
                                    </div>`
                                ).join('')}
                            </div>
                        </div>
                    </div>
                    <div class="ss-hint">${t('Zmiana języka odświeży cały interfejs')}</div>
                </div>

                <div class="ss-actions">
                    <button class="ss-btn ss-btn-primary" id="ss-save-general">
                        <i class="fas fa-save"></i> ${t('Zapisz zmiany')}
                    </button>
                </div>

                <div class="ss-group" style="margin-top:24px">
                    <div class="ss-group-title">${t('Automatyczne aktualizacje')}</div>
                    <div style="display:flex;align-items:center;gap:14px;">
                        <label class="ss-toggle">
                            <input type="checkbox" id="ss-auto-update" ${settings.auto_update ? 'checked' : ''}>
                            <span class="ss-toggle-slider"></span>
                        </label>
                        <div>
                            <div style="font-size:13px;font-weight:500">${t('Aktualizuj system automatycznie')}</div>
                            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">${t('System będzie automatycznie pobierał i instalował aktualizacje bezpieczeństwa')}</div>
                        </div>
                    </div>
                </div>
            </div>
        `;

        // === Network Section ===
        const networkHtml = `
            <div class="ss-section" data-section="network">
                <div class="ss-section-title"><i class="fas fa-network-wired"></i> ${t('Ustawienia sieciowe')}</div>
                <div class="ss-group">
                    <div class="ss-group-title">${t('Serwer WWW')}</div>
                    <div class="ss-row">
                        <label>${t('Port serwera')}</label>
                        <input type="number" id="ss-port" value="${settings.port}" min="1" max="65535" step="1">
                    </div>
                    <div class="ss-hint">${t('Port na którym działa interfejs EthOS (domyślnie: 9000)')}</div>
                    <div class="ss-msg ss-msg-warn app-ml-0">
                        <i class="fas fa-exclamation-triangle"></i>
                        ${t('Zmiana portu wymaga restartu serwera. Po restarcie otwórz')} <strong>http://&lt;adres-ip&gt;:&lt;nowy-port&gt;</strong>
                    </div>
                </div>

                <div class="ss-group" style="margin-top:24px">
                    <div class="ss-group-title"><i class="fas fa-lock" style="margin-right:6px;opacity:.6"></i> HTTPS / SSL</div>
                    <div id="ss-ssl-content">
                        <div style="text-align:center;padding:12px;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>
                    </div>
                </div>

                <div class="ss-actions">
                    <button class="ss-btn ss-btn-primary" id="ss-save-network">
                        <i class="fas fa-save"></i> ${t('Zapisz zmiany')}
                    </button>
                </div>
            </div>
        `;

        // === Security Section ===
        const securityHtml = `
            <div class="ss-section" data-section="security">
                <div class="ss-section-title"><i class="fas fa-shield-alt"></i> ${t('Bezpieczeństwo')}</div>
                <div class="ss-group">
                    <div class="ss-group-title">${t('Zmiana hasła')}</div>
                    <div id="ss-pw-msg"></div>
                    <div class="ss-row">
                        <label>${t('Obecne hasło')}</label>
                        <input type="password" id="ss-pw-current" placeholder="${t('Wpisz obecne hasło')}" autocomplete="current-password">
                    </div>
                    <div class="ss-row">
                        <label>${t('Nowe hasło')}</label>
                        <input type="password" id="ss-pw-new" placeholder="${t('Min. 4 znaki')}" autocomplete="new-password">
                    </div>
                    <div class="ss-pw-strength"><div class="ss-pw-strength-bar" id="ss-pw-bar"></div></div>
                    <div class="ss-row">
                        <label>${t('Powtórz nowe hasło')}</label>
                        <input type="password" id="ss-pw-confirm" placeholder="${t('Powtórz nowe hasło')}" autocomplete="new-password">
                    </div>
                </div>

                <div class="ss-actions">
                    <button class="ss-btn ss-btn-warn" id="ss-change-pw">
                        <i class="fas fa-key"></i> ${t('Zmień hasło')}
                    </button>
                </div>

                <div class="ss-group" style="margin-top:24px">
                    <div class="ss-group-title">${t('Uwierzytelnianie dwuskładnikowe (2FA)')}</div>
                    <div id="ss-2fa-msg"></div>
                    <div id="ss-2fa-content">
                        <div style="text-align:center;padding:12px;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>
                    </div>
                </div>
            </div>
        `;

        // (About section removed — info available in Resource Monitor)

        // (Performance section removed — sysctl is read-only, low value)

        // === Maintenance Section ===
        const maintenanceHtml = `
            <div class="ss-section" data-section="maintenance">
                <div class="ss-section-title"><i class="fas fa-tools"></i> ${t('Konserwacja systemu')}</div>

                <div class="ss-group" style="margin-bottom:24px">
                    <div class="ss-group-title">${t('Kopia konfiguracji')}</div>
                    <p style="font-size:13px;color:var(--text-muted);margin:0 0 12px">${t('Eksportuj lub importuj ustawienia systemowe jako plik ZIP.')}</p>
                    <div class="ss-actions">
                        <button class="ss-btn ss-btn-primary" id="ss-config-export">
                            <i class="fas fa-download"></i> ${t('Eksportuj konfigurację')}
                        </button>
                        <button class="ss-btn ss-btn-warn" id="ss-config-import">
                            <i class="fas fa-upload"></i> ${t('Importuj konfigurację')}
                        </button>
                        <input type="file" id="ss-config-file" accept=".zip" style="display:none">
                    </div>
                </div>

                <div class="ss-reset-box">
                    <div class="ss-reset-icon"><i class="fas fa-exclamation-triangle"></i></div>
                    <div class="ss-reset-title">${t('Przywracanie ustawień fabrycznych')}</div>
                    <div class="ss-reset-desc">
                        ${t('Ta operacja przywróci system do stanu początkowego, jak po pierwszej instalacji. Zostaną usunięte:')}
                        <ul>
                            <li>${t('Wszystkie ustawienia i konfiguracje')}</li>
                            <li>${t('Baza użytkowników i profile')}</li>
                            <li>${t('Udziały sieciowe (Samba, NFS)')}</li>
                            <li>${t('Certyfikaty SSL i domeny')}</li>
                            <li>${t('Logi, cache i miniaturki')}</li>
                            <li>${t('Kontenerach Docker (opcjonalnie)')}</li>
                        </ul>
                    </div>

                    <div class="ss-reset-opt">
                        <input type="checkbox" id="ss-reset-keep-docker">
                        <label for="ss-reset-keep-docker">${t('Zachowaj kontenery Docker (AppStore)')}</label>
                    </div>

                    <div class="ss-reset-confirm">
                        <label>${t('Wpisz RESET aby potwierdzić:')}</label>
                        <input type="text" id="ss-reset-input" placeholder="RESET" autocomplete="off" spellcheck="false">
                    </div>

                    <div id="ss-reset-msg"></div>

                    <div class="ss-actions app-justify-center">
                        <button class="ss-btn ss-btn-danger" id="ss-factory-reset" disabled>
                            <i class="fas fa-undo-alt"></i> ${t('Przywróć ustawienia fabryczne')}
                        </button>
                    </div>
                </div>
            </div>
        `;

        // (Firewall section removed — dedicated Firewall app is more complete)

        // === Thermal Section ===
        const thermalHtml = `
            <div class="ss-section" data-section="thermal">
                <div class="ss-section-title"><i class="fas fa-thermometer-half"></i> ${t('Termika i wentylatory')}</div>
                <div class="ss-group">
                    <div class="ss-group-title">${t('Strefy termiczne')}</div>
                    <div id="ss-thermal-zones" style="font-size:13px"><i class="fas fa-spinner fa-spin"></i></div>
                </div>
                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title">${t('Wentylatory')}</div>
                    <div id="ss-thermal-fans" style="font-size:13px"><i class="fas fa-spinner fa-spin"></i></div>
                </div>
                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title">${t('Polityka wentylatorów')}</div>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px" id="ss-fan-policies">
                        <button class="ss-btn" data-fan-policy="quiet"><i class="fas fa-volume-mute"></i> ${t('Cichy')}</button>
                        <button class="ss-btn" data-fan-policy="balanced"><i class="fas fa-balance-scale"></i> ${t('Zrównoważony')}</button>
                        <button class="ss-btn" data-fan-policy="performance"><i class="fas fa-bolt"></i> ${t('Wydajność')}</button>
                    </div>
                    <div style="margin-top:8px;font-size:12px;color:var(--text-muted)" id="ss-fan-current-policy"></div>
                </div>
                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title">${t('Awaryjne wyłączenie')}</div>
                    <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
                        <label style="font-size:13px">${t('Temperatura krytyczna (°C):')}</label>
                        <input type="number" id="ss-thermal-emergency" class="fm-input" style="width:80px" min="50" max="120" value="95">
                        <button class="ss-btn ss-btn-warn" id="ss-thermal-emergency-save"><i class="fas fa-save"></i> ${t('Zapisz')}</button>
                    </div>
                </div>
            </div>
        `;

        // (Hardware section removed — Resource Monitor provides same info + live monitoring)

        const container = document.createElement('div');
        container.innerHTML = generalHtml + networkHtml + securityHtml + maintenanceHtml + thermalHtml;
        wrap.appendChild(container);

        // -- Event: Language selector --
        const langBtn = wrap.querySelector('#ss-lang-btn');
        const langDrop = wrap.querySelector('#ss-lang-dropdown');
        if (langBtn && langDrop) {
            langBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                const isOpen = langDrop.classList.toggle('open');
                langBtn.classList.toggle('open', isOpen);
            });
            langDrop.querySelectorAll('.ss-lang-opt').forEach(opt => {
                opt.addEventListener('click', async () => {
                    const lang = opt.dataset.lang;
                    if (lang === I18n.lang) { langDrop.classList.remove('open'); langBtn.classList.remove('open'); return; }
                    langBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zmiana języka…');
                    langDrop.classList.remove('open');
                    await setLanguage(lang);
                    // Re-render the entire settings window to reflect new language
                    render();
                    toast(t('Język zmieniony na: ') + (I18n.supportedLangs[lang]||{}).name, 'success');
                });
            });
            // Close dropdown on outside click
            document.addEventListener('click', (e) => {
                if (!langBtn.contains(e.target) && !langDrop.contains(e.target)) {
                    langDrop.classList.remove('open');
                    langBtn.classList.remove('open');
                }
            });
        }

        // -- Event: Load timezones --
        wrap.querySelector('#ss-load-tz')?.addEventListener('click', async (e) => {
            e.preventDefault();
            if (timezones.length === 0) {
                const link = e.currentTarget;
                link.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}`;
                try {
                    timezones = await api('/settings/timezones');
                } catch (err) {
                    toast(t('Błąd ładowania stref: ') + err.message, 'error');
                    link.innerHTML = `<i class="fas fa-sync-alt"></i> ${t('Załaduj pełną listę stref')}`;
                    return;
                }
            }
            const sel = wrap.querySelector('#ss-timezone');
            const cur = settings.timezone;
            sel.innerHTML = timezones.map(tz =>
                `<option value="${esc(tz)}" ${tz === cur ? 'selected' : ''}>${esc(tz)}</option>`
            ).join('');
            toast(t('Załadowano ') + timezones.length + ' stref czasowych', 'info');
        });

        // -- Event: Save general --
        wrap.querySelector('#ss-save-general')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zapisywanie…');

            const payload = {
                nas_name: wrap.querySelector('#ss-nas-name').value.trim(),
                hostname: wrap.querySelector('#ss-hostname').value.trim(),
                timezone: wrap.querySelector('#ss-timezone').value,
            };

            try {
                const r = await api('/settings/', { method: 'POST', body: payload });
                if (r.ok) {
                    if (r.changes && r.changes.length) {
                        toast(r.changes.join('; '), 'success');
                    } else {
                        toast(t('Brak zmian'), 'info');
                    }
                    // Update NAS name in taskbar if changed
                    if (payload.nas_name && payload.nas_name !== settings.nas_name) {
                        NAS.nasName = payload.nas_name;
                        const nameEl = document.querySelector('.start-label, .taskbar-nas-name');
                        if (nameEl) nameEl.textContent = payload.nas_name;
                    }
                    settings = await api('/settings/');
                    render();
                } else {
                    toast(t('Błąd: ') + (r.errors || []).join('; '), 'error');
                }
            } catch (err) {
                toast(t('Błąd: ') + err.message, 'error');
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-save"></i> ${t('Zapisz zmiany')}`;
        });

        // -- Event: Auto-update toggle --
        wrap.querySelector('#ss-auto-update')?.addEventListener('change', async (e) => {
            const enabled = e.target.checked;
            try {
                const r = await api('/settings/auto-update', { method: 'POST', body: { enabled } });
                if (r.ok) {
                    settings.auto_update = r.auto_update;
                    toast(enabled ? t('Auto-aktualizacje włączone') : t('Auto-aktualizacje wyłączone'), 'success');
                } else {
                    e.target.checked = !enabled;
                    toast(r.error || t('Błąd'), 'error');
                }
            } catch (err) {
                e.target.checked = !enabled;
                toast(t('Błąd: ') + err.message, 'error');
            }
        });

        // -- Event: Save network --
        wrap.querySelector('#ss-save-network')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const newPort = parseInt(wrap.querySelector('#ss-port').value);
            if (!newPort || newPort < 1 || newPort > 65535) {
                toast(t('Nieprawidłowy port (1-65535)'), 'error');
                return;
            }

            if (newPort !== settings.port) {
                // Confirm restart
                if (!await confirmDialog(t('Zmiana portu z {old} na {new} wymaga restartu serwera.').replace('{old}', settings.port).replace('{new}', newPort) + `\n\n` + t('Po restarcie otwórz:') + `\nhttp://${location.hostname}:${newPort}\n\n` + t('Kontynuować?'))) {
                    return;
                }
            }

            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zapisywanie…');

            try {
                const r = await api('/settings/', { method: 'POST', body: { port: newPort } });
                if (r.ok) {
                    if (r.restart_needed) {
                        toast(t('Port zmieniony — restartowanie serwera…'), 'success');
                        // Trigger restart
                        await api('/settings/restart', { method: 'POST' });
                        // Show redirect countdown
                        let countdown = 8;
                        const msgDiv = document.createElement('div');
                        msgDiv.className = 'ss-msg ss-msg-warn';
                        msgDiv.style.marginLeft = '0';
                        msgDiv.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Serwer restartuje się… Przekierowanie za')} ${countdown}s`;
                        wrap.querySelector('[data-section="network"]').appendChild(msgDiv);
                        const iv = setInterval(() => {
                            countdown--;
                            msgDiv.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Serwer restartuje się… Przekierowanie za')} ${countdown}s`;
                            if (countdown <= 0) {
                                clearInterval(iv);
                                window.location.href = `http://${location.hostname}:${newPort}`;
                            }
                        }, 1000);
                    } else {
                        toast(r.changes?.join('; ') || t('Brak zmian'), r.changes?.length ? 'success' : 'info');
                    }
                } else {
                    toast(t('Błąd: ') + (r.errors || []).join('; '), 'error');
                    btn.disabled = false;
                    btn.innerHTML = `<i class="fas fa-save"></i> ${t('Zapisz zmiany')}`;
                }
            } catch (err) {
                toast(t('Błąd: ') + err.message, 'error');
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-save"></i> ${t('Zapisz zmiany')}`;
            }
        });

        // -- SSL section loader --
        async function _sslLoad() {
            const box = wrap.querySelector('#ss-ssl-content');
            if (!box) return;
            try {
                const s = await api('/settings/ssl/status');
                const active = s.ssl_active;
                const hasCert = !!s.cert;
                const selfSigned = s.self_signed;
                const certbot = s.certbot_installed;
                const domain = s.config?.domain || '';
                const port = s.https_port || 443;
                const sidePort = s.https_side_port || 9443;

                let html = '';

                // Self-signed status (always-on HTTPS alongside HTTP)
                if (selfSigned && !active) {
                    const ssInfo = s.self_signed_cert;
                    html += `<div class="ss-ssl-card">
                        <div class="ss-ssl-status">
                            <div class="ss-ssl-dot" style="background:#3b82f6"></div>
                            <div>
                                <div style="font-size:13px;font-weight:600">${t('HTTPS dostępne')} <span style="font-size:11px;color:var(--text-muted)">(self-signed)</span></div>
                                <div class="ss-ssl-info">${t('Port')}: ${sidePort} — ${t('certyfikat wygenerowany automatycznie przy instalacji')}</div>
                            </div>
                        </div>
                    </div>`;
                    if (ssInfo) {
                        html += `<div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
                            <i class="fas fa-shield-alt" style="margin-right:4px"></i>
                            ${esc(ssInfo.subject || 'ethos.local')} — ${t('ważny do')} <strong>${esc(ssInfo.not_after || '?')}</strong>
                        </div>`;
                    }
                    html += `<div class="ss-msg ss-msg-warn" style="margin-left:0">
                        <i class="fas fa-info-circle"></i>
                        ${t('Self-signed certyfikat powoduje ostrzeżenie w przeglądarce. Dla produkcji użyj Let\'s Encrypt poniżej.')}
                    </div>`;
                }

                // Let's Encrypt status card
                if (active) {
                    html += `<div class="ss-ssl-card">
                        <div class="ss-ssl-status">
                            <div class="ss-ssl-dot" style="background:#22c55e"></div>
                            <div>
                                <div style="font-size:13px;font-weight:600">${t('HTTPS aktywne')} (Let's Encrypt)</div>
                                <div class="ss-ssl-info">${t('Port')}: ${port}${domain ? ' — ' + esc(domain) : ''}</div>
                            </div>
                        </div>
                        <button class="ss-btn ss-btn-danger ss-btn-small" id="ss-ssl-disable"><i class="fas fa-times"></i> ${t('Wyłącz')}</button>
                    </div>`;
                }

                if (hasCert && s.cert) {
                    html += `<div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">
                        <i class="fas fa-certificate" style="margin-right:4px"></i>
                        ${t('Certyfikat')}: <strong>${esc(s.cert.subject || domain)}</strong>
                        — ${t('ważny do')} <strong>${esc(s.cert.not_after || '?')}</strong>
                        ${s.cert.issuer ? '('+esc(s.cert.issuer)+')' : ''}
                    </div>`;
                    if (!active) {
                        html += `<button class="ss-btn ss-btn-primary" id="ss-ssl-enable" style="margin-bottom:12px">
                            <i class="fas fa-lock"></i> ${t("Włącz HTTPS (Let's Encrypt)")}
                        </button>`;
                    }
                }

                // Let's Encrypt setup section
                html += `<div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border,#334155)">
                    <div style="font-size:13px;font-weight:600;margin-bottom:10px"><i class="fas fa-certificate" style="margin-right:6px;opacity:.6"></i> Let's Encrypt</div>`;

                if (!certbot) {
                    html += `<div class="ss-msg ss-msg-warn" style="margin-left:0">
                        <i class="fas fa-info-circle"></i>
                        ${t('Certbot nie jest zainstalowany.')}
                        <button class="ss-btn ss-btn-primary" id="ss-ssl-install-certbot" style="margin-left:auto;padding:6px 12px;font-size:11px">
                            <i class="fas fa-download"></i> ${t('Zainstaluj Certbot')}
                        </button>
                    </div>`;
                } else if (!hasCert) {
                    html += `<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">${t("Uzyskaj darmowy certyfikat Let's Encrypt:")}</div>
                    <div class="ss-row">
                        <label>${t('Domena')}</label>
                        <input type="text" id="ss-ssl-domain" placeholder="nas.example.com" value="${esc(domain)}">
                    </div>
                    <div class="ss-row">
                        <label>${t('E-mail')}</label>
                        <input type="email" id="ss-ssl-email" placeholder="admin@example.com" value="${esc(s.config?.email || '')}">
                    </div>
                    <div class="ss-row">
                        <label>${t('Port HTTPS')}</label>
                        <input type="number" id="ss-ssl-port" value="${port}" min="1" max="65535">
                    </div>
                    <div class="ss-hint" style="margin-left:0">${t('Domena musi wskazywać na ten serwer (port 80 musi być dostępny)')}</div>
                    <button class="ss-btn ss-btn-primary" id="ss-ssl-obtain">
                        <i class="fas fa-certificate"></i> ${t('Uzyskaj certyfikat')}
                    </button>`;
                } else {
                    html += `<div style="font-size:12px;color:var(--text-muted)">${t("Certyfikat Let's Encrypt zainstalowany.")}</div>`;
                }
                html += `</div>`;

                box.innerHTML = html;

                // Handlers
                box.querySelector('#ss-ssl-install-certbot')?.addEventListener('click', async (btn_e) => {
                    const b = btn_e.currentTarget;
                    b.disabled = true; b.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Instalowanie…');
                    try {
                        const r = await api('/settings/ssl/install-certbot', { method: 'POST' });
                        if (r.error) { toast(r.error, 'error'); } else { toast(t('Certbot zainstalowany'), 'success'); _sslLoad(); }
                    } catch (e) { toast(e.message, 'error'); }
                });

                box.querySelector('#ss-ssl-obtain')?.addEventListener('click', async (btn_e) => {
                    const b = btn_e.currentTarget;
                    const dm = box.querySelector('#ss-ssl-domain')?.value.trim();
                    const em = box.querySelector('#ss-ssl-email')?.value.trim();
                    const pt = parseInt(box.querySelector('#ss-ssl-port')?.value) || 443;
                    if (!dm) { toast(t('Podaj domenę'), 'error'); return; }
                    if (!em) { toast(t('Podaj e-mail'), 'error'); return; }
                    b.disabled = true; b.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Uzyskiwanie…');
                    try {
                        const r = await api('/settings/ssl/obtain', { method: 'POST', body: { domain: dm, email: em, https_port: pt } });
                        if (r.error) { toast(r.error, 'error'); } else { toast(r.message || t('Certyfikat uzyskany'), 'success'); _sslLoad(); }
                    } catch (e) { toast(e.message, 'error'); }
                    b.disabled = false; b.innerHTML = `<i class="fas fa-certificate"></i> ${t('Uzyskaj certyfikat')}`;
                });

                box.querySelector('#ss-ssl-enable')?.addEventListener('click', async () => {
                    try {
                        const r = await api('/settings/ssl/enable', { method: 'POST', body: { enable: true, https_port: port } });
                        if (r.error) { toast(r.error, 'error'); } else {
                            toast(r.message || t('HTTPS włączone — wymagany restart'), 'success');
                            _sslLoad();
                        }
                    } catch (e) { toast(e.message, 'error'); }
                });

                box.querySelector('#ss-ssl-disable')?.addEventListener('click', async () => {
                    try {
                        const r = await api('/settings/ssl/enable', { method: 'POST', body: { enable: false } });
                        if (r.error) { toast(r.error, 'error'); } else {
                            toast(r.message || t('HTTPS wyłączone — wymagany restart'), 'success');
                            _sslLoad();
                        }
                    } catch (e) { toast(e.message, 'error'); }
                });

            } catch (e) {
                box.innerHTML = `<div class="ss-msg ss-msg-err" style="margin-left:0"><i class="fas fa-exclamation-circle"></i> ${esc(e.message)}</div>`;
            }
        }
        _sslLoad();

        // -- Event: Password strength indicator --
        wrap.querySelector('#ss-pw-new')?.addEventListener('input', (e) => {
            const pw = e.target.value;
            const bar = wrap.querySelector('#ss-pw-bar');
            let str = 0;
            if (pw.length >= 4) str++;
            if (pw.length >= 8) str++;
            if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) str++;
            if (/[0-9]/.test(pw)) str++;
            if (/[^a-zA-Z0-9]/.test(pw)) str++;
            const pct = Math.min(str * 20, 100);
            const colors = ['#ef4444', '#ef4444', '#eab308', '#f59e0b', '#22c55e', '#22c55e'];
            bar.style.width = pct + '%';
            bar.style.background = colors[str] || '#ef4444';
        });

        // -- Event: Change password --
        wrap.querySelector('#ss-change-pw')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const msgDiv = wrap.querySelector('#ss-pw-msg');
            msgDiv.innerHTML = '';

            const curPw = wrap.querySelector('#ss-pw-current').value;
            const newPw = wrap.querySelector('#ss-pw-new').value;
            const confirmPw = wrap.querySelector('#ss-pw-confirm').value;

            if (!curPw) { msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Podaj obecne hasło')}</div>`; return; }
            if (!newPw || newPw.length < 4) { msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Nowe hasło musi mieć min. 4 znaki')}</div>`; return; }
            if (newPw !== confirmPw) { msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Hasła nie są identyczne')}</div>`; return; }

            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zmieniam…');

            try {
                const r = await api('/settings/change-password', {
                    method: 'POST',
                    body: { current_password: curPw, new_password: newPw }
                });
                if (r.ok) {
                    msgDiv.innerHTML = `<div class="ss-msg ss-msg-ok"><i class="fas fa-check-circle"></i> ${t('Hasło zostało zmienione')}</div>`;
                    wrap.querySelector('#ss-pw-current').value = '';
                    wrap.querySelector('#ss-pw-new').value = '';
                    wrap.querySelector('#ss-pw-confirm').value = '';
                    const bar = wrap.querySelector('#ss-pw-bar');
                    bar.style.width = '0%';
                } else {
                    msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(r.error || t('Błąd'))}</div>`;
                }
            } catch (err) {
                msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(err.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-key"></i> ${t('Zmień hasło')}`;
        });

        // -- 2FA Setup --
        const _2faContent = wrap.querySelector('#ss-2fa-content');
        const _2faMsg = wrap.querySelector('#ss-2fa-msg');

        async function _2faLoad() {
            try {
                const st = await api('/totp/status');
                if (st.enabled) {
                    _2faContent.innerHTML = `
                        <div style="display:flex;align-items:center;gap:12px;padding:10px 0">
                            <i class="fas fa-check-circle" style="font-size:22px;color:#22c55e"></i>
                            <div>
                                <div style="font-weight:600;font-size:14px;color:var(--text-primary)">${t('2FA jest włączone')}</div>
                                <div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${t('Twoje konto jest chronione kodem jednorazowym z aplikacji Authenticator')}</div>
                            </div>
                        </div>
                        <div class="ss-actions">
                            <button class="ss-btn ss-btn-danger" id="ss-2fa-disable"><i class="fas fa-times-circle"></i> ${t('Wyłącz 2FA')}</button>
                        </div>`;
                    wrap.querySelector('#ss-2fa-disable').onclick = () => _2faDisableFlow();
                } else {
                    _2faContent.innerHTML = `
                        <div style="display:flex;align-items:center;gap:12px;padding:10px 0">
                            <i class="fas fa-shield-alt" style="font-size:22px;color:var(--text-muted)"></i>
                            <div>
                                <div style="font-weight:600;font-size:14px;color:var(--text-primary)">${t('2FA nie jest włączone')}</div>
                                <div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${t('Dodaj dodatkową warstwę ochrony za pomocą aplikacji Google Authenticator, Authy itp.')}</div>
                            </div>
                        </div>
                        <div class="ss-actions">
                            <button class="ss-btn ss-btn-primary" id="ss-2fa-enable"><i class="fas fa-qrcode"></i> ${t('Włącz 2FA')}</button>
                        </div>`;
                    wrap.querySelector('#ss-2fa-enable').onclick = () => _2faSetupFlow();
                }
            } catch {
                _2faContent.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Nie udało się sprawdzić statusu 2FA')}</div>`;
            }
        }

        async function _2faSetupFlow() {
            _2faMsg.innerHTML = '';
            _2faContent.innerHTML = `<div style="text-align:center;padding:16px;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i> ${t('Generowanie klucza...')}</div>`;
            try {
                const setup = await api('/totp/setup', { method: 'POST' });
                if (setup.error) { _2faMsg.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(setup.error)}</div>`; _2faLoad(); return; }
                _2faContent.innerHTML = `
                    <div class="ss-2fa-setup">
                        <div class="ss-2fa-step">${t('1. Zeskanuj kod QR w aplikacji Authenticator')}</div>
                        <div class="ss-2fa-qr"><img src="${setup.qr_code_base64}" alt="QR" style="max-width:200px;border-radius:8px;background:#fff;padding:8px"></div>
                        <div class="ss-hint" style="text-align:center;margin:4px 0 12px">${t('Lub wpisz ręcznie klucz:')} <code style="font-size:12px;background:var(--bg-hover);padding:2px 6px;border-radius:4px;user-select:all">${esc(setup.secret)}</code></div>
                        <div class="ss-2fa-step">${t('2. Wpisz 6-cyfrowy kod z aplikacji')}</div>
                        <div style="display:flex;gap:8px;align-items:center;margin:8px 0">
                            <input type="text" id="ss-2fa-code" class="ss-2fa-input" maxlength="6" inputmode="numeric" pattern="[0-9]*" placeholder="000000" autocomplete="one-time-code">
                            <button class="ss-btn ss-btn-primary" id="ss-2fa-verify"><i class="fas fa-check"></i> ${t('Weryfikuj')}</button>
                            <button class="ss-btn" id="ss-2fa-cancel">${t('Anuluj')}</button>
                        </div>
                        <div class="ss-2fa-step" style="margin-top:16px">${t('3. Zapisz kody awaryjne (backup)')}</div>
                        <div class="ss-2fa-backup">${setup.backup_codes.map(c => `<span class="ss-2fa-backup-code">${c}</span>`).join('')}</div>
                        <div class="ss-hint" style="margin-top:6px"><i class="fas fa-exclamation-triangle" style="color:#f59e0b"></i> ${t('Każdy kod można użyć tylko raz. Zapisz je w bezpiecznym miejscu.')}</div>
                    </div>`;
                const codeInput = wrap.querySelector('#ss-2fa-code');
                codeInput.focus();
                codeInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') wrap.querySelector('#ss-2fa-verify').click(); });
                wrap.querySelector('#ss-2fa-cancel').onclick = () => _2faLoad();
                wrap.querySelector('#ss-2fa-verify').onclick = async () => {
                    const code = codeInput.value.trim();
                    if (!/^\d{6}$/.test(code)) { _2faMsg.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Wpisz 6-cyfrowy kod')}</div>`; return; }
                    _2faMsg.innerHTML = '';
                    const r = await api('/totp/verify', { method: 'POST', body: { code } });
                    if (r.ok) {
                        _2faMsg.innerHTML = `<div class="ss-msg ss-msg-ok"><i class="fas fa-check-circle"></i> ${t('2FA zostało włączone!')}</div>`;
                        _2faLoad();
                    } else {
                        _2faMsg.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(r.error || t('Nieprawidłowy kod'))}</div>`;
                    }
                };
            } catch (e) {
                _2faMsg.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(e.message)}</div>`;
                _2faLoad();
            }
        }

        async function _2faDisableFlow() {
            _2faMsg.innerHTML = '';
            _2faContent.innerHTML = `
                <div style="padding:10px 0">
                    <div style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">${t('Wpisz kod z aplikacji Authenticator lub kod awaryjny, aby wyłączyć 2FA.')}</div>
                    <div style="display:flex;gap:8px;align-items:center">
                        <input type="text" id="ss-2fa-dis-code" class="ss-2fa-input" maxlength="8" inputmode="numeric" placeholder="${t('Kod 2FA lub backup')}" autocomplete="one-time-code">
                        <button class="ss-btn ss-btn-danger" id="ss-2fa-dis-confirm"><i class="fas fa-times-circle"></i> ${t('Wyłącz')}</button>
                        <button class="ss-btn" id="ss-2fa-dis-cancel">${t('Anuluj')}</button>
                    </div>
                </div>`;
            const inp = wrap.querySelector('#ss-2fa-dis-code');
            inp.focus();
            inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') wrap.querySelector('#ss-2fa-dis-confirm').click(); });
            wrap.querySelector('#ss-2fa-dis-cancel').onclick = () => _2faLoad();
            wrap.querySelector('#ss-2fa-dis-confirm').onclick = async () => {
                const val = inp.value.trim();
                if (!val) { _2faMsg.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Wpisz kod')}</div>`; return; }
                const body = val.length === 6 ? { code: val } : { backup_code: val };
                const r = await api('/totp/disable', { method: 'POST', body });
                if (r.ok) {
                    _2faMsg.innerHTML = `<div class="ss-msg ss-msg-ok"><i class="fas fa-check-circle"></i> ${t('2FA zostało wyłączone')}</div>`;
                    _2faLoad();
                } else {
                    _2faMsg.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(r.error || t('Nieprawidłowy kod'))}</div>`;
                }
            };
        }

        _2faLoad();

        // -- Event: Factory Reset confirm input --
        const resetInput = wrap.querySelector('#ss-reset-input');
        const resetBtn = wrap.querySelector('#ss-factory-reset');
        if (resetInput && resetBtn) {
            resetInput.addEventListener('input', () => {
                resetBtn.disabled = resetInput.value.trim() !== 'RESET';
            });

            resetBtn.addEventListener('click', async () => {
                const confirmed = await showPowerConfirm(
                    t('Przywracanie ustawień fabrycznych'),
                    'fa-exclamation-triangle',
                    '#ef4444'
                );
                if (!confirmed) return;

                const msgDiv = wrap.querySelector('#ss-reset-msg');
                const keepDocker = wrap.querySelector('#ss-reset-keep-docker')?.checked || false;

                resetBtn.disabled = true;
                resetBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Resetowanie…');
                msgDiv.innerHTML = `<div class="ss-msg ss-msg-warn"><i class="fas fa-spinner fa-spin"></i> ${t('Trwa przywracanie ustawień fabrycznych… Nie zamykaj okna.')}</div>`;

                try {
                    const r = await api('/settings/factory-reset', {
                        method: 'POST',
                        body: { confirm: 'RESET', keep_docker: keepDocker },
                    });
                    if (r.ok) {
                        msgDiv.innerHTML = `<div class="ss-msg ss-msg-ok"><i class="fas fa-check-circle"></i> ${esc(r.message)}</div>`;
                        // Show restart overlay
                        showRestartOverlay(t('Przywracanie ustawień fabrycznych — serwer restartuje się…'));
                    } else {
                        msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(r.error || t('Błąd'))}</div>`;
                        resetBtn.disabled = false;
                        resetBtn.innerHTML = `<i class="fas fa-undo-alt"></i> ${t('Przywróć ustawienia fabryczne')}`;
                    }
                } catch (err) {
                    msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(err.message)}</div>`;
                    resetBtn.disabled = false;
                    resetBtn.innerHTML = `<i class="fas fa-undo-alt"></i> ${t('Przywróć ustawienia fabryczne')}`;
                }
            });
        }

        // -- Config Export/Import --
        const exportBtn = wrap.querySelector('#ss-config-export');
        const importBtn = wrap.querySelector('#ss-config-import');
        const configFile = wrap.querySelector('#ss-config-file');

        if (exportBtn) {
            exportBtn.addEventListener('click', async () => {
                exportBtn.disabled = true;
                exportBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Eksportowanie…');
                try {
                    const resp = await fetch('/api/settings/config/export', {
                        method: 'POST',
                        headers: { 'Authorization': 'Bearer ' + NAS.token, 'X-CSRF-Token': NAS.csrfToken },
                    });
                    if (!resp.ok) throw new Error('Export failed');
                    const blob = await resp.blob();
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = resp.headers.get('Content-Disposition')?.match(/filename="?(.+?)"?$/)?.[1] || 'ethos_config.zip';
                    a.click();
                    URL.revokeObjectURL(url);
                    toast(t('Konfiguracja wyeksportowana'), 'success');
                } catch (e) { toast(e.message, 'error'); }
                exportBtn.disabled = false;
                exportBtn.innerHTML = `<i class="fas fa-download"></i> ${t('Eksportuj konfigurację')}`;
            });
        }

        if (importBtn && configFile) {
            importBtn.addEventListener('click', () => configFile.click());
            configFile.addEventListener('change', async () => {
                const file = configFile.files[0];
                if (!file) return;
                if (!await confirmDialog(t('Importować konfigurację? Obecne ustawienia zostaną nadpisane.'))) {
                    configFile.value = '';
                    return;
                }
                importBtn.disabled = true;
                importBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Importowanie…');
                try {
                    const fd = new FormData();
                    fd.append('file', file);
                    const resp = await fetch('/api/settings/config/import', {
                        method: 'POST',
                        headers: { 'Authorization': 'Bearer ' + NAS.token, 'X-CSRF-Token': NAS.csrfToken },
                        body: fd,
                    });
                    const r = await resp.json();
                    if (r.error) { toast(r.error, 'error'); }
                    else { toast(r.message || t('Konfiguracja zaimportowana'), 'success'); }
                } catch (e) { toast(e.message, 'error'); }
                importBtn.disabled = false;
                importBtn.innerHTML = `<i class="fas fa-upload"></i> ${t('Importuj konfigurację')}`;
                configFile.value = '';
            });
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  SSL & Domains — MIGRATED to Domains Manager app
    //  (domains.js, /api/domains-mgr/*)
    // ═══════════════════════════════════════════════════════════

    // -- Thermal Logic --
    async function loadThermal() {
        try {
            const data = await api('/power/thermal');
            const zonesEl = wrap.querySelector('#ss-thermal-zones');
            const fansEl = wrap.querySelector('#ss-thermal-fans');
            const policyEl = wrap.querySelector('#ss-fan-current-policy');
            const emergEl = wrap.querySelector('#ss-thermal-emergency');
            if (zonesEl) {
                const zones = data.zones || [];
                zonesEl.innerHTML = zones.length ? zones.map(z => {
                    const c = z.temp_c > 80 ? '#ef4444' : z.temp_c > 60 ? '#eab308' : '#10b981';
                    return `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)"><span style="min-width:160px;font-weight:500">${esc(z.name || z.type)}</span><span style="color:${c};font-weight:700">${z.temp_c.toFixed(1)}°C</span></div>`;
                }).join('') : `<span style="color:var(--text-muted)">${t('Brak danych')}</span>`;
            }
            if (fansEl) {
                const fans = data.fans || [];
                fansEl.innerHTML = fans.length ? fans.map(f =>
                    `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)"><span style="min-width:120px;font-weight:500">${esc(f.name || 'Fan')}</span><span><i class="fas fa-fan" style="margin-right:4px"></i>${f.rpm || 0} RPM</span>${f.pwm_pct != null ? `<span style="color:var(--text-muted)">(${f.pwm_pct}%)</span>` : ''}</div>`
                ).join('') : `<span style="color:var(--text-muted)">${t('Brak wentylatorów')}</span>`;
            }
            if (policyEl) policyEl.textContent = t('Aktualna polityka:') + ' ' + (data.policy || 'auto');
            if (emergEl && data.emergency_threshold) emergEl.value = data.emergency_threshold;
            // Highlight active policy
            wrap.querySelectorAll('[data-fan-policy]').forEach(b => {
                b.classList.toggle('ss-btn-primary', b.dataset.fanPolicy === data.policy);
            });
        } catch(e) { /* ignore on VMs without hwmon */ }
    }

    wrap.querySelectorAll('[data-fan-policy]').forEach(btn => {
        btn.addEventListener('click', async () => {
            try {
                const r = await api('/power/thermal/policy', {method:'PUT', body:{policy: btn.dataset.fanPolicy}});
                if (r.error) { toast(r.error,'error'); return; }
                toast(t('Polityka zmieniona'),'success');
                loadThermal();
            } catch(e) { toast(e.message,'error'); }
        });
    });

    const emergSaveBtn = wrap.querySelector('#ss-thermal-emergency-save');
    if (emergSaveBtn) {
        emergSaveBtn.addEventListener('click', async () => {
            const val = parseInt(wrap.querySelector('#ss-thermal-emergency').value);
            if (isNaN(val) || val < 50 || val > 120) { toast(t('Zakres: 50-120°C'),'error'); return; }
            try {
                const r = await api('/power/thermal/emergency', {method:'PUT', body:{threshold_celsius: val}});
                if (r.error) { toast(r.error,'error'); return; }
                toast(t('Próg zapisany'),'success');
            } catch(e) { toast(e.message,'error'); }
        });
    }

    // Load thermal on tab switch (lazy)
    const origTabClick = wrap.querySelectorAll('.ss-tab');
    origTabClick.forEach(tab => {
        tab.addEventListener('click', () => {
            if (tab.dataset.tab === 'thermal') loadThermal();
        });
    });

    load();
}
