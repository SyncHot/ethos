# EthOS Drive

A desktop sync client for EthOS — like Synology Drive, but for your EthOS NAS.

## Features

- **Two-way file sync** between your PC and EthOS server
- **Real-time monitoring** — changes sync instantly via file system watcher
- **Selective sync** — choose which folders to keep locally
- **Multiple sync tasks** — sync different folder pairs independently
- **Conflict resolution** — automatic or interactive conflict handling
- **File versioning** — restore previous versions of any synced file
- **Bandwidth control** — throttle upload/download speeds, schedule-based limits
- **Filter rules** — exclude files by extension, size, pattern, or `.syncignore`
- **System tray** — minimal footprint, always accessible from taskbar
- **Windows integration** — auto-start, explorer overlay icons, context menu
- **Secure** — token-based auth, HTTPS, no plaintext credential storage

## Requirements

- Windows 10/11 (64-bit)
- Python 3.11+ (for development)
- EthOS server v1.0+ with Sync Drive service enabled

## Development Setup

```bash
# Create virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run in development mode
python -m ethos_drive

# Run tests
pytest tests/ -v
```

## Building

```bash
# Build Windows executable
build\build.bat

# Output: dist\EthOSDriveSetup.exe
```

## Architecture

```
src/ethos_drive/
├── main.py              # Entry point
├── app.py               # Application singleton
├── config.py            # Configuration & credential management
├── sync/
│   ├── engine.py        # Core two-way sync algorithm
│   ├── scanner.py       # Directory scanner with checksums
│   ├── watcher.py       # Real-time filesystem watcher
│   ├── transfer.py      # Chunked upload/download manager
│   ├── conflict.py      # Conflict detection & resolution
│   ├── versioning.py    # Version history tracking
│   ├── filters.py       # Sync filter rules engine
│   └── state.py         # Local sync state database (SQLite)
├── api/
│   ├── client.py        # HTTP API client for EthOS server
│   └── realtime.py      # SocketIO real-time client
├── ui/
│   ├── tray.py          # System tray icon & menu
│   ├── main_window.py   # Main settings/dashboard window
│   ├── login.py         # Login & connection dialog
│   ├── task_editor.py   # Sync task configuration
│   ├── activity.py      # Activity & history view
│   └── conflicts.py     # Conflict resolution dialog
├── platform/
│   ├── windows.py       # Windows-specific integration
│   └── shell_ext.py     # Explorer shell extension
└── utils/
    ├── crypto.py        # Hashing & checksums
    ├── logging.py       # Logging configuration
    └── paths.py         # Path normalization utilities
```

## Sync Protocol

The client communicates with the EthOS server via:
- **REST API** (`/api/sync-drive/...`) for file operations, metadata, state
- **SocketIO** for real-time change notifications from server

### Sync Algorithm

1. **Scan** — Walk local directories, compute file fingerprints (mtime + size + xxhash)
2. **Compare** — Fetch server state, diff against local state DB
3. **Plan** — Classify changes: upload, download, conflict, delete
4. **Resolve** — Apply conflict resolution strategy
5. **Execute** — Transfer files with chunked upload/download, update state
6. **Watch** — Monitor filesystem for incremental changes

## License

Part of the EthOS project.
