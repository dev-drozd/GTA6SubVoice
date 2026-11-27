from __future__ import annotations

import collections
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import sounddevice as sd


@dataclass
class AudioDeviceInfo:
    index: int
    name: str
    hostapi: str
    is_default: bool


def list_output_devices() -> list[AudioDeviceInfo]:
    """Возвращает список доступных устройств вывода Windows с приоритетом WASAPI."""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_out = sd.default.device[1]
        wasapi_default = -1
        for h in hostapis:
            if "WASAPI" in str(h.get("name", "")).upper():
                wasapi_default = int(h.get("default_output_device", -1))
                break
    except Exception:
        return []

    wasapi_items: list[AudioDeviceInfo] = []
    other_items: list[AudioDeviceInfo] = []

    for idx, dev in enumerate(devices):
        if dev.get("max_output_channels", 0) <= 0:
            continue
        ha_idx = dev.get("hostapi", 0)
        ha_name = hostapis[ha_idx]["name"] if 0 <= ha_idx < len(hostapis) else ""
        info = AudioDeviceInfo(
            index=idx,
            name=str(dev["name"]),
            hostapi=ha_name,
            is_default=(idx == wasapi_default or idx == default_out),
        )
        if "WASAPI" in ha_name.upper():
            wasapi_items.append(info)
        else:
            other_items.append(info)

    seen_names = {d.name for d in wasapi_items}
    result = list(wasapi_items)
    for item in other_items:
        if item.name not in seen_names:
            result.append(item)
            seen_names.add(item.name)
    return result


def resolve_device_index(device_name: Optional[str]) -> Optional[int]:
    if not device_name:
        try:
            for h in sd.query_hostapis():
                if "WASAPI" in str(h.get("name", "")).upper():
                    wasapi_def = int(h.get("default_output_device", -1))
                    if wasapi_def >= 0:
                        return wasapi_def
        except Exception:
            pass
        return None

    for dev in list_output_devices():
        if dev.name == device_name:
            return dev.index
    lower = device_name.lower()
    for dev in list_output_devices():
        if lower in dev.name.lower():
            return dev.index
    return None


def _resample_to_rate(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr or audio.size < 2:
        return audio
    out_len = max(1, int(round(audio.size * (dst_sr / float(src_sr)))))
    x_old = np.linspace(0.0, 1.0, audio.size, dtype=np.float32)
    x_new = np.linspace(0.0, 1.0, out_len, dtype=np.float32)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def _cosine_fade_in(n: int) -> np.ndarray:
    if n <= 1:
        return np.ones(max(1, n), dtype=np.float32)
    t = np.linspace(0.0, np.pi, n, dtype=np.float32)
    return (0.5 * (1.0 - np.cos(t))).astype(np.float32)


def _cosine_fade_out(n: int) -> np.ndarray:
    if n <= 1:
        return np.zeros(max(1, n), dtype=np.float32)
    t = np.linspace(0.0, np.pi, n, dtype=np.float32)
    return (0.5 * (1.0 + np.cos(t))).astype(np.float32)


class AudioPlayer:
    """Потоковый PortAudio/WASAPI OutputStream без щелчков и зависаний (48 000 Гц / нативная частота устройства):
    - Использует стабильный буфер вывода 60 мс (исключает треск underrun при высокой нагрузке OCR/TTS/игры).
    - НИКОГДА не вызывает пользовательские колбэки, Win32 API и сигналы Qt внутри real-time колбэка PortAudio:
      уведомления о состоянии воспроизведения отправляются асинхронно через неблокирующую очередь.
    - Плавный косинусный fade-in (8 мс) и fade-out (20 мс) устраняют щелчки на стыках.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        device_name: Optional[str] = None,
        on_playback_state_changed: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device_name = device_name
        self._on_state_cb = on_playback_state_changed

        self._lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._stream: Optional[sd.OutputStream] = None
        self._stream_sr: int = sample_rate
        self._stream_channels: int = 2

        self._current: Optional[np.ndarray] = None
        self._pos: int = 0
        self._queue: collections.deque[np.ndarray] = collections.deque()

        self._fade_out_samples = max(1, int(round(self._stream_sr * 0.020)))
        self._fade_in_samples = max(1, int(round(self._stream_sr * 0.008)))
        self._fade_in_ramp = _cosine_fade_in(self._fade_in_samples)
        self._fading_out: Optional[np.ndarray] = None
        self._fade_out_ramp: Optional[np.ndarray] = None
        self._fade_pos: int = 0
        self._pending_after_fade: Optional[np.ndarray] = None
        self._is_playing: bool = False
        self._is_status_playing: bool = False
        self._ducking_active: bool = False
        self.underflow_count: int = 0

        # Диспетчер уведомлений в отдельном потоке, чтобы _audio_callback никогда не блокировался на Win32 / Qt / GIL
        self._notify_queue: queue.SimpleQueue[Optional[bool]] = queue.SimpleQueue()
        self._notifier_thread: Optional[threading.Thread] = None
        self._closed: bool = False
        if self._on_state_cb is not None:
            self._start_notifier()

    def _start_notifier(self) -> None:
        if self._notifier_thread is not None and self._notifier_thread.is_alive():
            return
        self._notifier_thread = threading.Thread(
            target=self._notifier_loop,
            name="AudioStateNotifier",
            daemon=True,
        )
        self._notifier_thread.start()

    def _notifier_loop(self) -> None:
        while True:
            item = self._notify_queue.get()
            if item is None:
                break
            cb = self._on_state_cb
            if cb is not None:
                try:
                    cb(bool(item))
                except Exception:
                    pass

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._is_playing

    def remaining_seconds(self) -> float:
        """Возвращает оставшуюся длительность воспроизведения диалогов (игнорируя прерываемые статусные анонсы)."""
        with self._lock:
            if self._is_status_playing and not self._queue:
                return 0.0
            rem_samples = 0
            if self._current is not None and self._pos < self._current.size and not self._is_status_playing:
                rem_samples += self._current.size - self._pos
            if self._pending_after_fade is not None:
                rem_samples += self._pending_after_fade.size
            for q in self._queue:
                rem_samples += q.size
            return rem_samples / float(max(1, self._stream_sr))

    def _notify_state_async(self, playing: bool) -> None:
        """Неблокирующая постановка уведомления в очередь для безопасного вызова из колбэка PortAudio."""
        if self._on_state_cb is not None and not self._closed:
            self._notify_queue.put(bool(playing))

    def open_stream(self) -> None:
        try:
            with self._stream_lock:
                if self._closed:
                    return
                self._ensure_stream()
        except Exception:
            pass

    def _open_for_device(self, dev_idx: Optional[int]) -> sd.OutputStream:
        if dev_idx is not None:
            info = sd.query_devices(dev_idx)
        else:
            def_out = sd.default.device[1]
            info = sd.query_devices(def_out) if def_out is not None and def_out >= 0 else None

        if info is not None:
            native_sr = int(round(float(info.get("default_samplerate", 48000.0))))
            native_ch = min(2, max(1, int(info.get("max_output_channels", 2))))
            high_lat = float(info.get("default_high_output_latency", 0.04) or 0.04)
        else:
            native_sr = 48000
            native_ch = 2
            high_lat = 0.04

        self._stream_sr = native_sr
        self._stream_channels = native_ch
        self._fade_out_samples = max(1, int(round(self._stream_sr * 0.020)))
        self._fade_in_samples = max(1, int(round(self._stream_sr * 0.008)))
        self._fade_in_ramp = _cosine_fade_in(self._fade_in_samples)

        # Стабильный буфер вывода >= 60 мс (вместо 3 мс 'low') исключает провалы WASAPI при параллельной работе OCR, Silero, DXGI и игры
        target_latency = max(0.06, min(0.12, high_lat))
        stream = sd.OutputStream(
            samplerate=native_sr,
            channels=native_ch,
            dtype="float32",
            device=dev_idx,
            latency=target_latency,
            callback=self._audio_callback,
        )
        stream.start()
        return stream

    def _ensure_stream(self) -> None:
        if self._stream is not None and self._stream.active:
            return
        self._close_stream()
        dev_idx = resolve_device_index(self.device_name)
        try:
            self._stream = self._open_for_device(dev_idx)
        except Exception:
            self._stream = self._open_for_device(None)

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def set_device(self, device_name: Optional[str]) -> None:
        with self._stream_lock:
            if device_name == self.device_name:
                return
            self.device_name = device_name
            was_open = self._stream is not None
            self._close_stream()
            if was_open and not self._closed:
                try:
                    self._ensure_stream()
                except Exception:
                    pass

    def _write_mono_to_out(self, outdata: np.ndarray, start: int, segment: np.ndarray) -> None:
        end = start + segment.size
        if outdata.shape[1] == 1:
            outdata[start:end, 0] = segment
        else:
            outdata[start:end, :] = segment[:, None]

    def _audio_callback(self, outdata: np.ndarray, frames: int, _time_info, status) -> None:
        if getattr(status, "output_underflow", False):
            self.underflow_count += 1
        outdata.fill(0.0)
        written = 0
        became_idle = False

        with self._lock:
            while written < frames:
                need = frames - written

                if self._fading_out is not None:
                    rem_fade = self._fading_out.size - self._fade_pos
                    take = min(need, rem_fade)
                    if take > 0:
                        chunk = self._fading_out[self._fade_pos : self._fade_pos + take]
                        ramp = self._fade_out_ramp[self._fade_pos : self._fade_pos + take]
                        self._write_mono_to_out(outdata, written, chunk * ramp)
                        self._fade_pos += take
                        written += take
                    if self._fade_pos >= self._fading_out.size:
                        self._fading_out = None
                        self._fade_out_ramp = None
                        self._fade_pos = 0
                        if self._pending_after_fade is not None:
                            self._current = self._pending_after_fade
                            self._pos = 0
                            self._pending_after_fade = None
                    continue

                if self._current is None or self._pos >= self._current.size:
                    if self._queue:
                        self._current = self._queue.popleft()
                        self._pos = 0
                    else:
                        self._current = None
                        self._pos = 0
                        if self._is_playing:
                            self._is_playing = False
                            self._is_status_playing = False
                            self._ducking_active = False
                            became_idle = True
                        break

                rem = self._current.size - self._pos
                take = min(need, rem)
                segment = self._current[self._pos : self._pos + take]
                if self._pos < self._fade_in_samples:
                    fi_n = min(take, self._fade_in_samples - self._pos)
                    ramp = self._fade_in_ramp[self._pos : self._pos + fi_n]
                    seg_copy = segment.copy()
                    seg_copy[:fi_n] *= ramp
                    self._write_mono_to_out(outdata, written, seg_copy)
                else:
                    self._write_mono_to_out(outdata, written, segment)

                self._pos += take
                written += take
                self._is_playing = True

        if became_idle:
            self._notify_state_async(False)

    def play(
        self,
        audio: np.ndarray,
        interrupt: bool = False,
        trigger_ducking: bool = True,
        is_status: bool = False,
        interrupt_only_if_status: bool = False,
    ) -> None:
        if self._closed:
            return
        raw = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        if raw.size == 0:
            return
        with self._stream_lock:
            if self._closed:
                return
            self._ensure_stream()
        buf = _resample_to_rate(raw, self.sample_rate, self._stream_sr)
        with self._lock:
            effective_interrupt = interrupt or (
                interrupt_only_if_status and self._is_status_playing and not self._queue
            )
            if effective_interrupt:
                self._queue.clear()
                if self._current is not None and self._pos < self._current.size:
                    tail = self._current[self._pos : self._pos + self._fade_out_samples].copy()
                    self._fading_out = tail
                    self._fade_out_ramp = _cosine_fade_out(tail.size)
                    self._fade_pos = 0
                    self._current = None
                    self._pos = 0
                    self._pending_after_fade = buf
                else:
                    self._fading_out = None
                    self._fade_out_ramp = None
                    self._pending_after_fade = None
                    self._current = buf
                    self._pos = 0
                self._is_status_playing = is_status
            else:
                if self._current is None and self._fading_out is None:
                    self._current = buf
                    self._pos = 0
                    self._is_status_playing = is_status
                else:
                    self._queue.append(buf)
                    if not is_status:
                        self._is_status_playing = False
            self._is_playing = True
            should_notify_start = bool(trigger_ducking)
            if should_notify_start:
                self._ducking_active = True

        if should_notify_start:
            self._notify_state_async(True)

    def enqueue_chunk(self, audio: np.ndarray) -> None:
        self.play(audio, interrupt=False, trigger_ducking=True)

    def stop_immediately(self) -> None:
        became_idle = False
        with self._lock:
            self._queue.clear()
            self._pending_after_fade = None
            if self._current is not None and self._pos < self._current.size:
                tail = self._current[self._pos : self._pos + self._fade_out_samples].copy()
                self._fading_out = tail
                self._fade_out_ramp = _cosine_fade_out(tail.size)
                self._fade_pos = 0
                self._current = None
                self._pos = 0
            else:
                self._current = None
                self._pos = 0
                if self._is_playing:
                    self._is_playing = False
                    self._is_status_playing = False
                    self._ducking_active = False
                    became_idle = True
        if became_idle:
            self._notify_state_async(False)

    def close(self) -> None:
        self._closed = True
        self.stop_immediately()
        # _stream_lock отделён от _lock, чтобы stream.stop() не вызывал AB-BA deadlock с _audio_callback
        with self._stream_lock:
            self._close_stream()
        if self._notifier_thread is not None and self._notifier_thread.is_alive():
            self._notify_queue.put(None)
            self._notifier_thread.join(timeout=1.0)
            self._notifier_thread = None
