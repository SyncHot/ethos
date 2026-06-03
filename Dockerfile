# =============================================================================
# EthOS - NAS Operating System Docker Image
# Multi-stage build with all system dependencies
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Build frontend (if needed in future)
# ─────────────────────────────────────────────────────────────────────────────
FROM node:18-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/ ./
RUN npm ci --omit=dev && npm run build || true

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Production image with Python + system dependencies
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS production

LABEL maintainer="EthOS Team"
LABEL description="EthOS - Synology DSM-inspired NAS Operating System"
LABEL version="1.0.147"

# Environment variables
ENV ETHOS_ROOT=/opt/ethos \
    DATA_DIR=/opt/ethos/data \
    LOG_DIR=/opt/ethos/logs \
    PORT=9000 \
    NAS_NAME=EthOS \
    TZ=Europe/Warsaw \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install system dependencies in one layer for better caching
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ── Core utilities ──
    curl wget ca-certificates gnupg \
    sudo coreutils util-linux \
    \
    # ── Storage & disk management ──
    smartmontools mdadm lvm2 parted udisks2 udevil \
    hdparm ntfs-3g exfatprogs e2fsprogs \
    \
    # ── File sharing protocols ──
    samba smbclient nfs-kernel-server \
    lighttpd vsftpd minidlna \
    \
    # ── Network & WiFi ──
    wpasupplicant dnsmasq rfkill wireless-tools iw \
    avahi-daemon \
    \
    # ── Hardware monitoring ──
    lm-sensors usbutils pciutils \
    \
    # ── Archive tools ──
    rsync p7zip-full unrar-free zstd tar gzip bzip2 xz-utils \
    \
    # ── Multimedia ──
    ffmpeg imagemagick ghostscript poppler-utils \
    enscript tesseract-ocr libtesseract-dev \
    \
    # ── Document processing ──
    libreoffice-writer libreoffice-common \
    cups cups-client \
    \
    # ── Virtualization (optional - large packages) ──
    # qemu-system-x86 qemu-utils ovmf \
    # qemu-system-arm qemu-efi-aarch64 \
    \
    # ── Security & firewall ──
    ufw fail2ban openssh-server \
    \
    # ── Web server & SSL ──
    nginx certbot \
    \
    # ── Development tools (for runtime compilation) ──
    build-essential gcc g++ make cmake \
    pkg-config libffi-dev libssl-dev \
    libjpeg-dev libpng-dev libtiff-dev \
    \
    # ── Python dependencies for pytesseract, PyMuPDF, etc. ──
    tesseract-ocr-eng tesseract-ocr-pol \
    \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create application directories
RUN mkdir -p ${ETHOS_ROOT}/backend \
             ${ETHOS_ROOT}/frontend \
             ${DATA_DIR} \
             ${LOG_DIR} \
             /opt/ethos/backups \
             /opt/ethos/uploads \
    && chown -R root:root ${ETHOS_ROOT} \
    && chmod -R 755 ${ETHOS_ROOT}

WORKDIR ${ETHOS_ROOT}/backend

# Copy Python dependencies first (better Docker layer caching)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ .
COPY frontend/ ../frontend/

# Create necessary runtime files/directories
RUN mkdir -p ${DATA_DIR}/stickynotes \
             ${DATA_DIR}/ssh_keys \
             ${DATA_DIR}/.thumb_cache \
    && touch ${LOG_DIR}/auth.log \
             ${LOG_DIR}/access.log \
             ${LOG_DIR}/ethos.log

# Expose ports
EXPOSE 9000/tcp
EXPOSE 22/tcp
EXPOSE 445/tcp
EXPOSE 139/tcp
EXPOSE 80/tcp
EXPOSE 443/tcp

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health || exit 1

# Start the application
CMD ["python", "app.py"]
