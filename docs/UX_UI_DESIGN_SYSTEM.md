# EthOS — System Designu UX/UI

> Przygotowane przez: **UX/UI Designer & QA Lead** | Model referencyjny: Claude Sonnet / GPT-4o-mini
> Data: 2026-03-17

---

## Spis Treści

1. [Filozofia Projektowania](#1-filozofia-projektowania)
2. [System Designu: Tokeny i Zmienne CSS](#2-system-designu-tokeny-i-zmienne-css)
3. [Typografia i Ikonografia](#3-typografia-i-ikonografia)
4. [Komponenty UI](#4-komponenty-ui)
5. [Architektura Dashboard](#5-architektura-dashboard)
6. [System Aplikacji](#6-system-aplikacji)
7. [Wzorce Nawigacji](#7-wzorce-nawigacji)
8. [Responsywność i Mobile](#8-responsywność-i-mobile)
9. [Dostępność (a11y)](#9-dostępność-a11y)
10. [Przewodnik po Ciemnym/Jasnym Motywie](#10-przewodnik-po-ciemnymjasnym-motywie)

---

## 1. Filozofia Projektowania

### Metafora Pulpitu (Desktop Metaphor)

EthOS wykorzystuje metaforę systemu operacyjnego desktop jako paradygmat interakcji z urządzeniem NAS.
Użytkownik widzi znajomy interfejs — pulpit, pasek zadań, okna aplikacji — co drastycznie
obniża barierę wejścia w porównaniu z tradycyjnymi panelami administracyjnymi NAS (Synology DSM,
TrueNAS WebUI, Unraid).

**Kluczowe założenia:**

- **Znajomość** — użytkownik domowy powinien czuć się jak w systemie Windows/macOS
- **Zarządzanie bez terminala** — operacje na dyskach, sieci, kontenerach — wszystko przez GUI
- **Jednopunktowy dostęp** — jeden URL (port 9000 / HTTPS 443), jedna sesja, wszystkie narzędzia

### Prostota vs. Funkcjonalność — Progressive Disclosure

Centralnym wyzwaniem EthOS jest balans między prostotą (użytkownik domowy) a mocą
(zaawansowany administrator). Rozwiązanie: **progressive disclosure**.

```
┌─────────────────────────────────────────────────┐
│  Warstwa 1: Widok podstawowy                    │
│  → Pliki, zdjęcia, pobieranie, status           │
│  → Widoczne od razu po zalogowaniu              │
├─────────────────────────────────────────────────┤
│  Warstwa 2: Zarządzanie                         │
│  → Docker, RAID, backup, użytkownicy            │
│  → Dostępne z app launcher                      │
├─────────────────────────────────────────────────┤
│  Warstwa 3: Zaawansowane                        │
│  → Terminal, builder, konfiguracja sieci        │
│  → Ukryte za "Ustawienia zaawansowane"          │
└─────────────────────────────────────────────────┘
```

**Zasady projektowe:**

1. **Domyślna prostota** — najczęstsze akcje wymagają ≤2 kliknięć
2. **Kontekstowe rozszerzenia** — zaawansowane opcje pojawiają się przy potrzebie
3. **Informacja zwrotna** — każda akcja generuje toast notification lub zmianę stanu wizualnego
4. **Odwracalność** — operacje destrukcyjne wymagają potwierdzenia w modal overlay
5. **Spójność** — ten sam komponent zachowuje się identycznie w każdej aplikacji

### Inspiracje i Benchmarki

| System          | Co bierzemy                | Czego unikamy              |
|-----------------|---------------------------|---------------------------|
| Windows 11      | Zarządzanie oknami, taskbar | Złożoność Settings        |
| macOS           | Elegancja animacji         | Brak customizacji         |
| Synology DSM 7  | Metafora desktop NAS       | Ciężkość i wolne ładowanie|
| Unraid          | Elastyczność dyskowa       | Archaiczny UI             |

---

## 2. System Designu: Tokeny i Zmienne CSS

### Mechanizm Themingu

EthOS implementuje system motywów przez atrybut HTML `[data-theme]` na elemencie `<html>`:

```html
<!-- Jasny motyw (domyślny) -->
<html data-theme="light">

<!-- Ciemny motyw -->
<html data-theme="dark">
```

Przełączanie motywu odbywa się w Settings i jest persystowane w localStorage.

### Pełna Mapa Tokenów CSS

#### Kolory Tła (Background)

| Zmienna CSS       | Jasny motyw   | Ciemny motyw  | Zastosowanie                          |
|-------------------|---------------|---------------|---------------------------------------|
| `--bg-primary`    | `#ffffff`     | `#1a1a2e`     | Główne tło aplikacji, okna           |
| `--bg-base`       | `#f5f5f7`     | `#16213e`     | Tło pulpitu, tło sekcji              |
| `--bg-hover`      | `#e8e8ed`     | `#1f3056`     | Stan hover na elementach listy        |

#### Kolory Tekstu (Text)

| Zmienna CSS       | Jasny motyw   | Ciemny motyw  | Zastosowanie                          |
|-------------------|---------------|---------------|---------------------------------------|
| `--text-primary`  | `#1d1d1f`     | `#e4e4e8`     | Główny tekst, nagłówki               |
| `--text-muted`    | `#86868b`     | `#8b8b9e`     | Tekst pomocniczy, opisy, timestampy  |

#### Akcenty i Interakcje

| Zmienna CSS       | Jasny motyw   | Ciemny motyw  | Zastosowanie                          |
|-------------------|---------------|---------------|---------------------------------------|
| `--accent`        | `#0071e3`     | `#4da3ff`     | Przyciski primary, linki, zaznaczenia|
| `--border`        | `#d2d2d7`     | `#2a2a4a`     | Obramowania kart, separatory         |

#### Cienie i Zaokrąglenia

| Zmienna CSS       | Wartość               | Zastosowanie                          |
|-------------------|-----------------------|---------------------------------------|
| `--shadow-sm`     | `0 1px 3px rgba(...)` | Karty, dropdowny, popovery           |
| `--r-sm`          | `6px`                 | Border-radius małych elementów        |

### Reguły Używania Tokenów

```css
/* ✅ POPRAWNIE — używaj zmiennych */
.card {
  background: var(--bg-primary);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  box-shadow: var(--shadow-sm);
}

/* ❌ BŁĘDNIE — hardcoded kolory */
.card {
  background: #ffffff;
  color: #333333;
  border: 1px solid #ddd;
}
```

**ZASADA:** Nigdy nie używaj hardcoded kolorów w komponentach. Jedyne miejsce na wartości
hex/rgb to definicje zmiennych w `:root` i `[data-theme="dark"]`.

### Struktura Plików CSS

```
style.css          — zmienne globalne, reset, layout desktop, taskbar, modals
apps.css           — style specyficzne per aplikacja (prefiksowane klasami)
mobile/            — nadpisania dla widoku mobilnego
```

---

## 3. Typografia i Ikonografia

### Font Family: Inter

EthOS używa rodziny fontów **Inter** z Google Fonts jako jedynego fontu systemowego.

```css
font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
```

### Skala Typograficzna

| Rola                  | Rozmiar   | Waga  | Linia  | Zastosowanie                    |
|-----------------------|-----------|-------|--------|---------------------------------|
| Heading 1             | 24px      | 700   | 1.2    | Tytuł okna aplikacji           |
| Heading 2             | 20px      | 600   | 1.3    | Nagłówki sekcji                |
| Heading 3             | 16px      | 600   | 1.4    | Pod-nagłówki, nazwy kart       |
| Body                  | 14px      | 400   | 1.5    | Główny tekst, opisy            |
| Body Small            | 13px      | 400   | 1.4    | Tekst pomocniczy, tabele       |
| Caption               | 12px      | 400   | 1.3    | Timestampy, badges, metadata   |
| Monospace             | 13px      | 400   | 1.5    | Terminal, kod, ścieżki plików  |

### System Ikon: Font Awesome 6.5.1

Ikony ładowane z CDN:
```html
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
```

#### Konwencje Nazewnictwa Ikon per Aplikacja

| Aplikacja        | Ikona główna              | Klasa CSS                    |
|------------------|--------------------------|------------------------------|
| File Manager     | `fa-folder`              | `fa-solid fa-folder`         |
| Storage Manager  | `fa-hard-drive`          | `fa-solid fa-hard-drive`     |
| Backup Manager   | `fa-clock-rotate-left`   | `fa-solid fa-clock-rotate-left` |
| Download Manager | `fa-download`            | `fa-solid fa-download`       |
| Docker Manager   | `fa-cube`                | `fa-solid fa-cube`           |
| Surveillance     | `fa-video`               | `fa-solid fa-video`          |
| Gallery          | `fa-images`              | `fa-solid fa-images`         |
| Document Editor  | `fa-file-lines`          | `fa-solid fa-file-lines`     |
| Builder          | `fa-microchip`           | `fa-solid fa-microchip`      |
| Monitor          | `fa-chart-line`          | `fa-solid fa-chart-line`     |
| Settings         | `fa-gear`                | `fa-solid fa-gear`           |
| Network          | `fa-network-wired`       | `fa-solid fa-network-wired`  |
| Users            | `fa-users`               | `fa-solid fa-users`          |
| AI Chat          | `fa-robot`               | `fa-solid fa-robot`          |
| Terminal         | `fa-terminal`            | `fa-solid fa-terminal`       |

#### Rozmiary Ikon

| Kontekst          | Klasa modyfikatora  | Rozmiar  |
|-------------------|---------------------|----------|
| Inline z tekstem  | (brak)              | 14px     |
| Przycisk          | `fa-sm`             | 12px     |
| Lista aplikacji   | `fa-lg`             | 20px     |
| App Launcher      | `fa-2x`             | 32px     |
| Pusty stan        | `fa-3x`             | 48px     |

---

## 4. Komponenty UI

### 4.1. Przyciski (Buttons)

#### Hierarchia Przycisków

```html
<!-- Primary — główna akcja -->
<button class="btn btn-primary">
  <i class="fa-solid fa-save"></i> Zapisz
</button>

<!-- Secondary — akcja drugorzędna -->
<button class="btn btn-secondary">
  <i class="fa-solid fa-times"></i> Anuluj
</button>

<!-- Danger — akcja destrukcyjna -->
<button class="btn btn-danger">
  <i class="fa-solid fa-trash"></i> Usuń
</button>

<!-- Ghost — akcja trzeciorzędna (link-like) -->
<button class="btn btn-ghost">Więcej opcji</button>
```

#### Style CSS Przycisków

```css
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: var(--r-sm);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, box-shadow 0.15s;
  border: 1px solid transparent;
}

.btn-primary {
  background: var(--accent);
  color: #ffffff;
  border-color: var(--accent);
}
.btn-primary:hover {
  filter: brightness(1.1);
  box-shadow: var(--shadow-sm);
}

.btn-secondary {
  background: var(--bg-hover);
  color: var(--text-primary);
  border-color: var(--border);
}

.btn-danger {
  background: #e53935;
  color: #ffffff;
  border-color: #c62828;
}
```

### 4.2. Modale (Modals)

#### Struktura HTML

```html
<div class="modal-overlay" id="confirm-delete">
  <div class="modal-box">
    <div class="modal-header">
      <h3>Potwierdź usunięcie</h3>
      <button class="modal-close">&times;</button>
    </div>
    <div class="modal-body">
      <p>Czy na pewno chcesz usunąć ten plik? Tej operacji nie można cofnąć.</p>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeModal('confirm-delete')">Anuluj</button>
      <button class="btn btn-danger" onclick="deleteFile()">Usuń</button>
    </div>
  </div>
</div>
```

#### Style CSS Modali

```css
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
  backdrop-filter: blur(2px);
}

.modal-box {
  background: var(--bg-primary);
  border-radius: 12px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
  width: 90%;
  max-width: 520px;
  max-height: 80vh;
  overflow-y: auto;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.modal-body {
  padding: 20px;
  color: var(--text-primary);
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--border);
}

### 4.3. Elementy Formularzy (Select i Checkbox)

Elementy formularzy w EthOS zostały ujednolicone, aby przypominały styl File Managera i AppStore.
Style są aplikowane globalnie na tagi `<select>` i `<input type="checkbox">`.

#### Select Box

Elementy `<select>` mają usunięty domyślny wygląd przeglądarki (`appearance: none`) i zastąpiony stylem spójnym (tło `var(--bg-elevated)`, obramowanie, chevron).

```css
select {
  appearance: none;
  background-color: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  color: var(--text);
  padding: 6px 24px 6px 8px; /* Miejsce na ikonę po prawej */
  /* Ikona chevron jest dodawana przez background-image */
  background-position: right 6px center;
  font-size: 13px;
}

select:focus {
  border-color: var(--accent);
  outline: none;
}
```

Dla selectów wewnątrz formularzy z klasą `.fm-input`, stosowane są odpowiednie dostosowania paddingu i pozycji ikony.

#### Checkbox

Checkboxy mają spójny wygląd 16x16px z obramowaniem `var(--text-muted)` i akcentem po zaznaczeniu.
Unikaj stosowania `transform: scale(...)` do powiększania checkboxów — domyślny styl zapewnia odpowiedni rozmiar i czytelność.

```css
input[type="checkbox"] {
  width: 16px;
  height: 16px;
  border: 2px solid var(--text-muted);
  border-radius: var(--r-sm);
  cursor: pointer;
}

input[type="checkbox"]:checked {
  background: var(--accent);
  border-color: var(--accent);
}
```

```

### 4.4. Toast Notifications

```
┌──────────────────────────────────────────┐
│ ✅  Plik został zapisany pomyślnie       │  ← success
└──────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│ ⚠️  Dysk osiągnął 90% pojemności         │  ← warning
└──────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│ ❌  Błąd połączenia z serwerem           │  ← error
└──────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│ ℹ️  Aktualizacja dostępna: v2.4.1        │  ← info
└──────────────────────────────────────────┘
```

**Wywołanie w JavaScript:**

```javascript
showToast('Plik zapisany', 'success');
showToast('Dysk prawie pełny', 'warning');
showToast('Błąd zapisu', 'error');
showToast('Nowa wersja dostępna', 'info');
```

**Zachowanie:** Toast pojawia się w prawym górnym rogu, auto-dismiss po 4s (error: 6s),
kliknięcie zamyka natychmiast.

### 4.5. Formularze (Form Inputs)

```html
<div class="form-group">
  <label class="form-label" for="share-name">Nazwa udostępnienia</label>
  <input class="form-input" type="text" id="share-name" placeholder="np. Dokumenty">
  <span class="form-hint">Nazwa widoczna w sieci lokalnej</span>
</div>

<div class="form-group">
  <label class="form-label" for="protocol">Protokół</label>
  <select class="form-select" id="protocol">
    <option value="smb">SMB/CIFS</option>
    <option value="nfs">NFS</option>
    <option value="webdav">WebDAV</option>
  </select>
</div>

<div class="form-group">
  <label class="form-check">
    <input type="checkbox" checked> Udostępnij tylko do odczytu
  </label>
</div>
```

### 4.6. Tabele (Tables)

```html
<table class="data-table">
  <thead>
    <tr>
      <th>Nazwa</th>
      <th>Rozmiar</th>
      <th>Zmodyfikowany</th>
      <th>Akcje</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><i class="fa-solid fa-file-pdf"></i> raport.pdf</td>
      <td>2.4 MB</td>
      <td>2026-03-15 14:32</td>
      <td>
        <button class="btn btn-ghost btn-sm"><i class="fa-solid fa-download"></i></button>
        <button class="btn btn-ghost btn-sm"><i class="fa-solid fa-share"></i></button>
      </td>
    </tr>
  </tbody>
</table>
```

### 4.7. Tabs / Panels

```html
<div class="tab-bar">
  <button class="tab active" data-tab="general">Ogólne</button>
  <button class="tab" data-tab="network">Sieć</button>
  <button class="tab" data-tab="advanced">Zaawansowane</button>
</div>
<div class="tab-panel active" id="tab-general">
  <!-- Zawartość zakładki -->
</div>
```

### 4.8. Karty (Cards)

```html
<div class="card">
  <div class="card-header">
    <i class="fa-solid fa-hard-drive"></i>
    <span>Dysk 1 — WD Red 4TB</span>
  </div>
  <div class="card-body">
    <div class="progress-bar">
      <div class="progress-fill" style="width: 67%"></div>
    </div>
    <span class="text-muted">2.68 TB / 4.00 TB wykorzystane</span>
  </div>
</div>
```

### 4.9. Paski Postępu (Progress Bars)

```html
<div class="progress-bar">
  <div class="progress-fill" style="width: 45%"></div>
</div>
```

```css
.progress-bar {
  height: 8px;
  background: var(--bg-hover);
  border-radius: 4px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--accent);
  border-radius: 4px;
  transition: width 0.3s ease;
}
/* Warianty kolorystyczne */
.progress-fill.warning { background: #ff9800; }
.progress-fill.danger  { background: #e53935; }
```

---

## 5. Architektura Dashboard

### Struktura Pulpitu

```
┌─────────────────────────────────────────────────────────────┐
│                         DESKTOP                              │
│                                                              │
│  ┌──────────────────────┐  ┌──────────────────────┐         │
│  │ ▬ File Manager    ─□× │  │ ▬ Monitor         ─□× │       │
│  │┌────────────────────┐│  │┌────────────────────┐│         │
│  ││ 📁 Documents       ││  ││ CPU ████░░░░ 45%   ││         │
│  ││ 📁 Photos          ││  ││ RAM ██████░░ 72%   ││         │
│  ││ 📁 Backups         ││  ││ Disk ███████░ 89%  ││         │
│  ││ 📄 readme.txt      ││  ││ Temp 52°C          ││         │
│  │└────────────────────┘│  │└────────────────────┘│         │
│  └──────────────────────┘  └──────────────────────┘         │
│                                                              │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ [≡] [📁] [🐳] [📊] [⬇️] [📷]           🔔  🌙  👤  14:32  │
│                    TASKBAR                                    │
└──────────────────────────────────────────────────────────────┘
```

### Zarządzanie Oknami (desktop.js — 84KB)

**desktop.js** odpowiada za:

1. **Tworzenie okien** — generuje DOM z titlebar, przyciskami minimize/maximize/close
2. **Drag & drop** — przeciąganie okien za titlebar
3. **Resize** — zmiana rozmiaru przez krawędzie okna
4. **Z-index stacking** — aktywne okno zawsze na wierzchu
5. **Minimize do taskbar** — animacja i ikona na pasku zadań
6. **Maximize** — pełnoekranowe rozszerzenie okna
7. **Snap** — (opcjonalnie) przyciąganie do krawędzi ekranu

#### Cykl Życia Okna

```
    [App Launcher klik]
           │
           ▼
    createWindow(appId)
           │
           ▼
    ┌─────────────┐
    │  RENDERING   │ ─── generateWindowHTML()
    └──────┬──────┘      attachDragListeners()
           │              attachResizeListeners()
           ▼
    ┌─────────────┐
    │   ACTIVE     │ ─── z-index = ++maxZ
    └──────┬──────┘      focus state
           │
    ┌──────┴───────────────┐
    │         │             │
    ▼         ▼             ▼
 [MINIMIZE] [MAXIMIZE]  [CLOSE]
    │         │             │
    ▼         │             ▼
 TASKBAR      │        destroyWindow()
 ICON         │        removeFromDOM()
    │         │        cleanupListeners()
    ▼         │
 [RESTORE] ◄──┘
    │
    ▼
  ACTIVE
```

### Taskbar

Pasek zadań na dole ekranu zawiera:

- **Menu Start / App Launcher** — lewy róg, grid ikon wszystkich aplikacji
- **Pinned Apps** — często używane aplikacje
- **Running Apps** — otwarte okna (kliknięcie przywraca/minimalizuje)
- **System Tray** — prawy róg: powiadomienia, przełącznik motywu, profil użytkownika, zegar

### App Launcher

```
┌────────────────────────────────────────┐
│  🔍 Szukaj aplikacji...                │
├────────────────────────────────────────┤
│                                        │
│  📁 Pliki      💾 Dyski     🔄 Backup  │
│                                        │
│  ⬇️ Pobieranie  🐳 Docker   📷 Kamery  │
│                                        │
│  🖼️ Galeria    📝 Edytor   🏗️ Builder  │
│                                        │
│  📊 Monitor    ⚙️ Ustawienia 🌐 Sieć   │
│                                        │
│  👥 Użytkownicy 🤖 AI Chat  💻 Terminal │
│                                        │
└────────────────────────────────────────┘
```

---

## 6. System Aplikacji

### AppRegistry — Wzorzec Rejestracji

Wszystkie aplikacje rejestrują się przez centralny `AppRegistry` w pliku **apps.js** (349KB).

```javascript
// Wzorzec rejestracji aplikacji
AppRegistry.register('file-manager', {
  name: 'File Manager',
  icon: 'fa-solid fa-folder',
  category: 'system',
  singleton: true,     // tylko jedno okno
  defaultSize: { width: 900, height: 600 },
  init: function(windowEl) {
    // Inicjalizacja DOM i logiki
  },
  destroy: function() {
    // Cleanup przy zamykaniu
  }
});
```

### Prefiksy Klas CSS per Aplikacja

Każda aplikacja używa unikalnego prefiksu klasy CSS, aby uniknąć kolizji stylów:

| Aplikacja        | Prefiks  | Przykładowe klasy                          |
|------------------|----------|--------------------------------------------|
| Document Editor  | `dte-`   | `.dte-toolbar`, `.dte-page`, `.dte-ruler`  |
| File Manager     | `fm-`    | `.fm-sidebar`, `.fm-breadcrumb`, `.fm-grid`|
| Download Manager | `dl-`    | `.dl-queue`, `.dl-progress`, `.dl-stats`   |
| Surveillance     | `surv-`  | `.surv-grid`, `.surv-feed`, `.surv-timeline`|
| Storage Manager  | `sm-`    | `.sm-drive-list`, `.sm-raid-config`        |
| Docker Manager   | `dk-`    | `.dk-container`, `.dk-compose`, `.dk-store`|

### Cykl Życia Aplikacji

```
  AppRegistry.register()
         │
         ▼
  [Kliknięcie w App Launcher]
         │
         ▼
  desktop.createWindow(appId)
         │
         ▼
  app.init(windowElement)
         │
         ├── Render DOM wewnątrz okna
         ├── Bind event listeners
         ├── Fetch danych z API (fetch/Socket.IO)
         └── Zarejestruj interval/timeout jeśli potrzeba
         │
         ▼
  [Okno aktywne — interakcja użytkownika]
         │
         ▼
  [Kliknięcie Close]
         │
         ▼
  app.destroy()
         │
         ├── Wyczyść intervaly/timeouty
         ├── Odłącz event listenery
         ├── Odłącz Socket.IO listenery
         └── Cleanup stanu
```

### Pliki Aplikacji

Poza monolitycznym `apps.js`, poszczególne aplikacje mają dedykowane pliki w `js/apps/`:

```
js/apps/
├── file-manager.js
├── storage-manager.js
├── backup-manager.js
├── download-manager.js
├── docker-manager.js
├── surveillance.js
├── gallery.js
├── document-editor.js
├── builder.js
├── monitor.js
├── settings.js
├── network.js
├── users.js
├── ai-chat.js
├── terminal.js
└── ... (25+ plików)
```

---

## 7. Wzorce Nawigacji

### Przeglądarka Plików (File Browser Pattern)

```
┌──────────────────────────────────────────────────┐
│ ← → ↑  │  /home/user/Documents/Projects          │  ← Breadcrumb
├─────────┼────────────────────────────────────────┤
│ 📁 Home │ Nazwa           Rozmiar  Zmodyfikowany  │
│ 📁 Docs │ 📁 ethos-core   —        2026-03-15     │
│ 📁 Photos│ 📁 backups     —        2026-03-14     │
│ 📁 Music│ 📄 notes.md     12 KB    2026-03-12     │
│ 📁 Video│ 📄 config.yml   2 KB     2026-03-10     │
│ 💾 Dysk1│                                         │
│ 💾 Dysk2│                                         │
├─────────┴────────────────────────────────────────┤
│ 4 elementy │ 14 KB zaznaczono                      │
└──────────────────────────────────────────────────┘
```

**Elementy nawigacyjne:**

- **Breadcrumb** — klikalny path bar, każdy segment to link
- **Sidebar / drzewo folderów** — hierarchiczna nawigacja po lewej
- **Nawigacja wstecz/do przodu** — historia nawigacji per okno
- **Wejście do folderu** — podwójne kliknięcie otwiera folder
- **Kontekst menu** — prawy przycisk myszy → kopiuj, wytnij, wklej, zmień nazwę, usuń, udostępnij

### Dialog Zapisu/Otwarcia (Save/Open Modal)

Dialogi zapisu i otwierania pliku pojawiają się jako modale z wbudowanym file browserem:

```
┌─────────────────────────────────────────┐
│ Zapisz jako...                     [×]  │
├─────────────────────────────────────────┤
│ Lokalizacja: /home/user/Documents/ [📁] │
│                                         │
│ 📁 Projects                             │
│ 📁 Work                                 │
│ 📁 Personal                             │
│                                         │
├─────────────────────────────────────────┤
│ Nazwa pliku: [dokument.docx          ]  │
│ Format:      [DOCX                  ▾]  │
├─────────────────────────────────────────┤
│              [Anuluj]  [💾 Zapisz]      │
└─────────────────────────────────────────┘
```

---

## 8. Responsywność i Mobile

### Strategia Adaptacji

EthOS obsługuje dwa tryby:

1. **Desktop** — pełna metafora pulpitu z oknami (≥1024px)
2. **Mobile** — uproszczony widok full-screen per aplikacja (≤768px)

### Katalog `mobile/`

Dedykowany katalog `mobile/` zawiera nadpisania CSS i logiki JS dla urządzeń mobilnych:

- Okna rozszerzają się na pełny ekran
- Taskbar zamienia się w bottom navigation bar
- Brak drag & drop okien
- Elementy touch-friendly: min 44×44px target size
- Swipe gestures dla nawigacji wstecz

### Breakpointy

```css
/* Desktop pełny */
@media (min-width: 1024px) { /* domyślny layout */ }

/* Tablet */
@media (max-width: 1023px) and (min-width: 769px) {
  /* Zmniejszone okna, simplified taskbar */
}

/* Mobile */
@media (max-width: 768px) {
  /* Full-screen apps, bottom nav */
}
```

### Adaptacja Komponentów

| Komponent       | Desktop              | Mobile                    |
|-----------------|---------------------|---------------------------|
| Okno            | Floating, resizable | Full-screen               |
| Taskbar         | Bottom bar + tray   | Bottom navigation (5 ikon)|
| App Launcher    | Grid overlay        | Full-screen list          |
| File Browser    | Sidebar + main      | Tylko main, hamburger menu|
| Modals          | Centered 520px      | Full-width bottom sheet   |
| Tables          | Pełna tabela        | Card layout               |

---

## 9. Dostępność (a11y)

### Aktualny Stan

EthOS jako Vanilla JS SPA ma ograniczoną dostępność out-of-the-box.
Poniżej audyt i rekomendacje.

### Rekomendacje — Priorytet Wysoki

#### ARIA Labels

```html
<!-- ✅ Dodaj role i aria-label do kluczowych elementów -->
<div class="taskbar" role="navigation" aria-label="Pasek zadań">
<div class="app-launcher" role="menu" aria-label="Uruchamianie aplikacji">
<div class="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="modal-title">
<button class="btn btn-danger" aria-label="Usuń plik dokument.pdf">
```

#### Nawigacja Klawiaturowa

```
Tab        → Przechodzenie między elementami interaktywnymi
Enter/Space → Aktywacja przycisku / linku
Escape     → Zamknięcie modalu / dropdownu
Arrow keys → Nawigacja w listach, tabelach, menu
```

**Do implementacji:**
- Focus trap w otwartych modalach
- Widoczny focus ring (`outline`) — nie usuwać `outline: none` globalnie
- Skip-to-content link dla screen readerów
- `aria-live="polite"` na kontenerze toast notifications
- Alt text na wszystkich obrazkach (Gallery, File Manager)

#### Kontrast Kolorów

Zweryfikować WCAG AA (4.5:1) dla:
- `--text-muted` na `--bg-primary` — może wymagać korekty w dark mode
- `--accent` na `--bg-primary` — sprawdzić oba motywy
- Ikony placeholder — zapewnić wystarczający kontrast

### Rekomendacje — Priorytet Średni

- Semantyczny HTML: `<nav>`, `<main>`, `<aside>`, `<header>`, `<footer>` zamiast `<div>`
- `<table>` z `<caption>` i `<th scope="col/row">`
- `prefers-reduced-motion` — respektowanie preferencji animacji
- `prefers-color-scheme` — automatyczne wykrywanie motywu systemowego

---

## 10. Przewodnik po Ciemnym/Jasnym Motywie

### Mechanizm Przełączania

```javascript
// Przełączanie motywu
function toggleTheme() {
  const html = document.documentElement;
  const current = html.getAttribute('data-theme');
  const next = current === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}

// Inicjalizacja przy starcie
const savedTheme = localStorage.getItem('theme') || 'light';
document.documentElement.setAttribute('data-theme', savedTheme);
```

### Częste Pułapki i Jak Ich Unikać

#### Bug #1: Biały Tekst na Białym Tle (Document Editor)

**Problem:** Edytor dokumentów miał obszar strony z wymuszonym `background: white` (symulacja
kartki papieru), ale tekst dziedziczył `color: var(--text-primary)`, który w dark mode
daje jasny kolor → biały tekst na białym tle.

**Rozwiązanie:**

```css
/* Obszary z wymuszonym tłem MUSZĄ mieć wymuszony kolor tekstu */
.dte-page {
  background: #ffffff;
  color: #1d1d1f;  /* Hardcoded — nie var()! */
}
```

**ZASADA:** Jeśli element ma hardcoded background, MUSI mieć hardcoded color.

#### Bug #2: Duplikacja Atrybutu Class

**Problem:** Element HTML z dwoma atrybutami `class=""` — przeglądarka ignoruje drugi.

```html
<!-- ❌ BŁĘDNIE — drugi class jest ignorowany -->
<div class="panel" class="dark-panel">

<!-- ✅ POPRAWNIE — połączone klasy -->
<div class="panel dark-panel">
```

### Checklist dla Nowych Komponentów

- [ ] Wszystkie kolory przez `var(--nazwa)` — żadnych hardcoded hex
- [ ] Wyjątek: jeśli element ma stały background (kartka, preview), ustaw jawnie oba: bg + color
- [ ] Testuj w OBU motywach przed commitem
- [ ] Sprawdź hover state w obu motywach
- [ ] Sprawdź disabled state w obu motywach
- [ ] Sprawdź focus ring widoczność w obu motywach
- [ ] Border widoczny zarówno w jasnym jak ciemnym motywie
- [ ] Cienie (box-shadow) wyglądają dobrze w obu motywach
- [ ] Ikony (Font Awesome) nie zlewają się z tłem

### Testowanie Motywów — Szybki Skrót

```javascript
// W DevTools Console — szybkie przełączanie
document.documentElement.setAttribute('data-theme', 'dark');
document.documentElement.setAttribute('data-theme', 'light');
```

---

*Dokument jest żywą referencją — aktualizuj przy każdej zmianie w systemie designu.*
