/* ═══════════════════════════════════════════════════════════
   EthOS — Fail2Ban Manager
   Manage jails, bans and whitelist
   ═══════════════════════════════════════════════════════════ */

AppRegistry['fail2ban'] = function (appDef) {
    const win = createWindow('fail2ban', {
        title: 'Fail2Ban Manager',
        icon: 'fa-shield-alt',
        iconColor: '#ef4444',
        width: 800,
        height: 600,
        resizable: true,
        maximizable: true
    });

    const body = win.body;
    body.style.display = 'flex';
    body.style.flexDirection = 'column';
    body.style.background = 'var(--bg-default)';
    body.style.color = 'var(--text-default)';
    body.style.padding = '20px';

    // Header
    const header = document.createElement('div');
    header.style.marginBottom = '20px';
    header.innerHTML = `
        <h2 style="margin:0 0 10px 0;display:flex;align-items:center;gap:10px">
            <i class="fas fa-shield-alt" style="color:#ef4444"></i>
            ${t('Ochrona przed atakami (Fail2Ban)')}
        </h2>
        <p style="margin:0;opacity:0.7">${t('Monitorowanie i blokowanie podejrzanych adresów IP (SSH, Samba, Web)')}</p>
    `;
    body.appendChild(header);

    // Whitelist Section
    const whitelistSection = document.createElement('div');
    whitelistSection.style.marginBottom = '20px';
    whitelistSection.className = 'f2b-section';
    whitelistSection.innerHTML = `
        <h3 style="font-size:1.1em;border-bottom:1px solid var(--border);padding-bottom:5px;margin-bottom:10px">
            ${t('Biała lista (IP ignorowane)')}
        </h3>
        <div id="f2b-whitelist" style="font-family:monospace;background:var(--bg-surface);padding:10px;border-radius:6px;border:1px solid var(--border)">
            ${t('Ładowanie...')}
        </div>
    `;
    body.appendChild(whitelistSection);

    // Jails & Bans Section
    const jailsSection = document.createElement('div');
    jailsSection.className = 'f2b-section';
    jailsSection.style.flex = '1';
    jailsSection.style.overflow = 'hidden';
    jailsSection.style.display = 'flex';
    jailsSection.style.flexDirection = 'column';
    jailsSection.innerHTML = `
        <h3 style="font-size:1.1em;border-bottom:1px solid var(--border);padding-bottom:5px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center">
            <span>Aktywne bany</span>
            <button id="f2b-refresh" class="app-btn app-btn-sm"><i class="fas fa-sync-alt"></i> ${t('Odśwież')}</button>
        </h3>
        <div id="f2b-jails" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:15px">
            ${t('Ładowanie...')}
        </div>
    `;
    body.appendChild(jailsSection);

    // Logic
    async function loadData() {
        try {
            // Load whitelist
            const wlRes = await api('/fail2ban/whitelist');
            const wlEl = body.querySelector('#f2b-whitelist');
            if (wlRes.whitelist && wlRes.whitelist.length > 0) {
                wlEl.textContent = wlRes.whitelist.join(', ');
            } else {
                wlEl.textContent = t('Brak (lub domyślne localhost)');
            }

            // Load status/jails
            const statusRes = await api('/fail2ban/status');
            const jailsEl = body.querySelector('#f2b-jails');
            jailsEl.innerHTML = '';

            if (statusRes.error) {
                jailsEl.innerHTML = `<div style="color:var(--text-error)">${t('Błąd:')} ${statusRes.error}</div>`;
                return;
            }

            if (!statusRes.jails || statusRes.jails.length === 0) {
                jailsEl.innerHTML = '<div style="opacity:0.6;font-style:italic">' + t('Brak aktywnych więzień (jails).') + '</div>';
                return;
            }

            statusRes.jails.forEach(jail => {
                const jailCard = document.createElement('div');
                jailCard.style.background = 'var(--bg-surface)';
                jailCard.style.border = '1px solid var(--border)';
                jailCard.style.borderRadius = '8px';
                jailCard.style.padding = '15px';

                const header = document.createElement('div');
                header.style.display = 'flex';
                header.style.justifyContent = 'space-between';
                header.style.marginBottom = '10px';
                header.innerHTML = `
                    <div style="font-weight:bold;font-size:1.1em">
                        <i class="fas fa-lock" style="color:var(--text-accent);margin-right:8px"></i> ${jail.name}
                    </div>
                    <div style="font-size:0.9em;opacity:0.8">
                        Bany: <b>${jail.currently_banned}</b> (Razem: ${jail.total_banned})
                    </div>
                `;
                jailCard.appendChild(header);

                if (jail.banned_ips && jail.banned_ips.length > 0) {
                    const table = document.createElement('table');
                    table.style.width = '100%';
                    table.style.borderCollapse = 'collapse';
                    table.innerHTML = `
                        <tr style="text-align:left;border-bottom:1px solid var(--border);opacity:0.7">
                            <th style="padding:5px">IP</th>
                            <th style="padding:5px;text-align:right">Akcja</th>
                        </tr>
                    `;

                    jail.banned_ips.forEach(ip => {
                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td style="padding:8px 5px;font-family:monospace">${ip}</td>
                            <td style="padding:8px 5px;text-align:right">
                                <button class="app-btn app-btn-xs app-btn-danger unban-btn" data-jail="${jail.name}" data-ip="${ip}">
                                    <i class="fas fa-trash"></i> Unban
                                </button>
                            </td>
                        `;
                        table.appendChild(tr);
                    });
                    jailCard.appendChild(table);
                } else {
                    const empty = document.createElement('div');
                    empty.textContent = t('Brak aktywnych banów.');
                    empty.style.opacity = '0.5';
                    empty.style.fontSize = '0.9em';
                    jailCard.appendChild(empty);
                }

                jailsEl.appendChild(jailCard);
            });

            // Bind unban buttons
            body.querySelectorAll('.unban-btn').forEach(btn => {
                btn.onclick = async () => {
                    const jail = btn.dataset.jail;
                    const ip = btn.dataset.ip;
                    if (!await confirmDialog(t('Czy na pewno odblokować IP') + ' ' + ip + ' ' + t('w sekcji') + ' ' + jail + '?')) return;

                    try {
                        const res = await api('/fail2ban/unban', { method: 'POST', body: { jail, ip } });
                        if (res.success) {
                            toast(`Odblokowano ${ip}`, 'success');
                            loadData();
                        } else {
                            toast(res.error || t('Błąd'), 'error');
                        }
                    } catch (e) {
                        toast(e.message, 'error');
                    }
                };
            });

        } catch (e) {
            console.error(e);
            body.querySelector('#f2b-jails').textContent = t('Błąd ładowania danych:') + ' ' + e.message;
        }
    }

    body.querySelector('#f2b-refresh').onclick = loadData;
    loadData();
};
