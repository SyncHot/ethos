# 🧠 Frontend — Lessons Learned

> **Format:** `Błąd → Rozwiązanie → Reguła`
> Każdy agent frontend MUSI przeczytać ten plik przed generowaniem kodu JS.

---

| # | Błąd | Rozwiązanie | Reguła |
|---|------|-------------|--------|
| 1 | `api()` zwraca sparsowany JSON, a kod robił `resp.json()` i `resp.ok` — oba failują na plain object | Usunąć `.json()`, sprawdzać `data.error` zamiast `resp.ok` | **`api()` ZAWSZE zwraca sparsowany obiekt JS, NIGDY obiekt Response. Nie wolno wywoływać `.json()` ani `.ok` na wyniku `api()`.** |
| 2 | `loadComments()` wywoływał GET na endpoincie który nie istnieje — komentarze są osadzone w obiekcie ticketu | Zmieniono na sync function czytającą z lokalnego stanu `tickets[]` | **Przed wywołaniem API sprawdź czy dane nie są już dostępne lokalnie (embedded w parent obiekcie). Sprawdź czy endpoint GET istnieje w backendzie.** |
| 3 | Klasy CSS `.tk-form`, `.tk-input` użyte w HTML ale nigdy niezdefiniowane w CSS — formularz wyglądał jak bałagan | Dodano pełne definicje CSS dla `.tk-form`, `.tk-form-group`, `.tk-input` | **Każda klasa CSS użyta w HTML MUSI mieć odpowiadającą definicję w `apps.css`. Nie twórz HTML z klasami "na zapas" — definiuj CSS jednocześnie.** |
| 4 | Duplikat atrybutu `class` na elemencie HTML: `<div class="a" class="b">` — przeglądarka ignoruje drugi | Złączono w jeden atrybut: `class="a b"` | **Element HTML może mieć TYLKO JEDEN atrybut `class`. Zawsze łącz klasy w jednym atrybucie.** |
| 5 | Edytor dokumentów: biały tekst na ciemnym tle (dark mode) — `color: var(--text-primary)` dziedziczył kolor motywu | Wymuszono `background: #fff; color: #000` niezależnie od motywu | **Obszary edycji treści (edytory, pola tekstowe) MUSZĄ mieć stałe kolory niezależne od motywu. Nie używaj `var(--text-primary)` w edytorach.** |
| 6 | Masked API key (`XXX***XXX`) wysyłany z frontendu do testu — backend nie mógł go użyć | Stworzono osobny endpoint `test-saved-debrid` który czyta klucz z configu | **Nigdy nie wysyłaj zamaskowanych wartości z powrotem do backendu jako danych operacyjnych. Jeśli wartość jest zamaskowana, utwórz endpoint który czyta oryginał z serwera.** |

---

### Wzorzec API call w EthOS:

```javascript
// ✅ POPRAWNIE:
const data = await api('/tickets/projects');
// data to już obiekt JS, np. { projects: [...] }

const result = await api('/tickets/projects', { method: 'POST', body: payload });
// result to { ok: true, item: {...} } lub { error: "..." }
if (result.error) throw new Error(result.error);

// ❌ BŁĘDNIE:
const resp = await api('/tickets/projects');
const data = await resp.json();  // CRASH — resp to obiekt, nie Response
if (!resp.ok) throw ...;         // CRASH — .ok nie istnieje na obiekcie
```

### Checklist przed oddaniem kodu frontend:
- [ ] Czy każda klasa CSS użyta w HTML jest zdefiniowana w `apps.css`?
- [ ] Czy `api()` traktowany jest jako zwracający parsed JSON (nie Response)?
- [ ] Czy elementy edytora mają stałe kolory niezależne od motywu?
- [ ] Czy żaden element HTML nie ma duplikatu atrybutu?
- [ ] Czy zamaskowane wartości nie są wysyłane jako dane operacyjne?
