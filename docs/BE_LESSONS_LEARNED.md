# 🧠 Backend — Lessons Learned

> **Format:** `Błąd → Rozwiązanie → Reguła`
> Każdy agent backend MUSI przeczytać ten plik przed generowaniem kodu Python.

---

| # | Błąd | Rozwiązanie | Reguła |
|---|------|-------------|--------|
| 1 | `save_path[len(DATA_ROOT):]` — obcięcie ścieżki string slicingiem gubiło leading `/` | Zastąpiono explicit checkiem: `path if path.startswith('/') else '/' + path` | **Nie manipuluj ścieżkami przez string slicing. Używaj `os.path.relpath()`, `pathlib.Path`, lub explicit walidacji prefixu.** |
| 2 | Nowe serwisy debrid dodane, ale ich klucze API pominięte w maskowaniu (`get_config`) i ochronie (`set_config`) | Dodano nowe klucze do obu list masking/unmasking | **Utrzymuj JEDNĄ stałą `SENSITIVE_KEYS = [...]` i referencuj ją wszędzie. Nie duplikuj list kluczy.** |
| 3 | Blueprint `tickets.py` zwraca `{"ok": true, "item": {...}}` — niezgodne z konwencją innych modułów (np. stickynotes zwraca `{"note": {...}}`) | Ustalono standard: `{"ok": true, "item": {...}}` dla POST/PUT | **Ustal i stosuj JEDEN format odpowiedzi API. Standard EthOS: `{"ok": true, "item": {...}}` dla mutacji, `{"<collection>": [...]}` dla list.** |
| 4 | Endpoint GET dla komentarzy nie istniał, a frontend go wywoływał — komentarze są osadzone w ticketach | Frontend naprawiony (czyta z lokalnego stanu) | **Dokumentuj WSZYSTKIE endpointy w komentarzu na górze blueprintu. Jeśli dane są embedded (np. comments w ticket), NIE twórz osobnego GET — zamiast tego udokumentuj że dane są w parent obiekcie.** |

---

### Wzorzec odpowiedzi API w EthOS:

```python
# Lista zasobów:
return jsonify({'projects': projects_list})

# Tworzenie/aktualizacja:
return jsonify({'ok': True, 'item': new_item}), 201

# Usuwanie:
return jsonify({'ok': True}), 200

# Błąd:
return jsonify({'error': 'Opis błędu'}), 400/403/404
```

### Checklist przed oddaniem kodu backend:
- [ ] Czy ścieżki plików używają `os.path` / `pathlib` zamiast string slicing?
- [ ] Czy wszystkie wrażliwe klucze są w JEDNEJ stałej `SENSITIVE_KEYS`?
- [ ] Czy format odpowiedzi API jest spójny z resztą systemu?
- [ ] Czy wszystkie endpointy są udokumentowane w komentarzu blueprintu?
- [ ] Czy embedded dane (np. comments) nie mają zbędnych osobnych endpointów GET?
