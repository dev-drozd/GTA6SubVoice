from __future__ import annotations

import os
import platform
import struct
import sys
from dataclasses import dataclass
from typing import Optional

import psutil

# Минимальные аппаратные и программные требования GTA6SubVoice
MIN_WINDOWS_BUILD = 18362  # Windows 10 (1903+) или Windows 11 (сборка >= 22000)
MIN_CPU_THREADS = 4
MIN_RAM_ADVERTISED_GB = 8
# Windows и BIOS резервируют часть из 8 ГБ под встроенную графику, поэтому на 8 ГБ ноутбуках
# доступный ОС объём virtual_memory().total обычно составляет ~7.2–7.9 ГБ.
# Порог 6.5 ГБ отсекает ПК с 4–6 ГБ ОЗУ без ложных срабатываний на 8 ГБ системах.
MIN_EFFECTIVE_RAM_GB = 6.5
PF_AVX2_INSTRUCTIONS_AVAILABLE = 40


@dataclass(frozen=True)
class SystemInfo:
    """Снимок ключевых аппаратных и программных характеристик текущего ПК."""

    platform_name: str
    architecture: str
    pointer_bits: int
    win_major: int
    win_minor: int
    win_build: int
    cpu_threads: int
    avx2_supported: Optional[bool]
    ram_total_bytes: int


@dataclass(frozen=True)
class SystemCheckItem:
    """Результат проверки одного системного требования."""

    key: str
    title: str
    required: str
    detected: str
    passed: bool


@dataclass(frozen=True)
class SystemCheckReport:
    """Итоговый отчёт совместимости ПК с минимальными требованиями программы."""

    ok: bool
    info: SystemInfo
    items: tuple[SystemCheckItem, ...]

    @property
    def failed_items(self) -> list[SystemCheckItem]:
        return [item for item in self.items if not item.passed]

    def summary_text(self) -> str:
        lines = [
            f"{'[OK]' if item.passed else '[НЕТ]'} {item.title}: {item.detected} (требуется: {item.required})"
            for item in self.items
        ]
        return "\n".join(lines)


def _format_threads_ru(count: int) -> str:
    n = abs(int(count))
    if 11 <= (n % 100) <= 14:
        word = "потоков"
    elif n % 10 == 1:
        word = "поток"
    elif n % 10 in (2, 3, 4):
        word = "потока"
    else:
        word = "потоков"
    return f"{count} {word}"


def _detect_avx2_windows() -> Optional[bool]:
    """Определяет поддержку инструкций AVX2 на Windows через kernel32.IsProcessorFeaturePresent."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        return bool(k32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE))
    except Exception:
        return None


def collect_system_info() -> SystemInfo:
    """Собирает системную информацию о текущей ОС, CPU и оперативной памяти."""
    win_major = 0
    win_minor = 0
    win_build = 0
    if sys.platform == "win32":
        try:
            ver = sys.getwindowsversion()
            win_major = int(ver.major)
            win_minor = int(ver.minor)
            win_build = int(ver.build)
        except Exception:
            pass

    try:
        ram_total = int(psutil.virtual_memory().total)
    except Exception:
        ram_total = 0

    return SystemInfo(
        platform_name=sys.platform,
        architecture=platform.machine() or "unknown",
        pointer_bits=struct.calcsize("P") * 8,
        win_major=win_major,
        win_minor=win_minor,
        win_build=win_build,
        cpu_threads=int(os.cpu_count() or 0),
        avx2_supported=_detect_avx2_windows(),
        ram_total_bytes=ram_total,
    )


def _format_os_name(info: SystemInfo) -> str:
    if info.platform_name != "win32":
        return f"{info.platform_name} ({info.architecture}, {info.pointer_bits}-bit)"
    if info.win_major == 10 and info.win_build >= 22000:
        os_label = "Windows 11"
    elif info.win_major >= 10:
        os_label = f"Windows {info.win_major}"
    elif info.win_major > 0:
        os_label = f"Windows {info.win_major}.{info.win_minor}"
    else:
        os_label = "Windows"
    return f"{os_label} ({info.pointer_bits}-bit, сборка {info.win_build})"


def check_system_requirements(info: Optional[SystemInfo] = None) -> SystemCheckReport:
    """Проверяет, соответствует ли ПК минимальным системным требованиям GTA6SubVoice."""
    sys_info = info or collect_system_info()
    arch_low = (sys_info.architecture or "").strip().lower()

    os_is_win64 = sys_info.platform_name == "win32" and sys_info.pointer_bits == 64 and arch_low in ("amd64", "x86_64")
    win_ver_ok = sys_info.win_major > 10 or (
        sys_info.win_major == 10 and sys_info.win_build >= MIN_WINDOWS_BUILD
    )
    os_ok = os_is_win64 and win_ver_ok

    threads_ok = sys_info.cpu_threads >= MIN_CPU_THREADS

    # Если IsProcessorFeaturePresent вернул None из-за редкой ошибки API, не блокируем ложно на Win10/11
    avx2_ok = sys_info.avx2_supported is not False
    if sys_info.avx2_supported is True:
        avx2_detected = "Поддерживаются"
    elif sys_info.avx2_supported is False:
        avx2_detected = "Не поддерживаются"
    else:
        avx2_detected = "Не определено"

    ram_gb = sys_info.ram_total_bytes / float(1024**3) if sys_info.ram_total_bytes > 0 else 0.0
    ram_ok = ram_gb >= MIN_EFFECTIVE_RAM_GB

    items = (
        SystemCheckItem(
            key="os",
            title="Операционная система",
            required="Windows 10 (1903+) / Windows 11 (64-bit)",
            detected=_format_os_name(sys_info),
            passed=os_ok,
        ),
        SystemCheckItem(
            key="cpu_threads",
            title="Процессор (потоки)",
            required=f"От {MIN_CPU_THREADS} потоков",
            detected=_format_threads_ru(sys_info.cpu_threads),
            passed=threads_ok,
        ),
        SystemCheckItem(
            key="avx2",
            title="Инструкции процессора (AVX2)",
            required="Поддержка AVX2",
            detected=avx2_detected,
            passed=avx2_ok,
        ),
        SystemCheckItem(
            key="ram",
            title="Оперативная память",
            required=f"От {MIN_RAM_ADVERTISED_GB} ГБ",
            detected=f"{ram_gb:.1f} ГБ" if ram_gb > 0 else "Не определено",
            passed=ram_ok,
        ),
    )

    all_ok = all(item.passed for item in items)
    return SystemCheckReport(ok=all_ok, info=sys_info, items=items)
