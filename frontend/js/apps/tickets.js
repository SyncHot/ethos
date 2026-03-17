/* ─────────────────── Tickets / Kanban Board (EthOS) ─────────────────── */
/* globals AppRegistry, createWindow, NAS, api, toast, t, _escHtml */

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
    medium: 'Średni',
    low: 'Niski',
};

const PRIORITY_ICONS = {
    critical: '🔴',
    high: '🟠',
    medium: '🟡',
    low: '🟢',
};

const LABEL_COLORS = [
    '#3b82f6', '#ef4444', '#22c55e', '#f59e0b', '#8b5cf6',
    '#ec4899', '#06b6d4', '#f97316', '#14b8a6', '#6366f1',
];

const DEFAULT_COLUMNS = ['Backlog', 'Do zrobienia', 'W trakcie', 'Review', 'Gotowe'];

/* ═══════════════════════════ MODAL HELPER ═══════════════════════════ */

function tkShowModal(title, contentHTML, confirmLabel, onConfirm) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-box" style="width:550px;max-height:90vh;display:flex;flex-direction:column;">
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

/* ═══════════════════════════ MAIN RENDER ═══════════════════════════ */

async function renderTickets(body, launchOpts) {
    /* ── closure state ── */
    let projects = [];
    let currentProject = null;
    let tickets = [];
    let comments = [];
    let filterAssignee = '';
    let filterPriority = '';
    let filterSearch = '';

    /* ── root container ── */
    body.innerHTML = '<div class="tk-app"><div class="tk-loading" style="padding:2rem;text-align:center;"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div></div>';
    const app = body.querySelector('.tk-app');

    /* ── navigation ── */
    async function showProjectList() {
        currentProject = null;
        tickets = [];
        filterAssignee = '';
        filterPriority = '';
        filterSearch = '';
        await loadProjects();
        renderProjectList();
    }

    async function showBoard(project) {
        currentProject = project;
        await loadTickets(project.id);
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
            const data = await api('/tickets/projects/' + projectId + '/tickets');
            tickets = data.tickets || [];
        } catch (e) {
            toast(t('Błąd ładowania ticketów'), 'error');
            tickets = [];
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
            const data = await api('/tickets/projects/' + currentProject.id + '/tickets', {
                method: 'POST', body: payload,
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
                        <span>${totalTickets} ${t('ticketów')}</span>
                        <span>${inProgress} ${t('w trakcie')}</span>
                    </div>
                    <div class="tk-project-footer">
                        <span class="tk-project-members">${memberStr}${_escHtml(extraMembers)}</span>
                        <span class="tk-project-actions">
                            <button class="tk-btn-icon tk-edit-project" data-id="${_escHtml(p.id)}" title="${t('Ustawienia')}">
                                <i class="fas fa-ellipsis-vertical"></i>
                            </button>
                            ${isOwner ? '<button class="tk-btn-icon tk-delete-project" data-id="' + _escHtml(p.id) + '" title="' + t('Usuń') + '"><i class="fas fa-trash"></i></button>' : ''}
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
                if (e.target.closest('.tk-btn-icon')) return;
                const proj = projects.find(p => String(p.id) === card.dataset.id);
                if (proj) showBoard(proj);
            });
        });

        grid.querySelectorAll('.tk-edit-project').forEach(btn => {
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

    /* ═══════════════════ KANBAN BOARD VIEW ═══════════════════ */

    function getFilteredTickets() {
        return tickets.filter(t => {
            if (filterAssignee && t.assignee !== filterAssignee) return false;
            if (filterPriority && t.priority !== filterPriority) return false;
            if (filterSearch && !t.title.toLowerCase().includes(filterSearch.toLowerCase())) return false;
            return true;
        });
    }

    function renderBoard() {
        const columns = currentProject.columns || DEFAULT_COLUMNS;
        const filtered = getFilteredTickets();
        const members = currentProject.members || [];
        const projColor = currentProject.color || '#8b5cf6';

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
                <button class="tk-btn-icon" id="tk-project-settings" title="${t('Ustawienia')}">
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

        const board = app.querySelector('#tk-board');

        /* ── render columns ── */
        columns.forEach(colName => {
            const colTickets = filtered.filter(tk => tk.column === colName);
            const col = document.createElement('div');
            col.className = 'tk-column';
            col.dataset.column = colName;

            col.innerHTML = `
                <div class="tk-column-header">
                    <span class="tk-column-title">${_escHtml(colName)}</span>
                    <span class="tk-column-count">${colTickets.length}</span>
                </div>
                <div class="tk-column-body" data-column="${_escHtml(colName)}">
                    ${colTickets.length === 0 ? '<div class="tk-empty-col">' + t('Brak ticketów') + '</div>' : ''}
                </div>
            `;

            const colBody = col.querySelector('.tk-column-body');

            colTickets.forEach((tk, idx) => {
                const card = document.createElement('div');
                card.className = 'tk-card';
                card.draggable = true;
                card.dataset.id = tk.id;
                card.dataset.column = colName;
                card.dataset.order = idx;

                const labels = (tk.labels || []).map(l =>
                    `<span class="tk-label" style="background:${tkLabelColor(l)};">${_escHtml(l)}</span>`
                ).join('');

                const prioColor = PRIORITY_COLORS[tk.priority] || PRIORITY_COLORS.medium;

                card.innerHTML = `
                    <div class="tk-card-header">
                        <span class="tk-priority-dot" style="background:${prioColor};" title="${_escHtml(PRIORITY_LABELS[tk.priority] || tk.priority)}"></span>
                        <span class="tk-card-title">${_escHtml(tk.title)}</span>
                    </div>
                    ${tk.assignee ? '<div class="tk-card-assignee"><i class="fas fa-user"></i> ' + _escHtml(tk.assignee) + '</div>' : ''}
                    ${labels ? '<div class="tk-card-labels">' + labels + '</div>' : ''}
                `;

                /* drag events on card */
                card.addEventListener('dragstart', (e) => {
                    e.dataTransfer.setData('text/plain', JSON.stringify({
                        id: tk.id, fromColumn: colName,
                    }));
                    card.classList.add('tk-card-dragging');
                    setTimeout(() => card.style.opacity = '0.5', 0);
                });

                card.addEventListener('dragend', () => {
                    card.classList.remove('tk-card-dragging');
                    card.style.opacity = '';
                    board.querySelectorAll('.tk-column-dragover').forEach(el =>
                        el.classList.remove('tk-column-dragover')
                    );
                });

                /* click to open detail */
                card.addEventListener('click', () => showTicketDetail(tk));

                colBody.appendChild(card);
            });

            /* drop events on column body */
            colBody.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                colBody.classList.add('tk-column-dragover');
            });

            colBody.addEventListener('dragleave', (e) => {
                if (!colBody.contains(e.relatedTarget)) {
                    colBody.classList.remove('tk-column-dragover');
                }
            });

            colBody.addEventListener('drop', (e) => {
                e.preventDefault();
                colBody.classList.remove('tk-column-dragover');
                try {
                    const payload = JSON.parse(e.dataTransfer.getData('text/plain'));
                    if (payload.id) {
                        const targetCards = colBody.querySelectorAll('.tk-card');
                        let order = targetCards.length;
                        moveTicket(payload.id, colName, order);
                    }
                } catch (_) { /* ignore bad data */ }
            });

            board.appendChild(col);
        });

        /* ── toolbar bindings ── */
        app.querySelector('#tk-back').onclick = () => showProjectList();
        app.querySelector('#tk-new-ticket').onclick = () => showCreateTicketModal();
        app.querySelector('#tk-project-settings').onclick = () => showProjectModal(currentProject);

        app.querySelector('#tk-f-assignee').onchange = (e) => {
            filterAssignee = e.target.value;
            renderBoard();
        };
        app.querySelector('#tk-f-priority').onchange = (e) => {
            filterPriority = e.target.value;
            renderBoard();
        };
        app.querySelector('#tk-f-search').oninput = (e) => {
            filterSearch = e.target.value;
            renderBoard();
        };
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
                        <label>${t('Kolumna')}</label>
                        <select id="tk-tf-column" class="tk-input">
                            ${columns.map((c, i) => `<option value="${_escHtml(c)}" ${i === 0 ? 'selected' : ''}>${_escHtml(c)}</option>`).join('')}
                        </select>
                    </div>
                    <div class="tk-form-group">
                        <label>${t('Priorytet')}</label>
                        <select id="tk-tf-priority" class="tk-input">
                            ${Object.entries(PRIORITY_LABELS).map(([k, v]) =>
                                `<option value="${k}" ${k === 'medium' ? 'selected' : ''}>${PRIORITY_ICONS[k]} ${_escHtml(v)}</option>`
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
                    <div class="tk-form-group">
                        <label>${t('Etykiety')} <small>(${t('przecinek')})</small></label>
                        <input type="text" id="tk-tf-labels" class="tk-input" placeholder="bug, backend, urgent" />
                    </div>
                </div>
            </div>
        `;

        tkShowModal(t('Nowy ticket'), html, t('Utwórz'), (modal) => {
            const title = modal.querySelector('#tk-tf-title').value.trim();
            if (!title) { toast(t('Tytuł jest wymagany'), 'warning'); return false; }

            createTicket({
                title,
                description: modal.querySelector('#tk-tf-desc').value.trim(),
                column: modal.querySelector('#tk-tf-column').value,
                priority: modal.querySelector('#tk-tf-priority').value,
                assignee: modal.querySelector('#tk-tf-assignee').value,
                labels: modal.querySelector('#tk-tf-labels').value
                    .split(',').map(s => s.trim()).filter(Boolean),
            });
        });
    }

    /* ═══════════════════ TICKET DETAIL MODAL ═══════════════════ */

    async function showTicketDetail(ticket) {
        const columns = currentProject.columns || DEFAULT_COLUMNS;
        const members = currentProject.members || [];
        const ticketComments = await loadComments(ticket.id);

        const labels = (ticket.labels || []);
        const labelsHTML = labels.map(l =>
            `<span class="tk-label tk-label-removable" style="background:${tkLabelColor(l)};" data-label="${_escHtml(l)}">
                ${_escHtml(l)} <i class="fas fa-times tk-remove-label"></i>
            </span>`
        ).join('');

        const commentsHTML = ticketComments.map(c => `
            <div class="tk-comment">
                <div class="tk-comment-header">
                    <strong>${_escHtml(c.author || c.user || 'unknown')}</strong>
                    <span class="tk-comment-date">${c.created_at ? new Date(c.created_at).toLocaleString() : ''}</span>
                </div>
                <div class="tk-comment-body">${_escHtml(c.text || c.body || '')}</div>
            </div>
        `).join('');

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
                        <label>${t('Kolumna')}</label>
                        <select id="tk-df-column" class="tk-input">
                            ${columns.map(c => `<option value="${_escHtml(c)}" ${c === ticket.column ? 'selected' : ''}>${_escHtml(c)}</option>`).join('')}
                        </select>
                    </div>
                    <div class="tk-form-group">
                        <label>${t('Priorytet')}</label>
                        <select id="tk-df-priority" class="tk-input">
                            ${Object.entries(PRIORITY_LABELS).map(([k, v]) =>
                                `<option value="${k}" ${k === ticket.priority ? 'selected' : ''}>${PRIORITY_ICONS[k]} ${_escHtml(v)}</option>`
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
                    ${ticket.created_at ? '<div>' + t('Utworzony') + ': ' + new Date(ticket.created_at).toLocaleString() + '</div>' : ''}
                    ${ticket.updated_at ? '<div>' + t('Zaktualizowany') + ': ' + new Date(ticket.updated_at).toLocaleString() + '</div>' : ''}
                </div>

                <div class="tk-comments-section" style="margin-top:16px;padding-top:12px;border-top:1px solid rgba(255,255,255,0.1);">
                    <label>${t('Komentarze')} (${ticketComments.length})</label>
                    <div id="tk-df-comments" class="tk-comments-list">${commentsHTML || '<div class="tk-empty-col">' + t('Brak komentarzy') + '</div>'}</div>
                    <textarea id="tk-df-new-comment" class="tk-input" rows="2" style="margin-top:8px;" placeholder="${t('Dodaj komentarz...')}"></textarea>
                    <button class="tk-btn tk-btn-small" id="tk-df-send-comment" style="margin-top:4px;">
                        <i class="fas fa-paper-plane"></i> ${t('Wyślij')}
                    </button>
                </div>
            </div>
        `;

        const overlay = tkShowModal(
            PRIORITY_ICONS[ticket.priority] + ' ' + _escHtml(ticket.title),
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
                    column: modal.querySelector('#tk-df-column').value,
                    priority: modal.querySelector('#tk-df-priority').value,
                    assignee: modal.querySelector('#tk-df-assignee').value,
                    labels: updatedLabels,
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

        /* ── comments ── */
        overlay.querySelector('#tk-df-send-comment').onclick = async () => {
            const textarea = overlay.querySelector('#tk-df-new-comment');
            const text = textarea.value.trim();
            if (!text) return;

            const ok = await addComment(ticket.id, text);
            if (ok) {
                textarea.value = '';
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
                toast(t('Komentarz dodany'), 'success');
            }
        };
    }

    /* ═══════════════════ INIT ═══════════════════ */

    await showProjectList();
}
