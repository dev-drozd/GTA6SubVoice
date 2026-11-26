from __future__ import annotations

import gc
import html
import inspect
import logging
import re
import threading
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np

from src.audio_dsp import normalize_speed_preset
from src.paths import find_silero_model
from src.text_filter import StressDictManager, mark_question_focus, normalize_text_base
from src.voice_manager import ALL_SILERO_SPEAKERS

logger = logging.getLogger(__name__)

# Невидимые символы форматирования и комбинируемые ударения, удаляемые пустой строкой ДО ALLOWED_TEXT_RE
_INVISIBLE_CHARS_RE = re.compile(r"[\u00ad\u200b\ufeff\u0301]")
# Сохраняем русские буквы, цифры, знаки препинания, ударение Silero '+' и разметку '**слово**'/'*слово*'
ALLOWED_TEXT_RE = re.compile(r"[^а-яёА-ЯЁ0-9\s\.,!\?;:\-\–\—…\"'«»\(\)\+\*]+")
_DANGLING_PLUS_RE = re.compile(r"\+(?![аеёиоуыэюяАЕЁИОУЫЭЮЯ])")
MULTI_WORD_EMPH_RE = re.compile(r"\*\*([^*]+)\*\*")
_SINGLE_OR_DOUBLE_STAR_TOKEN_RE = re.compile(r"(\*\*|\*)([^\s*]+)\1")
_EMPH_WORD_EDGE_RE = re.compile(r"^([^А-ЯЁа-яё\+]*)(.*?)([^А-ЯЁа-яё\+]*)$")
CYRILLIC_LETTER_RE = re.compile(r"[а-яёА-ЯЁ]")
_BROKEN_TRAIL_DOT_RE = re.compile(r"([,;:\-–])\s*\.(?!\.)")
_MULTI_DOTS_RE = re.compile(r"\.{2,}")
_MISSING_SPACE_AFTER_PUNCT_RE = re.compile(r"([.!?])(?=[А-ЯЁа-яё«\"])")
_PUA_SINGLE_STAR = "\ue001"
_PUA_DOUBLE_STAR = "\ue002"


def _normalize_emphasis_tokens(text: str) -> str:
    """Проверяет оба формата звёздочек Silero v5_6_ru:
    - '**слово**' (логическая эмфаза)
    - '*слово*' (интонационный центр общего вопроса)
    Гарантирует, что маркеры оборачивают строго одно кириллическое слово (сохраняя '+' и дефисы),
    пунктуация остаётся снаружи ('*хорошо*', '*берут*?', '**очень**,'), а случайные одиночные '*' удаляются.
    """

    def _split_multi_double(m: re.Match) -> str:
        inner = m.group(1).strip()
        raw_tokens = inner.split()
        if len(raw_tokens) <= 1:
            return m.group(0)
        out_tokens: list[str] = []
        for tok in raw_tokens:
            if not CYRILLIC_LETTER_RE.search(tok):
                out_tokens.append(tok)
                continue
            em = _EMPH_WORD_EDGE_RE.match(tok)
            if em and em.group(2) and CYRILLIC_LETTER_RE.search(em.group(2)):
                out_tokens.append(f"{em.group(1)}**{em.group(2)}**{em.group(3)}")
            else:
                out_tokens.append(tok)
        return " ".join(out_tokens) if out_tokens else inner

    def _validate_star_token(m: re.Match) -> str:
        stars, tok = m.group(1), m.group(2)
        if not CYRILLIC_LETTER_RE.search(tok):
            return tok
        em = _EMPH_WORD_EDGE_RE.match(tok)
        if em and em.group(2) and CYRILLIC_LETTER_RE.search(em.group(2)):
            marker = _PUA_DOUBLE_STAR if stars == "**" else _PUA_SINGLE_STAR
            return f"{em.group(1)}{marker}{em.group(2)}{marker}{em.group(3)}"
        return tok

    out = MULTI_WORD_EMPH_RE.sub(_split_multi_double, text)
    out = _SINGLE_OR_DOUBLE_STAR_TOKEN_RE.sub(_validate_star_token, out)
    # Удаляем любые висячие/непарные '*' и многословные интервалы с одиночной звёздочкой
    out = out.replace("*", "")
    out = out.replace(_PUA_DOUBLE_STAR, "**").replace(_PUA_SINGLE_STAR, "*")
    return out


def _finalize_for_silero(text: str) -> str:
    """Финальная санитизация текста для Silero v5_6_ru после базовой нормализации, StressDictManager и фокуса вопроса."""
    if not text:
        return ""
    # Удаляем невидимые символы и комбинируемые ударения пустой строкой ДО ALLOWED_TEXT_RE, чтобы не разрывать слова
    cleaned = _INVISIBLE_CHARS_RE.sub("", text)
    cleaned = cleaned.replace("…", "...").replace("—", "–")
    cleaned = re.sub(r"([А-ЯЁа-яёA-Za-z0-9,:;!?])«", r"\1 «", cleaned)
    cleaned = re.sub(r"»([А-ЯЁа-яёA-Za-z0-9])", r"» \1", cleaned)
    cleaned = ALLOWED_TEXT_RE.sub(" ", cleaned)
    # Удаляем висячий '+', не стоящий непосредственно перед русской гласной
    cleaned = _DANGLING_PLUS_RE.sub("", cleaned)
    # Исправляем огрехи OCR/пунктуации, чтобы внутренние SENT_SPLIT_RE и classify_sentence в Silero v5.6 работали чисто
    cleaned = cleaned.replace("!?", "?!")
    cleaned = _BROKEN_TRAIL_DOT_RE.sub(r"\1", cleaned)
    cleaned = _MULTI_DOTS_RE.sub("...", cleaned)
    cleaned = _MISSING_SPACE_AFTER_PUNCT_RE.sub(r"\1 ", cleaned)
    cleaned = _normalize_emphasis_tokens(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def prepare_text_for_silero(
    text: str,
    stress_dict: Optional[StressDictManager] = None,
) -> str:
    """Полный конвейер подготовки текста для нативного Silero v5_6_ru:
    1. normalize_text_base() — очистка OCR, смешанные гомоглифы, тянущиеся буквы, числа/валюты/время, транслитерация, **эмфаза**
    2. StressDictManager.apply() — однопроходный словарь сленга и пользовательских замен из data/stress_dict.json
    3. mark_question_focus() — центр вопросительной интонации '*слово*' для общих вопросов ('?')
    4. _finalize_for_silero() — удаление невидимых символов, висячих '+', валидация '*слово*'/'**слово**'
    """
    base = normalize_text_base(text, stress_dict=stress_dict)
    if not base:
        return ""
    stressed = stress_dict.apply(base) if stress_dict is not None else base
    stressed = mark_question_focus(stressed)
    return _finalize_for_silero(stressed)


def sanitize_text_for_silero(
    text: str,
    stress_dict: Optional[StressDictManager] = None,
) -> str:
    """Подготавливает и очищает текст субтитра для Silero v5_6_ru."""
    return prepare_text_for_silero(text, stress_dict=stress_dict)


class SileroTTSEngine:
    """Потокобезопасная обёртка над локальной CPU-моделью Silero TTS v5_6_ru с частотой 48 000 Гц."""

    def __init__(
        self,
        model_path: Optional[str | Path] = None,
        num_threads: int = 2,
        sample_rate: int = 48000,
        stress_dict: Optional[StressDictManager] = None,
    ) -> None:
        self.num_threads = max(1, int(num_threads))
        self.sample_rate = sample_rate if sample_rate in (24000, 48000) else 48000
        self.model_path: Optional[Path] = find_silero_model(str(model_path) if model_path else None)
        self.stress_dict = stress_dict
        self._model = None
        self._speakers: list[str] = list(ALL_SILERO_SPEAKERS)
        self._apply_tts_params: set[str] = {
            "ssml_text",
            "put_accent",
            "put_yo",
            "put_stress_homo",
            "put_yo_homo",
            "stress_single_vowel",
            "intensity",
        }
        self._lock = threading.Lock()
        self.load_time_ms: float = 0.0
        self.warmup_time_ms: float = 0.0
        self.last_synthesis_meta: dict[str, str] = {
            "tts_stressed": "",
            "tts_mode": "text",
        }

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def speakers(self) -> list[str]:
        return list(self._speakers)

    def _build_apply_kwargs(
        self,
        text: str,
        speaker: str,
        sr: int,
        speed: str | float = "medium",
    ) -> dict:
        preset = normalize_speed_preset(speed)
        use_ssml = preset in ("fast", "x-fast") and "ssml_text" in self._apply_tts_params
        if use_ssml:
            escaped = html.escape(text, quote=False)
            kwargs: dict = {
                "ssml_text": f'<speak><prosody rate="{preset}">{escaped}</prosody></speak>',
                "speaker": speaker,
                "sample_rate": sr,
                "put_accent": True,
                "put_yo": True,
            }
        else:
            kwargs = {
                "text": text,
                "speaker": speaker,
                "sample_rate": sr,
                "put_accent": True,
                "put_yo": True,
            }
        if "put_stress_homo" in self._apply_tts_params:
            kwargs["put_stress_homo"] = True
        if "put_yo_homo" in self._apply_tts_params:
            kwargs["put_yo_homo"] = True
        if "stress_single_vowel" in self._apply_tts_params:
            kwargs["stress_single_vowel"] = False
        if "intensity" in self._apply_tts_params and "*" in text:
            # Мягкая intensity=2 для естественной вопросительной интонации (*слово*) и intensity=3 при наличии **эмфазы**
            kwargs["intensity"] = 3 if "**" in text else 2
        return kwargs

    def load(self, model_path: Optional[str | Path] = None, num_threads: Optional[int] = None) -> None:
        with self._lock:
            if model_path is not None:
                self.model_path = find_silero_model(str(model_path))
            elif self.model_path is None or not self.model_path.is_file():
                self.model_path = find_silero_model(None)

            if num_threads is not None:
                self.num_threads = max(1, int(num_threads))

            if self.model_path is None or not self.model_path.is_file():
                raise FileNotFoundError(
                    "Локальная модель Silero TTS 5.6 (models/v5_6_ru.pt) не найдена. "
                    "Установите её через Мастер компонентов или укажите файл вручную."
                )

            import torch

            torch.set_num_threads(self.num_threads)
            try:
                torch.set_num_interop_threads(1)
            except Exception:
                pass
            t0 = time.perf_counter()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                importer = torch.package.PackageImporter(str(self.model_path))
                model = importer.load_pickle("tts_models", "model")
                del importer
                model.to(torch.device("cpu"))
            gc.collect()

            self._model = model
            try:
                sig = inspect.signature(model.apply_tts)
                params = set(sig.parameters.keys())
                has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                if has_var_kw:
                    params.update(
                        {
                            "ssml_text",
                            "put_accent",
                            "put_yo",
                            "put_stress_homo",
                            "put_yo_homo",
                            "stress_single_vowel",
                            "intensity",
                        }
                    )
                self._apply_tts_params = params
            except Exception:
                self._apply_tts_params = {
                    "ssml_text",
                    "put_accent",
                    "put_yo",
                    "put_stress_homo",
                    "put_yo_homo",
                    "stress_single_vowel",
                    "intensity",
                }

            discovered = getattr(model, "speakers", None)
            if discovered:
                valid = [str(s) for s in discovered if str(s) in ALL_SILERO_SPEAKERS]
                if valid:
                    self._speakers = valid

            # Прогревочный прогон для голоса 'eugene' на 48000 Гц для разогрева кэшей CPU/JIT
            t_warm = time.perf_counter()
            try:
                warmup_speaker = "eugene" if "eugene" in self._speakers else self._speakers[0]
                with torch.inference_mode():
                    self._model.apply_tts(**self._build_apply_kwargs("Тест.", warmup_speaker, self.sample_rate))
            except Exception as exc:
                logger.warning("Silero warmup warning: %s", exc)
            self.warmup_time_ms = (time.perf_counter() - t_warm) * 1000.0
            self.load_time_ms = (time.perf_counter() - t0) * 1000.0

    def synthesize(
        self,
        text: str,
        speaker: str = "eugene",
        sample_rate: Optional[int] = None,
        speed: str | float = "medium",
    ) -> tuple[np.ndarray, float]:
        """Синтезирует чистую нативную русскую речь на 48000 Гц с официальными пресетами темпа Silero v5.6 (medium/fast/x-fast)."""
        if self._model is None:
            self.load()

        sr = sample_rate or self.sample_rate
        clean = prepare_text_for_silero(text, stress_dict=self.stress_dict)
        if not clean or not CYRILLIC_LETTER_RE.search(clean):
            self.last_synthesis_meta = {"tts_stressed": clean, "tts_mode": "text"}
            return np.zeros(0, dtype=np.float32), 0.0

        sp = speaker if speaker in self._speakers else ("eugene" if "eugene" in self._speakers else self._speakers[0])
        preset = normalize_speed_preset(speed)

        t0 = time.perf_counter()
        import torch

        with self._lock:
            with torch.inference_mode():
                kwargs = self._build_apply_kwargs(clean, sp, sr, speed=preset)
                used_mode = "ssml" if "ssml_text" in kwargs else "text"
                used_payload = str(kwargs.get("ssml_text") or kwargs.get("text") or clean)
                try:
                    tensor = self._model.apply_tts(**kwargs)
                except Exception as exc_primary:
                    logger.warning(
                        "Silero apply_tts failed (mode=%s, preset=%s, payload=%r): %s. Trying fallback without '*'/'**'.",
                        used_mode,
                        preset,
                        used_payload[:140],
                        exc_primary,
                    )
                    plain = clean.replace("*", "")
                    kwargs_plain = self._build_apply_kwargs(plain, sp, sr, speed=preset)
                    used_mode = "ssml" if "ssml_text" in kwargs_plain else "text"
                    used_payload = str(kwargs_plain.get("ssml_text") or kwargs_plain.get("text") or plain)
                    try:
                        tensor = self._model.apply_tts(**kwargs_plain)
                    except Exception as exc_plain:
                        logger.error(
                            "Silero apply_tts fallback failed (mode=%s, preset=%s, payload=%r): %s. Falling back to text mode preset='medium'.",
                            used_mode,
                            preset,
                            used_payload[:140],
                            exc_plain,
                        )
                        kwargs_safe = self._build_apply_kwargs(plain, sp, sr, speed="medium")
                        used_mode = "text"
                        used_payload = str(kwargs_safe.get("text") or plain)
                        tensor = self._model.apply_tts(**kwargs_safe)

        self.last_synthesis_meta = {
            "tts_stressed": used_payload,
            "tts_mode": used_mode,
        }

        # Копируем в отдельный массив NumPy и сразу удаляем тензор PyTorch для освобождения памяти CPU
        audio = np.array(tensor.detach().cpu().numpy(), dtype=np.float32, copy=True).reshape(-1)
        del tensor
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return audio, elapsed_ms

    def unload(self) -> None:
        """Выгружает модель Silero из оперативной памяти при удалении пользователем локальных файлов моделей."""
        with self._lock:
            self._model = None
        gc.collect()
