from __future__ import annotations

import atexit
import math
import os
import re
import sys
import threading
import time
import weakref
from typing import Callable, Optional

DEFAULT_TARGET_PROCESS: str = "XboxPcApp.exe"
DEFAULT_DUCKING_PROCESSES: list[str] = [DEFAULT_TARGET_PROCESS]

PRESET_GAME_PROCESSES: list[tuple[str, str]] = [
    ("XboxPcApp.exe", "XboxPcApp.exe — Xbox (Remote Play / Xbox App)"),
    ("RemotePlay.exe", "RemotePlay.exe — PlayStation (PS Remote Play)"),
    ("GTA6.exe", "GTA6.exe — GTA VI (ПК-версия)"),
]

_XBOX_FAMILY_LOWER = frozenset(
    {
        "xboxpcapp.exe",
        "xboxpcappft.exe",
        "gamingservices.exe",
        "gamingservicesnet.exe",
    }
)
_WEBVIEW_HELPER_NAMES = frozenset({"msedgewebview2.exe"})
_SYSTEM_DIRS_LOWER = ("\\windows\\system32", "\\windows\\syswow64", "\\windows")
_SID_EXE_RE = re.compile(r"\|([^|%]+?\.exe)(?:%b|\||$)", re.IGNORECASE)


def _set_thread_below_normal() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -1)
    except Exception:
        pass


def _expand_target_names(targets: set[str]) -> set[str]:
    """Расширяет набор целевых процессов сопутствующими процессами семейства (XboxPcApp.exe <-> XboxPcAppFT.exe и т.д.)."""
    expanded = {t.strip().lower() for t in targets if t and t.strip()}
    if expanded & {"xboxpcapp.exe", "xboxpcappft.exe"}:
        expanded |= _XBOX_FAMILY_LOWER
    return expanded


def _parse_exe_from_session_id(session_id: str) -> tuple[str, str]:
    """Извлекает (имя_exe, каталог_в_нижнем_регистре) из идентификатора WASAPI-сессии.
    Работает даже тогда, когда psutil.Process(pid) бросает AccessDenied на защищённых / UWP / античит-играх.
    """
    if not session_id:
        return "", ""
    m = _SID_EXE_RE.search(session_id)
    if not m:
        return "", ""
    raw_path = m.group(1).strip()
    if not raw_path:
        return "", ""
    norm = raw_path.replace("/", "\\")
    basename = norm.rsplit("\\", 1)[-1]
    dir_lower = norm.rsplit("\\", 1)[0].lower() if "\\" in norm else ""
    return basename, dir_lower


def _is_non_system_app_dir(dir_lower: str) -> bool:
    if not dir_lower:
        return False
    return not any(dir_lower.endswith(s) for s in _SYSTEM_DIRS_LOWER)


def _collect_target_app_dirs(expanded_targets: set[str]) -> set[str]:
    """Определяет каталоги установки запущенных процессов из expanded_targets, чтобы надёжно глушить
    вспомогательные аудиопроцессы из той же папки (например UWP WindowsApps или папку игры).
    """
    dirs: set[str] = set()
    try:
        import psutil

        for proc in psutil.process_iter(["name", "exe"]):
            try:
                nm = (proc.info.get("name") or "").lower()
                if nm in expanded_targets:
                    exe_p = proc.info.get("exe") or ""
                    if exe_p:
                        d = os.path.dirname(exe_p).lower()
                        if _is_non_system_app_dir(d):
                            dirs.add(d)
            except Exception:
                continue
    except Exception:
        pass
    return dirs


def _match_dir_suffix(session_dir_lower: str, target_dirs: set[str]) -> bool:
    """Сравнивает NT-путь устройства ('\\device\\harddiskvolume3\\program files\\...') с DOS-путём ('c:\\program files\\...')."""
    if not session_dir_lower or not target_dirs or not _is_non_system_app_dir(session_dir_lower):
        return False
    for td in target_dirs:
        # Отрезаем букву диска 'c:' -> '\\program files\\...'
        tail = td[2:] if len(td) >= 2 and td[1] == ":" else td
        if tail and (session_dir_lower == td or session_dir_lower.endswith(tail)):
            return True
    return False


def _process_matches_targets(proc, targets: set[str]) -> Optional[str]:
    """Проверяет psutil.Process и до 5 уровней родительских процессов по именам targets
    (перехватывает дочерние аудиопроцессы вроде msedgewebview2.exe, запущенные из лаунчеров).
    """
    cur = proc
    for _depth in range(6):
        if cur is None:
            break
        try:
            name = cur.name()
        except Exception:
            break
        if name and name.lower() in targets:
            return name
        try:
            cur = cur.parent()
        except Exception:
            break
    return None


def _match_webview_cmdline(
    proc,
    expanded_targets: set[str],
    target_dirs: Optional[set[str]] = None,
) -> bool:
    """Сопоставляет брокерные аудиопроцессы WebView2 (например, когда UWP Xbox App запускает msedgewebview2.exe через svchost.exe)
    по флагам '--webview-exe-name=' и '--user-data-dir=', не затрагивая посторонние приложения с WebView2.
    """
    if proc is None:
        return False
    try:
        pname = (proc.name() or "").lower()
    except Exception:
        return False
    if pname not in _WEBVIEW_HELPER_NAMES:
        return False
    try:
        cmdline_parts = proc.cmdline() or []
    except Exception:
        return False
    if not cmdline_parts:
        return False
    cmd_lower = " ".join(str(part) for part in cmdline_parts).lower()

    for target_exe in expanded_targets:
        if f"--webview-exe-name={target_exe}" in cmd_lower:
            return True
        stem = target_exe[:-4] if target_exe.endswith(".exe") else target_exe
        if stem and (f"\\{stem}\\" in cmd_lower or f"/{stem}/" in cmd_lower):
            return True

    if expanded_targets & _XBOX_FAMILY_LOWER:
        if "microsoft.gamingapp" in cmd_lower:
            return True

    if target_dirs:
        for tdir in target_dirs:
            folder_name = tdir.rstrip("\\/").rsplit("\\", 1)[-1]
            if folder_name and len(folder_name) >= 4 and folder_name in cmd_lower:
                return True
    return False


def _collect_all_wasapi_sessions() -> list[tuple[str, int, str, str, object, object]]:
    """Собирает WASAPI-аудиосессии со ВСЕХ активных устройств вывода (наушники, динамики, монитор).
    Возвращает список (session_key, pid, process_name, exe_dir_lower, psutil_proc_или_None, ISimpleAudioVolume).
    Должна вызываться в потоке с инициализированным COM.
    """
    results: list[tuple[str, int, str, str, object, object]] = []
    seen_keys: set[str] = set()

    try:
        import psutil
        from comtypes import CLSCTX_ALL
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import (
            AudioUtilities,
            IAudioSessionControl2,
            IAudioSessionManager2,
            ISimpleAudioVolume,
        )

        enumerator = AudioUtilities.GetDeviceEnumerator()
        collection = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
        dev_count = collection.GetCount()
        for dev_idx in range(dev_count):
            dev = None
            mgr = None
            sess_enum = None
            try:
                dev = collection.Item(dev_idx)
                try:
                    dev_id = str(dev.GetId() or dev_idx)
                except Exception:
                    dev_id = str(dev_idx)
                iface = dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
                mgr = iface.QueryInterface(IAudioSessionManager2)
                sess_enum = mgr.GetSessionEnumerator()
                sess_count = sess_enum.GetCount()
                for s_idx in range(sess_count):
                    ctl = None
                    ctl2 = None
                    try:
                        ctl = sess_enum.GetSession(s_idx)
                        if not ctl:
                            continue
                        ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                        pid = int(ctl2.GetProcessId() or 0)
                        try:
                            sid = str(ctl2.GetSessionIdentifier() or "")
                        except Exception:
                            sid = ""
                        try:
                            inst_id = str(ctl2.GetSessionInstanceIdentifier() or s_idx)
                        except Exception:
                            inst_id = str(s_idx)

                        # Прямой QueryInterface к ISimpleAudioVolume (работает, даже если psutil не видит защищённый PID)
                        try:
                            vol = ctl.QueryInterface(ISimpleAudioVolume)
                        except Exception:
                            continue
                        if vol is None:
                            continue

                        proc = None
                        pname = ""
                        exe_dir_lower = ""
                        if pid > 0:
                            try:
                                proc = psutil.Process(pid)
                                pname = proc.name() or ""
                                try:
                                    ep = proc.exe() or ""
                                    if ep:
                                        exe_dir_lower = os.path.dirname(ep).lower()
                                except Exception:
                                    pass
                            except Exception:
                                proc = None

                        sid_name, sid_dir = _parse_exe_from_session_id(sid or inst_id)
                        if not pname and sid_name:
                            pname = sid_name
                        if not exe_dir_lower and sid_dir:
                            exe_dir_lower = sid_dir
                        if not pname:
                            continue

                        skey = f"{dev_id}|{pid}|{inst_id}"
                        if skey not in seen_keys:
                            seen_keys.add(skey)
                            results.append((skey, pid, pname, exe_dir_lower, proc, vol))
                    except Exception:
                        continue
                    finally:
                        ctl2 = None
                        ctl = None
            except Exception:
                continue
            finally:
                sess_enum = None
                mgr = None
                dev = None
        collection = None
        enumerator = None
        return results
    except Exception:
        pass

    # Запасной вариант: устройство мультимедиа по умолчанию через AudioUtilities.GetAllSessions()
    try:
        from pycaw.pycaw import AudioUtilities

        for idx, sess in enumerate(AudioUtilities.GetAllSessions()):
            proc = getattr(sess, "Process", None)
            if proc is None:
                continue
            try:
                pname = proc.name()
                pid = int(proc.pid)
            except Exception:
                continue
            vol = getattr(sess, "SimpleAudioVolume", None)
            if pname and vol is not None:
                skey = f"default|{pid}|{idx}"
                if skey not in seen_keys:
                    seen_keys.add(skey)
                    results.append((skey, pid, pname, "", proc, vol))
    except Exception:
        pass
    return results


def list_active_audio_processes() -> list[str]:
    """Возвращает уникальные имена исполняемых файлов, у которых сейчас есть WASAPI-сессия на любом активном устройстве вывода."""
    names: list[str] = []
    seen: set[str] = set()
    try:
        import comtypes

        comtypes.CoInitialize()
        try:
            sessions = _collect_all_wasapi_sessions()
            for _skey, _pid, pname, _dir, _proc, _vol in sessions:
                low = pname.lower()
                if low != "gta6subvoice.exe" and low not in seen:
                    seen.add(low)
                    names.append(pname)
            sessions.clear()
        finally:
            comtypes.CoUninitialize()
    except Exception:
        pass
    return sorted(names, key=str.lower)


def list_selectable_processes() -> list[tuple[str, str]]:
    """Возвращает список запущенных приложений Windows (аудиосессии + видимые окна, включая UWP внутри ApplicationFrameHost)
    в виде (имя_exe, подпись_для_списка). Активные аудиопроцессы идут первыми.
    """
    active_audio = list_active_audio_processes()
    window_titles: dict[str, tuple[str, str]] = {}

    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt
            import psutil

            u32 = ctypes.windll.user32
            wnd_proc_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
            u32.EnumWindows.argtypes = [wnd_proc_t, wt.LPARAM]
            u32.EnumChildWindows.argtypes = [wt.HWND, wnd_proc_t, wt.LPARAM]

            def _cb(hwnd: int, _lp: int) -> bool:
                if not u32.IsWindowVisible(hwnd):
                    return True
                ln = u32.GetWindowTextLengthW(hwnd)
                if ln <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(ln + 1)
                u32.GetWindowTextW(hwnd, buf, ln + 1)
                title = buf.value.strip()
                if not title:
                    return True
                pid = wt.DWORD()
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    pname = psutil.Process(int(pid.value)).name()
                except Exception:
                    return True
                if not pname:
                    return True
                low = pname.lower()
                # Определяем реальный UWP-процесс, размещённый внутри ApplicationFrameHost.exe (например XboxPcApp.exe)
                if low == "applicationframehost.exe":
                    real_name = [""]

                    def _child_cb(ch: int, _clp: int) -> bool:
                        cpid = wt.DWORD()
                        u32.GetWindowThreadProcessId(ch, ctypes.byref(cpid))
                        if int(cpid.value) != int(pid.value):
                            try:
                                cn = psutil.Process(int(cpid.value)).name()
                                if cn and cn.lower() != "applicationframehost.exe":
                                    real_name[0] = cn
                                    return False
                            except Exception:
                                pass
                        return True

                    try:
                        u32.EnumChildWindows(hwnd, wnd_proc_t(_child_cb), 0)
                    except Exception:
                        pass
                    if real_name[0]:
                        pname = real_name[0]
                        low = pname.lower()
                    elif title.lower() == "xbox" or title.lower().startswith("xbox "):
                        pname = "XboxPcApp.exe"
                        low = "xboxpcapp.exe"
                    else:
                        return True

                if low in ("gta6subvoice.exe", "textinputhost.exe", "applicationframehost.exe"):
                    return True
                if low not in window_titles:
                    window_titles[low] = (pname, title[:42])
                return True

            u32.EnumWindows(wnd_proc_t(_cb), 0)
        except Exception:
            pass

    entries: dict[str, tuple[int, str, str]] = {}
    for pname in active_audio:
        low = pname.lower()
        w_info = window_titles.get(low)
        title = w_info[1] if isinstance(w_info, tuple) else ""
        label = f"{pname} — {title} [звук]" if title else f"{pname} [звук]"
        entries[low] = (0, pname, label)

    for low, w_info in window_titles.items():
        if low in entries or not isinstance(w_info, tuple):
            continue
        pname, title = w_info
        entries[low] = (1, pname, f"{pname} — {title}")

    ordered = sorted(entries.values(), key=lambda x: (x[0], x[1].lower()))
    return [(exe, lbl) for _prio, exe, lbl in ordered]


def _atexit_restore(weak_self: weakref.ref) -> None:
    obj = weak_self()
    if obj is not None:
        try:
            obj.restore_immediately()
        except Exception:
            pass


class AutoDucker:
    """Плавный однопоточный Windows WASAPI автоприглушитель/Mute по ВСЕМ активным устройствам вывода:
    - Один постоянный рабочий поток BELOW_NORMAL (1× CoInitialize / CoUninitialize -> без утечек потоков и COM).
    - Сканирует ВСЕ активные устройства вывода Windows через прямой ISimpleAudioVolume + GetSessionIdentifier
      (работает даже на играх с повышенными правами, UWP и античитами, где psutil возвращает AccessDenied).
    - Сопоставляет целевой процесс, семейство процессов Xbox, дочерние аудиопроцессы (до 5 уровней вверх,
      а также брокерный msedgewebview2.exe по cmdline) и процессы из той же папки установки.
    - Принудительно удерживает уровень приглушения / Mute каждые 120 мс во время речи/чтения.
    - Плавное снижение (80 мс), удержание после речи (250 мс) и плавное восстановление (320 мс).
    - Гарантированный возврат исходной громкости и Mute при остановке, смене процесса и выходе через weakref atexit.
    """

    def __init__(
        self,
        enabled: bool = True,
        depth_pct: int = 75,
        target_processes: Optional[list[str]] = None,
        duck_attack_ms: float = 80.0,
        release_delay_ms: float = 250.0,
        release_fade_ms: float = 320.0,
        session_provider: Optional[Callable[[], list[tuple[int, str, object]]]] = None,
        on_diagnostic: Optional[Callable[[str], None]] = None,
        mute_window: bool = False,
        enforce_interval_ms: float = 120.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.depth_pct = max(10, min(95, int(depth_pct)))
        self.mute_window = bool(mute_window)
        self.enforce_interval_ms = max(20.0, float(enforce_interval_ms))
        self.target_processes: list[str] = list(target_processes or DEFAULT_DUCKING_PROCESSES)
        self.duck_attack_ms = float(duck_attack_ms)
        self.release_delay_ms = float(release_delay_ms)
        self.release_fade_ms = float(release_fade_ms)
        self._session_provider = session_provider
        self._on_diagnostic = on_diagnostic

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

        # Сохранённые исходные громкости и состояния Mute пользователя: session_key -> состояние
        self._saved_volumes: dict[str, float] = {}
        self._saved_mutes: dict[str, tuple[bool, Optional[float]]] = {}
        self._reading_active = False
        self._speech_active = False
        self._speech_ended_at = 0.0
        self._is_ducked = False
        self._fully_ducked = False
        self._needs_duck_down = False
        self._is_muted = False
        self._needs_mute_apply = False
        self._closed = False
        self._worker: Optional[threading.Thread] = None

        self.last_ducked_processes: list[str] = []
        self.last_error: Optional[str] = None
        self._last_diag_key: str = ""
        self._atexit_cb = lambda ref=weakref.ref(self): _atexit_restore(ref)
        atexit.register(self._atexit_cb)

    def _emit_diag_once(self, key: str, msg: str) -> None:
        if key == self._last_diag_key or self._on_diagnostic is None:
            return
        self._last_diag_key = key
        try:
            self._on_diagnostic(msg)
        except Exception:
            pass

    def _ensure_worker_locked(self) -> None:
        if self._closed:
            return
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="AutoDuckerWorker",
                daemon=True,
            )
            self._worker.start()

    def configure(
        self,
        enabled: bool,
        depth_pct: int,
        target_processes: list[str],
        mute_window: Optional[bool] = None,
    ) -> None:
        restore_duck_only = False
        restore_mute_only = False
        restore_all_on_proc_change = False
        with self._lock:
            was_enabled = self.enabled
            was_mute = self.mute_window
            old_depth = self.depth_pct
            old_procs = self.target_processes

            self.enabled = bool(enabled)
            self.depth_pct = max(10, min(95, int(depth_pct)))
            if mute_window is not None:
                self.mute_window = bool(mute_window)
            self.target_processes = [p.strip() for p in target_processes if p.strip()] or list(DEFAULT_DUCKING_PROCESSES)
            procs_changed = self.target_processes != old_procs

            if procs_changed and (self._is_ducked or self._is_muted):
                restore_all_on_proc_change = True
            else:
                if (was_mute and not self.mute_window) and (self._is_muted or self._saved_mutes):
                    restore_mute_only = True
                if ((was_enabled and not self.enabled) or (not was_mute and self.mute_window and self._reading_active)) and (
                    self._is_ducked or self._saved_volumes
                ):
                    restore_duck_only = True

            if procs_changed:
                self._last_diag_key = ""

        if restore_all_on_proc_change:
            self._restore_sessions_sync(restore_volumes=True, restore_mutes=True)
        else:
            if restore_duck_only:
                self._restore_sessions_sync(restore_volumes=True, restore_mutes=False)
            if restore_mute_only:
                self._restore_sessions_sync(restore_volumes=False, restore_mutes=True)

        with self._lock:
            if self._closed:
                return
            if self.mute_window and self._reading_active:
                self._needs_mute_apply = True
                self._ensure_worker_locked()
                self._cond.notify_all()
            elif self.enabled and self._speech_active and (
                restore_all_on_proc_change
                or restore_mute_only
                or self.depth_pct != old_depth
                or procs_changed
                or (not was_enabled and self.enabled)
            ):
                self._fully_ducked = False
                self._needs_duck_down = True
                self._last_diag_key = ""
                self._ensure_worker_locked()
                self._cond.notify_all()

    def _get_target_sessions(self) -> tuple[list[tuple[str, int, str, object]], list[str]]:
        """Возвращает (список совпавших (session_key, pid, process_name, SimpleAudioVolume), имена_всех_активных)."""
        with self._lock:
            raw_targets = {p.lower() for p in self.target_processes}
        expanded = _expand_target_names(raw_targets)

        matched: list[tuple[str, int, str, object]] = []
        all_names: list[str] = []
        seen_names: set[str] = set()

        if self._session_provider is not None:
            for idx, (pid, pname, vol_iface) in enumerate(self._session_provider()):
                if pname:
                    pl = pname.lower()
                    if pl not in seen_names:
                        seen_names.add(pl)
                        all_names.append(pname)
                    if pl in expanded and vol_iface is not None:
                        matched.append((f"mock|{int(pid)}|{idx}", int(pid), pname, vol_iface))
            return matched, all_names

        all_sessions = _collect_all_wasapi_sessions()
        target_dirs: Optional[set[str]] = None

        for skey, pid, pname, exe_dir_lower, proc, vol in all_sessions:
            pl = pname.lower()
            if pl == "gta6subvoice.exe":
                continue
            if pl not in seen_names:
                seen_names.add(pl)
                all_names.append(pname)

            is_match = (pl in expanded) or (proc is not None and _process_matches_targets(proc, expanded) is not None)
            if not is_match and (exe_dir_lower or (proc is not None and pl in _WEBVIEW_HELPER_NAMES)):
                if target_dirs is None:
                    target_dirs = _collect_target_app_dirs(expanded)
                if exe_dir_lower and _match_dir_suffix(exe_dir_lower, target_dirs):
                    is_match = True
                elif proc is not None and _match_webview_cmdline(proc, expanded, target_dirs):
                    is_match = True

            if is_match:
                matched.append((skey, pid, pname, vol))

        return matched, all_names

    def _get_all_sessions_by_key(self) -> dict[str, object]:
        """Возвращает карту session_key -> ISimpleAudioVolume по ВСЕМ активным WASAPI-сессиям (для безопасного возврата)."""
        out: dict[str, object] = {}
        if self._session_provider is not None:
            for idx, (pid, _pname, vol_iface) in enumerate(self._session_provider()):
                if vol_iface is not None:
                    out[f"mock|{int(pid)}|{idx}"] = vol_iface
            return out
        for skey, _pid, _pname, _dir, _proc, vol in _collect_all_wasapi_sessions():
            out[skey] = vol
        return out

    def probe_target_sessions(self) -> tuple[list[str], list[str]]:
        """Диагностический метод для UI: возвращает (найденные_целевые_процессы, все_активные_аудиопроцессы)."""
        if self._session_provider is not None:
            matched, all_names = self._get_target_sessions()
            return sorted({m[2] for m in matched}, key=str.lower), sorted(all_names, key=str.lower)
        try:
            import comtypes

            comtypes.CoInitialize()
            try:
                matched, all_names = self._get_target_sessions()
                return sorted({m[2] for m in matched}, key=str.lower), sorted(all_names, key=str.lower)
            finally:
                comtypes.CoUninitialize()
        except Exception:
            return [], []

    def on_reading_start(self) -> None:
        """Вызывается при старте чтения субтитров (Старт / F8). Активирует полный Mute окна, если он включён."""
        with self._lock:
            if self._closed:
                return
            self._reading_active = True
            self._last_diag_key = ""
            if self.mute_window:
                self._needs_mute_apply = True
                self._ensure_worker_locked()
            self._cond.notify_all()

    def on_reading_end(self) -> None:
        """Вызывается при остановке чтения субтитров (Стоп / F8). Восстанавливает и Mute, и приглушённую громкость."""
        self.restore_immediately()

    def on_speech_start(self) -> None:
        """Сверхбыстрый (< 10 мкс) вызов, безопасный для многократного запуска из потоков TTS и аудио."""
        with self._lock:
            if self._closed:
                return
            if not self.enabled and not (self.mute_window and self._reading_active):
                return
            self._speech_active = True
            if self.enabled and not (self.mute_window and self._reading_active):
                # На каждом старте фразы перепроверяем сессии, чтобы сразу приглушать новые аудиопотоки игры/WebRTC
                self._needs_duck_down = True
                self._ensure_worker_locked()
            self._cond.notify_all()

    def on_speech_end(self) -> None:
        """Сверхбыстрый (< 10 мкс) вызов, безопасный для PortAudio-колбэка."""
        with self._lock:
            if not self._speech_active and not self._is_ducked:
                return
            self._speech_active = False
            self._speech_ended_at = time.monotonic()
            self._cond.notify_all()

    def _worker_loop(self) -> None:
        _set_thread_below_normal()
        com_inited = False
        if self._session_provider is None:
            try:
                import comtypes

                comtypes.CoInitialize()
                com_inited = True
            except Exception as exc:
                self.last_error = f"COM init error: {exc}"
                with self._lock:
                    self._emit_diag_once(f"com-err:{exc}", f"Ducking COM ошибка: {exc}")

        try:
            while True:
                action = "idle"
                with self._lock:
                    if self._closed:
                        break
                    want_mute = self.mute_window and self._reading_active
                    if not self.enabled and not want_mute:
                        self._cond.wait(timeout=1.0)
                        continue

                    if want_mute:
                        if self._needs_mute_apply:
                            action = "mute"
                        else:
                            self._cond.wait(timeout=self.enforce_interval_ms / 1000.0)
                            if self.mute_window and self._reading_active and not self._closed:
                                action = "enforce_mute"
                            else:
                                continue
                    elif self.enabled and self._speech_active:
                        if self._needs_duck_down or not self._fully_ducked:
                            action = "duck"
                        else:
                            # Периодически проверяем уровень, чтобы WebRTC / Xbox / игровые движки не сбрасывали громкость во время речи
                            self._cond.wait(timeout=self.enforce_interval_ms / 1000.0)
                            if self._speech_active and self.enabled and not (self.mute_window and self._reading_active) and not self._closed:
                                action = "enforce"
                            else:
                                continue
                    elif self._is_ducked:
                        delay_s = max(0.01, self.release_delay_ms / 1000.0)
                        elapsed = time.monotonic() - self._speech_ended_at
                        rem = delay_s - elapsed
                        if rem > 0.002:
                            self._cond.wait(timeout=rem)
                            continue
                        action = "restore"
                    else:
                        self._cond.wait(timeout=1.0)
                        continue

                if action == "mute":
                    self._perform_mute_sessions()
                elif action == "enforce_mute":
                    self._enforce_window_mute()
                elif action == "duck":
                    self._perform_duck_down()
                elif action == "enforce":
                    self._enforce_ducked_level()
                elif action == "restore":
                    self._perform_restore_fade()
        finally:
            if com_inited:
                try:
                    import comtypes

                    comtypes.CoUninitialize()
                except Exception:
                    pass

    @staticmethod
    def _mute_session(vol: object) -> Optional[tuple[bool, Optional[float]]]:
        """Отключает звук WASAPI-сессии через ISimpleAudioVolume.SetMute, проверяя срабатывание и откатываясь к громкости 0.0."""
        get_mute = getattr(vol, "GetMute", None)
        set_mute = getattr(vol, "SetMute", None)
        if callable(get_mute) and callable(set_mute):
            try:
                orig_mute = bool(int(get_mute()))
                if not orig_mute:
                    set_mute(1, None)
                    # Некоторые аудиодрайверы и UWP-сессии принимают SetMute(1) с S_OK, но оставляют GetMute() == 0
                    if bool(int(get_mute())):
                        return (orig_mute, None)
                else:
                    return (orig_mute, None)
            except Exception:
                pass
        try:
            orig_vol = float(vol.GetMasterVolume())
            vol.SetMasterVolume(0.0, None)
            return (False, orig_vol)
        except Exception:
            return None

    @staticmethod
    def _enforce_single_mute(vol: object, saved_state: tuple[bool, Optional[float]]) -> None:
        _orig_mute, orig_vol_fallback = saved_state
        if orig_vol_fallback is None:
            try:
                if not bool(int(vol.GetMute())):
                    vol.SetMute(1, None)
            except Exception:
                pass
        else:
            try:
                if float(vol.GetMasterVolume()) > 0.01:
                    vol.SetMasterVolume(0.0, None)
            except Exception:
                pass

    def _apply_mute_to_sessions_locked(self, sessions: list[tuple[str, int, str, object]]) -> list[str]:
        matched_names: list[str] = []
        for skey, _pid, pname, vol in sessions:
            if skey not in self._saved_mutes:
                state = self._mute_session(vol)
                if state is not None:
                    self._saved_mutes[skey] = state
            else:
                self._enforce_single_mute(vol, self._saved_mutes[skey])
            if pname not in matched_names:
                matched_names.append(pname)
        if self._saved_mutes:
            self._is_muted = True
        if matched_names:
            self.last_ducked_processes = matched_names
            self.last_error = None
            self._emit_diag_once(
                f"mute-ok:{','.join(matched_names)}",
                f"Звук окна полностью отключён: {', '.join(matched_names)} (Mute)",
            )
        return matched_names

    def _perform_mute_sessions(self) -> None:
        sessions = []
        try:
            sessions, all_names = self._get_target_sessions()
            if not sessions:
                with self._lock:
                    self._needs_mute_apply = False
                    targets_str = ", ".join(self.target_processes)
                    active_hint = f" (сейчас со звуком: {', '.join(all_names[:4])})" if all_names else ""
                    self._emit_diag_once(
                        f"mute-missing:{targets_str}:{','.join(all_names[:4])}",
                        f"Отключение звука окна: аудиосессия не найдена для {targets_str}{active_hint}",
                    )
                return

            with self._lock:
                if not self.mute_window or not self._reading_active or self._closed:
                    return
                self._apply_mute_to_sessions_locked(sessions)
                self._needs_mute_apply = False
        except Exception as exc:
            self.last_error = str(exc)
            with self._lock:
                self._needs_mute_apply = False
                self._emit_diag_once(f"mute-err:{exc}", f"Ошибка отключения звука окна: {exc}")
        finally:
            sessions.clear()

    def _enforce_window_mute(self) -> None:
        """Подхватывает поздно созданные аудиосессии игры или возвращает Mute, если игра/WebRTC сама сняла его."""
        sessions = []
        try:
            sessions, _all_names = self._get_target_sessions()
            if not sessions:
                return
            with self._lock:
                if not self.mute_window or not self._reading_active or self._closed:
                    return
                self._apply_mute_to_sessions_locked(sessions)
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            sessions.clear()

    def _enforce_ducked_level(self) -> None:
        """Удерживает громкость цели приглушённой, если игра или поток WebRTC создаёт новую сессию или сбрасывает уровень во время речи."""
        sessions = []
        try:
            sessions, _all_names = self._get_target_sessions()
            if not sessions:
                return
            with self._lock:
                if not self._speech_active or not self.enabled or self._closed:
                    return
                depth_factor = max(0.05, min(0.95, 1.0 - self.depth_pct / 100.0))
                for skey, _pid, _pname, vol in sessions:
                    try:
                        cur = float(vol.GetMasterVolume())
                    except Exception:
                        continue
                    if skey not in self._saved_volumes:
                        self._saved_volumes[skey] = cur
                    dst = max(0.0, min(1.0, self._saved_volumes[skey] * depth_factor))
                    if cur > dst + 0.03:
                        try:
                            vol.SetMasterVolume(dst, None)
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            sessions.clear()

    def _perform_duck_down(self) -> None:
        sessions = []
        try:
            sessions, all_names = self._get_target_sessions()
            if not sessions:
                with self._lock:
                    self._needs_duck_down = False
                    self._fully_ducked = False
                    targets_str = ", ".join(self.target_processes)
                    active_hint = f" (сейчас со звуком: {', '.join(all_names[:4])})" if all_names else ""
                    self._emit_diag_once(
                        f"missing:{targets_str}:{','.join(all_names[:4])}",
                        f"Auto-Ducking: аудиосессия не найдена для {targets_str}{active_hint}",
                    )
                    if self._speech_active:
                        self._cond.wait(timeout=0.30)
                return

            with self._lock:
                if not self._speech_active or not self.enabled or self._closed:
                    return
                depth_factor = max(0.05, min(0.95, 1.0 - self.depth_pct / 100.0))
                targets: list[tuple[str, object, float, float]] = []
                matched_names: list[str] = []
                needs_ramp = False
                for skey, _pid, pname, vol in sessions:
                    try:
                        cur = float(vol.GetMasterVolume())
                    except Exception:
                        continue
                    if skey not in self._saved_volumes:
                        self._saved_volumes[skey] = cur
                    orig = self._saved_volumes[skey]
                    dst = max(0.0, min(1.0, orig * depth_factor))
                    targets.append((skey, vol, cur, dst))
                    if abs(cur - dst) > 0.025:
                        needs_ramp = True
                    if pname not in matched_names:
                        matched_names.append(pname)

                if not targets:
                    self._needs_duck_down = False
                    return

                self._is_ducked = True
                self.last_ducked_processes = matched_names
                self.last_error = None
                self._emit_diag_once(
                    f"ok:{','.join(matched_names)}:{self.depth_pct}",
                    f"Auto-Ducking активен: {', '.join(matched_names)} (-{self.depth_pct}%)",
                )

            if not needs_ramp:
                with self._lock:
                    if self._speech_active and self.enabled:
                        self._fully_ducked = True
                        self._needs_duck_down = False
                return

            steps = max(4, min(12, int(round(self.duck_attack_ms / 6.5))))
            step_sleep = max(0.004, (self.duck_attack_ms / 1000.0) / steps)
            completed = True
            for s in range(1, steps + 1):
                with self._lock:
                    if not self._speech_active or not self.enabled or self._closed:
                        completed = False
                        break
                # Плавная косинусная S-кривая исключает щелчки регулировки громкости на сессиях HDMI/WASAPI
                t = 0.5 * (1.0 - math.cos(math.pi * (s / float(steps))))
                for _skey, vol, start_v, end_v in targets:
                    try:
                        vol.SetMasterVolume(start_v + (end_v - start_v) * t, None)
                    except Exception:
                        pass
                if s < steps:
                    with self._lock:
                        self._cond.wait(timeout=step_sleep)

            with self._lock:
                if completed and self._speech_active and self.enabled:
                    self._fully_ducked = True
                    self._needs_duck_down = False
        except Exception as exc:
            self.last_error = str(exc)
            with self._lock:
                self._needs_duck_down = False
                self._emit_diag_once(f"duck-err:{exc}", f"Auto-Ducking ошибка: {exc}")
                self._cond.wait(timeout=0.30)
        finally:
            sessions.clear()

    def _perform_restore_fade(self) -> None:
        all_vols: dict[str, object] = {}
        try:
            with self._lock:
                saved_copy = dict(self._saved_volumes)
                self._fully_ducked = False
            if not saved_copy:
                with self._lock:
                    self._is_ducked = False
                return

            all_vols = self._get_all_sessions_by_key()
            pairs: list[tuple[str, object, float, float]] = []
            for skey, orig_v in saved_copy.items():
                vol = all_vols.get(skey)
                if vol is None:
                    continue
                try:
                    cur = float(vol.GetMasterVolume())
                except Exception:
                    continue
                pairs.append((skey, vol, cur, orig_v))

            if not pairs:
                with self._lock:
                    if not self._speech_active:
                        self._is_ducked = False
                        self._saved_volumes.clear()
                return

            steps = max(6, min(24, int(round(self.release_fade_ms / 13.0))))
            step_sleep = max(0.004, (self.release_fade_ms / 1000.0) / steps)
            interrupted = False
            for s in range(1, steps + 1):
                with self._lock:
                    if self._speech_active or self._closed or not self._is_ducked:
                        interrupted = True
                        break
                # Плавная косинусная S-кривая исключает артефакты ступенчатого возврата громкости игры
                t = 0.5 * (1.0 - math.cos(math.pi * (s / float(steps))))
                for _skey, vol, start_v, end_v in pairs:
                    try:
                        vol.SetMasterVolume(start_v + (end_v - start_v) * t, None)
                    except Exception:
                        pass
                if s < steps:
                    with self._lock:
                        self._cond.wait(timeout=step_sleep)

            with self._lock:
                if not interrupted and not self._speech_active:
                    self._is_ducked = False
                    self._fully_ducked = False
                    self._saved_volumes.clear()
                elif interrupted and self._speech_active:
                    self._needs_duck_down = True
                    self._fully_ducked = False
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            all_vols.clear()

    def _restore_sessions_sync(self, restore_volumes: bool = True, restore_mutes: bool = True) -> None:
        saved_vols: dict[str, float] = {}
        saved_mutes: dict[str, tuple[bool, Optional[float]]] = {}
        with self._lock:
            if restore_volumes:
                self._needs_duck_down = False
                self._fully_ducked = False
                self._is_ducked = False
                saved_vols = dict(self._saved_volumes)
                self._saved_volumes.clear()
            if restore_mutes:
                self._needs_mute_apply = False
                self._is_muted = False
                saved_mutes = dict(self._saved_mutes)
                self._saved_mutes.clear()
            self._cond.notify_all()

        if not saved_vols and not saved_mutes:
            return

        def _do_restore() -> None:
            all_vols: dict[str, object] = {}
            try:
                all_vols = self._get_all_sessions_by_key()
                for skey, orig_v in saved_vols.items():
                    vol = all_vols.get(skey)
                    if vol is not None:
                        try:
                            vol.SetMasterVolume(orig_v, None)
                        except Exception:
                            pass
                for skey, (orig_mute, orig_vol_fallback) in saved_mutes.items():
                    vol = all_vols.get(skey)
                    if vol is None:
                        continue
                    if orig_vol_fallback is None:
                        try:
                            vol.SetMute(1 if orig_mute else 0, None)
                        except Exception:
                            pass
                    else:
                        try:
                            vol.SetMasterVolume(orig_vol_fallback, None)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                all_vols.clear()

        if self._session_provider is not None:
            _do_restore()
            return

        try:
            import comtypes

            comtypes.CoInitialize()
            try:
                _do_restore()
            finally:
                comtypes.CoUninitialize()
        except Exception:
            pass

    def restore_speech_ducking(self) -> None:
        """Восстанавливает только временное приглушение речи (например по F10), сохраняя Mute окна, если чтение активно."""
        with self._lock:
            self._speech_active = False
        self._restore_sessions_sync(restore_volumes=True, restore_mutes=False)

    def restore_immediately(self) -> None:
        """Синхронно и безусловно возвращает исходную громкость и состояние Mute при остановке или выходе."""
        with self._lock:
            self._reading_active = False
            self._speech_active = False
        self._restore_sessions_sync(restore_volumes=True, restore_mutes=True)

    def close(self) -> None:
        """Корректно восстанавливает громкость и останавливает фоновый поток."""
        self.restore_immediately()
        with self._lock:
            self._closed = True
            self._cond.notify_all()
            th = self._worker
            self._worker = None
        if th is not None and th.is_alive():
            th.join(timeout=1.0)
        try:
            atexit.unregister(self._atexit_cb)
        except Exception:
            pass
