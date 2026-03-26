/* ═══════════════════════════════════════════════════════════
   EthOS — Zarządzanie energią
   ═══════════════════════════════════════════════════════════ */

AppRegistry['power'] = function (appDef, launchOpts) {
    const w = createWindow('power', {
        title: t('Zarządzanie energią'),
        icon: 'fa-plug',
        iconColor: '#f59e0b',
        width: 680,
        height: 640,
    });
    const body = w.body;

    let state = { wol: {}, schedule: [], hdd: {}, cpu: {} };

    const CPU_GOV_INFO = {
        performance:  { label: 'Performance',  icon: 'fa-bolt',        color: '#ef4444', desc: 'Maksymalna wydajność, najwyższe zużycie energii' },
        powersave:    { label: 'Powersave',     icon: 'fa-leaf',        color: '#22c55e', desc: 'Minimalne zużycie energii, obniżona wydajność' },
        ondemand:     { label: 'On-demand',     icon: 'fa-gauge-high',  color: '#3b82f6', desc: 'Skaluje częstotliwość według obciążenia (zalecane)' },
        schedutil:    { label: 'Schedutil',     icon: 'fa-calendar',    color: '#8b5cf6', desc: 'Skaluje według schedulera jądra (nowoczesny)' },
        conservative: { label: 'Conservative', icon: 'fa-gauge',       color: '#f59e0b', desc: 'Skaluje wolniej niż ondemand, oszczędniejszy' },
        userspace:    { label: 'Userspace',     icon: 'fa-user-cog',   color: '#94a3b8', desc: 'Ręczne ustawienie przez aplikacje' },
    };

    const DAY_NAMES = ['Nd', 'Pn', 'Wt', 'Śr', 'Cz', 'Pt', 'So'];

    const HDD_OPTS = [
        { v: 0,   label: t('Wyłączone') },
        { v: 60,  label: '5 min'  },
        { v: 120, label: '10 min' },
        { v: 180, label: '15 min' },
        { v: 241, label: '30 min' },
        { v: 242, label: '1 godz' },
        { v: 243, label: '1.5 godz' },
        { v: 244, label: '2 godz' },
    ];

    function render() {
        const gov = state.cpu.current || 'unknown';
        const govInfo = CPU_GOV_INFO[gov] || { label: gov, icon: 'fa-microchip', color: '#94a3b8', desc: '' };
        const wolOn = state.wol.enabled;
        const hasCpu = (state.cpu.available || []).length > 0;

        body.innerHTML = `
        <div class="pwr-wrap">
            <!-- ── Status strip ── -->
            <div class="pwr-status-bar">
                <div class="pwr-status-chip">
                    <i class="fas fa-microchip" style="color:${govInfo.color}"></i>
                    <span>${govInfo.label}</span>
                </div>
                <div class="pwr-status-chip">
                    <i class="fas fa-network-wired" style="color:${wolOn ? '#22c55e' : '#94a3b8'}"></i>
                    <span>WOL ${wolOn ? t('włączone') : t('wyłączone')}</span>
                </div>
                ${state.wol.interface ? `<div class="pwr-status-chip pwr-iface">
                    <i class="fas fa-plug" style="color:#64748b"></i>
                    <span>${state.wol.interface}</span>
                </div>` : ''}
            </div>

            <div class="pwr-scroll">

            <!-- ── CPU Governor ── -->
            ${hasCpu ? `
            <div class="pwr-card">
                <div class="pwr-card-header">
                    <i class="fas fa-microchip app-icon-accent"></i>
                    <div>
                        <div class="pwr-card-title">${t('Profil wydajności CPU')}</div>
                        <div class="pwr-card-sub">${t('Kontroluje zużycie energii i taktowanie procesora')}</div>
                    </div>
                </div>
                <div class="pwr-gov-grid" id="pwr-gov-grid">
                    ${(state.cpu.available || []).map(g => {
                        const gi = CPU_GOV_INFO[g] || { label: g, icon: 'fa-microchip', color: '#94a3b8', desc: '' };
                        const active = g === (state.cpu.target || state.cpu.current);
                        return `<button class="pwr-gov-btn${active ? ' active' : ''}" data-gov="${g}">
                            <i class="fas ${gi.icon}" style="color:${gi.color}"></i>
                            <span class="pwr-gov-name">${gi.label}</span>
                            <span class="pwr-gov-desc">${gi.desc}</span>
                        </button>`;
                    }).join('')}
                </div>
            </div>` : ''}

            <!-- ── Wake-on-LAN ── -->
            <div class="pwr-card">
                <div class="pwr-card-header">
                    <i class="fas fa-network-wired" style="color:#3b82f6"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">Wake-on-LAN</div>
                        <div class="pwr-card-sub">${t('Zdalne uruchamianie przez sieć (Magic Packet)')}</div>
                    </div>
                    <label class="pwr-toggle">
                        <input type="checkbox" id="wol-enabled" ${wolOn ? 'checked' : ''}>
                        <span class="pwr-toggle-track"><span class="pwr-toggle-thumb"></span></span>
                    </label>
                </div>
                ${state.wol.interface ? `
                <div class="pwr-info-row">
                    <span class="pwr-info-label"><i class="fas fa-ethernet"></i> ${t('Interfejs')}</span>
                    <span class="pwr-info-val">${state.wol.interface}</span>
                    <span class="pwr-badge pwr-badge-${wolOn ? 'green' : 'gray'}">${state.wol.status}</span>
                </div>` : `
                <div class="pwr-info-row pwr-warn">
                    <i class="fas fa-triangle-exclamation"></i> ${t('Nie wykryto interfejsu sieciowego')}
                </div>`}
            </div>

            <!-- ── HDD Spindown ── -->
            ${Object.keys(state.hdd).length ? `
            <div class="pwr-card">
                <div class="pwr-card-header">
                    <i class="fas fa-hard-drive" style="color:#f59e0b"></i>
                    <div>
                        <div class="pwr-card-title">${t('Wygaszenie dysków (HDD Spindown)')}</div>
                        <div class="pwr-card-sub">${t('Automatyczne wyłączenie po bezczynności')}</div>
                    </div>
                </div>
                <div class="pwr-hdd-list" id="pwr-hdd-list">
                    ${Object.entries(state.hdd).map(([drive, val]) => `
                    <div class="pwr-hdd-row">
                        <i class="fas fa-circle-dot" style="color:#64748b;font-size:11px"></i>
                        <span class="pwr-hdd-name">/dev/<strong>${drive}</strong></span>
                        <select class="pwr-select hdd-select" data-drive="${drive}">
                            ${HDD_OPTS.map(o => `<option value="${o.v}" ${val == o.v ? 'selected' : ''}>${o.label}</option>`).join('')}
                        </select>
                    </div>`).join('')}
                </div>
            </div>` : ''}

            <!-- ── Harmonogram ── -->
            <div class="pwr-card">
                <div class="pwr-card-header">
                    <i class="fas fa-clock" style="color:#8b5cf6"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">${t('Harmonogram pracy')}</div>
                        <div class="pwr-card-sub">${t('Automatyczne wyłączanie i włączanie (Wake-on-RTC)')}</div>
                    </div>
                    <button class="pwr-add-btn" id="pwr-add-sched">
                        <i class="fas fa-plus"></i> ${t('Dodaj')}
                    </button>
                </div>
                <div id="pwr-sched-list">
                    ${state.schedule.length === 0 ? `
                    <div class="pwr-empty"><i class="fas fa-moon"></i> ${t('Brak reguł harmonogramu')}</div>
                    ` : state.schedule.map((rule, idx) => `
                    <div class="pwr-sched-row">
                        <label class="pwr-toggle pwr-toggle-sm">
                            <input type="checkbox" class="sched-enabled" data-idx="${idx}" ${rule.enabled ? 'checked' : ''}>
                            <span class="pwr-toggle-track"><span class="pwr-toggle-thumb"></span></span>
                        </label>
                        <div class="pwr-sched-info">
                            <div class="pwr-sched-days">${DAY_NAMES.filter((_, i) => rule.days.includes(i)).join(' · ')}</div>
                            <div class="pwr-sched-times">
                                <span><i class="fas fa-power-off" style="color:#ef4444"></i> ${rule.shutdown}</span>
                                <span><i class="fas fa-sun" style="color:#f59e0b"></i> ${rule.wakeup}</span>
                            </div>
                        </div>
                        <button class="pwr-icon-btn pwr-icon-danger remove-sched" data-idx="${idx}" title="${t('Usuń')}">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>`).join('')}
                </div>
            </div>

            </div><!-- /pwr-scroll -->

            <!-- ── Footer ── -->
            <div class="pwr-footer">
                <button class="pwr-save-btn" id="pwr-save">
                    <i class="fas fa-floppy-disk"></i> ${t('Zapisz ustawienia')}
                </button>
            </div>
        </div>`;

        // ── Events ──

        // CPU governor selection
        body.querySelectorAll('.pwr-gov-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                body.querySelectorAll('.pwr-gov-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.cpu.target = btn.dataset.gov;
            });
        });

        // HDD select
        body.querySelectorAll('.hdd-select').forEach(s => {
            s.addEventListener('change', e => {
                state.hdd[e.target.dataset.drive] = parseInt(e.target.value);
            });
        });

        // Schedule toggle
        body.querySelectorAll('.sched-enabled').forEach(cb => {
            cb.addEventListener('change', e => {
                state.schedule[parseInt(e.target.dataset.idx)].enabled = e.target.checked;
            });
        });

        // Remove schedule
        body.querySelectorAll('.remove-sched').forEach(btn => {
            btn.addEventListener('click', e => {
                const idx = parseInt(e.currentTarget.dataset.idx);
                state.schedule.splice(idx, 1);
                render();
            });
        });

        // Add schedule
        body.querySelector('#pwr-add-sched').addEventListener('click', showAddScheduleDialog);

        // Save
        body.querySelector('#pwr-save').addEventListener('click', saveSettings);
    }

    async function showAddScheduleDialog() {
        const overlay = document.createElement('div');
        overlay.className = 'pwr-overlay';
        overlay.innerHTML = `
        <div class="pwr-dialog">
            <div class="pwr-dialog-header">
                <i class="fas fa-clock" style="color:#8b5cf6"></i>
                <span>${t('Dodaj regułę harmonogramu')}</span>
                <button class="pwr-icon-btn pwr-dialog-close"><i class="fas fa-times"></i></button>
            </div>
            <div class="pwr-dialog-body">
                <div class="pwr-field">
                    <label>${t('Dni tygodnia')}</label>
                    <div class="pwr-day-picker">
                        ${DAY_NAMES.map((d, i) => `
                        <label class="pwr-day-btn">
                            <input type="checkbox" value="${i}" ${i >= 1 && i <= 5 ? 'checked' : ''}>
                            <span>${d}</span>
                        </label>`).join('')}
                    </div>
                </div>
                <div class="pwr-field-row">
                    <div class="pwr-field">
                        <label><i class="fas fa-power-off" style="color:#ef4444"></i> ${t('Wyłącz o')}</label>
                        <input type="time" id="pwr-sched-shutdown" value="23:00" class="pwr-input">
                    </div>
                    <div class="pwr-field">
                        <label><i class="fas fa-sun" style="color:#f59e0b"></i> ${t('Włącz o')}</label>
                        <input type="time" id="pwr-sched-wakeup" value="07:00" class="pwr-input">
                    </div>
                </div>
            </div>
            <div class="pwr-dialog-footer">
                <button class="pwr-btn-ghost pwr-dialog-cancel">${t('Anuluj')}</button>
                <button class="pwr-btn-primary pwr-dialog-confirm"><i class="fas fa-check"></i> ${t('Dodaj')}</button>
            </div>
        </div>`;

        body.appendChild(overlay);
        requestAnimationFrame(() => overlay.classList.add('visible'));

        const close = () => {
            overlay.classList.remove('visible');
            setTimeout(() => overlay.remove(), 200);
        };

        overlay.querySelector('.pwr-dialog-close').addEventListener('click', close);
        overlay.querySelector('.pwr-dialog-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        overlay.querySelector('.pwr-dialog-confirm').addEventListener('click', () => {
            const days = [...overlay.querySelectorAll('.pwr-day-picker input:checked')].map(c => parseInt(c.value));
            if (!days.length) { toast(t('Wybierz przynajmniej jeden dzień'), 'warning'); return; }
            const shutdown = overlay.querySelector('#pwr-sched-shutdown').value;
            const wakeup = overlay.querySelector('#pwr-sched-wakeup').value;
            state.schedule.push({ days, shutdown, wakeup, enabled: true });
            close();
            render();
        });
    }

    async function saveSettings() {
        const btn = body.querySelector('#pwr-save');
        btn.disabled = true;
        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Zapisywanie...')}`;

        const wolEnabled = body.querySelector('#wol-enabled').checked;
        try {
            await api('/power/save', {
                method: 'POST',
                body: {
                    cpu_governor: state.cpu.target || state.cpu.current,
                    wol_enabled: wolEnabled,
                    schedule: state.schedule,
                    hdd_spindown: state.hdd,
                }
            });
            toast(t('Zapisano ustawienia energii'), 'success');
            state.wol.enabled = wolEnabled;
            render();
        } catch (e) {
            toast(t('Błąd zapisu: ') + e.message, 'error');
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-floppy-disk"></i> ${t('Zapisz ustawienia')}`;
        }
    }

    async function load() {
        body.innerHTML = `<div class="pwr-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;
        try {
            const data = await api('/power/status');
            state = data;
            if (!state.cpu.target) state.cpu.target = state.cpu.current;
            render();
        } catch (e) {
            body.innerHTML = `<div class="pwr-loading pwr-error"><i class="fas fa-triangle-exclamation"></i> ${t('Błąd ładowania')}: ${e.message}</div>`;
        }
    }

    load();
};
