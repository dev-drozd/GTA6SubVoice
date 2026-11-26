from __future__ import annotations

import time

import numpy as np


SPEECH_PRESETS: tuple[str, ...] = ("medium", "fast", "x-fast")
PRESET_RATE_VALUES: dict[str, float] = {
    "medium": 1.0,
    "fast": 1.2,
    "x-fast": 1.5,
}
PRESET_DISPLAY_LABELS: dict[str, str] = {
    "medium": "Обычный (1.0x)",
    "fast": "Быстрый (1.2x)",
    "x-fast": "Очень быстрый (1.5x)",
}


def normalize_speed_preset(raw_speed: str | float | int | None) -> str:
    """Нормализует имя пресета или устаревший числовой коэффициент в официальный пресет Silero SSML ('medium'|'fast'|'x-fast')."""
    if isinstance(raw_speed, str):
        s = raw_speed.strip().lower()
        if s in SPEECH_PRESETS:
            return s
        try:
            num = float(s.rstrip("xх"))
        except ValueError:
            return "medium"
    elif isinstance(raw_speed, (int, float)):
        num = float(raw_speed)
    else:
        return "medium"

    if num <= 1.05:
        return "medium"
    if num <= 1.35:
        return "fast"
    return "x-fast"


def compute_adaptive_speed(
    base_speed: str | float = "medium",
    text_len: int = 0,
    adaptive_tempo: bool = False,
    backlog_sec: float = 0.0,
    recent_burst: int = 0,
) -> tuple[str, int]:
    """Вычисляет целевой официальный пресет Silero SSML ('medium'|'fast'|'x-fast', без WSOLA и DSP-искажений):
    - Если adaptive_tempo=False -> возвращает (нормализованный базовый пресет, 0).
    - Если adaptive_tempo=True -> фиксирует базу на 'medium' и ускоряет максимум до 'fast' при необходимости
      (наличие накопленной очереди озвучки / серии реплик / длинного субтитра).
    Возвращает (итоговый_пресет, ступень_ускорения).
    """
    base_preset = normalize_speed_preset(base_speed)
    if not adaptive_tempo:
        return base_preset, 0

    needs_boost = backlog_sec >= 0.45 or recent_burst >= 1 or text_len > 105
    if needs_boost:
        return "fast", 1
    return "medium", 0


def trim_silence(
    audio: np.ndarray,
    sample_rate: int = 48000,
    threshold_db: float = -54.0,
    pad_ms: float = 45.0,
) -> np.ndarray:
    """Обрезает тишину в начале и конце ниже -54 дБ с запасом 45 мс (сохраняет мягкую вопросительную интонацию
    и дыхание на концах фраз) и плавным 10 мс косинусным фейдом по краям.
    """
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return x

    threshold = 10.0 ** (threshold_db / 20.0)
    mask = np.abs(x) >= threshold
    if not np.any(mask):
        return x

    idx = np.flatnonzero(mask)
    pad = int(round(sample_rate * pad_ms / 1000.0))
    start = max(0, int(idx[0]) - pad)
    end = min(x.size, int(idx[-1]) + 1 + pad)
    trimmed = x[start:end].copy()

    # Плавный 10 мс косинусный микро-фейд строго на тихой внешней границе (без щелчков и влияния на тембр)
    fade = min(int(round(sample_rate * 0.010)), trimmed.size // 4)
    if fade > 1:
        t = np.linspace(0.0, np.pi, fade, dtype=np.float32)
        ramp = (0.5 * (1.0 - np.cos(t))).astype(np.float32)
        trimmed[:fade] *= ramp
        trimmed[-fade:] *= ramp[::-1]
    return trimmed


def process_audio(
    audio: np.ndarray,
    sample_rate: int = 48000,
    volume: float = 1.0,
    trim: bool = True,
) -> tuple[np.ndarray, float]:
    """Чистая передача нативного голоса без обработки (без эквалайзера, компрессии, тайм-стретча, питча и нелинейного клиппинга):
    1. Опциональная обрезка тишины по краям (-54 дБ, запас 45 мс, косинусный фейд 10 мс)
    2. Опциональное линейное усиление громкости (строгий 1:1 passthrough при volume == 1.0)
    3. Линейный потолок амплитуды (0.99) ТОЛЬКО если пик > 1.0 (защита от треска при громкости > 100%)
    Возвращает (одномерный массив float32 в [-1.0, 1.0], время_мс).
    """
    t0 = time.perf_counter()
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return x, 0.0

    if not np.all(np.isfinite(x)):
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)

    if trim:
        x = trim_silence(x, sample_rate=sample_rate, threshold_db=-54.0, pad_ms=45.0)
        if x.size == 0:
            return x, (time.perf_counter() - t0) * 1000.0

    vol = max(0.0, min(2.0, float(volume)))
    if abs(vol - 1.0) > 1e-3:
        x = (x * vol).astype(np.float32, copy=False)

    if x.size > 0:
        peak = float(np.max(np.abs(x)))
        if peak > 1.0:
            # Линейное масштабирование пика предотвращает жёсткий цифровой клиппинг без искажения тембра
            x = (x * (0.99 / peak)).astype(np.float32, copy=False)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return x, elapsed_ms
