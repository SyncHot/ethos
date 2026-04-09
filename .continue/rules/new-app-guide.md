---
name: New Application Development
globs: "**/*.{py,js,css}"
---

# Creating a New EthOS Application

Every EthOS app has 4 components:

```
1. BACKEND BLUEPRINT     backend/blueprints/{app}.py
2. FRONTEND JS           frontend/js/apps/{app}.js
3. FRONTEND CSS          frontend/css/apps.css (prefixed classes)
4. REGISTRATION          backend/app.py (import, register, get_apps, _API_TO_APP, index.html script tag)
```

## Dependency handling

Every new app must work on remotely deployed images built by the Builder app. Never assume dependencies are pre-installed:

```python
# Backend: check and install on demand
def _ensure_deps():
    try:
        import some_package
    except ImportError:
        host_run('pip install some_package', timeout=120)
```

```javascript
// Frontend: surface install status to user
const data = await api('/myapp/check-deps');
if (!data.ready) {
    body.innerHTML = `<button onclick="installDeps()">Install dependencies</button>`;
    return;
}
```

## App registration in app.py

1. Import the blueprint at the top of `backend/app.py`
2. Register it with `app.register_blueprint()`
3. Add an entry to `get_apps()` (provides icon, title, color for the desktop)
4. Add route prefix to `_API_TO_APP` mapping for permission checks
5. Add `<script>` tag in `frontend/index.html`

## Optional blueprints

Optional apps loaded by `load_optional_blueprints()` must NOT be hard-imported at the top of `app.py` — that causes `ModuleNotFoundError` on built images where the file was stripped.

## Data flow

```
User clicks icon → desktop.js calls AppRegistry[id]()
  → createWindow(id, { onRender: (body) => render(body) })
    → render(body) sets body.innerHTML, binds events
      → api('/app/action') → fetch with Bearer token
        → Flask @blueprint.route('/api/app/action')
          → load_json() / save_json() from data/
            → return jsonify({ok: true, ...})
```
