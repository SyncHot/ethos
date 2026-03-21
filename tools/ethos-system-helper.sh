#!/bin/bash
set -e

# EthOS System Helper
# Wrapper for privileged operations to allow restricted sudo usage.

COMMAND="$1"
shift

# Common validation functions
validate_dev() {
    local dev="$1"
    if [[ ! -b "$dev" ]]; then
        echo "Error: Device '$dev' not found or not a block device" >&2
        exit 1
    fi
    # Prevent system disk usage
    # Simple check: cannot be root (/) or /boot
    local mp
    mp=$(lsblk -dn -o MOUNTPOINT "$dev" 2>/dev/null || true)
    if [[ "$mp" == "/" || "$mp" == "/boot" ]]; then
        echo "Error: Cannot operate on system disk" >&2
        exit 1
    fi
}

case "$COMMAND" in
    systemctl)
        ACTION="$1"
        # Support optional --now flag between ACTION and SERVICE
        NOW_FLAG=""
        if [[ "$2" == "--now" ]]; then
            NOW_FLAG="--now"
            SERVICE="$3"
        else
            SERVICE="$2"
        fi
        if [[ ! "$ACTION" =~ ^(start|stop|restart|enable|disable|is-active|daemon-reload)$ ]]; then
            echo "Error: Invalid action '$ACTION'" >&2
            exit 1
        fi
        if [[ "$ACTION" == "daemon-reload" ]]; then
             exec /usr/bin/systemctl daemon-reload
        fi
        if [[ ! "$SERVICE" =~ ^(ethos-ticket-watcher\.service|rag_index_cron\.timer|fail2ban|ethos-power-.*\.service)$ ]]; then
            echo "Error: Invalid service '$SERVICE'" >&2
            exit 1
        fi
        exec /usr/bin/systemctl "$ACTION" ${NOW_FLAG:+"$NOW_FLAG"} "$SERVICE"
        ;;
    
    fail2ban-client)
        exec /usr/bin/fail2ban-client "$@"
        ;;
        
    copy-timer)
        SRC="$1"
        DEST="/etc/systemd/system/rag_index_cron.timer"
        if [[ ! -f "$SRC" ]]; then exit 1; fi
        cp "$SRC" "$DEST"
        ;;

    # User Management
    user-add)
        USERNAME="$1"
        SHELL="$2"
        HOME_DIR="$3"
        if [[ ! "$USERNAME" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        
        CMD=(/usr/sbin/useradd -m -s "$SHELL" "$USERNAME")
        if [[ -n "$HOME_DIR" ]]; then
            if [[ "$HOME_DIR" != /mnt/data/home/* && "$HOME_DIR" != /home/* ]]; then exit 1; fi
            mkdir -p "$(dirname "$HOME_DIR")"
            CMD=(/usr/sbin/useradd -m -d "$HOME_DIR" -s "$SHELL" "$USERNAME")
        fi
        exec "${CMD[@]}"
        ;;

    user-del)
        USERNAME="$1"
        if [[ ! "$USERNAME" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        if [[ "$USERNAME" == "root" || "$USERNAME" == "nasadmin" ]]; then exit 1; fi
        exec /usr/sbin/userdel -r "$USERNAME"
        ;;

    user-mod)
        USERNAME="$1"
        TYPE="$2"
        VALUE="$3"
        if [[ ! "$USERNAME" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        
        case "$TYPE" in
            shell) exec /usr/sbin/usermod -s "$VALUE" "$USERNAME" ;;
            group-append) exec /usr/sbin/usermod -aG "$VALUE" "$USERNAME" ;;
            groups-set) exec /usr/sbin/usermod -G "$VALUE" "$USERNAME" ;;
            *) exit 1 ;;
        esac
        ;;
    
    user-set-password)
        USERNAME="$1"
        if [[ ! "$USERNAME" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        exec /usr/sbin/chpasswd <<< "$USERNAME:$(cat)"
        ;;

    group-add)
        GROUP="$1"
        if [[ ! "$GROUP" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        exec /usr/sbin/groupadd "$GROUP"
        ;;

    group-del)
        GROUP="$1"
        if [[ ! "$GROUP" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        exec /usr/sbin/groupdel "$GROUP"
        ;;

    group-mod)
        GROUP="$1"
        USER="$2"
        ACTION="$3"
        if [[ ! "$GROUP" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        if [[ ! "$USER" =~ ^[a-z_][a-z0-9_-]*$ ]]; then exit 1; fi
        if [[ "$ACTION" == "remove" ]]; then
            exec /usr/bin/gpasswd -d "$USER" "$GROUP"
        else exit 1; fi
        ;;

    # Network
    ip-link)
        DEV="$1"
        STATE="$2"
        if [[ ! "$DEV" =~ ^[a-z0-9\.\-]+$ ]]; then exit 1; fi
        if [[ ! "$STATE" =~ ^(up|down)$ ]]; then exit 1; fi
        exec /usr/sbin/ip link set "$DEV" "$STATE"
        ;;
    
    nmcli)
        exec /usr/bin/nmcli "$@"
        ;;
        
    ap-control)
        ACTION="$1"
        if [[ ! "$ACTION" =~ ^(start|stop|status)$ ]]; then exit 1; fi
        AP_SCRIPT="/usr/local/bin/ethos-ap"
        if [[ -x "/opt/ethos/ethos-ap.sh" ]]; then AP_SCRIPT="/opt/ethos/ethos-ap.sh"; fi
        exec "$AP_SCRIPT" "$ACTION"
        ;;

    # Disk / Flasher
    lsblk)
        # Read-only, safe to expose
        exec lsblk "$@"
        ;;

    blockdev)
        # whitelist specific ops
        OP="$1"
        DEV="$2"
        validate_dev "$DEV"
        case "$OP" in
            --getsize64|--flushbufs|--rereadpt)
                exec /sbin/blockdev "$OP" "$DEV"
                ;;
            *)
                echo "Error: Invalid blockdev op" >&2
                exit 1
                ;;
        esac
        ;;

    smartctl)
        # Whitelist safe query ops
        # We need to parse args to ensure no destructive ops if possible, 
        # but smartctl is mostly query. -t is test (safeish).
        exec /usr/sbin/smartctl "$@"
        ;;

    umount)
        # Support optional -l (lazy) flag
        LAZY_FLAG=""
        DEV="$1"
        if [[ "$DEV" == "-l" ]]; then
            LAZY_FLAG="-l"
            DEV="$2"
        fi
        # Can be mountpoint or device
        if [[ "$DEV" == /dev/* ]]; then
            validate_dev "$DEV"
        fi
        exec /usr/bin/umount ${LAZY_FLAG:+"$LAZY_FLAG"} "$DEV"
        ;;
        
    fsck)
        DEV="$1"
        validate_dev "$DEV"
        exec /usr/sbin/fsck -y "$DEV"
        ;;
    
    fsck-check)
        DEV="$1"
        validate_dev "$DEV"
        exec /usr/sbin/fsck -n "$DEV"
        ;;

    badblocks)
        DEV="$1"
        validate_dev "$DEV"
        exec /usr/sbin/badblocks -wsv "$DEV"
        ;;

    write-image)
        # write-image <image_path> <device_path>
        IMAGE="$1"
        DEV="$2"
        validate_dev "$DEV"
        
        if [[ ! -f "$IMAGE" ]]; then
             echo "Error: Image file not found" >&2
             exit 1
        fi
        
        # Determine decompressor
        case "$IMAGE" in
            *.gz) DECOMPRESS="gunzip -c" ;;
            *.xz) DECOMPRESS="xz -dc" ;;
            *.zst|*.zstd) DECOMPRESS="zstd -dc" ;;
            *) DECOMPRESS="cat" ;;
        esac
        
        # Write
        $DECOMPRESS "$IMAGE" | dd of="$DEV" bs=4M oflag=direct conv=fsync status=progress
        ;;

    wipe-disk)
        # Zero out beginning and end of disk
        DEV="$1"
        validate_dev "$DEV"
        
        # 10MB at start
        dd if=/dev/zero of="$DEV" bs=1M count=10 oflag=direct,sync status=none
        
        # 10MB at end
        SZ=$(blockdev --getsize64 "$DEV")
        SEEK=$(( SZ / 1048576 - 10 ))
        if [[ $SEEK -gt 10 ]]; then
            dd if=/dev/zero of="$DEV" bs=1M seek=$SEEK count=10 oflag=direct,sync status=none
        fi
        
        sync
        blockdev --flushbufs "$DEV"
        blockdev --rereadpt "$DEV"
        ;;

    *)
        echo "Error: Unknown command '$COMMAND'" >&2
        exit 1
        ;;
esac
