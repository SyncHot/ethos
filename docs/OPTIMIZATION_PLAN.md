# EthOS Optimization Plan

## Overview
This document outlines the strategy for optimizing EthOS for performance, stability, and power efficiency (TDP). Based on consultation with Linux and OS development experts, the following areas have been identified for improvement.

## Optimization Areas

### 1. Application Server (WSGI)
**Current State:**
EthOS runs on the Flask development server (Werkzeug) or `socketio.run` which is not suitable for production workloads.

**Recommendation:**
Migrate to a production-grade WSGI server like **Gunicorn** with **gevent** workers. Place **Nginx** in front as a reverse proxy to handle SSL termination and static file serving.
*   **Ticket:** `[OS] Migrate to Production WSGI (Gunicorn)`
*   **Benefits:** Improved concurrency, better stability, lower memory footprint per request.

### 2. Data Storage
**Current State:**
Data is stored in flat JSON files (`tickets.json`, `resources.json`, etc.). This requires loading the entire dataset into memory for every read/write operation, causing high I/O and memory spikes.

**Recommendation:**
Migrate heavy-write datasets to **SQLite**. SQLite provides a transactional SQL database engine that doesn't require a separate server process and allows for efficient querying and partial updates.
*   **Ticket:** `[OS] Implement SQLite backend for heavy data`
*   **Benefits:** Reduced memory usage, faster reads/writes, atomic transactions.

### 3. Power Management (TDP)
**Current State:**
Standard Linux defaults are used.

**Recommendation:**
Implement aggressive power management policies:
*   **CPU Governor:** Use `schedutil` or `powersave` governor when idle.
*   **Disk Spindown:** Configure `hdparm` to spin down data drives after inactivity.
*   **Peripherals:** Disable unused USB/HDMI ports.
*   **Ticket:** `[OS] Power Management (CPU Governor & Spindown)`

### 4. Container Resources
**Current State:**
Docker containers may not have strict resource limits.

**Recommendation:**
Enforce default memory and CPU limits on all managed containers to prevent OOM kills and CPU starvation of the host OS.
*   **Ticket:** `[OS] Container Resource Limits Enforcement`

### 5. Frontend Optimization
**Current State:**
Frontend assets are served as-is.

**Recommendation:**
*   Minify JS/CSS.
*   Implement `Cache-Control: immutable` for static assets.
*   Reduce polling intervals for dashboard widgets.
*   **Ticket:** `[OS] Frontend Asset Optimization`

### 6. Kernel Tuning
**Current State:**
Default sysctl settings.

**Recommendation:**
Tune kernel parameters for low-memory environments:
*   `vm.swappiness=10` (reduce swapping)
*   `vm.vfs_cache_pressure=50` (prefer keeping inode/dentry cache)
*   Enable ZRAM if physical RAM is limited (<4GB).
*   **Ticket:** `[OS] Kernel Tuning (sysctl)`

## Status
Tickets for these tasks have been created in the Dashboard project (ID: `a026a9c5cf14`).
