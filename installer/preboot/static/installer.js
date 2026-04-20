/**
 * EthOS Installer — Frontend step wizard with scenario-based disk selection.
 *
 * Steps:
 *   0 Language → 1 Account → 2 Scenario → 3 Disks → 4 Security → 5 Summary → 6 Install → 7 Done
 */

const STEP_IDS = [
    'step-lang', 'step-account', 'step-scenario', 'step-disks',
    'step-security', 'step-summary', 'step-install', 'step-done'
];

const FLAGS = { pl: '🇵🇱', en: '🇬🇧', de: '🇩🇪', fr: '🇫🇷', es: '🇪🇸' };

const Installer = {
    step: 0,
    lang: 'pl',
    translations: {},
    // User data
    username: '',
    password: '',
    hostname: 'ethos',
    // Disk data
    osDisk: null,
    dataDisk: null,
    sameDisk: true,
    scenario: 'simple',
    bootDevice: null,
    disks: [],
    recommendation: null,
    // Encryption
    encrypt: false,
    passphrase: '',
    // Install state
    pollTimer: null,
    installNewIP: null,
    logSince: 0,

    async init() {
        this.buildStepDots();
        this.buildLangGrid();
        this.showStep(0);
        this.bindEvents();
    },

    // ── Navigation ──

    showStep(n) {
        this.step = n;
        STEP_IDS.forEach((id, i) => {
            document.getElementById(id).classList.toggle('hidden', i !== n);
        });
        this.updateDots();
        this.updateNav();
        if (n === 2) this.loadScenarios();
        if (n === 3) this.renderDiskStep();
        if (n === 5) this.buildSummary();
        if (n === 6) this.startInstall();
    },

    next() {
        if (!this.validateStep(this.step)) return;
        if (this.step < STEP_IDS.length - 1) {
            this.saveStepData(this.step);
            this.showStep(this.step + 1);
        }
    },

    prev() {
        if (this.step > 0 && this.step < 6) {
            this.showStep(this.step - 1);
        }
    },

    buildStepDots() {
        const c = document.getElementById('steps-indicator');
        c.innerHTML = STEP_IDS.map((_, i) =>
            `<div class="step-dot" data-step="${i}"></div>`
        ).join('');
    },

    updateDots() {
        document.querySelectorAll('.step-dot').forEach((d, i) => {
            d.className = 'step-dot' +
                (i < this.step ? ' done' : i === this.step ? ' active' : '');
        });
    },

    updateNav() {
        const back = document.getElementById('btn-back');
        const next = document.getElementById('btn-next');
        const nav = document.getElementById('nav-bar');
        nav.classList.toggle('hidden', this.step >= 6);
        back.classList.toggle('hidden', this.step === 0);
        const nextLabel = next.querySelector('span');
        if (this.step === 5) {
            nextLabel.textContent = this.t('Rozpocznij instalację');
            next.classList.add('btn-install');
        } else {
            nextLabel.textContent = this.t('Dalej');
            next.classList.remove('btn-install');
        }
    },

    // ── i18n ──

    t(key) {
        if (this.lang === 'pl') return key;
        return this.translations[key] || key;
    },

    async loadTranslations(lang) {
        try {
            const r = await fetch(`/api/i18n/${lang}`);
            this.translations = await r.json();
        } catch (e) {
            console.warn('i18n load failed:', e);
            this.translations = {};
        }
    },

    applyTranslations() {
        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            el.textContent = this.t(key);
        });
        document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
            el.placeholder = this.t(el.getAttribute('data-i18n-placeholder'));
        });
    },

    // ── Step 0: Language ──

    buildLangGrid() {
        const grid = document.getElementById('lang-grid');
        const langs = [
            { code: 'pl', label: 'Polski' },
            { code: 'en', label: 'English' },
            { code: 'de', label: 'Deutsch' },
            { code: 'fr', label: 'Français' },
            { code: 'es', label: 'Español' },
        ];
        grid.innerHTML = langs.map(l =>
            `<button class="lang-btn${l.code === this.lang ? ' selected' : ''}" data-lang="${l.code}">` +
            `<span class="lang-flag">${FLAGS[l.code] || ''}</span>${l.label}</button>`
        ).join('');
        grid.querySelectorAll('.lang-btn').forEach(btn => {
            btn.onclick = async () => {
                grid.querySelectorAll('.lang-btn').forEach(b => b.classList.remove('selected'));
                btn.classList.add('selected');
                this.lang = btn.dataset.lang;
                await this.loadTranslations(this.lang);
                this.applyTranslations();
            };
        });
    },

    // ── Step 2: Scenario selection ──

    async loadScenarios() {
        const loading = document.getElementById('scenario-loading');
        const cards = document.getElementById('scenario-cards');
        const errDiv = document.getElementById('scenario-error');
        loading.classList.remove('hidden');
        cards.classList.add('hidden');
        errDiv.classList.add('hidden');

        try {
            const r = await fetch('/api/disks/recommend');
            const data = await r.json();
            this.disks = data.disks || [];
            this.bootDevice = data.boot_device || null;
            this.recommendation = data;
            this.renderScenarioCards(data);
            cards.classList.remove('hidden');
        } catch (e) {
            errDiv.textContent = this.t('Błąd') + ': ' + e.message;
            errDiv.classList.remove('hidden');
        }
        loading.classList.add('hidden');
    },

    renderScenarioCards(data) {
        const grid = document.getElementById('scenario-cards');
        const scenarios = data.scenarios || [];
        const recommended = data.recommended || 'simple';

        // If only 1 disk → only simple available
        const hasSsd = this.disks.some(d =>
            (d.transport === 'nvme' || (d.transport === 'sata' && d.model && /ssd|nvme/i.test(d.model))) && !d.is_boot
        );
        const hasMultiple = this.disks.filter(d => !d.is_boot).length >= 2;

        const scenarioDefs = [
            {
                id: 'simple',
                icon: 'fa-desktop',
                title: this.t('Prosty'),
                desc: this.t('System i dane na jednym dysku'),
                detail: this.t('Jeden dysk na wszystko. Idealne do rozpoczęcia.'),
                available: true,
            },
            {
                id: 'performance',
                icon: 'fa-bolt',
                title: this.t('Wydajny'),
                desc: this.t('Szybki SSD na system, HDD na dane'),
                detail: this.t('System na szybkim dysku, dane na dużym. Najlepsza wydajność.'),
                available: hasMultiple,
            },
            {
                id: 'advanced',
                icon: 'fa-sliders-h',
                title: this.t('Zaawansowany'),
                desc: this.t('Ręczny wybór dysków'),
                detail: this.t('Pełna kontrola nad przypisaniem dysków do ról.'),
                available: hasMultiple,
            },
        ];

        grid.innerHTML = scenarioDefs.map(s => {
            const isRec = s.id === recommended;
            const sel = s.id === this.scenario;
            const cls = [
                'scenario-card',
                sel ? 'selected' : '',
                !s.available ? 'disabled' : '',
                isRec ? 'recommended' : '',
            ].filter(Boolean).join(' ');

            return `<div class="${cls}" data-scenario="${s.id}">
                ${isRec ? `<div class="scenario-badge">${this.t('Zalecane')}</div>` : ''}
                <div class="scenario-icon"><i class="fas ${s.icon}"></i></div>
                <div class="scenario-title">${s.title}</div>
                <div class="scenario-desc">${s.desc}</div>
                <div class="scenario-detail">${s.detail}</div>
                ${!s.available ? `<div class="scenario-unavail">${this.t('Potrzeba ≥2 dyski')}</div>` : ''}
            </div>`;
        }).join('');

        // Auto-select recommended
        if (!this.scenario || !scenarioDefs.find(s => s.id === this.scenario && s.available)) {
            this.scenario = recommended;
        }
        this._highlightScenario();

        grid.querySelectorAll('.scenario-card:not(.disabled)').forEach(card => {
            card.onclick = () => {
                this.scenario = card.dataset.scenario;
                this._highlightScenario();
            };
        });
    },

    _highlightScenario() {
        document.querySelectorAll('.scenario-card').forEach(c => {
            c.classList.toggle('selected', c.dataset.scenario === this.scenario);
        });
    },

    // ── Step 3: Disk selection ──

    renderDiskStep() {
        const osSection = document.getElementById('disk-os-section');
        const dataSection = document.getElementById('disk-data-section');
        const previewSection = document.getElementById('partition-preview');

        if (this.scenario === 'simple') {
            this.sameDisk = true;
            dataSection.classList.add('hidden');
            this.renderDiskList('disk-list-os', 'os');
            this._autoSelectOsDisk();
        } else {
            this.sameDisk = false;
            dataSection.classList.remove('hidden');
            this.renderDiskList('disk-list-os', 'os');
            this.renderDiskList('disk-list-data', 'data');
            this._autoAssignPerformance();
        }

        this.updatePartitionPreview();
    },

    _autoSelectOsDisk() {
        if (this.osDisk) return;
        const eligible = this.disks.filter(d => !d.is_boot && !(d.transport === 'usb' || d.removable));
        if (eligible.length === 1) {
            this.osDisk = eligible[0].name;
            this._selectDiskInUI('disk-list-os', this.osDisk);
        }
    },

    _autoAssignPerformance() {
        if (this.scenario !== 'performance') return;
        // Auto-assign: fastest disk → OS, largest remaining → data
        const eligible = this.disks.filter(d => !d.is_boot);
        const ssds = eligible.filter(d => d.transport === 'nvme' || (d.transport !== 'usb' && d.model && /ssd/i.test(d.model)));
        const hdds = eligible.filter(d => !ssds.includes(d) && !(d.transport === 'usb' || d.removable));

        if (ssds.length && hdds.length) {
            ssds.sort((a, b) => b.size_bytes - a.size_bytes);
            hdds.sort((a, b) => b.size_bytes - a.size_bytes);
            if (!this.osDisk) {
                this.osDisk = ssds[0].name;
                this._selectDiskInUI('disk-list-os', this.osDisk);
            }
            if (!this.dataDisk) {
                this.dataDisk = hdds[0].name;
                this._selectDiskInUI('disk-list-data', this.dataDisk);
            }
        }
    },

    _selectDiskInUI(containerId, diskName) {
        const c = document.getElementById(containerId);
        if (!c) return;
        c.querySelectorAll('.disk-item').forEach(item => {
            item.classList.toggle('selected', item.dataset.disk === diskName);
        });
    },

    renderDiskList(containerId, role) {
        const c = document.getElementById(containerId);
        c.innerHTML = this.disks.map(d => {
            const isBoot = d.is_boot;
            const isUsb = (d.transport === 'usb' || d.removable) && !isBoot;
            const disabled = isBoot || (role === 'os' && isUsb);
            const selected = role === 'os' ? this.osDisk === d.name : this.dataDisk === d.name;

            const typeIcon = d.transport === 'nvme' ? 'fa-bolt' :
                             d.transport === 'usb' ? 'fa-usb' : 'fa-hdd';
            const typeLabel = d.transport === 'nvme' ? 'NVMe' :
                              d.transport === 'usb' ? 'USB' : 'SATA';

            let badges = '';
            if (isBoot) badges += `<span class="disk-badge boot">${this.t('Instalator')}</span> `;
            if (isUsb && role === 'os') badges += `<span class="disk-badge usb-warn">${this.t('USB — nie można użyć jako systemowy')}</span> `;
            if (isUsb && role === 'data') badges += `<span class="disk-badge usb-warn">⚠ USB</span> `;
            if (d.smart_status === 'failed') badges += '<span class="disk-badge smart-fail">SMART ✗</span> ';

            return `<div class="disk-item${selected ? ' selected' : ''}${disabled ? ' disabled' : ''}" data-disk="${d.name}" data-role="${role}">
                <div class="disk-type-icon"><i class="fas ${typeIcon}"></i><span class="disk-type-label">${typeLabel}</span></div>
                <div class="disk-info">
                    <div class="disk-name">${this._esc(d.model || d.name)}</div>
                    <div class="disk-detail">/dev/${d.name} · ${d.partitions || 0} ${this.t('partycji')}${badges ? ' · ' + badges : ''}</div>
                </div>
                <div class="disk-size">${d.size_human}</div>
            </div>`;
        }).join('');

        c.querySelectorAll('.disk-item:not(.disabled)').forEach(item => {
            item.onclick = () => {
                c.querySelectorAll('.disk-item').forEach(i => i.classList.remove('selected'));
                item.classList.add('selected');
                if (role === 'os') this.osDisk = item.dataset.disk;
                else this.dataDisk = item.dataset.disk;
                this.updatePartitionPreview();
            };
        });
    },

    updatePartitionPreview() {
        const container = document.getElementById('partition-preview');
        const viz = document.getElementById('partition-viz');
        if (!this.osDisk) {
            container.classList.add('hidden');
            return;
        }
        container.classList.remove('hidden');

        const osDiskObj = this.disks.find(d => d.name === this.osDisk);
        if (!osDiskObj) return;

        const osSizeGB = Math.round(osDiskObj.size_bytes / 1073741824);
        let html = '';

        if (this.sameDisk) {
            // Same disk: ESP + RootA + RootB + Data
            const dataGB = Math.max(0, osSizeGB - 9);
            html += `<div class="pv-disk">
                <div class="pv-disk-label"><i class="fas fa-hdd"></i> ${this._esc(osDiskObj.model)} (${osDiskObj.size_human})</div>
                <div class="pv-bar">
                    <div class="pv-seg pv-esp" style="flex:0.5" title="ESP 512MB"><span>ESP</span></div>
                    <div class="pv-seg pv-root" style="flex:4" title="Root A 4GB"><span>Root A</span></div>
                    <div class="pv-seg pv-root-b" style="flex:4" title="Root B 4GB"><span>Root B</span></div>
                    <div class="pv-seg pv-data" style="flex:${Math.max(1, dataGB)}" title="${this.t('Dane')} ~${dataGB} GB"><span>${this.t('Dane')} ~${dataGB} GB</span></div>
                </div>
            </div>`;
        } else {
            // Separate: OS disk (ESP + RootA + RootB) + Data disk
            html += `<div class="pv-disk">
                <div class="pv-disk-label"><i class="fas fa-microchip"></i> ${this.t('System')}: ${this._esc(osDiskObj.model)} (${osDiskObj.size_human})</div>
                <div class="pv-bar">
                    <div class="pv-seg pv-esp" style="flex:0.5"><span>ESP</span></div>
                    <div class="pv-seg pv-root" style="flex:4"><span>Root A</span></div>
                    <div class="pv-seg pv-root-b" style="flex:4"><span>Root B</span></div>
                </div>
            </div>`;

            if (this.dataDisk) {
                const dataDiskObj = this.disks.find(d => d.name === this.dataDisk);
                if (dataDiskObj) {
                    const dataSizeGB = Math.round(dataDiskObj.size_bytes / 1073741824);
                    html += `<div class="pv-disk">
                        <div class="pv-disk-label"><i class="fas fa-database"></i> ${this.t('Dane')}: ${this._esc(dataDiskObj.model)} (${dataDiskObj.size_human})</div>
                        <div class="pv-bar">
                            <div class="pv-seg pv-data" style="flex:1"><span>${this.t('Dane')} ~${dataSizeGB} GB (Btrfs)</span></div>
                        </div>
                    </div>`;
                }
            }
        }

        viz.innerHTML = html;
    },

    // ── Step 4: Security ──

    bindEvents() {
        const chk = document.getElementById('chk-encrypt');
        chk.onchange = () => {
            this.encrypt = chk.checked;
            document.getElementById('encrypt-details').classList.toggle('hidden', !this.encrypt);
        };
    },

    // ── Step 5: Summary ──

    buildSummary() {
        const table = document.getElementById('summary-table');
        const osDiskObj = this.disks.find(d => d.name === this.osDisk);
        const dataDiskObj = this.sameDisk ? osDiskObj : this.disks.find(d => d.name === this.dataDisk);

        const scenarioLabels = {
            simple: this.t('Prosty') + ' — ' + this.t('jeden dysk'),
            performance: this.t('Wydajny') + ' — ' + this.t('SSD + HDD'),
            advanced: this.t('Zaawansowany'),
        };

        const rows = [
            [this.t('Język'), (FLAGS[this.lang] || '') + ' ' + this.lang.toUpperCase()],
            [this.t('Użytkownik'), this.username],
            [this.t('Nazwa hosta'), this.hostname],
            [this.t('Tryb'), scenarioLabels[this.scenario] || this.scenario],
            [this.t('Dysk systemu'), osDiskObj ? `${osDiskObj.model} (${osDiskObj.size_human})` : '—'],
            [this.t('Dysk danych'), this.sameDisk ? this.t('Ten sam dysk') : (dataDiskObj ? `${dataDiskObj.model} (${dataDiskObj.size_human})` : '—')],
            [this.t('Szyfrowanie'), this.encrypt ? '🔒 LUKS' : this.t('Wyłączone')],
        ];
        table.innerHTML = rows.map(([l, v]) =>
            `<div class="summary-row"><div class="summary-label">${l}</div><div class="summary-value">${this._esc(v)}</div></div>`
        ).join('');

        const i18n_confirm = { pl: 'INSTALUJ', en: 'INSTALL', de: 'INSTALLIEREN', fr: 'INSTALLER', es: 'INSTALAR' };
        const token = i18n_confirm[this.lang] || 'INSTALUJ';
        document.getElementById('confirm-label').textContent =
            `${this.t('Wpisz INSTALUJ aby potwierdzić')}: ${token}`;
        document.getElementById('inp-confirm').placeholder = token;
    },

    // ── Step 6: Install ──

    async startInstall() {
        const body = {
            os_disk: this.osDisk,
            data_disk: this.sameDisk ? 'same' : this.dataDisk,
            username: this.username,
            password: this.password,
            hostname: this.hostname,
            lang: this.lang,
            confirmation: document.getElementById('inp-confirm').value.trim(),
            encrypt: this.encrypt,
            passphrase: this.passphrase,
        };

        try {
            const r = await fetch('/api/install/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await r.json();
            if (!data.ok) {
                alert(this.t('Błąd') + ': ' + (data.error || 'Unknown'));
                this.showStep(5);
                return;
            }
            this.pollProgress();
        } catch (e) {
            alert(this.t('Błąd') + ': ' + e.message);
            this.showStep(5);
        }
    },

    pollProgress() {
        if (this.pollTimer) clearInterval(this.pollTimer);
        this.logSince = 0;
        document.getElementById('install-log').innerHTML = '';
        this.pollTimer = setInterval(async () => {
            try {
                const [data, logData] = await Promise.all([
                    fetch('/api/install/progress').then(r => r.json()),
                    fetch(`/api/install/logs?since=${this.logSince}`).then(r => r.json()),
                ]);

                document.getElementById('progress-bar').style.width = data.percent + '%';
                document.getElementById('progress-pct').textContent = data.percent + '%';
                document.getElementById('progress-msg').textContent = this.t(data.message || '');

                if (logData.logs && logData.logs.length) {
                    const logEl = document.getElementById('install-log');
                    for (const entry of logData.logs) {
                        const d = new Date(entry.ts * 1000);
                        const ts = d.toLocaleTimeString('pl', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                        const isErr = /error|fatal|fail/i.test(entry.msg);
                        const div = document.createElement('div');
                        div.className = 'log-line' + (isErr ? ' log-err' : '');
                        div.innerHTML = `<span class="log-ts">${this._esc(ts)}</span>${this._esc(entry.msg)}`;
                        logEl.appendChild(div);
                    }
                    logEl.scrollTop = logEl.scrollHeight;
                    this.logSince = logData.total;
                }

                if (data.done) {
                    clearInterval(this.pollTimer);
                    this.pollTimer = null;
                    this.installNewIP = data.new_ip || null;
                    this.showStep(7);
                }
                if (data.error) {
                    clearInterval(this.pollTimer);
                    this.pollTimer = null;
                    document.getElementById('progress-msg').innerHTML =
                        `<div class="msg-error">${this.t('Błąd')}: ${this._esc(data.error)}</div>`;
                }
            } catch (e) { /* server may be rebooting */ }
        }, 1500);
    },

    // ── Step 7: Done / Reboot ──

    async reboot() {
        const ip = this.installNewIP || '—';
        const port = 9000;
        const url = ip !== '—' ? `http://${ip}:${port}` : this.t('Szukaj EthOS w sieci');
        document.getElementById('done-url').textContent = url;
        try {
            await fetch('/api/install/reboot', { method: 'POST' });
        } catch (e) { /* expected — server shuts down */ }
    },

    // ── Validation ──

    validateStep(n) {
        if (n === 1) return this.validateAccount();
        if (n === 2) return this.validateScenario();
        if (n === 3) return this.validateDisks();
        if (n === 4) return this.validateSecurity();
        if (n === 5) return this.validateSummary();
        return true;
    },

    validateAccount() {
        let ok = true;
        const u = document.getElementById('inp-username').value.trim();
        const p = document.getElementById('inp-password').value;
        const p2 = document.getElementById('inp-password2').value;

        document.getElementById('err-username').textContent = '';
        document.getElementById('err-password').textContent = '';
        document.getElementById('err-password2').textContent = '';

        if (!u) {
            document.getElementById('err-username').textContent = this.t('Nazwa użytkownika jest wymagana');
            ok = false;
        } else if (!/^[a-z_][a-z0-9_-]{0,31}$/.test(u)) {
            document.getElementById('err-username').textContent = 'a-z, 0-9, _, - only';
            ok = false;
        }
        if (p.length < 4) {
            document.getElementById('err-password').textContent = this.t('Hasło musi mieć min. 4 znaki');
            ok = false;
        }
        if (p !== p2) {
            document.getElementById('err-password2').textContent = this.t('Hasła nie są zgodne');
            ok = false;
        }
        return ok;
    },

    validateScenario() {
        if (!this.scenario) {
            alert(this.t('Wybierz tryb instalacji'));
            return false;
        }
        return true;
    },

    validateDisks() {
        if (!this.osDisk) {
            alert(this.t('Wybierz dysk systemu'));
            return false;
        }
        if (!this.sameDisk && !this.dataDisk) {
            alert(this.t('Wybierz dysk danych'));
            return false;
        }
        if (!this.sameDisk && this.osDisk === this.dataDisk) {
            alert(this.t('Dysk systemu i danych nie mogą być takie same'));
            return false;
        }
        return true;
    },

    validateSecurity() {
        if (!this.encrypt) return true;
        const p = document.getElementById('inp-passphrase').value;
        const p2 = document.getElementById('inp-passphrase2').value;

        document.getElementById('err-passphrase').textContent = '';
        document.getElementById('err-passphrase2').textContent = '';

        if (p.length < 8) {
            document.getElementById('err-passphrase').textContent =
                this.t('Hasło musi mieć min. 8 znaków');
            return false;
        }
        if (p !== p2) {
            document.getElementById('err-passphrase2').textContent =
                this.t('Hasła nie są zgodne');
            return false;
        }
        return true;
    },

    validateSummary() {
        const confirm = document.getElementById('inp-confirm').value.trim().toUpperCase();
        const tokens = { pl: 'INSTALUJ', en: 'INSTALL', de: 'INSTALLIEREN', fr: 'INSTALLER', es: 'INSTALAR' };
        const expected = tokens[this.lang] || 'INSTALUJ';
        if (confirm !== expected) {
            alert(`${this.t('Wpisz INSTALUJ aby potwierdzić')}: ${expected}`);
            return false;
        }
        return true;
    },

    saveStepData(n) {
        if (n === 1) {
            this.username = document.getElementById('inp-username').value.trim();
            this.password = document.getElementById('inp-password').value;
            this.hostname = document.getElementById('inp-hostname').value.trim() || 'ethos';
        }
        if (n === 4) {
            this.encrypt = document.getElementById('chk-encrypt').checked;
            this.passphrase = this.encrypt ? document.getElementById('inp-passphrase').value : '';
        }
    },

    // ── Util ──

    _esc(s) {
        const d = document.createElement('div');
        d.textContent = s || '';
        return d.innerHTML;
    },
};

document.addEventListener('DOMContentLoaded', () => Installer.init());
