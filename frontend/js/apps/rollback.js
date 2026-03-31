/* ═══════════════════════════════════════════════════════════
   EthOS — Przywracanie systemu (Rollback)
   ${t('Snapshoty plików systemowych z opcją przywracania')}
   ═══════════════════════════════════════════════════════════ */

AppRegistry['rollback'] = function (appDef) {
    createWindow('rollback', {
        title: t('Przywracanie systemu'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 900,
        height: 620,
        singleton: true,
        onRender: (body) => renderRollbackApp(body),
    });
};

function renderRollbackApp(body) {
    const $ = (s) => body.querySelector(s);
    const $$ = (s) => body.querySelectorAll(s);

    let snapshots = [];
    let autoConfig = {};
    let storageUsed = 0;
    let busy = false;

    function formatSize(bytes) {
        if (!bytes || bytes <= 0) return '0 B';
        var units = ['B', 'KB', 'MB', 'GB'];
        var i = 0;
        var v = bytes;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return v.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
    }

    function formatDate(iso) {
        if (!iso) return '—';
        try {
            var d = new Date(iso);
            return d.toLocaleString('pl-PL', {
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit'
            });
        } catch (e) { return iso; }
    }

    // ── Layout ──
    body.innerHTML = `
    <div class="rollback-app">
        <div class="rollback-header">
            <div class="rollback-header-left">
                <h2 class="rollback-title"><i class="fas fa-history"></i> ${t('Snapshoty systemu')}</h2>
                <span class="rollback-storage" id="rb-storage"></span>
            </div>
            <div class="rollback-header-right">
                <button class="rollback-btn rollback-btn-settings" id="rb-settings-btn" title="${t('Ustawienia')}">
                    <i class="fas fa-cog"></i>
                </button>
                <button class="rollback-btn rollback-btn-primary" id="rb-create-btn">
                    <i class="fas fa-plus"></i> ${t('Nowy snapshot')}
                </button>
            </div>
        </div>

        <!-- Settings panel (hidden by default) -->
        <div class="rollback-settings hidden" id="rb-settings">
            <div class="rollback-settings-title"><i class="fas fa-cog"></i> ${t('Ustawienia automatyczne')}</div>
            <div class="rollback-settings-grid">
                <label class="rollback-setting-row">
                    <span>${t('Snapshot przed aktualizacją')}</span>
                    <label class="rollback-toggle">
                        <input type="checkbox" id="rb-auto-before-update">
                        <span class="rollback-toggle-slider"></span>
                    </label>
                </label>
                <label class="rollback-setting-row">
                    <span>${t('Codzienny snapshot')}</span>
                    <label class="rollback-toggle">
                        <input type="checkbox" id="rb-auto-daily">
                        <span class="rollback-toggle-slider"></span>
                    </label>
                </label>
                <div class="rollback-setting-row">
                    <span>${t('Maks. liczba snapshotów')}</span>
                    <input type="number" class="rollback-input rollback-input-sm" id="rb-auto-max" min="1" max="50" value="5">
                </div>
                <div class="rollback-setting-row">
                    <span></span>
                    <button class="rollback-btn rollback-btn-primary rollback-btn-sm" id="rb-auto-save">${t('Zapisz')}</button>
                </div>
            </div>
        </div>

        <!-- Snapshot list -->
        <div class="rollback-list" id="rb-list"></div>

        <!-- Empty state -->
        <div class="rollback-empty hidden" id="rb-empty">
            <i class="fas fa-box-open"></i>
            <p>${t('Brak snapshotów')}</p>
            <p class="rollback-empty-hint">${t('Utwórz pierwszy snapshot, aby zabezpieczyć system')}</p>
        </div>

        <!-- Busy overlay -->
        <div class="rollback-busy hidden" id="rb-busy">
            <div class="rollback-busy-inner">
                <i class="fas fa-spinner fa-spin"></i>
                <span id="rb-busy-text">${t('Operacja w toku…')}</span>
            </div>
        </div>
    </div>
    `;

    // ── Data loading ──
    async function loadSnapshots() {
        try {
            var r = await api('/rollback/snapshots');
            snapshots = r.snapshots || [];
            storageUsed = r.storage_used || 0;
            renderList();
            renderStorage();
        } catch (e) {
            NAS.toast(t('Błąd ładowania snapshotów'), 'error');
        }
    }

    async function loadAutoConfig() {
        try {
            autoConfig = await api('/rollback/auto');
            renderAutoConfig();
        } catch (e) {
            // silent
        }
    }

    // ── Rendering ──
    function renderStorage() {
        var el = $('#rb-storage');
        if (el) el.textContent = t('Zajętość') + ': ' + formatSize(storageUsed);
    }

    function renderList() {
        var listEl = $('#rb-list');
        var emptyEl = $('#rb-empty');
        if (!snapshots.length) {
            listEl.innerHTML = '';
            emptyEl.classList.remove('hidden');
            return;
        }
        emptyEl.classList.add('hidden');

        listEl.innerHTML = snapshots.map(function(s) {
            var isAuto = s.auto ? ' <span class="rollback-badge rollback-badge-auto">' + t('auto') + '</span>' : '';
            var desc = s.description ? '<div class="rollback-snap-desc">' + escapeHtml(s.description) + '</div>' : '';
            return '<div class="rollback-snap-card" data-id="' + s.id + '">'
                + '<div class="rollback-snap-info">'
                + '  <div class="rollback-snap-header">'
                + '    <span class="rollback-snap-id"><i class="fas fa-archive"></i> ' + escapeHtml(s.id) + '</span>'
                + isAuto
                + '  </div>'
                + desc
                + '  <div class="rollback-snap-meta">'
                + '    <span><i class="far fa-clock"></i> ' + formatDate(s.created_at) + '</span>'
                + '    <span><i class="fas fa-database"></i> ' + formatSize(s.size) + '</span>'
                + '  </div>'
                + '</div>'
                + '<div class="rollback-snap-actions">'
                + '  <button class="rollback-btn rollback-btn-restore" data-action="restore" data-id="' + s.id + '" title="' + t('Przywróć') + '">'
                + '    <i class="fas fa-undo"></i> ' + t('Przywróć')
                + '  </button>'
                + '  <button class="rollback-btn rollback-btn-danger" data-action="delete" data-id="' + s.id + '" title="' + t('Usuń') + '">'
                + '    <i class="fas fa-trash"></i>'
                + '  </button>'
                + '</div>'
                + '</div>';
        }).join('');

        // Bind action buttons
        listEl.querySelectorAll('[data-action]').forEach(function(btn) {
            btn.onclick = function(e) {
                e.stopPropagation();
                var action = this.getAttribute('data-action');
                var id = this.getAttribute('data-id');
                if (action === 'restore') confirmRestore(id);
                if (action === 'delete') confirmDelete(id);
            };
        });
    }

    function renderAutoConfig() {
        var chkUpdate = $('#rb-auto-before-update');
        var chkDaily = $('#rb-auto-daily');
        var inpMax = $('#rb-auto-max');
        if (chkUpdate) chkUpdate.checked = !!autoConfig.before_update;
        if (chkDaily) chkDaily.checked = !!autoConfig.daily;
        if (inpMax) inpMax.value = autoConfig.max_snapshots || 5;
    }

    function escapeHtml(str) {
        var d = document.createElement('div');
        d.textContent = str || '';
        return d.innerHTML;
    }

    function setBusy(on, text) {
        busy = on;
        var el = $('#rb-busy');
        if (!el) return;
        if (on) {
            $('#rb-busy-text').textContent = text || t('Operacja w toku…');
            el.classList.remove('hidden');
        } else {
            el.classList.add('hidden');
        }
    }

    // ── Actions ──
    async function createSnapshot() {
        var description = '';
        try {
            description = prompt(t('Opis snapshotu (opcjonalnie):'), '') || '';
        } catch (e) { /* no prompt support */ }

        setBusy(true, t('Tworzenie snapshotu…'));
        try {
            var r = await api('/rollback/snapshots', {
                method: 'POST',
                body: { description: description }
            });
            if (r.warning) NAS.toast(r.warning, 'warning');
            NAS.toast(t('Snapshot utworzony'), 'success');
            await loadSnapshots();
        } catch (e) {
            NAS.toast(t('Błąd') + ': ' + (e.message || e), 'error');
        } finally {
            setBusy(false);
        }
    }

    async function confirmRestore(id) {
        var snap = snapshots.find(function(s) { return s.id === id; });
        if (!snap) return;
        var msg = t('Czy na pewno chcesz przywrócić snapshot') + ' "' + id + '"?\n\n'
                + t('Obecne pliki systemowe zostaną nadpisane.') + '\n'
                + t('Automatycznie zostanie utworzona kopia bieżącego stanu.') + '\n\n'
                + t('System zostanie zrestartowany po przywróceniu.');
        if (!await confirmDialog(msg)) return;
        doRestore(id);
    }

    async function doRestore(id) {
        setBusy(true, t('Przywracanie snapshotu…'));
        try {
            var r = await api('/rollback/snapshots/' + encodeURIComponent(id) + '/restore', {
                method: 'POST'
            });
            NAS.toast(t('Snapshot przywrócony — system zostanie zrestartowany'), 'success');
        } catch (e) {
            NAS.toast(t('Błąd przywracania') + ': ' + (e.message || e), 'error');
        } finally {
            setBusy(false);
            await loadSnapshots();
        }
    }

    async function confirmDelete(id) {
        if (!await confirmDialog(t('Usunąć snapshot') + ' "' + id + '"?')) return;
        doDelete(id);
    }

    async function doDelete(id) {
        try {
            await api('/rollback/snapshots/' + encodeURIComponent(id), {
                method: 'DELETE'
            });
            NAS.toast(t('Snapshot usunięty'), 'success');
            await loadSnapshots();
        } catch (e) {
            NAS.toast(t('Błąd usuwania') + ': ' + (e.message || e), 'error');
        }
    }

    async function saveAutoConfig() {
        var cfg = {
            before_update: !!$('#rb-auto-before-update').checked,
            daily: !!$('#rb-auto-daily').checked,
            max_snapshots: parseInt($('#rb-auto-max').value, 10) || 5,
        };
        try {
            autoConfig = await api('/rollback/auto', {
                method: 'PUT',
                body: cfg
            });
            NAS.toast(t('Ustawienia zapisane'), 'success');
            await loadSnapshots();
        } catch (e) {
            NAS.toast(t('Błąd zapisu ustawień'), 'error');
        }
    }

    // ── Event binding ──
    $('#rb-create-btn').onclick = function() {
        if (!busy) createSnapshot();
    };

    $('#rb-settings-btn').onclick = function() {
        var panel = $('#rb-settings');
        panel.classList.toggle('hidden');
    };

    $('#rb-auto-save').onclick = function() {
        saveAutoConfig();
    };

    // ── Init ──
    loadSnapshots();
    loadAutoConfig();
}
