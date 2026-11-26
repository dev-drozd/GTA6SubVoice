from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from src.paths import DATA_DIR

MAX_SESSIONS_TO_KEEP = 5
MAX_FRAMES_PER_SESSION = 500


class DebugCaptureLogger:
    """Опциональный диагностический логгер для отладки OCR и фильтрации текста.
    - Когда выключен (по умолчанию): нулевые записи на диск, нулевые аллокации и нагрузка на CPU.
    - Когда включён: сохраняет компактный JPEG-кадр и полную запись JSONL/TXT ТОЛЬКО для субтитров,
      реально дошедших до озвучки TTS (с дедупликацией по кадру, чтобы многострочный кадр сохранял 1 JPEG).
    - Автоматически ротирует старые сессии (хранит 5 последних) и ограничивает кадры (до 500 на сессию).
    """

    def __init__(
        self,
        enabled: bool = False,
        base_dir: Optional[Path] = None,
        max_sessions: int = MAX_SESSIONS_TO_KEEP,
        max_frames: int = MAX_FRAMES_PER_SESSION,
    ) -> None:
        self.enabled = bool(enabled)
        self.base_dir = base_dir or (DATA_DIR / "debug_logs")
        self.max_sessions = max(1, int(max_sessions))
        self.max_frames = max(10, int(max_frames))

        self._lock = threading.Lock()
        self._session_dir: Optional[Path] = None
        self._frames_dir: Optional[Path] = None
        self._events_path: Optional[Path] = None
        self._index_path: Optional[Path] = None
        self._event_seq: int = 0
        self._frame_seq: int = 0
        self._saved_frame_paths: dict[int, str] = {}

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            if not self.enabled:
                self._saved_frame_paths.clear()

    def start_new_session(self) -> None:
        with self._lock:
            self._session_dir = None
            self._frames_dir = None
            self._events_path = None
            self._index_path = None
            self._event_seq = 0
            self._frame_seq = 0
            self._saved_frame_paths.clear()

    def ensure_root_dir(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        return self.base_dir

    @property
    def current_session_dir(self) -> Optional[Path]:
        with self._lock:
            return self._session_dir

    def _ensure_session_locked(self) -> None:
        if self._session_dir is not None:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._prune_old_sessions_locked()

        stamp = datetime.now().strftime("session_%Y%m%d_%H%M%S")
        sdir = self.base_dir / stamp
        suffix = 1
        while sdir.exists():
            sdir = self.base_dir / f"{stamp}_{suffix}"
            suffix += 1

        fdir = sdir / "frames"
        fdir.mkdir(parents=True, exist_ok=True)
        self._session_dir = sdir
        self._frames_dir = fdir
        self._events_path = sdir / "events.jsonl"
        self._index_path = sdir / "subtitles.txt"
        self._event_seq = 0
        self._frame_seq = 0
        self._saved_frame_paths.clear()

    def _prune_old_sessions_locked(self) -> None:
        try:
            sessions = sorted(
                [p for p in self.base_dir.iterdir() if p.is_dir() and p.name.startswith("session_")],
                key=lambda p: p.name,
            )
            excess = len(sessions) - (self.max_sessions - 1)
            for old_dir in sessions[: max(0, excess)]:
                shutil.rmtree(old_dir, ignore_errors=True)
        except Exception:
            pass

    def record_spoken(
        self,
        *,
        frame: Optional[np.ndarray],
        frame_id: int,
        ocr_engine: str,
        ocr_ms: float,
        ocr_rejected: int,
        ocr_lines: Optional[list[dict]],
        raw_utterance: str,
        speaker: Optional[str],
        voice: str,
        speech_filtered: str,
        speech_punctuated: str,
        tts_input: str,
        tts_stressed: str = "",
        tts_mode: str = "text",
        speed: str | float = "medium",
        boost: int | float = 0,
        tts_ms: float = 0.0,
        dsp_ms: float = 0.0,
    ) -> Optional[Path]:
        """Сохраняет одну озвученную реплику субтитров и исходный скриншот ROI (если ещё не сохранён)."""
        with self._lock:
            if not self.enabled:
                return None
            try:
                self._ensure_session_locked()
                assert self._frames_dir is not None and self._events_path is not None

                rel_img: Optional[str] = None
                roi_shape: Optional[list[int]] = None
                if frame is not None and frame.size > 0:
                    roi_shape = [int(frame.shape[0]), int(frame.shape[1])]
                    if frame_id in self._saved_frame_paths:
                        rel_img = self._saved_frame_paths[frame_id]
                    elif self._frame_seq < self.max_frames:
                        self._frame_seq += 1
                        fname = f"{self._frame_seq:04d}.jpg"
                        fpath = self._frames_dir / fname
                        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                        if ok:
                            fpath.write_bytes(encoded.tobytes())
                            rel_img = f"frames/{fname}"
                            if len(self._saved_frame_paths) > 32:
                                self._saved_frame_paths.clear()
                            self._saved_frame_paths[frame_id] = rel_img

                self._event_seq += 1
                record = {
                    "id": self._event_seq,
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                    "image": rel_img,
                    "roi_shape": roi_shape,
                    "ocr_engine": ocr_engine,
                    "ocr_ms": round(float(ocr_ms), 1),
                    "ocr_rejected_boxes": int(ocr_rejected),
                    "ocr_lines": ocr_lines or [],
                    "raw_utterance": raw_utterance,
                    "speaker": speaker,
                    "voice": voice,
                    "speech_filtered": speech_filtered,
                    "speech_punctuated": speech_punctuated,
                    "tts_input": tts_input,
                    "tts_stressed": tts_stressed or tts_input,
                    "tts_mode": tts_mode,
                    "speed": speed if isinstance(speed, str) else round(float(speed), 3),
                    "boost": int(boost) if isinstance(boost, int) else round(float(boost), 3),
                    "tts_ms": round(float(tts_ms), 1),
                    "dsp_ms": round(float(dsp_ms), 2),
                }
                with open(self._events_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                if self._index_path is not None:
                    ts_short = datetime.now().strftime("%H:%M:%S")
                    spk_str = speaker or "—"
                    img_str = rel_img or "no-frame"
                    final_tts = (tts_stressed or tts_input).strip()
                    with open(self._index_path, "a", encoding="utf-8") as idx_f:
                        idx_f.write(
                            f"[{ts_short}] #{self._event_seq:04d} {img_str} | {spk_str} ({voice}) | {tts_input} | TTS[{tts_mode}]: {final_tts}\n"
                        )
                return self._events_path
            except Exception:
                return None
