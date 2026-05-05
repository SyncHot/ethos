/* ═══════════════════════════════════════════════════════════
   EthOS — Event Log
   ═══════════════════════════════════════════════════════════ */

window.AppRegistry = window.AppRegistry || {};
const AppRegistry = window.AppRegistry;


AppRegistry['event-log'] = function (appDef) {
    createWindow('event-log', {
        title: t('Dziennik zdarzeń'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1000,
        height: 650,
        onRender: (body) => renderEventLog(body),
    });
};

function renderEventLog(body) {
    const state = {
        events: [],
        total: 0,
        offset: 0,
        limit: 100,
        filter: { category: '', level: '', search: '' },
        autoScroll: true,
        stats: { total: 0, by_category: {}, by_level: {} },
    };

    const CATEGORIES = {
        system: { icon: 'fa-cog', label: 'System', color: '#3b82f6' },
        files: { icon: 'fa-folder', label: 'Pliki', color: '#f59e0b' },
        backup: { icon: 'fa-shield-alt', label: 'Backup', color: '#06b6d4' },
        docker: { icon: 'fa-cubes', label: 'Docker', color: '#2496ed' },
        storage: { icon: 'fa-hdd', label: 'Dyski', color: '#10b981' },
        network: { icon: 'fa-network-wired', label: t('Sieć'), color: '#0ea5e9' },
        printer: { icon: 'fa-print', label: 'Druk', color: '#ef4444' },
        security: { icon: 'fa-shield-halved', label: t('Bezpieczeństwo'), color: '#a855f7' },
        error: { icon: 'fa-exclamation-triangle', label: t('Błąd'), color: '#ef4444' },
        frontend: { icon: 'fa-desktop', label: 'Frontend', color: '#8b5cf6' },
    };

    const LEVELS = {
        debug: { icon: 'fa-bug', color: '#6b7280' },
        info: { icon: 'fa-info-circle', color: '#3b82f6' },
        warning: { icon: 'fa-exclamation-triangle', color: '#f59e0b' },
        error: { icon: 'fa-times-circle', color: '#ef4444' },
    };

    body.innerHTML = `
        <div class="elog">
            <div class="elog-toolbar">
                <div class="elog-filters">
                    <select id="elog-cat-filter" class="elog-select" title="Kategoria">
                        <option value="">Wszystkie kategorie</option>
                        ${Object.entries(CATEGORIES).map(([k, v]) =>
                            `<option value="${k}">${v.label}</option>`
                        ).join('')}
                    </select>
                    <select id="elog-level-filter" class="elog-select" title="Poziom">
                        <option value="">Wszystkie poziomy</option>
                        <option value="error">${t('Błędy')}</option>
                        <option value="warning">${t('Ostrzeżenia')}</option>
                        <option value="info">Info</option>
                        <option value="debug">Debug</option>
                    </select>
                    <div class="elog-search-wrap">
                        <i class="fas fa-search"></i>
                        <input type="text" id="elog-search" class="elog-search" placeholder="Szukaj w logach…">
                    </div>
                </div>
                <div class="elog-actions">
                    <button class="elog-btn" id="elog-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
                    <button class="elog-btn elog-btn-danger" id="elog-clear" title="${t('Wyczyść dziennik')}"><i class="fas fa-trash"></i> ${t('Wyczyść')}</button>
                </div>
            </div>
            <div class="elog-stats" id="elog-stats"></div>
            <div class="elog-list" id="elog-list"></div>
            <div class="elog-statusbar" id="elog-statusbar"></div>
        </div>
    `;

    function renderStats() {
        const el = body.querySelector('#elog-stats');
        const s = state.stats;
        el.innerHTML = Object.entries(CATEGORIES).map(([key, cat]) => {
            const count = s.by_category[key] || 0;
            if (!count) return '';
            return `<span class="elog-stat-chip" data-cat="${key}" title="${cat.label}">
                <i class="fas ${cat.icon}" style="color:${cat.color}"></i> ${count}
            </span>`;
        }).join('') + `
            ${(s.by_level.error || 0) > 0 ? `<span class="elog-stat-chip elog-stat-errors"><i class="fas fa-times-circle app-icon-danger"></i> ${s.by_level.error} ${t('błędów')}</span>` : ''}
            ${(s.by_level.warning || 0) > 0 ? `<span class="elog-stat-chip elog-stat-warnings"><i class="fas fa-exclamation-triangle app-text-warn"></i> ${s.by_level.warning} ${t('ostrzeżeń')}</span>` : ''}
        `;

        // Click stat chip to filter by category
        el.querySelectorAll('.elog-stat-chip[data-cat]').forEach(chip => {
            chip.style.cursor = 'pointer';
            chip.addEventListener('click', () => {
                const cat = chip.dataset.cat;
                const sel = body.querySelector('#elog-cat-filter');
                sel.value = state.filter.category === cat ? '' : cat;
                sel.dispatchEvent(new Event('change'));
            });
        });
    }

    function renderEvents() {
        const list = body.querySelector('#elog-list');
        if (!state.events.length) {
            list.innerHTML = `<div class="elog-empty"><i class="fas fa-rectangle-list"></i><p>${t('Brak zdarzeń')}</p></div>`;
            return;
        }

        list.innerHTML = state.events.map(ev => {
            const cat = CATEGORIES[ev.category] || CATEGORIES.system;
            const lvl = LEVELS[ev.level] || LEVELS.info;
            const details = ev.details ? `<div class="elog-details">${formatDetails(ev.details)}</div>` : '';
            return `
                <div class="elog-entry elog-level-${ev.level}">
                    <div class="elog-entry-time">${ev.time}</div>
                    <span class="elog-entry-cat" style="color:${cat.color}" title="${cat.label}">
                        <i class="fas ${cat.icon}"></i>
                    </span>
                    <span class="elog-entry-level" style="color:${lvl.color}" title="${ev.level}">
                        <i class="fas ${lvl.icon}"></i>
                    </span>
                    <div class="elog-entry-msg">${escapeHtml(ev.message)}${details}</div>
                </div>
            `;
        }).join('');

        // Scroll to top (newest first)
        list.scrollTop = 0;

        // Update statusbar
        body.querySelector('#elog-statusbar').textContent =
            `${t('Wyświetlono')} ${state.events.length} ${t('z')} ${state.total} ${t('zdarzeń')}`;
    }

    function escapeHtml(str) {
        return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function formatDetails(details) {
        if (!details || typeof details !== 'object') return '';
        const parts = [];
        for (const [key, val] of Object.entries(details)) {
            const valStr = Array.isArray(val) ? val.join(', ') : String(val);
            parts.push(`<span class="elog-detail-key">${escapeHtml(key)}:</span> ${escapeHtml(valStr)}`);
        }
        return parts.join(' · ');
    }

    async function loadEvents() {
        try {
            const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
            if (state.filter.category) params.set('category', state.filter.category);
            if (state.filter.level) params.set('level', state.filter.level);
            if (state.filter.search) params.set('search', state.filter.search);

            const [evData, statsData] = await Promise.all([
                api(`/eventlog?${params}`),
                api('/eventlog/stats')
            ]);

            state.events = evData.events || [];
            state.total = evData.total || 0;
            state.stats = statsData;

            renderStats();
            renderEvents();
        } catch (e) {
            body.querySelector('#elog-list').innerHTML =
                `<div class="elog-empty"><i class="fas fa-exclamation-triangle"></i><p>${t('Błąd ładowania logów')}</p></div>`;
        }
    }

    // Filter handlers
    body.querySelector('#elog-cat-filter').addEventListener('change', (e) => {
        state.filter.category = e.target.value;
        state.offset = 0;
        loadEvents();
    });
    body.querySelector('#elog-level-filter').addEventListener('change', (e) => {
        state.filter.level = e.target.value;
        state.offset = 0;
        loadEvents();
    });

    let searchTimer;
    body.querySelector('#elog-search').addEventListener('input', (e) => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            state.filter.search = e.target.value;
            state.offset = 0;
            loadEvents();
        }, 300);
    });

    body.querySelector('#elog-refresh').addEventListener('click', () => loadEvents());
    body.querySelector('#elog-clear').addEventListener('click', async () => {
        if (!await confirmDialog(t('Wyczyścić cały dziennik zdarzeń?'))) return;
        await api('/eventlog/clear', { method: 'POST' });
        loadEvents();
    });

    // Real-time updates via socketio
    const onNewEvent = () => {
        // Only auto-refresh if at top (no offset, no filters)
        if (state.offset === 0) loadEvents();
    };
    if (NAS.socket) {
        NAS.socket.on('eventlog_new', onNewEvent);
    }

    // Initial load
    loadEvents();

    // Auto-refresh every 30s
    const interval = setInterval(() => {
        if (!WM.windows.has('event-log')) {
            clearInterval(interval);
            if (NAS.socket) NAS.socket.off('eventlog_new', onNewEvent);
            return;
        }
        loadEvents();
    }, 30000);
}

