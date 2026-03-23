/**
 * EthOS Installer — Frontend step wizard.
 */

const STEP_IDS = ['step-lang', 'step-account', 'step-disks', 'step-network', 'step-summary', 'step-install', 'step-done'];
const STEP_LABELS = ['Wybierz język', 'Utwórz konto', 'Wybierz dyski', 'Konfiguracja sieci', 'Podsumowanie', 'Instalacja', 'Gotowe'];

const FLAGS = { pl: '🇵🇱', en: '🇬🇧', de: '🇩🇪', fr: '🇫🇷', es: '🇪🇸' };

const Installer = {
    step: 0,
    lang: 'pl',
    translations: {},
    // Data
    username: '',
    password: '',
    hostname: 'ethos',
    osDisk: null,
    dataDisk: null, // null = same disk
    sameDisk: true,
    bootDevice: null,
    disks: [],
    wifiSSID: null,
    wifiConnected: false,
    networkOk: false,
    pollTimer: null,

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
        // Hooks per step
        if (n === 2) this.loadDisks();
        if (n === 3) this.loadNetwork();
        if (n === 4) this.buildSummary();
        if (n === 5) this.startInstall();
    },

    next() {
        if (!this.validateStep(this.step)) return;
        if (this.step < STEP_IDS.length - 1) {
            this.saveStepData(this.step);
            this.showStep(this.step + 1);
        }
    },

    prev() {
        if (this.step > 0 && this.step < 5) { // Can't go back during install
            this.showStep(this.step - 1);
        }
    },

    buildStepDots() {
        const c = document.getElementById('steps-indicator');
        c.innerHTML = STEP_IDS.map((_, i) => `<div class="step-dot" data-step="${i}"></div>`).join('');
    },

    updateDots() {
        document.querySelectorAll('.step-dot').forEach((d, i) => {
            d.className = 'step-dot' + (i < this.step ? ' done' : i === this.step ? ' active' : '');
        });
    },

    updateNav() {
        const back = document.getElementById('btn-back');
        const next = document.getElementById('btn-next');
        const nav = document.getElementById('nav-bar');
        // Hide nav on install/done steps
        nav.classList.toggle('hidden', this.step >= 5);
        back.classList.toggle('hidden', this.step === 0);
        // On summary step, change button text
        const nextLabel = next.querySelector('span');
        if (this.step === 4) {
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

    // ── Step 1: Language ──

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

    // ── Step 2: Account ──

    // ── Step 3: Disks ──

    async loadDisks() {
        const loading = document.getElementById('disk-loading');
        const section = document.getElementById('disk-section');
        const errDiv = document.getElementById('disk-error');
        loading.classList.remove('hidden');
        section.classList.add('hidden');
        errDiv.classList.add('hidden');
        try {
            const r = await fetch('/api/disks/discover');
            const data = await r.json();
            this.disks = data.disks || [];
            this.bootDevice = data.boot_device;
            if (this.disks.length === 0) {
                errDiv.textContent = this.t('Nie znaleziono dysków');
                errDiv.classList.remove('hidden');
            } else {
                this.renderDiskList('disk-list-os', 'os');
                this.renderDiskList('disk-list-data', 'data');
                section.classList.remove('hidden');
            }
        } catch (e) {
            errDiv.textContent = this.t('Błąd') + ': ' + e.message;
            errDiv.classList.remove('hidden');
        }
        loading.classList.add('hidden');
    },

    renderDiskList(containerId, role) {
        const c = document.getElementById(containerId);
        c.innerHTML = this.disks.map(d => {
            const isBoot = d.is_boot;
            const disabled = isBoot;
            const selected = role === 'os' ? this.osDisk === d.name : this.dataDisk === d.name;
            let badges = '';
            if (isBoot) badges += `<span class="disk-badge boot">${this.t('Dysk startowy (USB)')}</span> `;
            if (d.smart_status === 'failed') badges += '<span class="disk-badge smart-fail">SMART FAIL</span> ';
            return `<div class="disk-item${selected ? ' selected' : ''}${disabled ? ' disabled' : ''}" data-disk="${d.name}" data-role="${role}">` +
                `<div class="disk-radio"></div>` +
                `<div class="disk-info"><div class="disk-name">${this._esc(d.model)}</div>` +
                `<div class="disk-detail">/dev/${d.name} · ${d.transport || '?'} · ${d.partitions} part${badges ? ' · ' + badges : ''}</div></div>` +
                `<div class="disk-size">${d.size_human}</div></div>`;
        }).join('');
        c.querySelectorAll('.disk-item:not(.disabled)').forEach(item => {
            item.onclick = () => {
                c.querySelectorAll('.disk-item').forEach(i => i.classList.remove('selected'));
                item.classList.add('selected');
                if (role === 'os') this.osDisk = item.dataset.disk;
                else this.dataDisk = item.dataset.disk;
            };
        });
    },

    bindEvents() {
        const chk = document.getElementById('chk-same-disk');
        chk.onchange = () => {
            this.sameDisk = chk.checked;
            document.getElementById('data-disk-section').classList.toggle('hidden', this.sameDisk);
        };
    },

    // ── Step 4: Network ──

    async loadNetwork() {
        const statusDiv = document.getElementById('net-status');
        const ethDiv = document.getElementById('net-ethernet');
        const wifiDiv = document.getElementById('net-wifi-section');
        statusDiv.classList.remove('hidden');
        ethDiv.classList.add('hidden');
        wifiDiv.classList.add('hidden');

        try {
            const r = await fetch('/api/wifi/status');
            const data = await r.json();

            if (data.ethernet && data.ethernet_ip) {
                document.getElementById('eth-ip').textContent = data.ethernet_ip;
                ethDiv.classList.remove('hidden');
                statusDiv.classList.add('hidden');
                this.networkOk = true;
            } else if (data.has_wifi) {
                statusDiv.innerHTML = `<span>${this.t('Brak sieci — wybierz WiFi')}</span>`;
                wifiDiv.classList.remove('hidden');
                this.scanWifi();
            } else {
                statusDiv.innerHTML = `<div class="msg-warn">${this.t('Brak sieci — wybierz WiFi')}</div>`;
            }
        } catch (e) {
            statusDiv.innerHTML = `<div class="msg-error">${this.t('Błąd')}: ${e.message}</div>`;
        }
    },

    async scanWifi() {
        const list = document.getElementById('wifi-list');
        list.innerHTML = `<div class="loading"><i class="fas fa-spinner fa-spin"></i> ${this.t('Skanowanie sieci WiFi...')}</div>`;
        try {
            const r = await fetch('/api/wifi/scan');
            const data = await r.json();
            const nets = data.networks || [];
            if (nets.length === 0) {
                list.innerHTML = `<div class="msg-warn">${this.t('Nie znaleziono sieci WiFi')}</div>`;
                return;
            }
            list.innerHTML = nets.map(n => {
                const bars = n.signal >= 75 ? '▂▄▆█' : n.signal >= 50 ? '▂▄▆' : n.signal >= 25 ? '▂▄' : '▂';
                const lock = n.security ? '<i class="fas fa-lock"></i>' : '';
                return `<div class="wifi-item" data-ssid="${this._esc(n.ssid)}">` +
                    `<span class="wifi-bars">${bars}</span>` +
                    `<span class="wifi-name">${this._esc(n.ssid)}</span>` +
                    `<span class="wifi-lock">${lock}</span>` +
                    `<span class="wifi-signal">${n.signal}%</span></div>`;
            }).join('');

            list.querySelectorAll('.wifi-item').forEach(item => {
                item.onclick = () => {
                    list.querySelectorAll('.wifi-item').forEach(i => i.classList.remove('selected'));
                    item.classList.add('selected');
                    this.wifiSSID = item.dataset.ssid;
                    document.getElementById('wifi-pass-group').classList.remove('hidden');
                    document.getElementById('btn-wifi-connect').classList.remove('hidden');
                    document.getElementById('inp-wifi-pass').focus();
                };
            });
        } catch (e) {
            list.innerHTML = `<div class="msg-error">${this.t('Błąd')}: ${e.message}</div>`;
        }
    },

    async connectWifi() {
        const btn = document.getElementById('btn-wifi-connect');
        const result = document.getElementById('wifi-result');
        const pass = document.getElementById('inp-wifi-pass').value;
        btn.disabled = true;
        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${this.t('Łączenie...')}`;
        result.classList.add('hidden');

        try {
            const r = await fetch('/api/wifi/connect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ssid: this.wifiSSID, password: pass }),
            });
            const data = await r.json();
            result.classList.remove('hidden');
            if (data.ok) {
                result.innerHTML = `<div class="msg-ok"><i class="fas fa-check-circle"></i> ${this.t('Połączono z')} ${this._esc(this.wifiSSID)}${data.ip ? ' — ' + data.ip : ''}</div>`;
                this.networkOk = true;
                this.wifiConnected = true;
            } else {
                result.innerHTML = `<div class="msg-error"><i class="fas fa-times-circle"></i> ${this.t('Nie udało się połączyć')}: ${this._esc(data.message || data.error || '')}</div>`;
            }
        } catch (e) {
            result.classList.remove('hidden');
            result.innerHTML = `<div class="msg-error">${this.t('Błąd')}: ${e.message}</div>`;
        }
        btn.disabled = false;
        btn.innerHTML = `<span>${this.t('Połącz')}</span>`;
    },

    // ── Step 5: Summary ──

    buildSummary() {
        const table = document.getElementById('summary-table');
        const osDiskObj = this.disks.find(d => d.name === this.osDisk);
        const dataDiskObj = this.sameDisk ? osDiskObj : this.disks.find(d => d.name === this.dataDisk);
        const network = this.networkOk
            ? (this.wifiConnected ? `WiFi: ${this.wifiSSID}` : 'Ethernet')
            : this.t('Brak sieci');

        const rows = [
            [this.t('Język'), (FLAGS[this.lang] || '') + ' ' + this.lang.toUpperCase()],
            [this.t('Użytkownik'), this.username],
            [this.t('Nazwa hosta'), this.hostname],
            [this.t('Dysk systemu'), osDiskObj ? `${osDiskObj.model} (${osDiskObj.size_human})` : '—'],
            [this.t('Dysk danych'), this.sameDisk ? this.t('Ten sam dysk') : (dataDiskObj ? `${dataDiskObj.model} (${dataDiskObj.size_human})` : '—')],
            [this.t('Sieć'), network],
        ];
        table.innerHTML = rows.map(([l, v]) =>
            `<div class="summary-row"><div class="summary-label">${l}</div><div class="summary-value">${this._esc(v)}</div></div>`
        ).join('');

        // Update confirm label
        const i18n_confirm = {
            pl: 'INSTALUJ', en: 'INSTALL', de: 'INSTALLIEREN', fr: 'INSTALLER', es: 'INSTALAR'
        };
        const token = i18n_confirm[this.lang] || 'INSTALUJ';
        document.getElementById('confirm-label').textContent = `${this.t('Wpisz INSTALUJ aby potwierdzić')}: ${token}`;
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
                this.showStep(4); // Go back to summary
                return;
            }
            this.pollProgress();
        } catch (e) {
            alert(this.t('Błąd') + ': ' + e.message);
            this.showStep(4);
        }
    },

    pollProgress() {
        if (this.pollTimer) clearInterval(this.pollTimer);
        this.pollTimer = setInterval(async () => {
            try {
                const r = await fetch('/api/install/progress');
                const data = await r.json();
                document.getElementById('progress-bar').style.width = data.percent + '%';
                document.getElementById('progress-pct').textContent = data.percent + '%';
                document.getElementById('progress-msg').textContent = this.t(data.message || '');

                if (data.done) {
                    clearInterval(this.pollTimer);
                    this.pollTimer = null;
                    this.installNewIP = data.new_ip || '—';
                    this.showStep(6); // Done
                    const port = 9000;
                    document.getElementById('done-url').textContent = `http://${this.installNewIP}:${port}`;
                }
                if (data.error) {
                    clearInterval(this.pollTimer);
                    this.pollTimer = null;
                    document.getElementById('progress-msg').innerHTML =
                        `<div class="msg-error">${this.t('Błąd')}: ${this._esc(data.error)}</div>`;
                }
            } catch (e) {
                // Server might be rebooting
            }
        }, 2000);
    },

    // ── Step 7: Done ──

    async reboot() {
        const btn = document.getElementById('btn-reboot');
        btn.disabled = true;
        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${this.t('System zostanie uruchomiony ponownie...')}`;
        try {
            await fetch('/api/install/reboot', { method: 'POST' });
        } catch (e) { /* expected — server shuts down */ }
    },

    // ── Validation ──

    validateStep(n) {
        if (n === 1) return this.validateAccount();
        if (n === 2) return this.validateDisks();
        if (n === 3) return true; // Network is optional (ethernet may be there)
        if (n === 4) return this.validateSummary();
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

    validateDisks() {
        if (!this.osDisk) {
            alert(this.t('Wybierz dysk systemu'));
            return false;
        }
        if (!this.sameDisk && !this.dataDisk) {
            alert(this.t('Wybierz dysk danych'));
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
    },

    // ── Util ──

    _esc(s) {
        const d = document.createElement('div');
        d.textContent = s || '';
        return d.innerHTML;
    },
};

document.addEventListener('DOMContentLoaded', () => Installer.init());
