"""Windows-specific integration — auto-start, notifications, shell extensions."""

import logging
import os
import sys

log = logging.getLogger(__name__)


def set_auto_start(enabled: bool):
    """Enable or disable auto-start with Windows login via registry."""
    if os.name != "nt":
        return

    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)

        if enabled:
            if getattr(sys, "frozen", False):
                exe_path = sys.executable
            else:
                exe_path = f'"{sys.executable}" -m ethos_drive'
            winreg.SetValueEx(key, "EthOS Drive", 0, winreg.REG_SZ, exe_path)
            log.info("Auto-start enabled")
        else:
            try:
                winreg.DeleteValue(key, "EthOS Drive")
                log.info("Auto-start disabled")
            except FileNotFoundError:
                pass

        winreg.CloseKey(key)
    except Exception as e:
        log.error("Failed to set auto-start: %s", e)


def is_auto_start_enabled() -> bool:
    """Check if auto-start is enabled."""
    if os.name != "nt":
        return False

    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "EthOS Drive")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def get_default_sync_folder() -> str:
    """Get the default sync folder path (e.g., C:\\Users\\<user>\\EthOS Drive)."""
    home = os.path.expanduser("~")
    return os.path.join(home, "EthOS Drive")


def show_windows_notification(title: str, message: str, icon_path: str = None):
    """Show a Windows 10/11 toast notification."""
    if os.name != "nt":
        return

    try:
        from PySide6.QtWidgets import QSystemTrayIcon
        # Use Qt's tray notification (cross-platform)
        # This is handled by the SystemTray class
        pass
    except Exception as e:
        log.debug("Notification failed: %s", e)


def add_to_explorer_context_menu():
    """Add 'Sync with EthOS' to Windows Explorer right-click context menu."""
    if os.name != "nt":
        return

    try:
        import winreg

        exe_path = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" -m ethos_drive'

        # Folder context menu
        key_path = r"Software\Classes\Directory\shell\EthOSDrive"
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Sync with EthOS Drive")
        winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)

        cmd_key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\command")
        winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, f'{exe_path} --sync-folder "%1"')
        winreg.CloseKey(cmd_key)

        log.info("Explorer context menu added")
    except Exception as e:
        log.error("Failed to add context menu: %s", e)


def remove_explorer_context_menu():
    """Remove EthOS Drive from Explorer context menu."""
    if os.name != "nt":
        return

    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Classes\Directory\shell\EthOSDrive\command")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Classes\Directory\shell\EthOSDrive")
        log.info("Explorer context menu removed")
    except Exception as e:
        log.debug("Context menu removal: %s", e)
