from __future__ import annotations

import collections
import gc
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal

from src.audio_dsp import PRESET_RATE_VALUES, compute_adaptive_speed, process_audio
from src.audio_player import AudioPlayer
from src.capture import (
    CachedRoiResolver,
    ChangeDetector,
    ScreenCapturer,
    is_target_window_active,
    set_current_thread_below_normal,
)
from src.config import AppConfig
from src.debug_logger import DebugCaptureLogger
from src.ducking import AutoDucker
from src.ocr_engine import OcrEngineManager
from src.punctuation import PunctuationRestorer
from src.text_filter import StressDictManager, SubtitleTextFilter, normalize_text_base, parse_subtitle_utterances
from src.tts_engine import SileroTTSEngine
from src.voice_manager import VoiceManager, speaker_display_name

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def split_long_subtitle(text: str, max_len: int = 140) -> list[str]:
    """Сохраняет цельные естественные интонационные дуги Silero v5_6_ru:
    Разбивает только аномально длинные субтитры (> 140 симв.) строго по границам предложений (. / ? / ! / …).
    """
    s = text.strip()
    if len(s) <= max_len:
        return [s] if s else []

    raw_parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(s) if p.strip()]
    if len(raw_parts) <= 1:
        return [s]

    merged: list[str] = []
    for part in raw_parts:
        if merged and (len(merged[-1]) + 1 + len(part)) <= max_len:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged or [s]


@dataclass
class PipelineLogEvent:
    timestamp: str
    status: str  # «spoken» | «filtered» | «info» | «error»
    speaker: str
    voice: str
    latency_ms: float
    message: str


@dataclass
class QueuedUtterance:
    speaker_name: Optional[str]
    raw_speech: str
    t_stable: float
    ocr_ms: float
    batch_index: int
    batch_total: int
    raw_utterance: str = ""
    ocr_engine: str = ""
    ocr_rejected: int = 0
    ocr_lines_meta: Optional[list[dict]] = None
    debug_frame: Optional[np.ndarray] = None
    debug_frame_id: int = 0


class SubtitlePipeline(QObject):
    """3-поточный непропускающий FIFO-конвейер с нулевой потерей субтитров и без просадки FPS игры:
    Этап 1 (_capture_loop, BELOW_NORMAL): быстрый захват DXGI + кэшированный ROI + детектор изменений -> очередь кадров
    Этап 2 (_ocr_loop, BELOW_NORMAL):     прогретый 2-поточный RapidOCR -> дедупликация TextFilter -> очередь _tts_queue
    Этап 3 (_tts_loop, BELOW_NORMAL):     последовательный 2-поточный Silero v5.6 @ 48 кГц + автоускорение -> WASAPI + Auto-Ducking
    """

    status_changed = Signal(str, str)  # (код_состояния, текст_для_UI)
    loading_progress = Signal(str)
    log_event = Signal(object)  # Событие PipelineLogEvent
    last_phrase_changed = Signal(str, str)  # (имя_спикера, чистая_фраза)
    character_updated = Signal(str)
    model_ready = Signal(list, float)
    error_occurred = Signal(str)

    def __init__(
        self,
        config: AppConfig,
        voice_manager: VoiceManager,
        stress_dict: Optional[StressDictManager] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.voice_manager = voice_manager
        self.stress_dict = stress_dict or StressDictManager()

        duck_targets = [config.target_process] if config.target_process else list(config.ducking_processes)
        self.ducker = AutoDucker(
            enabled=config.ducking_enabled,
            depth_pct=config.ducking_depth_pct,
            target_processes=duck_targets,
            on_diagnostic=lambda msg: self._emit_log("info", "—", "Ducking", 0.0, msg),
            mute_window=bool(getattr(config, "mute_target_window", False)),
        )
        self.tts = SileroTTSEngine(
            model_path=config.silero_model_path,
            num_threads=config.torch_num_threads,
            sample_rate=config.sample_rate,
            stress_dict=self.stress_dict,
        )
        # Ограничиваем RapidOCR 2 потоками, чтобы он не отнимал процессорные кадры у игры
        ocr_threads = max(1, min(2, int(config.torch_num_threads)))
        self.ocr = OcrEngineManager(
            engine_name=config.ocr_engine,
            num_threads=ocr_threads,
            font_classifier_enabled=bool(getattr(config, "font_classifier_enabled", True)),
        )
        self.text_filter = SubtitleTextFilter(
            caps_ratio_threshold=config.caps_ratio_threshold,
            dedup_threshold=config.dedup_threshold,
        )
        self.punctuator = PunctuationRestorer(enabled=config.enable_punctuation)
        self.player = AudioPlayer(
            sample_rate=config.sample_rate,
            device_name=config.audio_device,
            on_playback_state_changed=self._on_audio_playback_state,
        )
        self.change_detector = ChangeDetector(
            bright_threshold=44,
            diff_threshold=0.0010,
            min_text_coverage=0.0002,
        )
        self._roi_resolver = CachedRoiResolver(ttl_sec=0.5)
        self.debug_logger = DebugCaptureLogger(enabled=config.debug_capture_log)
        self._debug_frame_seq = 0
        self._roi_selecting = False
        self._own_hwnds: tuple[int, ...] = ()
        self._shutting_down = False

        self._running = False
        self._stop_event = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._ocr_thread: Optional[threading.Thread] = None
        self._tts_thread: Optional[threading.Thread] = None

        # Развязанные FIFO-очереди: OCR не тормозит на TTS, а TTS никогда не теряет субтитры из очереди
        self._ocr_frames_lock = threading.Lock()
        self._ocr_frames: collections.deque[tuple[np.ndarray, float]] = collections.deque(maxlen=6)
        self._frame_ready_event = threading.Event()

        self._tts_queue: queue.Queue[Optional[QueuedUtterance]] = queue.Queue()
        self._generation = 0
        self._ocr_warmed = False
        self._recent_overlaps: collections.deque[float] = collections.deque(maxlen=8)
        self._status_audio_cache: dict[str, np.ndarray] = {}

        # Лёгкая потокобезопасная история последних реплик для быстрого редактирования со смартфона по Wi-Fi
        self._mobile_history_lock = threading.Lock()
        self._mobile_event_seq: int = 0
        self._recent_mobile_history: collections.deque[dict] = collections.deque(maxlen=30)

    def _record_mobile_utterance(
        self,
        *,
        speaker_name: str,
        gender: str,
        voice: str,
        raw_text: str,
        tts_text: str,
    ) -> None:
        """Сохраняет недавно озвученную фразу для отображения в мобильном веб-пульте."""
        with self._mobile_history_lock:
            self._mobile_event_seq += 1
            entry = {
                "id": self._mobile_event_seq,
                "ts": time.strftime("%H:%M:%S"),
                "speaker": speaker_name,
                "gender": gender if gender in ("male", "female") else "male",
                "voice": voice,
                "voice_label": speaker_display_name(voice, short=True),
                "raw_text": raw_text,
                "tts_text": tts_text,
            }
            self._recent_mobile_history.appendleft(entry)

    def get_recent_utterances(self, limit: int = 20) -> list[dict]:
        """Возвращает последние озвученные реплики (новейшие первыми)."""
        with self._mobile_history_lock:
            items = list(self._recent_mobile_history)
        return items[: max(1, int(limit))]

    @property
    def is_running(self) -> bool:
        return self._running

    def set_own_hwnds(self, hwnds: tuple[int, ...] | list[int]) -> None:
        """Регистрирует нативные HWND окон GTA6SubVoice, чтобы они НИКОГДА не попадали в захват OCR."""
        self._own_hwnds = tuple(int(h) for h in hwnds if h)

    def set_roi_selecting(self, active: bool) -> None:
        """Ставит захват на паузу, пока оверлей выбора области (F9) открыт, чтобы его вуаль не читалась в OCR."""
        self._roi_selecting = bool(active)
        self.change_detector.reset()
        self._roi_resolver.invalidate()

    def _is_target_active_for_reading(self) -> bool:
        if not self.config.only_when_target_active or not self.config.target_process:
            return True
        return is_target_window_active(self.config.target_process)

    def _on_audio_playback_state(self, is_playing: bool) -> None:
        if is_playing:
            self.ducker.on_speech_start()
        else:
            self.ducker.on_speech_end()
            if self._running and self._tts_queue.empty():
                if self._is_target_active_for_reading():
                    self.status_changed.emit("waiting", "Слушаю")
                else:
                    self.status_changed.emit("paused_window", "Нет фокуса")

    def _emit_log(
        self,
        status: str,
        speaker: str,
        voice: str,
        latency_ms: float,
        message: str,
    ) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_event.emit(
            PipelineLogEvent(
                timestamp=ts,
                status=status,
                speaker=speaker,
                voice=voice,
                latency_ms=latency_ms,
                message=message,
            )
        )

    def _prebuild_status_announcements(self) -> None:
        phrases = {
            "started": "Чтение субтитров запущено.",
            "stopped": "Чтение субтитров остановлено.",
        }
        for key, text in phrases.items():
            try:
                raw, _ = self.tts.synthesize(text=text, speaker="eugene", sample_rate=self.config.sample_rate)
                if raw.size > 0:
                    proc, _ = process_audio(
                        raw,
                        sample_rate=self.config.sample_rate,
                        volume=1.0,
                        trim=True,
                    )
                    self._status_audio_cache[key] = proc
            except Exception:
                pass

    def announce_status(self, key: str) -> None:
        if not self.config.announce_status:
            return
        buf = self._status_audio_cache.get(key)
        if buf is None or buf.size == 0:
            return
        vol = max(0.0, min(2.0, self.config.volume / 100.0))
        scaled = (buf * vol).astype(np.float32, copy=True) if abs(vol - 1.0) > 1e-3 else buf.copy()
        if float(np.max(np.abs(scaled))) > 1.0:
            scaled = np.clip(scaled, -1.0, 1.0).astype(np.float32, copy=False)
        self.player.play(scaled, interrupt=True, trigger_ducking=False, is_status=True)

    def load_models_async(self) -> None:
        self.status_changed.emit("loading", "Загрузка Silero 5.6 и OCR...")
        self.loading_progress.emit("1/3  Подготовка аудиопотока WASAPI и PyTorch CPU...")

        def _loader() -> None:
            set_current_thread_below_normal()
            try:
                self.player.open_stream()
                self.loading_progress.emit("2/3  Загрузка нейронной модели Silero TTS v5.6 и прогрев диктора...")
                self.tts.load(
                    model_path=self.config.silero_model_path or None,
                    num_threads=self.config.torch_num_threads,
                )
                self.voice_manager.set_available_speakers(self.tts.speakers)

                self.loading_progress.emit("3/3  Прогрев RapidOCR и подготовка голосовых оповещений...")
                self._prebuild_status_announcements()
                try:
                    self.ocr.warmup()
                    self._ocr_warmed = True
                except Exception:
                    pass

                self.change_detector.reset()
                self.model_ready.emit(self.tts.speakers, self.tts.load_time_ms)
                self._emit_log(
                    "info",
                    "—",
                    ", ".join(self.tts.speakers),
                    self.tts.load_time_ms,
                    f"Silero 5.6 + OCR готовы (warmup {self.tts.warmup_time_ms:.0f}мс)",
                )
                if not self._running:
                    self.status_changed.emit("idle", "Готов")
            except Exception as exc:
                self.status_changed.emit("error", "Модель не готова")
                self.error_occurred.emit(str(exc))
                self._emit_log("error", "—", "—", 0.0, f"Silero: {exc}")

        threading.Thread(target=_loader, name="SileroLoader", daemon=True).start()

    def apply_config(self, cfg: AppConfig) -> None:
        self.config = cfg
        self.ocr.set_engine(cfg.ocr_engine)
        self.ocr.set_font_classifier_enabled(bool(getattr(cfg, "font_classifier_enabled", True)))
        self.text_filter.caps_ratio_threshold = cfg.caps_ratio_threshold
        self.text_filter.dedup_threshold = cfg.dedup_threshold
        self.punctuator.enabled = cfg.enable_punctuation
        self._roi_resolver.invalidate()
        self.player.set_device(cfg.audio_device)
        duck_targets = [cfg.target_process] if cfg.target_process else []
        self.ducker.configure(
            enabled=cfg.ducking_enabled,
            depth_pct=cfg.ducking_depth_pct,
            target_processes=duck_targets,
            mute_window=bool(getattr(cfg, "mute_target_window", False)),
        )
        self.debug_logger.set_enabled(cfg.debug_capture_log)

    def _drain_queues(self) -> None:
        with self._ocr_frames_lock:
            self._ocr_frames.clear()
        while True:
            try:
                self._tts_queue.get_nowait()
            except queue.Empty:
                break

    def start(self) -> None:
        if self._running:
            return
        if not (self.config.target_process or "").strip():
            self.status_changed.emit("idle", "Выберите процесс")
            self._emit_log("info", "—", "—", 0.0, "Процесс игры / окна не выбран — запуск отменён")
            return

        self.player.open_stream()
        if not self.tts.is_loaded:
            self.load_models_async()

        self._stop_event.clear()
        self._drain_queues()
        self._running = True
        self._recent_overlaps.clear()
        self.change_detector.reset()
        self.text_filter.reset_dedup()
        self._roi_resolver.invalidate()
        self.debug_logger.set_enabled(self.config.debug_capture_log)
        if self.config.debug_capture_log:
            self.debug_logger.start_new_session()
        self.ducker.on_reading_start()

        self._tts_thread = threading.Thread(target=self._tts_loop, name="TtsWorker", daemon=True)
        self._tts_thread.start()

        self._ocr_thread = threading.Thread(target=self._ocr_loop, name="OcrWorker", daemon=True)
        self._ocr_thread.start()

        self._capture_thread = threading.Thread(target=self._capture_loop, name="CaptureLoop", daemon=True)
        self._capture_thread.start()

        self.status_changed.emit("waiting", "Слушаю")
        self._emit_log("info", "—", "—", 0.0, "Чтение субтитров запущено (очередь без пропусков)")
        self.announce_status("started")

    def stop(self, announce: bool = True) -> None:
        if not self._running:
            self.ducker.restore_immediately()
            return
        self._running = False
        self._generation += 1
        self._stop_event.set()
        self._frame_ready_event.set()
        self._drain_queues()
        self._tts_queue.put(None)  # Сразу пробуждаем поток TTS
        self.player.stop_immediately()
        self.ducker.restore_immediately()

        for attr, timeout in (
            ("_capture_thread", 1.0),
            ("_ocr_thread", 1.5),
            ("_tts_thread", 1.5),
        ):
            th = getattr(self, attr)
            if th is not None:
                th.join(timeout=timeout)
                setattr(self, attr, None)

        self.status_changed.emit("idle", "Остановлено")
        self._emit_log("info", "—", "—", 0.0, "Чтение субтитров остановлено")
        if announce:
            self.announce_status("stopped")
        gc.collect(0)

    def toggle_start_stop(self) -> bool:
        if self._running:
            self.stop(announce=True)
            return False
        self.start()
        return True

    def interrupt_speech(self) -> None:
        self._generation += 1
        self._drain_queues()
        self.player.stop_immediately()
        self.ducker.restore_speech_ducking()
        self._emit_log("info", "—", "—", 0.0, "Очередь и фраза прерваны (F10)")

    def speak_test_async(
        self,
        text: str,
        speaker: str = "eugene",
        label: str = "Тест",
    ) -> None:
        """Синтезирует и воспроизводит образец чистого нативного голоса Silero без обработки аудио."""

        def _run() -> None:
            set_current_thread_below_normal()
            try:
                self.player.open_stream()
                if not self.tts.is_loaded:
                    self.tts.load(
                        model_path=self.config.silero_model_path or None,
                        num_threads=self.config.torch_num_threads,
                    )
                t0 = time.perf_counter()
                final_speed, boost_steps = compute_adaptive_speed(
                    base_speed=self.config.speech_speed,
                    text_len=len(text),
                    adaptive_tempo=self.config.adaptive_tempo,
                )
                raw_audio, tts_ms = self.tts.synthesize(
                    text=text,
                    speaker=speaker,
                    sample_rate=self.config.sample_rate,
                    speed=final_speed,
                )
                if raw_audio.size == 0:
                    return
                processed, _dsp_ms = process_audio(
                    raw_audio,
                    sample_rate=self.config.sample_rate,
                    volume=self.config.volume / 100.0,
                    trim=True,
                )
                total_ms = (time.perf_counter() - t0) * 1000.0
                self.ducker.on_speech_start()
                self.player.play(processed, interrupt=True, trigger_ducking=True, is_status=False)
                self.last_phrase_changed.emit(label, text)
                boost_str = f" (+{boost_steps}ст.)" if boost_steps > 0 else ""
                rate_val = PRESET_RATE_VALUES.get(final_speed, 1.0)
                self._emit_log(
                    "spoken",
                    label,
                    speaker_display_name(speaker, short=True),
                    total_ms,
                    f"TTS {tts_ms:.0f}мс | {final_speed} ({rate_val:.1f}x){boost_str} | {text[:45]}",
                )
            except Exception as exc:
                self.error_occurred.emit(str(exc))
                self._emit_log("error", label, speaker, 0.0, f"Ошибка теста: {exc}")

        threading.Thread(target=_run, name="SpeakTest", daemon=True).start()

    # ------------------------------------------------------------------ Этап 1: Захват (без вреда для FPS игры)
    def _push_frame_for_ocr(self, frame: np.ndarray, t_stable: float) -> None:
        with self._ocr_frames_lock:
            self._ocr_frames.append((frame, t_stable))
        self._frame_ready_event.set()

    def _capture_loop(self) -> None:
        set_current_thread_below_normal()
        capturer = ScreenCapturer()
        last_submit_ts = 0.0
        first_grab = True
        was_state: Optional[str] = None
        try:
            self._emit_log("info", "—", capturer.backend_name.upper(), 0.0, f"Захват: {capturer.backend_name.upper()}")
            while not self._stop_event.is_set():
                interval = 1.0 / max(2, self.config.scan_fps)
                t_loop = time.perf_counter()

                if self._roi_selecting:
                    first_grab = True
                    self.change_detector.reset()
                    self._stop_event.wait(0.08)
                    continue

                target_proc = (self.config.target_process or "").strip()

                # 1. Строго находим окно выбранного процесса — НИКОГДА не откатываемся к чтению всего рабочего стола!
                resolved = (
                    self._roi_resolver.resolve_for_capture(self.config.roi, target_process=target_proc)
                    if target_proc
                    else None
                )
                if resolved is None:
                    if was_state != "missing":
                        was_state = "missing"
                        self._generation += 1
                        self._drain_queues()
                        self.change_detector.reset()
                        if self.player.is_playing and not self.player._is_status_playing:
                            self.player.stop_immediately()
                        self.ducker.restore_immediately()
                        self.status_changed.emit("paused_window", "Окно не найдено")
                    self._stop_event.wait(max(0.12, interval))
                    continue

                # 2. Не сканируем и не озвучиваем субтитры, пока окно выбранного процесса не активно (если опция включена)
                if not self._is_target_active_for_reading():
                    if was_state != "inactive":
                        was_state = "inactive"
                        self._generation += 1
                        self._drain_queues()
                        self.change_detector.reset()
                        if self.player.is_playing and not self.player._is_status_playing:
                            self.player.stop_immediately()
                        self.ducker.restore_speech_ducking()
                        self.status_changed.emit("paused_window", "Нет фокуса")
                    self._stop_event.wait(max(0.08, interval))
                    continue

                if was_state != "active":
                    was_state = "active"
                    first_grab = True
                    self.change_detector.reset()
                    if self._running:
                        self.ducker.on_reading_start()
                    self.status_changed.emit("waiting", "Слушаю")

                target_hwnd, rect, win_rect = resolved
                frame = capturer.grab_target_window_roi(
                    target_hwnd,
                    win_rect,
                    rect,
                    target_process=target_proc,
                    force_initial=first_grab,
                    own_hwnds=self._own_hwnds,
                )

                if frame is not None and frame.size > 0:
                    changed, _diff, cov = self.change_detector.has_changed(frame)
                    now = time.perf_counter()
                    # Периодическое контрольное пересканирование каждые 0.35 с, пока в кадре есть пиксели субтитров
                    periodic_due = (cov >= self.change_detector.min_text_coverage) and ((now - last_submit_ts) >= 0.35)

                    if first_grab or changed or periodic_due:
                        if changed and not first_grab:
                            debounce_sec = max(0.0, min(0.12, self.config.debounce_ms / 1000.0))
                            if debounce_sec > 0:
                                if self._stop_event.wait(debounce_sec):
                                    break
                                if not self._is_target_active_for_reading():
                                    self.change_detector.reset()
                                    continue
                                fresh_resolved = self._roi_resolver.resolve_for_capture(
                                    self.config.roi, target_process=target_proc
                                )
                                if fresh_resolved is None:
                                    self.change_detector.reset()
                                    continue
                                f_hwnd, f_rect, f_win_rect = fresh_resolved
                                fresh = capturer.grab_target_window_roi(
                                    f_hwnd,
                                    f_win_rect,
                                    f_rect,
                                    target_process=target_proc,
                                    force_initial=False,
                                    own_hwnds=self._own_hwnds,
                                )
                                if fresh is not None and fresh.size > 0:
                                    frame = fresh
                                    self.change_detector.has_changed(frame)

                        t_stable = time.perf_counter()
                        last_submit_ts = t_stable
                        first_grab = False
                        self._push_frame_for_ocr(frame, t_stable)
                    else:
                        first_grab = False

                spent = time.perf_counter() - t_loop
                sleep_for = max(0.008, interval - spent)
                self._stop_event.wait(sleep_for)
        finally:
            capturer.close()

    # ------------------------------------------------------------------ Этап 2: Быстрый OCR -> Непропускающая FIFO-очередь
    def _ocr_loop(self) -> None:
        set_current_thread_below_normal()
        if not self._ocr_warmed:
            try:
                self.ocr.warmup()
                self._ocr_warmed = True
            except Exception:
                pass

        while not self._stop_event.is_set():
            self._frame_ready_event.wait(timeout=0.25)
            if self._stop_event.is_set():
                break
            if not self.tts.is_loaded:
                self._stop_event.wait(0.08)
                continue

            while not self._stop_event.is_set() and self.tts.is_loaded:
                with self._ocr_frames_lock:
                    if not self._ocr_frames:
                        self._frame_ready_event.clear()
                        break
                    frame, t_stable = self._ocr_frames.popleft()

                self._process_ocr_frame(frame, t_stable)
                frame = None

    def _process_ocr_frame(self, frame: np.ndarray, t_stable: float) -> None:
        if not self.tts.is_loaded or self._stop_event.is_set():
            return

        try:
            ocr_res = self.ocr.recognize(frame)
        except Exception as exc:
            self._emit_log("error", "—", "OCR", 0.0, f"Ошибка OCR: {exc}")
            return

        if not ocr_res.lines:
            return

        parsed = parse_subtitle_utterances([ln.text for ln in ocr_res.lines])
        if not parsed:
            return

        accepted_batch: list[tuple[Optional[str], str, str]] = []
        for _spk_hint, _speech_hint, raw_line in parsed:
            # Сразу фиксируем фразу в дедупликации при постановке в очередь, чтобы соседние кадры не дублировали и не теряли строки
            filt = self.text_filter.filter(raw_line, update_dedup=True)
            if filt.accepted:
                accepted_batch.append((filt.speaker_name, filt.speech_text, raw_line))
            elif filt.reason != "empty" and not filt.reason.startswith("duplicate"):
                self._emit_log(
                    "filtered",
                    "—",
                    "—",
                    ocr_res.elapsed_ms,
                    f"Пропуск [{filt.reason}]: {filt.cleaned_text[:48]}",
                )

        if not accepted_batch:
            return

        # Сохраняем исходный ROI-кадр и метаданные OCR только при включённом логе отладки
        dbg_on = self.debug_logger.enabled
        dbg_frame = frame.copy() if dbg_on else None
        dbg_frame_id = 0
        dbg_lines_meta: Optional[list[dict]] = None
        if dbg_on:
            self._debug_frame_seq += 1
            dbg_frame_id = self._debug_frame_seq
            dbg_lines_meta = [
                {
                    "text": ln.text,
                    "bbox": list(ln.bbox),
                    "conf": round(float(ln.confidence), 3),
                }
                for ln in ocr_res.lines
            ]

        total = len(accepted_batch)
        for idx, (speaker_name, raw_speech, raw_utt) in enumerate(accepted_batch):
            self._tts_queue.put(
                QueuedUtterance(
                    speaker_name=speaker_name,
                    raw_speech=raw_speech,
                    t_stable=t_stable,
                    ocr_ms=ocr_res.elapsed_ms,
                    batch_index=idx,
                    batch_total=total,
                    raw_utterance=raw_utt if dbg_on else "",
                    ocr_engine=ocr_res.engine if dbg_on else "",
                    ocr_rejected=ocr_res.rejected_boxes if dbg_on else 0,
                    ocr_lines_meta=dbg_lines_meta,
                    debug_frame=dbg_frame,
                    debug_frame_id=dbg_frame_id,
                )
            )

    # ------------------------------------------------------------------ Этап 3: Последовательный нейро-TTS
    def _measure_catchup_pressure(self, queued_waiting: int) -> tuple[float, int]:
        """Оценивает запас очереди и воспроизведения, чтобы опциональное автоускорение поднимало темп до 'fast' при отставании."""
        now = time.monotonic()
        while self._recent_overlaps and (now - self._recent_overlaps[0]) > 6.5:
            self._recent_overlaps.popleft()

        audio_rem = self.player.remaining_seconds()
        if queued_waiting == 0 and audio_rem < 0.25:
            self._recent_overlaps.clear()
            return 0.0, 0

        effective_backlog = audio_rem + (queued_waiting * 1.35)
        if effective_backlog >= 0.45 or queued_waiting > 0:
            self._recent_overlaps.append(now)

        return effective_backlog, len(self._recent_overlaps)

    def _tts_loop(self) -> None:
        set_current_thread_below_normal()
        inter_speaker_gap = np.zeros(int(round(self.config.sample_rate * 0.13)), dtype=np.float32)

        while not self._stop_event.is_set():
            try:
                utt = self._tts_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if utt is None or self._stop_event.is_set():
                continue

            gen_at_start = self._generation
            self._speak_queued_utterance(utt, gen_at_start, inter_speaker_gap)

    def _speak_queued_utterance(
        self,
        utt: QueuedUtterance,
        gen_at_start: int,
        inter_speaker_gap: np.ndarray,
    ) -> None:
        if (
            self._stop_event.is_set()
            or gen_at_start != self._generation
            or not self.tts.is_loaded
            or not self._is_target_active_for_reading()
        ):
            return

        speech_text = self.punctuator.restore(utt.raw_speech)
        speaker_display = utt.speaker_name or "Диктор"

        speaker_id, detected_gender, created = self.voice_manager.resolve_speaker(
            speaker_name=utt.speaker_name,
            voice_mode=self.config.voice_mode,
            single_speaker=self.config.single_speaker,
            dual_male_speaker=self.config.dual_male_speaker,
            dual_female_speaker=self.config.dual_female_speaker,
        )
        if created and utt.speaker_name:
            self.character_updated.emit(utt.speaker_name)

        waiting_in_queue = self._tts_queue.qsize()
        backlog_sec, recent_burst = self._measure_catchup_pressure(waiting_in_queue)

        # Официальные пресеты нейронного темпа Silero: medium (1.0x) | fast (1.2x) | x-fast (1.5x)
        final_speed, boost = compute_adaptive_speed(
            base_speed=self.config.speech_speed,
            text_len=len(speech_text),
            adaptive_tempo=self.config.adaptive_tempo,
            backlog_sec=backlog_sec,
            recent_burst=recent_burst,
        )

        chunks = split_long_subtitle(speech_text, max_len=140)
        if not chunks:
            return

        spoken_first = False
        first_e2e_ms = 0.0
        first_tts_ms = 0.0
        first_dsp_ms = 0.0
        first_mode = "text"
        sent_payloads: list[str] = []

        for chunk_idx, chunk_text in enumerate(chunks):
            if self._stop_event.is_set() or gen_at_start != self._generation:
                break

            try:
                if hasattr(self.tts, "last_synthesis_meta"):
                    self.tts.last_synthesis_meta = {}
                raw_audio, tts_ms = self.tts.synthesize(
                    text=chunk_text,
                    speaker=speaker_id,
                    sample_rate=self.config.sample_rate,
                    speed=final_speed,
                )
            except Exception as exc:
                self._emit_log("error", speaker_display, speaker_id, 0.0, f"Ошибка TTS: {exc}")
                break

            if raw_audio.size == 0 or self._stop_event.is_set() or gen_at_start != self._generation:
                break

            processed, dsp_ms = process_audio(
                raw_audio,
                sample_rate=self.config.sample_rate,
                volume=self.config.volume / 100.0,
                trim=True,
            )
            if processed.size == 0 or self._stop_event.is_set() or gen_at_start != self._generation or not self._is_target_active_for_reading():
                break

            tts_meta = dict(getattr(self.tts, "last_synthesis_meta", {}) or {})
            chunk_payload = str(tts_meta.get("tts_stressed") or chunk_text)
            chunk_mode = str(tts_meta.get("tts_mode") or ("ssml" if final_speed != "medium" else "text"))

            if chunk_idx == 0:
                # Вставляем естественную паузу 130 мс между последовательными репликами в одном атомарном play()
                to_play = processed
                if self.player.is_playing and not self.player._is_status_playing:
                    to_play = np.concatenate([inter_speaker_gap, processed])
                # Запускаем приглушение целевого процесса сразу перед отправкой звука в WASAPI
                self.ducker.on_speech_start()
                self.player.play(
                    to_play,
                    interrupt=False,
                    trigger_ducking=True,
                    is_status=False,
                    interrupt_only_if_status=True,
                )
                self.status_changed.emit("speaking", "Озвучиваю")
                self.last_phrase_changed.emit(speaker_display, speech_text)

                spoken_first = True
                first_e2e_ms = (time.perf_counter() - utt.t_stable) * 1000.0
                first_tts_ms = tts_ms
                first_dsp_ms = dsp_ms
                first_mode = chunk_mode
                sent_payloads.append(chunk_payload)

                voice_label = speaker_display_name(speaker_id, short=True)
                boost_tag = f" (+{boost}ст.)" if boost > 0 else ""
                queue_tag = f" [Q:{waiting_in_queue}]" if waiting_in_queue > 0 else ""
                multi_tag = f" [{utt.batch_index + 1}/{utt.batch_total}]" if utt.batch_total > 1 else ""
                rate_val = PRESET_RATE_VALUES.get(final_speed, 1.0)
                dbg_tts_tag = f" | TTS: {chunk_payload}" if self.debug_logger.enabled else ""
                self._emit_log(
                    "spoken",
                    speaker_display,
                    voice_label,
                    first_e2e_ms,
                    f"[OCR {utt.ocr_ms:.0f} | TTS {tts_ms:.0f} | {final_speed} ({rate_val:.1f}x){boost_tag}]{multi_tag}{queue_tag} {speech_text[:50]}{dbg_tts_tag}",
                )
            else:
                self.player.enqueue_chunk(processed)
                sent_payloads.append(chunk_payload)

        if spoken_first:
            self._record_mobile_utterance(
                speaker_name=utt.speaker_name or "",
                gender=detected_gender,
                voice=speaker_id,
                raw_text=speech_text,
                tts_text=" || ".join(sent_payloads) if sent_payloads else speech_text,
            )

        if spoken_first and self.debug_logger.enabled:
            self.debug_logger.record_spoken(
                frame=utt.debug_frame,
                frame_id=utt.debug_frame_id,
                ocr_engine=utt.ocr_engine or self.config.ocr_engine,
                ocr_ms=utt.ocr_ms,
                ocr_rejected=utt.ocr_rejected,
                ocr_lines=utt.ocr_lines_meta,
                raw_utterance=utt.raw_utterance or utt.raw_speech,
                speaker=utt.speaker_name,
                voice=speaker_id,
                speech_filtered=utt.raw_speech,
                speech_punctuated=speech_text,
                tts_input=normalize_text_base(speech_text, stress_dict=self.stress_dict),
                tts_stressed=" || ".join(sent_payloads),
                tts_mode=first_mode,
                speed=final_speed,
                boost=boost,
                tts_ms=first_tts_ms,
                dsp_ms=first_dsp_ms,
            )
        utt.debug_frame = None

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self.stop(announce=False)
        finally:
            self.ducker.close()
            self.player.close()
            self.ocr.close()
