AppRegistry['power'] = function (appDef, launchOpts) {
    const w = WM.createWindow({
        title: 'Zarządzanie energią',
        width: 700,
        height: 600,
        icon: 'power',
        center: true
    });

    let state = {
        wol: {},
        schedule: [],
        hdd: {},
        cpu: {}
    };

    const render = () => {
        w.content.innerHTML = `
            <div class="app-body">
                <div class="app-section">
                    <h3>Procesor i Wake-on-LAN</h3>
                    <div class="form-group">
                        <label>Profil wydajności CPU</label>
                        <select id="cpu-gov" class="form-control">
                            ${(state.cpu.available || []).map(g => 
                                `<option value="${g}" ${g === state.cpu.target ? 'selected' : ''}>${g}</option>`
                            ).join('')}
                        </select>
                        <small>Aktualnie: ${state.cpu.current}</small>
                    </div>
                    
                    <div class="form-group">
                        <label class="checkbox-container">
                            <input type="checkbox" id="wol-enabled" ${state.wol.enabled ? 'checked' : ''}>
                            <span>Włącz Wake-on-LAN</span>
                        </label>
                        <small>Interfejs: ${state.wol.interface || 'Nie wykryto'} (${state.wol.status})</small>
                    </div>
                </div>

                <div class="app-section">
                    <h3>Harmonogram pracy</h3>
                    <p>Automatyczne wyłączanie i włączanie (Wake-on-RTC).</p>
                    <div id="schedule-list" class="list-group"></div>
                    <button class="btn btn-sm btn-primary mt-2" id="add-schedule">Dodaj regułę</button>
                </div>

                <div class="app-section">
                    <h3>Dyski twarde (HDD Spindown)</h3>
                    <div id="hdd-list" class="list-group"></div>
                </div>

                <div class="app-actions">
                    <button class="btn btn-primary" id="save-power">Zapisz ustawienia</button>
                </div>
            </div>
        `;
        
        // Render Schedule List
        const schedList = w.content.querySelector('#schedule-list');
        state.schedule.forEach((rule, idx) => {
            const item = document.createElement('div');
            item.className = 'list-item flex-row';
            item.innerHTML = `
                <div style="flex:1">
                    <div><strong>${_formatDays(rule.days)}</strong></div>
                    <div>Wyłącz: ${rule.shutdown} | Włącz: ${rule.wakeup}</div>
                </div>
                <div>
                    <button class="btn btn-sm btn-danger remove-sched" data-idx="${idx}">Usuń</button>
                </div>
            `;
            schedList.appendChild(item);
        });

        // Render HDD List
        const hddList = w.content.querySelector('#hdd-list');
        Object.keys(state.hdd).forEach(drive => {
            const val = state.hdd[drive];
            const item = document.createElement('div');
            item.className = 'list-item flex-row';
            item.innerHTML = `
                <div style="flex:1"><strong>/dev/${drive}</strong></div>
                <select class="form-control hdd-select" data-drive="${drive}" style="width:150px">
                    <option value="0" ${val == 0 ? 'selected' : ''}>Wyłączone</option>
                    <option value="60" ${val == 60 ? 'selected' : ''}>5 min</option>
                    <option value="120" ${val == 120 ? 'selected' : ''}>10 min</option>
                    <option value="180" ${val == 180 ? 'selected' : ''}>15 min</option>
                    <option value="241" ${val == 241 ? 'selected' : ''}>30 min</option>
                    <option value="242" ${val == 242 ? 'selected' : ''}>1 godz</option>
                    <option value="243" ${val == 243 ? 'selected' : ''}>1.5 godz</option>
                    <option value="244" ${val == 244 ? 'selected' : ''}>2 godz</option>
                </select>
            `;
            hddList.appendChild(item);
        });

        // Bind events
        w.content.querySelector('#add-schedule').onclick = () => {
            // Simple prompt for now, could be modal
            const daysStr = prompt("Dni tygodnia (0-6, np. 1,2,3,4,5 dla pn-pt):", "1,2,3,4,5");
            if (!daysStr) return;
            const shutdown = prompt("Godzina wyłączenia (HH:MM):", "23:00");
            if (!shutdown) return;
            const wakeup = prompt("Godzina włączenia (HH:MM):", "07:00");
            if (!wakeup) return;
            
            const days = daysStr.split(',').map(d => parseInt(d.trim())).filter(d => !isNaN(d));
            state.schedule.push({days, shutdown, wakeup, enabled: true});
            render();
        };

        w.content.querySelectorAll('.remove-sched').forEach(b => {
            b.onclick = (e) => {
                const idx = parseInt(e.target.dataset.idx);
                state.schedule.splice(idx, 1);
                render();
            };
        });

        w.content.querySelectorAll('.hdd-select').forEach(s => {
            s.onchange = (e) => {
                state.hdd[e.target.dataset.drive] = parseInt(e.target.value);
            };
        });

        w.content.querySelector('#save-power').onclick = async () => {
            const cpuGov = w.content.querySelector('#cpu-gov').value;
            const wolEnabled = w.content.querySelector('#wol-enabled').checked;
            
            try {
                await fetch('/api/power/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        cpu_governor: cpuGov,
                        wol_enabled: wolEnabled,
                        schedule: state.schedule,
                        hdd_spindown: state.hdd
                    })
                });
                toast("Zapisano ustawienia energii");
            } catch (e) {
                toast("Błąd zapisu: " + e.message, "error");
            }
        };
    };

    const load = async () => {
        try {
            const res = await fetch('/api/power/status');
            const data = await res.json();
            state = data;
            render();
        } catch (e) {
            w.content.innerHTML = `<div class="p-3">Błąd ładowania: ${e.message}</div>`;
        }
    };

    const _formatDays = (days) => {
        const names = ['Nd', 'Pn', 'Wt', 'Śr', 'Cz', 'Pt', 'So'];
        return days.map(d => names[d]).join(', ');
    };

    load();
};
