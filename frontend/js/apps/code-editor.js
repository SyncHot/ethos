/**
 * EthOS — Edytor kodu
 * Simple code editor with syntax highlighting, line numbers, and formatting preservation
 */

AppRegistry['code-editor'] = function (appDef, launchOpts) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('code-editor', level, msg, details) : console.log('[code-editor]', msg, details || '');

    const winId = 'code-editor';
    if (WM.windows.has(winId) && launchOpts?.path) {
        closeWindow(winId);
    }

    createWindow(winId, {
        title: t('Edytor kodu'),
        icon: appDef?.icon || 'fa-code',
        iconColor: appDef?.color || '#22d3ee',
        width: 960,
        height: 680,
        minWidth: 600,
        minHeight: 400,
        onRender: (body) => renderCodeEditor(body, launchOpts),
    });
};

function renderCodeEditor(body, launchOpts) {
    let currentPath = launchOpts?.path || null;
    let currentFilename = launchOpts?.filename || 'nowy_plik.txt';
    let modified = false;
    let currentDir = launchOpts?.dir || null;

    if (currentPath) {
        currentFilename = currentPath.split('/').pop();
        currentDir = currentPath.substring(0, currentPath.lastIndexOf('/'));
    }

    body.innerHTML = `
        <div class="code-editor">
            <div class="ce-toolbar">
                <div class="ce-toolbar-group">
                    <button class="ce-btn" data-action="new" title="Nowy plik"><i class="fas fa-file-medical"></i></button>
                    <button class="ce-btn" data-action="open" title="${t('Otwórz plik')}"><i class="fas fa-folder-open"></i></button>
                    <button class="ce-btn" data-action="save" title="Zapisz (Ctrl+S)"><i class="fas fa-save"></i></button>
                    <button class="ce-btn" data-action="save-as" title="Zapisz jako..."><i class="fas fa-file-export"></i></button>
                </div>
                <div class="ce-sep"></div>
                <div class="ce-toolbar-group">
                    <button class="ce-btn" data-action="undo" title="Cofnij (Ctrl+Z)"><i class="fas fa-undo"></i></button>
                    <button class="ce-btn" data-action="redo" title="${t('Ponów (Ctrl+Y)')}"><i class="fas fa-redo"></i></button>
                </div>
                <div class="ce-sep"></div>
                <div class="ce-toolbar-group">
                    <button class="ce-btn" data-action="find" title="${t('Znajdź (Ctrl+F)')}"><i class="fas fa-search"></i></button>
                    <button class="ce-btn" data-action="replace" title="${t('Zamień (Ctrl+H)')}"><i class="fas fa-exchange-alt"></i></button>
                </div>
                <div class="ce-sep"></div>
                <div class="ce-toolbar-group">
                    <select class="ce-select" id="ce-font-size" title="Rozmiar czcionki">
                        <option value="11">11px</option>
                        <option value="12">12px</option>
                        <option value="13" selected>13px</option>
                        <option value="14">14px</option>
                        <option value="16">16px</option>
                        <option value="18">18px</option>
                        <option value="20">20px</option>
                    </select>
                    <label class="ce-check" title="Zawijanie wierszy">
                        <input type="checkbox" id="ce-word-wrap"> <span>Zawijaj</span>
                    </label>
                    <label class="ce-check" title="Minimap">
                        <input type="checkbox" id="ce-minimap" checked> <span>Minimap</span>
                    </label>
                </div>
                <div style="flex:1"></div>
                <span class="ce-filename" id="ce-filename">${_ceEsc(currentFilename)}</span>
            </div>
            <div class="ce-find-bar" id="ce-find-bar" style="display:none;">
                <input type="text" class="ce-find-input" id="ce-find-input" placeholder="${t('Znajdź...')}">
                <input type="text" class="ce-find-input" id="ce-replace-input" placeholder="${t('Zamień na...')}" style="display:none;">
                <button class="ce-btn-sm" id="ce-find-prev" title="Poprzedni"><i class="fas fa-chevron-up"></i></button>
                <button class="ce-btn-sm" id="ce-find-next" title="${t('Następny')}"><i class="fas fa-chevron-down"></i></button>
                <button class="ce-btn-sm" id="ce-replace-one" title="${t('Zamień')}" style="display:none;">${t('Zamień')}</button>
                <button class="ce-btn-sm" id="ce-replace-all" title="${t('Zamień wszystko')}" style="display:none;">${t('Wszystko')}</button>
                <span class="ce-find-count" id="ce-find-count"></span>
                <button class="ce-btn-sm" id="ce-find-close" title="Zamknij"><i class="fas fa-times"></i></button>
            </div>
            <div class="ce-body">
                <div class="ce-gutter" id="ce-gutter"></div>
                <textarea class="ce-textarea" id="ce-textarea" spellcheck="false" autocomplete="off" autocorrect="off" autocapitalize="off"></textarea>
                <canvas class="ce-minimap" id="ce-minimap-canvas" width="80" height="400"></canvas>
            </div>
            <div class="ce-statusbar">
                <span id="ce-status-pos">Ln 1, Col 1</span>
                <span id="ce-status-lang">${t('Zwykły tekst')}</span>
                <span id="ce-status-encoding">UTF-8</span>
                <span id="ce-status-modified"></span>
            </div>
        </div>
    `;

    const textarea = body.querySelector('#ce-textarea');
    const gutter = body.querySelector('#ce-gutter');
    const statusPos = body.querySelector('#ce-status-pos');
    const statusLang = body.querySelector('#ce-status-lang');
    const statusMod = body.querySelector('#ce-status-modified');
    const filenameEl = body.querySelector('#ce-filename');
    const fontSizeSelect = body.querySelector('#ce-font-size');
    const wordWrapCheck = body.querySelector('#ce-word-wrap');
    const minimapCheck = body.querySelector('#ce-minimap');
    const minimapCanvas = body.querySelector('#ce-minimap-canvas');
    const findBar = body.querySelector('#ce-find-bar');
    const findInput = body.querySelector('#ce-find-input');
    const replaceInput = body.querySelector('#ce-replace-input');
    const findCountEl = body.querySelector('#ce-find-count');
    let findMode = 'find'; // 'find' or 'replace'

    // Detect language
    function detectLang(filename) {
        const ext = filename.split('.').pop().toLowerCase();
        const map = {
            'js': 'JavaScript', 'ts': 'TypeScript', 'py': 'Python', 'rb': 'Ruby',
            'java': 'Java', 'c': 'C', 'cpp': 'C++', 'h': 'C/C++ Header',
            'cs': 'C#', 'go': 'Go', 'rs': 'Rust', 'php': 'PHP',
            'html': 'HTML', 'htm': 'HTML', 'css': 'CSS', 'scss': 'SCSS', 'less': 'LESS',
            'json': 'JSON', 'xml': 'XML', 'yaml': 'YAML', 'yml': 'YAML',
            'md': 'Markdown', 'sql': 'SQL', 'sh': 'Shell', 'bash': 'Shell',
            'ps1': 'PowerShell', 'bat': 'Batch', 'cmd': 'Batch',
            'ini': 'INI', 'cfg': 'Config', 'conf': 'Config', 'toml': 'TOML',
            'dockerfile': 'Dockerfile', 'txt': t('Zwykły tekst'), 'log': 'Log',
            'csv': 'CSV', 'tsv': 'TSV', 'env': 'Environment',
            'lua': 'Lua', 'r': 'R', 'swift': 'Swift', 'kt': 'Kotlin',
            'dart': 'Dart', 'vue': 'Vue', 'svelte': 'Svelte', 'jsx': 'JSX', 'tsx': 'TSX',
        };
        return map[ext] || t('Zwykły tekst');
    }

    function updateLanguage() {
        statusLang.textContent = detectLang(currentFilename);
    }

    // ─── Line numbers ───
    function updateLineNumbers() {
        const lines = textarea.value.split('\n');
        const lineCount = lines.length;
        let html = '';
        for (let i = 1; i <= lineCount; i++) {
            html += i + '\n';
        }
        gutter.textContent = html;
        // Sync scroll
        gutter.scrollTop = textarea.scrollTop;
    }

    // ─── Minimap ───
    function renderMinimap() {
        if (!minimapCheck.checked) {
            minimapCanvas.style.display = 'none';
            return;
        }
        minimapCanvas.style.display = 'block';
        const ctx = minimapCanvas.getContext('2d');
        const rect = minimapCanvas.parentElement.getBoundingClientRect();
        minimapCanvas.height = rect.height;
        minimapCanvas.width = 80;
        ctx.clearRect(0, 0, 80, minimapCanvas.height);

        const text = textarea.value;
        const lines = text.split('\n');
        const scale = Math.min(1, minimapCanvas.height / (lines.length * 3 + 10));
        const lineHeight = 3 * scale;

        ctx.fillStyle = 'rgba(255,255,255,0.03)';
        ctx.fillRect(0, 0, 80, minimapCanvas.height);

        // Draw visible region indicator
        const totalLines = lines.length;
        const scrollRatio = textarea.scrollTop / (textarea.scrollHeight - textarea.clientHeight || 1);
        const visibleLines = Math.ceil(textarea.clientHeight / 18); // approx 18px per line
        const viewStart = scrollRatio * totalLines;
        const viewY = (viewStart / totalLines) * minimapCanvas.height;
        const viewH = (visibleLines / totalLines) * minimapCanvas.height;
        ctx.fillStyle = 'rgba(100,180,255,0.12)';
        ctx.fillRect(0, viewY, 80, Math.max(viewH, 10));

        // Draw lines
        ctx.fillStyle = 'rgba(200,200,200,0.3)';
        for (let i = 0; i < lines.length && i * lineHeight < minimapCanvas.height; i++) {
            const len = Math.min(lines[i].length, 60);
            if (len > 0) {
                ctx.fillRect(4, i * lineHeight, len * 1.1, Math.max(lineHeight - 1, 1));
            }
        }
    }

    // Minimap click to scroll
    minimapCanvas.addEventListener('click', (e) => {
        const rect = minimapCanvas.getBoundingClientRect();
        const ratio = (e.clientY - rect.top) / rect.height;
        textarea.scrollTop = ratio * (textarea.scrollHeight - textarea.clientHeight);
    });

    // ─── Modified state ───
    function markModified() {
        if (!modified) {
            modified = true;
            statusMod.textContent = '● Zmodyfikowany';
            filenameEl.textContent = '● ' + currentFilename;
        }
    }
    function markSaved() {
        modified = false;
        statusMod.textContent = '';
        filenameEl.textContent = currentFilename;
    }

    // ─── Cursor position ───
    function updateCursorPos() {
        const val = textarea.value;
        const pos = textarea.selectionStart;
        const before = val.substring(0, pos);
        const line = before.split('\n').length;
        const col = pos - before.lastIndexOf('\n');
        statusPos.textContent = `Ln ${line}, Col ${col}`;
    }

    // ─── Tab handling to preserve formatting ───
    textarea.addEventListener('keydown', (e) => {
        if (e.key === 'Tab') {
            e.preventDefault();
            const start = textarea.selectionStart;
            const end = textarea.selectionEnd;
            const val = textarea.value;

            if (start === end) {
                // Insert tab (4 spaces)
                textarea.value = val.substring(0, start) + '    ' + val.substring(end);
                textarea.selectionStart = textarea.selectionEnd = start + 4;
            } else {
                // Indent/unindent selection
                const lineStart = val.lastIndexOf('\n', start - 1) + 1;
                const selectedText = val.substring(lineStart, end);

                if (e.shiftKey) {
                    // Unindent
                    const unindented = selectedText.replace(/^(    | {1,3}|\t)/gm, '');
                    textarea.value = val.substring(0, lineStart) + unindented + val.substring(end);
                    textarea.selectionStart = lineStart;
                    textarea.selectionEnd = lineStart + unindented.length;
                } else {
                    // Indent
                    const indented = selectedText.replace(/^/gm, '    ');
                    textarea.value = val.substring(0, lineStart) + indented + val.substring(end);
                    textarea.selectionStart = lineStart;
                    textarea.selectionEnd = lineStart + indented.length;
                }
            }
            markModified();
            updateLineNumbers();
            renderMinimap();
        }

        // Auto-close brackets
        const pairs = { '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`' };
        if (pairs[e.key]) {
            const start = textarea.selectionStart;
            const end = textarea.selectionEnd;
            if (start !== end) {
                // Wrap selection
                e.preventDefault();
                const val = textarea.value;
                const selected = val.substring(start, end);
                textarea.value = val.substring(0, start) + e.key + selected + pairs[e.key] + val.substring(end);
                textarea.selectionStart = start + 1;
                textarea.selectionEnd = end + 1;
                markModified();
                updateLineNumbers();
            }
        }

        // Enter — auto-indent
        if (e.key === 'Enter') {
            e.preventDefault();
            const val = textarea.value;
            const pos = textarea.selectionStart;
            const lineStart = val.lastIndexOf('\n', pos - 1) + 1;
            const currentLine = val.substring(lineStart, pos);
            const indent = currentLine.match(/^(\s*)/)[1];
            // Add extra indent after { or :
            const lastChar = currentLine.trimEnd().slice(-1);
            const extraIndent = (lastChar === '{' || lastChar === ':' || lastChar === '(') ? '    ' : '';
            const insert = '\n' + indent + extraIndent;
            textarea.value = val.substring(0, pos) + insert + val.substring(textarea.selectionEnd);
            textarea.selectionStart = textarea.selectionEnd = pos + insert.length;
            markModified();
            updateLineNumbers();
            renderMinimap();
        }
    });

    textarea.addEventListener('input', () => {
        markModified();
        updateLineNumbers();
        renderMinimap();
    });

    textarea.addEventListener('scroll', () => {
        gutter.scrollTop = textarea.scrollTop;
        renderMinimap();
    });

    textarea.addEventListener('click', updateCursorPos);
    textarea.addEventListener('keyup', updateCursorPos);

    // ─── Font size ───
    fontSizeSelect.addEventListener('change', () => {
        const sz = fontSizeSelect.value + 'px';
        textarea.style.fontSize = sz;
        gutter.style.fontSize = sz;
        updateLineNumbers();
        renderMinimap();
    });

    // ─── Word wrap ───
    wordWrapCheck.addEventListener('change', () => {
        textarea.style.whiteSpace = wordWrapCheck.checked ? 'pre-wrap' : 'pre';
        textarea.style.overflowX = wordWrapCheck.checked ? 'hidden' : 'auto';
        updateLineNumbers();
    });

    // ─── Minimap toggle ───
    minimapCheck.addEventListener('change', () => {
        renderMinimap();
    });

    // ─── Find / Replace ───
    function openFind(showReplace) {
        findMode = showReplace ? 'replace' : 'find';
        findBar.style.display = 'flex';
        replaceInput.style.display = showReplace ? '' : 'none';
        body.querySelector('#ce-replace-one').style.display = showReplace ? '' : 'none';
        body.querySelector('#ce-replace-all').style.display = showReplace ? '' : 'none';
        findInput.focus();
        findInput.select();
    }

    function closeFind() {
        findBar.style.display = 'none';
        textarea.focus();
    }

    function findMatches() {
        const query = findInput.value;
        if (!query) { findCountEl.textContent = ''; return []; }
        const text = textarea.value;
        const matches = [];
        let idx = 0;
        while (true) {
            idx = text.indexOf(query, idx);
            if (idx === -1) break;
            matches.push(idx);
            idx += query.length;
        }
        findCountEl.textContent = matches.length ? `${matches.length} ${t('wyników')}` : t('Brak wyników');
        return matches;
    }

    function findNext() {
        const matches = findMatches();
        if (!matches.length) return;
        const pos = textarea.selectionEnd;
        let next = matches.find(m => m >= pos);
        if (next === undefined) next = matches[0];
        textarea.selectionStart = next;
        textarea.selectionEnd = next + findInput.value.length;
        textarea.focus();
        // Scroll to selection
        const lines = textarea.value.substring(0, next).split('\n');
        const approxTop = (lines.length - 1) * 18;
        textarea.scrollTop = approxTop - textarea.clientHeight / 2;
    }

    function findPrev() {
        const matches = findMatches();
        if (!matches.length) return;
        const pos = textarea.selectionStart;
        let prev = [...matches].reverse().find(m => m < pos);
        if (prev === undefined) prev = matches[matches.length - 1];
        textarea.selectionStart = prev;
        textarea.selectionEnd = prev + findInput.value.length;
        textarea.focus();
    }

    function replaceOne() {
        const query = findInput.value;
        const replacement = replaceInput.value;
        if (!query) return;
        const start = textarea.selectionStart;
        const selected = textarea.value.substring(start, textarea.selectionEnd);
        if (selected === query) {
            textarea.value = textarea.value.substring(0, start) + replacement + textarea.value.substring(textarea.selectionEnd);
            textarea.selectionStart = textarea.selectionEnd = start + replacement.length;
            markModified();
            updateLineNumbers();
            renderMinimap();
        }
        findNext();
    }

    function replaceAll() {
        const query = findInput.value;
        const replacement = replaceInput.value;
        if (!query) return;
        const before = textarea.value;
        textarea.value = before.split(query).join(replacement);
        if (textarea.value !== before) {
            markModified();
            updateLineNumbers();
            renderMinimap();
        }
        findMatches();
    }

    body.querySelector('#ce-find-next').addEventListener('click', findNext);
    body.querySelector('#ce-find-prev').addEventListener('click', findPrev);
    body.querySelector('#ce-find-close').addEventListener('click', closeFind);
    body.querySelector('#ce-replace-one').addEventListener('click', replaceOne);
    body.querySelector('#ce-replace-all').addEventListener('click', replaceAll);
    findInput.addEventListener('input', findMatches);
    findInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); findNext(); }
        if (e.key === 'Escape') closeFind();
    });
    replaceInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); replaceOne(); }
        if (e.key === 'Escape') closeFind();
    });

    // ─── Keyboard shortcuts ───
    textarea.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            saveFile();
        }
        if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
            e.preventDefault();
            openFind(false);
        }
        if ((e.ctrlKey || e.metaKey) && e.key === 'h') {
            e.preventDefault();
            openFind(true);
        }
    });

    // ─── Toolbar actions ───
    body.querySelector('.ce-toolbar').addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        switch (btn.dataset.action) {
            case 'new': newFile(); break;
            case 'open': openFileBrowser(); break;
            case 'save': saveFile(); break;
            case 'save-as': saveFileAs(); break;
            case 'undo': document.execCommand('undo'); break;
            case 'redo': document.execCommand('redo'); break;
            case 'find': openFind(false); break;
            case 'replace': openFind(true); break;
        }
    });

    // ─── File operations ───
    async function newFile() {
        if (modified) {
            const ok = await confirmDialog('Nowy plik', t('Niezapisane zmiany zostaną utracone. Kontynuować?'));
            if (!ok) return;
        }
        textarea.value = '';
        currentPath = null;
        currentFilename = 'nowy_plik.txt';
        currentDir = null;
        filenameEl.textContent = currentFilename;
        markSaved();
        updateLineNumbers();
        updateLanguage();
        renderMinimap();
        updateCursorPos();
    }

    async function saveFile() {
        if (!currentPath) {
            saveFileAs();
            return;
        }
        const text = textarea.value;
        const blob = new Blob([text], { type: 'text/plain' });
        const formData = new FormData();
        formData.append('files', blob, currentFilename);
        formData.append('path', currentDir || '/home');

        try {
            const resp = await fetch('/api/files/upload', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${NAS.token}`, 'X-CSRFToken': NAS.csrfToken },
                body: formData
            });
            const data = await resp.json();
            if (data.ok || data.uploaded?.length) {
                toast(`Zapisano: ${currentFilename}`, 'success');
                markSaved();
            } else {
                toast(data.error || t('Błąd zapisu'), 'error');
            }
        } catch {
            toast(t('Błąd zapisu pliku'), 'error');
        }
    }

    async function saveFileAs() {
        // Show folder picker, then ask for filename
        let browsePath = currentDir || '/home';

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:520px;">
                <div class="modal-header">
                    <span>Zapisz jako</span>
                    <button class="modal-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="modal-body" style="padding:0;">
                    <div style="padding:10px 16px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;">
                        <button class="ce-btn" id="ce-saveas-up" title="${t('W górę')}"><i class="fas fa-arrow-up"></i></button>
                        <span id="ce-saveas-path" style="font-size:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></span>
                    </div>
                    <div id="ce-saveas-list" style="height:250px;overflow-y:auto;padding:4px 0;"></div>
                    <div style="padding:10px 16px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center;">
                        <label style="font-size:12px;color:var(--text-muted);white-space:nowrap;">Nazwa:</label>
                        <input type="text" id="ce-saveas-name" value="${_ceEsc(currentFilename)}" 
                               style="flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:var(--r-sm);background:var(--bg-primary);color:var(--text-primary);font-size:13px;outline:none;">
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" id="ce-saveas-cancel">Anuluj</button>
                    <button class="btn btn-primary" id="ce-saveas-confirm">Zapisz</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const pathEl = overlay.querySelector('#ce-saveas-path');
        const listEl = overlay.querySelector('#ce-saveas-list');
        const nameInput = overlay.querySelector('#ce-saveas-name');
        const closeOverlay = () => overlay.remove();

        overlay.querySelector('.modal-close').addEventListener('click', closeOverlay);
        overlay.querySelector('#ce-saveas-cancel').addEventListener('click', closeOverlay);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeOverlay(); });

        overlay.querySelector('#ce-saveas-up').addEventListener('click', () => {
            const parent = browsePath.substring(0, browsePath.lastIndexOf('/')) || '/';
            loadDir(parent);
        });

        overlay.querySelector('#ce-saveas-confirm').addEventListener('click', async () => {
            const fname = nameInput.value.trim();
            if (!fname) { toast(t('Podaj nazwę pliku'), 'warning'); return; }
            closeOverlay();

            const text = textarea.value;
            const blob = new Blob([text], { type: 'text/plain' });
            const formData = new FormData();
            formData.append('files', blob, fname);
            formData.append('path', browsePath);

            try {
                const resp = await fetch('/api/files/upload', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${NAS.token}`, 'X-CSRFToken': NAS.csrfToken },
                    body: formData
                });
                const data = await resp.json();
                if (data.ok || data.uploaded?.length) {
                    toast(`Zapisano: ${fname}`, 'success');
                    currentFilename = fname;
                    currentDir = browsePath;
                    currentPath = browsePath + '/' + fname;
                    filenameEl.textContent = fname;
                    markSaved();
                    updateLanguage();
                } else {
                    toast(data.error || t('Błąd zapisu'), 'error');
                }
            } catch {
                toast(t('Błąd zapisu pliku'), 'error');
            }
        });

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

            listEl.innerHTML = '';
            if (!dirs.length) {
                listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak podfolderów')}</div>`;
            }
            dirs.forEach(d => {
                const row = document.createElement('div');
                row.className = 'dte-browse-item';
                row.innerHTML = `<i class="fas fa-folder" style="color:#f59e0b"></i> ${_ceEsc(d.name)}`;
                row.addEventListener('click', () => loadDir(path + (path === '/' ? '' : '/') + d.name));
                listEl.appendChild(row);
            });
        }

        loadDir(browsePath);
        setTimeout(() => nameInput.focus(), 100);
    }

    async function openFileBrowser() {
        if (modified) {
            const ok = await confirmDialog(t('Otwórz plik'), t('Niezapisane zmiany zostaną utracone. Kontynuować?'));
            if (!ok) return;
        }

        let browsePath = currentDir || '/home';

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:520px;">
                <div class="modal-header">
                    <span>${t('Otwórz plik')}</span>
                    <button class="modal-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="modal-body" style="padding:0;">
                    <div style="padding:10px 16px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:center;">
                        <button class="ce-btn" id="ce-open-up" title="${t('W górę')}"><i class="fas fa-arrow-up"></i></button>
                        <span id="ce-open-path" style="font-size:12px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"></span>
                    </div>
                    <div id="ce-open-list" style="height:300px;overflow-y:auto;padding:4px 0;"></div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" id="ce-open-cancel">Anuluj</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const pathEl = overlay.querySelector('#ce-open-path');
        const listEl = overlay.querySelector('#ce-open-list');
        const closeOverlay = () => overlay.remove();

        overlay.querySelector('.modal-close').addEventListener('click', closeOverlay);
        overlay.querySelector('#ce-open-cancel').addEventListener('click', closeOverlay);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeOverlay(); });

        overlay.querySelector('#ce-open-up').addEventListener('click', () => {
            const parent = browsePath.substring(0, browsePath.lastIndexOf('/')) || '/';
            loadDir(parent);
        });

        const textExts = ['txt', 'log', 'md', 'json', 'xml', 'yaml', 'yml', 'csv', 'ini', 'cfg', 'conf',
            'sh', 'py', 'js', 'ts', 'html', 'css', 'sql', 'php', 'java', 'c', 'cpp', 'h', 'rb', 'go', 'rs',
            'toml', 'env', 'bat', 'cmd', 'ps1', 'lua', 'r', 'swift', 'kt', 'dart', 'vue', 'svelte',
            'jsx', 'tsx', 'scss', 'less', 'makefile', 'dockerfile', 'gitignore', 'editorconfig',
            'htaccess', 'nginx', 'tsv'];

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
            const files = items.filter(i => {
                if (i.is_dir) return false;
                const ext = i.name.split('.').pop().toLowerCase();
                const baseName = i.name.toLowerCase();
                return textExts.includes(ext) || ['makefile', 'dockerfile', '.gitignore', '.env', '.editorconfig', '.htaccess'].includes(baseName);
            }).sort((a, b) => a.name.localeCompare(b.name));

            listEl.innerHTML = '';
            if (!dirs.length && !files.length) {
                listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak plików tekstowych')}</div>`;
                return;
            }
            dirs.forEach(d => {
                const row = document.createElement('div');
                row.className = 'dte-browse-item';
                row.innerHTML = `<i class="fas fa-folder" style="color:#f59e0b"></i> ${_ceEsc(d.name)}`;
                row.addEventListener('click', () => loadDir(path + (path === '/' ? '' : '/') + d.name));
                listEl.appendChild(row);
            });
            files.forEach(f => {
                const row = document.createElement('div');
                row.className = 'dte-browse-item';
                row.innerHTML = `<i class="fas fa-file-code" style="color:#60a5fa"></i> ${_ceEsc(f.name)}`;
                row.addEventListener('click', () => {
                    closeOverlay();
                    openFileContent(path + (path === '/' ? '' : '/') + f.name);
                });
                listEl.appendChild(row);
            });
        }

        loadDir(browsePath);
    }

    async function openFileContent(path) {
        toast('Otwieranie pliku...', 'info');
        try {
            const resp = await fetch(`/api/files/preview?path=${encodeURIComponent(path)}`, {
                headers: { 'Authorization': `Bearer ${NAS.token}` }
            });
            if (!resp.ok) { toast(t('Nie można otworzyć pliku'), 'error'); return; }
            const text = await resp.text();
            textarea.value = text;
            currentPath = path;
            currentFilename = path.split('/').pop();
            currentDir = path.substring(0, path.lastIndexOf('/'));
            filenameEl.textContent = currentFilename;
            markSaved();
            updateLineNumbers();
            updateLanguage();
            renderMinimap();
            updateCursorPos();
            textarea.scrollTop = 0;
            gutter.scrollTop = 0;
            toast(`Otwarto: ${currentFilename}`, 'success');
        } catch {
            toast(t('Nie można otworzyć pliku'), 'error');
        }
    }

    // ─── Init ───
    updateLineNumbers();
    updateLanguage();
    updateCursorPos();

    if (currentPath) {
        openFileContent(currentPath);
    }

    // Resize observer for minimap
    const resizeObserver = new ResizeObserver(() => {
        renderMinimap();
    });
    resizeObserver.observe(body.querySelector('.ce-body'));
    setTimeout(() => renderMinimap(), 100);
}

function _ceEsc(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}
