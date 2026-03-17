# 🧠 UX/UI & CSS — Lessons Learned

> **Format:** `Błąd → Rozwiązanie → Reguła`
> Każdy agent UX/UI MUSI przeczytać ten plik przed generowaniem kodu HTML/CSS.

---

| # | Błąd | Rozwiązanie | Reguła |
|---|------|-------------|--------|
| 1 | Klasy CSS (`.tk-form`, `.tk-input`) użyte w HTML ale niezdefiniowane — formularz bez stylów | Zdefiniowano pełny zestaw: `.tk-form`, `.tk-form-group`, `.tk-form-row`, `.tk-input` | **ZASADA ZERO: Nie wolno użyć klasy CSS w HTML bez jej definicji. HTML i CSS MUSZĄ być tworzone razem, w tym samym tasku.** |
| 2 | Inline style'e (`style="display:flex;gap:8px"`) zamiast klas — niespójność, trudność w utrzymaniu | Zastąpiono klasami `.tk-form-row`, `.tk-color-picker` | **Nie używaj inline `style=""` w EthOS. Każdy pattern wizualny MUSI być klasą CSS z prefixem aplikacji (`.tk-`, `.fm-`, `.dl-`).** |
| 3 | Edytor dokumentów: `var(--text-primary)` w dark mode = biały tekst na białym tle | Wymuszone stałe kolory `#fff`/`#000` niezależne od motywu | **Obszary treści użytkownika (edytory, podglądy) MUSZĄ mieć hardcoded kolory. Zmienne motywu (`var(--*)`) tylko dla chrome UI.** |
| 4 | Duplikat `class=""` na jednym elemencie — przeglądarka ignoruje drugi atrybut | Złączono w jeden `class="a b c"` | **HTML element = JEDEN atrybut `class`. Zawsze łącz. Sprawdź w review.** |
| 5 | Modalne formularze bez `.tk-form-group` wrapper — labele i inputy zlane w jeden strumień | Każdy label+input opakowany w `<div class="tk-form-group">` | **Każda para label+input MUSI być w kontenerze `.{prefix}-form-group` z `margin-bottom`. Bez wyjątków.** |

---

### Wzorzec formularza w EthOS:

```html
<!-- ✅ POPRAWNIE: -->
<div class="tk-form">
    <div class="tk-form-group">
        <label>Nazwa</label>
        <input type="text" class="tk-input" placeholder="...">
    </div>
    <div class="tk-form-group">
        <label>Opis</label>
        <textarea class="tk-input" rows="3"></textarea>
    </div>
    <div class="tk-form-row">
        <div class="tk-form-group" style="flex:1;">
            <label>Priorytet</label>
            <select class="tk-input">...</select>
        </div>
        <div class="tk-form-group" style="flex:1;">
            <label>Assignee</label>
            <select class="tk-input">...</select>
        </div>
    </div>
</div>

<!-- ❌ BŁĘDNIE: -->
<label>Nazwa</label>
<input type="text" style="width:100%;padding:8px;" placeholder="...">
```

### Konwencja prefixów CSS:
| Aplikacja | Prefix |
|-----------|--------|
| Tickets/Kanban | `.tk-` |
| File Manager | `.fm-` |
| Downloads | `.dl-` |
| Documents | `.doceditor-` |
| Builder | `.builder-` |

### Checklist przed oddaniem kodu UX/UI:
- [ ] Czy KAŻDA klasa CSS użyta w HTML ma definicję w `apps.css`?
- [ ] Czy ZERO inline `style=""` (poza wyjątkami dynamicznymi z JS)?
- [ ] Czy formularze używają `.{prefix}-form-group` wrapperów?
- [ ] Czy edytory/podglądy treści mają hardcoded kolory (#fff/#000)?
- [ ] Czy żaden element nie ma duplikatu atrybutu HTML?
- [ ] Czy przetestowano w dark mode I light mode?
