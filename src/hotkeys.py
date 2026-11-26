from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from typing import Callable, Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_MAP: dict[str, int] = {
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    "SPACE": 0x20,
    "TAB": 0x09,
    "ESC": 0x1B,
    "PAUSE": 0x13,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PGUP": 0x21,
    "PGDN": 0x22,
    "NUM0": 0x60,
    "NUM1": 0x61,
    "NUM2": 0x62,
    "NUM3": 0x63,
    "NUM4": 0x64,
    "NUM5": 0x65,
    "NUM6": 0x66,
    "NUM7": 0x67,
    "NUM8": 0x68,
    "NUM9": 0x69,
}


def parse_hotkey_string(hotkey: str) -> Optional[tuple[int, int]]:
    """Преобразует строку вроде 'F8' или 'Ctrl+Shift+F9' в пару (модификаторы Win32, код клавиши VK)."""
    parts = [p.strip().upper() for p in hotkey.split("+") if p.strip()]
    if not parts:
        return None
    mods = MOD_NOREPEAT
    vk = None
    for part in parts:
        if part in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif part == "ALT":
            mods |= MOD_ALT
        elif part == "SHIFT":
            mods |= MOD_SHIFT
        elif part in ("WIN", "META"):
            mods |= MOD_WIN
        elif part in VK_MAP:
            vk = VK_MAP[part]
    if vk is None:
        return None
    return mods, vk


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class GlobalHotkeyManager(QObject):
    """Нативный менеджер глобальных горячих клавиш Windows без нагрузки на CPU (RegisterHotKey + nativeEventFilter Qt)."""

    hotkey_triggered = Signal(str)  # Имя действия: "toggle_pause" | "select_roi" | "interrupt"

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._id_to_action: dict[int, str] = {}
        self._next_id = 100
        self._filter = _WinHotkeyFilter(self._on_wm_hotkey)

    @property
    def native_filter(self) -> QAbstractNativeEventFilter:
        return self._filter

    def _on_wm_hotkey(self, hotkey_id: int) -> None:
        action = self._id_to_action.get(hotkey_id)
        if action:
            self.hotkey_triggered.emit(action)

    def register_all(self, mapping: dict[str, str]) -> dict[str, bool]:
        """Снимает прошлые горячие клавиши и регистрирует новую карту {действие: строка_клавиши}."""
        self.unregister_all()
        results: dict[str, bool] = {}
        for action, key_str in mapping.items():
            parsed = parse_hotkey_string(key_str)
            if not parsed:
                results[action] = False
                continue
            mods, vk = parsed
            hk_id = self._next_id
            self._next_id += 1
            ok = bool(user32.RegisterHotKey(None, hk_id, mods, vk))
            if ok:
                self._id_to_action[hk_id] = action
            results[action] = ok
        return results

    def unregister_all(self) -> None:
        for hk_id in list(self._id_to_action.keys()):
            try:
                user32.UnregisterHotKey(None, hk_id)
            except Exception:
                pass
        self._id_to_action.clear()


class _WinHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback: Callable[[int], None]) -> None:
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, event_type, message):
        try:
            if event_type == b"windows_generic_MSG" or event_type == "windows_generic_MSG":
                msg = _MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    self._callback(int(msg.wParam))
                    return True, 0
        except Exception:
            pass
        return False, 0
