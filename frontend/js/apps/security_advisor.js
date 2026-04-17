/* Security Advisor — system security scanner with score and one-click fixes */
AppRegistry['security-advisor'] = function(appDef, launchOpts) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('security-advisor', level, msg, details) : console.log('[security-advisor]', msg, details || '');

    createWindow('security-advisor', {
        title: t('Security Advisor'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 800, height: 600,
        onRender: (body) => {
            body.innerHTML = `
                <div class="sa-container">
                    <div class="sa-header">
                        <div class="sa-score-ring">
                            <svg viewBox="0 0 120 120" class="sa-ring-svg">
                                <circle cx="60" cy="60" r="52" class="sa-ring-bg"/>
                                <circle cx="60" cy="60" r="52" class="sa-ring-fg" id="sa-ring-fg"/>
                            </svg>
                            <div class="sa-score-text" id="sa-score-text">—</div>
                        </div>
                        <div class="sa-summary">
                            <h2>${t('Bezpieczeństwo systemu')}</h2>
                            <p id="sa-summary-text">${t('Kliknij Skanuj, aby sprawdzić system.')}</p>
                            <button class="btn btn-primary" id="sa-scan-btn">
                                <i class="fas fa-shield-alt"></i> ${t('Skanuj system')}
                            </button>
                        </div>
                    </div>
                    <div class="sa-results" id="sa-results"></div>
                </div>`;

            const scanBtn = body.querySelector('#sa-scan-btn');
            scanBtn.onclick = async () => {
                scanBtn.disabled = true;
                scanBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Skanowanie...')}`;
                body.querySelector('#sa-results').innerHTML = '';
                try {
                    const data = await api('/security-advisor/scan');
                    _saRenderResults(body, data);
                } catch(e) {
                    toast(t('Błąd skanowania'), 'error');
                } finally {
                    scanBtn.disabled = false;
                    scanBtn.innerHTML = `<i class="fas fa-shield-alt"></i> ${t('Skanuj system')}`;
                }
            };

            // Auto-scan on open
            scanBtn.click();
        },
    });
};

function _saRenderResults(body, data) {
    if (!data || data.error) { toast(data?.error || 'Error', 'error'); return; }
    const score = data.score || 0;

    // Score ring
    const circumference = 2 * Math.PI * 52;
    const offset = circumference * (1 - score / 100);
    const fg = body.querySelector('#sa-ring-fg');
    if (fg) {
        fg.style.strokeDasharray = circumference;
        fg.style.strokeDashoffset = offset;
        fg.style.stroke = score >= 80 ? '#22c55e' : score >= 50 ? '#f59e0b' : '#ef4444';
    }
    const scoreText = body.querySelector('#sa-score-text');
    if (scoreText) scoreText.textContent = score;

    const summary = body.querySelector('#sa-summary-text');
    if (summary) {
        const label = score >= 80 ? t('Dobry poziom bezpieczeństwa') :
                      score >= 50 ? t('Wymaga poprawy') : t('Niski poziom bezpieczeństwa');
        summary.textContent = `${label} — ${data.passed}/${data.total} ${t('testów zaliczonych')}`;
    }

    // Checks list
    const container = body.querySelector('#sa-results');
    if (!container) return;

    const severityOrder = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
    const checks = (data.checks || []).sort((a, b) => {
        if (a.passed !== b.passed) return a.passed ? 1 : -1;
        return (severityOrder[a.severity] || 4) - (severityOrder[b.severity] || 4);
    });

    let html = '';
    for (const c of checks) {
        const icon = c.passed ? 'fa-check-circle' : 'fa-exclamation-triangle';
        const color = c.passed ? 'var(--success)' : _saSeverityColor(c.severity);
        const badge = c.passed ? '' : `<span class="sa-badge" style="background:${color}">${c.severity.toUpperCase()}</span>`;
        const desc = c.description ? `<div class="sa-check-desc">${c.description}</div>` : '';
        const fixBtn = (!c.passed && c.fixable && c.fix_action)
            ? `<button class="btn btn-small btn-primary sa-fix-btn" data-action="${c.fix_action}"><i class="fas fa-wrench"></i> ${t('Napraw')}</button>`
            : '';
        const linkBtn = (!c.passed && c.link_app)
            ? `<button class="btn btn-small sa-link-btn" data-app="${c.link_app}"><i class="fas fa-external-link-alt"></i> ${c.link_label || t('Otwórz')}</button>`
            : '';
        html += `<div class="sa-check ${c.passed ? 'sa-passed' : 'sa-failed'}">
            <i class="fas ${icon}" style="color:${color}"></i>
            <div class="sa-check-content">
                <div class="sa-check-title">${c.title} ${badge}</div>
                ${desc}
            </div>
            ${fixBtn}${linkBtn}
        </div>`;
    }
    container.innerHTML = html;

    // Dangerous actions that need user confirmation before applying
    const _CONFIRM_ACTIONS = {
        disable_ssh_password: {
            title: t('Wyłączyć logowanie hasłem SSH?'),
            message: t('Upewnij się, że masz skonfigurowany klucz SSH, zanim wyłączysz logowanie hasłem — w przeciwnym razie stracisz dostęp SSH do tego serwera.'),
            confirm: t('Tak, wyłącz'),
        },
    };

    // Fix button handlers
    container.querySelectorAll('.sa-fix-btn').forEach(btn => {
        btn.onclick = async () => {
            const action = btn.dataset.action;

            // Show confirmation dialog for dangerous actions
            const warn = _CONFIRM_ACTIONS[action];
            if (warn) {
                const confirmed = await new Promise(resolve => {
                    const overlay = document.createElement('div');
                    overlay.className = 'sa-confirm-overlay';
                    overlay.innerHTML = `<div class="sa-confirm-dialog">
                        <div class="sa-confirm-icon"><i class="fas fa-exclamation-triangle"></i></div>
                        <div class="sa-confirm-title">${warn.title}</div>
                        <div class="sa-confirm-msg">${warn.message}</div>
                        <div class="sa-confirm-btns">
                            <button class="btn btn-small sa-confirm-cancel">${t('Anuluj')}</button>
                            <button class="btn btn-small btn-danger sa-confirm-ok">${warn.confirm}</button>
                        </div>
                    </div>`;
                    body.appendChild(overlay);
                    overlay.querySelector('.sa-confirm-cancel').onclick = () => { overlay.remove(); resolve(false); };
                    overlay.querySelector('.sa-confirm-ok').onclick = () => { overlay.remove(); resolve(true); };
                });
                if (!confirmed) return;
            }

            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i>`;
            try {
                const r = await api('/security-advisor/fix', { method: 'POST', body: { action } });
                if (r.ok) {
                    toast(r.message || t('Naprawiono'), 'success');
                    // Re-scan
                    const data = await api('/security-advisor/scan');
                    _saRenderResults(body, data);
                } else {
                    toast(r.error || t('Błąd'), 'error');
                }
            } catch(e) {
                toast(t('Błąd naprawy'), 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-wrench"></i> ${t('Napraw')}`;
            }
        };
    });

    container.querySelectorAll('.sa-link-btn').forEach(btn => {
        btn.onclick = () => {
            const appId = btn.dataset.app;
            const appDef = (NAS.apps || []).find(a => a.id === appId);
            if (appDef) openApp(appDef);
        };
    });
}

function _saSeverityColor(sev) {
    switch(sev) {
        case 'critical': return '#dc2626';
        case 'high': return '#ea580c';
        case 'medium': return '#d97706';
        case 'low': return '#2563eb';
        default: return '#6b7280';
    }
}
