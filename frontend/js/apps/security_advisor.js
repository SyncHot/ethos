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
        html += `<div class="sa-check ${c.passed ? 'sa-passed' : 'sa-failed'}">
            <i class="fas ${icon}" style="color:${color}"></i>
            <div class="sa-check-content">
                <div class="sa-check-title">${c.title} ${badge}</div>
                ${desc}
            </div>
            ${fixBtn}
        </div>`;
    }
    container.innerHTML = html;

    // Fix button handlers
    container.querySelectorAll('.sa-fix-btn').forEach(btn => {
        btn.onclick = async () => {
            const action = btn.dataset.action;
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i>`;
            try {
                const r = await api('/security-advisor/fix', { method: 'POST', body: JSON.stringify({ action }) });
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
