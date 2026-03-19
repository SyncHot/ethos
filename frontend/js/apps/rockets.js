/* ─────────────────── Rockets App ─────────────────── */
/* globals AppRegistry, createWindow, NAS, api, toast, t */

AppRegistry['rockets'] = function (appDef, launchOpts) {
    const winId = 'rockets';
    
    // Check if already open
    if (WM.windows.has(winId)) {
        focusWindow(winId);
        if (WM.windows.get(winId).minimized) restoreWindow(winId);
        return;
    }

    createWindow(winId, {
        title: t('Rockets'),
        icon: appDef?.icon || 'fa-rocket',
        iconColor: appDef?.color || '#ef4444',
        width: 800,
        height: 600,
        onRender: (body) => renderRockets(body),
    });

    function renderRockets(body) {
        body.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;text-align:center;padding:2rem;">
                <div style="font-size:4rem;color:#ef4444;margin-bottom:1rem;">
                    <i class="fas fa-rocket"></i>
                </div>
                <h1 style="font-size:2rem;margin-bottom:1rem;">Rockets</h1>
                <p>Start your engines!</p>
                <div id="rockets-status" style="margin-top:1rem;color:#888;">Checking status...</div>
            </div>
        `;

        api('/rockets/status')
            .then(res => {
                const el = document.getElementById('rockets-status');
                if (el) el.textContent = res.message;
            })
            .catch(err => {
                const el = document.getElementById('rockets-status');
                if (el) el.textContent = 'Backend error: ' + err.message;
            });
    }
};
