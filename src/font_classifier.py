from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import numpy as np

from src.paths import APP_DIR, BUNDLED_MODELS_DIR, MODELS_DIR, USER_ROOT

PATCH_H = 24
PATCH_W = 96
FONT_CLF_FILENAME = "subtitle_font_clf.onnx"
DEFAULT_SUBTITLE_FONT_THRESHOLD = 0.27


def find_font_classifier_model(configured_path: Optional[str] = None) -> Optional[Path]:
    """Ищет локальный компактный ONNX-классификатор шрифта субтитров (~120 КБ) в управляемых каталогах models/."""
    if configured_path:
        p = Path(configured_path)
        if p.is_absolute():
            if p.is_file():
                return p.resolve()
        else:
            for root in (APP_DIR, USER_ROOT):
                cand = root / p
                if cand.is_file():
                    return cand.resolve()

    search_dirs: list[Path] = [BUNDLED_MODELS_DIR, MODELS_DIR, APP_DIR / "_internal" / "models"]
    seen: set[Path] = set()
    for d in search_dirs:
        if d in seen:
            continue
        seen.add(d)
        cand = d / FONT_CLF_FILENAME
        if cand.is_file():
            return cand.resolve()
    return None


def extract_font_patch_windows(
    bgr_roi: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    max_windows: int = 2,
) -> np.ndarray:
    """Извлекает инвариантные к разрешению, координатам и цветовому профилю патчи (K, 2, 24, 96) float32
    для одной строки OCR:
      - Сохраняет нативные пропорции глифов за счёт масштабирования высоты до 24 px (без сжатия по ширине).
      - Канал 0: перцентильно-нормированная яркость [0..1] (глиф + тень/подложка).
      - Канал 1: нормированный морфологический top-hat силуэт штрихов [0..1] (чистая геометрия шрифта).
    """
    import cv2

    if bgr_roi is None or bgr_roi.size == 0:
        return np.zeros((0, 2, PATCH_H, PATCH_W), dtype=np.float32)

    rh, rw = bgr_roi.shape[:2]
    # Добавляем 2 px контекста вокруг рамки, чтобы сохранить тень и контур шрифта
    px0 = max(0, int(x0) - 2)
    py0 = max(0, int(y0) - 2)
    px1 = min(rw, int(x1) + 2)
    py1 = min(rh, int(y1) + 2)
    if px1 <= px0 + 2 or py1 <= py0 + 2:
        return np.zeros((0, 2, PATCH_H, PATCH_W), dtype=np.float32)

    patch = bgr_roi[py0:py1, px0:px1]
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    bh, bw = gray.shape[:2]

    k_size = max(5, min(11, (bh // 2) | 1))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel).astype(np.float32)

    gf = gray.astype(np.float32)
    p5 = float(np.percentile(gf, 5))
    p98 = float(np.percentile(gf, 98))
    scale_g = max(20.0, p98 - p5)
    ch0 = np.clip((gf - p5) / scale_g, 0.0, 1.0)

    th99 = float(np.percentile(tophat, 99.2))
    scale_th = max(24.0, th99)
    ch1 = np.clip(tophat / scale_th, 0.0, 1.0)

    stacked = np.stack([ch0, ch1], axis=-1)  # Форма входа: (bh, bw, 2)
    new_w = max(8, int(round(bw * (PATCH_H / float(max(1, bh))))))
    interp = cv2.INTER_AREA if bh > PATCH_H else cv2.INTER_LINEAR
    resized = cv2.resize(stacked, (new_w, PATCH_H), interpolation=interp)

    windows: list[np.ndarray] = []
    if new_w <= PATCH_W:
        canvas = np.zeros((PATCH_H, PATCH_W, 2), dtype=np.float32)
        # Для коротких строк заполняем поля нейтральным фоном на ch0 и нулём на штриховом ch1
        canvas[:, :, 0] = float(np.median(resized[:, :, 0]))
        x_off = (PATCH_W - new_w) // 2
        canvas[:, x_off : x_off + new_w, :] = resized
        windows.append(np.transpose(canvas, (2, 0, 1)))
    else:
        # Берём окно текста по центру или равномерные срезы без искажения пропорций
        if max_windows <= 1:
            start = max(0, (new_w - PATCH_W) // 2)
            crop = resized[:, start : start + PATCH_W, :]
            windows.append(np.transpose(crop, (2, 0, 1)))
        else:
            # Равномерно берём до max_windows окон вдоль строки
            positions = np.linspace(0, max(0, new_w - PATCH_W), num=max_windows, dtype=int)
            for s in positions:
                crop = resized[:, int(s) : int(s) + PATCH_W, :]
                windows.append(np.transpose(crop, (2, 0, 1)))

    return np.ascontiguousarray(np.stack(windows, axis=0), dtype=np.float32)


class SubtitleFontClassifier:
    """Компактная CNN (~115 КБ ONNX, инференс на CPU < 0.5 мс на батч), отличающая 2D-HUD шрифт субтитров игры
    от надписей на карте, меню, телефона и посторонних окон Windows.
    """

    def __init__(
        self,
        model_path: Optional[str | Path] = None,
        num_threads: int = 2,
        threshold: float = DEFAULT_SUBTITLE_FONT_THRESHOLD,
    ) -> None:
        self.model_path: Optional[Path] = (
            Path(model_path) if model_path is not None else find_font_classifier_model()
        )
        self.num_threads = max(1, int(num_threads))
        self.threshold = float(threshold)
        self._session = None
        self._input_name: str = "patches"
        self._lock = threading.Lock()
        self._load_attempted: bool = False

    @property
    def is_available(self) -> bool:
        return self._ensure_loaded()

    def _ensure_loaded(self) -> bool:
        if self._session is not None:
            return True
        if self._load_attempted:
            return False
        with self._lock:
            if self._session is not None:
                return True
            if self._load_attempted:
                return False
            self._load_attempted = True
            if self.model_path is None or not self.model_path.is_file():
                self.model_path = find_font_classifier_model()
            if self.model_path is None or not self.model_path.is_file():
                return False
            try:
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self.num_threads
                opts.inter_op_num_threads = 1
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                sess = ort.InferenceSession(
                    str(self.model_path),
                    sess_options=opts,
                    providers=["CPUExecutionProvider"],
                )
                self._input_name = sess.get_inputs()[0].name
                self._session = sess
                return True
            except Exception:
                self._session = None
                return False

    def score_boxes(
        self,
        bgr_roi: np.ndarray,
        boxes: list[tuple[int, int, int, int]],
    ) -> list[float]:
        """Возвращает вероятность шрифта субтитров в [0.0, 1.0] для каждого прямоугольника (x0, y0, x1, y1).
        Если опциональная ONNX-модель недоступна, возвращает 1.0 для каждого блока (fail-open).
        """
        if not boxes:
            return []
        if not self._ensure_loaded() or self._session is None:
            return [1.0] * len(boxes)

        batch_list: list[np.ndarray] = []
        slices: list[tuple[int, int]] = []
        cursor = 0
        for x0, y0, x1, y1 in boxes:
            wins = extract_font_patch_windows(bgr_roi, x0, y0, x1, y1, max_windows=2)
            n = wins.shape[0]
            if n > 0:
                batch_list.append(wins)
            slices.append((cursor, cursor + n))
            cursor += n

        if not batch_list:
            return [0.0] * len(boxes)

        inp = np.ascontiguousarray(np.concatenate(batch_list, axis=0), dtype=np.float32)
        with self._lock:
            logits = self._session.run(None, {self._input_name: inp})[0].reshape(-1)
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))

        scores: list[float] = []
        for s, e in slices:
            if e <= s:
                scores.append(0.0)
            else:
                # Максимум вероятности по окнам удерживает длинные строки стабильными, даже если край задевает светлый фон
                scores.append(float(np.max(probs[s:e])))
        return scores
