---
name: Frontend Conventions
globs: "frontend/**/*.{js,css,html}"
---

# Frontend Conventions

## App registration pattern

Every app in `frontend/js/apps/{name}.js`:

```javascript
AppRegistry['app-id'] = function(appDef, launchOpts) {
    createWindow('app-id', {
        title: t('App Name'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 900, height: 600,
        onRender: (body) => { /* build UI */ },
    });
};
```

## API calls

```javascript
// api() returns PARSED JSON — NEVER call .json() or check .ok on the result
const data = await api('/storage/disks');
if (data.error) { toast(data.error, 'error'); return; }
```

The `api()` helper (in `desktop.js`) automatically injects Bearer token and CSRF header. It handles 401 → login redirect and 403 → password change modal.

## Key globals (NAS object in desktop.js)

- `NAS.token` — current auth token
- `NAS.user.username` — current username (NOT `NAS.username` — that does not exist)
- `NAS.socket` — Socket.IO instance (always guard: `if (!NAS.socket) return;`)
- `NAS.stats` — live system stats

## i18n

Use `t('Key text')` for all user-facing strings. Polish is the source language. Translations live in `frontend/locales/{en,pl,de,fr,es}.json`. Supports interpolation: `t('Hello {name}', { name: 'World' })`.

## CSS rules

- Use CSS custom properties from `:root` — never hardcode colors
- Each app's CSS classes use a prefix (`fm-` File Manager, `dl-` Downloads, `dkr-` Docker, `cam-` Surveillance, `bkp-` Backup, `set-` Settings, `vm-` VM Manager, `mon-` Monitor, `trm-` Terminal, `dte-` Document Editor)
- Dark theme (default) + light via `[data-theme="light"]` overrides
- Text editors/code editors MUST use fixed `background: #fff; color: #000` regardless of theme
- Every CSS class used in HTML MUST have a definition in `style.css` or `apps.css`
- Typography: Inter font family, base 14px
- Breakpoints: Mobile-first at 768px, desktop at 1200px

## Naming

- JavaScript: `camelCase` for functions/variables; `UPPER_SNAKE` for constants
- CSS: `kebab-case` with app prefix

## Common pitfalls

- Do NOT call `.json()` on `api()` result — it already returns parsed JSON
- Use `NAS.user?.username`, never `NAS.username`
- HTML elements must have only ONE `class` attribute — merge: `class="a b"`
- Never send masked API keys (`XXX***XXX`) back to backend — create endpoint that reads original from config
- Before creating a GET endpoint, check if data is already embedded in a parent object
- Always use `NAS.socket` (not bare `socket`) for Socket.IO
