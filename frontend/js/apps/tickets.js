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

const TICKET_TYPES = {
    task:    { icon: 'fa-check-square', color: '#4c9aff', label: 'Task' },
    bug:     { icon: 'fa-bug',          color: '#ef4444', label: 'Bug' },
    epic:    { icon: 'fa-bolt',         color: '#6554c0', label: 'Epic' },
    subtask: { icon: 'fa-minus-square', color: '#36b37e', label: 'Subtask' },
};

const COMPLEXITY_LEVELS = {
    simple:  { label: 'Prosty', color: '#22c55e' },
    medium:  { label: 'Średni', color: '#f59e0b' },
    complex: { label: 'Złożony', color: '#ef4444' },
};

const LABEL_COLORS = [
    '#3b82f6', '#ef4444', '#22c55e', '#f59e0b', '#8b5cf6',
    '#ec4899', '#06b6d4', '#f97316', '#14b8a6', '#6366f1',
];

const DEFAULT_COLUMNS = ['Backlog', 'Do zrobienia', 'W trakcie', 'Review', 'Gotowe'];

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
            const data = await api('/tickets/projects/' + projectId + '/tickets');
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

    /* ── Find 5 Bugs Button Helper ── */
    async function runAppAudit() {
        try {
            toast(t('Rozpoczynanie audytu...'), 'info');
            
            // Ensure we have the latest project list to find ETHOS
            if (!projects.length) await loadProjects();
            
            let targetProject = projects.find(p => p.name === 'ETHOS');
            if (!targetProject) {
                console.warn('Projekt ETHOS nie znaleziony, używam bieżącego');
                targetProject = currentProject;
                if (!targetProject) {
                     toast(t('Nie wybrano projektu'), 'error');
                     return;
                }
            }

            toast(t('Generowanie ticketów...'), 'info');
            const res = await api(`/tickets/projects/${targetProject.id}/bug-hunt`, { method: 'POST' });
            
            if (res.ok) {
                toast(t(`Utworzono ${res.count} ticketów dla aplikacji: ${res.app}`), 'success');
                // Refresh if we are viewing that project
                if (currentProject && currentProject.id === targetProject.id) {
                    await loadTickets(currentProject.id);
                    renderBoard();
                }
            }
        } catch (e) {
            console.error(e);
            toast(t('Błąd: ') + e.message, 'error');
        }
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
                    <button class="tk-act-btn" id="tk-find-bugs-btn" title="${t('Audyt aplikacji (Epic)')}">
                        <i class="fas fa-bug"></i>
                    </button>
                    ${currentProject.copilot_enabled ? '<button class="tk-act-btn" id="tk-watcher-btn" title="Ticket Watcher"><i class="fas fa-tower-broadcast"></i></button>' : ''}
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
                    <button class="tk-epic-toggle" title="${isCollapsed ? 'Rozwiń' : 'Zwiń'}">
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
                badge.innerHTML = '<i class="fas fa-robot"></i> ' + _escHtml(watcherExecuting.model_label || watcherExecuting.model || 'agent');
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
                        badge.innerHTML = '<i class="fas fa-robot"></i> ' + _escHtml(watcherExecuting.model_label || watcherExecuting.model || 'agent');
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
                badge.innerHTML = '<i class="fas fa-robot"></i> ' + _escHtml(watcherExecuting.model_label || watcherExecuting.model || 'agent');
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
                const watcher = (watcherExecuting && watcherExecuting.ticket_id === tk.id) ? (watcherExecuting.model||'') : '';
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

        /* ── fetch copilot logs list ── */
        let copilotLogs = [];
        try {
            const logsResp = await api('/tickets/tickets/' + ticket.id + '/copilot-logs');
            copilotLogs = (logsResp && logsResp.logs) || [];
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
                        s += ' — ' + _escHtml(watcherExecuting.model_label || watcherExecuting.model || '');
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
                            const label = (log.type === 'qa' ? '🔍 QA' : '🤖 Dev') + ' ' +
                                d.toLocaleString('pl', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' });
                            const sizeKB = (log.size / 1024).toFixed(1);
                            const modelTag = log.model ? ' <span class="tk-log-model">' + _escHtml(log.model) + '</span>' : '';
                            return '<button class="tk-log-tab' + (i === 0 ? ' active' : '') + '" data-filename="' +
                                _escHtml(log.filename) + '" data-idx="' + i + '">' + label + modelTag + ' <span class="tk-log-size">' + sizeKB + 'KB</span></button>';
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

        /* ── attachments logic ── */
        overlay.querySelector('#tk-df-upload-btn').onclick = () => {
            overlay.querySelector('#tk-df-upload-input').click();
        };

        function bindAttachmentEvents() {
             overlay.querySelectorAll('.tk-att-delete').forEach(btn => {
                btn.onclick = async (e) => {
                     e.stopPropagation(); 
                     if(!confirm(t('Usunąć plik?'))) return;
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
            const loadLog = async (filename) => {
                // stop previous polling
                if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
                _logOffset = 0;
                _logFilename = filename;
                logViewer.innerHTML = '<div class="tk-log-loading"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div>';
                try {
                    const resp = await api('/tickets/tickets/' + ticket.id + '/copilot-logs/' + encodeURIComponent(filename));
                    const content = (resp && resp.content) || '';
                    _logOffset = resp.offset || content.length;
                    logViewer.innerHTML = '<pre class="tk-log-content">' + _escHtml(content) + '</pre>';
                    logViewer.scrollTop = logViewer.scrollHeight;
                    // start polling for new content
                    _logPollTimer = setInterval(() => pollLogUpdates(filename), 3000);
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
                    const resp = await api('/tickets/tickets/' + ticket.id + '/copilot-logs/' + encodeURIComponent(filename) + '?offset=' + _logOffset);
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
                loadLog(tab.dataset.filename);
            });
            // auto-load first log
            const firstTab = logTabs.querySelector('.tk-log-tab');
            if (firstTab) loadLog(firstTab.dataset.filename);
        }
    }

    /* ═══════════════════ WATCHER MODAL ═══════════════════ */

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
