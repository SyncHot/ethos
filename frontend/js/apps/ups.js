/* ═══════════════════════════════════════════════════════════
   EthOS — UPS Management (NUT Integration)
   ═══════════════════════════════════════════════════════════ */

AppRegistry['ups'] = function (appDef) {
    createWindow('ups', {
        title: t('Zasilanie UPS'),
        icon: 'fa-battery-full',
        iconColor: '#22c55e',
        width: 600,
        height: 500,
        onRender: (body) => renderUPSApp(body),
    });
};

function renderUPSApp(body) {
    const $ = (s) => body.querySelector(s);
    let config = {};
    let status = {};

    body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%">
        <div style="padding:15px;background:var(--bg-secondary);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:15px">
             <div style="font-size:2em;color:var(--text-muted)" id="ups-main-icon"><i class="fas fa-battery-full"></i></div>
             <div style="flex:1">
                 <div style="font-size:1.2em;font-weight:600" id="ups-model">${t('Ładowanie...')}</div>
                 <div style="font-size:0.9em;color:var(--text-muted)" id="ups-status-line"></div>
             </div>
             <div style="text-align:right">
                 <div style="font-size:1.5em;font-weight:bold" id="ups-charge">--%</div>
                 <div style="font-size:0.8em;color:var(--text-muted)" id="ups-runtime">-- min</div>
             </div>
        </div>

        <div style="padding:10px;border-bottom:1px solid var(--border);background:var(--bg-primary)">
            <ul class="nav nav-tabs" style="margin:0;padding:0;display:flex;gap:15px;list-style:none">
                <li class="nav-item active" data-tab="status" style="cursor:pointer;padding:5px 10px;font-weight:600;border-bottom:2px solid var(--accent)">Status</li>
                <li class="nav-item" data-tab="settings" style="cursor:pointer;padding:5px 10px;color:var(--text-muted)">Ustawienia</li>
            </ul>
        </div>

        <div id="ups-tab-status" style="flex:1;padding:20px;overflow-y:auto">
            <div class="form-group">
                <label>${t('Obciążenie')}</label>
                <div class="progress" style="height:20px;background:var(--bg-secondary);border-radius:4px;overflow:hidden;margin-top:5px">
                    <div id="ups-load-bar" style="height:100%;background:var(--accent);width:0%"></div>
                </div>
                <div style="text-align:right;font-size:0.8em;margin-top:2px" id="ups-load-val">0%</div>
            </div>
            
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-top:20px">
                <div class="dash-card" style="padding:15px">
                    <div style="font-size:0.8em;color:var(--text-muted)">${t('Napięcie wejściowe')}</div>
                    <div style="font-size:1.2em;font-weight:600" id="ups-voltage">-- V</div>
                </div>
                <div class="dash-card" style="padding:15px">
                    <div style="font-size:0.8em;color:var(--text-muted)">Status</div>
                    <div style="font-size:1.2em;font-weight:600" id="ups-status-code">--</div>
                </div>
            </div>
        </div>

        <div id="ups-tab-settings" style="flex:1;padding:20px;overflow-y:auto;display:none">
            <div class="form-group">
                <label>${t('Zarządzanie UPS')}</label>
                <label class="switch">
                    <input type="checkbox" id="ups-enabled">
                    <span class="slider round"></span>
                </label>
                <span style="font-size:0.9em;margin-left:10px">${t('Włącz usługę NUT')}</span>
            </div>
            
            <div class="form-group" style="margin-top:15px">
                <label>Tryb pracy</label>
                <select id="ups-mode" class="fm-input">
                    <option value="usb">Lokalny (USB)</option>
                    <option value="net">Sieciowy (Slave)</option>
                </select>
            </div>
            
            <div class="form-group" style="margin-top:15px">
                <label>Webhook URL (Powiadomienia)</label>
                <input type="text" id="ups-webhook" class="fm-input" placeholder="https://..." style="width:100%">
                <div style="font-size:0.8em;color:var(--text-muted);margin-top:5px">${t('Opcjonalnie: URL do powiadomień POST przy zmianie zasilania')}</div>
            </div>
            
            <div id="ups-usb-config">
                <div class="form-group" style="margin-top:15px">
                    <label>Auto-wykrywanie</label>
                    <button class="btn btn-secondary" id="ups-scan-btn" style="width:100%"><i class="fas fa-search"></i> ${t('Skanuj urządzenia USB')}</button>
                    <div id="ups-scan-result" style="margin-top:5px;font-size:0.9em;color:var(--text-muted)"></div>
                </div>
            </div>

            <div class="form-group" style="margin-top:20px;padding-top:20px;border-top:1px solid var(--border)">
                <label>Opcje zamykania systemu</label>
                <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
                    <span>${t('Wyłącz gdy bateria <')} </span>
                    <input type="number" id="ups-shutdown-pct" class="fm-input" style="width:70px" min="5" max="90" value="20">
                    <span>%</span>
                </div>
                <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
                    <span>Lub po</span>
                    <input type="number" id="ups-shutdown-time" class="fm-input" style="width:70px" min="0" value="300">
                    <span>${t('sekundach na baterii (0 = wyłączone)')}</span>
                </div>
            </div>
            
            <div style="margin-top:20px;text-align:right">
                <button class="btn btn-primary" id="ups-save-btn"><i class="fas fa-save"></i> Zapisz</button>
            </div>
        </div>
    </div>`;

    // Tabs
    const tabs = body.querySelectorAll('.nav-item');
    tabs.forEach(t => {
        t.onclick = () => {
            tabs.forEach(x => { x.classList.remove('active'); x.style.borderBottom='none'; x.style.color='var(--text-muted)'; });
            t.classList.add('active');
            t.style.borderBottom='2px solid var(--accent)';
            t.style.color='var(--text)';
            
            body.querySelector('#ups-tab-status').style.display = t.dataset.tab === 'status' ? 'block' : 'none';
            body.querySelector('#ups-tab-settings').style.display = t.dataset.tab === 'settings' ? 'block' : 'none';
        };
    });

    async function loadData() {
        try {
            const [stat, cfg] = await Promise.all([
                api('/ups/status'),
                api('/ups/settings')
            ]);
            config = cfg;
            status = stat;
            updateUI();
        } catch (e) {
            console.error(e);
        }
    }

    function updateUI() {
        // Status Header
        $('#ups-model').textContent = status.model || t('Brak połączenia');
        $('#ups-charge').textContent = (status.battery_charge || 0) + '%';
        $('#ups-runtime').textContent = status.runtime ? Math.round(status.runtime / 60) + ' min' : '--';
        $('#ups-status-line').textContent = status.status || t('Nieznany');
        
        const color = (status.battery_charge < 20 || (status.status||'').includes('OB')) ? 'var(--danger)' : 'var(--text-muted)';
        $('#ups-main-icon').style.color = color;
        if ((status.status||'').includes('OB')) {
             $('#ups-main-icon').innerHTML = '<i class="fas fa-plug-circle-xmark"></i>';
        } else {
             $('#ups-main-icon').innerHTML = '<i class="fas fa-battery-full"></i>';
        }
        
        // Status Tab
        $('#ups-load-bar').style.width = (status.load || 0) + '%';
        $('#ups-load-val').textContent = (status.load || 0) + '%';
        $('#ups-voltage').textContent = (status.voltage || 0) + ' V';
        $('#ups-status-code').textContent = status.status || '--';
        
        // Settings Tab
        $('#ups-enabled').checked = config.enabled;
        $('#ups-mode').value = config.mode || 'usb';
        $('#ups-webhook').value = config.webhook_url || '';
        $('#ups-shutdown-pct').value = config.shutdown_threshold || 20;
        $('#ups-shutdown-time').value = config.shutdown_timer || 300;
    }

    $('#ups-scan-btn').onclick = async () => {
        const btn = $('#ups-scan-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Skanowanie...';
        try {
            const res = await api('/ups/scan', { method: 'POST' });
            if (res.found) {
                $('#ups-scan-result').innerHTML = `
                    <div style="margin-bottom:5px">${t('Znaleziono:')} ${res.config}</div>
                    <button class="btn btn-sm btn-green" id="ups-apply-scan">${t('Zastosuj konfigurację')}</button>
                `;
                $('#ups-apply-scan').onclick = async () => {
                    if (!await confirmDialog(t('Czy na pewno chcesz zastosować tę konfigurację? Spowoduje to restart usługi UPS.'))) return;
                    try {
                        await api('/ups/apply', { method: 'POST', body: { config: res.config } });
                        toast(t('Konfiguracja UPS zastosowana'), 'success');
                        $('#ups-enabled').checked = true;
                        $('#ups-save-btn').click(); // Save enabled state
                    } catch(e) {
                        toast(e.message, 'error');
                    }
                };
            } else {
                $('#ups-scan-result').textContent = t('Nie znaleziono urządzeń');
            }
        } catch (e) {
             $('#ups-scan-result').textContent = t('Błąd:') + ' ' + e.message;
        }
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-search"></i> ' + t('Skanuj urządzenia USB');
    };

    $('#ups-save-btn').onclick = async () => {
        const btn = $('#ups-save-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Zapisywanie...';
        
        const newConfig = {
            enabled: $('#ups-enabled').checked,
            mode: $('#ups-mode').value,
            webhook_url: $('#ups-webhook').value.trim(),
            shutdown_threshold: parseInt($('#ups-shutdown-pct').value),
            shutdown_timer: parseInt($('#ups-shutdown-time').value)
        };
        
        try {
            await api('/ups/settings', { method: 'POST', body: newConfig });
            toast(t('Ustawienia zapisane'), 'success');
        } catch (e) {
            toast(t('Błąd zapisu:') + ' ' + e.message, 'error');
        }
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-save"></i> Zapisz';
    };

    loadData();
    const interval = setInterval(() => {
        if (!document.body.contains(body)) { clearInterval(interval); return; }
        api('/ups/status').then(s => { status = s; updateUI(); });
    }, 5000);
}
