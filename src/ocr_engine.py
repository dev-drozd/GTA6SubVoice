from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from src.font_classifier import SubtitleFontClassifier
from src.paths import find_ocr_model_paths

_SPEAKER_COLON_HINT_RE = re.compile(r"^[А-ЯЁA-Z][а-яёa-zА-ЯЁA-Z0-9\-\.\s]{1,34}:\s+\S")
_SPEECH_PUNCT_HINT_RE = re.compile(r"[.!?…,;—–]")
_TIMER_ONLY_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$")


@dataclass
class OcrLine:
    text: str
    bbox: tuple[int, int, int, int]  # (x0, y0, x1, y1) в исходных координатах ROI
    angle_deg: float = 0.0
    stroke_contrast: float = 0.0
    confidence: float = 1.0


@dataclass
class OcrResult:
    text: str
    elapsed_ms: float
    engine: str
    confidence: float = 1.0
    lines: list[OcrLine] = field(default_factory=list)
    rejected_boxes: int = 0


def preprocess_roi_for_ocr(
    bgr: np.ndarray,
    target_h_min: int = 64,
    target_h_max: int = 280,
    padding: int = 0,
) -> tuple[np.ndarray, float, int]:
    """Адаптивная предобработка ROI для OCR (работает как с тёмной подложкой, так и без неё):
    - Автоматически нормализует контраст на затемнённых HDR/SDR-потоках, чтобы крайние буквы и знаки препинания были чёткими.
    - Сохраняет обычные ROI субтитров (высота 64–280 px) в масштабе 1:1, не размывая точки, запятые, ! и ?.
    - Не добавляет искусственную чёрную рамку вокруг крупных ROI (она подавляет короткие строки у края в DBNet);
      добавляет лишь небольшой зеркальный отступ (border padding) для очень узких полос (< 48 px).
    Возвращает (подготовленный_bgr, масштаб, отступ).
    """
    import cv2

    if bgr is None or bgr.size == 0:
        return bgr, 1.0, 0

    work = bgr
    p99 = float(np.percentile(work, 99.5))
    if 50.0 <= p99 < 205.0:
        gain = min(1.90, 235.0 / p99)
        work = np.clip(work.astype(np.float32) * gain, 0.0, 255.0).astype(np.uint8)

    h, w = work.shape[:2]
    if h < target_h_min:
        scale = min(2.0, target_h_min / float(max(1, h)))
    elif h > target_h_max:
        scale = max(0.50, target_h_max / float(h))
    else:
        scale = 1.0

    if abs(scale - 1.0) > 0.05:
        resized = cv2.resize(
            work,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_CUBIC,
        )
    else:
        resized = work
        scale = 1.0

    eff_pad = padding if padding > 0 else (4 if h < 48 else 0)
    if eff_pad > 0:
        resized = cv2.copyMakeBorder(
            resized,
            eff_pad,
            eff_pad,
            eff_pad,
            eff_pad,
            borderType=cv2.BORDER_REPLICATE,
        )
    return resized, scale, eff_pad


def _polygon_angle_deg(box) -> float:
    """Оценивает наклон базовой линии строки в градусах по 4 точкам полигона DBNet [tl, tr, br, bl]."""
    try:
        if len(box) < 4:
            return 0.0
        dx_top = float(box[1][0] - box[0][0])
        dy_top = float(box[1][1] - box[0][1])
        dx_bot = float(box[2][0] - box[3][0])
        dy_bot = float(box[2][1] - box[3][1])
        ang_top = math.degrees(math.atan2(dy_top, dx_top)) if abs(dx_top) > 1e-3 else 0.0
        ang_bot = math.degrees(math.atan2(dy_bot, dx_bot)) if abs(dx_bot) > 1e-3 else 0.0
        return 0.5 * (ang_top + ang_bot)
    except Exception:
        return 0.0


def verify_subtitle_box(
    bgr_roi: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    angle_deg: float = 0.0,
) -> tuple[bool, float, str]:
    """Универсальный верификатор боксов 2D-HUD субтитров (независимый от разрешения, координат и цветопередачи):
    Одинаково работает как с тёмной подложкой, так и без неё:
      Слой 1 (геометрия 2D-HUD): отсекает наклонный 3D-текст в мире (например, экран телефона: 2.4..8.7 град.)
      Слой 2 (чёткость штрихов 2D-HUD): измеряет относительный контраст штрихов через top-hat,
              отсекая плоские малоконтрастные 3D-текстуры (th95 ~ 23..33) и пропуская чёткие субтитры
              (как яркие, так и приглушённые вторые строки).
    Возвращает (принят, контраст_штрихов, причина).
    """
    import cv2

    if bgr_roi is None or bgr_roi.size == 0:
        return False, 0.0, "empty"

    rh, rw = bgr_roi.shape[:2]
    ix0 = max(0, int(np.floor(x0)))
    ix1 = min(rw, int(np.ceil(x1)))
    iy0 = max(0, int(np.floor(y0)))
    iy1 = min(rh, int(np.ceil(y1)))
    bh = max(1, iy1 - iy0)
    bw = max(1, ix1 - ix0)
    if bh < 6 or bw < 10:
        return False, 0.0, "too_tiny"

    # Слой 1: 2D-HUD субтитры строго горизонтальны (~0 град.); наклонный 3D-текст телефона/вывесок имеет 2.4..9 град.
    # Короткие слова ('Да.', 'хочешь?') могут давать лёгкий наклон от округления с малым вертикальным подъёмом (<= 3.4 px),
    # тогда как слова 3D-телефона под >= 3 град. ('причала.' 75x27, подъём ~6.1 px) заметно уходят по диагонали.
    vert_rise = abs(math.tan(math.radians(angle_deg))) * float(bw)
    if bw >= bh * 3.8 and vert_rise > 3.0:
        max_tilt = 2.2
    elif bw >= bh * 2.2 and vert_rise > 3.4:
        max_tilt = 2.8
    else:
        max_tilt = 5.0
    if abs(angle_deg) > max_tilt:
        return False, 0.0, f"tilted_3d_text(ang={angle_deg:+.1f})"

    patch = bgr_roi[iy0:iy1, ix0:ix1]
    if patch.size == 0:
        return False, 0.0, "empty_patch"

    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    k_size = max(5, min(11, (bh // 2) | 1))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    th95 = float(np.percentile(tophat, 95))
    med = float(np.median(gray))

    # Слой 2: проверка резкости штрихов шрифта 2D-HUD.
    # На тёмном фоне (подложка или ночная сцена, med < 50) даже затемнённые строки имеют высокий контраст (th95 >= 22).
    # На более светлом фоне (med >= 50) плоский 3D-текст телефона даёт th95 ~ 23..33, а чёткий 2D-HUD (th95 >= 42).
    min_th = 22.0 if med < 50.0 else 42.0
    if th95 < min_th:
        return False, th95, f"low_hud_sharpness(th95={th95:.1f},med={med:.0f})"

    return True, th95, f"ok(ang={angle_deg:+.1f},th95={th95:.1f})"


def _filter_grouped_subtitle_lines(
    ocr_lines: list[OcrLine],
    roi_width: int,
    tilted_wide_count: int,
) -> tuple[list[OcrLine], int]:
    """Фильтр геометрической согласованности всей сцены (независимо от координат и цвета):
    - Если на кадре отброшены >= 2 широких наклонных бокса (3D-телефон в руке), подавляет случайные
      горизонтальные фрагменты того же экрана (вроде имени контакта 'Мия'), если нет чёткой строки диалога ('Имя: ...').
    - Если найдена основная центральная строка ('Имя: ...' или диалог со знаками >= 16 симв.),
      отбрасывает смещённые короткие боковые HUD-плашки (например плашку звонка 'Мия 00:00' сбоку).
    - Отбрасывает обрезанные у самого края ROI подсказки UI без спикера и финальной пунктуации.
    """
    if not ocr_lines:
        return [], 0

    rw = max(1, int(roi_width))
    rejected = 0

    # 1. Защита от 3D-экрана телефона: при >= 2 наклонных боксах подавляем одиночные фрагменты без 'Имя: ...'
    if tilted_wide_count >= 2:
        kept: list[OcrLine] = []
        for ln in ocr_lines:
            if _SPEAKER_COLON_HINT_RE.match(ln.text) and ln.stroke_contrast >= 55.0:
                kept.append(ln)
            else:
                rejected += 1
        ocr_lines = kept
        if not ocr_lines:
            return [], rejected

    # 1б. Защита от обрезанных краевых UI-подсказок: без спикера и конечного знака препинания вплотную к краю ROI
    if rw >= 60:
        kept_edge: list[OcrLine] = []
        for ln in ocr_lines:
            lx0, _ly0, lx1, _ly1 = ln.bbox
            bw = max(1, lx1 - lx0)
            txt = ln.text.strip()
            touches_edge = (lx1 >= rw - 2) or (lx0 <= 2)
            has_spk = bool(_SPEAKER_COLON_HINT_RE.match(txt))
            has_end_punct = txt.endswith((".", "!", "?", "…"))
            is_short_clipped_ui = (
                touches_edge
                and not has_spk
                and not has_end_punct
                and len(txt) <= 28
                and len(txt.split()) <= 4
                and bw < rw * 0.55
            )
            if is_short_clipped_ui:
                rejected += 1
                continue
            kept_edge.append(ln)
        ocr_lines = kept_edge
        if not ocr_lines:
            return [], rejected

    if len(ocr_lines) <= 1:
        return ocr_lines, rejected

    # 2. Определяем опорную строку субтитров (сначала 'Имя: ...', иначе самую широкую строку с пунктуацией)
    anchors = [ln for ln in ocr_lines if _SPEAKER_COLON_HINT_RE.match(ln.text)]
    if not anchors:
        anchors = [
            ln
            for ln in ocr_lines
            if len(ln.text) >= 16 and _SPEECH_PUNCT_HINT_RE.search(ln.text) and ln.stroke_contrast >= 50.0
        ]

    if anchors:
        anchor = max(anchors, key=lambda ln: ln.bbox[2] - ln.bbox[0])
        anchor_cx = (anchor.bbox[0] + anchor.bbox[2]) * 0.5
        anchor_h = max(8.0, float(anchor.bbox[3] - anchor.bbox[1]))

        filtered: list[OcrLine] = []
        for ln in ocr_lines:
            cx = (ln.bbox[0] + ln.bbox[2]) * 0.5
            dx_ratio = abs(cx - anchor_cx) / float(rw)
            has_spk = bool(_SPEAKER_COLON_HINT_RE.match(ln.text))
            is_short_side_hud = (
                dx_ratio > 0.20
                and not has_spk
                and (
                    _TIMER_ONLY_RE.match(ln.text) is not None
                    or (len(ln.text) <= 22 and not _SPEECH_PUNCT_HINT_RE.search(ln.text))
                )
            )
            # Игнорируем слишком мелкий или слишком крупный посторонний HUD-текст вдали от оси субтитров
            lh = max(1.0, float(ln.bbox[3] - ln.bbox[1]))
            size_mismatch = dx_ratio > 0.25 and not has_spk and (lh < anchor_h * 0.55 or lh > anchor_h * 1.75)
            if is_short_side_hud or size_mismatch:
                rejected += 1
                continue
            filtered.append(ln)
        ocr_lines = filtered

    return ocr_lines, rejected


def _group_boxes_into_lines(
    bgr_roi: np.ndarray,
    items: list,
    offset_x: int = 0,
    offset_y: int = 0,
    scale: float = 1.0,
    pad: int = 10,
    font_classifier: Optional[SubtitleFontClassifier] = None,
) -> tuple[list[OcrLine], int]:
    if not items:
        return [], 0

    rh, rw = bgr_roi.shape[:2]
    enriched = []
    rejected = 0
    tilted_wide_count = 0

    for item in items:
        box, text, score = item[0], str(item[1]).strip(), float(item[2])
        if not text:
            continue
        xs = [(pt[0] - pad) / scale + offset_x for pt in box]
        ys = [(pt[1] - pad) / scale + offset_y for pt in box]
        x0 = max(0, int(np.floor(min(xs))))
        x1 = min(rw, int(np.ceil(max(xs))))
        y0 = max(0, int(np.floor(min(ys))))
        y1 = min(rh, int(np.ceil(max(ys))))
        y_center = (y0 + y1) * 0.5
        height = max(1.0, float(y1 - y0))
        width = max(1.0, float(x1 - x0))
        angle = _polygon_angle_deg(box)

        ok, contrast, reason = verify_subtitle_box(bgr_roi, x0, y0, x1, y1, angle_deg=angle)
        if not ok:
            rejected += 1
            if reason.startswith("tilted_3d_text") and width >= height * 2.0:
                tilted_wide_count += 1
            continue

        enriched.append((y_center, height, x0, y0, x1, y1, text, score, angle, contrast))

    if not enriched:
        return [], rejected

    # Слой 3 (компактный ONNX-классификатор шрифта, ~0.27 мс/батч): отсеивает не-субтитровые шрифты (карта, кнопки, меню Windows)
    if font_classifier is not None and font_classifier.is_available:
        probs = font_classifier.score_boxes(bgr_roi, [(e[2], e[3], e[4], e[5]) for e in enriched])
        kept_enriched = []
        for entry, prob in zip(enriched, probs):
            if prob >= font_classifier.threshold:
                kept_enriched.append(entry)
            else:
                rejected += 1
        enriched = kept_enriched
        if not enriched:
            return [], rejected

    enriched.sort(key=lambda e: (round(e[0] / 4.0), e[2]))
    raw_lines: list[list[tuple]] = []
    for entry in enriched:
        placed = False
        for line in raw_lines:
            ref_y = line[0][0]
            ref_h = line[0][1]
            max_h = max(ref_h, entry[1])
            min_h = min(ref_h, entry[1])
            dy = abs(entry[0] - ref_y)
            if dy <= max_h * 0.62:
                # Склеиваем по горизонтали на одной строке только близкие блоки
                # (чтобы не слепить центральный субтитр с боковой HUD-плашкой звонка на той же высоте Y)
                line_x0 = min(e[2] for e in line)
                line_x1 = max(e[4] for e in line)
                gap_x = max(0, entry[2] - line_x1, line_x0 - entry[4])
                max_gap = max_h * 3.5
                # Если строка начинается с 'Спикер:' и совпадает по базовой линии и высоте (перекрытое посередине слово,
                # например 'Местные жители:' ... 'Стой!'), склеиваем через зазор, чтобы имя спикера не отделилось от реплики.
                left_tail = sorted(line, key=lambda e: e[2])[-1][6].rstrip()
                if (
                    entry[2] >= line_x1
                    and left_tail.endswith((":", ";"))
                    and dy <= max_h * 0.45
                    and (min_h / max(1.0, max_h)) >= 0.70
                ):
                    max_gap = max(max_gap, min(max_h * 8.0, float(rw) * 0.35))
                if gap_x <= max_gap:
                    line.append(entry)
                    placed = True
                    break
        if not placed:
            raw_lines.append([entry])

    ocr_lines: list[OcrLine] = []
    for line in raw_lines:
        line.sort(key=lambda e: e[2])
        line_text = " ".join(e[6] for e in line if e[6])
        lx0 = min(e[2] for e in line)
        ly0 = min(e[3] for e in line)
        lx1 = max(e[4] for e in line)
        ly1 = max(e[5] for e in line)
        avg_conf = sum(e[7] for e in line) / len(line)
        avg_ang = sum(e[8] for e in line) / len(line)
        avg_contrast = sum(e[9] for e in line) / len(line)
        ocr_lines.append(
            OcrLine(
                text=line_text.strip(),
                bbox=(lx0, ly0, lx1, ly1),
                angle_deg=avg_ang,
                stroke_contrast=avg_contrast,
                confidence=avg_conf,
            )
        )

    ocr_lines, extra_rej = _filter_grouped_subtitle_lines(ocr_lines, rw, tilted_wide_count)
    return ocr_lines, rejected + extra_rej


class RapidOcrBackend:
    """Быстрый CPU-движок RapidOCR ONNX (~45-70 мс на 2 потоках без вреда для FPS игры) с универсальной 2D-HUD фильтрацией."""

    def __init__(
        self,
        num_threads: int = 2,
        font_classifier: Optional[SubtitleFontClassifier] = None,
    ) -> None:
        self.num_threads = num_threads
        self.font_classifier = font_classifier
        self.font_classifier_enabled = True
        self._ocr = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._ocr is not None:
            return
        from rapidocr_onnxruntime import RapidOCR

        paths = find_ocr_model_paths()
        if not (paths["det"] and paths["rec"] and paths["keys"]):
            raise FileNotFoundError(
                "Модели RapidOCR (PP-OCRv5 кириллица) не найдены в models/ocr/. "
                "Установите их через Мастер компонентов или переключитесь на Windows OCR."
            )

        kwargs: dict = {
            "use_cls": False,
            "intra_op_num_threads": self.num_threads,
            "inter_op_num_threads": 1,
            "det_limit_type": "max",
            "det_limit_side_len": 960,
            "det_model_path": str(paths["det"]),
            "rec_model_path": str(paths["rec"]),
            "rec_keys_path": str(paths["keys"]),
        }
        if paths["cls"]:
            kwargs["cls_model_path"] = str(paths["cls"])

        self._ocr = RapidOCR(**kwargs)

    def warmup(self) -> None:
        with self._lock:
            self._ensure_loaded()
            dummy = np.full((64, 256, 3), 15, dtype=np.uint8)
            try:
                self._ocr(dummy)
            except Exception:
                pass

    def recognize(self, bgr: np.ndarray) -> OcrResult:
        t0 = time.perf_counter()
        with self._lock:
            self._ensure_loaded()
            prep, scale, pad = preprocess_roi_for_ocr(bgr, target_h_min=64, target_h_max=280, padding=0)
            res, _ = self._ocr(prep)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if not res:
            return OcrResult(text="", elapsed_ms=elapsed_ms, engine="rapidocr", confidence=0.0, lines=[])

        active_clf = self.font_classifier if self.font_classifier_enabled else None
        lines, rejected = _group_boxes_into_lines(
            bgr,
            res,
            offset_x=0,
            offset_y=0,
            scale=scale,
            pad=pad,
            font_classifier=active_clf,
        )
        if not lines:
            return OcrResult(
                text="",
                elapsed_ms=elapsed_ms,
                engine="rapidocr",
                confidence=0.0,
                lines=[],
                rejected_boxes=rejected,
            )

        avg_conf = sum(ln.confidence for ln in lines) / len(lines)
        full_text = "\n".join(ln.text for ln in lines)
        return OcrResult(
            text=full_text,
            elapsed_ms=elapsed_ms,
            engine="rapidocr",
            confidence=avg_conf,
            lines=lines,
            rejected_boxes=rejected,
        )


class WinRtOcrBackend:
    """Сверхэкономичный по ОЗУ движок Windows.Media.Ocr (WinRT) с универсальной проверкой 2D-HUD и геометрии."""

    def __init__(self, font_classifier: Optional[SubtitleFontClassifier] = None) -> None:
        self.font_classifier = font_classifier
        self.font_classifier_enabled = True
        self._engine = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._engine is not None:
            return
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        lang = Language("ru")
        if OcrEngine.is_language_supported(lang):
            self._engine = OcrEngine.try_create_from_language(lang)
        if self._engine is None:
            self._engine = OcrEngine.try_create_from_user_profile_languages()
        if self._engine is None:
            raise RuntimeError("Windows OCR (ru) недоступен в системе.")
        self._loop = asyncio.new_event_loop()

    async def _recognize_async(self, bgr: np.ndarray) -> tuple[list[OcrLine], int]:
        import cv2
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter

        prep, scale, pad = preprocess_roi_for_ocr(bgr, target_h_min=64, target_h_max=280, padding=0)
        bgra = cv2.cvtColor(prep, cv2.COLOR_BGR2BGRA)
        h, w = bgra.shape[:2]
        writer = DataWriter()
        sb = None
        try:
            writer.write_bytes(bgra.tobytes())
            buf = writer.detach_buffer()
            sb = SoftwareBitmap.create_copy_from_buffer(buf, BitmapPixelFormat.BGRA8, w, h)
            result = await self._engine.recognize_async(sb)
        finally:
            if sb is not None:
                try:
                    sb.close()
                except Exception:
                    pass
            try:
                writer.close()
            except Exception:
                pass

        # WinRT возвращает общий угол text_angle (в градусах), когда читается наклонный 3D-экран телефона
        winrt_angle = float(getattr(result, "text_angle", None) or 0.0)
        if abs(winrt_angle) > 2.2:
            return [], len(list(result.lines))

        lines: list[OcrLine] = []
        rejected = 0
        rh, rw = bgr.shape[:2]
        for line in result.lines:
            ltext = (line.text or "").strip()
            if not ltext:
                continue
            words = list(line.words)
            if words:
                xs0 = [(wd.bounding_rect.x - pad) / scale for wd in words]
                ys0 = [(wd.bounding_rect.y - pad) / scale for wd in words]
                xs1 = [(wd.bounding_rect.x + wd.bounding_rect.width - pad) / scale for wd in words]
                ys1 = [(wd.bounding_rect.y + wd.bounding_rect.height - pad) / scale for wd in words]
                x0 = max(0, int(np.floor(min(xs0))))
                y0 = max(0, int(np.floor(min(ys0))))
                x1 = min(rw, int(np.ceil(max(xs1))))
                y1 = min(rh, int(np.ceil(max(ys1))))
            else:
                x0, y0, x1, y1 = 0, 0, rw, rh

            ok, contrast, _ = verify_subtitle_box(bgr, x0, y0, x1, y1, angle_deg=winrt_angle)
            if not ok:
                rejected += 1
                continue

            lines.append(
                OcrLine(
                    text=ltext,
                    bbox=(x0, y0, x1, y1),
                    angle_deg=winrt_angle,
                    stroke_contrast=contrast,
                    confidence=1.0,
                )
            )

        if self.font_classifier_enabled and self.font_classifier is not None and self.font_classifier.is_available and lines:
            probs = self.font_classifier.score_boxes(bgr, [ln.bbox for ln in lines])
            kept_lines: list[OcrLine] = []
            for ln, prob in zip(lines, probs):
                if prob >= self.font_classifier.threshold:
                    kept_lines.append(ln)
                else:
                    rejected += 1
            lines = kept_lines

        lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        lines, extra_rej = _filter_grouped_subtitle_lines(lines, rw, tilted_wide_count=0)
        return lines, rejected + extra_rej

    def recognize(self, bgr: np.ndarray) -> OcrResult:
        t0 = time.perf_counter()
        with self._lock:
            self._ensure_loaded()
            lines, rejected = self._loop.run_until_complete(self._recognize_async(bgr))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        full_text = "\n".join(ln.text for ln in lines)
        return OcrResult(
            text=full_text,
            elapsed_ms=elapsed_ms,
            engine="winrt",
            confidence=1.0 if lines else 0.0,
            lines=lines,
            rejected_boxes=rejected,
        )

    def close(self) -> None:
        with self._lock:
            if self._loop is not None:
                try:
                    if not self._loop.is_closed():
                        self._loop.close()
                except Exception:
                    pass
                self._loop = None
            self._engine = None


class OcrEngineManager:
    """Единый фасад OCR с поддержкой RapidOCR (по умолчанию) и Windows OCR (WinRT) с резервным откатом."""

    def __init__(
        self,
        engine_name: str = "rapidocr",
        num_threads: int = 4,
        font_classifier_enabled: bool = True,
    ) -> None:
        self.engine_name = engine_name
        self.num_threads = num_threads
        self.font_classifier_enabled = bool(font_classifier_enabled)
        self._font_clf = SubtitleFontClassifier(num_threads=max(1, min(2, int(num_threads))))
        self._rapid: Optional[RapidOcrBackend] = None
        self._winrt: Optional[WinRtOcrBackend] = None

    def set_engine(self, engine_name: str) -> None:
        if engine_name in ("rapidocr", "winrt"):
            self.engine_name = engine_name

    def set_font_classifier_enabled(self, enabled: bool) -> None:
        self.font_classifier_enabled = bool(enabled)
        if self._rapid is not None:
            self._rapid.font_classifier_enabled = self.font_classifier_enabled
        if self._winrt is not None:
            self._winrt.font_classifier_enabled = self.font_classifier_enabled

    def _get_backend(self):
        if self.engine_name == "winrt":
            if self._winrt is None:
                self._winrt = WinRtOcrBackend(font_classifier=self._font_clf)
            self._winrt.font_classifier_enabled = self.font_classifier_enabled
            return self._winrt
        if self._rapid is None:
            self._rapid = RapidOcrBackend(num_threads=self.num_threads, font_classifier=self._font_clf)
        self._rapid.font_classifier_enabled = self.font_classifier_enabled
        return self._rapid

    def warmup(self) -> None:
        if self.font_classifier_enabled:
            _ = self._font_clf.is_available
        backend = self._get_backend()
        if isinstance(backend, RapidOcrBackend):
            backend.warmup()

    def recognize(self, bgr: np.ndarray) -> OcrResult:
        try:
            return self._get_backend().recognize(bgr)
        except Exception:
            if self.engine_name == "winrt":
                if self._rapid is None:
                    self._rapid = RapidOcrBackend(num_threads=self.num_threads, font_classifier=self._font_clf)
                self._rapid.font_classifier_enabled = self.font_classifier_enabled
                return self._rapid.recognize(bgr)
            else:
                if self._winrt is None:
                    self._winrt = WinRtOcrBackend(font_classifier=self._font_clf)
                self._winrt.font_classifier_enabled = self.font_classifier_enabled
                return self._winrt.recognize(bgr)

    def close(self) -> None:
        if self._winrt is not None:
            self._winrt.close()
            self._winrt = None
