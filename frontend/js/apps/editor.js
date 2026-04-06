/**
 * EthOS — Edytor dokumentów
 * Web-based document editor with DOCX/PDF support
 */

AppRegistry['doc-editor'] = function (appDef, launchOpts) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('doc-editor', level, msg, details) : console.log('[doc-editor]', msg, details || '');

    const winId = 'doc-editor';
    // Close existing if re-opening with a file
    if (WM.windows.has(winId) && launchOpts?.path) {
        closeWindow(winId);
    }

    createWindow(winId, {
        title: t('Edytor dokumentów'),
        icon: appDef?.icon || 'fa-file-word',
        iconColor: appDef?.color || '#2563eb',
        width: 960,
        height: 680,
        minWidth: 600,
        minHeight: 400,
        onRender: (body) => renderDocEditor(body, launchOpts),
    });
};

function renderDocEditor(body, launchOpts) {
    // State
    let currentPath = launchOpts?.path || null;
    let currentFilename = launchOpts?.filename || 'Nowy dokument.docx';
    let modified = false;
    let currentDir = launchOpts?.dir || null;

    // If we have a path, extract directory
    if (currentPath) {
        currentFilename = currentPath.split('/').pop();
        currentDir = currentPath.substring(0, currentPath.lastIndexOf('/'));
    }

    body.innerHTML = `
        <div class="doceditor">
            <div class="doceditor-toolbar">
                <div class="doceditor-toolbar-group doceditor-file-actions">
                    <button class="dte-btn" data-action="new" title="Nowy dokument">
                        <i class="fas fa-file-medical"></i>
                    </button>
                    <button class="dte-btn" data-action="open" title="${t('Otwórz plik')}">
                        <i class="fas fa-folder-open"></i>
                    </button>
                    <button class="dte-btn" data-action="save" title="Zapisz (Ctrl+S)">
                        <i class="fas fa-save"></i>
                    </button>
                    <div class="dte-dropdown">
                        <button class="dte-btn" data-action="save-as" title="Zapisz jako...">
                            <i class="fas fa-file-export"></i> <i class="fas fa-caret-down" style="font-size:9px"></i>
                        </button>
                        <div class="dte-dropdown-menu">
                            <button data-saveas="docx"><i class="fas fa-file-word"></i> Zapisz jako DOCX</button>
                            <button data-saveas="pdf"><i class="fas fa-file-pdf"></i> Eksportuj do PDF</button>
                            <button data-saveas="html"><i class="fas fa-file-code"></i> Zapisz jako HTML</button>
                        </div>
                    </div>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <button class="dte-btn" data-cmd="undo" title="Cofnij (Ctrl+Z)"><i class="fas fa-undo"></i></button>
                    <button class="dte-btn" data-cmd="redo" title="${t('Ponów (Ctrl+Y)')}"><i class="fas fa-redo"></i></button>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <select class="dte-select dte-font-family" title="Czcionka">
                        <option value="Calibri">Calibri</option>
                        <option value="Arial">Arial</option>
                        <option value="Times New Roman">Times New Roman</option>
                        <option value="Verdana">Verdana</option>
                        <option value="Georgia">Georgia</option>
                        <option value="Courier New">Courier New</option>
                        <option value="Tahoma">Tahoma</option>
                        <option value="Trebuchet MS">Trebuchet MS</option>
                    </select>
                    <select class="dte-select dte-font-size" title="Rozmiar">
                        <option value="1">8</option>
                        <option value="2">10</option>
                        <option value="3" selected>12</option>
                        <option value="4">14</option>
                        <option value="5">18</option>
                        <option value="6">24</option>
                        <option value="7">36</option>
                    </select>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <button class="dte-btn" data-cmd="bold" title="Pogrubienie (Ctrl+B)"><i class="fas fa-bold"></i></button>
                    <button class="dte-btn" data-cmd="italic" title="Kursywa (Ctrl+I)"><i class="fas fa-italic"></i></button>
                    <button class="dte-btn" data-cmd="underline" title="${t('Podkreślenie (Ctrl+U)')}"><i class="fas fa-underline"></i></button>
                    <button class="dte-btn" data-cmd="strikeThrough" title="${t('Przekreślenie')}"><i class="fas fa-strikethrough"></i></button>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <input type="color" class="dte-color" id="dte-fg-color" value="#000000" title="Kolor tekstu">
                    <input type="color" class="dte-color" id="dte-bg-color" value="#ffff00" title="${t('Podświetlenie')}">
                    <button class="dte-btn" data-action="highlight" title="${t('Podświetl zaznaczenie')}">
                        <i class="fas fa-highlighter"></i>
                    </button>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <button class="dte-btn" data-cmd="justifyLeft" title="Do lewej"><i class="fas fa-align-left"></i></button>
                    <button class="dte-btn" data-cmd="justifyCenter" title="${t('Wyśrodkuj')}"><i class="fas fa-align-center"></i></button>
                    <button class="dte-btn" data-cmd="justifyRight" title="Do prawej"><i class="fas fa-align-right"></i></button>
                    <button class="dte-btn" data-cmd="justifyFull" title="Wyjustuj"><i class="fas fa-align-justify"></i></button>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <button class="dte-btn" data-cmd="insertUnorderedList" title="Lista punktowana"><i class="fas fa-list-ul"></i></button>
                    <button class="dte-btn" data-cmd="insertOrderedList" title="Lista numerowana"><i class="fas fa-list-ol"></i></button>
                    <button class="dte-btn" data-cmd="outdent" title="${t('Zmniejsz wcięcie')}"><i class="fas fa-outdent"></i></button>
                    <button class="dte-btn" data-cmd="indent" title="${t('Zwiększ wcięcie')}"><i class="fas fa-indent"></i></button>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <select class="dte-select dte-heading" title="${t('Nagłówek')}">
                        <option value="">Normalny</option>
                        <option value="H1">${t('Nagłówek 1')}</option>
                        <option value="H2">${t('Nagłówek 2')}</option>
                        <option value="H3">${t('Nagłówek 3')}</option>
                    </select>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <button class="dte-btn" data-action="link" title="Wstaw link"><i class="fas fa-link"></i></button>
                    <button class="dte-btn" data-action="image" title="Wstaw obraz"><i class="fas fa-image"></i></button>
                    <button class="dte-btn" data-action="table" title="${t('Wstaw tabelę')}"><i class="fas fa-table"></i></button>
                    <button class="dte-btn" data-action="hr" title="${t('Wstaw linię poziomą')}"><i class="fas fa-minus"></i></button>
                    <button class="dte-btn" data-action="blockquote" title="Cytat"><i class="fas fa-quote-right"></i></button>
                </div>
                <div class="dte-separator"></div>
                <div class="doceditor-toolbar-group">
                    <button class="dte-btn" data-cmd="removeFormat" title="${t('Wyczyść formatowanie')}"><i class="fas fa-eraser"></i></button>
                </div>
            </div>
            <div class="doceditor-page-wrap">
                <div class="doceditor-page" contenteditable="true" id="dte-editor" spellcheck="true">
                    <p><br></p>
                </div>
            </div>
            <div class="doceditor-statusbar">
                <span class="dte-filename" id="dte-status-filename">${_escHtml(currentFilename)}</span>
                <span class="dte-modified" id="dte-status-mod" style="display:none">● Zmodyfikowany</span>
                <span class="dte-spacer"></span>
                <span class="dte-wordcount" id="dte-status-words">0 ${t('słów')}</span>
            </div>
        </div>
    `;

    const editor = body.querySelector('#dte-editor');
    const statusFilename = body.querySelector('#dte-status-filename');
    const statusMod = body.querySelector('#dte-status-mod');
    const statusWords = body.querySelector('#dte-status-words');

    // ─── execCommand helpers ───
    function exec(cmd, val) {
        editor.focus();
        document.execCommand(cmd, false, val || null);
        updateToolbarState();
    }

    // Toolbar command buttons
    body.querySelectorAll('[data-cmd]').forEach(btn => {
        btn.addEventListener('mousedown', e => e.preventDefault());
        btn.addEventListener('click', () => exec(btn.dataset.cmd));
    });

    // Font family
    const fontFamily = body.querySelector('.dte-font-family');
    fontFamily.addEventListener('change', () => {
        exec('fontName', fontFamily.value);
    });

    // Font size
    const fontSize = body.querySelector('.dte-font-size');
    fontSize.addEventListener('change', () => {
        exec('fontSize', fontSize.value);
    });

    // Heading selector
    const heading = body.querySelector('.dte-heading');
    heading.addEventListener('change', () => {
        if (heading.value) {
            exec('formatBlock', heading.value);
        } else {
            exec('formatBlock', 'P');
        }
    });

    // Text color
    const fgColor = body.querySelector('#dte-fg-color');
    fgColor.addEventListener('input', () => {
        exec('foreColor', fgColor.value);
    });

    // Highlight
    const bgColor = body.querySelector('#dte-bg-color');
    body.querySelector('[data-action="highlight"]').addEventListener('click', () => {
        exec('hiliteColor', bgColor.value);
    });

    // Insert link
    body.querySelector('[data-action="link"]').addEventListener('click', () => {
        promptDialog('Wstaw link', 'Podaj URL:', 'https://').then(url => {
            if (url) exec('createLink', url);
        });
    });

    // Insert image (URL)
    body.querySelector('[data-action="image"]').addEventListener('click', () => {
        promptDialog('Wstaw obraz', 'Podaj URL obrazu:', 'https://').then(url => {
            if (url) exec('insertImage', url);
        });
    });

    // Insert table
    body.querySelector('[data-action="table"]').addEventListener('click', () => {
        promptDialog(t('Wstaw tabelę'), 'Rozmiar (np. 3x3):', '3x3').then(size => {
            if (!size) return;
            const [cols, rows] = size.split('x').map(Number);
            if (!cols || !rows || cols > 20 || rows > 50) return;
            let html = '<table><tbody>';
            for (let r = 0; r < rows; r++) {
                html += '<tr>';
                for (let c = 0; c < cols; c++) {
                    html += '<td>&nbsp;</td>';
                }
                html += '</tr>';
            }
            html += '</tbody></table><p><br></p>';
            exec('insertHTML', html);
        });
    });

    // Horizontal rule
    body.querySelector('[data-action="hr"]').addEventListener('click', () => {
        exec('insertHTML', '<hr><p><br></p>');
    });

    // Blockquote
    body.querySelector('[data-action="blockquote"]').addEventListener('click', () => {
        exec('formatBlock', 'BLOCKQUOTE');
    });

    // Dropdown toggle
    body.querySelectorAll('.dte-dropdown').forEach(dd => {
        const btn = dd.querySelector('.dte-btn');
        const menu = dd.querySelector('.dte-dropdown-menu');
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const open = menu.classList.contains('show');
            body.querySelectorAll('.dte-dropdown-menu.show').forEach(m => m.classList.remove('show'));
            if (!open) menu.classList.add('show');
        });
    });
    document.addEventListener('click', () => {
        body.querySelectorAll('.dte-dropdown-menu.show').forEach(m => m.classList.remove('show'));
    });

    // Save-as menu items
    body.querySelectorAll('[data-saveas]').forEach(btn => {
        btn.addEventListener('click', () => {
            const fmt = btn.dataset.saveas;
            saveAs(fmt);
        });
    });

    // ─── Toolbar state update ───
    function updateToolbarState() {
        body.querySelectorAll('[data-cmd]').forEach(btn => {
            const cmd = btn.dataset.cmd;
            if (['bold', 'italic', 'underline', 'strikeThrough',
                 'justifyLeft', 'justifyCenter', 'justifyRight', 'justifyFull',
                 'insertUnorderedList', 'insertOrderedList'].includes(cmd)) {
                btn.classList.toggle('active', document.queryCommandState(cmd));
            }
        });
    }

    editor.addEventListener('mouseup', updateToolbarState);
    editor.addEventListener('keyup', updateToolbarState);

    // ─── Word count ───
    function updateWordCount() {
        const text = editor.innerText || '';
        const words = text.trim().split(/\s+/).filter(w => w.length > 0).length;
        const chars = text.length;
        statusWords.textContent = `${words} ${t('słów')} · ${chars} ${t('znaków')}`;
    }

    // ─── Modification tracking ───
    editor.addEventListener('input', () => {
        if (!modified) {
            modified = true;
            statusMod.style.display = '';
            // Update window title
            const titleEl = document.querySelector('#win-doc-editor .window-title span');
            if (titleEl && !titleEl.textContent.startsWith('● ')) {
                titleEl.textContent = '● ' + titleEl.textContent;
            }
        }
        updateWordCount();
    });

    function markSaved() {
        modified = false;
        statusMod.style.display = 'none';
        const titleEl = document.querySelector('#win-doc-editor .window-title span');
        if (titleEl) titleEl.textContent = titleEl.textContent.replace(/^● /, '');
    }

    // ─── File actions ───

    // New
    body.querySelector('[data-action="new"]').addEventListener('click', async () => {
        if (modified) {
            const ok = await confirmDialog('Nowy dokument', t('Niezapisane zmiany zostaną utracone. Kontynuować?'));
            if (!ok) return;
        }
        editor.innerHTML = '<p><br></p>';
        currentPath = null;
        currentFilename = 'Nowy dokument.docx';
        currentDir = null;
        statusFilename.textContent = currentFilename;
        markSaved();
        updateWordCount();
    });

    // Open
    body.querySelector('[data-action="open"]').addEventListener('click', () => {
        openFilePicker();
    });

    // Save (overwrite)
    body.querySelector('[data-action="save"]').addEventListener('click', () => {
        saveFile();
    });

    // Ctrl+S
    editor.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            saveFile();
        }
    });

    // ─── Save logic ───
    async function saveFile() {
        if (!currentPath) {
            // No file open — save as new DOCX
            saveAs('docx');
            return;
        }

        const html = editor.innerHTML;
        toast('Zapisywanie...', 'info');

        const res = await api('/editor/save', {
            method: 'POST',
            body: { html, path: currentPath }
        });

        if (res.ok) {
            toast(t('Zapisano pomyślnie'), 'success');
            markSaved();
        } else {
            toast(res.error || t('Błąd zapisu'), 'error');
        }
    }

    async function saveAs(format) {
        const html = editor.innerHTML;
        const baseName = currentFilename.replace(/\.[^.]+$/, '');

        let defaultName;
        if (format === 'docx') defaultName = baseName + '.docx';
        else if (format === 'pdf') defaultName = baseName + '.pdf';
        else if (format === 'html') defaultName = baseName + '.html';

        // Show folder picker with filename input
        let browsePath = currentDir || '/home';
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:520px;">
                <div class="modal-header">
                    <span>Zapisz jako ${format.toUpperCase()}</span>
                    <button class="modal-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="modal-body" style="padding:0;">
                    <div style="padding:10px 16px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;">
                        <button class="dte-btn dte-saveas-up" title="${t('W górę')}"><i class="fas fa-arrow-up"></i></button>
                        <span class="dte-saveas-path" style="font-size:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></span>
                    </div>
                    <div class="dte-saveas-list" style="height:250px;overflow-y:auto;padding:4px 0;"></div>
                    <div style="padding:10px 16px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center;">
                        <label style="font-size:12px;color:var(--text-muted);white-space:nowrap;">Nazwa:</label>
                        <input type="text" class="dte-saveas-name" value="${_escHtml(defaultName)}" 
                               style="flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:var(--r-sm);background:var(--bg-primary);color:var(--text-primary);font-size:13px;outline:none;">
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary dte-saveas-cancel">Anuluj</button>
                    <button class="btn btn-primary dte-saveas-confirm">Zapisz</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const pathEl = overlay.querySelector('.dte-saveas-path');
        const listEl = overlay.querySelector('.dte-saveas-list');
        const nameInput = overlay.querySelector('.dte-saveas-name');
        const closeOverlay = () => overlay.remove();

        overlay.querySelector('.modal-close').addEventListener('click', closeOverlay);
        overlay.querySelector('.dte-saveas-cancel').addEventListener('click', closeOverlay);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeOverlay(); });

        overlay.querySelector('.dte-saveas-up').addEventListener('click', () => {
            const parent = browsePath.substring(0, browsePath.lastIndexOf('/')) || '/';
            loadSaveDir(parent);
        });

        overlay.querySelector('.dte-saveas-confirm').addEventListener('click', async () => {
            const filename = nameInput.value.trim();
            if (!filename) { toast(t('Podaj nazwę pliku'), 'warning'); return; }
            closeOverlay();

            const savePath = browsePath;
            toast('Zapisywanie...', 'info');

            let endpoint, payload;
            if (format === 'docx') {
                endpoint = '/editor/save-docx';
                payload = { html, path: savePath, filename };
            } else if (format === 'pdf') {
                endpoint = '/editor/save-pdf';
                payload = { html, path: savePath, filename };
            } else if (format === 'html') {
                const blob = new Blob([`<!DOCTYPE html><html><head><meta charset="utf-8"><title>${_escHtml(filename)}</title></head><body>${html}</body></html>`], { type: 'text/html' });
                const formData = new FormData();
                formData.append('file', blob, filename);
                formData.append('path', savePath);

                const resp = await fetch('/api/files/upload', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${NAS.token}`, 'X-CSRFToken': NAS.csrfToken },
                    body: formData
                });
                const data = await resp.json();
                if (data.ok || data.uploaded) {
                    toast(`Zapisano: ${filename}`, 'success');
                    currentFilename = filename;
                    currentDir = savePath;
                    statusFilename.textContent = filename;
                    markSaved();
                } else {
                    toast(data.error || t('Błąd zapisu'), 'error');
                }
                return;
            }

            const res = await api(endpoint, {
                method: 'POST',
                body: payload
            });

            if (res.ok) {
                toast(`Zapisano: ${filename}`, 'success');
                if (format === 'docx') {
                    currentPath = res.saved_path;
                    currentFilename = filename;
                    currentDir = currentPath.substring(0, currentPath.lastIndexOf('/'));
                    statusFilename.textContent = filename;
                    markSaved();
                }
            } else {
                toast(res.error || t('Błąd zapisu'), 'error');
            }
        });

        async function loadSaveDir(path) {
            browsePath = path;
            pathEl.textContent = path;
            listEl.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>';

            const data = await api(`/files/list?path=${encodeURIComponent(path)}`);
            if (!data || data.error || data.locked) {
                listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak dostępu')}</div>`;
                return;
            }

            const items = data.items || [];
            const dirs = items.filter(i => i.is_dir).sort((a, b) => a.name.localeCompare(b.name));

            listEl.innerHTML = '';
            if (!dirs.length) {
                listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak podfolderów')}</div>`;
            }
            dirs.forEach(d => {
                const row = document.createElement('div');
                row.className = 'dte-browse-item';
                row.innerHTML = `<i class="fas fa-folder" style="color:#f59e0b"></i> ${_escHtml(d.name)}`;
                row.addEventListener('click', () => loadSaveDir(path + (path === '/' ? '' : '/') + d.name));
                listEl.appendChild(row);
            });
        }

        loadSaveDir(browsePath);
        setTimeout(() => nameInput.focus(), 100);
    }

    // ─── Open file picker ───
    async function openFilePicker() {
        if (modified) {
            const ok = await confirmDialog(t('Otwórz plik'), t('Niezapisane zmiany zostaną utracone. Kontynuować?'));
            if (!ok) return;
        }

        // Show a simple file browser dialog
        let browsePath = currentDir || '/home';

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:500px;">
                <div class="modal-header">
                    <span>${t('Otwórz dokument')}</span>
                    <button class="modal-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="modal-body" style="padding:0;">
                    <div style="padding:10px 16px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;">
                        <button class="dte-btn dte-browse-up" title="${t('W górę')}"><i class="fas fa-arrow-up"></i></button>
                        <span class="dte-browse-path" style="font-size:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></span>
                    </div>
                    <div class="dte-browse-list" style="height:300px;overflow-y:auto;padding:4px 0;"></div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary dte-browse-cancel">Anuluj</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const pathEl = overlay.querySelector('.dte-browse-path');
        const listEl = overlay.querySelector('.dte-browse-list');
        const closeOverlay = () => overlay.remove();

        overlay.querySelector('.modal-close').addEventListener('click', closeOverlay);
        overlay.querySelector('.dte-browse-cancel').addEventListener('click', closeOverlay);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeOverlay(); });

        overlay.querySelector('.dte-browse-up').addEventListener('click', () => {
            const parent = browsePath.substring(0, browsePath.lastIndexOf('/')) || '/';
            loadDir(parent);
        });

        const docExts = ['.docx', '.txt', '.html', '.htm'];

        async function loadDir(path) {
            browsePath = path;
            pathEl.textContent = path;
            listEl.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>';

            const data = await api(`/files/list?path=${encodeURIComponent(path)}`);
            if (!data || data.error || data.locked) {
                listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak dostępu')}</div>`;
                return;
            }

            const items = data.items || [];
            const dirs = items.filter(i => i.is_dir).sort((a, b) => a.name.localeCompare(b.name));
            const files = items.filter(i => !i.is_dir && docExts.some(e => i.name.toLowerCase().endsWith(e)))
                .sort((a, b) => a.name.localeCompare(b.name));

            if (!dirs.length && !files.length) {
                listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak folderów lub obsługiwanych plików')}</div>`;
                return;
            }

            listEl.innerHTML = '';
            dirs.forEach(d => {
                const row = document.createElement('div');
                row.className = 'dte-browse-item';
                row.innerHTML = `<i class="fas fa-folder" style="color:#f59e0b"></i> ${_escHtml(d.name)}`;
                row.addEventListener('click', () => loadDir(path + '/' + d.name));
                listEl.appendChild(row);
            });
            files.forEach(f => {
                const ext = f.name.split('.').pop().toLowerCase();
                const icon = ext === 'docx' ? 'fa-file-word' : ext === 'pdf' ? 'fa-file-pdf' : 'fa-file-alt';
                const color = ext === 'docx' ? '#2563eb' : ext === 'pdf' ? '#ef4444' : '#64748b';
                const row = document.createElement('div');
                row.className = 'dte-browse-item';
                row.innerHTML = `<i class="fas ${icon}" style="color:${color}"></i> ${_escHtml(f.name)}`;
                row.addEventListener('click', () => {
                    closeOverlay();
                    openDocument(path + '/' + f.name);
                });
                listEl.appendChild(row);
            });
        }

        loadDir(browsePath);
    }

    // ─── Open a document ───
    async function openDocument(path) {
        toast('Otwieranie dokumentu...', 'info');

        const res = await api('/editor/open', {
            method: 'POST',
            body: { path }
        });

        if (res.ok) {
            editor.innerHTML = res.html || '<p><br></p>';
            currentPath = path;
            currentFilename = res.filename || path.split('/').pop();
            currentDir = path.substring(0, path.lastIndexOf('/'));
            statusFilename.textContent = currentFilename;
            markSaved();
            updateWordCount();
            toast(`Otwarto: ${currentFilename}`, 'success');
        } else {
            toast(res.error || t('Nie można otworzyć pliku'), 'error');
        }
    }

    // ─── If launched with a file, open it ───
    if (currentPath) {
        openDocument(currentPath);
    } else {
        // Set default font
        editor.focus();
        document.execCommand('fontName', false, 'Calibri');
        updateWordCount();
    }
}

// ─── Helper: escape HTML for template literals ───
function _escHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}
