/* ═══════════════════════════════════════════════════════════
   EthOS — First Boot Setup Wizard (i18n-enabled)
   ═══════════════════════════════════════════════════════════ */

const Setup = {
    step: 0,
    data: { hostname: 'ethos', username: 'nasadmin', password: '', nas_name: 'EthOS', data_disk: '', language: 'pl' },
    wifiConnected: false,
    networkSkipped: false,
};

async function checkSetupNeeded() {
    try {
        const r = await fetch('/api/setup/status');
        const d = await r.json();
        return d.needs_setup === true;
    } catch { return false; }
}

function showSetupWizard() {
    document.getElementById('setup-wizard').classList.remove('hidden');
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('desktop').classList.add('hidden');
    try {
        const params = new URLSearchParams(window.location.search);
        const savedStep = params.get('setup_step');
        if (savedStep) {
            Setup.step = parseInt(savedStep, 10) || 0;
            Setup.wifiConnected = true;
            history.replaceState(null, '', window.location.pathname);
        }
    } catch (e) { /* ignore */ }

    // Preboot installer already handled disk setup — just render
    Setup._installerNeeded = false;
    Setup._installerDone = !!document.cookie; // placeholder; setupCheckInstallerDisk does the real check
    setupRenderStep();
}

function hideSetupWizard() {
    const el = document.getElementById('setup-wizard');
    el.classList.add('fade-out');
    setTimeout(() => el.classList.add('hidden'), 600);
}

function setupRenderStep() {
    const c = document.getElementById('setup-content');
    const steps = [setupStepLanguage, setupStepWelcome, setupStepNetwork, setupStepCredentials, setupStepDisk, setupStepFinish];
    if (Setup.step < 0) Setup.step = 0;
    if (Setup.step >= steps.length) Setup.step = steps.length - 1;
    c.innerHTML = steps[Setup.step]();
    setupBindStep();
}

/* ──────────── Step 0: Language ──────────── */
function setupStepLanguage() {
    const langs = I18n.supportedLangs;
    const cur = Setup.data.language || I18n.lang || 'pl';
    let items = '';
    for (const [code, info] of Object.entries(langs)) {
        const sel = code === cur;
        items += '<div class="setup-lang-option" data-lang="' + code + '" style="display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--bg-secondary);border:2px solid ' + (sel ? 'var(--accent)' : 'var(--border)') + ';border-radius:10px;cursor:pointer;transition:border-color .15s;">'
            + '<span style="font-size:28px;">' + info.flag + '</span>'
            + '<span style="font-size:14px;font-weight:600;flex:1;">' + info.name + '</span>'
            + '<div class="lang-check" style="font-size:20px;color:' + (sel ? 'var(--accent)' : 'var(--bg-tertiary)') + ';"><i class="fas ' + (sel ? 'fa-check-circle' : 'fa-circle') + '"></i></div>'
            + '</div>';
    }
    return '<div class="login-card" style="max-width:480px;">'
        + '<div class="login-logo"><div class="logo-icon"><i class="fas fa-globe"></i></div></div>'
        + '<h1 class="login-hostname">' + t('Wybierz język') + '</h1>'
        + '<p style="color:var(--text-secondary);margin:12px 0 24px;line-height:1.6;">Select system language / ' + t('Wybierz język systemu') + '</p>'
        + '<div id="setup-lang-list" style="display:flex;flex-direction:column;gap:6px;margin-bottom:24px;max-height:45vh;overflow-y:auto;padding-right:4px;">' + items + '</div>'
        + '<button class="btn-login" id="setup-next"><span>' + t('Dalej') + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div>';
}

/* ──────────── Step 1: Welcome ──────────── */
function setupStepWelcome() {
    return '<div class="login-card" style="max-width:480px;">'
        + '<div class="login-logo"><div class="logo-icon"><i class="fas fa-server"></i></div></div>'
        + '<h1 class="login-hostname">' + t('Witaj w EthOS') + '</h1>'
        + '<p style="color:var(--text-secondary);margin:12px 0 28px;line-height:1.6;">'
        + t('Kreator pomoże Ci skonfigurować system NAS.') + '<br>' + t('Ustawisz nazwę hosta, konto administratora i sieć.') + '</p>'
        + setupProgress(1)
        + '<button class="btn-login" id="setup-next"><span>' + t('Rozpocznij') + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div>';
}

/* ──────────── Step 2: Network ──────────── */
function setupStepNetwork() {
    return '<div class="login-card" style="max-width:520px;">'
        + '<h2 style="margin:0 0 6px;font-size:20px;font-weight:700;"><i class="fas fa-wifi" style="margin-right:8px;color:var(--accent);"></i>' + t('Konfiguracja sieci') + '</h2>'
        + '<p style="color:var(--text-secondary);font-size:13px;margin-bottom:20px;">' + t('Połącz się z siecią WiFi lub pomiń ten krok (Ethernet działa automatycznie).') + '</p>'
        + setupProgress(2)
        + '<div id="setup-net-content" style="text-align:left;">'
        + '<div style="text-align:center;padding:20px;color:var(--text-secondary);"><i class="fas fa-spinner fa-spin" style="font-size:24px;"></i><div style="margin-top:10px;">' + t('Szukam interfejsów...') + '</div></div>'
        + '</div>'
        + '<div style="display:flex;gap:10px;margin-top:16px;">'
        + '<button type="button" class="btn-login" id="setup-back" style="flex:1;background:var(--bg-tertiary);"><i class="fas fa-arrow-left"></i> <span>' + t('Wstecz') + '</span></button>'
        + '<button type="button" class="btn-login" id="setup-next" style="flex:2;"><span>' + (Setup.wifiConnected ? t('Dalej') : t('Pomiń')) + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div></div>';
}

/* ──────────── Step 3: Credentials ──────────── */
function setupStepCredentials() {
    return '<div class="login-card" style="max-width:480px;">'
        + '<h2 style="margin:0 0 6px;font-size:20px;font-weight:700;"><i class="fas fa-user-cog" style="margin-right:8px;color:var(--accent);"></i>' + t('Konto administratora') + '</h2>'
        + '<p style="color:var(--text-secondary);font-size:13px;margin-bottom:20px;">' + t('Ustaw nazwę hosta i konto z pełnym dostępem do systemu.') + '</p>'
        + setupProgress(3)
        + '<form id="setup-cred-form" autocomplete="off">'
        + '<div class="form-group"><label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">' + t('Nazwa NAS') + '</label><div class="input-icon"><i class="fas fa-home"></i><input type="text" id="setup-nasname" value="' + esc(Setup.data.nas_name) + '" placeholder="EthOS"></div></div>'
        + '<div class="form-group"><label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">' + t('Hostname') + '</label><div class="input-icon"><i class="fas fa-network-wired"></i><input type="text" id="setup-hostname" value="' + esc(Setup.data.hostname) + '" placeholder="ethos"></div></div>'
        + '<div class="form-group"><label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">' + t('Nazwa użytkownika') + '</label><div class="input-icon"><i class="fas fa-user"></i><input type="text" id="setup-username" value="' + esc(Setup.data.username) + '" placeholder="nasadmin"></div></div>'
        + '<div class="form-group"><label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">' + t('Hasło') + '</label><div class="input-icon"><i class="fas fa-lock"></i><input type="password" id="setup-password" value="' + esc(Setup.data.password) + '" placeholder="' + t('Min. 4 znaki') + '"></div></div>'
        + '<div class="form-group"><label style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;display:block;">' + t('Powtórz hasło') + '</label><div class="input-icon"><i class="fas fa-lock"></i><input type="password" id="setup-password2" placeholder="' + t('Powtórz hasło') + '"></div></div>'
        + '<p class="login-error" id="setup-error"></p>'
        + '<div style="display:flex;gap:10px;">'
        + '<button type="button" class="btn-login" id="setup-back" style="flex:1;background:var(--bg-tertiary);"><i class="fas fa-arrow-left"></i> <span>' + t('Wstecz') + '</span></button>'
        + '<button type="submit" class="btn-login" style="flex:2;"><span>' + t('Dalej') + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div></form></div>';
}

/* ──────────── Step 4: Disk ──────────── */
function setupStepDisk() {
    return '<div class="login-card" style="max-width:520px;">'
        + '<h2 style="margin:0 0 6px;font-size:20px;font-weight:700;"><i class="fas fa-hdd" style="margin-right:8px;color:var(--accent);"></i>' + t('Dysk danych') + '</h2>'
        + '<p style="color:var(--text-secondary);font-size:13px;margin-bottom:20px;">' + t('Wybierz dysk, na którym będą przechowywane dane użytkowników, kopie zapasowe i pliki NAS.') + '</p>'
        + setupProgress(4)
        + '<div id="setup-disk-content" style="text-align:left;"><div style="text-align:center;padding:20px;color:var(--text-secondary);"><i class="fas fa-spinner fa-spin" style="font-size:24px;"></i><div style="margin-top:10px;">' + t('Szukam dysków...') + '</div></div></div>'
        + '<div style="display:flex;gap:10px;margin-top:16px;">'
        + '<button type="button" class="btn-login" id="setup-back" style="flex:1;background:var(--bg-tertiary);"><i class="fas fa-arrow-left"></i> <span>' + t('Wstecz') + '</span></button>'
        + '<button type="button" class="btn-login" id="setup-next" style="flex:2;"><span>' + t('Dalej') + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div></div>';
}

/* ──────────── Step 5: Finish ──────────── */
function setupStepFinish() {
    var diskInfo = Setup.data.data_disk
        ? '<div><i class="fas fa-hdd" style="width:20px;color:var(--accent);"></i> ' + t('Dysk danych:') + ' <b>' + esc(Setup.data.data_disk) + '</b></div>'
        : '<div><i class="fas fa-hdd" style="width:20px;color:var(--text-muted);"></i> ' + t('Dysk danych:') + ' <b>' + t('dysk systemowy') + '</b></div>';
    return '<div class="login-card" style="max-width:480px;">'
        + '<div class="login-logo"><div class="logo-icon" style="background:linear-gradient(135deg,#22c55e,#16a34a);"><i class="fas fa-check"></i></div></div>'
        + '<h1 class="login-hostname">' + t('Gotowe!') + '</h1>'
        + '<p style="color:var(--text-secondary);margin:12px 0 24px;line-height:1.6;">' + t('EthOS jest gotowy do użycia.') + '</p>'
        + setupProgress(5)
        + '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:20px;text-align:left;"><div style="font-size:13px;color:var(--text-secondary);line-height:1.8;">'
        + '<div><i class="fas fa-home" style="width:20px;color:var(--accent);"></i> NAS: <b>' + esc(Setup.data.nas_name) + '</b></div>'
        + '<div><i class="fas fa-network-wired" style="width:20px;color:var(--accent);"></i> Host: <b>' + esc(Setup.data.hostname) + '</b></div>'
        + '<div><i class="fas fa-user" style="width:20px;color:var(--accent);"></i> Admin: <b>' + esc(Setup.data.username) + '</b></div>'
        + diskInfo
        + '</div></div>'
        + '<button class="btn-login" id="setup-finish"><span>' + t('Zaloguj się') + '</span> <i class="fas fa-sign-in-alt"></i></button>'
        + '<div id="setup-finish-status" style="margin-top:12px;"></div></div>';
}

/* ──────────── Progress dots ──────────── */
function setupProgress(current) {
    var labels = [t('Start'), t('Sieć'), t('Konto'), t('Dysk'), t('Gotowe')];
    var last = labels.length - 1;
    var cur = current - 1;
    var out = '<div style="display:flex;justify-content:center;gap:8px;margin-bottom:20px;">';
    for (var i = 0; i <= last; i++) {
        out += '<div style="display:flex;align-items:center;gap:4px;">'
            + '<div style="width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;'
            + (i === cur ? 'background:var(--accent);color:#fff;' : i < cur ? 'background:#22c55e;color:#fff;' : 'background:var(--bg-tertiary);color:var(--text-muted);')
            + '">' + (i < cur ? '<i class="fas fa-check" style="font-size:10px;"></i>' : (i + 1)) + '</div>'
            + '<span style="font-size:11px;color:' + (i === cur ? 'var(--text-primary)' : 'var(--text-muted)') + ';display:' + (i === cur ? 'inline' : 'none') + ';">' + labels[i] + '</span>'
            + '</div>';
        if (i < last) out += '<div style="width:20px;height:2px;background:' + (i < cur ? '#22c55e' : 'var(--bg-tertiary)') + ';align-self:center;"></div>';
    }
    out += '</div>';
    return out;
}

function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function setupReadResponse(r) {
    var raw = await r.text();
    var data = null;
    try { data = raw ? JSON.parse(raw) : {}; } catch (e) { data = null; }
    if (!r.ok) {
        var msg = (data && (data.error || data.message)) || raw || ('HTTP ' + r.status);
        throw new Error(msg);
    }
    return data || {};
}

function setupFriendlyDiskError(msg) {
    var m = (msg || '').toString();
    var ml = m.toLowerCase();

    // Hide low-level /dev and partition internals from end users.
    if (ml.includes('/dev/') || ml.includes('partycj') || ml.includes('mkpart') || ml.includes('mount')) {
        if (ml.includes('brak wystarczającej wolnej przestrzeni')) {
            return t('Na wybranym dysku brakuje wolnego miejsca na utworzenie partycji danych. Wybierz inny dysk.');
        }
        return t('Nie udało się przygotować wybranego dysku. Wybierz inny dysk albo użyj opcji dysku systemowego.');
    }

    if (ml.includes('urządzenie') && ml.includes('nie istnieje')) {
        return t('Wybrany dysk nie jest już dostępny. Odśwież listę i wybierz dysk ponownie.');
    }

    if (ml.includes('szyfrowania') || ml.includes('luks')) {
        return t('Nie udało się skonfigurować szyfrowania dysku. Sprawdź hasło szyfrowania i spróbuj ponownie.');
    }

    return m || t('Wystąpił błąd przygotowania dysku. Spróbuj ponownie.');
}

/* ──────────── Event binding ──────────── */
function setupBindStep() {
    var nextBtn = document.getElementById('setup-next');
    var backBtn = document.getElementById('setup-back');
    var finishBtn = document.getElementById('setup-finish');

    // Language step
    if (Setup.step === 0) {
        var langList = document.getElementById('setup-lang-list');
        if (langList) {
            // Scroll selected language into view
            var selected = langList.querySelector('.setup-lang-option[data-lang="' + (Setup.data.language || 'pl') + '"]');
            if (selected) selected.scrollIntoView({ block: 'center', behavior: 'instant' });

            langList.querySelectorAll('.setup-lang-option').forEach(function(opt) {
                opt.addEventListener('click', async function() {
                    var lang = opt.dataset.lang;
                    Setup.data.language = lang;
                    await setLanguage(lang, false);
                    setupRenderStep();
                });
            });
        }
    }

    if (nextBtn) nextBtn.addEventListener('click', async function() {
        if (Setup.step === 4 && !Setup._diskReady) return;
        // Auto-mount existing data partition when leaving disk step
        if (Setup.step === 4 && Setup._prepMode === 'mount') {
            try {
                nextBtn.disabled = true; nextBtn.style.opacity = '0.5';
                var mr = await fetch('/api/setup/prepare-disk', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'mount', device: Setup._prepDevice, partition: Setup._prepPartition }) });
                var md = await mr.json();
                if (md.error) throw new Error(md.error);
                Setup.data.data_disk = md.mountpoint;
                nextBtn.disabled = false; nextBtn.style.opacity = '1';
            } catch (me) {
                nextBtn.disabled = false; nextBtn.style.opacity = '1';
                var ds = document.getElementById('setup-disk-options');
                if (ds) ds.innerHTML = '<div style="color:#ef4444;font-size:13px;margin-top:8px;"><i class="fas fa-exclamation-circle"></i> ' + esc(setupFriendlyDiskError(me.message)) + '</div>';
                return;
            }
        }
        if (Setup.step === 3) return; // credentials has form submit

        // Auto-skip network
        if (Setup.step === 1) {
            try {
                var r = await fetch('/api/network/interfaces');
                var d = await r.json();
                var connected = (d.interfaces || []).filter(function(i) { return i.name !== 'lo' && (i.state || '').toUpperCase() === 'UP'; });
                if (connected.length > 0) {
                    Setup.wifiConnected = true;
                    Setup.networkSkipped = true;
                    Setup.step = 3;
                    setupRenderStep();
                    return;
                }
            } catch(e) {}
        }
        Setup.step++; setupRenderStep();
    });

    if (backBtn) backBtn.addEventListener('click', function() {
        if (Setup.step === 5 && Setup._diskSkipped) {
            Setup.step = 3; // skip back over disk step
            Setup._diskSkipped = false;
        } else if (Setup.step === 3 && Setup.networkSkipped) {
            Setup.step = 1;
            Setup.networkSkipped = false;
        } else {
            Setup.step--;
        }
        setupRenderStep();
    });

    if (Setup.step === 2) setupLoadNetwork();

    if (Setup.step === 3) {
        var form = document.getElementById('setup-cred-form');
        if (form) form.addEventListener('submit', function(e) {
            e.preventDefault();
            var err = document.getElementById('setup-error');
            var nasname = document.getElementById('setup-nasname').value.trim();
            var hostname = document.getElementById('setup-hostname').value.trim();
            var username = document.getElementById('setup-username').value.trim();
            var pw = document.getElementById('setup-password').value;
            var pw2 = document.getElementById('setup-password2').value;

            if (!username || username.length < 2) { err.textContent = t('Nazwa użytkownika min. 2 znaki'); return; }
            if (!pw || pw.length < 4) { err.textContent = t('Hasło min. 4 znaki'); return; }
            if (pw === 'ethos') { err.textContent = t('Hasło nie może być domyślne ("ethos")'); return; }
            if (pw !== pw2) { err.textContent = t('Hasła nie są identyczne'); return; }

            Setup.data.nas_name = nasname || 'EthOS';
            Setup.data.hostname = hostname || 'ethos';
            Setup.data.username = username;
            Setup.data.password = pw;

            // Auto-skip disk step if installer already prepared a data disk
            setupCheckInstallerDisk().then(function(skip) {
                if (skip) {
                    Setup.step = 5; // skip to Finish
                } else {
                    Setup.step = 4; // go to Disk step
                }
                setupRenderStep();
            });
        });
    }

    if (Setup.step === 4) setupLoadDisks();
    if (finishBtn) finishBtn.addEventListener('click', setupDoFinish);
}

/* ──────────── Network step ──────────── */
async function setupLoadNetwork() {
    var el = document.getElementById('setup-net-content');
    try {
        var [ifData, wifi] = await Promise.all([
            fetch('/api/network/interfaces').then(function(r){return r.json();}),
            fetch('/api/network/wifi/status').then(function(r){return r.json();}).catch(function(){return null;}),
        ]);
        var ifaces = ifData.interfaces || [];
        var html = '';
        var connected = ifaces.filter(function(i){return i.name !== 'lo' && (i.state || '').toUpperCase() === 'UP';});
        if (connected.length) {
            html += '<div style="margin-bottom:16px;"><div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text-primary);"><i class="fas fa-plug" style="color:#22c55e;margin-right:6px;"></i>' + t('Aktywne połączenia') + '</div>';
            for (var iface of connected) {
                var ip = (iface.addresses || []).find(function(a){return a.family === 'inet';});
                ip = ip ? ip.address : '';
                html += '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:6px;display:flex;align-items:center;gap:10px;">'
                    + '<i class="fas ' + (iface.type === 'wifi' ? 'fa-wifi' : 'fa-ethernet') + '" style="color:var(--accent);"></i>'
                    + '<div style="flex:1;"><div style="font-size:13px;font-weight:600;">' + esc(iface.name) + '</div>'
                    + (ip ? '<div style="font-size:12px;color:var(--text-secondary);">' + esc(ip) + '</div>' : '') + '</div>'
                    + '<span style="font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(34,197,94,.15);color:#22c55e;">' + t('połączono') + '</span></div>';
            }
            html += '</div>';
        }
        var shouldScan = false;
        if (wifi && wifi.interface) {
            if (wifi.connected) Setup.wifiConnected = true;
            html += '<div><div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text-primary);"><i class="fas fa-wifi" style="color:var(--accent);margin-right:6px;"></i>' + t('Sieci WiFi') + '</div>'
                + '<div id="setup-wifi-list" style="max-height:260px;overflow-y:auto;"><div style="text-align:center;padding:16px;color:var(--text-secondary);"><i class="fas fa-spinner fa-spin"></i> ' + t('Szukam sieci...') + '</div></div></div>';
            shouldScan = true;
        } else if (!connected.length) {
            html += '<div style="background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);border-radius:10px;padding:16px;font-size:13px;color:var(--text-secondary);"><i class="fas fa-exclamation-triangle" style="color:#f59e0b;margin-right:6px;"></i>'
                + t('Nie wykryto interfejsu WiFi ani połączenia kablowego. Podłącz kabel Ethernet i odśwież.') + '</div>';
        }
        var nextBtn = document.getElementById('setup-next');
        if (nextBtn) {
            var hasNet = connected.length > 0 || Setup.wifiConnected;
            nextBtn.innerHTML = '<span>' + (hasNet ? t('Dalej') : t('Pomiń')) + '</span> <i class="fas fa-arrow-right"></i>';
        }
        el.innerHTML = html;
        if (shouldScan) setupScanWifi();
    } catch (e) {
        el.innerHTML = '<div style="color:#ef4444;font-size:13px;"><i class="fas fa-exclamation-circle"></i> ' + esc(e.message) + '</div>';
    }
}

async function setupScanWifi() {
    try {
        var resp = await fetch('/api/network/wifi/scan', { method: 'POST' }).then(function(r){return r.json();});
        var networks = resp.networks || resp || [];
        var listEl = document.getElementById('setup-wifi-list');
        if (!listEl) return;
        if (!networks.length) {
            listEl.innerHTML = '<div style="padding:12px;color:var(--text-secondary);font-size:13px;">' + t('Nie znaleziono sieci WiFi') + '</div>';
            return;
        }
        var seen = {};
        for (var n of networks) { if (!n.ssid) continue; if (!seen[n.ssid] || n.signal > seen[n.ssid].signal) seen[n.ssid] = n; }
        var unique = Object.values(seen).sort(function(a,b){return b.signal - a.signal;});
        listEl.innerHTML = unique.map(function(n) {
            var bars = n.signal > 70 ? 3 : n.signal > 40 ? 2 : 1;
            return '<div class="setup-wifi-item" data-ssid="' + esc(n.ssid) + '" style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:4px;cursor:pointer;display:flex;align-items:center;gap:10px;transition:background .15s;">'
                + '<i class="fas fa-wifi" style="color:var(--accent);opacity:' + (0.3 + bars * 0.23) + ';"></i>'
                + '<div style="flex:1;font-size:13px;font-weight:500;text-align:left;">' + esc(n.ssid) + '</div>'
                + (n.security && n.security !== '--' ? '<i class="fas fa-lock" style="font-size:10px;color:var(--text-muted);"></i>' : '')
                + '<span style="font-size:11px;color:var(--text-muted);">' + n.signal + '%</span></div>';
        }).join('');
        listEl.innerHTML += '<div style="text-align:center;margin-top:6px;"><button onclick="setupScanWifi()" style="padding:4px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg-secondary);color:var(--text-secondary);cursor:pointer;font-size:12px;"><i class="fas fa-sync-alt"></i> ' + t('Odśwież') + '</button></div>';
        listEl.querySelectorAll('.setup-wifi-item').forEach(function(item) {
            item.addEventListener('mouseenter', function() { item.style.background = 'var(--bg-tertiary)'; });
            item.addEventListener('mouseleave', function() { item.style.background = 'var(--bg-secondary)'; });
            item.addEventListener('click', function() {
                var secured = !!item.querySelector('.fa-lock');
                setupConnectWifi(item.dataset.ssid, secured);
            });
        });
    } catch (e) {
        var listEl = document.getElementById('setup-wifi-list');
        if (listEl) listEl.innerHTML = '<div style="color:#ef4444;font-size:13px;">' + esc(e.message) + '</div>';
    }
}

async function setupAskWifiPassword(ssid) {
    return new Promise(function(resolve) {
        var old = document.getElementById('setup-wifi-pass-backdrop');
        if (old) old.remove();

        var backdrop = document.createElement('div');
        backdrop.id = 'setup-wifi-pass-backdrop';
        backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(2,6,23,.62);display:flex;align-items:center;justify-content:center;z-index:12000;padding:16px;';

        var box = document.createElement('div');
        box.className = 'login-card';
        box.style.maxWidth = '420px';
        box.style.width = '100%';
        box.style.padding = '28px 24px 20px';
        box.style.textAlign = 'left';
        box.innerHTML = ''
            + '<h3 style="margin:0 0 6px;font-size:18px;font-weight:700;color:var(--text-primary);"><i class="fas fa-lock" style="color:var(--accent);margin-right:8px;"></i>' + t('Hasło do sieci') + '</h3>'
            + '<p style="margin:0 0 14px;font-size:12px;color:var(--text-secondary);">' + t('Podaj hasło WiFi:') + ' <b>' + esc(ssid) + '</b></p>'
            + '<div class="input-icon" style="margin-bottom:14px;">'
            + '<i class="fas fa-key"></i>'
            + '<input type="password" id="setup-wifi-pass-input" placeholder="' + t('Hasło') + '" autocomplete="off">'
            + '</div>'
            + '<div style="display:flex;gap:10px;">'
            + '<button type="button" class="btn-login" id="setup-wifi-pass-cancel" style="flex:1;background:var(--bg-tertiary);margin-top:0;">' + t('Anuluj') + '</button>'
            + '<button type="button" class="btn-login" id="setup-wifi-pass-ok" style="flex:1;margin-top:0;"><i class="fas fa-plug"></i> ' + t('Połącz') + '</button>'
            + '</div>';

        backdrop.appendChild(box);
        document.body.appendChild(backdrop);

        var input = box.querySelector('#setup-wifi-pass-input');
        var btnCancel = box.querySelector('#setup-wifi-pass-cancel');
        var btnOk = box.querySelector('#setup-wifi-pass-ok');

        function closeWith(val) {
            if (backdrop && backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
            resolve(val);
        }

        backdrop.addEventListener('click', function(e) {
            if (e.target === backdrop) closeWith(null);
        });
        btnCancel.addEventListener('click', function() { closeWith(null); });
        btnOk.addEventListener('click', function() { closeWith(input.value || ''); });
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); closeWith(input.value || ''); }
            if (e.key === 'Escape') { e.preventDefault(); closeWith(null); }
        });

        setTimeout(function() { if (input) input.focus(); }, 0);
    });
}

async function setupConnectWifi(ssid, secured) {
    var password = '';
    if (secured) {
        password = await setupAskWifiPassword(ssid);
        if (password === null) return;
    }
    var listEl = document.getElementById('setup-wifi-list');
    if (listEl) listEl.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-secondary);"><i class="fas fa-spinner fa-spin"></i> ' + t('Łączę z') + ' ' + esc(ssid) + '...</div>';
    try {
        var r = await fetch('/api/network/wifi/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ssid: ssid, password: password }) });
        var data = await r.json();
        if (data.error) throw new Error(data.error);
        Setup.wifiConnected = true;
        var newIp = data.new_ip;
        var hostname = data.hostname || 'ethos';
        var el = document.getElementById('setup-net-content');
        if (el && newIp) {
            var newUrl = 'http://' + newIp + ':9000';
            var localUrl = 'http://' + hostname + '.local:9000';
            var countdown = 15;
            el.innerHTML = '<div style="text-align:center;padding:20px;">'
                + '<div style="font-size:40px;color:#22c55e;margin-bottom:12px;"><i class="fas fa-check-circle"></i></div>'
                + '<div style="font-size:16px;font-weight:700;color:var(--text-primary);margin-bottom:8px;">' + t('Połączono z') + ' ' + esc(ssid) + '</div>'
                + '<div style="font-size:13px;color:var(--text-secondary);margin-bottom:16px;">' + t('Hotspot \u201eethos\u201d został wyłączony.') + '<br>' + t('Połącz swoje urządzenie z siecią') + ' <b>' + esc(ssid) + '</b> ' + t('i otwórz:') + '</div>'
                + '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:8px;">'
                + '<div style="font-size:14px;font-weight:600;color:var(--accent);word-break:break-all;"><a href="' + esc(newUrl) + '" style="color:var(--accent);text-decoration:none;">' + esc(newUrl) + '</a></div>'
                + '<div style="font-size:12px;color:var(--text-muted);margin-top:4px;">' + t('lub') + ' <a href="' + esc(localUrl) + '" style="color:var(--accent);text-decoration:none;">' + esc(localUrl) + '</a></div></div>'
                + '<div style="font-size:12px;color:var(--text-muted);" id="setup-redirect-msg">' + t('Przekierowanie za') + ' <span id="setup-redirect-cd">' + countdown + '</span> ' + t('sek...') + '</div></div>';
            var btns = el.closest('.login-card');
            if (btns) { var b = btns.querySelector('div:last-child'); if (b && b.querySelector('#setup-next')) b.style.display = 'none'; }
            var cdInterval = setInterval(function() {
                countdown--;
                var cdEl = document.getElementById('setup-redirect-cd');
                if (cdEl) cdEl.textContent = countdown;
                if (countdown <= 0) { clearInterval(cdInterval); window.location.href = newUrl + '?setup_step=3'; }
            }, 1000);
        } else {
            setupLoadNetwork();
            var nextBtn = document.getElementById('setup-next');
            if (nextBtn) nextBtn.innerHTML = '<span>' + t('Dalej') + '</span> <i class="fas fa-arrow-right"></i>';
        }
    } catch (e) {
        if (listEl) listEl.innerHTML = '<div style="color:#ef4444;padding:12px;font-size:13px;"><i class="fas fa-times-circle"></i> ' + esc(e.message) + '<br><button onclick="setupScanWifi()" style="margin-top:8px;padding:6px 16px;border:1px solid var(--border);border-radius:8px;background:var(--bg-secondary);color:var(--text-primary);cursor:pointer;">' + t('Spróbuj ponownie') + '</button></div>';
    }
}

/* ──────────── Auto-skip disk step if installer already prepared data disk ──────────── */
async function setupCheckInstallerDisk() {
    try {
        // Prefer installer_result.json (v2 handover contract)
        var resultResp = await fetch('/api/installer/result');
        if (resultResp.ok) {
            var result = await resultResp.json();
            if (result.version >= 2 && result.data_devices && result.data_devices.length) {
                var r2 = await fetch('/api/setup/disks');
                var resp = await r2.json();
                var disks = resp.disks || [];

                // Build set of expected labels from installer result
                var expectedLabels = {};
                for (var ei = 0; ei < result.data_devices.length; ei++) {
                    expectedLabels[result.data_devices[ei].label] = true;
                }

                for (var i = 0; i < disks.length; i++) {
                    var disk = disks[i];
                    var parts = disk.partitions || [];
                    for (var j = 0; j < parts.length; j++) {
                        if (!expectedLabels[parts[j].label]) continue;
                        if (parts[j].mountpoint) {
                            Setup.data.data_disk = parts[j].mountpoint;
                            Setup._diskSkipped = true;
                            return true;
                        }
                        // Not mounted — auto-mount
                        try {
                            var mr = await fetch('/api/setup/prepare-disk', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ mode: 'mount', device: disk.device, partition: parts[j].device })
                            });
                            var md = await mr.json();
                            if (!md.error && md.mountpoint) {
                                Setup.data.data_disk = md.mountpoint;
                                Setup._diskSkipped = true;
                                return true;
                            }
                        } catch (e) { /* Fall through */ }
                    }
                }
                return false;
            }
        }

        // Fallback: legacy flow using installer status
        var r = await fetch('/api/installer/status');
        var d = await r.json();
        if (d.status !== 'done' || !d.data_disks || !d.data_disks.length) return false;

        var r2 = await fetch('/api/setup/disks');
        var resp = await r2.json();
        var disks = resp.disks || [];
        var selected = {};
        for (var si = 0; si < d.data_disks.length; si++) selected[d.data_disks[si]] = true;

        for (var i = 0; i < disks.length; i++) {
            var disk = disks[i];
            if (!selected[disk.device]) continue;

            var parts = disk.partitions || [];

            for (var j = 0; j < parts.length; j++) {
                if ((parts[j].label || '').indexOf('EthOS-Data') < 0) continue;
                if (parts[j].mountpoint) {
                    Setup.data.data_disk = parts[j].mountpoint;
                    Setup._diskSkipped = true;
                    return true;
                }
                try {
                    var mr = await fetch('/api/setup/prepare-disk', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ mode: 'mount', device: disk.device, partition: parts[j].device })
                    });
                    var md = await mr.json();
                    if (!md.error && md.mountpoint) {
                        Setup.data.data_disk = md.mountpoint;
                        Setup._diskSkipped = true;
                        return true;
                    }
                } catch (e) { /* Fall through */ }
            }
        }
        return false;
    } catch (e) { return false; }
}

/* ──────────── Disk selection step ──────────── */
async function setupLoadDisks() {
    var el = document.getElementById('setup-disk-content');
    if (!el) return;
    try {
        var r = await fetch('/api/setup/disks');
        var resp = await r.json();
        var disks = resp.disks || [];
        var sysDisk = disks.find(function(d) { return d.is_system; });
        var extDisks = disks.filter(function(d) { return !d.is_system; });
        var fmtSz = function(b) { return b >= 1e12 ? (b/1e12).toFixed(1)+' TB' : b >= 1e9 ? (b/1e9).toFixed(1)+' GB' : (b/1e6).toFixed(0)+' MB'; };

        // Build unified option list
        // Each: { choice, device, partition?, diskObj?, label, sublabel, icon, iconColor, needsPrep, recommended }
        var options = [];

        // 1) Existing EthOS-Data partition on system disk
        if (sysDisk && sysDisk.existing_data_partition) {
            var edp = sysDisk.existing_data_partition;
            options.push({
                choice: 'sysexist', device: sysDisk.device, partition: edp.device,
                label: t('Istniejąca partycja danych') + ' \u2014 ' + fmtSz(edp.size),
                sublabel: edp.device + ' \u00b7 EthOS-Data \u00b7 ' + (edp.fstype || 'ext4'),
                icon: 'fa-database', iconColor: '#22c55e', needsPrep: false, recommended: true
            });
        }

        // 2) Free space on system disk (only if no existing data partition and >1 GB free)
        if (sysDisk && !sysDisk.existing_data_partition && sysDisk.free_space > 1000000000) {
            options.push({
                choice: 'sysfree', device: sysDisk.device,
                label: t('Utwórz partycję danych') + ' \u2014 ' + fmtSz(sysDisk.free_space),
                sublabel: sysDisk.device + ' \u00b7 ' + t('wolne miejsce na dysku systemowym'),
                icon: 'fa-plus-circle', iconColor: '#3b82f6', needsPrep: true, recommended: true
            });
        }

        // 3) External disks
        for (var i = 0; i < extDisks.length; i++) {
            var d = extDisks[i];
            var nm = d.model || d.name;
            var icon = d.removable ? 'fa-usb' : d.transport === 'nvme' ? 'fa-bolt' : 'fa-hdd';
            var clr = d.removable ? '#a78bfa' : '#f59e0b';
            var tags = '';
            if (d.removable) tags += ' USB';
            if (d.transport) tags += ' ' + d.transport.toUpperCase();
            var pdesc = d.usable_mountpoint ? t('gotowy do użycia') : t('wymagane formatowanie');
            options.push({
                choice: 'ext', device: d.device, diskObj: d,
                label: nm + ' \u00b7 ' + fmtSz(d.size),
                sublabel: d.device + (tags ? ' \u00b7' + tags : '') + ' \u00b7 ' + pdesc,
                icon: icon, iconColor: clr,
                needsPrep: !d.usable_mountpoint, recommended: false
            });
        }

        // 4) System directory fallback (no separate partition)
        options.push({
            choice: 'sysdir', device: '',
            label: t('Użyj dysku systemowego'),
            sublabel: t('Dane w katalogu instalacyjnym EthOS \u2014 zalecane tylko tymczasowo'),
            icon: 'fa-folder', iconColor: 'var(--text-muted)',
            needsPrep: false, recommended: false
        });

        // Render option cards
        var html = '<div id="setup-disk-list" style="display:flex;flex-direction:column;gap:6px;">';
        for (var oi = 0; oi < options.length; oi++) {
            var opt = options[oi];
            var sel = oi === 0;
            var recBadge = opt.recommended ? '<span style="font-size:10px;padding:1px 8px;border-radius:8px;background:rgba(34,197,94,.12);color:#22c55e;margin-left:6px;">' + t('zalecane') + '</span>' : '';
            html += '<div class="setup-disk-option" data-idx="' + oi + '" style="display:flex;align-items:center;gap:12px;padding:12px 14px;background:var(--bg-secondary);border:2px solid ' + (sel ? 'var(--accent)' : 'var(--border)') + ';border-radius:10px;cursor:pointer;transition:border-color .15s;">'
                + '<div style="font-size:20px;color:' + opt.iconColor + ';width:28px;text-align:center;"><i class="fas ' + opt.icon + '"></i></div>'
                + '<div style="flex:1;min-width:0;"><div style="font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(opt.label) + recBadge + '</div><div style="font-size:11px;color:var(--text-muted);">' + esc(opt.sublabel) + '</div></div>'
                + '<div class="disk-check" style="font-size:20px;color:' + (sel ? 'var(--accent)' : 'var(--bg-tertiary)') + ';"><i class="fas ' + (sel ? 'fa-check-circle' : 'fa-circle') + '"></i></div></div>';
        }
        html += '</div><div id="setup-disk-options" style="margin-top:12px;"></div>';
        el.innerHTML = html;

        Setup._diskOptions = options;
        Setup._selectedIdx = 0;

        function selectOption(idx) {
            Setup._selectedIdx = idx;
            el.querySelectorAll('.setup-disk-option').forEach(function(o, i) {
                var isSel = i === idx;
                o.style.borderColor = isSel ? 'var(--accent)' : 'var(--border)';
                var chk = o.querySelector('.disk-check');
                if (chk) { chk.style.color = isSel ? 'var(--accent)' : 'var(--bg-tertiary)'; chk.innerHTML = '<i class="fas ' + (isSel ? 'fa-check-circle' : 'fa-circle') + '"></i>'; }
            });
            renderDiskSubOptions(options[idx]);
        }

        function renderDiskSubOptions(opt) {
            var optEl = document.getElementById('setup-disk-options');
            if (!optEl) return;

            // ── sysexist: just mount existing partition ──
            if (opt.choice === 'sysexist') {
                Setup._prepMode = 'mount';
                Setup._prepDevice = opt.device;
                Setup._prepPartition = opt.partition;
                Setup.data.data_disk = '/mnt/data';
                Setup._diskReady = true;
                optEl.innerHTML = '<div style="background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.2);border-radius:10px;padding:12px 16px;font-size:13px;color:var(--text-secondary);"><i class="fas fa-check-circle" style="color:#22c55e;margin-right:6px;"></i>' + t('Partycja zostanie zamontowana automatycznie.') + '</div>';
                updNext();
                return;
            }

            // ── sysdir: no separate partition ──
            if (opt.choice === 'sysdir') {
                Setup._prepMode = null;
                Setup.data.data_disk = '';
                Setup._diskReady = true;
                optEl.innerHTML = '';
                updNext();
                return;
            }

            // ── sysfree: create new partition from free space ──
            if (opt.choice === 'sysfree') {
                Setup._prepMode = 'syspart';
                Setup._prepDevice = opt.device;
                Setup.data.data_disk = '';
                Setup._diskReady = false;
                optEl.innerHTML = '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:16px;">'
                    + '<div style="font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text-primary);"><i class="fas fa-cog" style="color:var(--accent);margin-right:6px;"></i>' + t('Utwórz partycję danych') + '</div>'
                    + '<div style="font-size:12px;color:var(--text-muted);margin-bottom:12px;">' + t('Zostanie utworzona nowa partycja EthOS-Data z wolnego miejsca na dysku. Partycja systemowa nie zostanie naruszona.') + '</div>'
                    + setupDiskEncryptionHTML()
                    + '<button type="button" id="setup-disk-prepare-btn" onclick="setupPrepareDisk()" style="margin-top:10px;padding:10px 20px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:13px;font-weight:600;cursor:pointer;width:100%;"><i class="fas fa-plus-circle"></i> ' + t('Utwórz partycję') + '</button>'
                    + '<div id="setup-disk-status" style="margin-top:8px;"></div></div>';
                setupBindEncryptionToggle();
                updNext();
                return;
            }

            // ── ext: external disk ──
            if (opt.choice === 'ext') {
                var disk = opt.diskObj;
                var hasUsable = !!disk.usable_mountpoint;
                var usableFs = ((disk.partitions.find(function(p) { return p.mountpoint && ['ext4', 'xfs', 'btrfs'].includes(p.fstype); })) || {}).fstype || '';
                Setup._prepMode = 'format';
                Setup._prepDevice = opt.device;

                optEl.innerHTML = '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:16px;">'
                    + '<div style="font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text-primary);"><i class="fas fa-cog" style="color:var(--accent);margin-right:6px;"></i>' + t('Przygotowanie dysku') + '</div>'
                    + '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px;">'
                    + (hasUsable ? '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;"><input type="radio" name="disk_mode" value="asis" checked><span>' + t('Użyj istniejącej partycji') + ' (' + esc(usableFs) + ')</span></label>' : '')
                    + '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;"><input type="radio" name="disk_mode" value="format" ' + (hasUsable ? '' : 'checked') + '><span>' + t('Formatuj cały dysk (ext4)') + '</span><span style="color:#ef4444;font-size:11px;margin-left:4px;"><i class="fas fa-exclamation-triangle"></i> ' + t('kasuje dane') + '</span></label></div>'
                    + '<div id="setup-fmt-panel" style="' + (hasUsable ? 'display:none;' : '') + '">'
                    + setupDiskEncryptionHTML()
                    + '<button type="button" id="setup-disk-prepare-btn" onclick="setupPrepareDisk()" style="margin-top:10px;padding:10px 20px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:13px;font-weight:600;cursor:pointer;width:100%;"><i class="fas fa-magic"></i> ' + t('Przygotuj dysk') + '</button></div>'
                    + '<div id="setup-disk-status" style="margin-top:8px;"></div></div>';

                document.querySelectorAll('input[name="disk_mode"]').forEach(function(radio) {
                    radio.addEventListener('change', function() {
                        var fmtPanel = document.getElementById('setup-fmt-panel');
                        if (radio.value === 'format') {
                            if (fmtPanel) fmtPanel.style.display = '';
                            Setup._diskReady = false; Setup.data.data_disk = '';
                        } else {
                            if (fmtPanel) fmtPanel.style.display = 'none';
                            Setup._diskReady = true; Setup.data.data_disk = disk.usable_mountpoint;
                        }
                        updNext();
                    });
                });
                setupBindEncryptionToggle();
                if (hasUsable) { Setup._diskReady = true; Setup.data.data_disk = disk.usable_mountpoint; }
                else { Setup._diskReady = false; Setup.data.data_disk = ''; }
                updNext();
                return;
            }
        }

        function updNext() {
            var btn = document.getElementById('setup-next');
            if (!btn) return;
            btn.disabled = !Setup._diskReady;
            btn.style.opacity = Setup._diskReady ? '1' : '0.5';
        }

        el.querySelectorAll('.setup-disk-option').forEach(function(o, idx) {
            o.addEventListener('click', function() { selectOption(idx); });
        });
        selectOption(0);
    } catch (e) {
        el.innerHTML = '<div style="color:#ef4444;font-size:13px;"><i class="fas fa-exclamation-circle"></i> ' + t('Błąd ładowania dysków:') + ' ' + esc(e.message) + '</div>';
    }
}

/* Helper: encryption options HTML block */
function setupDiskEncryptionHTML() {
    return '<div style="padding:10px 0;border-top:1px solid var(--border);"><label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;"><input type="checkbox" id="disk-encrypt-cb"><span><i class="fas fa-lock" style="font-size:11px;color:var(--accent);"></i> ' + t('Szyfrowanie danych (LUKS)') + '</span></label>'
        + '<div id="disk-pass-row" style="display:none;margin-top:8px;padding-left:24px;"><input type="password" id="disk-passphrase" placeholder="' + t('Hasło szyfrowania (min. 4 znaki)') + '" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-primary);color:var(--text-primary);font-size:13px;box-sizing:border-box;">'
        + '<div style="font-size:11px;color:var(--text-muted);margin-top:4px;"><i class="fas fa-info-circle"></i> ' + t('Dysk odblokuje się automatycznie przy starcie. Hasło służy do awaryjnego odblokowania.') + '</div></div></div>';
}

/* Helper: bind encryption checkbox toggle */
function setupBindEncryptionToggle() {
    var encCb = document.getElementById('disk-encrypt-cb');
    if (encCb) encCb.addEventListener('change', function() {
        var row = document.getElementById('disk-pass-row');
        if (row) row.style.display = encCb.checked ? '' : 'none';
    });
}

async function setupPrepareDisk() {
    var btn = document.getElementById('setup-disk-prepare-btn');
    var status = document.getElementById('setup-disk-status');
    var mode = Setup._prepMode;
    var device = Setup._prepDevice;
    var encrypt = (document.getElementById('disk-encrypt-cb') || {}).checked || false;
    var passphrase = (document.getElementById('disk-passphrase') || {}).value || '';
    if (!device || !mode) return;

    if (encrypt && (!passphrase || passphrase.length < 4)) {
        if (status) status.innerHTML = '<div style="color:#ef4444;font-size:13px;"><i class="fas fa-exclamation-circle"></i> ' + t('Hasło szyfrowania musi mieć min. 4 znaki') + '</div>';
        return;
    }

    var confirmMsg;
    if (mode === 'syspart') {
        confirmMsg = t('Utworzyć partycję danych z wolnego miejsca na dysku') + ' ' + device + '?\n' + t('Partycja systemowa nie zostanie naruszona.');
    } else {
        confirmMsg = t('UWAGA: Wszystkie dane na dysku') + ' ' + device + ' ' + t('zostaną NIEODWRACALNIE usunięte!') + '\n\n' + t('Czy chcesz kontynuować?');
    }
    if (!await confirmDialog(confirmMsg)) return;

    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Przygotowuję...'); }
    var statusMsg = mode === 'syspart'
        ? t('Tworzenie partycji, formatowanie') + (encrypt ? ', ' + t('szyfrowanie') : '')
        : t('Partycjonowanie, formatowanie') + (encrypt ? ', ' + t('szyfrowanie') : '') + ' (' + t('może potrwać minutę') + ')';
    if (status) status.innerHTML = '<div style="color:var(--text-secondary);font-size:13px;"><i class="fas fa-spinner fa-spin"></i> ' + statusMsg + '</div>';

    try {
        var body = { device: device, mode: mode, encrypt: encrypt, passphrase: passphrase };
        var r = await fetch('/api/setup/prepare-disk', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        var data = await setupReadResponse(r);
        if (data.error) throw new Error(data.error);
        Setup.data.data_disk = data.mountpoint;
        Setup._diskReady = true;
        if (status) status.innerHTML = '<div style="color:#22c55e;font-size:13px;"><i class="fas fa-check-circle"></i> ' + t('Dysk przygotowany \u2014 zamontowany w') + ' ' + esc(data.mountpoint) + (data.encrypted ? ' <i class="fas fa-lock" style="font-size:11px;"></i> ' + t('zaszyfrowany') : '') + '</div>';
        if (btn) btn.style.display = 'none';
        var nextBtn = document.getElementById('setup-next');
        if (nextBtn) { nextBtn.disabled = false; nextBtn.style.opacity = '1'; }
    } catch (e) {
        if (status) status.innerHTML = '<div style="color:#ef4444;font-size:13px;"><i class="fas fa-exclamation-circle"></i> ' + esc(setupFriendlyDiskError(e.message)) + '</div>';
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-magic"></i> ' + t('Przygotuj dysk'); }
    }
}

/* ──────────── Finish ──────────── */
async function setupDoFinish() {
    var btn = document.getElementById('setup-finish');
    var status = document.getElementById('setup-finish-status');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> '+t('Konfiguruję...'); }
    var progressTimer = null;

    function renderSetupProgress(p) {
        if (!status) return;
        var msg = p && p.message ? p.message : t('Konfiguruję...');
        var elapsed = p && typeof p.elapsed === 'number' ? p.elapsed : 0;
        status.innerHTML = '<div style="color:var(--text-secondary);font-size:13px;"><i class="fas fa-spinner fa-spin"></i> ' + esc(msg) + ' <span style="opacity:.8;">(' + elapsed + 's)</span></div>';
    }

    async function pollSetupProgress() {
        try {
            var pr = await fetch('/api/setup/progress');
            var pd = await pr.json();
            if (pd && (pd.active || pd.message)) renderSetupProgress(pd);
        } catch (e) {
            // ignore polling errors during setup
        }
    }

    renderSetupProgress({ message: t('Konfiguruję...'), elapsed: 0 });
    progressTimer = setInterval(pollSetupProgress, 800);
    pollSetupProgress();
    try {
        var r = await fetch('/api/setup/complete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Setup.data) });
        var data = await setupReadResponse(r);
        if (data.error) throw new Error(data.error);
        if (status) status.innerHTML = '<div style="color:#22c55e;font-size:13px;"><i class="fas fa-check-circle"></i> '+t('Konfiguracja zakończona!')+'</div>';
        setTimeout(function() {
            hideSetupWizard();
            document.getElementById('login-screen').classList.remove('hidden');
            var hostEl = document.getElementById('login-hostname');
            if (hostEl) hostEl.textContent = Setup.data.nas_name;
            translateDOM();
        }, 1500);
    } catch (e) {
        if (status) status.innerHTML = '<div style="color:#ef4444;font-size:13px;"><i class="fas fa-exclamation-circle"></i> '+esc(setupFriendlyDiskError(e.message))+'</div>';
        if (btn) { btn.disabled = false; btn.innerHTML = '<span>'+t('Zaloguj się')+'</span> <i class="fas fa-sign-in-alt"></i>'; }
    } finally {
        if (progressTimer) clearInterval(progressTimer);
    }
}
