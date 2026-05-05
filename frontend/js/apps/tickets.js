/* ─────────────────── Tickets / Kanban Board (EthOS) ─────────────────── */
/* globals AppRegistry, createWindow, NAS, api, toast, t */

(function () {
if (AppRegistry['tickets']) return;

AppRegistry['tickets'] = function (appDef, launchOpts) {
    const winId = 'tickets';
    createWindow(winId, {
        title: t('Tickets'),
        icon: appDef?.icon || 'fa-columns',
        iconColor: appDef?.color || '#8b5cf6',
        width: 1200,
        height: 750,
        minWidth: 800,
        minHeight: 500,
        onRender: (body) => renderTickets(body, launchOpts),
    });
};

/* ═══════════════════════════ CONSTANTS ═══════════════════════════ */

const PRIORITY_COLORS = {
    critical: '#ef4444',
    high: '#f97316',
    medium: '#eab308',
    low: '#22c55e',
};

const PRIORITY_LABELS = {
    critical: 'Krytyczny',
    high: 'Wysoki',
    medium: t('Średni'),
    low: 'Niski',
};

const PRIORITY_ICONS = {
    critical: '🔴',
    high: '🟠',
    medium: '🟡',
    low: '🟢',
};

const TICKET_TYPES = {
    task:    { icon: 'fa-check-square', color: '#4c9aff', label: 'Task' },
    bug:     { icon: 'fa-bug',          color: '#ef4444', label: 'Bug' },
    epic:    { icon: 'fa-bolt',         color: '#6554c0', label: 'Epic' },
    subtask: { icon: 'fa-minus-square', color: '#36b37e', label: 'Subtask' },
};

const COMPLEXITY_LEVELS = {
    simple:  { label: 'Prosty', color: '#22c55e' },
    medium:  { label: t('Średni'), color: '#f59e0b' },
    complex: { label: t('Złożony'), color: '#ef4444' },
};

const LABEL_COLORS = [
    '#3b82f6', '#ef4444', '#22c55e', '#f59e0b', '#8b5cf6',
    '#ec4899', '#06b6d4', '#f97316', '#14b8a6', '#6366f1',
];

const DEFAULT_COLUMNS = ['Backlog', 'Do zrobienia', 'W trakcie', 'QA', 'Review', 'Gotowe'];

function _escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _tkTypeIcon(type) {
    const info = TICKET_TYPES[type] || TICKET_TYPES.task;
    return `<i class="fas ${info.icon}" style="color:${info.color};font-size:12px;" title="${info.label}"></i>`;
}

/* ═══════════════════════════ MODAL HELPER ═══════════════════════════ */

function tkShowModal(title, contentHTML, confirmLabel, onConfirm) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" style="width:550px;max-width:95vw;max-height:90vh;display:flex;flex-direction:column;">
            <div class="modal-header">
                <span>${title}</span>
                <button class="modal-close"><i class="fas fa-times"></i></button>
            </div>
            <div class="modal-body" style="overflow-y:auto;flex:1;">${contentHTML}</div>
            <div class="modal-footer">
                <button class="btn btn-secondary tk-modal-cancel">${t('Anuluj')}</button>
                <button class="btn btn-primary tk-modal-confirm">${_escHtml(confirmLabel || t('Zapisz'))}</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector('.modal-close').onclick = () => overlay.remove();
    overlay.querySelector('.tk-modal-cancel').onclick = () => overlay.remove();
    overlay.querySelector('.tk-modal-confirm').onclick = () => {
        if (onConfirm(overlay) !== false) overlay.remove();
    };
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    return overlay;
}

function tkConfirm(title, message, onYes) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" style="width:400px;">
            <div class="modal-header">
                <span>${title}</span>
                <button class="modal-close"><i class="fas fa-times"></i></button>
            </div>
            <div class="modal-body"><p>${message}</p></div>
            <div class="modal-footer">
                <button class="btn btn-secondary tk-modal-cancel">${t('Anuluj')}</button>
                <button class="btn btn-primary" style="background:#ef4444;" id="tk-confirm-yes">${t('Usuń')}</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector('.modal-close').onclick = () => overlay.remove();
    overlay.querySelector('.tk-modal-cancel').onclick = () => overlay.remove();
    overlay.querySelector('#tk-confirm-yes').onclick = () => { onYes(); overlay.remove(); };
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
}

/* ═══════════════════════════ LABEL COLOR HELPER ═══════════════════════════ */

function tkLabelColor(label) {
    let hash = 0;
    for (let i = 0; i < label.length; i++) hash = label.charCodeAt(i) + ((hash << 5) - hash);
    return LABEL_COLORS[Math.abs(hash) % LABEL_COLORS.length];
}

function _shortId(id) {
    // t_006c13830d66 → #006c13
    return '#' + (id || '').replace(/^t_/, '').slice(0, 6);
}

/* ═══════════════════════════ MAIN RENDER ═══════════════════════════ */

async function renderTickets(body, launchOpts) {
    /* ── closure state ── */
    let projects = [];
    let currentProject = null;
    let tickets = [];
    let ticketChildren = {};
    let ticketIsEpic = new Set();
    let comments = [];
    let filterAssignee = '';
    let filterPriority = '';
    let filterSearch = '';
    let selectedTickets = new Set();
    let watcherExecuting = null;  // { executing, ticket_id, model, elapsed, ... }

    function watcherLabel(we) {
        if (!we) return '';
        const agentMap = { localai: 'Local AI', freemodel: 'Free Model', copilot: 'Copilot' };
        const agent = agentMap[we.agent] || 'Copilot';
        const model = we.model_label || we.model || '';
        return model ? (agent + ' · ' + model) : agent;
    }

    /* ── root container ── */
    body.innerHTML = '<div class="tk-app"><div class="tk-loading" style="padding:2rem;text-align:center;"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div></div>';
    const app = body.querySelector('.tk-app');

    /* ── real-time updates via Socket.IO ── */
    window._onTicketsEvent = (ev) => {
        if (!currentProject) {
            if (ev.type.startsWith('project_') || ev.type === 'ticket_created' || ev.type === 'ticket_deleted') {
                loadProjects().then(renderProjectList);
            }
            return;
        }
        if (ev.type === 'watcher_activity') {
            // lightweight: just refresh watcher state + re-render cards (no ticket reload)
            watcherExecuting = (ev.executing && ev.ticket_id) ? ev : null;
            renderBoard();
            return;
        }
        if (ev.project_id !== currentProject.id) return;
        Promise.all([loadTickets(currentProject.id), loadWatcherState()]).then(() => renderBoard());
    };

    /* ── navigation ── */
    async function showProjectList() {
        currentProject = null;
        tickets = [];
        filterAssignee = '';
        filterPriority = '';
        filterSearch = '';
        selectedTickets.clear();
        await loadProjects();
        renderProjectList();
    }

    async function showBoard(project) {
        currentProject = project;
        selectedTickets.clear();
        await Promise.all([loadTickets(project.id), loadWatcherState()]);
        renderBoard();
    }

    /* ═══════════════════ API CALLS ═══════════════════ */

    async function loadProjects() {
        try {
            const data = await api('/tickets/projects');
            projects = data.projects || [];
        } catch (e) {
            toast(t('Błąd ładowania projektów'), 'error');
            projects = [];
        }
    }

    async function loadTickets(projectId) {
        try {
            const data = await api('/tickets/projects/' + projectId);
            tickets = data.tickets || [];

            // Build epic -> children mapping from labels like 'epic:<id>'
            ticketChildren = {};
            ticketIsEpic = new Set();
            tickets.forEach(t => {
                (t.labels || []).forEach(l => {
                    const m = String(l).match(/^epic:(.+)$/);
                    if (m) {
                        const eid = m[1];
                        ticketChildren[eid] = ticketChildren[eid] || [];
                        ticketChildren[eid].push(t);
                        t.parent = eid;
                    }
                });
            });
            // Mark epic tickets that exist in the current list
            Object.keys(ticketChildren).forEach(eid => {
                const epicTicket = tickets.find(tt => tt.id === eid);
                if (epicTicket) ticketIsEpic.add(eid);
            });

        } catch (e) {
            toast(t('Błąd ładowania ticketów'), 'error');
            tickets = [];
        }
    }

    async function loadWatcherState() {
        try {
            const data = await api('/tickets/watcher/executing');
            watcherExecuting = data && data.executing ? data : null;
        } catch (e) {
            watcherExecuting = null;
        }
    }

    async function createProject(payload) {
        try {
            const data = await api('/tickets/projects', { method: 'POST', body: payload });
            if (data.error) throw new Error(data.error);
            toast(t('Projekt utworzony'), 'success');
            await showProjectList();
        } catch (e) {
            toast(t('Błąd tworzenia projektu'), 'error');
        }
    }

    async function updateProject(id, payload) {
        try {
            const data = await api('/tickets/projects/' + id, { method: 'PUT', body: payload });
            if (data.error) throw new Error(data.error);
            toast(t('Projekt zaktualizowany'), 'success');
            if (currentProject) {
                Object.assign(currentProject, payload);
                renderBoard();
            } else {
                await showProjectList();
            }
        } catch (e) {
            toast(t('Błąd aktualizacji projektu'), 'error');
        }
    }

    async function deleteProject(id) {
        try {
            const data = await api('/tickets/projects/' + id, { method: 'DELETE' });
            if (data.error) throw new Error(data.error);
            toast(t('Projekt usunięty'), 'success');
            await showProjectList();
        } catch (e) {
            toast(t('Błąd usuwania projektu'), 'error');
        }
    }

    async function createTicket(payload) {
        try {
            const data = await api('/tickets/tickets', {
                method: 'POST', body: Object.assign({project_id: currentProject.id}, payload),
            });
            if (data.error) throw new Error(data.error);
            toast(t('Ticket utworzony'), 'success');
            await loadTickets(currentProject.id);
            renderBoard();
        } catch (e) {
            toast(t('Błąd tworzenia ticketu'), 'error');
        }
    }

    async function updateTicket(id, payload) {
        try {
            const data = await api('/tickets/tickets/' + id, { method: 'PUT', body: payload });
            if (data.error) throw new Error(data.error);
            toast(t('Ticket zaktualizowany'), 'success');
            await loadTickets(currentProject.id);
            renderBoard();
        } catch (e) {
            toast(t('Błąd aktualizacji ticketu'), 'error');
        }
    }

    async function deleteTicket(id) {
        try {
            const data = await api('/tickets/tickets/' + id, { method: 'DELETE' });
            if (data.error) throw new Error(data.error);
            toast(t('Ticket usunięty'), 'success');
            await loadTickets(currentProject.id);
            renderBoard();
        } catch (e) {
            toast(t('Błąd usuwania ticketu'), 'error');
        }
    }

    async function deleteSelectedTickets() {
        if (selectedTickets.size === 0) return;
        const ids = Array.from(selectedTickets);
        
        tkConfirm(
            t('Usuń tickety'),
            t('Czy na pewno chcesz usunąć') + ' ' + ids.length + ' ' + t('zaznaczonych ticketów?'),
            async () => {
                 try {
                    await Promise.all(ids.map(id => api('/tickets/tickets/' + id, { method: 'DELETE' })));
                    selectedTickets.clear();
                    updateSelectionToolbar();
                    toast(t('Tickety usunięte'), 'success');
                    await loadTickets(currentProject.id);
                    renderBoard();
                } catch (err) {
                    console.error(err);
                    toast(t('Błąd podczas usuwania'), 'error');
                }
            }
        );
    }

    async function moveTicket(id, column, order) {
        try {
            await api('/tickets/tickets/' + id + '/move', {
                method: 'PUT', body: { column, order },
            });
            await loadTickets(currentProject.id);
            renderBoard();
        } catch (e) {
            toast(t('Błąd przenoszenia ticketu'), 'error');
        }
    }

    function loadComments(ticketId) {
        const ticket = tickets.find(t => t.id === ticketId);
        return (ticket && ticket.comments) || [];
    }

    async function addComment(ticketId, text) {
        try {
            await api('/tickets/tickets/' + ticketId + '/comments', {
                method: 'POST', body: { text },
            });
            return true;
        } catch (e) {
            toast(t('Błąd dodawania komentarza'), 'error');
            return false;
        }
    }

    /* ═══════════════════ PROJECT LIST VIEW ═══════════════════ */

    function renderProjectList() {
        const searchVal = filterSearch;
        const filtered = searchVal
            ? projects.filter(p => p.name.toLowerCase().includes(searchVal.toLowerCase()))
            : projects;

        app.innerHTML = `
            <div class="tk-toolbar">
                <button class="tk-btn tk-btn-primary" id="tk-new-project">
                    <i class="fas fa-plus"></i> ${t('Nowy projekt')}
                </button>
                <div style="flex:1;"></div>
                <div class="tk-search-wrap">
                    <i class="fas fa-search"></i>
                    <input type="text" class="tk-search" placeholder="${t('Szukaj projektów...')}"
                           value="${_escHtml(searchVal)}" />
                </div>
            </div>
            <div class="tk-projects-grid" id="tk-pgrid"></div>
        `;

        const grid = app.querySelector('#tk-pgrid');

        if (filtered.length === 0) {
            grid.innerHTML = `<div class="tk-empty">
                <i class="fas fa-clipboard-list" style="font-size:3rem;opacity:0.3;"></i>
                <p>${t('Brak projektów')}</p>
            </div>`;
        } else {
            grid.innerHTML = filtered.map(p => {
                const color = p.color || '#8b5cf6';
                const totalTickets = p.ticket_count ?? p.tickets_count ?? 0;
                const inProgress = p.in_progress_count ?? 0;
                const members = (p.members || []).slice(0, 3);
                const memberStr = members.map(m => _escHtml(m)).join(', ');
                const extraMembers = (p.members || []).length > 3
                    ? ' +' + ((p.members || []).length - 3) : '';
                const isOwner = p.owner === NAS.user?.username;

                return `<div class="tk-project-card" data-id="${_escHtml(p.id)}" style="border-top:4px solid ${_escHtml(color)};">
                    <div class="tk-project-card-header">
                        <span class="tk-project-dot" style="background:${_escHtml(color)};"></span>
                        <span class="tk-project-name">${_escHtml(p.name)}</span>
                    </div>
                    ${p.description ? '<p class="tk-project-desc">' + _escHtml(p.description) + '</p>' : ''}
                    <div class="tk-project-stats">
                        <span><i class="fas fa-ticket" style="opacity:0.5;margin-right:3px;"></i>${totalTickets}</span>
                        <span><i class="fas fa-spinner" style="opacity:0.5;margin-right:3px;"></i>${inProgress}</span>
                        <span><i class="fas fa-check" style="opacity:0.5;margin-right:3px;"></i>${p.done_count ?? 0}</span>
                    </div>
                    <div class="tk-project-footer">
                        <span class="tk-project-members">${memberStr}${_escHtml(extraMembers)}</span>
                        <span class="tk-actions">
                            <button class="tk-act-btn" data-id="${_escHtml(p.id)}" title="${t('Ustawienia')}"><i class="fas fa-pen"></i></button>
                            ${isOwner ? '<button class="tk-act-btn danger tk-delete-project" data-id="' + _escHtml(p.id) + '" title="' + t('Usuń') + '"><i class="fas fa-trash"></i></button>' : ''}
                        </span>
                    </div>
                </div>`;
            }).join('');
        }

        /* ── event bindings ── */
        app.querySelector('#tk-new-project').onclick = () => showProjectModal();

        app.querySelector('.tk-search').oninput = (e) => {
            filterSearch = e.target.value;
            renderProjectList();
        };

        grid.querySelectorAll('.tk-project-card').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('.tk-act-btn')) return;
                const proj = projects.find(p => String(p.id) === card.dataset.id);
                if (proj) showBoard(proj);
            });
        });

        grid.querySelectorAll('.tk-act-btn:not(.danger)').forEach(btn => {
            btn.onclick = (e) => {
                e.stopPropagation();
                const proj = projects.find(p => String(p.id) === btn.dataset.id);
                if (proj) showProjectModal(proj);
            };
        });

        grid.querySelectorAll('.tk-delete-project').forEach(btn => {
            btn.onclick = (e) => {
                e.stopPropagation();
                const proj = projects.find(p => String(p.id) === btn.dataset.id);
                if (proj) {
                    tkConfirm(
                        t('Usuń projekt'),
                        t('Czy na pewno chcesz usunąć projekt') + ' <strong>' + _escHtml(proj.name) + '</strong>?',
                        () => deleteProject(proj.id)
                    );
                }
            };
        });
    }

    /* ═══════════════════ PROJECT MODAL ═══════════════════ */

    async function showProjectModal(existing) {
        const isEdit = !!existing;
        const title = isEdit ? t('Edytuj projekt') : t('Nowy projekt');
        const name = existing?.name || '';
        const desc = existing?.description || '';
        const color = existing?.color || '#8b5cf6';
        const existingMembers = existing?.members || [];
        const columns = (existing?.columns || DEFAULT_COLUMNS).join(', ');
        const copilotEnabled = existing?.copilot_enabled || false;
        const localaiEnabled = existing?.localai_enabled || false;
        const freemodelEnabled = existing?.freemodel_enabled || false;
        const ollamaEnabled = existing?.ollama_enabled || false;
        const ollamaModel = existing?.ollama_model || 'mistral';
        const qaModel = existing?.qa_model || '';
        const auditApps = existing?.audit_apps || '';

        let activeModelName = '';
        try {
            const am = await api('/aichat/models/active');
            const m = am?.model;
            if (m && m.name) activeModelName = m.name;
            else if (m && m.id) activeModelName = m.id;
        } catch(e) {}
        const localaiLabel = activeModelName
            ? t('Lokalny agent') + ` (${activeModelName})`
            : t('Lokalny agent (brak aktywnego modelu)');

        let allSystemUsers = [];
        try {
            const ulist = await api('/users/list');
            allSystemUsers = (ulist || []).filter(u => u.nasos_user).map(u => u.username);
        } catch(e) {}

        const html = `
            <div class="tk-form">
                <div class="tk-form-group">
                    <label>${t('Nazwa')}</label>
                    <input type="text" id="tk-pf-name" class="tk-input" value="${_escHtml(name)}" placeholder="${t('Nazwa projektu')}" autofocus />
                </div>
                <div class="tk-form-group">
                    <label>${t('Opis')}</label>
                    <textarea id="tk-pf-desc" class="tk-input" rows="3" placeholder="${t('Opis projektu (opcjonalnie)')}">${_escHtml(desc)}</textarea>
                </div>
                <div class="tk-form-row">
                    <div class="tk-form-group">
                        <label>${t('Kolor')}</label>
                        <div class="tk-color-picker">
                            <input type="color" id="tk-pf-color" value="${_escHtml(color)}" />
                            <span class="tk-color-hex" id="tk-pf-color-val">${_escHtml(color)}</span>
                        </div>
                    </div>
                    <div class="tk-form-group">
                        <label>${t('Członkowie')}</label>
                        <div class="tk-chip-picker" id="tk-pf-members-picker">
                            <div class="tk-chips" id="tk-pf-chips"></div>
                            <select class="tk-input tk-chip-select" id="tk-pf-member-add">
                                <option value="">${t('Dodaj członka...')}</option>
                            </select>
                        </div>
                    </div>
                </div>
                <div class="tk-form-group">
                    <label>${t('Kolumny')} <small>(${t('oddzielone przecinkiem')})</small></label>
                    <input type="text" id="tk-pf-columns" class="tk-input" value="${_escHtml(columns)}" placeholder="Backlog, Do zrobienia, W trakcie, Review, Gotowe" />
                </div>
                <div class="tk-form-group">
                    <label class="tk-toggle-row">
                        <input type="checkbox" id="tk-pf-copilot" ${copilotEnabled ? 'checked' : ''} />
                        <span class="tk-toggle-slider"></span>
                        <span class="tk-toggle-label"><i class="fas fa-robot"></i> ${t('Copilot Agent')}</span>
                    </label>
                    <small class="tk-toggle-hint">${t('Copilot automatycznie realizuje tickety z kolumny "Do zrobienia"')}</small>
                </div>
                <div class="tk-form-group">
                    <label class="tk-toggle-row">
                        <input type="checkbox" id="tk-pf-localai" ${localaiEnabled ? 'checked' : ''} />
                        <span class="tk-toggle-slider"></span>
                        <span class="tk-toggle-label"><i class="fas fa-brain"></i> ${localaiLabel}</span>
                    </label>
                    <small class="tk-toggle-hint">${activeModelName ? t('Użyj lokalnego modelu do analizy i propozycji zmian. Nie wymaga kluczy API.') : t('Brak aktywnego modelu. Pobierz i aktywuj model w Bibliotece modeli (AIChat).')}</small>
                </div>
                <div class="tk-form-group">
                    <label class="tk-toggle-row">
                        <input type="checkbox" id="tk-pf-ollama" ${ollamaEnabled ? 'checked' : ''} />
                        <span class="tk-toggle-slider"></span>
                        <span class="tk-toggle-label"><i class="fas fa-wind"></i> ${t('Ollama (Zdalny serwer)')}</span>
                    </label>
                    <small class="tk-toggle-hint">${t('Używa zdalnego serwera Ollama. Skonfiguruj URL i klucz API w ustawieniach AIChat.')}</small>
                </div>
                <div class="tk-form-group" id="tk-ollama-model-group" style="display:${ollamaEnabled ? 'block' : 'none'};margin-left:20px;">
                    <label>${t('Model Ollama (Fix):')}</label>
                    <select id="tk-pf-ollama-model" class="tk-input" style="width:100%">
                        <option value="${_escHtml(ollamaModel)}">${_escHtml(ollamaModel) || t('Ładowanie...')}</option>
                    </select>
                    <small class="tk-toggle-hint" id="tk-ollama-model-hint">${t('Ładowanie listy modeli...')}</small>
                </div>
                <div class="tk-form-group" id="tk-qa-model-group" style="display:${ollamaEnabled ? 'block' : 'none'};margin-left:20px;">
                    <label>${t('Model Ollama (QA + dedup — szybki):')}</label>
                    <select id="tk-pf-qa-model" class="tk-input" style="width:100%">
                        <option value="">${t('(tak sam jak Fix)')}</option>
                        <option value="${_escHtml(qaModel)}" ${qaModel ? 'selected' : ''}>${_escHtml(qaModel) || ''}</option>
                    </select>
                    <small class="tk-toggle-hint" id="tk-qa-model-hint">${t('Mały/szybki model do weryfikacji QA i deduplikacji. Zostaw puste żeby używać tego samego co Fix.')}</small>
                </div>
                <div class="tk-form-group">
                    <label class="tk-toggle-row">
                        <input type="checkbox" id="tk-pf-freemodel" ${freemodelEnabled ? 'checked' : ''} />
                        <span class="tk-toggle-slider"></span>
                        <span class="tk-toggle-label"><i class="fas fa-gift"></i> ${t('Darmowe modele Copilota')}</span>
                    </label>
                    <small class="tk-toggle-hint">${t('Używa darmowych modeli Copilot CLI (GPT-4.1, GPT-5 Mini). Pełny flow jak Copilot, ale bez zużycia limitu premium.')}</small>
                </div>
                <div class="tk-form-group" id="tk-audit-apps-group" style="display:${ollamaEnabled ? 'block' : 'none'};margin-left:20px;">
                    <label><i class="fas fa-magnifying-glass-chart"></i> ${t('Auto-audyt aplikacji (co 2 dni):')}</label>
                    <input type="text" id="tk-pf-audit-apps" class="tk-input" style="width:100%"
                        value="${_escHtml(auditApps)}"
                        placeholder="np. tickets,aichat,docker-manager   lub   * (wszystkie)">
                    <small class="tk-toggle-hint">${t('IDs aplikacji oddzielone przecinkiem. Watcher będzie je automatycznie audytował Ollamą co 2 dni i tworzył tickety z bugami. Zostaw puste żeby wyłączyć.')}</small>
                </div>
            </div>
        `;

        const overlay = tkShowModal(title, html, isEdit ? t('Zapisz') : t('Utwórz'), (modal) => {
            const n = modal.querySelector('#tk-pf-name').value.trim();
            if (!n) { toast(t('Nazwa jest wymagana'), 'warning'); return false; }

            const payload = {
                name: n,
                description: modal.querySelector('#tk-pf-desc').value.trim(),
                color: modal.querySelector('#tk-pf-color').value,
                members: Array.from(modal.querySelectorAll('#tk-pf-chips .tk-chip')).map(c => c.dataset.user),
                columns: modal.querySelector('#tk-pf-columns').value
                    .split(',').map(s => s.trim()).filter(Boolean),
                copilot_enabled: modal.querySelector('#tk-pf-copilot').checked,
                localai_enabled: modal.querySelector('#tk-pf-localai').checked,
                ollama_enabled: modal.querySelector('#tk-pf-ollama').checked,
                ollama_model: modal.querySelector('#tk-pf-ollama-model')?.value.trim() || 'mistral',
                qa_model: modal.querySelector('#tk-pf-qa-model')?.value.trim() || '',
                audit_apps: modal.querySelector('#tk-pf-audit-apps')?.value.trim() || '',
                freemodel_enabled: modal.querySelector('#tk-pf-freemodel').checked,
            };
            if (payload.columns.length === 0) payload.columns = [...DEFAULT_COLUMNS];

            if (isEdit) {
                updateProject(existing.id, payload);
            } else {
                createProject(payload);
            }
        });

        const colorInput = overlay.querySelector('#tk-pf-color');
        const colorVal = overlay.querySelector('#tk-pf-color-val');
        colorInput.oninput = () => { colorVal.textContent = colorInput.value; };

        const copilotToggle = overlay.querySelector('#tk-pf-copilot');
        const localToggle = overlay.querySelector('#tk-pf-localai');
        const ollamaToggle = overlay.querySelector('#tk-pf-ollama');
        const freeToggle = overlay.querySelector('#tk-pf-freemodel');
        const ollamaModelGroup = overlay.querySelector('#tk-ollama-model-group');
        const auditAppsGroup = overlay.querySelector('#tk-audit-apps-group');
        
        if (copilotToggle && localToggle && ollamaToggle && freeToggle) {
            const updateMutualExclusivity = () => {
                if (copilotToggle.checked) { localToggle.checked = false; ollamaToggle.checked = false; freeToggle.checked = false; }
                if (localToggle.checked) { copilotToggle.checked = false; ollamaToggle.checked = false; freeToggle.checked = false; }
                if (ollamaToggle.checked) { copilotToggle.checked = false; localToggle.checked = false; freeToggle.checked = false; }
                if (freeToggle.checked) { copilotToggle.checked = false; localToggle.checked = false; ollamaToggle.checked = false; }
                const show = ollamaToggle.checked ? 'block' : 'none';
                if (ollamaModelGroup) ollamaModelGroup.style.display = show;
                if (auditAppsGroup) auditAppsGroup.style.display = show;
                const qaModelGroup2 = overlay.querySelector('#tk-qa-model-group');
                if (qaModelGroup2) qaModelGroup2.style.display = show;
            };
            
            copilotToggle.onchange = updateMutualExclusivity;
            localToggle.onchange = () => {
                updateMutualExclusivity();
                if (localToggle.checked && !activeModelName) {
                    toast(t('Brak aktywnego modelu. Pobierz i aktywuj model w Bibliotece modeli (AIChat).'), 'warning');
                }
            };
            const loadOllamaModels = async (currentVal) => {
                const sel = overlay.querySelector('#tk-pf-ollama-model');
                const qasel = overlay.querySelector('#tk-pf-qa-model');
                const hint = overlay.querySelector('#tk-ollama-model-hint');
                const qahint = overlay.querySelector('#tk-qa-model-hint');
                if (!sel) return;
                try {
                    if (hint) hint.textContent = t('Ładowanie listy modeli...');
                    const data = await api('/tickets/ollama-models');
                    const models = data.models || [];
                    if (models.length === 0) {
                        sel.innerHTML = `<option value="">${t('Brak modeli — sprawdź czy Ollama działa')}</option>`;
                        if (hint) hint.textContent = t('Nie znaleziono modeli na serwerze Ollama.');
                    } else {
                        sel.innerHTML = models.map(m =>
                            `<option value="${m}" ${m === (currentVal || '') ? 'selected' : ''}>${m}</option>`
                        ).join('');
                        if (hint) hint.textContent = t('Wybierz model z listy');
                        if (qasel) {
                            qasel.innerHTML = `<option value="">${t('(tak sam jak Fix)')}</option>` +
                                models.map(m =>
                                    `<option value="${m}" ${m === (qaModel || '') ? 'selected' : ''}>${m}</option>`
                                ).join('');
                            if (qahint) qahint.textContent = t('Wybierz szybki model do QA i deduplikacji');
                        }
                    }
                } catch(e) {
                    sel.innerHTML = `<option value="${currentVal || ''}">${currentVal || t('Wpisz ręcznie...')}</option>`;
                    if (hint) hint.textContent = t('Nie można pobrać listy — Ollama niedostępna');
                }
            };
            const qaModelGroup = overlay.querySelector('#tk-qa-model-group');
            ollamaToggle.onchange = () => {
                updateMutualExclusivity();
                if (ollamaModelGroup) ollamaModelGroup.style.display = ollamaToggle.checked ? 'block' : 'none';
                if (qaModelGroup) qaModelGroup.style.display = ollamaToggle.checked ? 'block' : 'none';
                if (ollamaToggle.checked) loadOllamaModels(ollamaModel);
            };
            if (ollamaEnabled) loadOllamaModels(ollamaModel);
            freeToggle.onchange = updateMutualExclusivity;
        }

        /* ── Chip picker logic ── */
        const chipsEl = overlay.querySelector('#tk-pf-chips');
        const addSel  = overlay.querySelector('#tk-pf-member-add');
        let selectedMembers = [...existingMembers];

        function renderChips() {
            chipsEl.innerHTML = selectedMembers.map(u =>
                `<span class="tk-chip" data-user="${_escHtml(u)}">${_escHtml(u)} <i class="fas fa-times tk-chip-remove" data-user="${_escHtml(u)}"></i></span>`
            ).join('');
            addSel.innerHTML = '<option value="">' + t('Dodaj członka...') + '</option>' +
                allSystemUsers.filter(u => !selectedMembers.includes(u))
                    .map(u => '<option value="' + _escHtml(u) + '">' + _escHtml(u) + '</option>').join('');
        }
        renderChips();

        addSel.onchange = () => {
            const v = addSel.value;
            if (v && !selectedMembers.includes(v)) {
                selectedMembers.push(v);
                renderChips();
            }
            addSel.value = '';
        };
        chipsEl.addEventListener('click', (e) => {
            const rm = e.target.closest('.tk-chip-remove');
            if (rm) {
                selectedMembers = selectedMembers.filter(u => u !== rm.dataset.user);
                renderChips();
            }
        });
    }

    /* ── Touch Drag Helper ── */
    function setupTouchDrag(el, ticketId, fromColumnName) {
        let timer = null;
        let isDragging = false;
        let clone = null;
        let startX, startY;
        // Delay fetching boardEl until touchstart to ensure it exists
        
        el.addEventListener('touchstart', (e) => {
            if (e.touches.length > 1) return;
            startX = e.touches[0].clientX;
            startY = e.touches[0].clientY;
            
            // Long press to start drag
            timer = setTimeout(() => {
                isDragging = true;
                if (navigator.vibrate) navigator.vibrate(50);
                
                // Create clone
                clone = el.cloneNode(true);
                clone.classList.add('tk-card-dragging-mobile');
                // Remove ID to avoid dupes
                clone.removeAttribute('id');
                // Absolute positioning
                clone.style.left = (e.touches[0].clientX - el.offsetWidth / 2) + 'px';
                clone.style.top = (e.touches[0].clientY - el.offsetHeight / 2) + 'px';
                clone.style.width = (el.offsetWidth) + 'px';
                document.body.appendChild(clone);
                
                // Dim original
                el.style.opacity = '0.5';
            }, 500); // 500ms long press
        }, { passive: true });

        el.addEventListener('touchmove', (e) => {
            if (!isDragging) {
                const dx = Math.abs(e.touches[0].clientX - startX);
                const dy = Math.abs(e.touches[0].clientY - startY);
                if (dx > 10 || dy > 10) clearTimeout(timer);
                return;
            }
            
            e.preventDefault(); // Prevent scrolling
            if (clone) {
                clone.style.left = (e.touches[0].clientX - clone.offsetWidth / 2) + 'px';
                clone.style.top = (e.touches[0].clientY - clone.offsetHeight / 2) + 'px';
                
                // Highlight drop target
                const target = document.elementFromPoint(e.touches[0].clientX, e.touches[0].clientY);
                const col = target?.closest('.tk-column');
                const boardEl = document.getElementById('tk-board');
                if (boardEl) boardEl.querySelectorAll('.tk-column-dragover').forEach(c => c.classList.remove('tk-column-dragover'));
                if (col) col.classList.add('tk-column-dragover');
            }
        }, { passive: false });

        const endDrag = (e) => {
            clearTimeout(timer);
            if (!isDragging) return;
            
            isDragging = false;
            if (clone) clone.remove();
            clone = null;
            el.style.opacity = '';
            
            const boardEl = document.getElementById('tk-board');
            if (boardEl) boardEl.querySelectorAll('.tk-column-dragover').forEach(c => c.classList.remove('tk-column-dragover'));

            const changedTouch = e.changedTouches ? e.changedTouches[0] : e.touches[0];
            if (!changedTouch) return;

            const target = document.elementFromPoint(changedTouch.clientX, changedTouch.clientY);
            const col = target?.closest('.tk-column');
            
            if (col) {
                const newColName = col.dataset.column;
                // Move if column changed OR reorder within same column
                // But for now let's just support moving columns or reordering if we implemented logic
                // The reordering logic relies on finding index.
                
                const draggables = [...col.querySelectorAll('.tk-card, .tk-epic-group')];
                let order = draggables.length;
                for (let i = 0; i < draggables.length; i++) {
                     // If we are dragging within same column, ignore self in calculation?
                     // API handles reorder cleanly if we just give index.
                     const rect = draggables[i].getBoundingClientRect();
                     const midY = rect.top + rect.height / 2;
                     if (changedTouch.clientY < midY) {
                         // If dragging same item, and we are above it, order is i. 
                         // If dragging same item and we are below it, it doesn't matter much.
                         // Simple approximation:
                         if (draggables[i].dataset.id === ticketId) continue;
                         order = i; break;
                     }
                }
                
                // Optimization: Don't call API if dropping on self in same column
                if (newColName === fromColumnName) {
                     // Check if order changed? It's complex to calc exactly.
                     // Just call move, backend handles it or it's a no-op visually.
                }
                moveTicket(ticketId, newColName, order);
            }
        };

        el.addEventListener('touchend', endDrag);
        el.addEventListener('touchcancel', endDrag);
    }

    /* ═══════════════════ KANBAN BOARD VIEW ═══════════════════ */

    function getFilteredTickets() {
        return tickets.filter(tk => {
            if (filterAssignee && tk.assignee !== filterAssignee) return false;
            if (filterPriority && tk.priority !== filterPriority) return false;
            if (filterSearch && !tk.title.toLowerCase().includes(filterSearch.toLowerCase())) return false;
            return true;
        });
    }

    /* ── Ticket Watcher Status ── */
    let _watcherPollTimer = null;

    async function _pollWatcherStatus(container) {
        clearTimeout(_watcherPollTimer);
        try {
            const s = await api('/tickets/watcher/status');
            const dot = container.querySelector('#tk-watcher-dot');
            const btn = container.querySelector('#tk-watcher-status-btn');
            if (!dot) return;

            const st = s.status || {};
            const lk = s.lock;

            let label = '';
            let color = '#6b7280'; // grey = unknown

            if (!s.active) {
                color = '#ef4444';
                label = 'Watcher zatrzymany';
            } else if (lk) {
                const phase = lk.phase || 'fix';
                const phaseIcon = phase === 'qa' ? '🔍 QA' : '🔧 Fix';
                const elapsed = lk.elapsed ? ` (${Math.round(lk.elapsed/60)}min)` : '';
                color = '#f59e0b';
                label = `${phaseIcon}: ${lk.ticket_id || '?'}${elapsed}`;
            } else if (st.phase === 'audit') {
                color = '#a855f7';
                label = `🔍 Audyt: ${st.app_id || '?'}`;
            } else {
                color = '#22c55e';
                label = 'Watcher idle';
            }

            dot.style.background = color;
            dot.title = label;
            if (btn) btn.title = label;

            // update watcher bar if visible
            const bar = container.querySelector('#tk-watcher-bar');
            if (bar) {
                bar.textContent = label;
                bar.style.color = color;
            }

            // refresh card indicators if executing ticket changed
            const newTid = lk ? lk.ticket_id : null;
            const oldTid = watcherExecuting ? watcherExecuting.ticket_id : null;
            if (newTid !== oldTid) {
                watcherExecuting = lk ? { ...lk, executing: true } : null;
                renderBoard();
            }
        } catch (e) { /* ignore */ }
        _watcherPollTimer = setTimeout(() => _pollWatcherStatus(container), 8000);
    }

    async function _showWatcherStatusModal() {
        let s;
        try { s = await api('/tickets/watcher/status'); } catch(e) { s = {active:false,error:e.message}; }
        const existing = document.querySelector('.tk-watcher-stat-overlay');
        if (existing) existing.remove();
        const overlay = document.createElement('div');
        overlay.className = 'tk-watcher-stat-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9999;display:flex;align-items:center;justify-content:center;';

        const dot = s.active ? (s.lock ? '🟠' : '🟢') : '🔴';
        const statusText = s.active ? (s.lock ? 'Aktywny (przetwarza ticket)' : 'Aktywny (czeka)') : 'Zatrzymany';

        const schedRows = Object.entries(s.schedule||{}).map(([k, v]) => {
            const [projId, appId] = k.split(':');
            const last = v.last_audit ? new Date(v.last_audit*1000).toLocaleString() : 'nigdy';
            const nextTs = v.last_audit ? v.last_audit + 2*24*3600 : 0;
            const next = nextTs > Date.now()/1000 ? new Date(nextTs*1000).toLocaleString() : 'przy następnym cyklu';
            return `<tr style="border-bottom:1px solid #1f2937;">
              <td style="padding:6px 8px;color:#d1d5db;">${appId}</td>
              <td style="padding:6px 8px;color:#9ca3af;font-size:11px;">${last}</td>
              <td style="padding:6px 8px;color:#9ca3af;font-size:11px;">${next}</td>
              <td style="padding:6px 8px;color:#86efac;">${v.tickets_created||0}</td>
            </tr>`;
        }).join('');

        overlay.innerHTML = `
<div style="background:#1e1e2e;border:1px solid #374151;border-radius:12px;padding:24px;width:780px;max-width:96vw;max-height:88vh;display:flex;flex-direction:column;">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-shrink:0;">
    <h3 style="color:#e5e7eb;margin:0;">🤖 Ticket Watcher</h3>
    <button id="tk-ws-close" style="background:none;border:none;color:#9ca3af;font-size:20px;cursor:pointer;">✕</button>
  </div>
  <div style="display:flex;gap:4px;margin-bottom:14px;flex-shrink:0;">
    <button class="tk-ws-tab active" data-tab="status" style="padding:6px 16px;border:1px solid #374151;border-radius:6px 6px 0 0;background:#111827;color:#e5e7eb;cursor:pointer;font-size:13px;">📊 Status</button>
    <button class="tk-ws-tab" data-tab="log" style="padding:6px 16px;border:1px solid #374151;border-radius:6px 6px 0 0;background:#1e1e2e;color:#9ca3af;cursor:pointer;font-size:13px;">📋 Live Log</button>
  </div>
  <div id="tk-ws-status-panel" style="flex:1;overflow-y:auto;">
    <div style="background:#111827;border-radius:8px;padding:14px;margin-bottom:16px;">
      <div style="font-size:18px;margin-bottom:6px;">${dot} <span style="color:#e5e7eb;">${statusText}</span></div>
      ${s.lock ? `<div style="color:#fbbf24;font-size:12px;font-family:monospace;">Ticket: ${s.lock.ticket_id} | Model: ${s.lock.model||'?'} | Start: ${new Date((s.lock.started||0)*1000).toLocaleTimeString()}</div>` : ''}
      <div style="color:#6b7280;font-size:11px;margin-top:6px;">Watcher skanuje kolejkę co 2 minuty. Audyty aplikacji co 2 dni.</div>
    </div>
    ${schedRows ? `
    <div style="color:#9ca3af;font-size:12px;margin-bottom:8px;">📅 Historia audytów:</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead><tr style="color:#6b7280;border-bottom:2px solid #374151;">
        <th style="padding:6px 8px;text-align:left;">Aplikacja</th>
        <th style="padding:6px 8px;text-align:left;">Ostatni audyt</th>
        <th style="padding:6px 8px;text-align:left;">Następny</th>
        <th style="padding:6px 8px;text-align:left;">Tickety</th>
      </tr></thead>
      <tbody>${schedRows}</tbody>
    </table>` : '<div style="color:#6b7280;font-size:13px;">Brak historii audytów.</div>'}
    <div style="margin-top:16px;padding:12px;background:#0f1629;border-radius:6px;color:#6b7280;font-size:12px;">
      💡 Aby włączyć automatyczny audyt: otwórz ustawienia projektu → włącz Ollama → wpisz app-id w polu "Aplikacje do audytu" (np. <code style="color:#818cf8;">flasher, aichat, storage</code>)
    </div>
  </div>
  <div id="tk-ws-log-panel" style="flex:1;display:none;flex-direction:column;min-height:0;">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-shrink:0;">
      <span style="color:#6b7280;font-size:12px;" id="tk-ws-log-info">Ładowanie…</span>
      <label style="margin-left:auto;color:#9ca3af;font-size:12px;cursor:pointer;">
        <input type="checkbox" id="tk-ws-log-autoscroll" checked style="margin-right:4px;">auto-scroll
      </label>
      <button id="tk-ws-log-clear" style="background:#1f2937;border:1px solid #374151;color:#9ca3af;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;">🗑 wyczyść widok</button>
    </div>
    <div id="tk-ws-log-box" style="flex:1;background:#0d1117;border-radius:6px;padding:10px;overflow-y:auto;font-family:monospace;font-size:11.5px;color:#d1d5db;line-height:1.6;min-height:320px;max-height:55vh;"></div>
  </div>
</div>`;

        // ── tab switching ──
        const statusPanel = overlay.querySelector('#tk-ws-status-panel');
        const logPanel    = overlay.querySelector('#tk-ws-log-panel');
        overlay.querySelectorAll('.tk-ws-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                overlay.querySelectorAll('.tk-ws-tab').forEach(t => {
                    t.style.background = '#1e1e2e'; t.style.color = '#9ca3af';
                    t.classList.remove('active');
                });
                tab.style.background = '#111827'; tab.style.color = '#e5e7eb';
                tab.classList.add('active');
                if (tab.dataset.tab === 'log') {
                    statusPanel.style.display = 'none';
                    logPanel.style.display = 'flex';
                    _startLogPolling();
                } else {
                    statusPanel.style.display = '';
                    logPanel.style.display = 'none';
                    _stopLogPolling();
                }
            });
        });

        // ── live log polling ──
        const logBox  = overlay.querySelector('#tk-ws-log-box');
        const logInfo = overlay.querySelector('#tk-ws-log-info');
        let   logOffset = 0, logPollTimer = null, logInitialized = false;

        function _logLine(raw) {
            // colour-code log levels
            const div = document.createElement('div');
            div.style.whiteSpace = 'pre-wrap';
            div.style.wordBreak = 'break-all';
            if (/\bERROR\b/.test(raw))                    div.style.color = '#f87171';
            else if (/\bWARNING\b/.test(raw))             div.style.color = '#fbbf24';
            else if (/\[aider\]/.test(raw))               div.style.color = '#c084fc';
            else if (/\bAider\b/.test(raw))               div.style.color = '#818cf8';
            else if (/Cycle start/.test(raw))             div.style.color = '#34d399';
            else if (/Claimed|→/.test(raw))               div.style.color = '#60a5fa';
            else if (/Escalat|QA (PASS|FAIL)/.test(raw)) div.style.color = '#f472b6';
            else                                          div.style.color = '#d1d5db';
            div.textContent = raw;
            return div;
        }

        async function _pollLog() {
            try {
                const params = logInitialized ? `?offset=${logOffset}` : `?lines=300`;
                const data = await api('/tickets/watcher/log' + params);
                if (data.lines && data.lines.length) {
                    const autoScroll = overlay.querySelector('#tk-ws-log-autoscroll').checked;
                    const wasAtBottom = logBox.scrollHeight - logBox.scrollTop <= logBox.clientHeight + 10;
                    data.lines.forEach(l => logBox.appendChild(_logLine(l)));
                    if (autoScroll && wasAtBottom) logBox.scrollTop = logBox.scrollHeight;
                }
                logOffset = data.size || logOffset;
                logInitialized = true;
                logInfo.textContent = `${new Date().toLocaleTimeString()} — offset ${logOffset}`;
            } catch(e) { logInfo.textContent = 'Błąd: ' + e.message; }
            logPollTimer = setTimeout(_pollLog, 2000);
        }

        function _startLogPolling() { if (!logPollTimer) _pollLog(); }
        function _stopLogPolling()  { clearTimeout(logPollTimer); logPollTimer = null; }

        overlay.querySelector('#tk-ws-log-clear').addEventListener('click', () => {
            logBox.innerHTML = '';
        });
        overlay.querySelector('#tk-ws-close').addEventListener('click', () => {
            _stopLogPolling(); overlay.remove();
        });
        overlay.addEventListener('click', e => {
            if (e.target === overlay) { _stopLogPolling(); overlay.remove(); }
        });
        document.body.appendChild(overlay);
    }

    /* ── App Audit: pick app → run ── */
    const _AUDIT_APPS = [
        {id:'aichat',          name:'AI Chat',             icon:'fa-robot'},
        {id:'antivirus',       name:'Antivirus',           icon:'fa-virus-slash'},
        {id:'appstore',        name:'App Store',           icon:'fa-store'},
        {id:'backup',          name:'Backup',              icon:'fa-box-archive'},
        {id:'builder',         name:'Builder',             icon:'fa-hammer'},
        {id:'cloud-backup',    name:'Cloud Backup',        icon:'fa-cloud'},
        {id:'cron-manager',    name:'Cron Manager',        icon:'fa-clock'},
        {id:'dashboard',       name:'Dashboard',           icon:'fa-chart-line'},
        {id:'ddns',            name:'DDNS',                icon:'fa-network-wired'},
        {id:'diskrepair',      name:'Disk Repair',         icon:'fa-hdd'},
        {id:'dlna',            name:'DLNA',                icon:'fa-broadcast-tower'},
        {id:'docker-manager',  name:'Docker',              icon:'fa-docker'},
        {id:'domains-manager', name:'Domains',             icon:'fa-globe'},
        {id:'downloads',       name:'Downloads',           icon:'fa-download'},
        {id:'encryption',      name:'Encryption',          icon:'fa-lock'},
        {id:'eventlog',        name:'Event Log',           icon:'fa-list-alt'},
        {id:'fail2ban',        name:'Fail2Ban',            icon:'fa-ban'},
        {id:'familyhub',       name:'Family Hub',          icon:'fa-users'},
        {id:'firewall',        name:'Firewall',            icon:'fa-fire'},
        {id:'flasher',         name:'USB Flasher',         icon:'fa-usb'},
        {id:'gallery',         name:'Gallery',             icon:'fa-images'},
        {id:'hardware',        name:'Hardware',            icon:'fa-microchip'},
        {id:'mail-server',     name:'Mail Server',         icon:'fa-envelope'},
        {id:'monitor',         name:'Monitor',             icon:'fa-desktop'},
        {id:'network',         name:'Network',             icon:'fa-network-wired'},
        {id:'notifications',   name:'Notifications',       icon:'fa-bell'},
        {id:'packages',        name:'Packages',            icon:'fa-box'},
        {id:'power',           name:'Power',               icon:'fa-power-off'},
        {id:'radio-music',     name:'Radio & Music',       icon:'fa-music'},
        {id:'raid-manager',    name:'RAID Manager',        icon:'fa-database'},
        {id:'remote-log',      name:'Remote Log',          icon:'fa-terminal'},
        {id:'resources',       name:'Resources',           icon:'fa-server'},
        {id:'security-advisor',name:'Security Advisor',    icon:'fa-shield-alt'},
        {id:'settings',        name:'Settings',            icon:'fa-cog'},
        {id:'sharing',         name:'File Sharing',        icon:'fa-share-alt'},
        {id:'ssd-cache',       name:'SSD Cache',           icon:'fa-bolt'},
        {id:'ssh-manager',     name:'SSH Manager',         icon:'fa-key'},
        {id:'stickynotes',     name:'Sticky Notes',        icon:'fa-sticky-note'},
        {id:'storage',         name:'Storage',             icon:'fa-database'},
        {id:'sync-drive',      name:'Sync Drive',          icon:'fa-sync'},
        {id:'tickets',         name:'Tickets',             icon:'fa-ticket-alt'},
        {id:'totp',            name:'2FA / TOTP',          icon:'fa-mobile-alt'},
        {id:'updater',         name:'Updater',             icon:'fa-arrow-up'},
        {id:'ups',             name:'UPS',                 icon:'fa-battery-full'},
        {id:'users',           name:'Users',               icon:'fa-users'},
        {id:'video-station',   name:'Video Station',       icon:'fa-film'},
    ];

    function showAuditAppPicker(targetProject) {
        const existing = document.querySelector('.tk-audit-picker-overlay');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.className = 'tk-audit-picker-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9999;display:flex;align-items:center;justify-content:center;';

        const aiLabel = targetProject.ollama_enabled
            ? `<span style="color:#a855f7;font-size:12px;">🤖 ${targetProject.ollama_model || 'ollama'} aktywna</span>`
            : `<span style="color:#6b7280;font-size:12px;">⚠️ Ollama wyłączona — analiza statyczna</span>`;

        overlay.innerHTML = `
<div style="background:#1e1e2e;border:1px solid #374151;border-radius:12px;padding:24px;width:580px;max-width:95vw;max-height:80vh;overflow-y:auto;">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
    <h3 style="color:#e5e7eb;margin:0;">🔍 Wybierz aplikację do audytu</h3>
    <button id="tk-audit-close" style="background:none;border:none;color:#9ca3af;font-size:20px;cursor:pointer;">✕</button>
  </div>
  <div style="margin-bottom:16px;">${aiLabel}</div>
  <div id="tk-audit-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;"></div>
</div>`;

        const grid = overlay.querySelector('#tk-audit-grid');
        _AUDIT_APPS.forEach(app => {
            const btn = document.createElement('button');
            btn.style.cssText = 'background:#111827;border:1px solid #374151;border-radius:8px;padding:12px 8px;color:#e5e7eb;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:6px;font-size:13px;transition:border-color .15s,background .15s;';
            btn.innerHTML = `<i class="fas ${app.icon}" style="font-size:20px;color:#6366f1;"></i><span>${app.name}</span>`;
            btn.addEventListener('mouseenter', () => { btn.style.borderColor='#6366f1'; btn.style.background='#1a1a2e'; });
            btn.addEventListener('mouseleave', () => { btn.style.borderColor='#374151'; btn.style.background='#111827'; });
            btn.addEventListener('click', () => {
                overlay.remove();
                _executeAudit(targetProject, app.id, app.name);
            });
            grid.appendChild(btn);
        });

        overlay.querySelector('#tk-audit-close').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
        document.body.appendChild(overlay);
    }

    function _showAuditProgressModal(appName, hasAI) {
        const existing = document.querySelector('.tk-audit-progress-overlay');
        if (existing) existing.remove();
        const overlay = document.createElement('div');
        overlay.className = 'tk-audit-progress-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;display:flex;align-items:center;justify-content:center;';
        overlay.innerHTML = `
<div style="background:#1e1e2e;border:1px solid #374151;border-radius:12px;padding:28px;width:560px;max-width:95vw;">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px;">
    <span style="font-size:22px;">🔍</span>
    <div>
      <div style="color:#e5e7eb;font-weight:600;font-size:16px;">Audyt: ${appName}</div>
      <div style="color:#9ca3af;font-size:12px;">${hasAI ? '🤖 Ollama AI aktywna' : '⚙️ Analiza statyczna'}</div>
    </div>
    <button id="tk-ap-close" style="margin-left:auto;background:none;border:none;color:#6b7280;font-size:18px;cursor:pointer;" title="Zamknij (audyt trwa w tle)">✕</button>
  </div>
  <div style="background:#111827;border-radius:8px;height:10px;margin-bottom:14px;overflow:hidden;">
    <div id="tk-ap-bar" style="background:linear-gradient(90deg,#6366f1,#a855f7);height:100%;width:0%;transition:width .4s;border-radius:8px;"></div>
  </div>
  <div id="tk-ap-log" style="background:#0d0d1a;border-radius:6px;padding:12px;height:200px;overflow-y:auto;font-family:monospace;font-size:12px;color:#d1d5db;"></div>
  <div id="tk-ap-done" style="display:none;margin-top:14px;padding:12px;background:#052e16;border:1px solid #16a34a;border-radius:8px;color:#86efac;font-size:13px;"></div>
  <div id="tk-ap-warn" style="display:none;margin-top:14px;padding:12px;background:#1a1000;border:1px solid #d97706;border-radius:8px;color:#fbbf24;font-size:13px;"></div>
</div>`;
        overlay.querySelector('#tk-ap-close').addEventListener('click', () => overlay.remove());
        document.body.appendChild(overlay);
        return overlay;
    }

    async function _executeAudit(targetProject, appId, appName) {
        const hasAI = !!targetProject.ollama_enabled;
        const modal = _showAuditProgressModal(appName, hasAI);
        const bar = modal.querySelector('#tk-ap-bar');
        const logEl = modal.querySelector('#tk-ap-log');
        const doneEl = modal.querySelector('#tk-ap-done');
        const warnEl = modal.querySelector('#tk-ap-warn');
        let lastLogLen = 0;

        function appendLog(lines) {
            lines.forEach(l => {
                const div = document.createElement('div');
                div.style.cssText = 'padding:2px 0;border-bottom:1px solid #1f2937;';
                div.textContent = l;
                if (l.includes('❌') || l.includes('Error')) div.style.color = '#f87171';
                else if (l.includes('✅') || l.includes('✓')) div.style.color = '#86efac';
                else if (l.includes('🤖')) div.style.color = '#c084fc';
                logEl.appendChild(div);
            });
            logEl.scrollTop = logEl.scrollHeight;
        }

        try {
            // Start async job
            const startRes = await api(`/tickets/projects/${targetProject.id}/bug-hunt`, {
                method: 'POST',
                body: { app_id: appId }
            });
            if (!startRes.job_id) {
                warnEl.textContent = startRes.warning || startRes.error || 'Unexpected response';
                warnEl.style.display = 'block';
                return;
            }

            const jobId = startRes.job_id;
            appendLog([`🚀 Job started: ${jobId}`]);

            // Poll for progress
            let done = false;
            while (!done) {
                await new Promise(r => setTimeout(r, 2000));
                try {
                    const status = await api(`/tickets/projects/${targetProject.id}/bug-hunt/status/${jobId}`);
                    if (!status) break;

                    // Update progress bar
                    bar.style.width = (status.progress || 0) + '%';

                    // Append new log lines
                    const newLines = (status.log || []).slice(lastLogLen);
                    if (newLines.length) {
                        appendLog(newLines);
                        lastLogLen = status.log.length;
                    }

                    if (status.status === 'done') {
                        done = true;
                        bar.style.width = '100%';
                        const res = status.result || {};
                        if (res.warning) {
                            warnEl.textContent = '⚠️ ' + res.warning;
                            warnEl.style.display = 'block';
                        } else if (res.ok) {
                            const a = res.analysis || {};
                            doneEl.innerHTML = `✅ <strong>Audyt zakończony: ${res.app}</strong><br>
📊 Endpoints: ${a.endpoints||0} &nbsp;|&nbsp; Bezpieczeństwo: ${a.security_issues||0} &nbsp;|&nbsp; Wydajność: ${a.performance_issues||0}
${a.ai_issues ? `<br>🤖 AI insights: ${a.ai_issues}` : ''}
<br>🎟️ Utworzono ticketów: <strong>${res.count}</strong>`;
                            doneEl.style.display = 'block';
                            if (currentProject && currentProject.id === targetProject.id) {
                                await loadTickets(currentProject.id);
                                renderBoard();
                            }
                        }
                    } else if (status.status === 'error') {
                        done = true;
                        warnEl.textContent = '❌ ' + (status.error || 'Unknown error');
                        warnEl.style.display = 'block';
                        bar.style.background = '#ef4444';
                    }
                } catch (pollErr) {
                    console.warn('Poll error:', pollErr);
                }
            }
        } catch (e) {
            console.error(e);
            warnEl.textContent = '❌ ' + e.message;
            warnEl.style.display = 'block';
        }
    }

    async function runAppAudit() {
        if (!projects.length) await loadProjects();

        let targetProject = projects.find(p => p.name === 'ETHOS') || currentProject;
        if (!targetProject) {
            toast(t('Nie wybrano projektu'), 'error');
            return;
        }

        showAuditAppPicker(targetProject);
    }

    function updateSelectionToolbar() {
        const btn = document.getElementById('tk-delete-selected');
        const countSpan = document.getElementById('tk-sel-count');
        if (!btn || !countSpan) return;
        if (selectedTickets.size > 0) {
            btn.style.display = 'inline-flex';
            countSpan.innerText = `(${selectedTickets.size})`;
        } else {
            btn.style.display = 'none';
        }
    }

    function updateColumnSelectAll(col) {
        if (!col) return;
        const all = col.querySelector('.tk-col-select-all');
        if (!all) return;
        const checks = col.querySelectorAll('.tk-ticket-check');
        if (checks.length === 0) { all.checked = false; all.indeterminate = false; return; }
        const checkedCount = Array.from(checks).filter(c => c.checked).length;
        all.checked = checkedCount === checks.length;
        all.indeterminate = checkedCount > 0 && checkedCount < checks.length;
    }

    // A11y Helpers
    function announceToScreenReader(message) {
        let sr = document.getElementById('tk-sr-live');
        if (!sr) {
            sr = document.createElement('div');
            sr.id = 'tk-sr-live';
            sr.setAttribute('aria-live', 'polite');
            sr.style.position = 'absolute';
            sr.style.width = '1px';
            sr.style.height = '1px';
            sr.style.padding = '0';
            sr.style.margin = '-1px';
            sr.style.overflow = 'hidden';
            sr.style.clip = 'rect(0,0,0,0)';
            sr.style.whiteSpace = 'nowrap';
            sr.style.border = '0';
            document.body.appendChild(sr);
        }
        sr.textContent = message;
    }

    async function handleCardKeydown(e, card, ticketId, currentColumn) {
        const columns = currentProject.columns || DEFAULT_COLUMNS;
        const colIdx = columns.indexOf(currentColumn);
        
        // Navigation within column
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            const allCards = [...document.querySelectorAll(`.tk-card[data-column="${currentColumn}"], .tk-card-child[data-column="${currentColumn}"]`)];
            const idx = allCards.indexOf(card);
            if (e.key === 'ArrowUp') {
                if (idx > 0) allCards[idx - 1].focus();
                else {
                    const colHeader = document.querySelector(`.tk-column[data-column="${currentColumn}"]`);
                    if(colHeader) colHeader.focus();
                }
            }
            if (e.key === 'ArrowDown' && idx < allCards.length - 1) allCards[idx + 1].focus();
        }

        // Navigation between columns
        if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !e.ctrlKey) {
            e.preventDefault();
            let newColIdx = e.key === 'ArrowLeft' ? colIdx - 1 : colIdx + 1;
            if (newColIdx >= 0 && newColIdx < columns.length) {
                const newCol = columns[newColIdx];
                const cardsInNewCol = [...document.querySelectorAll(`.tk-card[data-column="${newCol}"], .tk-card-child[data-column="${newCol}"]`)];
                
                if (cardsInNewCol.length > 0) {
                    const currentCards = [...document.querySelectorAll(`.tk-card[data-column="${currentColumn}"], .tk-card-child[data-column="${currentColumn}"]`)];
                    const myIdx = currentCards.indexOf(card);
                    const targetIdx = Math.min(myIdx, cardsInNewCol.length - 1);
                    cardsInNewCol[targetIdx].focus();
                } else {
                    const colHeader = document.querySelector(`.tk-column[data-column="${newCol}"]`);
                    if(colHeader) colHeader.focus();
                }
            }
        }

        // Move card (Ctrl + Arrow)
        if (e.ctrlKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
            e.preventDefault();
            let newColIdx = e.key === 'ArrowLeft' ? colIdx - 1 : colIdx + 1;
            if (newColIdx >= 0 && newColIdx < columns.length) {
                const newCol = columns[newColIdx];
                const targetCards = [...document.querySelectorAll(`.tk-card[data-column="${newCol}"], .tk-card-child[data-column="${newCol}"]`)];
                const order = targetCards.length; // Move to end
                
                try {
                    await moveTicket(ticketId, newCol, order);
                    setTimeout(() => {
                        const newCard = document.querySelector(`.tk-card[data-id="${ticketId}"], .tk-card-child[data-id="${ticketId}"], .tk-epic-card[data-id="${ticketId}"]`);
                        if(newCard) {
                            newCard.focus();
                            announceToScreenReader(t('Przeniesiono do') + ' ' + newCol);
                        }
                    }, 100);
                } catch(err) { console.error(err); }
            }
        }

        // Open details
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            const tk = tickets.find(t => t.id === ticketId);
            if (tk) showTicketDetail(tk);
        }
    }

    function handleColumnKeydown(e, colName) {
        const columns = currentProject.columns || DEFAULT_COLUMNS;
        const colIdx = columns.indexOf(colName);

        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
            e.preventDefault();
            let newColIdx = e.key === 'ArrowLeft' ? colIdx - 1 : colIdx + 1;
            if (newColIdx >= 0 && newColIdx < columns.length) {
                const newCol = columns[newColIdx];
                const colHeader = document.querySelector(`.tk-column[data-column="${newCol}"]`);
                if(colHeader) colHeader.focus();
            }
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            const cards = [...document.querySelectorAll(`.tk-card[data-column="${colName}"], .tk-card-child[data-column="${colName}"]`)];
            if (cards.length > 0) cards[0].focus();
        }
    }

    function renderBoard() {
        const columns = currentProject.columns || DEFAULT_COLUMNS;
        const filtered = getFilteredTickets();
        const members = currentProject.members || [];
        const projColor = currentProject.color || '#8b5cf6';

        // 1. Ensure basic DOM structure exists (Incremental)
        let board = document.getElementById('tk-board');
        if (!board) {
            app.innerHTML = `
                <div class="tk-toolbar">
                    <button class="tk-btn tk-btn-back" id="tk-back">
                        <i class="fas fa-arrow-left"></i> ${t('Projekty')}
                    </button>
                    <span class="tk-board-title" style="border-left:3px solid ${_escHtml(projColor)};padding-left:10px;">
                        ${_escHtml(currentProject.name)}
                    </span>
                    <div style="flex:1;"></div>
                    <button class="tk-btn tk-btn-primary" id="tk-new-ticket">
                        <i class="fas fa-plus"></i> ${t('Ticket')}
                    </button>
                    <button class="tk-btn tk-btn-danger" id="tk-delete-selected" style="margin-left:8px; display:none;">
                        <i class="fas fa-trash"></i> <span id="tk-sel-count"></span>
                    </button>
                    <button class="tk-act-btn" id="tk-find-bugs-btn" title="${t('Audyt aplikacji (Epic)') + (currentProject.ollama_enabled ? ' + 🤖 AI' : '')}">
                        <i class="fas fa-bug"></i>
                    </button>
                    ${currentProject.ollama_enabled ? `<button class="tk-act-btn" id="tk-watcher-status-btn" title="Ticket Watcher status" style="position:relative;"><i class="fas fa-robot"></i><span id="tk-watcher-dot" style="position:absolute;top:4px;right:4px;width:7px;height:7px;border-radius:50%;background:#6b7280;"></span></button><span id="tk-watcher-bar" style="font-size:11px;opacity:0.7;margin-left:4px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle;"></span>` : ''}
                    ${(currentProject.copilot_enabled || currentProject.localai_enabled || currentProject.ollama_enabled || currentProject.freemodel_enabled) ? '<button class="tk-act-btn" id="tk-watcher-btn" title="AI Agent"><i class="fas fa-tower-broadcast"></i></button><button class="tk-act-btn" id="tk-ai-usage-btn" title="AI Usage"><i class="fas fa-chart-bar"></i></button>' : ''}
                    <button class="tk-act-btn" id="tk-mobile-filter-toggle" title="${t('Filtry')}">
                        <i class="fas fa-filter"></i>
                    </button>
                    <button class="tk-act-btn" id="tk-project-settings" title="${t('Ustawienia')}">
                        <i class="fas fa-sliders"></i>
                    </button>
                </div>
                <div class="tk-filter-bar">
                    <div class="tk-filter-group">
                        <i class="fas fa-user" style="font-size:11px;opacity:0.5;"></i>
                        <select id="tk-f-assignee" class="tk-filter-select">
                            <option value="">${t('Wszyscy')}</option>
                            ${members.map(m => `<option value="${_escHtml(m)}" ${filterAssignee === m ? 'selected' : ''}>${_escHtml(m)}</option>`).join('')}
                        </select>
                    </div>
                    <div class="tk-filter-group">
                        <i class="fas fa-flag" style="font-size:11px;opacity:0.5;"></i>
                        <select id="tk-f-priority" class="tk-filter-select">
                            <option value="">${t('Priorytet')}</option>
                            ${Object.entries(PRIORITY_LABELS).map(([k, v]) =>
                                `<option value="${k}" ${filterPriority === k ? 'selected' : ''}>${PRIORITY_ICONS[k]} ${_escHtml(v)}</option>`
                            ).join('')}
                        </select>
                    </div>
                    <div style="flex:1;"></div>
                    <div class="tk-search-wrap">
                        <i class="fas fa-search"></i>
                        <input type="text" class="tk-search" id="tk-f-search"
                               placeholder="${t('Szukaj...')}" value="${_escHtml(filterSearch)}" />
                    </div>
                </div>
                <div class="tk-board" id="tk-board"></div>
            `;
            board = document.getElementById('tk-board');

            // Bindings
            app.querySelector('#tk-back').onclick = () => showProjectList();
            app.querySelector('#tk-new-ticket').onclick = () => showCreateTicketModal();
            app.querySelector('#tk-delete-selected').onclick = () => deleteSelectedTickets();
            const findBugsBtn = app.querySelector('#tk-find-bugs-btn');
            if (findBugsBtn) findBugsBtn.onclick = () => runAppAudit();
            
            // Mobile Filter Toggle
            const filterToggle = app.querySelector('#tk-mobile-filter-toggle');
            if (filterToggle) {
                filterToggle.onclick = () => {
                   const bar = app.querySelector('.tk-filter-bar');
                   if (bar) bar.classList.toggle('visible');
                };
            }

            app.querySelector('#tk-project-settings').onclick = () => showProjectModal(currentProject);
            const watcherBtn = app.querySelector('#tk-watcher-btn');
            if (watcherBtn) watcherBtn.onclick = () => showWatcherModal();

            // Watcher status button — poll every 30s, show live status dot
            const wsBtn = app.querySelector('#tk-watcher-status-btn');
            if (wsBtn) {
                wsBtn.onclick = () => _showWatcherStatusModal();
                _pollWatcherStatus(app);
            }
            const aiUsageBtn = app.querySelector('#tk-ai-usage-btn');
            if (aiUsageBtn) aiUsageBtn.onclick = () => showAiUsageModal();

            app.querySelector('#tk-f-assignee').onchange = (e) => {
                filterAssignee = e.target.value;
                renderBoard();
            };
            app.querySelector('#tk-f-priority').onchange = (e) => {
                filterPriority = e.target.value;
                renderBoard();
            };
            let _searchDebounce = null;
            app.querySelector('#tk-f-search').oninput = (e) => {
                filterSearch = e.target.value;
                clearTimeout(_searchDebounce);
                _searchDebounce = setTimeout(() => renderBoard(), 250);
            };

            app.addEventListener('change', (e) => {
                if (e.target.classList.contains('tk-ticket-check')) {
                    const id = e.target.dataset.id;
                    if (e.target.checked) selectedTickets.add(id);
                    else selectedTickets.delete(id);
                    updateSelectionToolbar();
                    updateColumnSelectAll(e.target.closest('.tk-column'));
                }
                if (e.target.classList.contains('tk-col-select-all')) {
                    const colName = e.target.dataset.column;
                    const checked = e.target.checked;
                    const col = e.target.closest('.tk-column');
                    if (col) {
                        col.querySelectorAll('.tk-ticket-check').forEach(cb => {
                            cb.checked = checked;
                            if (checked) selectedTickets.add(cb.dataset.id);
                            else selectedTickets.delete(cb.dataset.id);
                        });
                    }
                    updateSelectionToolbar();
                }
            });
        } else {
             // Update Toolbar Title/Color if needed (cheap)
             const titleEl = app.querySelector('.tk-board-title');
             if (titleEl && titleEl.innerText !== currentProject.name) {
                 titleEl.innerText = currentProject.name;
                 titleEl.style.borderLeftColor = projColor;
             }
        }

        // 2. Prep Helpers
        const EPIC_COLORS = ['#6554c0','#0065ff','#00875a','#ff5630','#ff991f','#36b37e','#00b8d9','#6554c0'];
        const epicColorMap = {};
        let epicColorIdx = 0;
        Object.keys(ticketChildren).forEach(eid => {
            epicColorMap[eid] = EPIC_COLORS[epicColorIdx % EPIC_COLORS.length];
            epicColorIdx++;
        });
        if (!window._tkEpicCollapsed) window._tkEpicCollapsed = {};

        function createEpicGroup(epicTk, children, colName) {
            const color = epicColorMap[epicTk.id] || '#6554c0';
            const allChildren = ticketChildren[epicTk.id] || [];
            const doneCol = (currentProject.columns || DEFAULT_COLUMNS).slice(-1)[0] || 'Gotowe';
            const doneCount = allChildren.filter(c => c.column === doneCol).length;
            const totalCount = allChildren.length;
            const pct = totalCount ? Math.round((doneCount / totalCount) * 100) : 0;
            const isCollapsed = !!window._tkEpicCollapsed[epicTk.id];

            const group = document.createElement('div');
            group.className = 'tk-epic-group';
            group.style.setProperty('--epic-color', color);
            group.dataset.id = epicTk.id;

            const epicCard = document.createElement('div');
            epicCard.className = 'tk-card tk-epic-card';
            epicCard.draggable = true;
            epicCard.tabIndex = 0;
            epicCard.setAttribute('role', 'button');
            epicCard.addEventListener('keydown', (e) => handleCardKeydown(e, epicCard, epicTk.id, colName));
            epicCard.dataset.id = epicTk.id;
            epicCard.dataset.column = colName;

            const prioColor = PRIORITY_COLORS[epicTk.priority] || PRIORITY_COLORS.medium;
            const epicCompInfo = COMPLEXITY_LEVELS[epicTk.complexity] || COMPLEXITY_LEVELS.medium;
            const visibleLabels = (epicTk.labels || []).filter(l => !String(l).startsWith('epic:'));
            const labelsHtml = visibleLabels.map(l =>
                `<span class="tk-label" style="background:${tkLabelColor(l)};">${_escHtml(l)}</span>`
            ).join('');

            epicCard.innerHTML = `
                <div class="tk-card-select" onclick="event.stopPropagation()" style="position:absolute;top:6px;right:6px;z-index:10;">
                    <input type="checkbox" class="tk-ticket-check" data-id="${epicTk.id}" ${selectedTickets.has(epicTk.id) ? 'checked' : ''}>
                </div>
                <div class="tk-card-header">
                    ${_tkTypeIcon('epic')}
                    <span class="tk-priority-dot" style="background:${prioColor};" title="${_escHtml(PRIORITY_LABELS[epicTk.priority] || epicTk.priority)}"></span>
                    <span class="tk-card-title">${_escHtml(epicTk.title)}</span>
                    <span class="tk-ticket-id">${_shortId(epicTk.id)}</span>
                    <span class="tk-complexity-badge" style="background:${epicCompInfo.color};" title="${t('Złożoność')}: ${_escHtml(epicCompInfo.label)}">${_escHtml(epicCompInfo.label[0])}</span>
                </div>
                <div class="tk-epic-badge">
                    <i class="fas fa-layer-group"></i> EPIC · ${totalCount} subtask${totalCount !== 1 ? 's' : ''}
                    <button class="tk-epic-toggle" title="${isCollapsed ? t('Rozwiń') : t('Zwiń')}">
                        <i class="fas fa-chevron-${isCollapsed ? 'right' : 'down'}"></i>
                    </button>
                </div>
                <div class="tk-epic-progress">
                    <div class="tk-epic-progress-bar"><div class="tk-epic-progress-fill" style="width:${pct}%;"></div></div>
                    <span class="tk-epic-progress-text">${doneCount}/${totalCount}</span>
                </div>
                ${epicTk.assignee ? '<div class="tk-card-assignee"><i class="fas fa-user"></i> ' + _escHtml(epicTk.assignee) + '</div>' : ''}
                ${labelsHtml ? '<div class="tk-card-labels">' + labelsHtml + '</div>' : ''}
            `;

            if (watcherExecuting && watcherExecuting.ticket_id === epicTk.id) {
                epicCard.classList.add('tk-agent-active');
                const badge = document.createElement('div');
                badge.className = 'tk-agent-badge';
                badge.innerHTML = '<i class="fas fa-robot"></i> ' + _escHtml(watcherLabel(watcherExecuting));
                epicCard.appendChild(badge);
            }

            epicCard.querySelector('.tk-epic-toggle').addEventListener('click', (e) => {
                e.stopPropagation();
                window._tkEpicCollapsed[epicTk.id] = !window._tkEpicCollapsed[epicTk.id];
                renderBoard();
            });
            epicCard.addEventListener('dragstart', (e) => {
                e.dataTransfer.setData('text/plain', JSON.stringify({ id: epicTk.id, fromColumn: colName }));
                epicCard.classList.add('tk-card-dragging');
                setTimeout(() => epicCard.style.opacity = '0.5', 0);
            });
            epicCard.addEventListener('dragend', () => {
                epicCard.classList.remove('tk-card-dragging');
                epicCard.style.opacity = '';
                board.querySelectorAll('.tk-column-dragover').forEach(el => el.classList.remove('tk-column-dragover'));
            });
            setupTouchDrag(epicCard, epicTk.id, colName); // Add touch support
            epicCard.addEventListener('click', () => showTicketDetail(epicTk));

            group.appendChild(epicCard);

            if (children.length) {
                const childWrap = document.createElement('div');
                childWrap.className = 'tk-epic-children' + (isCollapsed ? ' collapsed' : '');
                childWrap.style.setProperty('--epic-color', color);

                children.forEach((ch, cidx) => {
                    const isDone = ch.column === doneCol;
                    const childCard = document.createElement('div');
                    childCard.className = 'tk-card-child';
                    childCard.draggable = true;
                    childCard.tabIndex = 0;
                    childCard.setAttribute('role', 'button');
                    childCard.addEventListener('keydown', (e) => handleCardKeydown(e, childCard, ch.id, colName));
                    childCard.dataset.id = ch.id;
                    childCard.dataset.column = colName;
                    
                    const cLabels = (ch.labels || []).filter(l => !String(l).startsWith('epic:'));
                    const cLabelsHtml = cLabels.map(l =>
                        `<span class="tk-label" style="background:${tkLabelColor(l)};">${_escHtml(l)}</span>`
                    ).join('');

                    childCard.innerHTML = `
                        <div class="tk-card-select" onclick="event.stopPropagation()" style="position:absolute;top:6px;right:6px;z-index:10;">
                            <input type="checkbox" class="tk-ticket-check" data-id="${ch.id}" ${selectedTickets.has(ch.id) ? 'checked' : ''}>
                        </div>
                        <div class="tk-card-header">
                            ${_tkTypeIcon(ch.type || 'subtask')}
                            <span class="tk-subtask-icon${isDone ? ' done' : ''}" style="--epic-color:${color};">
                                ${isDone ? '<i class="fas fa-check"></i>' : ''}
                            </span>
                            <span class="tk-card-title" ${isDone ? 'style="text-decoration:line-through;opacity:0.6;"' : ''}>${_escHtml(ch.title)}</span>
                        </div>
                        ${ch.assignee ? '<div class="tk-card-assignee"><i class="fas fa-user"></i> ' + _escHtml(ch.assignee) + '</div>' : ''}
                        ${cLabelsHtml ? '<div class="tk-card-labels">' + cLabelsHtml + '</div>' : ''}
                    `;

                    if (watcherExecuting && watcherExecuting.ticket_id === ch.id) {
                        childCard.classList.add('tk-agent-active');
                        const badge = document.createElement('div');
                        badge.className = 'tk-agent-badge';
                        badge.innerHTML = '<i class="fas fa-robot"></i> ' + _escHtml(watcherLabel(watcherExecuting));
                        childCard.appendChild(badge);
                    }

                    childCard.addEventListener('dragstart', (e) => {
                        e.dataTransfer.setData('text/plain', JSON.stringify({ id: ch.id, fromColumn: colName }));
                        childCard.classList.add('tk-card-dragging');
                        setTimeout(() => childCard.style.opacity = '0.5', 0);
                    });
                    childCard.addEventListener('dragend', () => {
                        childCard.classList.remove('tk-card-dragging');
                        childCard.style.opacity = '';
                        board.querySelectorAll('.tk-column-dragover').forEach(el => el.classList.remove('tk-column-dragover'));
                    });
                    setupTouchDrag(childCard, ch.id, colName); // Add touch support
                    childCard.addEventListener('click', () => showTicketDetail(ch));
                    childWrap.appendChild(childCard);
                });
                group.appendChild(childWrap);
            }
            return group;
        }

        function createStandaloneCard(tk, idx) {
            const card = document.createElement('div');
            card.className = 'tk-card';
            card.draggable = true;
            card.tabIndex = 0;
            card.setAttribute('role', 'button');
            card.addEventListener('keydown', (e) => handleCardKeydown(e, card, tk.id, tk.column));
            card.dataset.id = tk.id;
            card.dataset.column = tk.column;

            const visibleLabels = (tk.labels || []).filter(l => !String(l).startsWith('epic:'));
            const labelsHtml = visibleLabels.map(l =>
                `<span class="tk-label" style="background:${tkLabelColor(l)};">${_escHtml(l)}</span>`
            ).join('');
            const prioColor = PRIORITY_COLORS[tk.priority] || PRIORITY_COLORS.medium;
            const compInfo = COMPLEXITY_LEVELS[tk.complexity] || COMPLEXITY_LEVELS.medium;

            card.innerHTML = `
                <div class="tk-card-select" onclick="event.stopPropagation()" style="position:absolute;top:6px;right:6px;z-index:10;">
                    <input type="checkbox" class="tk-ticket-check" data-id="${tk.id}" ${selectedTickets.has(tk.id) ? 'checked' : ''}>
                </div>
                <div class="tk-card-header">
                    ${_tkTypeIcon(tk.type)}
                    <span class="tk-priority-dot" style="background:${prioColor};" title="${_escHtml(PRIORITY_LABELS[tk.priority] || tk.priority)}"></span>
                    <span class="tk-card-title">${_escHtml(tk.title)}</span>
                    <span class="tk-complexity-badge" style="background:${compInfo.color};" title="${t('Złożoność')}: ${_escHtml(compInfo.label)}">${_escHtml(compInfo.label[0])}</span>
                </div>
                <span class="tk-ticket-id">${_shortId(tk.id)}</span>
                ${tk.assignee ? '<div class="tk-card-assignee"><i class="fas fa-user"></i> ' + _escHtml(tk.assignee) + '</div>' : ''}
                ${labelsHtml ? '<div class="tk-card-labels">' + labelsHtml + '</div>' : ''}
            `;

            if (watcherExecuting && watcherExecuting.ticket_id === tk.id) {
                card.classList.add('tk-agent-active');
                const badge = document.createElement('div');
                badge.className = 'tk-agent-badge';
                badge.innerHTML = '<i class="fas fa-robot"></i> ' + _escHtml(watcherLabel(watcherExecuting));
                card.appendChild(badge);
            }

            card.addEventListener('dragstart', (e) => {
                e.dataTransfer.setData('text/plain', JSON.stringify({ id: tk.id, fromColumn: tk.column }));
                card.classList.add('tk-card-dragging');
                setTimeout(() => card.style.opacity = '0.5', 0);
            });
            card.addEventListener('dragend', () => {
                card.classList.remove('tk-card-dragging');
                card.style.opacity = '';
                board.querySelectorAll('.tk-column-dragover').forEach(el => el.classList.remove('tk-column-dragover'));
            });
            setupTouchDrag(card, tk.id, tk.column); // Add touch support
            card.addEventListener('click', () => showTicketDetail(tk));
            return card;
        }

        // 3. Render Columns (Incremental)
        columns.forEach((colName, colIdx) => {
            let col = board.querySelector(`.tk-column[data-column="${colName}"]`);
            if (!col) {
                col = document.createElement('div');
                col.className = 'tk-column';
                col.tabIndex = 0;
                col.setAttribute('role', 'region');
                col.setAttribute('aria-label', colName);
                col.addEventListener('keydown', (e) => handleColumnKeydown(e, colName));
                col.dataset.column = colName;
                col.innerHTML = `
                    <div class="tk-column-header">
                        <input type="checkbox" class="tk-col-select-all" data-column="${_escHtml(colName)}" title="${t('Zaznacz wszystkie')}">
                        <span class="tk-column-title">${_escHtml(colName)}</span>
                        <span class="tk-column-count">0</span>
                    </div>
                    <div class="tk-column-body" data-column="${_escHtml(colName)}"></div>
                `;
                
                // Drop events
                const colBody = col.querySelector('.tk-column-body');
                colBody.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    colBody.classList.add('tk-column-dragover');
                    colBody.querySelectorAll('.tk-drop-indicator').forEach(el => el.remove());
                    const draggables = [...colBody.querySelectorAll('.tk-card, .tk-epic-group')];
                    let insertBefore = null;
                    for (const card of draggables) {
                        const rect = card.getBoundingClientRect();
                        const midY = rect.top + rect.height / 2;
                        if (e.clientY < midY) { insertBefore = card; break; }
                    }
                    const indicator = document.createElement('div');
                    indicator.className = 'tk-drop-indicator';
                    if (insertBefore) colBody.insertBefore(indicator, insertBefore);
                    else colBody.appendChild(indicator);
                });
                colBody.addEventListener('dragleave', (e) => {
                    if (!colBody.contains(e.relatedTarget)) {
                        colBody.classList.remove('tk-column-dragover');
                        colBody.querySelectorAll('.tk-drop-indicator').forEach(el => el.remove());
                    }
                });
                colBody.addEventListener('drop', (e) => {
                    e.preventDefault();
                    colBody.classList.remove('tk-column-dragover');
                    colBody.querySelectorAll('.tk-drop-indicator').forEach(el => el.remove());
                    try {
                        const payload = JSON.parse(e.dataTransfer.getData('text/plain'));
                        if (payload.id) {
                            const draggables = [...colBody.querySelectorAll('.tk-card, .tk-epic-group')];
                            let order = draggables.length;
                            for (let i = 0; i < draggables.length; i++) {
                                const rect = draggables[i].getBoundingClientRect();
                                const midY = rect.top + rect.height / 2;
                                if (e.clientY < midY) {
                                    const cardId = draggables[i].dataset.id;
                                    if (cardId === payload.id) continue;
                                    order = i; break;
                                }
                            }
                            moveTicket(payload.id, colName, order);
                        }
                    } catch (_) { }
                });
            }

            // Ensure column order
            const existingCol = board.children[colIdx];
            if (existingCol !== col) {
                if(existingCol) board.insertBefore(col, existingCol);
                else board.appendChild(col);
            }

            // Update Column Content (Reconciliation)
            const colBody = col.querySelector('.tk-column-body');
            const colTickets = filtered.filter(tk => tk.column === colName);
            const items = [];
            const processed = new Set();
            
            // Collect Epics
            colTickets.forEach(tk => {
                if (processed.has(tk.id)) return;
                if (ticketIsEpic.has(tk.id)) {
                    items.push({ type: 'epic', ticket: tk });
                    processed.add(tk.id);
                    (ticketChildren[tk.id] || []).filter(c => c.column === colName).forEach(c => processed.add(c.id));
                }
            });
            // Collect Standalone
            colTickets.forEach(tk => {
                if (processed.has(tk.id)) return;
                items.push({ type: 'standalone', ticket: tk });
                processed.add(tk.id);
            });

            col.querySelector('.tk-column-count').innerText = items.length;
            col.setAttribute('aria-label', colName + ' (' + items.length + ')');

            // Existing Map
            const existingElMap = new Map();
            Array.from(colBody.children).forEach(el => {
                if (el.dataset.id) existingElMap.set(el.dataset.id, el);
            });

            // Empty state
            const emptyMsg = colBody.querySelector('.tk-empty-col');
            if (items.length === 0) {
                 if (!emptyMsg) colBody.innerHTML = `<div class="tk-empty-col">${t('Brak ticketów')}</div>`;
            } else {
                 if (emptyMsg) emptyMsg.remove();
            }

            let lastEl = null;
            items.forEach((item, idx) => {
                const tk = item.ticket;
                let el = existingElMap.get(tk.id);
                const isEpic = item.type === 'epic';
                
                // Calc Hash
                const base = [tk.id, tk.title, tk.priority, tk.complexity, tk.type, tk.assignee, (tk.labels||[]).join(','), tk.column].join('|');
                const watcher = (watcherExecuting && watcherExecuting.ticket_id === tk.id) ? watcherLabel(watcherExecuting) : '';
                let childHash = '';
                let collapsed = '';
                if (isEpic) {
                    const children = (ticketChildren[tk.id] || []).filter(c => c.column === colName);
                    childHash = children.map(c => c.id + c.title + c.column + c.status + c.priority).join('|');
                    collapsed = !!window._tkEpicCollapsed[tk.id];
                }
                const dataHash = `V1|${isEpic?'E':'S'}|${base}|${watcher}|${childHash}|${collapsed}`;

                if (!el) {
                    // Create
                    if (isEpic) {
                         const children = (ticketChildren[tk.id] || []).filter(c => c.column === colName);
                         el = createEpicGroup(tk, children, colName);
                    } else {
                         el = createStandaloneCard(tk, idx);
                    }
                    el.dataset.hash = dataHash;
                } else {
                    // Update?
                    if (el.dataset.hash !== dataHash) {
                        if (isEpic) {
                             const children = (ticketChildren[tk.id] || []).filter(c => c.column === colName);
                             const newEl = createEpicGroup(tk, children, colName);
                             el.replaceWith(newEl);
                             el = newEl;
                        } else {
                             const newEl = createStandaloneCard(tk, idx);
                             el.replaceWith(newEl);
                             el = newEl;
                        }
                        el.dataset.hash = dataHash;
                    }
                    existingElMap.delete(tk.id);
                }

                // Place
                if (idx === 0) {
                    if (colBody.firstElementChild !== el) colBody.prepend(el);
                } else {
                    if (lastEl.nextElementSibling !== el) lastEl.after(el);
                }
                lastEl = el;
            });
            
            // Remove extra
            existingElMap.forEach(el => el.remove());

            updateColumnSelectAll(col);
        });

        // Cleanup columns
        Array.from(board.children).forEach(el => {
            if (!columns.includes(el.dataset.column)) el.remove();
        });
        
    }

    /* ═══════════════════ CREATE TICKET MODAL ═══════════════════ */

    function showCreateTicketModal() {
        const columns = currentProject.columns || DEFAULT_COLUMNS;
        const members = currentProject.members || [];

        const html = `
            <div class="tk-form">
                <div class="tk-form-group">
                    <label>${t('Tytuł')} *</label>
                    <input type="text" id="tk-tf-title" class="tk-input" placeholder="${t('Tytuł ticketu')}" autofocus />
                </div>
                <div class="tk-form-group">
                    <label>${t('Opis')}</label>
                    <textarea id="tk-tf-desc" class="tk-input" rows="4" placeholder="${t('Opis ticketu (opcjonalnie)')}"></textarea>
                </div>
                <div class="tk-form-row">
                    <div class="tk-form-group">
                        <label>${t('Typ')}</label>
                        <select id="tk-tf-type" class="tk-input">
                            ${Object.entries(TICKET_TYPES).map(([k, v]) =>
                                `<option value="${k}" ${k === 'task' ? 'selected' : ''}>${v.label}</option>`
                            ).join('')}
                        </select>
                    </div>
                    <div class="tk-form-group">
                        <label>${t('Kolumna')}</label>
                        <select id="tk-tf-column" class="tk-input">
                            ${columns.map((c, i) => `<option value="${_escHtml(c)}" ${i === 0 ? 'selected' : ''}>${_escHtml(c)}</option>`).join('')}
                        </select>
                    </div>
                </div>
                <div class="tk-form-row">
                    <div class="tk-form-group">
                        <label>${t('Priorytet')}</label>
                        <select id="tk-tf-priority" class="tk-input">
                            ${Object.entries(PRIORITY_LABELS).map(([k, v]) =>
                                `<option value="${k}" ${k === 'medium' ? 'selected' : ''}>${PRIORITY_ICONS[k]} ${_escHtml(v)}</option>`
                            ).join('')}
                        </select>
                    </div>
                    <div class="tk-form-group">
                        <label>${t('Złożoność')}</label>
                        <select id="tk-tf-complexity" class="tk-input">
                            ${Object.entries(COMPLEXITY_LEVELS).map(([k, v]) =>
                                `<option value="${k}" ${k === 'medium' ? 'selected' : ''}>${_escHtml(v.label)}</option>`
                            ).join('')}
                        </select>
                    </div>
                </div>
                <div class="tk-form-row">
                    <div class="tk-form-group">
                        <label>${t('Przypisany')}</label>
                        <select id="tk-tf-assignee" class="tk-input">
                            <option value="">${t('Nieprzypisany')}</option>
                            ${members.map(m => `<option value="${_escHtml(m)}">${_escHtml(m)}</option>`).join('')}
                        </select>
                    </div>
                </div>
                <div class="tk-form-group">
                    <label>${t('Etykiety')} <small>(${t('przecinek')})</small></label>
                    <input type="text" id="tk-tf-labels" class="tk-input" placeholder="bug, backend, urgent" />
                </div>
            </div>
        `;

        tkShowModal(t('Nowy ticket'), html, t('Utwórz'), (modal) => {
            const title = modal.querySelector('#tk-tf-title').value.trim();
            if (!title) { toast(t('Tytuł jest wymagany'), 'warning'); return false; }

            createTicket({
                title,
                description: modal.querySelector('#tk-tf-desc').value.trim(),
                type: modal.querySelector('#tk-tf-type').value,
                column: modal.querySelector('#tk-tf-column').value,
                priority: modal.querySelector('#tk-tf-priority').value,
                complexity: modal.querySelector('#tk-tf-complexity').value,
                assignee: modal.querySelector('#tk-tf-assignee').value,
                labels: modal.querySelector('#tk-tf-labels').value
                    .split(',').map(s => s.trim()).filter(Boolean),
            });
        });
    }

    /* ═══════════════════ TICKET DETAIL MODAL ═══════════════════ */

    async function showTicketDetail(ticket) {
        /* Show a lightweight loading indicator while fetching */
        const loadingOverlay = document.createElement('div');
        loadingOverlay.className = 'modal-overlay';
        loadingOverlay.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;"><div class="tk-loading"><i class="fas fa-spinner fa-spin" style="font-size:1.5rem;"></i></div></div>';
        document.body.appendChild(loadingOverlay);

        const columns = currentProject.columns || DEFAULT_COLUMNS;
        const members = currentProject.members || [];
        const ticketComments = await loadComments(ticket.id);
        let localModel = null;
        try {
            const lm = await api('/aichat/models/active');
            localModel = (lm && lm.model) || null;
        } catch (e) { localModel = null; }

        /* ── fetch AI agent logs (copilot + localai + ollama) ── */
        let copilotLogs = [];
        try {
            const [logsResp, localResp, ollamaResp] = await Promise.allSettled([
                api('/tickets/tickets/' + ticket.id + '/copilot-logs'),
                api('/tickets/tickets/' + ticket.id + '/copilot-logs?agent=localai'),
                api('/tickets/tickets/' + ticket.id + '/copilot-logs?agent=ollama'),
            ]);
            const cLogs = (logsResp.status === 'fulfilled' && logsResp.value?.logs) || [];
            const lLogs = (localResp.status === 'fulfilled' && localResp.value?.logs) || [];
            const oLogs = (ollamaResp.status === 'fulfilled' && ollamaResp.value?.logs) || [];
            copilotLogs = [
                ...cLogs.map(l => ({ ...l, agent: l.agent || 'copilot' })),
                ...lLogs.map(l => ({ ...l, agent: l.agent || 'localai' })),
                ...oLogs.map(l => ({ ...l, agent: 'ollama' })),
            ];
        } catch (e) { /* ignore — no logs available */ }

        const labels = (ticket.labels || []);
        const labelsHTML = labels.map(l =>
            `<span class="tk-label tk-label-removable" style="background:${tkLabelColor(l)};" data-label="${_escHtml(l)}">
                ${_escHtml(l)} <i class="fas fa-times tk-remove-label"></i>
            </span>`
        ).join('');

        const commentsHTML = ticketComments.map(c => {
            const ts = c.created || c.created_at;
            const timeStr = ts ? new Date(ts * 1000).toLocaleString('pl', { day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit' }) : '';
            const canDelete = (c.author || c.user) === (NAS.user?.username) || NAS.user?.role === 'admin';
            return `
            <div class="tk-comment" data-comment-id="${_escHtml(c.id || '')}">
                <div class="tk-comment-header">
                    <strong>${_escHtml(c.author || c.user || 'unknown')}</strong>
                    <span style="display:flex;align-items:center;gap:6px;">
                        <span class="tk-comment-date">${timeStr}</span>
                        ${canDelete ? '<button class="tk-comment-delete" data-cid="' + _escHtml(c.id || '') + '" title="' + t('Usuń') + '"><i class="fas fa-trash"></i></button>' : ''}
                    </span>
                </div>
                <div class="tk-comment-body">${_escHtml(c.text || c.body || '')}</div>
            </div>`;
        }).join('');

        const html = `
            <div class="tk-form tk-detail-form">
                <div class="tk-form-group">
                    <label>${t('Tytuł')}</label>
                    <input type="text" id="tk-df-title" class="tk-input" value="${_escHtml(ticket.title)}" />
                </div>
                <div class="tk-form-group">
                    <label>${t('Opis')}</label>
                    <textarea id="tk-df-desc" class="tk-input" rows="4">${_escHtml(ticket.description || '')}</textarea>
                </div>
                <div class="tk-form-row">
                    <div class="tk-form-group">
                        <label>${t('Typ')}</label>
                        <select id="tk-df-type" class="tk-input">
                            ${Object.entries(TICKET_TYPES).map(([k, v]) =>
                                `<option value="${k}" ${k === (ticket.type || 'task') ? 'selected' : ''}>${v.label}</option>`
                            ).join('')}
                        </select>
                    </div>
                    <div class="tk-form-group">
                        <label>${t('Kolumna')}</label>
                        <select id="tk-df-column" class="tk-input">
                            ${columns.map(c => `<option value="${_escHtml(c)}" ${c === ticket.column ? 'selected' : ''}>${_escHtml(c)}</option>`).join('')}
                        </select>
                    </div>
                </div>
                <div class="tk-form-row">
                    <div class="tk-form-group">
                        <label>${t('Priorytet')}</label>
                        <select id="tk-df-priority" class="tk-input">
                            ${Object.entries(PRIORITY_LABELS).map(([k, v]) =>
                                `<option value="${k}" ${k === ticket.priority ? 'selected' : ''}>${PRIORITY_ICONS[k]} ${_escHtml(v)}</option>`
                            ).join('')}
                        </select>
                    </div>
                    <div class="tk-form-group">
                        <label>${t('Złożoność')}</label>
                        <select id="tk-df-complexity" class="tk-input">
                            ${Object.entries(COMPLEXITY_LEVELS).map(([k, v]) =>
                                `<option value="${k}" ${k === (ticket.complexity || 'medium') ? 'selected' : ''}>${_escHtml(v.label)}</option>`
                            ).join('')}
                        </select>
                    </div>
                </div>
                <div class="tk-form-group">
                    <label>${t('Przypisany')}</label>
                    <select id="tk-df-assignee" class="tk-input">
                        <option value="">${t('Nieprzypisany')}</option>
                        ${members.map(m => `<option value="${_escHtml(m)}" ${m === ticket.assignee ? 'selected' : ''}>${_escHtml(m)}</option>`).join('')}
                    </select>
                </div>

                <label>${t('Etykiety')}</label>
                <div id="tk-df-labels" class="tk-labels-wrap">${labelsHTML}</div>
                <div style="display:flex;gap:6px;margin-top:4px;">
                    <input type="text" id="tk-df-new-label" class="tk-input" style="flex:1;" placeholder="${t('Nowa etykieta')}" />
                    <button class="tk-btn tk-btn-small" id="tk-df-add-label"><i class="fas fa-plus"></i></button>
                </div>

                <div class="tk-detail-meta" style="margin-top:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.1);font-size:0.8rem;opacity:0.6;">
                    ${ticket.reporter ? '<div>' + t('Zgłaszający') + ': ' + _escHtml(ticket.reporter) + '</div>' : ''}
                    ${ticket.created ? '<div>' + t('Utworzony') + ': ' + new Date(ticket.created * 1000).toLocaleString('pl') + '</div>' : ''}
                    ${ticket.updated ? '<div>' + t('Zaktualizowany') + ': ' + new Date(ticket.updated * 1000).toLocaleString('pl') + '</div>' : ''}
                </div>

                <div class="tk-manual-tests-section" style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.1);">
                    <label><i class="fas fa-vial" style="margin-right:4px;"></i> ${t('Testy manualne')}</label>
                    <div id="tk-df-manual-tests" style="margin-top:8px;">
                        ${(ticket.manual_tests || []).map((mt, i) => `
                        <div class="tk-mt-step" data-step="${i}" style="display:flex;gap:6px;align-items:flex-start;margin-bottom:6px;background:rgba(255,255,255,0.03);padding:8px;border-radius:6px;border:1px solid rgba(255,255,255,0.08);">
                            <span style="min-width:22px;color:rgba(255,255,255,0.4);font-size:0.8rem;padding-top:6px;">${i + 1}.</span>
                            <div style="flex:1;display:flex;flex-direction:column;gap:4px;">
                                <input type="text" class="tk-input tk-mt-action" value="${_escHtml(mt.action || '')}" placeholder="${t('Akcja (np. Otwórz apkę Dashboard)')}" style="font-size:0.85rem;" />
                                <input type="text" class="tk-input tk-mt-expected" value="${_escHtml(mt.expected || '')}" placeholder="${t('Oczekiwany wynik')}" style="font-size:0.85rem;opacity:0.8;" />
                            </div>
                            <label style="display:flex;align-items:center;gap:4px;padding-top:6px;cursor:pointer;white-space:nowrap;font-size:0.75rem;opacity:0.7;" title="${t('Screenshot')}">
                                <input type="checkbox" class="tk-mt-screenshot" ${mt.screenshot ? 'checked' : ''} /> <i class="fas fa-camera"></i>
                            </label>
                            <button class="tk-mt-remove" style="background:none;border:none;color:rgba(255,255,255,0.3);cursor:pointer;padding:6px;" title="${t('Usuń krok')}">
                                <i class="fas fa-times"></i>
                            </button>
                        </div>
                        `).join('')}
                    </div>
                    <div style="display:flex;gap:6px;margin-top:6px;">
                        <button class="tk-btn tk-btn-small" id="tk-df-add-test-step"><i class="fas fa-plus"></i> ${t('Dodaj krok')}</button>
                        <button class="tk-btn tk-btn-small" id="tk-df-ai-gen-tests" style="opacity:0.8;" title="${t('AI wygeneruje kroki testowe z opisu ticketu')}"><i class="fas fa-magic"></i> ${t('Generuj z AI')}</button>
                    </div>
                </div>

                <div class="tk-attachments-section" style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.1);">
                    <label>${t('Załączniki')}</label>
                    <div class="tk-attachments-list" id="tk-df-attachments" style="display:grid;grid-template-columns:repeat(auto-fill, minmax(100px, 1fr));gap:8px;margin-top:8px;">
                        ${(ticket.attachments || []).map(a => {
                            const isImage = a.mimetype && a.mimetype.startsWith('image/');
                            const url = `/api/tickets/tickets/${ticket.id}/attachments/${encodeURIComponent(a.filename)}`;
                            return `
                            <div class="tk-attachment-item" style="position:relative;background:rgba(255,255,255,0.05);border-radius:6px;overflow:hidden;aspect-ratio:1;">
                                ${isImage 
                                    ? `<div style="width:100%;height:100%;background:url('${url}') center/cover no-repeat;cursor:pointer;" onclick="window.open('${url}','_blank')"></div>`
                                    : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:24px;color:rgba(255,255,255,0.5);"><i class="fas fa-file"></i></div>`
                                }
                                <div style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,0.6);padding:4px;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                                    ${_escHtml(a.filename)}
                                </div>
                                <button class="tk-att-delete" data-filename="${_escHtml(a.filename)}" style="position:absolute;top:2px;right:2px;background:rgba(0,0,0,0.5);border:none;color:#fff;border-radius:4px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;cursor:pointer;">
                                    <i class="fas fa-times" style="font-size:10px;"></i>
                                </button>
                            </div>`;
                        }).join('')}
                    </div>
                    <div style="margin-top:8px;">
                        <input type="file" id="tk-df-upload-input" style="display:none;" />
                        <button class="tk-btn tk-btn-small" id="tk-df-upload-btn"><i class="fas fa-paperclip"></i> ${t('Dodaj plik')}</button>
                    </div>
                </div>

                <div class="tk-comments-section" style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.1);">
                    <label>${t('Komentarze')} (${ticketComments.length})</label>
                    <div id="tk-df-comments" class="tk-comments-list">${commentsHTML || '<div class="tk-empty-col">' + t('Brak komentarzy') + '</div>'}</div>
                    <textarea id="tk-df-new-comment" class="tk-input" rows="2" style="margin-top:8px;" placeholder="${t('Dodaj komentarz...')}"></textarea>
                    <button class="tk-btn tk-btn-small" id="tk-df-send-comment" style="margin-top:4px;">
                        <i class="fas fa-paper-plane"></i> ${t('Wyślij')}
                    </button>
                </div>

                ${(() => {
                    const isActive = watcherExecuting && watcherExecuting.ticket_id === ticket.id;
                    const modelList = copilotLogs.map(l => l.model).filter(Boolean);
                    const uniqueModels = [...new Set(modelList)];
                    let s = '<div class="tk-agent-section" style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.1);">';
                    s += '<label><i class="fas fa-robot" style="margin-right:4px;"></i> ' + t('Agent Copilot / Lokalny') + '</label>';
                    if (isActive) {
                        const elapsed = watcherExecuting.elapsed ? Math.round(watcherExecuting.elapsed / 60) + ' min' : '';
                        const qaCycle = watcherExecuting.qa_cycle || 0;
                        s += '<div class="tk-agent-status-live">';
                        s += '<span class="tk-agent-pulse">●</span> ';
                        s += '<strong>' + t('Agent pracuje') + '</strong>';
                        s += ' — ' + _escHtml(watcherLabel(watcherExecuting));
                        if (elapsed) s += ' · ' + elapsed;
                        if (qaCycle > 0) s += ' · QA #' + qaCycle;
                        s += '</div>';
                    }
                    if (uniqueModels.length) {
                        s += '<div class="tk-model-history">';
                        s += '<span class="tk-model-history-label">' + t('Modele') + ':</span> ';
                        s += uniqueModels.map(m => '<span class="tk-model-tag">' + _escHtml(m) + '</span>').join(' ');
                        s += '</div>';
                    } else if (!isActive) {
                        s += '<div class="tk-agent-hint" style="font-size:12px;opacity:0.75;">' + t('Copilot nie wykonał jeszcze akcji dla tego ticketa.') + '</div>';
                    }
                    s += '<div class="tk-agent-actions" style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px;">';
                    const lm = localModel ? (localModel.name || localModel.id || 'local') : null;
                    const lmLabel = lm ? _escHtml(lm) : t('brak aktywnego modelu');
                    s += '<button class="tk-btn tk-btn-small" id="tk-open-aichat"><i class="fas fa-comments"></i> ' + t('AI Chat (lokalny)') + '</button>';
                    s += '<div class="tk-agent-hint" style="font-size:12px;opacity:0.75;">' + t('Otwórz AI Chat z kontekstem ticketa (bez RAG). Lokalny model: ') + lmLabel + '</div>';
                    if (!localModel) {
                        s += '<div class="tk-agent-hint" style="font-size:12px;color:#f97316;">' + t('Aktywuj model w Bibliotece modeli (np. Qwen 2.5 Coder 7B) aby użyć lokalnego agenta.') + '</div>';
                    }
                    s += '</div>';
                    s += '</div>';
                    return s;
                })()}

                ${copilotLogs.length ? `
                <div class="tk-copilot-logs-section" style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.1);">
                    <label><i class="fas fa-robot" style="margin-right:4px;"></i> Copilot Logs (${copilotLogs.length}) <span id="tk-log-live" class="tk-log-live-badge" style="display:none;">● LIVE</span></label>
                    <div class="tk-log-tabs" id="tk-log-tabs">
                        ${copilotLogs.map((log, i) => {
                            const d = new Date(log.timestamp * 1000);
                            const agentIcon = log.agent === 'localai' ? '🧠' : log.agent === 'freemodel' ? '🎁' : (log.type === 'qa' ? '🔍 QA' : '🤖 Dev');
                            const label = agentIcon + ' ' +
                                d.toLocaleString('pl', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' });
                            const sizeKB = (log.size / 1024).toFixed(1);
                            const modelTag = log.model ? ' <span class="tk-log-model">' + _escHtml(log.model) + '</span>' : '';
                            return '<button class="tk-log-tab' + (i === 0 ? ' active' : '') + '" data-filename="' +
                                _escHtml(log.filename) + '" data-agent="' + (log.agent || 'copilot') + '" data-idx="' + i + '">' + label + modelTag + ' <span class="tk-log-size">' + sizeKB + 'KB</span></button>';
                        }).join('')}
                    </div>
                    <div class="tk-log-viewer" id="tk-log-viewer">
                        <div class="tk-log-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>
                    </div>
                </div>
                ` : ''}
            </div>
        `;

        loadingOverlay.remove();

        const overlay = tkShowModal(
            PRIORITY_ICONS[ticket.priority] + ' <span class="tk-ticket-id">' + _shortId(ticket.id) + '</span> ' + _escHtml(ticket.title),
            html,
            t('Zapisz'),
            (modal) => {
                const title = modal.querySelector('#tk-df-title').value.trim();
                if (!title) { toast(t('Tytuł jest wymagany'), 'warning'); return false; }

                const labelEls = modal.querySelectorAll('#tk-df-labels .tk-label-removable');
                const updatedLabels = Array.from(labelEls).map(el => el.dataset.label);

                updateTicket(ticket.id, {
                    title,
                    description: modal.querySelector('#tk-df-desc').value.trim(),
                    type: modal.querySelector('#tk-df-type').value,
                    column: modal.querySelector('#tk-df-column').value,
                    priority: modal.querySelector('#tk-df-priority').value,
                    complexity: modal.querySelector('#tk-df-complexity').value,
                    assignee: modal.querySelector('#tk-df-assignee').value,
                    labels: updatedLabels,
                    manual_tests: Array.from(modal.querySelectorAll('.tk-mt-step')).map((el, i) => ({
                        step: i + 1,
                        action: el.querySelector('.tk-mt-action').value.trim(),
                        expected: el.querySelector('.tk-mt-expected').value.trim(),
                        screenshot: el.querySelector('.tk-mt-screenshot').checked,
                    })).filter(s => s.action),
                });
            }
        );

        /* ── add delete button to footer ── */
        const footer = overlay.querySelector('.modal-footer');
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'btn btn-secondary';
        deleteBtn.style.cssText = 'background:#ef4444;border-color:#ef4444;margin-right:auto;';
        deleteBtn.innerHTML = '<i class="fas fa-trash"></i> ' + t('Usuń');
        footer.prepend(deleteBtn);

        deleteBtn.onclick = () => {
            overlay.remove();
            tkConfirm(
                t('Usuń ticket'),
                t('Czy na pewno chcesz usunąć ticket') + ' <strong>' + _escHtml(ticket.title) + '</strong>?',
                () => deleteTicket(ticket.id)
            );
        };

        /* ── label management ── */
        function bindLabelRemoval() {
            overlay.querySelectorAll('.tk-remove-label').forEach(btn => {
                btn.onclick = (e) => {
                    e.stopPropagation();
                    btn.closest('.tk-label-removable').remove();
                };
            });
        }
        bindLabelRemoval();

        overlay.querySelector('#tk-df-add-label').onclick = () => {
            const input = overlay.querySelector('#tk-df-new-label');
            const val = input.value.trim();
            if (!val) return;
            const container = overlay.querySelector('#tk-df-labels');
            const span = document.createElement('span');
            span.className = 'tk-label tk-label-removable';
            span.style.background = tkLabelColor(val);
            span.dataset.label = val;
            span.innerHTML = _escHtml(val) + ' <i class="fas fa-times tk-remove-label"></i>';
            container.appendChild(span);
            input.value = '';
            bindLabelRemoval();
        };

        overlay.querySelector('#tk-df-new-label').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                overlay.querySelector('#tk-df-add-label').click();
            }
        });

        /* ── manual tests logic ── */
        function _mtAddStep(action = '', expected = '', screenshot = true) {
            const container = overlay.querySelector('#tk-df-manual-tests');
            const steps = container.querySelectorAll('.tk-mt-step');
            const idx = steps.length;
            const div = document.createElement('div');
            div.className = 'tk-mt-step';
            div.dataset.step = idx;
            div.style.cssText = 'display:flex;gap:6px;align-items:flex-start;margin-bottom:6px;background:rgba(255,255,255,0.03);padding:8px;border-radius:6px;border:1px solid rgba(255,255,255,0.08);';
            div.innerHTML = `
                <span style="min-width:22px;color:rgba(255,255,255,0.4);font-size:0.8rem;padding-top:6px;">${idx + 1}.</span>
                <div style="flex:1;display:flex;flex-direction:column;gap:4px;">
                    <input type="text" class="tk-input tk-mt-action" value="${_escHtml(action)}" placeholder="${t('Akcja (np. Otwórz apkę Dashboard)')}" style="font-size:0.85rem;" />
                    <input type="text" class="tk-input tk-mt-expected" value="${_escHtml(expected)}" placeholder="${t('Oczekiwany wynik')}" style="font-size:0.85rem;opacity:0.8;" />
                </div>
                <label style="display:flex;align-items:center;gap:4px;padding-top:6px;cursor:pointer;white-space:nowrap;font-size:0.75rem;opacity:0.7;" title="${t('Screenshot')}">
                    <input type="checkbox" class="tk-mt-screenshot" ${screenshot ? 'checked' : ''} /> <i class="fas fa-camera"></i>
                </label>
                <button class="tk-mt-remove" style="background:none;border:none;color:rgba(255,255,255,0.3);cursor:pointer;padding:6px;" title="${t('Usuń krok')}">
                    <i class="fas fa-times"></i>
                </button>`;
            container.appendChild(div);
            _mtBindRemove();
            div.querySelector('.tk-mt-action').focus();
        }

        function _mtBindRemove() {
            overlay.querySelectorAll('.tk-mt-remove').forEach(btn => {
                btn.onclick = () => {
                    btn.closest('.tk-mt-step').remove();
                    _mtRenumber();
                };
            });
        }

        function _mtRenumber() {
            overlay.querySelectorAll('.tk-mt-step').forEach((el, i) => {
                el.dataset.step = i;
                el.querySelector('span').textContent = (i + 1) + '.';
            });
        }

        _mtBindRemove();

        overlay.querySelector('#tk-df-add-test-step').onclick = () => _mtAddStep();

        overlay.querySelector('#tk-df-ai-gen-tests').onclick = async () => {
            const btn = overlay.querySelector('#tk-df-ai-gen-tests');
            const desc = overlay.querySelector('#tk-df-desc').value.trim();
            const title = overlay.querySelector('#tk-df-title').value.trim();
            if (!title && !desc) { toast(t('Wpisz tytuł lub opis ticketu'), 'warning'); return; }
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Generuję (może zająć 3-5 min)...');
            try {
                const start = await api('/tickets/tickets/' + ticket.id + '/generate-tests', {
                    method: 'POST',
                    body: { title, description: desc },
                });
                if (!start || !start.task_id) { toast(t('Nie udało się wystartować generowania'), 'warning'); throw new Error('no task_id'); }
                const taskId = start.task_id;
                let elapsed = 0;
                const poll = async () => {
                    while (elapsed < 600) {
                        await new Promise(r => setTimeout(r, 5000));
                        elapsed += 5;
                        const min = Math.floor(elapsed / 60);
                        const sec = elapsed % 60;
                        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Generuję') + ` (${min}:${String(sec).padStart(2,'0')})...`;
                        const res = await api('/tickets/gen-tests-poll/' + taskId);
                        if (res.status === 'done') {
                            if (res.tests && res.tests.length) {
                                res.tests.forEach(s => _mtAddStep(s.action || '', s.expected || '', s.screenshot !== false));
                                toast(t('Wygenerowano') + ' ' + res.tests.length + ' ' + t('kroków'), 'success');
                            } else {
                                toast(t('AI nie zwróciło testów') + (res.error ? ': ' + res.error : ''), 'warning');
                            }
                            return;
                        }
                        if (res.status === 'error') { toast(t('Błąd AI: ') + (res.error || ''), 'error'); return; }
                    }
                    toast(t('Przekroczono czas oczekiwania (10 min)'), 'warning');
                };
                await poll();
            } catch (e) {
                toast(t('Błąd generowania testów: ') + (e.message || e), 'error');
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-magic"></i> ' + t('Generuj z AI');
        };

        /* ── attachments logic ── */
        overlay.querySelector('#tk-df-upload-btn').onclick = () => {
            overlay.querySelector('#tk-df-upload-input').click();
        };

        function bindAttachmentEvents() {
             overlay.querySelectorAll('.tk-att-delete').forEach(btn => {
                btn.onclick = async (e) => {
                     e.stopPropagation(); 
                     if(!await confirmDialog(t('Usunąć plik?'))) return;
                     const filename = btn.dataset.filename;
                     const item = btn.closest('.tk-attachment-item');
                     item.style.opacity = '0.5';
                     try {
                         await api(`/tickets/tickets/${ticket.id}/attachments/${encodeURIComponent(filename)}`, { method: 'DELETE' });
                         item.remove();
                     } catch(e) { 
                         toast(t('Błąd usuwania'), 'error'); 
                         item.style.opacity = '1';
                     }
                };
            });
        }
        bindAttachmentEvents();
        
        overlay.querySelector('#tk-df-upload-input').onchange = async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            
            const fd = new FormData();
            fd.append('file', file);
            
            // Optimistic UI
            const container = overlay.querySelector('#tk-df-attachments');
            const tempId = 'temp-' + Date.now();
            const tempHTML = `
                <div class="tk-attachment-item" id="${tempId}" style="position:relative;background:rgba(255,255,255,0.05);border-radius:6px;overflow:hidden;aspect-ratio:1;display:flex;align-items:center;justify-content:center;">
                    <i class="fas fa-spinner fa-spin"></i>
                </div>`;
            container.insertAdjacentHTML('beforeend', tempHTML);
            
            try {
                const res = await api(`/tickets/tickets/${ticket.id}/attachments`, {
                    method: 'POST',
                    body: fd
                });
                
                const a = res.attachment;
                const isImage = a.mimetype && a.mimetype.startsWith('image/');
                const url = `/api/tickets/tickets/${ticket.id}/attachments/${encodeURIComponent(a.filename)}`;
                
                const finalHTML = `
                    <div class="tk-attachment-item" style="position:relative;background:rgba(255,255,255,0.05);border-radius:6px;overflow:hidden;aspect-ratio:1;">
                        ${isImage 
                            ? `<div style="width:100%;height:100%;background:url('${url}') center/cover no-repeat;cursor:pointer;" onclick="window.open('${url}','_blank')"></div>`
                            : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:24px;color:rgba(255,255,255,0.5);"><i class="fas fa-file"></i></div>`
                        }
                        <div style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,0.6);padding:4px;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                            ${_escHtml(a.filename)}
                        </div>
                        <button class="tk-att-delete" data-filename="${_escHtml(a.filename)}" style="position:absolute;top:2px;right:2px;background:rgba(0,0,0,0.5);border:none;color:#fff;border-radius:4px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;cursor:pointer;">
                            <i class="fas fa-times" style="font-size:10px;"></i>
                        </button>
                    </div>`;
                
                const tempEl = document.getElementById(tempId);
                if (tempEl) tempEl.outerHTML = finalHTML;
                
                bindAttachmentEvents(); 
                toast(t('Plik dodany'), 'success');
                
            } catch (err) {
                console.error(err);
                document.getElementById(tempId)?.remove();
                toast(t('Błąd wysyłania pliku'), 'error');
            }
            e.target.value = '';
        };

        /* ── AI Chat integration ── */
        const aiBtn = overlay.querySelector('#tk-open-aichat');
        if (aiBtn) {
            aiBtn.onclick = () => {
                const appDef = (window.NAS && NAS.apps || []).find(a => a.id === 'ai-chat');
                if (!appDef) { toast(t('AI Chat niedostępny'), 'error'); return; }
                const launchOpts = {
                    ticketContext: {
                        id: ticket.id,
                        title: ticket.title,
                        description: ticket.description || '',
                        priority: ticket.priority,
                        complexity: ticket.complexity,
                        column: ticket.column,
                        labels: ticket.labels || [],
                    },
                    preferredModel: localModel ? { id: localModel.id, name: localModel.name } : null,
                    disableRag: true
                };
                openApp(appDef, launchOpts);
            };
        }

        /* ── comments ── */
        let _commentCount = ticketComments.length;
        function _updateCommentCount() {
            const lbl = overlay.querySelector('.tk-comments-section > label');
            if (lbl) lbl.textContent = t('Komentarze') + ' (' + _commentCount + ')';
        }

        const _sendComment = async () => {
            const textarea = overlay.querySelector('#tk-df-new-comment');
            const text = textarea.value.trim();
            if (!text) return;

            const ok = await addComment(ticket.id, text);
            if (ok) {
                textarea.value = '';
                await loadTickets(currentProject.id);
                const list = overlay.querySelector('#tk-df-comments');
                const emptyMsg = list.querySelector('.tk-empty-col');
                if (emptyMsg) emptyMsg.remove();

                const div = document.createElement('div');
                div.className = 'tk-comment';
                div.innerHTML = `
                    <div class="tk-comment-header">
                        <strong>${_escHtml(NAS.user?.username)}</strong>
                        <span class="tk-comment-date">${new Date().toLocaleString()}</span>
                    </div>
                    <div class="tk-comment-body">${_escHtml(text)}</div>
                `;
                list.appendChild(div);
                list.scrollTop = list.scrollHeight;
                _commentCount++;
                _updateCommentCount();
                toast(t('Komentarz dodany'), 'success');
            }
        };
        overlay.querySelector('#tk-df-send-comment').onclick = _sendComment;
        overlay.querySelector('#tk-df-new-comment').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                _sendComment();
            }
        });

        /* ── comment deletion ── */
        overlay.querySelector('#tk-df-comments').addEventListener('click', async (e) => {
            const btn = e.target.closest('.tk-comment-delete');
            if (!btn) return;
            const cid = btn.dataset.cid;
            if (!cid) return;
            try {
                await api('/tickets/tickets/' + ticket.id + '/comments/' + cid, { method: 'DELETE' });
                const row = btn.closest('.tk-comment');
                if (row) row.remove();
                _commentCount--;
                _updateCommentCount();
                await loadTickets(currentProject.id);
                toast(t('Komentarz usunięty'), 'success');
            } catch (err) {
                toast(t('Błąd usuwania komentarza'), 'error');
            }
        });

        /* ── copilot log tabs ── */
        const logTabs = overlay.querySelector('#tk-log-tabs');
        const logViewer = overlay.querySelector('#tk-log-viewer');
        let _logPollTimer = null;
        let _logOffset = 0;
        let _logFilename = null;

        /* Clean up log polling when modal is removed from DOM */
        const _cleanupObserver = new MutationObserver(() => {
            if (!document.body.contains(overlay)) {
                if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
                _cleanupObserver.disconnect();
            }
        });
        _cleanupObserver.observe(document.body, { childList: true });

        if (logTabs && logViewer) {
            let _logAgent = 'copilot';

            const loadLog = async (filename, agent) => {
                // stop previous polling
                if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
                _logOffset = 0;
                _logFilename = filename;
                _logAgent = agent || 'copilot';
                logViewer.innerHTML = '<div class="tk-log-loading"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div>';
                try {
                    const resp = await api('/tickets/tickets/' + ticket.id + '/copilot-logs/' + encodeURIComponent(filename) + '?agent=' + _logAgent);
                    const content = (resp && resp.content) || '';
                    _logOffset = resp.offset || content.length;
                    logViewer.innerHTML = '<pre class="tk-log-content">' + _escHtml(content) + '</pre>';
                    logViewer.scrollTop = logViewer.scrollHeight;
                    // start polling for new content
                    _logPollTimer = setInterval(() => pollLogUpdates(filename), 2000);
                } catch (e) {
                    logViewer.innerHTML = '<div class="tk-log-error"><i class="fas fa-exclamation-triangle"></i> ' + t('Błąd ładowania logu') + '</div>';
                }
            };

            const pollLogUpdates = async (filename) => {
                if (filename !== _logFilename) return;
                if (!document.body.contains(logViewer)) {
                    clearInterval(_logPollTimer); _logPollTimer = null; return;
                }
                const liveBadge = overlay.querySelector('#tk-log-live');
                try {
                    const resp = await api('/tickets/tickets/' + ticket.id + '/copilot-logs/' + encodeURIComponent(filename) + '?agent=' + _logAgent + '&offset=' + _logOffset);
                    const newContent = (resp && resp.content) || '';
                    if (newContent.length > 0) {
                        _logOffset = resp.offset || (_logOffset + newContent.length);
                        const pre = logViewer.querySelector('.tk-log-content');
                        if (pre) {
                            pre.insertAdjacentHTML('beforeend', _escHtml(newContent));
                            const wasAtBottom = logViewer.scrollHeight - logViewer.scrollTop - logViewer.clientHeight < 80;
                            if (wasAtBottom) logViewer.scrollTop = logViewer.scrollHeight;
                        }
                        if (liveBadge) liveBadge.style.display = 'inline';
                    } else {
                        if (liveBadge) liveBadge.style.display = 'none';
                    }
                } catch (e) { /* silent — will retry next poll */ }
            };

            logTabs.addEventListener('click', (e) => {
                const tab = e.target.closest('.tk-log-tab');
                if (!tab) return;
                logTabs.querySelectorAll('.tk-log-tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                loadLog(tab.dataset.filename, tab.dataset.agent);
            });

            // If ticket is currently being processed, find & auto-select its live log tab
            let autoLoaded = false;
            if (watcherExecuting && watcherExecuting.executing && watcherExecuting.ticket_id === ticket.id && watcherExecuting.log_file) {
                const liveTab = logTabs.querySelector('[data-filename="' + CSS.escape(watcherExecuting.log_file) + '"]');
                if (liveTab) {
                    logTabs.querySelectorAll('.tk-log-tab').forEach(t => t.classList.remove('active'));
                    liveTab.classList.add('active');
                    loadLog(liveTab.dataset.filename, liveTab.dataset.agent);
                    const liveBadge = overlay.querySelector('#tk-log-live');
                    if (liveBadge) liveBadge.style.display = 'inline';
                    autoLoaded = true;
                }
            }
            if (!autoLoaded) {
                const firstTab = logTabs.querySelector('.tk-log-tab');
                if (firstTab) loadLog(firstTab.dataset.filename, firstTab.dataset.agent);
            }
        }
    }

    /* ═══════════════════ WATCHER MODAL ═══════════════════ */

    function showAiUsageModal() {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:700px;max-width:95vw;max-height:90vh;display:flex;flex-direction:column;">
                <div class="modal-header">
                    <span><i class="fas fa-chart-bar" style="margin-right:6px;"></i>${t('AI Usage')}</span>
                    <button class="modal-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="modal-body" style="overflow-y:auto;flex:1;padding:16px;" id="tk-ai-usage-body">
                    <div style="text-align:center;padding:40px;opacity:0.5;"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary tk-modal-cancel">${t('Zamknij')}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        overlay.querySelector('.modal-close').onclick = () => overlay.remove();
        overlay.querySelector('.tk-modal-cancel').onclick = () => overlay.remove();
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

        const body = overlay.querySelector('#tk-ai-usage-body');

        api(`/tickets/ai-usage/${currentProject.id}`).then(data => {
            const tot = data.totals || {};
            const premiumFmt = (v) => v % 1 === 0 ? v.toFixed(0) : v.toFixed(1);
            const timeFmt = (s) => {
                if (s < 60) return s + 's';
                const m = Math.floor(s / 60), rs = s % 60;
                if (m < 60) return m + 'm ' + rs + 's';
                const h = Math.floor(m / 60), rm = m % 60;
                return h + 'h ' + rm + 'm';
            };
            const tokenFmt = (n) => {
                if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
                if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
                if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
                return String(n);
            };

            let html = `
                <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px;">
                    <div style="background:var(--card-bg,#1e1e2e);border-radius:8px;padding:12px;text-align:center;">
                        <div style="font-size:22px;font-weight:700;color:#8b5cf6;">${premiumFmt(tot.premium_requests || 0)}</div>
                        <div style="font-size:11px;opacity:0.6;">Premium Requests</div>
                    </div>
                    <div style="background:var(--card-bg,#1e1e2e);border-radius:8px;padding:12px;text-align:center;">
                        <div style="font-size:22px;font-weight:700;color:#22c55e;">${tot.runs || 0}</div>
                        <div style="font-size:11px;opacity:0.6;">Runs (${tot.qa_runs || 0} QA)</div>
                    </div>
                    <div style="background:var(--card-bg,#1e1e2e);border-radius:8px;padding:12px;text-align:center;">
                        <div style="font-size:22px;font-weight:700;color:#3b82f6;">${timeFmt(tot.session_time_s || 0)}</div>
                        <div style="font-size:11px;opacity:0.6;">Session Time</div>
                    </div>
                    <div style="background:var(--card-bg,#1e1e2e);border-radius:8px;padding:12px;text-align:center;">
                        <div style="font-size:22px;font-weight:700;color:#f59e0b;">+${tot.code_added || 0} / -${tot.code_removed || 0}</div>
                        <div style="font-size:11px;opacity:0.6;">Lines Changed</div>
                    </div>
                </div>
            `;

            // By Model table
            const models = Object.entries(data.by_model || {});
            if (models.length) {
                html += `<h4 style="margin:16px 0 8px;font-size:13px;opacity:0.7;"><i class="fas fa-robot" style="margin-right:4px;"></i> ${t('By Model')}</h4>`;
                html += `<table style="width:100%;font-size:12px;border-collapse:collapse;">`;
                html += `<tr style="opacity:0.5;text-align:left;"><th style="padding:4px 8px;">Model</th><th>Premium</th><th>Runs</th><th>Time</th><th>Tokens In</th><th>Tokens Out</th></tr>`;
                models.sort((a, b) => b[1].premium_requests - a[1].premium_requests);
                for (const [model, m] of models) {
                    html += `<tr style="border-top:1px solid rgba(255,255,255,0.06);">`;
                    html += `<td style="padding:4px 8px;font-weight:500;">${_escHtml(model)}</td>`;
                    html += `<td style="padding:4px 8px;">${premiumFmt(m.premium_requests)}</td>`;
                    html += `<td style="padding:4px 8px;">${m.runs}</td>`;
                    html += `<td style="padding:4px 8px;">${timeFmt(m.session_time_s)}</td>`;
                    html += `<td style="padding:4px 8px;">${tokenFmt(m.tokens_in)}</td>`;
                    html += `<td style="padding:4px 8px;">${tokenFmt(m.tokens_out)}</td>`;
                    html += `</tr>`;
                }
                html += `</table>`;
            }

            // By Month table
            const months = Object.entries(data.by_month || {});
            if (months.length) {
                html += `<h4 style="margin:16px 0 8px;font-size:13px;opacity:0.7;"><i class="fas fa-calendar" style="margin-right:4px;"></i> ${t('By Month')}</h4>`;
                html += `<table style="width:100%;font-size:12px;border-collapse:collapse;">`;
                html += `<tr style="opacity:0.5;text-align:left;"><th style="padding:4px 8px;">Month</th><th>Premium</th><th>Runs</th><th>Time</th><th>+/-</th></tr>`;
                months.reverse();
                for (const [month, m] of months) {
                    html += `<tr style="border-top:1px solid rgba(255,255,255,0.06);">`;
                    html += `<td style="padding:4px 8px;font-weight:500;">${_escHtml(month)}</td>`;
                    html += `<td style="padding:4px 8px;">${premiumFmt(m.premium_requests)}</td>`;
                    html += `<td style="padding:4px 8px;">${m.runs} (${m.qa_runs} QA)</td>`;
                    html += `<td style="padding:4px 8px;">${timeFmt(m.session_time_s)}</td>`;
                    html += `<td style="padding:4px 8px;">+${m.code_added} / -${m.code_removed}</td>`;
                    html += `</tr>`;
                }
                html += `</table>`;
            }

            // By Day (last 14 days)
            const days = Object.entries(data.by_day || {});
            if (days.length) {
                const recentDays = days.slice(-14);
                html += `<h4 style="margin:16px 0 8px;font-size:13px;opacity:0.7;"><i class="fas fa-chart-line" style="margin-right:4px;"></i> ${t('Daily')} (${t('last')} ${recentDays.length} ${t('days')})</h4>`;
                // Mini bar chart
                const maxPremium = Math.max(...recentDays.map(([, d]) => d.premium_requests), 1);
                html += `<div style="display:flex;align-items:flex-end;gap:3px;height:80px;margin-bottom:4px;">`;
                for (const [day, d] of recentDays) {
                    const h = Math.max(2, (d.premium_requests / maxPremium) * 70);
                    const label = day.slice(5); // MM-DD
                    html += `<div style="flex:1;display:flex;flex-direction:column;align-items:center;" title="${day}: ${premiumFmt(d.premium_requests)} premium, ${d.runs} runs">`;
                    html += `<div style="width:100%;max-width:32px;height:${h}px;background:#8b5cf6;border-radius:3px 3px 0 0;min-width:8px;"></div>`;
                    html += `<div style="font-size:9px;opacity:0.4;margin-top:2px;white-space:nowrap;">${label}</div>`;
                    html += `</div>`;
                }
                html += `</div>`;
            }

            if (!models.length && !days.length) {
                html += `<div style="text-align:center;padding:30px;opacity:0.4;"><i class="fas fa-chart-bar" style="font-size:2rem;"></i><p>${t('Brak danych')}</p></div>`;
            }

            body.innerHTML = html;
        }).catch(err => {
            body.innerHTML = `<div style="text-align:center;padding:30px;color:#ef4444;"><i class="fas fa-exclamation-triangle"></i> ${_escHtml(String(err))}</div>`;
        });
    }

    function showWatcherModal() {
        const html = `
            <div class="tk-form">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
                    <i class="fas fa-tower-broadcast" style="font-size:16px;"></i>
                    <span style="font-weight:600;">Ticket Watcher</span>
                    <span id="tk-wm-status" class="tk-watcher-badge" style="font-size:11px;padding:2px 8px;border-radius:10px;margin-left:auto;">…</span>
                </div>
                <div id="tk-wm-exec" style="margin-bottom:12px;"></div>
                <div style="display:flex;gap:6px;">
                    <button type="button" class="tk-btn tk-btn-sm" id="tk-wm-start"><i class="fas fa-play"></i> Start</button>
                    <button type="button" class="tk-btn tk-btn-sm" id="tk-wm-stop"><i class="fas fa-stop"></i> Stop</button>
                    <button type="button" class="tk-btn tk-btn-sm" id="tk-wm-restart"><i class="fas fa-rotate"></i> Restart</button>
                </div>
                <small id="tk-wm-detail" style="opacity:.6;margin-top:8px;display:block;"></small>
            </div>
        `;

        const overlay = tkShowModal(t('Ticket Watcher'), html, t('Zamknij'), () => {});

        const statusBadge = overlay.querySelector('#tk-wm-status');
        const detailEl = overlay.querySelector('#tk-wm-detail');
        const execEl = overlay.querySelector('#tk-wm-exec');

        async function refreshStatus() {
            try {
                const s = await api('/tickets/watcher/status');
                const running = s.active === 'active';
                statusBadge.textContent = running ? 'running' : s.active;
                statusBadge.style.background = running ? '#22c55e' : '#ef4444';
                statusBadge.style.color = '#fff';
                const pid = s.pid && s.pid !== '0' ? ` · PID ${s.pid}` : '';
                const since = s.since ? ` · ${s.since}` : '';
                detailEl.textContent = `${s.state}/${s.substate}${pid}${since}`;
            } catch (e) {
                statusBadge.textContent = 'error';
                statusBadge.style.background = '#666';
                detailEl.textContent = String(e);
            }

            // Also show executing info
            try {
                const ex = await api('/tickets/watcher/executing');
                if (ex && ex.executing) {
                    const elapsed = ex.elapsed ? Math.round(ex.elapsed / 60) + ' min' : '';
                    const tk = tickets.find(t2 => t2.id === ex.ticket_id);
                    const title = tk ? _escHtml(tk.title) : ex.ticket_id;
                    execEl.innerHTML = '<div class="tk-agent-status-live"><span class="tk-agent-pulse">●</span> ' +
                        '<strong>' + _escHtml(ex.model_label || ex.model || '') + '</strong>' +
                        ' — ' + title +
                        (elapsed ? ' · ' + elapsed : '') +
                        (ex.qa_cycle > 0 ? ' · QA #' + ex.qa_cycle : '') +
                        '</div>';
                } else {
                    execEl.innerHTML = '<div style="font-size:12px;opacity:0.5;">' + t('Brak aktywnego zadania') + '</div>';
                }
            } catch (e) { execEl.innerHTML = ''; }
        }
        refreshStatus();

        async function doAction(action) {
            const btn = overlay.querySelector('#tk-wm-' + action);
            if (btn) btn.disabled = true;
            try {
                const r = await api('/tickets/watcher/control', { method: 'POST', body: { action } });
                toast(`Ticket Watcher: ${action} → ${r.active}`, 'success');
            } catch (e) {
                toast(`Ticket Watcher ${action} failed: ${e}`, 'error');
            }
            await refreshStatus();
            if (btn) btn.disabled = false;
        }

        overlay.querySelector('#tk-wm-start').onclick = () => doAction('start');
        overlay.querySelector('#tk-wm-stop').onclick = () => doAction('stop');
        overlay.querySelector('#tk-wm-restart').onclick = () => doAction('restart');
    }

    /* ═══════════════════ INIT ═══════════════════ */

    await showProjectList();
}

})();
