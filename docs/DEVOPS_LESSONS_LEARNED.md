# 🧠 DevOps & Integration — Lessons Learned

> **Format:** `Błąd → Rozwiązanie → Reguła`
> Każdy agent MUSI przeczytać ten plik przed rejestracją nowej aplikacji lub modyfikacją pipeline.

---

| # | Błąd | Rozwiązanie | Reguła |
|---|------|-------------|--------|
| 1 | Nowa aplikacja (tickets) zarejestrowana w 4 z 5 wymaganych miejsc — brak wpisu powodował 404 lub brak ikony | Checklist 5 miejsc: import, register_blueprint, _API_TO_APP, get_apps(), index.html script tag | **Rejestracja nowej appki = ZAWSZE 5 edycji. Użyj checklisty z APP_DEVELOPMENT_GUIDE.md. Pomiń jedno = app nie działa.** |
| 2 | Builder: budowanie obrazu nie startowało — zależność od RPi została usunięta ale config nie zaktualizowany | Usunięto RPi, zostawiono x86-only | **Po zmianie scope'u platformy (np. usunięcie ARM) sprawdź WSZYSTKIE pliki konfiguracyjne, nie tylko kod.** |
| 3 | Po zmianach w backendzie zapomniano `sudo systemctl restart ethos` — stary kod serwowany | Dodano restart do procedury | **Po KAŻDEJ zmianie w `backend/` MUSISZ wykonać `sudo systemctl restart ethos`. Frontend (js/css) NIE wymaga restartu.** |

---

### Rejestracja nowej aplikacji — 5 kroków:

```
1. backend/app.py ~linia 87:   from blueprints.{name} import {name}_bp
2. backend/app.py ~linia 141:  app.register_blueprint({name}_bp)
3. backend/app.py ~linia 379:  '/api/{prefix}/': '{name}'   w _API_TO_APP
4. backend/app.py ~linia 6054: { id, name, icon, ... }      w get_apps()
5. frontend/index.html ~209:   <script src="js/apps/{name}.js?v=1"></script>
```

### Checklist przed oddaniem zmian DevOps:
- [ ] Czy nowa app jest zarejestrowana we WSZYSTKICH 5 miejscach?
- [ ] Czy `sudo systemctl restart ethos` wykonany po zmianach backend?
- [ ] Czy pliki danych (`/opt/ethos/data/`) mają właściwe uprawnienia?
- [ ] Czy commit zawiera `Co-authored-by: Copilot <...>`?
