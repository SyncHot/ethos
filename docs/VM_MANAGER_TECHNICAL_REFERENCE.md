# EthOS VM Manager — Dokumentacja Techniczna

> **Zakres dokumentu:** `backend/blueprints/vm_manager.py` + `frontend/js/apps/vm-manager.js`
> **Wersja kodu:** main, kwiecien 2026

---

## Spis tresci

1. [System Design & Architektura](#1-system-design--architektura)
2. [Modele Danych](#2-modele-danych)
3. [Architektura Uruchamiania VM](#3-architektura-uruchamiania-vm)
4. [Siec (Networking)](#4-siec-networking)
5. [Konsola VNC / noVNC](#5-konsola-vnc--novnc)
6. [Zarzadzanie Dyskami](#6-zarzadzanie-dyskami)
7. [Snapshotty](#7-snapshotty)
8. [Interfejsy API (Contract)](#8-interfejsy-api-contract)
9. [Specyfikacja Techniczna](#9-specyfikacja-techniczna)
10. [Deployment i Utrzymanie (Ops)](#10-deployment-i-utrzymanie-ops)

---

## 1. System Design & Architektura

### 1.1 High-Level Overview

VM Manager to **Flask Blueprint** (`vm_bp`) ktory uruchamia wirtualne maszyny przez QEMU/KVM.
Kazda VM dziala jako **oddzielny subprocess** (`qemu-system-x86_64` / `qemu-system-aarch64`),
zarzadzany bezposrednio przez EthOS — bez libvirt ani zadnego posrednika.

```
+-----------------------------------------------------------+
|                     EthOS Process                         |
|  +-----------------------------------------------------+  |
|  |         Flask Blueprint /api/vm                     |  |
|  |                                                     |  |
|  |  +------------------+    +---------------------+   |  |
|  |  | _running_vms     |    | vm_state.json        |   |  |
|  |  | (in-memory dict) |    | (VM definitions)     |   |  |
|  |  +------------------+    +---------------------+   |  |
|  +-----------------------------------------------------+  |
|                                                           |
|  subprocess.Popen()   subprocess.Popen()   Popen()       |
|       |                    |                  |           |
|  +----v-----+    +---------v-------+  +------v------+    |
|  | QEMU VM1 |    | websockify      |  | socat proxy |    |
|  | (x86_64) |    | (WS->VNC proxy) |  | (TCP proxy) |    |
|  +----------+    +-----------------+  +-------------+    |
+-----------------------------------------------------------+

Kazda VM = jeden Popen + opcjonalny websockify + 0..N socat proxies
```

**Granice systemu:**

| Wewnetrzne | Zewnetrzne |
|---|---|
| Flask Blueprint HTTP | QEMU subprocesses (qemu-system-x86_64, qemu-system-aarch64) |
| `_running_vms` dict (in-memory) | websockify (WS proxy dla noVNC) |
| `vm_state.json` (definicje VM) | socat (TCP proxy dla port-forwards) |
| noVNC pliki statyczne | KVM (/dev/kvm) |
| | Bridge networking (br0 via nmcli) |

**Komunikacja z innymi modulami EthOS:**

- **Builder**: `GET /api/vm/builder-images` — lista obrazow z `installer/images/`
- **App Manager**: `register_pkg_routes()` — standardowy lifecycle install/uninstall pakietu
- **Autostart**: `vm_autostart_boot()` wywoływane przez `app.py` przy starcie serwisu

### 1.2 Stan w pamieci vs na dysku

```
vm_state.json           _running_vms (RAM)
(definicje VM)          (stan procesow)
     |                       |
     +-- persystentny        +-- tracony przy restarcie EthOS
     +-- ladowany/zapisywany +-- klucz: vm_id
         przy kazdej         +-- wartosc: { proc, pid, started,
         zmianie               vnc_port, ws_port, tap_dev, socat_procs }
```

Po restarcie EthOS: wszystkie VM sa w stanie `stopped` (procesy zginely).
Autostart VM jest zarzadzany przez `vm_autostart_boot()` wylaczane przy starcie serwisu.

---

## 2. Modele Danych

### 2.1 Definicja VM (vm_state.json)

```json
{
  "myvm-name-123456": {
    "name": "Ubuntu Server",
    "cpu": 2,
    "ram": 2048,
    "disk_size": "20G",
    "disk_format": "qcow2",
    "disk_file": "/mnt/data/vms/myvm-name-123456/disk0.qcow2",
    "disks": [
      {
        "id": "disk0",
        "file": "/mnt/data/vms/myvm-name-123456/disk0.qcow2",
        "format": "qcow2",
        "size": "20G",
        "bus": "virtio"
      },
      {
        "id": "disk1",
        "file": "/mnt/data/vms/myvm-name-123456/disk1.qcow2",
        "format": "qcow2",
        "size": "100G",
        "bus": "virtio"
      }
    ],
    "os_type": "linux",
    "boot_image": "/mnt/data/vms/_images/ubuntu-24.04-live-server-amd64.iso",
    "description": "Opcjonalny opis",
    "network": {
      "net_type": "user",
      "port_forwards": [
        {"proto": "tcp", "host": 2222, "guest": 22, "label": "SSH"}
      ]
    },
    "autostart": false,
    "created": "2026-04-08 12:00:00",
    "imported": false
  }
}
```

**Migracja legacy:** Stary format (`disk_file` bez `disks`) jest automatycznie migrowany
do nowej struktury `disks` przy pierwszym zaladowaniu przez `_load_vms()`.

### 2.2 VM ID — generowanie

```python
vm_id = sanitize(name).lower().replace(' ', '-')    # "Ubuntu Server" -> "ubuntu-server"
vm_id = re.sub(r'-+', '-', vm_id)                   # dedupe dashes
vm_id = f"{vm_id}-{str(int(time.time()))[-6:]}"     # dodaj 6-cyfrowy timestamp sufiks
# Przyklad: "ubuntu-server-789012"
```

### 2.3 _running_vms (in-memory)

```python
_running_vms[vm_id] = {
    'proc':        Popen,           # Referencja do procesu QEMU
    'pid':         int,             # PID QEMU
    'started':     float,           # time.time() w momencie startu
    'vnc_port':    int,             # np. 5901 (= 5900 + display)
    'vnc_display': int,             # np. 1
    'ws_port':     int | None,      # port WebSocket dla noVNC, np. 6080
    'ws_proc':     Popen | None,    # proces websockify
    'tap_dev':     str | None,      # np. "vmtap0" (tylko bridge mode)
    'socat_procs': [Popen, ...],    # socat proxy procesy (user-mode)
}
```

### 2.4 Sciezki plikow VM

```
_vm_root()          = /mnt/data/vms/           (jesli data disk dostepny)
                    = /opt/ethos/data/vms/      (fallback)

_iso_root()         = _vm_root()/_images/       (ISO/IMG/QCOW2 do bootowania)

_vm_dir(vm_id)      = _vm_root()/vm_id/
                      +-- disk0.qcow2           (boot disk)
                      +-- disk1.qcow2           (dodatkowe dyski)
                      +-- sd-card.img           (kopia RPi image)
                      +-- kernel8.img           (wyekstrahowany kernel, tylko RPi)
                      +-- bcm2710-rpi-3-b-plus.dtb  (DTB, tylko RPi)

data/vm_state.json                              (definicje wszystkich VM)
data/novnc/                                     (pliki statyczne noVNC UI)
venv/bin/websockify                             (proxy WebSocket -> VNC)
```

---

## 3. Architektura Uruchamiania VM

### 3.1 Trzy sciezki uruchamiania

VM Manager obsluguje 3 architektury, kazda z wlasnym QEMU command set:

```
start_vm(vm_id)
    |
    +-- is_rpi_image()? --YES--> [Raspberry Pi] raspi3b machine
    |
    +-- is_arm_image()? --YES--> [Generic ARM] aarch64/virt machine
    |
    +-- (else) ----------NO---> [x86_64] q35 machine (glowna sciezka)
```

### 3.2 Detekcja architektury (heurystyki)

Architektura jest wykrywana **na podstawie nazwy pliku** (`boot_image` + `vm.name`):

| Warunek | Architektura | Binarka QEMU |
|---|---|---|
| `rpi`, `raspberry`, `raspios`, `raspi` w nazwie | raspi3b | `qemu-system-aarch64` |
| `arm64`, `aarch64`, `armhf`, `-arm` w nazwie | aarch64/virt | `qemu-system-aarch64` |
| (pozostale) | x86_64/q35 | `qemu-system-x86_64` |

Wykrywanie: `_is_rpi_image()` i `_is_arm_image()` sprawdzaja dolne litery nazwy pliku.

### 3.3 Sciezka x86_64 — szczegolowy QEMU command

```
qemu-system-x86_64
  -enable-kvm            (jesli /dev/kvm dostepne)
  -machine q35
  -cpu host|qemu64 -smp CPU_COUNT
  -m RAM_MB

  # Boot image (ISO = cdrom, inne = disk z bootindex=0)
  -cdrom boot.iso -boot d
  LUB
  -drive file=boot.img,format=raw,if=none,id=bootimg,snapshot=on
  -device virtio-blk-pci,drive=bootimg,bootindex=0

  # Dyski VM (disk0 bootindex=1, pozostale bez bootindex)
  -drive file=disk0.qcow2,format=qcow2,if=none,id=disk0
  -device virtio-blk-pci,drive=disk0,bootindex=1

  # Siec (user-mode lub bridge)
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:19022-:22
  -device virtio-net-pci,netdev=net0

  # VNC display
  -vnc :1                (port = 5901)

  # UEFI firmware (wyszukuje OVMF w systemie)
  -bios /usr/share/OVMF/OVMF_CODE.fd

  # USB + wyswietlanie
  -device usb-ehci -device usb-tablet
  -vga virtio
  -monitor none
```

**Kluczowe decyzje projektowe:**

| Decyzja | Powod |
|---|---|
| `-machine q35` (nie `i440fx`) | Wymagane dla UEFI/OVMF; EthOS images sa GPT+EFI |
| `snapshot=on` dla boot images (.img/.raw) | Obraz instalacyjny nie zmienia sie przy bootowaniu |
| `virtio-blk-pci` dla diskow | Najwyzszy throughput w QEMU; wymaga sterownika w gosciu |
| `virtio-gpu` jako VGA | Najlepsza wydajnosc graficzna przez VNC |
| `usb-tablet` | Precyzyjne sledzenie myszy w VNC (absolutne wspolrzedne) |
| `-monitor none` | Upraszcza zarzadzanie; control przez SIGTERM/SIGKILL |

### 3.4 Sciezka Raspberry Pi (raspi3b) — szczegoly

QEMU `raspi3b` **NIE emuluje GPU firmware** (bootcode.bin / start.elf), wiec standardowy
rozruch przez SD card nie dziala. VM Manager automatycznie wydobywa kernel + DTB:

```
RPi .img (SD card) dostarczone przez uzytkownika
         |
         v
Kopia sd-card.img w vm_dir/       (oryginal zachowany bez zmian)
         |
         v
losetup --partscan sd-card.img    (montuje partycje)
Montowanie p1 (boot FAT32)
Kopiowanie kernel8.img + bcm2710-rpi-3-b-plus.dtb do vm_dir/
Odmontowanie
         |
         v
qemu-system-aarch64
  -machine raspi3b
  -kernel kernel8.img
  -dtb bcm2710-rpi-3-b-plus.dtb
  -append "console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait"
  -drive file=sd-card.img,format=raw,if=sd
  -serial mon:tcp:127.0.0.1:VNC_PORT,server=on,wait=off
  -vnc :DISPLAY
```

Ograniczenia raspi3b:
- Stala 1 GB RAM (QEMU ignoruje `-m` dla tej maszyny)
- Ograniczone emulowanie USB (dodatkowe dyski moga nie dzialac)
- Brak sieci uzytkownika (wymaga dedykowanego USB-net)

### 3.5 KVM — akceleracja sprzetowa

```
/dev/kvm dostepne? --YES--> -enable-kvm -cpu host
                             (natywna predkosc CPU hosta)
                    NO  --> -cpu qemu64
                             (~5-20x wolniejsze dla intensywnych obliczen)
```

`_kvm_available()` sprawdza obecnosc `/dev/kvm`. Wymaga:
- BIOS/UEFI: VT-x (Intel) lub AMD-V (AMD) wlaczone
- Kernel: modul `kvm_intel` lub `kvm_amd` zaladowany
- Plik `/dev/kvm` dostepny dla uzytkownika uruchamiajacego EthOS

### 3.6 Zatrzymanie VM

```
POST /machines/<id>/stop
    |
    +-- force=true  --> proc.kill()   (SIGKILL, natychmiastowe)
    |
    +-- force=false --> proc.terminate() (SIGTERM, gracja 10s)
                        po 10s timeout -> proc.kill()

W obu przypadkach:
    _stop_websockify()      - terminacja procesu websockify
    _stop_socat_proxies()   - terminacja procesow socat
    _destroy_tap()          - usuniecie TAP device (tylko bridge mode)
    _running_vms.pop()      - usuniecie z in-memory dict
```

---

## 4. Siec (Networking)

### 4.1 Trzy tryby sieci

| Tryb | Konfiguracja | Dostep z LAN | Setup |
|---|---|---|---|
| `user` (NAT) | domyslny | przez port-forwards | automatyczny |
| `bridge` | VM dostaje IP z DHCP routera | bezposredni | wymaga konfiguracji br0 |
| `none` | brak sieci | - | natychmiastowy |

### 4.2 User-Mode NAT (domyslny) — architektura socat

QEMU user-mode networking ma maly bufor TCP i slaby cleanup polaczen (CLOSE-WAIT accumulation),
ktory blokuje nowe zewnetrzne polaczenia. Rozwiazanie: QEMU binduje na `localhost:internal_port`,
a socat proxy obsluguje zewnetrzny `host_port`:

```
                   +-----------+     hostfwd    +------+
Klient LAN ------> | socat     | ------------>  | QEMU |
:host_port         | :19022    |  127.0.0.1     | VM   |
                   | fork,reuseaddr  :19022      |      |
                   +-----------+               +------+

Przyklad port-forward SSH (host:2222 -> guest:22):
  proxy_map = {2222: 19022}     (19022 = 19000 + 2222 % 1000)
  QEMU: -netdev user,id=net0,hostfwd=tcp:127.0.0.1:19022-:22
  socat: TCP-LISTEN:2222,fork,reuseaddr TCP:127.0.0.1:19022
```

`_find_free_internal_port()` wyznacza internal port deterministycznie:
`candidate = 19000 + (host_port % 1000)`, z fallback +1 jesli zajety.

**Walidacja portow przed uruchomieniem:**
Przed startem VM, dla kazdego `host_port > 0`, kod probuje `socket.bind(('', host_port))`.
Jesli zajety — VM nie startuje, zwracamy czytelny blad: "Port N jest zajety".

### 4.3 Bridge Networking (br0)

Bridge mode daje VM pelny dostep do LAN — VM dostaje swoj IP z DHCP routera.
Konfiguracja przez nmcli, zachowuje MAC primary interface (ten sam IP z DHCP):

```
PRZED bridge:
  eth0 (MAC: aa:bb:cc:dd:ee:ff) --- DHCP ---> 192.168.1.100

PO bridge:
  br0 (MAC: aa:bb:cc:dd:ee:ff) --- DHCP ---> 192.168.1.100
   +-- eth0 (slave)
   +-- vmtap0 (TAP device VM1)
   +-- vmtap1 (TAP device VM2)
```

**Konfiguracja nmcli krok po kroku:**

```
1. Usun stale polaczenia br0 i br0-port (deduplikacja)
2. Pobierz MAC primary interface (eth0)
3. Utwórz bridge connection: nmcli connection add type bridge con-name br0
   ifname br0 stp no 802-3-ethernet.cloned-mac-address MAC
4. Utwórz bridge-slave: nmcli connection add type ethernet con-name br0-port
   ifname eth0 master br0 slave-type bridge
5. Rozlacz istniejace polaczenie na eth0
6. Aktywuj br0-port (eth0 dolacza do bridge)
7. Aktywuj br0
8. Czekaj do 15s na IP przez DHCP
```

**Teardown bridge** (`POST /api/vm/bridge/teardown`):
Usuwa nmcli connections br0 i br0-port, przywraca bezposrednie polaczenie DHCP na eth0.

**TAP device lifecycle:**

```
start_vm() z bridge:
  _create_tap('br0')
    --> ip tuntap add dev vmtap0 mode tap
    --> ip link set vmtap0 master br0
    --> ip link set vmtap0 up
  QEMU: -netdev tap,id=net0,ifname=vmtap0,script=no,downscript=no

stop_vm():
  _destroy_tap('vmtap0')
    --> ip link del vmtap0
```

---

## 5. Konsola VNC / noVNC

### 5.1 Architektura konsoli

```
Przegladarka (noVNC JS)          EthOS Server
       |                              |
       | WebSocket :6080              |
       +----------------------------->+
                                      | websockify (Python)
                                      | WS :6080 <-> TCP :5901
                                      +-----> QEMU VNC :5901
```

Kazda VM ma dedykowane:
- **VNC display**: `display_N`, port = `5900 + N` (N=1..99)
- **WebSocket port**: `6080 + offset` (6080..6179)

`_next_vnc_port()` i `_next_ws_port()` szukaja wolnych portow przez skan `_running_vms`.

### 5.2 Websockify

Websockify to Python proxy ktory konwertuje WebSocket -> raw TCP dla noVNC.
Uruchamiany jako oddzielny subprocess przy kazdym starcie VM:

```python
proc = subprocess.Popen([
    'venv/bin/websockify',
    '--web', 'data/novnc/',     # serwuje pliki statyczne noVNC UI
    str(ws_port),               # port WebSocket (np. 6080)
    f'localhost:{vnc_port}',    # VNC (np. localhost:5901)
], start_new_session=True)
```

Jesli websockify nie jest zainstalowany (`venv/bin/websockify` nie istnieje) —
VM startuje normalnie, ale konsola przegladarkowa jest niedostepna.
Uzytkownicy moga uzyc zewnetrznego klienta VNC.

### 5.3 noVNC pliki statyczne

noVNC UI jest serwowane przez Flask:

```
GET /api/vm/novnc/vnc.html           --> data/novnc/vnc.html
GET /api/vm/novnc/core/rfb.js        --> data/novnc/core/rfb.js
```

Endpoint `/api/vm/novnc/<path>` nie wymaga autentykacji — to pliki statyczne open-source.
Ta sama domena/port co EthOS = brak CORS problem.

### 5.4 Adres IP dla port-forwarded linkow

Frontend VM Managera rozwiazuje IP hosta dla linkow port-forward:

```javascript
let _vmHost = location.hostname;
// Jesli to domena (nie IP) -> query /network/interfaces
if (!/^\d+\.\d+\.\d+\.\d+$/.test(_vmHost)) {
    api('/network/interfaces').then(d => {
        for (const iface of d.interfaces) {
            const v4 = iface.addresses.find(a => a.family === 'inet' && a.scope === 'global');
            if (v4) { _vmHost = v4.address; break; }
        }
    });
}
// Port-forward SSH link: ssh://192.168.1.100:2222
```

Powod: QEMU binduje socat na IP hosta, nie na nazwie domeny.

---

## 6. Zarzadzanie Dyskami

### 6.1 Format dyskow

| Format | Rozszerzenie | Zastosowanie |
|---|---|---|
| qcow2 | `.qcow2` | Domyslny. Sparse (malo miejsca). Obsluguje snapshoty |
| raw | `.raw`, `.img` | Maksymalna wydajnosc. Brak snapshots |
| vdi | `.vdi` | Import z VirtualBox |
| vmdk | `.vmdk` | Import z VMware |
| vhd/vhdx | `.vhd`, `.vhdx` | Import z Hyper-V (tylko import, konwersja do qcow2) |

Tworzenie nowego dysku: `qemu-img create -f qcow2 disk0.qcow2 20G`

### 6.2 Wiele diskow per VM

```
vm['disks'] = [
    {'id': 'disk0', 'file': 'disk0.qcow2', 'format': 'qcow2', 'size': '20G', 'bus': 'virtio'},
    {'id': 'disk1', 'file': 'disk1.qcow2', 'format': 'qcow2', 'size': '100G', 'bus': 'virtio'},
]
```

`disk0` = boot disk (nie mozna usunac).
`disk1..N` = dodatkowe dyski danych (mozna dodawac i usuwac gdy VM zatrzymana).

**Mapowanie na QEMU drives:**

```
disk0 --> -drive file=disk0.qcow2,format=qcow2,if=none,id=disk0
          -device virtio-blk-pci,drive=disk0,bootindex=1
disk1 --> -drive file=disk1.qcow2,format=qcow2,if=none,id=disk1
          -device virtio-blk-pci,drive=disk1
```

### 6.3 Resize dysku

```
POST /machines/<id>/disks/<disk_id>/resize {"size": "+10G"}
     |
     v
qemu-img resize disk.qcow2 +10G

Tylko rozszerzanie (+ prefix). Zmniejszanie NIE jest obslugiwane
(grozi utrata danych, wymaga shrink systemu plikow wewnatrz VM).
```

Po resize dysku — gosc musi jeszcze rozszerzyc partycje i system plikow
(np. `growpart /dev/vda 1 && resize2fs /dev/vda1`).

### 6.4 Import dysku

Endpoint `POST /api/vm/import-disk` akceptuje:
- **Upload pliku** (multipart/form-data) — VMDK/VDI/QCOW2/RAW/VHD
- **Sciezka na serwerze** (JSON `src_path`) — musi byc w dopuszczonych katalogach

Opcjonalna konwersja do qcow2 przez `qemu-img convert`:

```
upload.vmdk/vdi/raw/vhd
    |
    +-- convert=true (domyslne) --> qemu-img convert -O qcow2 src dest
    |                               + usuwa plik tymczasowy po konwersji
    |
    +-- convert=false -----------> uzywany bezposrednio (surowy format)
```

Timeout konwersji: 2 godziny (duze obrazy).

### 6.5 Konwersja formatow

`POST /api/vm/convert` konwertuje pliki miedzy formatami:

```
qemu-img convert -O TARGET_FORMAT source dest
Obslugiwane formaty target: raw, qcow2, vdi, vmdk
Timeout: 600s (10 min)
```

Plik wynikowy jest tworzony obok zrodla (ta sama sciezka, nowe rozszerzenie).

---

## 7. Snapshotty

Snapshoty dostepne **tylko dla QCOW2** (nie raw/vmdk/vdi). VM musi byc zatrzymana.

### 7.1 Operacje

| Operacja | Komenda qemu-img | Warunek |
|---|---|---|
| Lista | `qemu-img snapshot -l disk.qcow2` | dowolny stan VM |
| Tworzenie | `qemu-img snapshot -c tag disk.qcow2` | VM zatrzymana |
| Przywracanie | `qemu-img snapshot -a tag disk.qcow2` | VM zatrzymana |
| Usuwanie | `qemu-img snapshot -d tag disk.qcow2` | dowolny stan VM |

### 7.2 Parsowanie outputu

`qemu-img snapshot -l` zwraca tabele tekstowa. Parsowanie:

```
Snapshot list:
ID        TAG               VM SIZE  DATE             VM CLOCK     ICOUNT
1         clean-install     0 B      2026-04-01 12:00  00:00:00.000 0
2         post-updates      0 B      2026-04-05 15:30  00:12:45.123 0
```

Parsowane od linii 2 (skip headers), pola: id, tag, vm_size, date, time.

**Ograniczenie:** Snapshoty sa tylko dla `disk_file` (boot disk). Dodatkowe dyski (`disk1..N`)
nie sa snapshotowane automatycznie.

---

## 8. Interfejsy API (Contract)

### 8.1 Pelna lista endpointow

| Method | Endpoint | Admin | QEMU | Opis |
|---|---|---|---|---|
| GET | `/api/vm/status` | tak | - | Status QEMU/KVM |
| GET | `/api/vm/machines` | tak | tak | Lista VM |
| POST | `/api/vm/machines` | tak | tak | Utwórz VM |
| GET | `/api/vm/machines/<id>` | tak | tak | Pobierz VM |
| PUT | `/api/vm/machines/<id>` | tak | tak | Aktualizuj konfiguracje VM |
| DELETE | `/api/vm/machines/<id>` | tak | tak | Usun VM i dyski |
| PUT | `/api/vm/machines/<id>/autostart` | tak | tak | Ustaw autostart |
| PUT | `/api/vm/machines/<id>/network` | tak | tak | Aktualizuj siec VM |
| POST | `/api/vm/machines/<id>/start` | tak | tak | Uruchom VM |
| POST | `/api/vm/machines/<id>/stop` | tak | tak | Zatrzymaj VM |
| POST | `/api/vm/machines/<id>/restart` | tak | tak | Restart VM |
| GET | `/api/vm/machines/<id>/disk-info` | tak | tak | Info o boot disku (compat) |
| POST | `/api/vm/machines/<id>/resize-disk` | tak | tak | Resize boot dysku (compat) |
| GET | `/api/vm/machines/<id>/disks` | tak | tak | Lista wszystkich dyskow |
| POST | `/api/vm/machines/<id>/disks` | tak | tak | Dodaj dysk |
| DELETE | `/api/vm/machines/<id>/disks/<disk_id>` | tak | tak | Usun dysk |
| POST | `/api/vm/machines/<id>/disks/<disk_id>/resize` | tak | tak | Resize dysku |
| GET | `/api/vm/machines/<id>/snapshots` | tak | tak | Lista snapshots |
| POST | `/api/vm/machines/<id>/snapshots` | tak | tak | Utwórz snapshot |
| POST | `/api/vm/machines/<id>/snapshots/<tag>` | tak | tak | Przywroc snapshot |
| DELETE | `/api/vm/machines/<id>/snapshots/<tag>` | tak | tak | Usun snapshot |
| GET | `/api/vm/images` | tak | tak | Lista ISO/IMG do bootowania |
| POST | `/api/vm/images` | tak | tak | Upload obrazu |
| DELETE | `/api/vm/images/<filename>` | tak | tak | Usun obraz |
| GET | `/api/vm/builder-images` | tak | - | Obrazy z Builder |
| POST | `/api/vm/builder-images/copy` | tak | tak | Kopiuj obraz Builder -> VM images |
| POST | `/api/vm/import-disk` | tak | tak | Import + opcjonalna konwersja dysku |
| POST | `/api/vm/convert` | tak | tak | Konwertuj format obrazu |
| GET | `/api/vm/bridge` | tak | tak | Status bridge (br0) |
| POST | `/api/vm/bridge/setup` | tak | tak | Skonfiguruj bridge |
| POST | `/api/vm/bridge/teardown` | tak | tak | Usun bridge |
| GET | `/api/vm/novnc/<path>` | NIE | - | Pliki statyczne noVNC |

**Admin**: wymaga `@admin_required` (nie tylko `@require_auth`)
**QEMU**: wymaga zainstalowanego QEMU (dekorator `@_require_qemu`), inaczej HTTP 503

### 8.2 Input Schema

#### `POST /api/vm/machines` — tworzenie VM

```json
{
  "name": "Ubuntu Server",
  "cpu": 2,
  "ram": 2048,
  "disk_size": "20G",
  "disk_format": "qcow2",
  "os_type": "linux",
  "boot_image": "/mnt/data/vms/_images/ubuntu.iso",
  "description": "",
  "network": {
    "net_type": "user",
    "port_forwards": [
      {"proto": "tcp", "host": 2222, "guest": 22, "label": "SSH"},
      {"proto": "tcp", "host": 8080, "guest": 80, "label": "HTTP"}
    ]
  }
}
```

Ograniczenia: CPU 1-32, RAM 256-65536 MB, disk_size regex `^\d+[GMK]?$`.
`os_type`: `linux` (domyslny SSH port-forward) | `windows` (domyslny RDP) | `other`

#### `POST /api/vm/machines/<id>/start`

Brak parametrow. Konfiguracja pochodzi z zapisanej definicji VM.

#### `POST /api/vm/machines/<id>/stop`

```json
{"force": false}   // false = SIGTERM z 10s grace period, true = SIGKILL
```

### 8.3 Response Schema

#### `GET /api/vm/machines` — lista VM

```json
[
  {
    "id": "ubuntu-server-789012",
    "name": "Ubuntu Server",
    "cpu": 2,
    "ram": 2048,
    "disk_size": "20G",
    "os_type": "linux",
    "boot_image": "/mnt/data/vms/_images/ubuntu.iso",
    "autostart": false,
    "status": "running",
    "vnc_port": 5901,
    "vnc_display": 1,
    "ws_port": 6080,
    "pid": 12345,
    "started": 1712500000.0,
    "created": "2026-04-08 12:00:00",
    "description": "",
    "disks": [...],
    "network": {...},
    "arch": "x86_64"
  }
]
```

`arch` jest obliczane dynamicznie przy kazdym listowaniu (na podstawie `boot_image` + `name`).

#### `POST /api/vm/machines/<id>/start` — sukces

```json
{
  "ok": true,
  "pid": 12345,
  "vnc_port": 5901,
  "vnc_display": 1,
  "ws_port": 6080,
  "kvm": true,
  "message": "VM started (VNC: :1)"
}
```

---

## 9. Specyfikacja Techniczna

### 9.1 Zaleznosci

| Zaleznosc | Pakiet apt | Rola | Wymagana |
|---|---|---|---|
| `qemu-system-x86_64` | `qemu-system-x86` | Emulacja x86_64 | TAK |
| `qemu-img` | `qemu-utils` | Tworzenie/resize/konwersja diskow | TAK |
| `qemu-system-aarch64` | `qemu-system-arm` | Emulacja ARM/RPi | Opcjonalna |
| OVMF | `ovmf` | UEFI firmware dla x86_64 | Zalecana |
| AAVMF | `qemu-efi-aarch64` | UEFI firmware dla aarch64 | Opcjonalna |
| `socat` | `socat` | TCP proxy dla port-forwards | Zalecana |
| `websockify` | `pip install websockify` | WebSocket->VNC proxy | Zalecana |
| `losetup`, `ip` | `util-linux`, `iproute2` | TAP i RPi image setup | TAK |
| `nmcli` | `network-manager` | Bridge networking | Dla bridge mode |

### 9.2 OVMF — wyszukiwanie firmware

Kod szuka OVMF/AAVMF w kilku standardowych lokalizacjach (rozne dystrybucje):

```python
# x86_64 UEFI:
ovmf_paths = [
    '/usr/share/OVMF/OVMF_CODE.fd',
    '/usr/share/ovmf/OVMF.fd',
    '/usr/share/qemu/OVMF.fd',
]

# aarch64 UEFI:
aavmf_paths = [
    '/usr/share/AAVMF/AAVMF_CODE.fd',
    '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd',
]
```

Jesli OVMF nie jest znalezione — VM startuje bez UEFI (BIOS legacy mode).
**EthOS images wymagaja UEFI** (GPT + EFI). Bez OVMF, ethos-x86.img nie zbootuje.

### 9.3 Bezpieczenstwo — walidacja sciezek

Wszystkie sciezki do plikow obrazow sa weryfikowane przez `_is_allowed_image_path()`:

```python
_allowed_image_roots() = [
    _iso_root(),                            # /mnt/data/vms/_images/
    _vm_root(),                             # /mnt/data/vms/
    app_path('installer/images'),           # /opt/ethos/installer/images/
    '/media', '/mnt',                       # zamontowane napedy zewnetrzne
]
```

Sciezka spoza tych katalogow zwraca **HTTP 403 Forbidden**.

Dodatkowe zabezpieczenia:
- `_sanitize_name()`: dozwolone `[\w\s\-.]`, max 64 znaki
- `_validate_bridge_name()`: regex `^[a-zA-Z][a-zA-Z0-9\-]{0,14}$`
- `_validate_port_forwards()`: proto in ('tcp', 'udp'), porty 1-65535

### 9.4 Wspolbieznosc

VM Manager **uzywa stdlib `subprocess`**, NIE gevent-patched wersji:

```python
import subprocess  # stdlib, nie gevent
proc = subprocess.Popen(cmd, ..., start_new_session=True)
```

`start_new_session=True` detachuje QEMU od grupy procesow EthOS.
Zabezpiecza przed przypadkowym zakonczeniem VM przy restarcie EthOS.

`_running_vms` nie jest chroniony lockiem — modyfikacje sa z Flask greenletow
ktore sa kooperatywne, ale wielokrotny dostep moze byc problematyczny
przy bardzo duzej liczbie rownoczesnych requestow.

---

## 10. Deployment i Utrzymanie (Ops)

### 10.1 Logowanie

```python
log = logging.getLogger('vm-manager')
```

Logi trafiaja do standardowego `logs/ethos.log` (przez main Flask logger routing).
Nie ma dedykowanego pliku `logs/vm-manager.log`.

Kluczowe zdarzenia logowane:
- Start/stop VM (INFO)
- KVM availability (INFO przy liscie VM)
- socat proxy start/fail (INFO/ERROR)
- TAP device create/fail (INFO/ERROR)
- websockify exit early (ERROR)
- Bridge setup steps (INFO/ERROR)
- RPi kernel/DTB extraction (INFO)

### 10.2 Typowe Problemy i Rozwiazania

| Problem | Symptom | Rozwiazanie |
|---|---|---|
| QEMU nie zainstalowany | HTTP 503 przy kazdym endpoincie | Zainstaluj pakiet VM Manager przez Package Center |
| KVM niedostepne | VM startuje wolno | Wlacz VT-x/AMD-V w BIOS; sprawdz `ls /dev/kvm` |
| OVMF nie znalezione | ethos-x86.img nie bootuje | `apt install ovmf` lub `apt install qemu-system-x86` |
| Port zajety | "Port N jest zajety" przy start VM | Zmien host port w ustawieniach sieci VM |
| socat nie znaleziony | Port-forwards nie dzialaja | `apt install socat`; VM dziala ale porty niedostepne z LAN |
| websockify nie znaleziony | Brak konsoli w przegladarce | `pip install websockify`; uzyj zewnetrznego klienta VNC |
| Bridge bez IP | "Bridge created but did not get an IP" | Sprawdz DHCP router; `nmcli connection show br0` |
| RPi nie bootuje | "Failed to extract kernel/DTB" | Obraz musi byc RPi OS .img (nie skompresowany .gz) |
| VM "running" po restarcie EthOS | Status `running` w UI, ale VM martwa | Odswiezenie listy auto-wykrywa (proc.poll() != None) |
| Snapshot fail | "Snapshots only available for QCOW2" | Uzyj formatu qcow2; konwertuj: POST /api/vm/convert |

### 10.3 Autostart

`vm_autostart_boot()` jest wywolywane przez `app.py` przy starcie EthOS:

```python
# app.py (przy starcie):
from blueprints.vm_manager import vm_autostart_boot
threading.Thread(target=vm_autostart_boot, args=(app,), daemon=True).start()
```

Wewnatrz tworzy flask test_request_context z `g.role = 'admin'` i wywoluje `start_vm(vm_id)`
dla kazdej VM z `autostart=True`. Loguje wynik dla kazdej VM.

### 10.4 Rozmiar danych i data partition

VM disks to zazwyczaj **duze pliki** (dziesilatki-setki GB). Musza byc na data partition:

```
Lokalizacja diskow:
  /mnt/data/vms/          (data partition dostepna)
  /opt/ethos/data/vms/    (fallback — może bardzo szybko wypelnic 4GB root!)
```

ISO obrazy: `/mnt/data/vms/_images/` (kilka GB kazdy).

### 10.5 Czyszczenie po usunieciu VM

```python
DELETE /machines/<vm_id>
    |
    +-- _check_vm_process() == True? --> HTTP 409 (zatrzymaj najpierw)
    |
    +-- shutil.rmtree(_vm_dir(vm_id)) --> usuwa caly katalog z dyskami
    +-- del vms[vm_id]               --> usuwa z vm_state.json
```

ISO obrazy w `_images/` nie sa usuwane przy usuwaniu VM.
Mozna je usunac przez `DELETE /api/vm/images/<filename>` (sprawdza czy nie uzywany).

---

## Appendix A — Pelny cykl zycia VM

```
CREATE VM
  POST /machines
    +-- walidacja (cpu/ram limity, disk_size format, sciezka boot_image)
    +-- generuj vm_id (name-TIMESTAMP)
    +-- mkdir _vm_root()/vm_id/
    +-- qemu-img create -f qcow2 disk0.qcow2 SIZE
    +-- zapisz do vm_state.json
    +-- HTTP 200 {id, name}

START VM
  POST /machines/<id>/start
    +-- _check_vm_process() == True? --> 409
    +-- vm istnieje w vm_state.json? --> 404
    +-- weryfikacja sciezki boot_image
    +-- _next_vnc_port() --> display N, port 5900+N
    +-- wykryj arch (rpi / arm / x86)
    +-- zbuduj QEMU command (patrz sekcja 3.3)
    +-- weryfikacja portow (socket.bind test)
    +-- subprocess.Popen(cmd, start_new_session=True)
    +-- sleep(1) + proc.poll() check
    +-- _start_socat_proxies(proxy_map)
    +-- _start_websockify(vnc_port, ws_port)
    +-- zapisz do _running_vms
    +-- HTTP 200 {pid, vnc_port, ws_port, ...}

VM DZIALA
  GET /machines --> status: "running", pid, ws_port, ...
  Browser: iframe -> /api/vm/novnc/vnc.html?path=api/vm/novnc/&host=...&port=ws_port
  noVNC JS: WebSocket :ws_port <-> websockify <-> QEMU VNC :vnc_port

STOP VM
  POST /machines/<id>/stop {force: false}
    +-- proc.terminate() + wait(10s) + kill jesli timeout
    +-- _stop_websockify()
    +-- _stop_socat_proxies()
    +-- _destroy_tap()
    +-- _running_vms.pop(vm_id)
    +-- HTTP 200 {status: ok}

DELETE VM
  POST /machines/<id> (DELETE)
    +-- VM musi byc zatrzymana
    +-- shutil.rmtree(_vm_dir())  -- usuwa wszystkie dyski
    +-- del vms[vm_id] + zapisz
    +-- HTTP 200 {status: ok}
```

## Appendix B — Architektura sieci user-mode (szczegoly)

```
Host (EthOS NAS) 192.168.1.100
     |
     +-- socat TCP-LISTEN:2222 fork,reuseaddr TCP:127.0.0.1:19022
     |         (pid: socat_procs[0])
     |
     +-- QEMU qemu-system-x86_64 ... -netdev user,id=net0,hostfwd=tcp:127.0.0.1:19022-:22
     |         (pid: proc)
     |         |
     |         +-- QEMU User Networking (SLIRP)
     |             |
     |             +-- VM wewnatrz QEMU: 10.0.2.15 (domyslny SLIRP guest IP)
     |             +-- Gateway SLIRP: 10.0.2.2 (= host)
     |             +-- DNS SLIRP: 10.0.2.3

Polaczenie SSH z LAN:
  Klient --[TCP]:2222--> socat :2222 --[TCP]--> 127.0.0.1:19022
  --[QEMU hostfwd]--> VM :22 (SSH)

Polaczenie z VM do internetu:
  VM --[TCP]--> 10.0.2.2:80 --[SLIRP NAT]--> Internet
  (VM ma pelny dostep do internetu, ale nie moze byc osiagnieta z LAN
   bez jawnych port-forwards)
```
