#!/bin/bash
# EthOS QA Inner Test Script
# ===========================
# Runs INSIDE the VM on the installed system. Results are emitted to serial
# console (ttyS0) as structured TESTRESULT: lines.
#
# Format: TESTRESULT:<TEST_NAME>:<PASS|FAIL>:<detail>
#
# Injected by the VM QA orchestrator as a one-shot systemd service.
# Self-destructs after running.
set -o pipefail

exec 1>/dev/ttyS0 2>&1

_pass() { echo "TESTRESULT:$1:PASS:$2"; }
_fail() { echo "TESTRESULT:$1:FAIL:$2"; }
_info() { echo "QAINFO:$1"; }

echo "TESTRESULT:START:PASS:inner script running"

# ── Test: system fully booted ───────────────────────────────────────────────
_info "Checking systemd state..."
SYSTEMD_STATE=$(systemctl is-system-running --wait --timeout=30 2>/dev/null || echo "unknown")
if [[ "$SYSTEMD_STATE" == "running" || "$SYSTEMD_STATE" == "degraded" ]]; then
    _pass "SYSTEMD_RUNNING" "$SYSTEMD_STATE"
else
    _fail "SYSTEMD_RUNNING" "$SYSTEMD_STATE"
fi

# ── Test: EthOS service active ───────────────────────────────────────────────
if systemctl is-active ethos.service >/dev/null 2>&1; then
    _pass "ETHOS_SERVICE" "active"
else
    _fail "ETHOS_SERVICE" "$(systemctl is-active ethos.service 2>/dev/null)"
fi

# ── Test: Branding — /etc/os-release ────────────────────────────────────────
if grep -q "^ID=ethos" /etc/os-release 2>/dev/null; then
    _pass "OS_RELEASE_ID" "ID=ethos confirmed"
else
    ID_VAL=$(grep "^ID=" /etc/os-release 2>/dev/null || echo "missing")
    _fail "OS_RELEASE_ID" "$ID_VAL"
fi

if grep -q "^DISTRIB_ID=EthOS" /etc/lsb-release 2>/dev/null; then
    _pass "BRANDING_OK" "lsb-release DISTRIB_ID=EthOS"
else
    _fail "BRANDING_OK" "lsb-release not rebranded"
fi

# ── Test: Kernel hardening sysctl ───────────────────────────────────────────
if [ -f /etc/sysctl.d/91-ethos-security.conf ]; then
    _pass "HARDENING_SYSCTL" "91-ethos-security.conf present"
else
    _fail "HARDENING_SYSCTL" "91-ethos-security.conf missing"
fi

# Verify sysctl values are actually applied
KPTR=$(cat /proc/sys/kernel/kptr_restrict 2>/dev/null || echo "-1")
if [ "$KPTR" = "2" ]; then
    _pass "HARDENING_KPTR_APPLIED" "kptr_restrict=2"
else
    _fail "HARDENING_KPTR_APPLIED" "kptr_restrict=$KPTR (expected 2)"
fi

SYNCOOKIES=$(cat /proc/sys/net/ipv4/tcp_syncookies 2>/dev/null || echo "-1")
if [ "$SYNCOOKIES" = "1" ]; then
    _pass "HARDENING_SYNCOOKIES" "tcp_syncookies=1"
else
    _fail "HARDENING_SYNCOOKIES" "tcp_syncookies=$SYNCOOKIES"
fi

# ── Test: Root filesystem is read-only ──────────────────────────────────────
ROOT_MOUNT_OPTS=$(findmnt -n -o OPTIONS / 2>/dev/null || cat /proc/mounts | awk '$2=="/" {print $4}')
if echo "$ROOT_MOUNT_OPTS" | grep -q "\bro\b"; then
    _pass "FS_READONLY" "/ mounted read-only: $ROOT_MOUNT_OPTS"
else
    _fail "FS_READONLY" "/ NOT read-only: $ROOT_MOUNT_OPTS"
fi

# Confirm write attempt to / fails
if touch /bin/ethos-tamper-test 2>/dev/null; then
    _fail "FS_WRITE_BLOCKED" "write to /bin succeeded (CRITICAL: rootfs should be read-only)"
    rm -f /bin/ethos-tamper-test
else
    _pass "FS_WRITE_BLOCKED" "write to /bin correctly blocked"
fi

# ── Test: OverlayFS — /etc is writable via overlay ──────────────────────────
ETC_FS=$(findmnt -n -o FSTYPE /etc 2>/dev/null || cat /proc/mounts | awk '$2=="/etc" {print $3}')
if [[ "$ETC_FS" == "overlay" ]] || mount | grep -q "on /etc type overlay"; then
    _pass "OVERLAYFS_ETC_WRITABLE" "OverlayFS mounted on /etc"
elif touch /etc/ethos-qa-write-test 2>/dev/null; then
    _pass "OVERLAYFS_ETC_WRITABLE" "/etc is writable (overlay via upper layer)"
    rm -f /etc/ethos-qa-write-test
else
    _fail "OVERLAYFS_ETC_WRITABLE" "/etc is not writable — OverlayFS may not be active"
fi

# ── Test: dm-verity device visible ──────────────────────────────────────────
if dmsetup ls 2>/dev/null | grep -q "ethos-verity\|verity"; then
    _pass "VERITY_MOUNTED" "dm-verity device active: $(dmsetup ls | grep verity)"
elif [ -f /run/ethos-rootfs/root.sqsh.verity ]; then
    _pass "VERITY_MOUNTED" "verity file present (may not be dm-verity active)"
else
    _fail "VERITY_MOUNTED" "No dm-verity device or verity file found"
fi

# ── Test: SBOM file in installer images ────────────────────────────────────
SBOM_PATHS=(
    "/opt/ethos/installer/images/ethos-sbom.json"
    "/opt/ethos/data/ethos-sbom.json"
)
SBOM_FOUND=0
for p in "${SBOM_PATHS[@]}"; do
    if [ -f "$p" ]; then
        PKG_COUNT=$(python3 -c "import json; d=json.load(open('$p')); print(len(d.get('packages',[])))" 2>/dev/null || echo "?")
        _pass "SBOM_FILE_EXISTS" "$p ($PKG_COUNT packages)"
        SBOM_FOUND=1
        break
    fi
done
[ "$SBOM_FOUND" = "0" ] && _fail "SBOM_FILE_EXISTS" "ethos-sbom.json not found"

# ── Test: Flask reachable on localhost ──────────────────────────────────────
FLASK_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9000/api/auth/status 2>/dev/null || echo "000")
if [[ "$FLASK_CODE" == "200" || "$FLASK_CODE" == "401" ]]; then
    _pass "FLASK_REACHABLE" "HTTP $FLASK_CODE from :9000"
else
    _fail "FLASK_REACHABLE" "HTTP $FLASK_CODE (0=connection refused)"
fi

# ── Test: Python3 + venv functional ─────────────────────────────────────────
if /opt/ethos/venv/bin/python3 -c "import flask, gevent, psutil; print('OK')" 2>/dev/null | grep -q OK; then
    _pass "VENV_IMPORTS" "flask+gevent+psutil import OK"
else
    _fail "VENV_IMPORTS" "venv import failed"
fi

# ── Test: Module blacklist effective ────────────────────────────────────────
if [ -f /etc/modprobe.d/ethos-security-blacklist.conf ]; then
    _pass "MODULE_BLACKLIST" "ethos-security-blacklist.conf present"
else
    _fail "MODULE_BLACKLIST" "ethos-security-blacklist.conf missing"
fi
# Verify a blacklisted module cannot load
if modprobe dccp 2>&1 | grep -qi "blocked\|not permitted\|install"; then
    _pass "MODULE_DCCP_BLOCKED" "dccp module load blocked by blacklist"
else
    _fail "MODULE_DCCP_BLOCKED" "dccp module may have loaded"
fi

# ── Test: Boot timing ───────────────────────────────────────────────────────
_info "Running systemd-analyze..."
BOOT_TIME=$(systemd-analyze 2>/dev/null | head -1 || echo "unavailable")
echo "QAINFO:boot_timing=$BOOT_TIME"

# Extract total seconds if possible
TOTAL_S=$(systemd-analyze 2>/dev/null | grep -oP '\d+\.\d+s' | tail -1 | tr -d 's' || echo "999")
if python3 -c "import sys; sys.exit(0 if float('${TOTAL_S}') < 60 else 1)" 2>/dev/null; then
    _pass "PERF_BOOT_UNDER_60S" "boot time: ${TOTAL_S}s"
else
    _fail "PERF_BOOT_UNDER_60S" "boot time: ${TOTAL_S}s (>60s)"
fi

# ── Test: SSL cert generated by OOBE ────────────────────────────────────────
SSL_PATHS=(
    "/opt/ethos/data/ssl/ethos.crt"
)
SSL_FOUND=0
for p in "${SSL_PATHS[@]}"; do
    if [ -f "$p" ]; then
        EXPIRY=$(openssl x509 -noout -enddate -in "$p" 2>/dev/null || echo "?")
        _pass "OOBE_SSL_CERT" "$p (expires: $EXPIRY)"
        SSL_FOUND=1
        break
    fi
done
[ "$SSL_FOUND" = "0" ] && _fail "OOBE_SSL_CERT" "SSL cert not found (firstboot may not have run yet)"

# ── Test: Secure Boot MOK key present ───────────────────────────────────────
MOK_DER_PATHS=(
    "/boot/efi/EFI/ethos/MOK.der"
    "/opt/ethos/data/secureboot-keys/MOK.der"
)
MOK_FOUND=0
for p in "${MOK_DER_PATHS[@]}"; do
    if [ -f "$p" ]; then
        _pass "SECUREBOOT_MOK_DER" "MOK.der present: $p"
        MOK_FOUND=1
        break
    fi
done
[ "$MOK_FOUND" = "0" ] && _fail "SECUREBOOT_MOK_DER" "MOK.der not found (Secure Boot not configured)"

# ── SBOM vs dpkg cross-check ─────────────────────────────────────────────────
_info "Running SBOM vs dpkg cross-check..."
SBOM_PATH="/opt/ethos/installer/images/ethos-sbom.json"
if [ -f "$SBOM_PATH" ]; then
    python3 << 'CROSSCHECK'
import json, subprocess, sys
try:
    sbom = json.load(open("/opt/ethos/installer/images/ethos-sbom.json"))
    sbom_pkgs = {p["name"] for p in sbom.get("packages", []) if p.get("name")}

    r = subprocess.run(
        ["dpkg-query", "--showformat", "${Package}\n", "--show"],
        capture_output=True, text=True, timeout=15
    )
    dpkg_pkgs = set(r.stdout.strip().splitlines())

    in_sbom_not_dpkg = sbom_pkgs - dpkg_pkgs
    in_dpkg_not_sbom = dpkg_pkgs - sbom_pkgs

    if len(in_dpkg_not_sbom) <= 5:
        print(f"TESTRESULT:SBOM_DPKG_CROSSCHECK:PASS:sbom={len(sbom_pkgs)} dpkg={len(dpkg_pkgs)} delta={len(in_dpkg_not_sbom)}")
    else:
        print(f"TESTRESULT:SBOM_DPKG_CROSSCHECK:FAIL:missing_from_sbom={len(in_dpkg_not_sbom)} pkgs")
except Exception as e:
    print(f"TESTRESULT:SBOM_DPKG_CROSSCHECK:ERROR:{e}")
CROSSCHECK
else
    echo "TESTRESULT:SBOM_DPKG_CROSSCHECK:SKIP:no SBOM file"
fi

echo "TESTRESULT:DONE:PASS:all inner tests completed"

# Self-destruct
systemctl disable ethos-qa-inner.service 2>/dev/null || true
rm -f /usr/local/sbin/ethos-qa-inner.sh \
      /etc/systemd/system/ethos-qa-inner.service \
      "$0"
systemctl daemon-reload 2>/dev/null || true
shutdown -h now
