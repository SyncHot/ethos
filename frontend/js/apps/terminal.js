/* ═══════════════════════════════════════════════════════════
   EthOS — Terminal App (xterm.js + WebSocket PTY)
   ═══════════════════════════════════════════════════════════ */

AppRegistry['terminal'] = function (appDef) {
    const winId = 'terminal-' + Date.now();

    const content = `
        <div class="term-app" id="${winId}-app">
            <div class="term-login" id="${winId}-login">
                <div class="term-login-card">
                    <div class="term-login-icon">
                        <i class="fas fa-terminal"></i>
                    </div>
                    <h3>Terminal</h3>
                    <p class="term-login-sub">${t('Wybierz użytkownika, aby otworzyć sesję')}</p>
                    <div class="term-user-list" id="${winId}-users">
                        <div class="term-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie użytkowników…')}</div>
                    </div>
                </div>
            </div>
            <div class="term-container" id="${winId}-container" style="display:none">
                <div class="term-topbar" id="${winId}-topbar">
                    <span class="term-topbar-info">
                        <i class="fas fa-terminal"></i>
                        <span id="${winId}-username">—</span>
                    </span>
                    <div class="term-topbar-actions">
                        <button class="term-topbar-btn" id="${winId}-reconnect" title="Reconnect">
                            <i class="fas fa-redo"></i>
                        </button>
                        <button class="term-topbar-btn" id="${winId}-disconnect" title="${t('Rozłącz')}">
                            <i class="fas fa-sign-out-alt"></i>
                        </button>
                    </div>
                </div>
                <div class="term-xterm" id="${winId}-xterm"></div>
            </div>
        </div>
    `;

    const win = createWindow(winId, {
        title: 'Terminal',
        icon: appDef.icon || 'fa-terminal',
        iconColor: appDef.color || '#22c55e',
        width: 900,
        height: 550,
        minWidth: 500,
        minHeight: 300,
        content: content,
        singleton: false,
        onClose: () => {
            if (socket && socket.connected) {
                socket.emit('terminal_close');
            }
            if (term) term.dispose();
        }
    });

    if (!win) return;

    const body = document.getElementById(`win-body-${winId}`);
    let term = null;
    let fitAddon = null;
    let socket = null;
    let currentUser = null;

    // ── Load users ──
    loadUsers();

    async function loadUsers() {
        const listEl = document.getElementById(`${winId}-users`);
        try {
            const users = await api('/terminal/users');
            if (!users.length) {
                listEl.innerHTML = `<div class="term-no-users">${t('Brak dostępnych użytkowników')}</div>`;
                return;
            }

            // Sort: root first, then alphabetically
            users.sort((a, b) => {
                if (a.username === 'root') return -1;
                if (b.username === 'root') return 1;
                return a.username.localeCompare(b.username);
            });

            listEl.innerHTML = users.map(u => `
                <button class="term-user-btn" data-user="${u.username}" data-shell="${u.shell}">
                    <div class="term-user-avatar">
                        <i class="fas ${u.username === 'root' ? 'fa-user-shield' : 'fa-user'}"></i>
                    </div>
                    <div class="term-user-info">
                        <span class="term-user-name">${u.username}</span>
                        <span class="term-user-detail">${u.shell} · ${u.home}</span>
                    </div>
                    <i class="fas fa-chevron-right term-user-arrow"></i>
                </button>
            `).join('');

            listEl.addEventListener('click', (e) => {
                const btn = e.target.closest('.term-user-btn');
                if (!btn) return;
                const username = btn.dataset.user;
                startTerminal(username);
            });
        } catch (e) {
            listEl.innerHTML = `<div class="term-no-users">${t('Błąd:')} ${e.message}</div>`;
        }
    }

    function startTerminal(username) {
        currentUser = username;

        // Hide login, show terminal
        document.getElementById(`${winId}-login`).style.display = 'none';
        document.getElementById(`${winId}-container`).style.display = 'flex';
        document.getElementById(`${winId}-username`).textContent = `${username}@${NAS.nasName || 'nas'}`;

        // Create xterm instance
        term = new Terminal({
            cursorBlink: true,
            cursorStyle: 'bar',
            fontSize: 14,
            fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace",
            theme: {
                background: '#0d1117',
                foreground: '#c9d1d9',
                cursor: '#58a6ff',
                cursorAccent: '#0d1117',
                selectionBackground: 'rgba(56,139,253,0.3)',
                black: '#484f58',
                red: '#ff7b72',
                green: '#3fb950',
                yellow: '#d29922',
                blue: '#58a6ff',
                magenta: '#bc8cff',
                cyan: '#39d353',
                white: '#b1bac4',
                brightBlack: '#6e7681',
                brightRed: '#ffa198',
                brightGreen: '#56d364',
                brightYellow: '#e3b341',
                brightBlue: '#79c0ff',
                brightMagenta: '#d2a8ff',
                brightCyan: '#56d364',
                brightWhite: '#f0f6fc',
            },
            allowProposedApi: true,
            scrollback: 5000,
        });

        fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);

        const webLinksAddon = new WebLinksAddon.WebLinksAddon();
        term.loadAddon(webLinksAddon);

        const xtermEl = document.getElementById(`${winId}-xterm`);
        term.open(xtermEl);

        // Small delay to ensure container is rendered
        setTimeout(() => {
            fitAddon.fit();
            connectSocket(username);
        }, 100);

        // Handle window resize
        const resizeObserver = new ResizeObserver(() => {
            if (fitAddon && term) {
                try { fitAddon.fit(); } catch (e) {}
            }
        });
        resizeObserver.observe(xtermEl);

        // Send input to backend
        term.onData((data) => {
            if (socket && socket.connected) {
                socket.emit('terminal_input', { data: data });
            }
        });

        // Send resize events
        term.onResize(({ cols, rows }) => {
            if (socket && socket.connected) {
                socket.emit('terminal_resize', { cols, rows });
            }
        });

        // Topbar buttons
        document.getElementById(`${winId}-reconnect`).addEventListener('click', () => {
            reconnect();
        });

        document.getElementById(`${winId}-disconnect`).addEventListener('click', () => {
            disconnect();
        });
    }

    function connectSocket(username) {
        // Use existing NAS socket or create a new one
        socket = io({ transports: ['websocket', 'polling'] });

        socket.on('connect', () => {
            const dims = fitAddon.proposeDimensions();
            socket.emit('terminal_open', {
                username: username,
                cols: dims ? dims.cols : 80,
                rows: dims ? dims.rows : 24,
            });
        });

        socket.on('terminal_ready', (data) => {
            term.focus();
        });

        socket.on('terminal_output', (data) => {
            if (term) term.write(data.data);
        });

        socket.on('disconnect', () => {
            if (term) term.write(t('\r\n\x1b[33m[Rozłączono]\x1b[0m\r\n'));
        });
    }

    function reconnect() {
        if (socket) {
            socket.emit('terminal_close');
            socket.disconnect();
        }
        if (term) {
            term.clear();
            term.write(t('\x1b[33m[Łączenie ponowne…]\x1b[0m\r\n'));
        }
        setTimeout(() => {
            connectSocket(currentUser);
        }, 300);
    }

    function disconnect() {
        if (socket) {
            socket.emit('terminal_close');
            socket.disconnect();
        }
        if (term) {
            term.dispose();
            term = null;
        }
        // Show login screen again
        document.getElementById(`${winId}-container`).style.display = 'none';
        document.getElementById(`${winId}-login`).style.display = 'flex';
        currentUser = null;
    }
};
