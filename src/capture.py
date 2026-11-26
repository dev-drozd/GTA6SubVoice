from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import mss
import numpy as np

from src.config import RoiConfig

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    dwmapi = ctypes.windll.dwmapi
except Exception:
    dwmapi = None

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_MONITOR_CACHE_LOCK = threading.Lock()
_MONITOR_CACHE: tuple[float, list[dict]] = (0.0, [])
_PID_NAME_CACHE_LOCK = threading.Lock()
_PID_NAME_CACHE: dict[int, tuple[float, str]] = {}

_GW_HWNDPREV = 3
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PW_CLIENTONLY = 0x00000001
_PW_RENDERFULLCONTENT = 0x00000002
_BI_RGB = 0
_DIB_RGB_COLORS = 0
_SW_RESTORE = 9
_HWND_TOP = 0
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_SHOWWINDOW = 0x0040

_XBOX_PROCESS_FAMILY = frozenset({"xboxpcapp.exe", "xboxpcappft.exe"})


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# Явные 64-битные прототипы Win32 для безопасной передачи HWND/HDC без усечения на x64 Windows
if sys.platform == "win32":
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    ctypes.windll.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    ctypes.windll.kernel32.GetCurrentThreadId.argtypes = []

    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    if dwmapi is not None:
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]


def set_current_thread_below_normal() -> None:
    """Понижает приоритет текущего фонового потока (THREAD_PRIORITY_BELOW_NORMAL = -1),
    чтобы потоки OCR, TTS и захвата не конкурировали с игрой за процессорное время.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentThread()
        kernel32.SetThreadPriority(handle, -1)  # Пониженный приоритет THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        pass


# Включаем per-monitor DPI awareness, чтобы координаты GetWindowRect / mss / Qt-оверлея совпадали с физическими пикселями
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Режим PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


@dataclass
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_mss(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": max(1, self.width),
            "height": max(1, self.height),
        }

    def as_region_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def _get_pid_exe_name(pid: int) -> str:
    """Кэшированный поиск имени исполняемого файла по PID (TTL 2.0 с) без лишних накладных расходов."""
    if pid <= 0:
        return ""
    now = time.monotonic()
    with _PID_NAME_CACHE_LOCK:
        cached = _PID_NAME_CACHE.get(pid)
        if cached is not None and (now - cached[0]) < 2.0:
            return cached[1]
    name = ""
    try:
        import psutil

        name = (psutil.Process(pid).name() or "").lower()
    except Exception:
        name = ""
    with _PID_NAME_CACHE_LOCK:
        if len(_PID_NAME_CACHE) > 96:
            _PID_NAME_CACHE.clear()
        _PID_NAME_CACHE[pid] = (now, name)
    return name


def _get_window_title_lower(hwnd: int) -> str:
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip().lower()
    except Exception:
        return ""


def _is_window_cloaked(hwnd: int) -> bool:
    """Возвращает True, если DWM считает окно скрытым (UWP-рамка в фоне или другой виртуальный рабочий стол)."""
    if sys.platform != "win32" or dwmapi is None or not hwnd:
        return False
    try:
        cloaked = wintypes.DWORD(0)
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd,
            _DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and bool(cloaked.value)
    except Exception:
        return False


def _get_window_client_rect(hwnd: int) -> Optional[Rect]:
    crect = _RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(crect)):
        return None
    w = int(crect.right - crect.left)
    h = int(crect.bottom - crect.top)
    if w < 200 or h < 120:
        return None
    pt = _POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return Rect(int(pt.x), int(pt.y), w, h)


def _hwnd_matches_process(hwnd: int, target_lower: str) -> bool:
    """Проверяет, принадлежит ли hwnd процессу target_lower (включая дочернее CoreWindow внутри UWP ApplicationFrameHost)."""
    if not hwnd or not target_lower:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = _get_pid_exe_name(int(pid.value))
    if exe == target_lower:
        return True

    # Совместимость с Xbox App (XboxPcApp.exe, XboxPcAppFT.exe и UWP FrameHost)
    is_xbox_target = target_lower in _XBOX_PROCESS_FAMILY
    if is_xbox_target and exe in _XBOX_PROCESS_FAMILY:
        return True

    if exe == "applicationframehost.exe":
        if is_xbox_target:
            title = _get_window_title_lower(hwnd)
            if title == "xbox" or title.startswith("xbox ") or "xbox remote play" in title:
                return True
        found_child = [False]

        def _child_cb(child_hwnd: int, _lp: int) -> bool:
            cpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(child_hwnd, ctypes.byref(cpid))
            cexe = _get_pid_exe_name(int(cpid.value))
            if cexe == target_lower or (is_xbox_target and cexe in _XBOX_PROCESS_FAMILY):
                found_child[0] = True
                return False
            return True

        try:
            user32.EnumChildWindows(hwnd, _WNDENUMPROC(_child_cb), 0)
        except Exception:
            pass
        if found_child[0]:
            return True

    return False


def find_target_window(target_process: str = "XboxPcApp.exe") -> Optional[tuple[int, Rect]]:
    """Находит активное или верхнее видимое, не свёрнутое и не скрытое окно процесса target_process.
    Возвращает (hwnd, клиентский_rect_в_координатах_экрана) или None, если окно не найдено.
    """
    if sys.platform != "win32" or not target_process or not target_process.strip():
        return None
    target_lower = target_process.strip().lower()

    # 1. Предпочитаем текущее активное окно, если оно принадлежит target_process
    try:
        fg = int(user32.GetForegroundWindow() or 0)
        if (
            fg
            and user32.IsWindowVisible(fg)
            and not user32.IsIconic(fg)
            and not _is_window_cloaked(fg)
            and _hwnd_matches_process(fg, target_lower)
        ):
            rect = _get_window_client_rect(fg)
            if rect is not None:
                return (fg, rect)
    except Exception:
        pass

    # 2. Иначе выбираем верхнее видимое не скрытое окно процесса target_process
    matched: list[tuple[int, Rect]] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or _is_window_cloaked(hwnd):
            return True
        if _hwnd_matches_process(hwnd, target_lower):
            rect = _get_window_client_rect(hwnd)
            if rect is not None:
                matched.append((int(hwnd), rect))
                return False
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    except Exception:
        return None

    return matched[0] if matched else None


def find_target_window_rect(target_process: str = "XboxPcApp.exe") -> Optional[Rect]:
    """Возвращает экранные координаты клиентской области видимого не свёрнутого окна target_process."""
    found = find_target_window(target_process)
    return found[1] if found is not None else None


def find_target_window_hwnd(
    target_process: str = "XboxPcApp.exe",
    include_minimized: bool = True,
) -> Optional[tuple[int, bool]]:
    """Находит главный верхнеуровневый HWND окна target_process (даже если оно свёрнуто).
    Возвращает (hwnd, свёрнуто_ли) или None.
    """
    if sys.platform != "win32" or not target_process or not target_process.strip():
        return None
    target_lower = target_process.strip().lower()

    # 1. Сначала ищем активное не свёрнутое окно
    found_vis = find_target_window(target_process)
    if found_vis is not None:
        return (found_vis[0], False)

    if not include_minimized:
        return None

    # 2. Ищем видимые (включая свёрнутые) верхнеуровневые окна процесса target_process
    matched: list[tuple[int, bool]] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or _is_window_cloaked(hwnd):
            return True
        if _hwnd_matches_process(hwnd, target_lower):
            is_min = bool(user32.IsIconic(hwnd))
            if is_min:
                matched.append((int(hwnd), True))
                return False
            if _get_window_client_rect(hwnd) is not None:
                matched.append((int(hwnd), False))
                return False
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    except Exception:
        return None

    return matched[0] if matched else None


def bring_target_window_to_front(target_process: str = "XboxPcApp.exe") -> tuple[bool, bool, bool]:
    """Разворачивает (если свёрнуто) и выводит выбранное окно процесса на передний план.
    Возвращает (найдено, было_свёрнуто, уже_на_переднем_плане).
    """
    if sys.platform != "win32" or not target_process or not target_process.strip():
        return (False, False, False)

    found = find_target_window_hwnd(target_process, include_minimized=True)
    if found is None:
        return (False, False, False)

    hwnd, is_min = found
    try:
        fg = int(user32.GetForegroundWindow() or 0)
        if not is_min and fg == hwnd:
            return (True, False, True)

        if is_min:
            user32.ShowWindowAsync(hwnd, _SW_RESTORE)

        cur_tid = int(ctypes.windll.kernel32.GetCurrentThreadId())
        fg_tid = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
        tgt_tid = int(user32.GetWindowThreadProcessId(hwnd, None))

        attached_fg = False
        attached_tgt = False
        try:
            if fg_tid and fg_tid != cur_tid:
                attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
            if tgt_tid and tgt_tid != cur_tid and tgt_tid != fg_tid:
                attached_tgt = bool(user32.AttachThreadInput(cur_tid, tgt_tid, True))

            user32.SetWindowPos(
                hwnd,
                _HWND_TOP,
                0,
                0,
                0,
                0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW,
            )
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached_tgt:
                user32.AttachThreadInput(cur_tid, tgt_tid, False)
            if attached_fg:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        return (True, is_min, False)
    except Exception:
        return (True, is_min, False)


def find_xbox_window_rect() -> Optional[Rect]:
    """Обратно-совместимая функция поиска окна приложения Microsoft Xbox."""
    return find_target_window_rect("XboxPcApp.exe")


def is_target_window_active(target_process: str) -> bool:
    """Возвращает True, если целевое окно на переднем плане ИЛИ активно окно самого GTA6SubVoice,
    а целевое окно при этом видимо (не свёрнуто и не скрыто) на экране.
    """
    if sys.platform != "win32" or not target_process or not target_process.strip():
        return True
    try:
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd or user32.IsIconic(hwnd):
            return False
        target_lower = target_process.strip().lower()
        if _hwnd_matches_process(hwnd, target_lower):
            return True

        # Продолжаем чтение, если пользователь нажал внутри GTA6SubVoice (Старт, слайдер), пока окно игры видимо
        fg_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(fg_pid))
        if int(fg_pid.value) == os.getpid():
            return find_target_window(target_process) is not None
        fg_exe = _get_pid_exe_name(int(fg_pid.value))
        if fg_exe in ("gta6subvoice.exe", "xboxgamebarwidgets.exe", "gamebar.exe"):
            return find_target_window(target_process) is not None
        return False
    except Exception:
        return True


def _intersect_hwnd_with_roi(hwnd: int, roi_rect: Rect, min_area: int = 100) -> Optional[Rect]:
    wr = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
        return None
    inter_l = max(roi_rect.left, int(wr.left))
    inter_t = max(roi_rect.top, int(wr.top))
    inter_r = min(roi_rect.right, int(wr.right))
    inter_b = min(roi_rect.bottom, int(wr.bottom))
    if inter_r > inter_l and inter_b > inter_t:
        inter_w = inter_r - inter_l
        inter_h = inter_b - inter_t
        if (inter_w * inter_h) >= min_area:
            return Rect(inter_l, inter_t, inter_w, inter_h)
    return None


def _is_window_occluder_candidate(hwnd: int) -> tuple[bool, int]:
    """Возвращает (является_ли_видимым_непрозрачным_окном, ex_style) для проверки перекрытия ROI."""
    if not hwnd:
        return False, 0
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or _is_window_cloaked(hwnd):
        return False, 0
    ex_style = int(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE))
    # Прозрачные оверлеи с пропуском кликов не перекрывают окно игры
    if (ex_style & _WS_EX_LAYERED) and (ex_style & _WS_EX_TRANSPARENT):
        return False, ex_style
    # Игнорируем невидимые служебные tool-окна нулевого или крошечного размера
    wr = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
        return False, ex_style
    if (int(wr.right) - int(wr.left)) < 8 or (int(wr.bottom) - int(wr.top)) < 8:
        return False, ex_style
    return True, ex_style


def get_roi_occlusion_rects(
    target_hwnd: int,
    roi_rect: Rect,
    target_process: str = "",
    own_hwnds: tuple[int, ...] | list[int] = (),
) -> tuple[list[Rect], bool]:
    """Возвращает (список_областей_перекрытия, перекрыто_ли_собственным_окном) в экранных координатах
    для всех видимых непрозрачных окон выше target_hwnd по Z-порядку (плюс видимые/topmost окна GTA6SubVoice),
    пересекающихся с roi_rect.
    """
    if sys.platform != "win32" or not target_hwnd:
        return [], False
    target_lower = (target_process or "").strip().lower()
    t_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(target_hwnd, ctypes.byref(t_pid))
    target_pid = int(t_pid.value)
    own_pid = os.getpid()
    fg_hwnd = int(user32.GetForegroundWindow() or 0)

    occluders: list[Rect] = []
    seen_hwnds: set[int] = set()
    own_occluded = False

    def _add_rect(r: Rect, is_own: bool) -> None:
        nonlocal own_occluded
        occluders.append(r)
        if is_own:
            own_occluded = True

    # 1. Прямая проверка явных HWND окон GTA6SubVoice (не зависит от особенностей EnumWindows / Z-order)
    for oh in own_hwnds or ():
        ih = int(oh or 0)
        if not ih or ih == int(target_hwnd):
            continue
        try:
            ok, ex_style = _is_window_occluder_candidate(ih)
            if not ok:
                continue
            # Собственное окно может перекрывать цель при TOPMOST, в фокусе или когда target_hwnd не в фокусе
            if (ex_style & _WS_EX_TOPMOST) or ih == fg_hwnd or fg_hwnd != int(target_hwnd):
                inter = _intersect_hwnd_with_roi(ih, roi_rect)
                if inter is not None:
                    seen_hwnds.add(ih)
                    _add_rect(inter, is_own=True)
        except Exception:
            continue

    # 2. Прямой обход по Z-порядку строго ВЫШЕ target_hwnd через GW_HWNDPREV (быстро отлавливает любые всплывающие окна)
    above_hwnds: list[int] = []
    try:
        cur = user32.GetWindow(target_hwnd, _GW_HWNDPREV)
        guard = 0
        while cur and guard < 256:
            guard += 1
            ic = int(cur)
            above_hwnds.append(ic)
            if ic not in seen_hwnds:
                try:
                    ok, _ex = _is_window_occluder_candidate(ic)
                    if ok:
                        w_pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(ic, ctypes.byref(w_pid))
                        cur_pid = int(w_pid.value)
                        is_own = cur_pid == own_pid
                        same_proc = (not is_own) and (
                            cur_pid == target_pid or (bool(target_lower) and _hwnd_matches_process(ic, target_lower))
                        )
                        if not same_proc:
                            inter = _intersect_hwnd_with_roi(ic, roi_rect)
                            if inter is not None:
                                seen_hwnds.add(ic)
                                _add_rect(inter, is_own=is_own)
                except Exception:
                    pass
            cur = user32.GetWindow(cur, _GW_HWNDPREV)
    except Exception:
        pass

    # 3. Запасной проход EnumWindows с изоляцией исключений на каждом окне, чтобы редкое системное окно не сорвало проверку
    seen_target = [False]

    def _cb(cur: int, _lparam: int) -> bool:
        try:
            ic = int(cur or 0)
            if not ic:
                return True
            if ic == int(target_hwnd):
                seen_target[0] = True
                return True
            if ic in seen_hwnds:
                return True
            ok, ex_style = _is_window_occluder_candidate(ic)
            if not ok:
                return True
            w_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(ic, ctypes.byref(w_pid))
            cur_pid = int(w_pid.value)
            is_own = cur_pid == own_pid
            if seen_target[0]:
                if not (is_own and ((ex_style & _WS_EX_TOPMOST) or ic == fg_hwnd or fg_hwnd != int(target_hwnd))):
                    return True
            same_proc = (not is_own) and (
                cur_pid == target_pid or (bool(target_lower) and _hwnd_matches_process(ic, target_lower))
            )
            if same_proc:
                return True
            inter = _intersect_hwnd_with_roi(ic, roi_rect)
            if inter is not None:
                seen_hwnds.add(ic)
                _add_rect(inter, is_own=is_own)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return occluders, own_occluded


def is_roi_occluded_by_other_window(
    target_hwnd: int,
    roi_rect: Rect,
    target_process: str = "",
    own_hwnds: tuple[int, ...] | list[int] = (),
) -> bool:
    """Возвращает True, если ХОТЯ БЫ одно видимое непрозрачное окно выше target_hwnd по Z-порядку перекрывает roi_rect."""
    rects, _own = get_roi_occlusion_rects(target_hwnd, roi_rect, target_process=target_process, own_hwnds=own_hwnds)
    return bool(rects)


def capture_window_roi(hwnd: int, win_client_rect: Rect, roi_abs_rect: Rect) -> Optional[np.ndarray]:
    """Захватывает ROI напрямую из конкретного целевого окна через PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT).
    Никогда не захватывает чужие окна поверх hwnd. Возвращает None, если PrintWindow не поддерживается или даёт чёрный кадр.
    """
    if sys.platform != "win32" or not hwnd:
        return None
    w = int(win_client_rect.width)
    h = int(win_client_rect.height)
    if w < 32 or h < 16 or w > 8192 or h > 4320:
        return None

    hwnd_dc = None
    mem_dc = None
    bmp = None
    old_bmp = None
    try:
        hwnd_dc = user32.GetDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        if not mem_dc:
            return None
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        if not bmp:
            return None
        old_bmp = gdi32.SelectObject(mem_dc, bmp)

        ok = user32.PrintWindow(hwnd, mem_dc, _PW_CLIENTONLY | _PW_RENDERFULLCONTENT)
        if not ok:
            ok = user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT)
        if not ok:
            return None

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # Верхнесторонний DIB (top-down)
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = _BI_RGB

        buf = np.empty((h, w, 4), dtype=np.uint8)
        lines = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf.ctypes.data, ctypes.byref(bmi), _DIB_RGB_COLORS)
        if lines != h:
            return None

        rx0 = max(0, min(w - 1, roi_abs_rect.left - win_client_rect.left))
        ry0 = max(0, min(h - 1, roi_abs_rect.top - win_client_rect.top))
        rx1 = max(rx0 + 1, min(w, rx0 + roi_abs_rect.width))
        ry1 = max(ry0 + 1, min(h, ry0 + roi_abs_rect.height))
        crop = np.ascontiguousarray(buf[ry0:ry1, rx0:rx1, :3])
        # Отбрасываем полностью чёрные кадры (бывает при аппаратно защищённой MPO-поверхности)
        if crop.size == 0 or int(np.max(crop)) == 0:
            return None
        return crop
    except Exception:
        return None
    finally:
        try:
            if mem_dc and old_bmp:
                gdi32.SelectObject(mem_dc, old_bmp)
        except Exception:
            pass
        try:
            if bmp:
                gdi32.DeleteObject(bmp)
        except Exception:
            pass
        try:
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
        except Exception:
            pass
        try:
            if hwnd_dc:
                user32.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def get_monitor_rect(monitor_index: int = 1) -> Rect:
    """Возвращает геометрию монитора по индексу (1-based, 0 = весь виртуальный экран) с кэшем на 5 с против нагрузки на GDI."""
    global _MONITOR_CACHE
    now = time.monotonic()
    with _MONITOR_CACHE_LOCK:
        ts, mons = _MONITOR_CACHE
        if not mons or (now - ts) >= 5.0:
            with mss.MSS() as sct:
                mons = [dict(m) for m in sct.monitors]
            _MONITOR_CACHE = (now, mons)

    idx = monitor_index if 0 <= monitor_index < len(mons) else 1
    if idx >= len(mons):
        idx = 0
    m = mons[idx]
    return Rect(int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"]))


def get_reference_rect(roi: RoiConfig, target_process: str = "XboxPcApp.exe") -> tuple[Rect, str]:
    """Определяет опорную область (окно целевой игры если видимо, иначе экран для предпросмотра UI)."""
    if target_process:
        win_rect = find_target_window_rect(target_process)
        if win_rect is not None:
            return win_rect, f"window:{target_process}"
    return get_monitor_rect(roi.monitor_index), f"monitor_{roi.monitor_index}"


def _clamp_roi_inside_ref(roi: RoiConfig, ref: Rect) -> Rect:
    x = ref.left + int(round(roi.x_pct * ref.width))
    y = ref.top + int(round(roi.y_pct * ref.height))
    w = max(32, int(round(roi.w_pct * ref.width)))
    h = max(16, int(round(roi.h_pct * ref.height)))
    x = max(ref.left, min(ref.right - 32, x))
    y = max(ref.top, min(ref.bottom - 16, y))
    w = min(w, ref.right - x)
    h = min(h, ref.bottom - y)
    return Rect(x, y, max(32, w), max(16, h))


def resolve_roi_rect(roi: RoiConfig, target_process: str = "XboxPcApp.exe") -> tuple[Rect, str]:
    """Преобразует относительные проценты ROI в ограниченный экранный Rect (с откатом на монитор для UI)."""
    ref, source = get_reference_rect(roi, target_process=target_process)
    return _clamp_roi_inside_ref(roi, ref), source


def resolve_target_roi(roi: RoiConfig, target_process: str) -> Optional[tuple[int, Rect, Rect]]:
    """Строго вычисляет ROI ТОЛЬКО внутри окна выбранного процесса.
    Возвращает (hwnd, roi_abs_rect, window_client_rect) или None, если окно целевого процесса НЕ видимо.
    Никогда не откатывается к захвату всего рабочего стола!
    """
    if not target_process or not target_process.strip():
        return None
    found = find_target_window(target_process)
    if found is None:
        return None
    hwnd, win_rect = found
    roi_rect = _clamp_roi_inside_ref(roi, win_rect)
    return (hwnd, roi_rect, win_rect)


def rect_to_roi_percentages(
    abs_rect: Rect,
    roi_cfg: RoiConfig,
    target_process: str = "XboxPcApp.exe",
) -> RoiConfig:
    """Преобразует абсолютный экранный Rect обратно в относительные проценты относительно окна процесса или монитора."""
    ref, _ = get_reference_rect(roi_cfg, target_process=target_process)
    x_pct = max(0.0, min(0.98, (abs_rect.left - ref.left) / max(1, ref.width)))
    y_pct = max(0.0, min(0.98, (abs_rect.top - ref.top) / max(1, ref.height)))
    w_pct = max(0.02, min(1.0 - x_pct, abs_rect.width / max(1, ref.width)))
    h_pct = max(0.02, min(1.0 - y_pct, abs_rect.height / max(1, ref.height)))
    return RoiConfig(
        x_pct=round(x_pct, 5),
        y_pct=round(y_pct, 5),
        w_pct=round(w_pct, 5),
        h_pct=round(h_pct, 5),
        monitor_index=roi_cfg.monitor_index,
        bind_to_xbox=True,
    )


class ChangeDetector:
    """Быстрый универсальный Pre-OCR фильтр по морфологическому контрасту штрихов top-hat + absdiff (< 0.3 мс):
    - Работает одинаково для субтитров как с тёмной подложкой, так и без неё, на любом мониторе / HDR / SDR.
    - Игнорирует плавные движущиеся фоны геймплея (небо, море, дорогу), где нет тонких контрастных штрихов.
    """

    def __init__(
        self,
        bright_threshold: int = 44,
        diff_threshold: float = 0.0010,
        min_text_coverage: float = 0.0002,
    ) -> None:
        self.bright_threshold = bright_threshold
        self.diff_threshold = diff_threshold
        self.min_text_coverage = min_text_coverage
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self._prev_mask: Optional[np.ndarray] = None

    def reset(self) -> None:
        self._prev_mask = None

    def extract_text_mask(self, bgr: np.ndarray) -> np.ndarray:
        """Выделяет чёткие штрихи 2D-HUD текста через локальный top-hat контраст без требования тёмной подложки.
        Использует 2x2 max-pooling вместо грубого прореживания [::2, ::2], чтобы тонкие 1-px штрихи не терялись.
        """
        h2 = (bgr.shape[0] // 2) * 2
        w2 = (bgr.shape[1] // 2) * 2
        if h2 < 2 or w2 < 2:
            return np.zeros((1, 1), dtype=np.uint8)
        peak_full = np.max(bgr[:h2, :w2], axis=2)
        peak = peak_full.reshape(h2 // 2, 2, w2 // 2, 2).max(axis=(1, 3))
        tophat = cv2.morphologyEx(peak, cv2.MORPH_TOPHAT, self._kernel)
        eff_thresh = min(int(self.bright_threshold), 60)
        return ((peak >= eff_thresh) & (tophat >= 26)).astype(np.uint8)

    def has_changed(self, bgr: np.ndarray) -> tuple[bool, float, float]:
        """Возвращает (изменился_ли_кадр, доля_различий, покрытие_текстом)."""
        mask = self.extract_text_mask(bgr)
        size = max(1, mask.size)
        coverage = float(np.count_nonzero(mask)) / float(size)

        if self._prev_mask is None or self._prev_mask.shape != mask.shape:
            self._prev_mask = mask
            return coverage >= self.min_text_coverage, coverage, coverage

        diff_score = float(np.count_nonzero(mask != self._prev_mask)) / float(size)
        self._prev_mask = mask

        changed = (diff_score >= self.diff_threshold) and (coverage >= self.min_text_coverage)
        return changed, diff_score, coverage


class CachedRoiResolver:
    """Кэширует геометрию целевого окна и ROI на ~0.5 с, чтобы не вызывать EnumWindows() 8–10 раз в секунду."""

    def __init__(self, ttl_sec: float = 0.5) -> None:
        self.ttl_sec = ttl_sec
        self._last_ts: float = 0.0
        self._last_key: Optional[tuple] = None
        self._cached: Optional[tuple[Rect, str]] = None
        self._last_win_ts: float = 0.0
        self._last_win_key: Optional[tuple] = None
        self._cached_win: Optional[tuple[int, Rect, Rect]] = None

    def invalidate(self) -> None:
        self._cached = None
        self._last_ts = 0.0
        self._cached_win = None
        self._last_win_ts = 0.0

    def resolve(self, roi: RoiConfig, target_process: str = "XboxPcApp.exe") -> tuple[Rect, str]:
        now = time.monotonic()
        key = (roi.x_pct, roi.y_pct, roi.w_pct, roi.h_pct, roi.monitor_index, roi.bind_to_xbox, target_process)
        if self._cached is not None and self._last_key == key and (now - self._last_ts) < self.ttl_sec:
            return self._cached
        res = resolve_roi_rect(roi, target_process=target_process)
        self._cached = res
        self._last_key = key
        self._last_ts = now
        return res

    def resolve_for_capture(
        self,
        roi: RoiConfig,
        target_process: str,
    ) -> Optional[tuple[int, Rect, Rect]]:
        """Определяет (hwnd, roi_abs_rect, window_client_rect) строго для target_process (кэш ~0.5 с).
        Возвращает None, если окно выбранного процесса отсутствует или свёрнуто (не читает рабочий стол).
        """
        now = time.monotonic()
        key = (roi.x_pct, roi.y_pct, roi.w_pct, roi.h_pct, target_process)
        if self._last_win_key == key and (now - self._last_win_ts) < self.ttl_sec:
            if self._cached_win is not None:
                hwnd = self._cached_win[0]
                if sys.platform != "win32" or (
                    user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not _is_window_cloaked(hwnd)
                ):
                    return self._cached_win
            else:
                return None

        res = resolve_target_roi(roi, target_process=target_process)
        self._cached_win = res
        self._last_win_key = key
        self._last_win_ts = now
        return res


class ScreenCapturer:
    """Быстрый захват DXGI Desktop Duplication (bettercam) без просадки FPS и с безопасным откатом на mss.
    ВАЖНО для FPS игры: когда DXGI активен и grab() возвращает None (кадр не менялся),
    НЕ откатываемся в GDI BitBlt (mss) на каждом тике, чтобы не тормозить конвейер GPU.
    """

    def __init__(self) -> None:
        self._camera = None
        self._backend: str = "mss"
        self._mss = None
        self._primary_rect: Optional[Rect] = None
        self._dxgi_errors: int = 0
        self._dxgi_empty_streak: int = 0
        self._init_dxgi()

    @property
    def backend_name(self) -> str:
        return self._backend

    def _init_dxgi(self) -> None:
        try:
            self._primary_rect = get_monitor_rect(1)
            import bettercam

            cam = bettercam.create(output_idx=0, output_color="BGR")
            if cam is not None:
                if not hasattr(cam, "is_capturing"):
                    cam.is_capturing = False
                self._camera = cam
                self._backend = "dxgi"
        except Exception:
            self._camera = None
            self._backend = "mss"

    def _get_mss(self):
        if self._mss is None:
            self._mss = mss.MSS()
        return self._mss

    def grab(self, rect: Rect, force_initial: bool = False) -> Optional[np.ndarray]:
        """Захватывает ROI в виде BGR uint8 NumPy-массива (< 1 мс через DXGI, с откатом на mss для MPO/оверлеев)."""
        if self._camera is not None and self._primary_rect is not None:
            pr = self._primary_rect
            if (
                rect.left >= pr.left
                and rect.top >= pr.top
                and rect.right <= pr.right
                and rect.bottom <= pr.bottom
            ):
                region = (
                    rect.left - pr.left,
                    rect.top - pr.top,
                    rect.right - pr.left,
                    rect.bottom - pr.top,
                )
                try:
                    frame = self._camera.grab(region=region)
                    self._dxgi_errors = 0
                    if frame is not None and frame.size > 0:
                        self._dxgi_empty_streak = 0
                        return frame
                    self._dxgi_empty_streak += 1
                    if not force_initial and (self._dxgi_empty_streak % 2 != 0):
                        return None
                except Exception:
                    self._dxgi_errors += 1
                    if self._dxgi_errors >= 5:
                        try:
                            self._camera.release()
                        except Exception:
                            pass
                        self._camera = None
                        self._backend = "mss"

        # Запасной захват mss (для второго монитора, первого кадра, MPO-видеопотока или при отсутствии DXGI)
        try:
            sct = self._get_mss()
            raw = sct.grab(rect.as_mss())
            bgra = np.asarray(raw, dtype=np.uint8)
            return np.ascontiguousarray(bgra[:, :, :3])
        except Exception:
            return None

    def grab_target_window_roi(
        self,
        hwnd: int,
        win_client_rect: Rect,
        roi_abs_rect: Rect,
        target_process: str = "",
        force_initial: bool = False,
        own_hwnds: tuple[int, ...] | list[int] = (),
    ) -> Optional[np.ndarray]:
        """Захватывает ROI строго из окна выбранного процесса:
        - Если посторонние окна не перекрывают roi_abs_rect поверх hwnd -> быстрый DXGI/mss захват.
        - Если постороннее окно (или сам GTA6SubVoice) перекрывает roi_abs_rect:
            1) Сначала пробуем PrintWindow напрямую из hwnd, минуя чужие окна.
            2) Если PrintWindow не сработал (например аппаратная MPO-поверхность):
               - Если сам GTA6SubVoice перекрывает >= 18% ROI ИЛИ чужие окна >= 30% -> пропускаем кадр.
               - Если перекрыт лишь небольшой край -> захватываем DXGI/mss и закрашиваем зону перекрытия (+12 px),
                 чтобы посторонний UI-текст полностью стирался, а видимые субтитры продолжали читаться.
        """
        occ_rects, own_occ = get_roi_occlusion_rects(
            hwnd,
            roi_abs_rect,
            target_process=target_process,
            own_hwnds=own_hwnds,
        )
        if not occ_rects:
            return self.grab(roi_abs_rect, force_initial=force_initial)

        direct = capture_window_roi(hwnd, win_client_rect, roi_abs_rect)
        if direct is not None:
            return direct

        roi_area = max(1, roi_abs_rect.width * roi_abs_rect.height)
        occ_mask = np.zeros((roi_abs_rect.height, roi_abs_rect.width), dtype=bool)
        for oc in occ_rects:
            x0 = max(0, oc.left - roi_abs_rect.left - 12)
            y0 = max(0, oc.top - roi_abs_rect.top - 12)
            x1 = min(roi_abs_rect.width, oc.right - roi_abs_rect.left + 12)
            y1 = min(roi_abs_rect.height, oc.bottom - roi_abs_rect.top + 12)
            if x1 > x0 and y1 > y0:
                occ_mask[y0:y1, x0:x1] = True

        occ_ratio = float(np.count_nonzero(occ_mask)) / float(roi_area)
        max_allowed = 0.18 if own_occ else 0.30
        if occ_ratio >= max_allowed:
            return None

        frame = self.grab(roi_abs_rect, force_initial=force_initial)
        if frame is None or frame.size == 0:
            return None
        fh, fw = frame.shape[:2]
        if fh != roi_abs_rect.height or fw != roi_abs_rect.width:
            return None
        masked = frame.copy()
        masked[occ_mask] = 0
        return masked

    def close(self) -> None:
        if self._camera is not None:
            try:
                self._camera.release()
            except Exception:
                pass
            self._camera = None
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None
