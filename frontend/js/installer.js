/* ═══════════════════════════════════════════════════════════
   EthOS — System Installer Wizard v2 (pre-setup phase)
   Runs BEFORE the Setup Wizard. Decides where the OS lives.
   Self-contained: own step counter, own render, own binding.
   ───────────────────────────────────────────────────────────
   Architecture v2 highlights:
   - Persistent device IDs (/dev/disk/by-id/) as canonical refs
   - Role-based drive assignment (system vs data)
   - SMART pre-flight status
   - Typed confirmation token ("INSTALUJ") instead of confirm()
   - installer_result.json handover contract
   ═══════════════════════════════════════════════════════════ */

const Installer = {
    step: 0,
    devices: [],
    devicesById: {},        // {persistent_id: device_obj}
    bootMedium: '',         // persistent ID of boot medium
    bootSourceDisk: '',
    bootedFromUsb: false,
    platform: '',           // 'x86_64' | 'aarch64' | ...
    hasEfi: false,
    strategy: '',           // 'usb' | 'internal'
    targetDevice: '',       // persistent ID (not /dev/sdX)
    dataDisks: [],          // [persistent_id, ...]
    encrypt: false,
    passphrase: '',
    validated: false,
    summary: '',
    plan: null,             // structured plan from validation
    warnings: [],
    installing: false,
    installDone: false,
    pollTimer: null,
};

/* ─────────── Check if installer is needed ─────────── */
async function checkInstallerNeeded() {
    try {
        var r = await fetch('/api/installer/status');
        if (!r.ok) return false;
        var d = await r.json();
        // If already done/running, no need to show installer
        if (d.status === 'done') return false;
        return true;
    } catch { return false; }
}

/* ─────────── Show / render installer wizard ─────────── */
function showInstallerWizard() {
    Installer.step = 0;
    installerRenderStep();
}

function installerRenderStep() {
    var c = document.getElementById('setup-content');
    var steps = [installerStepStrategy, installerStepDrives, installerStepSummary];
    if (Installer.step < 0) Installer.step = 0;
    if (Installer.step >= steps.length) Installer.step = steps.length - 1;
    c.innerHTML = steps[Installer.step]();
    _installerBindButtons();
}

/* ─────────── Progress dots (installer-specific) ─────────── */
function installerProgress(current) {
    var labels = [t('Strategia'), t('Dyski'), t('Instalacja')];
    var last = labels.length - 1;
    var out = '<div style="display:flex;justify-content:center;gap:8px;margin-bottom:20px;">';
    for (var i = 0; i <= last; i++) {
        out += '<div style="display:flex;align-items:center;gap:4px;">'
            + '<div style="width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;'
            + (i === current ? 'background:var(--accent);color:#fff;' : i < current ? 'background:#22c55e;color:#fff;' : 'background:var(--bg-tertiary);color:var(--text-muted);')
            + '">' + (i < current ? '<i class="fas fa-check" style="font-size:10px;"></i>' : (i + 1)) + '</div>'
            + '<span style="font-size:11px;color:' + (i === current ? 'var(--text-primary)' : 'var(--text-muted)') + ';display:' + (i === current ? 'inline' : 'none') + ';">' + labels[i] + '</span>'
            + '</div>';
        if (i < last) out += '<div style="width:20px;height:2px;background:' + (i < current ? '#22c55e' : 'var(--bg-tertiary)') + ';align-self:center;"></div>';
    }
    out += '</div>';
    return out;
}

/* ─────────── Button binding (shared for all installer steps) ─────────── */
function _installerBindButtons() {
    var nextBtn = document.getElementById('setup-next');
    var backBtn = document.getElementById('setup-back');

    if (nextBtn) nextBtn.addEventListener('click', function() {
        // Step-specific validation
        if (Installer.step === 0 && !Installer.strategy) return;
        if (Installer.step === 1 && !installerValidateDrives()) return;
        if (Installer.step === 2) {
            // Summary step — Next only appears after install is done
            // Transition to regular setup wizard (skip Language + Welcome already shown)
            Setup._installerNeeded = false;
            Setup.step = 2;
            setupRenderStep();
            return;
        }
        Installer.step++;
        installerRenderStep();
    });

    if (backBtn) backBtn.addEventListener('click', function() {
        Installer.step--;
        installerRenderStep();
    });

    // Step-specific async loading
    if (Installer.step === 0) installerLoadStrategy();
    if (Installer.step === 1) installerLoadDrives();
    if (Installer.step === 2) installerLoadSummary();
}

/* ─────────── Step: Boot Strategy ─────────── */
function installerStepStrategy() {
    return '<div class="login-card" style="max-width:560px;">'
        + '<h2 style="margin:0 0 6px;font-size:20px;font-weight:700;">'
        + '<i class="fas fa-hdd" style="margin-right:8px;color:var(--accent);"></i>'
        + t('Strategia bootowania') + '</h2>'
        + '<p style="color:var(--text-secondary);font-size:13px;margin-bottom:20px;">'
        + t('Zdecyduj, gdzie ma pracować system operacyjny EthOS.') + '</p>'
        + installerProgress(0)
        + '<div id="installer-devices-loading" style="text-align:center;padding:20px;color:var(--text-secondary);">'
        + '<i class="fas fa-spinner fa-spin" style="font-size:24px;"></i>'
        + '<div style="margin-top:10px;">' + t('Wykrywam nośniki...') + '</div></div>'
        + '<div id="installer-strategy-content" style="display:none;"></div>'
        + '<div style="display:flex;gap:10px;margin-top:16px;">'
        + '<button type="button" class="btn-login" id="setup-back" style="flex:1;background:var(--bg-tertiary);">'
        + '<i class="fas fa-arrow-left"></i> <span>' + t('Wstecz') + '</span></button>'
        + '<button type="button" class="btn-login" id="setup-next" style="flex:2;" disabled>'
        + '<span>' + t('Dalej') + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div></div>';
}

async function installerLoadStrategy() {
    var loading = document.getElementById('installer-devices-loading');
    var content = document.getElementById('installer-strategy-content');
    if (!content) return;

    try {
        var r = await fetch('/api/installer/discover');
        var data = await r.json();
        if (data.error) throw new Error(data.error);

        Installer.devices = data.devices || [];
        Installer.bootSourceDisk = data.boot_source_disk || '';
        Installer.bootedFromUsb = data.booted_from_usb || false;
        Installer.bootMedium = data.boot_medium || '';
        Installer.platform = data.platform || '';
        Installer.hasEfi = data.has_efi || false;

        // Build lookup by persistent ID
        Installer.devicesById = {};
        for (var k = 0; k < Installer.devices.length; k++) {
            var dk = Installer.devices[k];
            Installer.devicesById[dk.id || dk.device] = dk;
        }

        var fmtSz = function(gb) { return gb >= 1000 ? (gb/1000).toFixed(1) + ' TB' : gb.toFixed(0) + ' GB'; };

        // Detect boot source label
        var bootDev = Installer.devices.find(function(d) { return d.is_boot_medium || d.is_boot_source; });
        var bootLabel = bootDev ? (bootDev.model + ' · ' + fmtSz(bootDev.size_gb)) : 'USB';

        // Internal disks (eligible for system role)
        var internalDisks = Installer.devices.filter(function(d) {
            return d.eligible_roles && d.eligible_roles.indexOf('system') >= 0;
        });
        var hasInternal = internalDisks.length > 0;

        // Strategy cards
        var html = '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:16px;">';

        // Option A: Keep on USB
        if (Installer.bootedFromUsb) {
            var usbSel = Installer.strategy === 'usb';
            html += _installerStrategyCard('usb', usbSel,
                'fa-usb', '#a78bfa',
                t('Pozostaw na USB'),
                t('System pracuje z pendrive\'a.') + ' ' + bootLabel,
                ''
            );
        }

        // Option B: Install to Internal Drive
        var intSel = Installer.strategy === 'internal';
        html += _installerStrategyCard('internal', intSel,
            'fa-server', '#3b82f6',
            t('Zainstaluj na dysku wewnętrznym'),
            hasInternal
                ? t('Przenieś system na dysk SATA/NVMe — szybciej i stabilniej.')
                : t('Brak dysków wewnętrznych — podłącz dysk SATA/NVMe.'),
            hasInternal ? '' : 'disabled'
        );

        html += '</div>';

        // Show detected devices summary with SMART status
        html += '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:8px;">'
            + '<div style="font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:8px;">'
            + '<i class="fas fa-list" style="color:var(--accent);margin-right:6px;"></i>'
            + t('Wykryte nośniki') + ' (' + Installer.devices.length + ')</div>';

        for (var i = 0; i < Installer.devices.length; i++) {
            var d = Installer.devices[i];
            var icon = d.is_usb ? 'fa-usb' : d.transport === 'nvme' ? 'fa-bolt' : 'fa-hdd';
            var clr = d.is_usb ? '#a78bfa' : d.transport === 'nvme' ? '#f59e0b' : '#60a5fa';
            var tags = '';
            if (d.is_boot_medium) tags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(34,197,94,.12);color:#22c55e;">BOOT</span>';
            if (d.is_raid_member) tags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(239,68,68,.12);color:#ef4444;">RAID</span>';
            if (d.smart_status === 'FAIL') {
                tags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(239,68,68,.12);color:#ef4444;"><i class="fas fa-heart-broken"></i> SMART FAIL</span>';
            } else if (d.smart_status === 'OK' && d.temperature_c && d.temperature_c > 50) {
                tags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(245,158,11,.12);color:#f59e0b;"><i class="fas fa-thermometer-half"></i> ' + d.temperature_c + '°C</span>';
            }
            if (d.connection_type) tags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(96,165,250,.1);color:#60a5fa;">' + esc(d.connection_type) + '</span>';

            html += '<div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;">'
                + '<i class="fas ' + icon + '" style="color:' + clr + ';width:16px;text-align:center;"></i>'
                + '<span style="flex:1;color:var(--text-primary);">' + esc(d.model) + '</span>'
                + '<span style="color:var(--text-muted);">' + fmtSz(d.size_gb) + '</span>'
                + tags + '</div>';
        }
        html += '</div>';

        if (loading) loading.style.display = 'none';
        content.style.display = '';
        content.innerHTML = html;

        // Bind strategy card clicks
        content.querySelectorAll('.installer-strategy-card').forEach(function(card) {
            if (card.dataset.disabled === 'true') return;
            card.addEventListener('click', function() {
                Installer.strategy = card.dataset.strategy;
                Installer.validated = false;
                content.querySelectorAll('.installer-strategy-card').forEach(function(c) {
                    var sel = c === card;
                    c.style.borderColor = sel ? 'var(--accent)' : 'var(--border)';
                    var chk = c.querySelector('.inst-check');
                    if (chk) { chk.style.color = sel ? 'var(--accent)' : 'var(--bg-tertiary)'; chk.innerHTML = '<i class="fas ' + (sel ? 'fa-check-circle' : 'fa-circle') + '"></i>'; }
                });
                var nextBtn = document.getElementById('setup-next');
                if (nextBtn) { nextBtn.disabled = false; nextBtn.style.opacity = '1'; }
            });
        });

        // If strategy already selected, enable next
        if (Installer.strategy) {
            var nextBtn = document.getElementById('setup-next');
            if (nextBtn) { nextBtn.disabled = false; nextBtn.style.opacity = '1'; }
        }

    } catch (e) {
        if (loading) loading.innerHTML = '<div style="color:#ef4444;font-size:13px;"><i class="fas fa-exclamation-circle"></i> ' + esc(e.message) + '</div>';
    }
}

function _installerStrategyCard(strategy, selected, icon, iconColor, title, desc, disabledOrNote) {
    var disabled = disabledOrNote === 'disabled';
    var note = (!disabled && disabledOrNote) ? disabledOrNote : '';
    return '<div class="installer-strategy-card" data-strategy="' + strategy + '" data-disabled="' + disabled + '" '
        + 'style="display:flex;align-items:center;gap:12px;padding:14px 16px;background:var(--bg-secondary);border:2px solid '
        + (selected ? 'var(--accent)' : 'var(--border)') + ';border-radius:10px;'
        + (disabled ? 'opacity:0.4;cursor:not-allowed;' : 'cursor:pointer;') + 'transition:border-color .15s;">'
        + '<div style="font-size:24px;color:' + iconColor + ';width:32px;text-align:center;"><i class="fas ' + icon + '"></i></div>'
        + '<div style="flex:1;"><div style="font-size:14px;font-weight:600;">' + esc(title) + '</div>'
        + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">' + esc(desc) + '</div>'
        + (note ? '<div style="font-size:10px;color:var(--text-muted);margin-top:2px;font-style:italic;">' + esc(note) + '</div>' : '')
        + '</div>'
        + '<div class="inst-check" style="font-size:20px;color:' + (selected ? 'var(--accent)' : 'var(--bg-tertiary)') + ';">'
        + '<i class="fas ' + (selected ? 'fa-check-circle' : 'fa-circle') + '"></i></div></div>';
}


/* ─────────── Step: Drive Selection ─────────── */
function installerStepDrives() {
    return '<div class="login-card" style="max-width:560px;">'
        + '<h2 style="margin:0 0 6px;font-size:20px;font-weight:700;">'
        + '<i class="fas fa-database" style="margin-right:8px;color:var(--accent);"></i>'
        + (Installer.strategy === 'internal' ? t('Wybór dysków') : t('Dyski danych')) + '</h2>'
        + '<p style="color:var(--text-secondary);font-size:13px;margin-bottom:20px;">'
        + (Installer.strategy === 'internal'
            ? t('Wybierz dysk docelowy dla systemu oraz opcjonalne dyski danych.')
            : t('Wybierz dyski, na których będą przechowywane dane użytkowników i aplikacji.'))
        + '</p>'
        + installerProgress(1)
        + '<div id="installer-drives-content"></div>'
        + '<div style="display:flex;gap:10px;margin-top:16px;">'
        + '<button type="button" class="btn-login" id="setup-back" style="flex:1;background:var(--bg-tertiary);">'
        + '<i class="fas fa-arrow-left"></i> <span>' + t('Wstecz') + '</span></button>'
        + '<button type="button" class="btn-login" id="setup-next" style="flex:2;">'
        + '<span>' + t('Dalej') + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div></div>';
}

function installerLoadDrives() {
    var el = document.getElementById('installer-drives-content');
    if (!el) return;

    var fmtSz = function(gb) { return gb >= 1000 ? (gb/1000).toFixed(1) + ' TB' : gb.toFixed(0) + ' GB'; };

    // Helper: get the canonical ID for a device (persistent ID preferred)
    var devId = function(d) { return d.id || d.device; };

    var html = '';

    // ── Section 1: System disk selection (only for 'internal' strategy) ──
    if (Installer.strategy === 'internal') {
        html += '<div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text-primary);">'
            + '<i class="fas fa-server" style="color:var(--accent);margin-right:6px;"></i>'
            + t('Dysk systemowy') + '</div>';

        var candidates = Installer.devices.filter(function(d) {
            return d.eligible_roles && d.eligible_roles.indexOf('system') >= 0;
        });

        if (!candidates.length) {
            html += '<div style="padding:12px;color:var(--text-muted);font-size:13px;">'
                + t('Brak dostępnych dysków do instalacji.') + '</div>';
        } else {
            html += '<div id="installer-sys-disks" style="display:flex;flex-direction:column;gap:4px;margin-bottom:16px;">';
            for (var i = 0; i < candidates.length; i++) {
                var d = candidates[i];
                var did = devId(d);
                var sel = did === Installer.targetDevice;
                var icon = d.is_usb ? 'fa-usb' : d.transport === 'nvme' ? 'fa-bolt' : 'fa-hdd';
                var clr = d.is_usb ? '#a78bfa' : d.transport === 'nvme' ? '#f59e0b' : '#60a5fa';
                var warnTags = '';
                if (d.size_gb < 16) {
                    warnTags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(245,158,11,.12);color:#f59e0b;"><i class="fas fa-exclamation-triangle"></i> <16GB</span>';
                }
                if (d.is_raid_member) {
                    warnTags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(239,68,68,.12);color:#ef4444;"><i class="fas fa-exclamation-triangle"></i> RAID</span>';
                }
                if (d.smart_status === 'FAIL') {
                    warnTags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(239,68,68,.12);color:#ef4444;"><i class="fas fa-heart-broken"></i> SMART</span>';
                }
                if (d.temperature_c && d.temperature_c > 50) {
                    warnTags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(245,158,11,.12);color:#f59e0b;"><i class="fas fa-thermometer-half"></i> ' + d.temperature_c + '°C</span>';
                }

                html += '<div class="installer-sys-disk" data-device-id="' + esc(did) + '" '
                    + 'style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--bg-secondary);'
                    + 'border:2px solid ' + (sel ? 'var(--accent)' : 'var(--border)') + ';border-radius:8px;cursor:pointer;transition:border-color .15s;">'
                    + '<i class="fas ' + icon + '" style="color:' + clr + ';font-size:18px;width:24px;text-align:center;"></i>'
                    + '<div style="flex:1;min-width:0;"><div style="font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
                    + esc(d.model) + ' · ' + fmtSz(d.size_gb) + warnTags + '</div>'
                    + '<div style="font-size:11px;color:var(--text-muted);">' + esc(d.device) + ' · ' + esc(d.connection_type || '') + ' · ' + esc(d.fstype_summary || '') + '</div></div>'
                    + '<div class="sys-check" style="font-size:18px;color:' + (sel ? 'var(--accent)' : 'var(--bg-tertiary)') + ';">'
                    + '<i class="fas ' + (sel ? 'fa-check-circle' : 'fa-circle') + '"></i></div></div>';
            }
            html += '</div>';
        }
    }

    // ── Section 2: Data disks (multi-select) ──
    var dataCandidates = Installer.devices.filter(function(d) {
        if (!d.eligible_roles || d.eligible_roles.indexOf('data') < 0) return false;
        // Exclude the chosen system disk
        var did = d.id || d.device;
        return did !== Installer.targetDevice;
    });

    if (dataCandidates.length > 0) {
        html += '<div style="font-size:13px;font-weight:600;margin-bottom:8px;color:var(--text-primary);">'
            + '<i class="fas fa-database" style="color:var(--accent);margin-right:6px;"></i>'
            + t('Dyski danych') + ' <span style="font-size:11px;font-weight:400;color:var(--text-muted);">'
            + t('(opcjonalne — wielokrotny wybór)') + '</span></div>';

        html += '<div id="installer-data-disks" style="display:flex;flex-direction:column;gap:4px;margin-bottom:12px;">';
        for (var j = 0; j < dataCandidates.length; j++) {
            var dd = dataCandidates[j];
            var ddId = devId(dd);
            var ddSel = Installer.dataDisks.indexOf(ddId) >= 0;
            var ddIcon = dd.is_usb ? 'fa-usb' : dd.transport === 'nvme' ? 'fa-bolt' : 'fa-hdd';
            var ddClr = dd.is_usb ? '#a78bfa' : dd.transport === 'nvme' ? '#f59e0b' : '#60a5fa';
            var ddTags = '';
            if (dd.smart_status === 'FAIL') {
                ddTags += ' <span style="font-size:9px;padding:1px 6px;border-radius:6px;background:rgba(239,68,68,.12);color:#ef4444;"><i class="fas fa-heart-broken"></i> SMART</span>';
            }

            html += '<div class="installer-data-disk" data-device-id="' + esc(ddId) + '" '
                + 'style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--bg-secondary);'
                + 'border:2px solid ' + (ddSel ? 'var(--accent)' : 'var(--border)') + ';border-radius:8px;cursor:pointer;transition:border-color .15s;">'
                + '<i class="fas ' + ddIcon + '" style="color:' + ddClr + ';font-size:16px;width:22px;text-align:center;"></i>'
                + '<div style="flex:1;min-width:0;"><div style="font-size:12px;font-weight:600;">'
                + esc(dd.model) + ' · ' + fmtSz(dd.size_gb) + ddTags + '</div>'
                + '<div style="font-size:10px;color:var(--text-muted);">' + esc(dd.device) + ' · ' + esc(dd.connection_type || '') + '</div></div>'
                + '<div class="dd-check" style="font-size:16px;color:' + (ddSel ? 'var(--accent)' : 'var(--bg-tertiary)') + ';">'
                + '<i class="fas ' + (ddSel ? 'fa-check-square' : 'fa-square') + '"></i></div></div>';
        }
        html += '</div>';

        // Info
        if (Installer.strategy === 'internal') {
            html += '<div style="font-size:11px;color:var(--text-muted);padding:0 4px;">'
                + '<i class="fas fa-info-circle" style="color:var(--accent);"></i> '
                + t('Jeśli nie wybierzesz dysku danych, dane użytkownika będą na partycji danych dysku systemowego.') + '</div>';
        }
    } else if (Installer.strategy === 'usb') {
        html += '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:12px 16px;font-size:12px;color:var(--text-muted);">'
            + '<i class="fas fa-info-circle" style="color:var(--accent);margin-right:6px;"></i>'
            + t('Brak dodatkowych dysków. Dane będą przechowywane na nośniku USB.') + '</div>';
    }

    // ── Encryption option ──
    html += '<div style="margin-top:12px;padding:10px 0;border-top:1px solid var(--border);">'
        + '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;">'
        + '<input type="checkbox" id="installer-encrypt-cb"' + (Installer.encrypt ? ' checked' : '') + '>'
        + '<span><i class="fas fa-lock" style="font-size:11px;color:var(--accent);"></i> '
        + t('Szyfrowanie (LUKS)') + '</span></label>'
        + '<div id="installer-pass-row" style="display:' + (Installer.encrypt ? 'block' : 'none') + ';margin-top:8px;padding-left:24px;">'
        + '<input type="password" id="installer-passphrase" placeholder="' + t('Hasło szyfrowania (min. 4 znaki)') + '" '
        + 'value="' + esc(Installer.passphrase) + '" '
        + 'style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-primary);color:var(--text-primary);font-size:13px;box-sizing:border-box;">'
        + '</div></div>';

    el.innerHTML = html;

    // ── Bind: system disk selection (using persistent ID) ──
    el.querySelectorAll('.installer-sys-disk').forEach(function(card) {
        card.addEventListener('click', function() {
            var did = card.dataset.deviceId;
            Installer.targetDevice = did;
            Installer.validated = false;
            // Remove from data disks if selected
            Installer.dataDisks = Installer.dataDisks.filter(function(dd) { return dd !== did; });
            el.querySelectorAll('.installer-sys-disk').forEach(function(c) {
                var isSel = c.dataset.deviceId === did;
                c.style.borderColor = isSel ? 'var(--accent)' : 'var(--border)';
                var chk = c.querySelector('.sys-check');
                if (chk) { chk.style.color = isSel ? 'var(--accent)' : 'var(--bg-tertiary)'; chk.innerHTML = '<i class="fas ' + (isSel ? 'fa-check-circle' : 'fa-circle') + '"></i>'; }
            });
            // Refresh data disks (exclude selected system disk)
            installerLoadDrives();
        });
    });

    // ── Bind: data disk multi-selection (using persistent ID) ──
    el.querySelectorAll('.installer-data-disk').forEach(function(card) {
        card.addEventListener('click', function() {
            var did = card.dataset.deviceId;
            var idx = Installer.dataDisks.indexOf(did);
            if (idx >= 0) {
                Installer.dataDisks.splice(idx, 1);
            } else {
                Installer.dataDisks.push(did);
            }
            Installer.validated = false;
            var isSel = Installer.dataDisks.indexOf(did) >= 0;
            card.style.borderColor = isSel ? 'var(--accent)' : 'var(--border)';
            var chk = card.querySelector('.dd-check');
            if (chk) { chk.style.color = isSel ? 'var(--accent)' : 'var(--bg-tertiary)'; chk.innerHTML = '<i class="fas ' + (isSel ? 'fa-check-square' : 'fa-square') + '"></i>'; }
        });
    });

    // ── Bind: encryption ──
    var encCb = document.getElementById('installer-encrypt-cb');
    if (encCb) {
        encCb.addEventListener('change', function() {
            Installer.encrypt = encCb.checked;
            var row = document.getElementById('installer-pass-row');
            if (row) row.style.display = encCb.checked ? 'block' : 'none';
        });
    }
}


/* ─────────── Step: Summary & Install ─────────── */
function installerStepSummary() {
    return '<div class="login-card" style="max-width:560px;">'
        + '<h2 style="margin:0 0 6px;font-size:20px;font-weight:700;">'
        + '<i class="fas fa-clipboard-check" style="margin-right:8px;color:var(--accent);"></i>'
        + t('Podsumowanie instalacji') + '</h2>'
        + '<p style="color:var(--text-secondary);font-size:13px;margin-bottom:20px;">'
        + t('Sprawdź plan instalacji. Wpisz INSTALUJ aby potwierdzić.') + '</p>'
        + installerProgress(2)
        + '<div id="installer-summary-content">'
        + '<div style="text-align:center;padding:20px;color:var(--text-secondary);"><i class="fas fa-spinner fa-spin" style="font-size:24px;"></i>'
        + '<div style="margin-top:10px;">' + t('Walidacja planu...') + '</div></div>'
        + '</div>'
        + '<div id="installer-progress-container" style="display:none;"></div>'
        + '<div style="display:flex;gap:10px;margin-top:16px;">'
        + '<button type="button" class="btn-login" id="setup-back" style="flex:1;background:var(--bg-tertiary);">'
        + '<i class="fas fa-arrow-left"></i> <span>' + t('Wstecz') + '</span></button>'
        + '<button type="button" class="btn-login" id="installer-execute-btn" style="flex:2;background:#ef4444;" disabled>'
        + '<i class="fas fa-play"></i> <span>' + t('Zainstaluj') + '</span></button>'
        + '<button type="button" class="btn-login" id="setup-next" style="flex:2;display:none;">'
        + '<span>' + t('Dalej') + '</span> <i class="fas fa-arrow-right"></i></button>'
        + '</div></div>';
}

async function installerLoadSummary() {
    var el = document.getElementById('installer-summary-content');
    if (!el) return;

    // Save passphrase from previous step
    var ppEl = document.getElementById('installer-passphrase');
    if (ppEl) Installer.passphrase = ppEl.value;

    try {
        var r = await fetch('/api/installer/validate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                strategy: Installer.strategy,
                target_device: Installer.targetDevice,
                data_disks: Installer.dataDisks,
                encrypt: Installer.encrypt,
                passphrase: Installer.passphrase,
            })
        });
        var data = await r.json();

        Installer.validated = data.valid;
        Installer.summary = data.summary || '';
        Installer.plan = data.plan || null;
        Installer.warnings = data.warnings || [];

        var html = '';

        // Plan visualization (partition scheme)
        if (Installer.plan) {
            html += _installerRenderPlan(Installer.plan);
        }

        // Summary text
        var summaryLines = Installer.summary.split('\n');
        html += '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:12px;">';
        for (var i = 0; i < summaryLines.length; i++) {
            var line = summaryLines[i];
            var isWarning = line.indexOf('UWAGA') >= 0 || line.indexOf('usunięte') >= 0;
            html += '<div style="font-size:13px;line-height:1.6;color:' + (isWarning ? '#ef4444' : 'var(--text-primary)') + ';">'
                + (isWarning ? '<i class="fas fa-exclamation-triangle" style="margin-right:4px;"></i>' : '')
                + esc(line) + '</div>';
        }
        html += '</div>';

        // Warnings
        if (Installer.warnings.length > 0) {
            html += '<div style="background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.2);border-radius:10px;padding:12px 16px;margin-bottom:12px;">';
            for (var w = 0; w < Installer.warnings.length; w++) {
                html += '<div style="font-size:12px;color:#d97706;line-height:1.6;">'
                    + '<i class="fas fa-exclamation-triangle" style="margin-right:4px;"></i>'
                    + esc(Installer.warnings[w]) + '</div>';
            }
            html += '</div>';
        }

        // Errors
        if (data.errors && data.errors.length > 0) {
            html += '<div style="background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.2);border-radius:10px;padding:12px 16px;margin-bottom:12px;">';
            for (var e = 0; e < data.errors.length; e++) {
                html += '<div style="font-size:12px;color:#ef4444;line-height:1.6;">'
                    + '<i class="fas fa-times-circle" style="margin-right:4px;"></i>'
                    + esc(data.errors[e]) + '</div>';
            }
            html += '</div>';
        }

        // Encryption note
        if (Installer.encrypt) {
            html += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;">'
                + '<i class="fas fa-lock" style="color:var(--accent);margin-right:4px;"></i>'
                + t('Szyfrowanie LUKS zostanie zastosowane.') + '</div>';
        }

        // Typed confirmation field
        if (data.valid) {
            html += '<div style="background:rgba(239,68,68,.04);border:1px solid rgba(239,68,68,.15);border-radius:10px;padding:14px 16px;margin-bottom:8px;">'
                + '<div style="font-size:12px;font-weight:600;color:#ef4444;margin-bottom:8px;">'
                + '<i class="fas fa-shield-alt" style="margin-right:4px;"></i>'
                + t('Potwierdzenie destrukcyjnej operacji') + '</div>'
                + '<div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;">'
                + t('Wpisz') + ' <strong style="color:#ef4444;">INSTALUJ</strong> ' + t('aby potwierdzić. Wszystkie dane na wybranych nośnikach zostaną NIEODWRACALNIE usunięte.') + '</div>'
                + '<input type="text" id="installer-confirm-token" placeholder="INSTALUJ" autocomplete="off" spellcheck="false" '
                + 'style="width:100%;padding:10px 14px;border:2px solid rgba(239,68,68,.3);border-radius:8px;background:var(--bg-primary);color:var(--text-primary);font-size:15px;font-weight:700;text-align:center;letter-spacing:2px;box-sizing:border-box;">'
                + '</div>';
        }

        el.innerHTML = html;

        // Bind typed confirmation
        var tokenInput = document.getElementById('installer-confirm-token');
        var execBtn = document.getElementById('installer-execute-btn');
        if (tokenInput && execBtn) {
            var checkToken = function() {
                var valid = tokenInput.value.trim() === 'INSTALUJ';
                execBtn.disabled = !valid;
                execBtn.style.opacity = valid ? '1' : '0.5';
                tokenInput.style.borderColor = valid ? '#22c55e' : 'rgba(239,68,68,.3)';
            };
            tokenInput.addEventListener('input', checkToken);
            execBtn.addEventListener('click', installerExecute);
            checkToken();
        } else if (execBtn) {
            execBtn.disabled = !data.valid;
            execBtn.style.opacity = data.valid ? '1' : '0.5';
        }

    } catch (err) {
        el.innerHTML = '<div style="color:#ef4444;font-size:13px;"><i class="fas fa-exclamation-circle"></i> ' + esc(err.message) + '</div>';
    }
}

/* ─────────── Plan visualization (partition scheme) ─────────── */
function _installerRenderPlan(plan) {
    if (!plan) return '';
    var html = '';

    // System disk plan
    if (plan.system) {
        var s = plan.system;
        html += '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:8px;">'
            + '<div style="font-size:12px;font-weight:600;color:var(--text-primary);margin-bottom:8px;">'
            + '<i class="fas fa-server" style="color:var(--accent);margin-right:6px;"></i>'
            + t('Dysk systemowy') + ': ' + esc(s.model || s.device) + '</div>'
            + '<div style="font-size:10px;color:var(--text-muted);margin-bottom:6px;">' + esc(s.action) + '</div>';

        // Partition bar visualization
        if (s.partitions && s.partitions.length) {
            var colors = ['#3b82f6', '#8b5cf6', '#22c55e'];
            html += '<div style="display:flex;height:24px;border-radius:6px;overflow:hidden;border:1px solid var(--border);margin-bottom:6px;">';
            for (var p = 0; p < s.partitions.length; p++) {
                var part = s.partitions[p];
                var widthPct = part.size_mb ? Math.max(8, Math.min(30, part.size_mb / 10)) : 62;
                if (part.size_pct) widthPct = part.size_pct * 0.62;
                html += '<div style="flex:' + (part.size_pct ? '1' : '0 0 ' + widthPct + 'px') + ';background:' + colors[p % colors.length] + ';display:flex;align-items:center;justify-content:center;">'
                    + '<span style="font-size:9px;color:#fff;font-weight:600;white-space:nowrap;padding:0 4px;">'
                    + esc(part.label) + (part.size_mb ? ' (' + part.size_mb + 'MB)' : '') + '</span></div>';
            }
            html += '</div>';
        }
        html += '</div>';
    }

    // Data disks plan
    if (plan.data && plan.data.length) {
        for (var d = 0; d < plan.data.length; d++) {
            var dd = plan.data[d];
            html += '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:10px 16px;margin-bottom:8px;">'
                + '<div style="font-size:12px;font-weight:600;color:var(--text-primary);margin-bottom:4px;">'
                + '<i class="fas fa-database" style="color:var(--accent);margin-right:6px;"></i>'
                + t('Dysk danych') + ' #' + (d + 1) + ': ' + esc(dd.model || dd.device) + '</div>'
                + '<div style="font-size:10px;color:var(--text-muted);">' + esc(dd.action) + '</div>'
                + '</div>';
        }
    }

    return html;
}

async function installerExecute() {
    var execBtn = document.getElementById('installer-execute-btn');
    var backBtn = document.getElementById('setup-back');
    var summaryEl = document.getElementById('installer-summary-content');
    var progressEl = document.getElementById('installer-progress-container');

    // Get typed confirmation token
    var tokenInput = document.getElementById('installer-confirm-token');
    var token = tokenInput ? tokenInput.value.trim() : '';
    if (token !== 'INSTALUJ') return;

    // Check RAID — still uses standard dialog since it's a secondary safety check
    var raidDevices = [];
    var allTargetIds = Installer.dataDisks.slice();
    if (Installer.strategy === 'internal' && Installer.targetDevice) {
        allTargetIds.push(Installer.targetDevice);
    }
    for (var i = 0; i < allTargetIds.length; i++) {
        var dev = Installer.devicesById[allTargetIds[i]];
        if (dev && dev.is_raid_member) raidDevices.push(dev.model || allTargetIds[i]);
    }
    var confirmRaid = false;
    if (raidDevices.length > 0) {
        if (!await confirmDialog(t('DANGER ZONE: Dyski') + ' ' + raidDevices.join(', ') + ' ' + t('są członkami macierzy RAID!') + '\n' + t('Kontynuowanie może uszkodzić macierz. Potwierdzasz?'))) return;
        confirmRaid = true;
    }

    if (execBtn) { execBtn.disabled = true; execBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Instaluję...'); }
    if (backBtn) { backBtn.disabled = true; backBtn.style.opacity = '0.5'; }

    Installer.installing = true;

    try {
        var r = await fetch('/api/installer/execute', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                strategy: Installer.strategy,
                target_device: Installer.targetDevice,
                data_disks: Installer.dataDisks,
                encrypt: Installer.encrypt,
                passphrase: Installer.passphrase,
                confirmation_token: token,
                confirm_raid: confirmRaid,
            })
        });
        var data = await r.json();
        if (data.error) {
            _installerShowError(data.error);
            if (execBtn) { execBtn.disabled = false; execBtn.innerHTML = '<i class="fas fa-play"></i> ' + t('Zainstaluj'); }
            if (backBtn) { backBtn.disabled = false; backBtn.style.opacity = '1'; }
            return;
        }

        // Show progress UI
        if (summaryEl) summaryEl.style.display = 'none';
        if (progressEl) {
            progressEl.style.display = '';
            progressEl.innerHTML = '<div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:10px;padding:16px;">'
                + '<div style="font-size:13px;font-weight:600;margin-bottom:12px;"><i class="fas fa-cog fa-spin" style="color:var(--accent);margin-right:6px;"></i>'
                + '<span id="installer-phase-msg">' + t('Rozpoczynam instalację...') + '</span></div>'
                + '<div style="background:var(--bg-tertiary);border-radius:6px;height:8px;overflow:hidden;margin-bottom:8px;">'
                + '<div id="installer-progress-bar" style="height:100%;background:var(--accent);width:0%;transition:width .3s;border-radius:6px;"></div></div>'
                + '<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);">'
                + '<span id="installer-pct">0%</span>'
                + '<span id="installer-speed"></span>'
                + '<span id="installer-elapsed">0:00</span></div>'
                + '<div id="installer-log-area" style="margin-top:12px;max-height:180px;overflow-y:auto;font-family:monospace;font-size:11px;color:var(--text-muted);background:var(--bg-primary);border-radius:6px;padding:8px;"></div>'
                + '</div>';
        }

        // Start polling
        _installerPollProgress();

    } catch (err) {
        _installerShowError(err.message);
        if (execBtn) { execBtn.disabled = false; execBtn.innerHTML = '<i class="fas fa-play"></i> ' + t('Zainstaluj'); }
        if (backBtn) { backBtn.disabled = false; backBtn.style.opacity = '1'; }
    }
}

function _installerShowError(msg) {
    var el = document.getElementById('installer-summary-content');
    if (el) {
        el.style.display = '';
        el.innerHTML += '<div style="color:#ef4444;font-size:13px;margin-top:8px;"><i class="fas fa-exclamation-circle"></i> ' + esc(msg) + '</div>';
    }
}

var _installerLogOffset = 0;

function _installerPollProgress() {
    if (Installer.pollTimer) clearTimeout(Installer.pollTimer);

    (async function poll() {
        try {
            var [statusR, logsR] = await Promise.all([
                fetch('/api/installer/status').then(function(r) { return r.json(); }),
                fetch('/api/installer/logs?offset=' + _installerLogOffset).then(function(r) { return r.json(); }),
            ]);

            // Update progress bar
            var bar = document.getElementById('installer-progress-bar');
            var pct = document.getElementById('installer-pct');
            var msg = document.getElementById('installer-phase-msg');
            var speed = document.getElementById('installer-speed');
            var elapsed = document.getElementById('installer-elapsed');

            if (bar) bar.style.width = statusR.percent + '%';
            if (pct) pct.textContent = statusR.percent + '%';
            if (msg) msg.textContent = statusR.message;
            if (speed && statusR.speed) speed.textContent = statusR.speed.toFixed(1) + ' MB/s';
            if (elapsed && statusR.elapsed) {
                var m = Math.floor(statusR.elapsed / 60);
                var s = statusR.elapsed % 60;
                elapsed.textContent = m + ':' + (s < 10 ? '0' : '') + s;
            }

            // Append logs
            if (logsR.logs && logsR.logs.length > 0) {
                var logArea = document.getElementById('installer-log-area');
                if (logArea) {
                    for (var i = 0; i < logsR.logs.length; i++) {
                        logArea.innerHTML += '<div>' + esc(logsR.logs[i]) + '</div>';
                    }
                    logArea.scrollTop = logArea.scrollHeight;
                }
                _installerLogOffset = logsR.total;
            }

            // Check completion
            if (statusR.status === 'done' || statusR.status === 'error') {
                Installer.installing = false;
                Installer.installDone = statusR.status === 'done';

                var phaseMsg = document.getElementById('installer-phase-msg');
                var execBtn = document.getElementById('installer-execute-btn');
                var nextBtn = document.getElementById('setup-next');
                var backBtn = document.getElementById('setup-back');

                if (statusR.status === 'done') {
                    if (phaseMsg) phaseMsg.innerHTML = '<i class="fas fa-check-circle" style="color:#22c55e;"></i> ' + t('Instalacja zakończona pomyślnie!');
                    if (execBtn) execBtn.style.display = 'none';
                    if (nextBtn) { nextBtn.style.display = ''; nextBtn.disabled = false; nextBtn.style.opacity = '1'; }
                    if (backBtn) backBtn.style.display = 'none';

                    // If strategy was 'internal', show reboot suggestion
                    if (Installer.strategy === 'internal') {
                        var progressEl = document.getElementById('installer-progress-container');
                        if (progressEl) {
                            progressEl.innerHTML += '<div style="background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.2);border-radius:10px;padding:12px 16px;margin-top:12px;font-size:13px;">'
                                + '<i class="fas fa-info-circle" style="color:#22c55e;margin-right:6px;"></i>'
                                + t('System został zainstalowany na dysku wewnętrznym. Po zakończeniu konfiguracji możesz wyjąć USB i uruchomić ponownie z dysku.') + '</div>';
                        }
                    }
                } else {
                    var errMsg = (statusR.result && statusR.result.message) || t('Błąd instalacji');
                    if (phaseMsg) phaseMsg.innerHTML = '<i class="fas fa-times-circle" style="color:#ef4444;"></i> ' + esc(errMsg);
                    if (execBtn) { execBtn.style.display = 'none'; }
                    if (backBtn) { backBtn.disabled = false; backBtn.style.opacity = '1'; }
                    // Show retry button
                    var progressEl = document.getElementById('installer-progress-container');
                    if (progressEl) {
                        progressEl.innerHTML += '<div style="text-align:center;margin-top:12px;">'
                            + '<button onclick="installerRetry()" class="btn-login" style="background:#ef4444;max-width:200px;">'
                            + '<i class="fas fa-redo"></i> ' + t('Spróbuj ponownie') + '</button></div>';
                    }
                }
                return; // stop polling
            }

        } catch (e) {
            // Network error during poll — retry
        }

        Installer.pollTimer = setTimeout(poll, 1500);
    })();
}

async function installerRetry() {
    try {
        await fetch('/api/installer/reset', { method: 'POST' });
    } catch (e) { /* ignore */ }
    _installerLogOffset = 0;
    Installer.installing = false;
    Installer.installDone = false;
    // Go back to strategy step
    Installer.step = 0;
    installerRenderStep();
}


/* ─────────── Validation before proceeding from drives step ─────────── */
function installerValidateDrives() {
    if (Installer.strategy === 'internal' && !Installer.targetDevice) {
        // Use toast if available, fallback to alert
        if (typeof toast === 'function') toast(t('Wybierz dysk docelowy dla systemu.'), 'error');
        else alert(t('Wybierz dysk docelowy dla systemu.'));
        return false;
    }
    // Save passphrase
    var ppEl = document.getElementById('installer-passphrase');
    if (ppEl) Installer.passphrase = ppEl.value;
    if (Installer.encrypt && (!Installer.passphrase || Installer.passphrase.length < 4)) {
        if (typeof toast === 'function') toast(t('Hasło szyfrowania musi mieć min. 4 znaki.'), 'error');
        else alert(t('Hasło szyfrowania musi mieć min. 4 znaki.'));
        return false;
    }
    return true;
}
