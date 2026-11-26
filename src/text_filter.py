from __future__ import annotations

import collections
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rapidfuzz import fuzz

from src.config import atomic_write_json
from src.gender_detector import get_shared_morph
from src.paths import DATA_DIR, find_bundled_data_file
from src.voice_manager import clean_character_name, is_valid_character_name

logger = logging.getLogger(__name__)

SPEAKER_PATTERN = re.compile(r"^([А-ЯЁA-Z][а-яёa-zА-ЯЁA-Z0-9\-\.\s]{1,38}):\s*(.+)$")
SPEAKER_FLEX_PATTERN = re.compile(r"^([А-ЯЁа-яёA-Za-z0-9\-\.\s]{1,40})([:;])\s*(.+)$")
_LATIN_TO_CYR_HOMOGLYPHS = str.maketrans(
    "ABCEHKMOPTXYabcehkmoptxy",
    "АВСЕНКМОРТХУавсенкмортху",
)
_MIXED_WORD_RE = re.compile(r"[А-ЯЁа-яёіІјЈA-Za-z]+")
_CYR_CHAR_RE = re.compile(r"[А-ЯЁа-яё]")
_EXT_CYR_OCR_RE = re.compile(r"[іІјЈ]")
_LAT_HOMOGLYPH_RE = re.compile(r"[ABCEHKMOPTXYabcehkmoptxy]")
_UNIQUE_LATIN_OR_EXT_OCR_RE = re.compile(r"[DFGIJLNQRSUVWZdfgijlnqrsuvwzіІјЈ]")
# Кириллические двойники букв при распознавании английских слов кириллическим OCR («Тіте» -> «Time», «Јаскеrs» -> «Jackers»)
_CYR_TO_LAT_HOMOGLYPHS = str.maketrans(
    "АВСЕНКМОРТХУавсенкмортхуіІјЈ",
    "ABCEHKMOPTXYabcehkmopmxyiIjJ",
)
_CYR_COMPAT_LATIN_CHARS = frozenset("АВСЕНКМОРТХУавсенкмортхуіІјЈ")
_RU_VOWEL_RE = re.compile(r"[АЕЁИОУЫЭЮЯаеёиоуыэюя]")
_RU_CONSONANT_RE = re.compile(r"[БВГДЖЗЙКЛМНПРСТФХЦЧШЩбвгджзйклмнпрстфхцчшщ]")

_NON_SPEAKER_COLON_WORDS = {
    "значит",
    "помни",
    "слушай",
    "слушайте",
    "знаешь",
    "короче",
    "вопрос",
    "ответ",
    "главное",
    "важно",
    "например",
    "итак",
    "внимание",
    "вот",
    "смотри",
    "смотрите",
    "скажи",
    "подумай",
    "представь",
    "кстати",
    "вывод",
    "итог",
    "правило",
    "цель",
    "задача",
    "адрес",
    "время",
    "место",
}

# Известные русские аббревиатуры, которые нельзя оборачивать в **эмфазу**
_KNOWN_RU_ABBREVIATIONS = {
    "ФБР",
    "ЦРУ",
    "США",
    "МВД",
    "ФСБ",
    "КГБ",
    "ДТП",
    "КПП",
    "ПВО",
    "СМС",
    "ММС",
    "ВИП",
    "НЛО",
    "ДНК",
    "РПГ",
    "БТР",
    "БМП",
    "ВДВ",
    "ВВС",
    "ВМФ",
    "ОМОН",
    "СОБР",
    "НАТО",
    "ООН",
    "СССР",
    "ЖКХ",
    "ТТХ",
    "ПДД",
    "СМИ",
    "ЗАГС",
}

GARBAGE_LINE_PREFIX_RE = re.compile(r"^(?:[A-Za-z0-9\[\]\(\)\|]{1,3}\s+)+(?=[А-ЯЁ][а-яё])")
# Поддерживает составные имена говорящих, где 2-е/3-е слово начинается со строчной («Местный житель:», «Офицер полиции:»)
INLINE_NEXT_SPEAKER_RE = re.compile(
    r"(?<=[.!?…])\s+([А-ЯЁA-Z][а-яёa-z]{1,18}(?:\s+[А-ЯЁа-яёA-Za-z]{2,18}){0,2}):\s+"
)
CYRILLIC_LOWER_RE = re.compile(r"[а-яё]")
TIMER_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
BUTTON_PREFIX_RE = re.compile(
    r"^\s*(?:[\(\[\{]\s*[YXAByxab]\s*[\)\]\}]|(?:LB|RB|LT|RT|LS|RS|L1|R1|L2|R2)\b)\s*(.*)$"
)
UI_MENU_COUNTER_RE = re.compile(r"(?:\b\d+\s*/\s*\d+\b|[<>◀▶])\s*$")
UI_CURRENCY_ONLY_RE = re.compile(r"^\s*[\$€£]?\s*\d[\d,\.\s]*[\$€£]?\s*$")
UI_TOGGLE_ONLY_RE = re.compile(r"^\s*(?:вкл|откл|выкл)\.?\s*$", re.IGNORECASE)
_SPANISH_HOLA_OCR_RE = re.compile(r"(?<![А-ЯЁа-яёA-Za-z])(?:[¡jјi1l\|]?[Hh]ola|[¡jјi1l\|][НH]ола|Нола)(?=[!,.?\s]|$)")
_SPEECH_PUNCT_RE = re.compile(r"[.!?…,;—–]")
_CONVERSATIONAL_NO_PUNCT_WORDS = {
    "да", "нет", "ладно", "алло", "стой", "стойте", "беги", "бегите", "эй",
    "привет", "пока", "понял", "поняла", "спасибо", "извини", "прости",
    "поехали", "погнали", "вперед", "вперёд", "назад", "помогите", "спасите",
    "осторожно", "берегись", "хватит", "черт", "чёрт", "господи", "боже",
    "супер", "отлично", "хорошо", "конечно", "наверное", "точно", "верю",
    "знаю", "угу", "ага", "давай", "брось", "отвали", "заткнись", "бегом",
    "быстрее", "быстро", "тихо", "стоп", "погоди", "подожди", "слушай",
    "смотри", "иди", "жми", "гони", "тормози", "держись", "прочь", "ура",
    "вау", "ну", "ох", "ах", "уф", "ола", "адьос", "пригнись", "ложись",
    "стреляй", "огонь", "молчи", "молчать", "верно", "правда", "серьезно",
    "серьёзно", "круто", "класс", "славно", "чудесно", "прекрасно",
}
_SPEECH_POS_TAGS = {"VERB", "INFN", "NPRO", "INTJ", "PRCL", "PRED"}
MULTI_SPACE_RE = re.compile(r"\s+")
DUP_COMMA_RE = re.compile(r",\s*,+")
OCR_GARBAGE_RE = re.compile(r"[|_~`^\\]+")
BRACKETED_ANNOTATION_RE = re.compile(r"\[[^\[\]]*\]")
OCR_BROKEN_LANG_TAG_RE = re.compile(
    r"[\[\(]?\s*по-(?:испански|английски|французски|немецки|итальянски|португальски|русски|китайски|японски|корейски|арабски)\s*[\]\)]",
    re.IGNORECASE,
)
_MAP_UI_PROMPT_PHRASES = {
    "укажите пункт назначения",
    "проложить маршрут",
    "добавить метку",
}
DEDUP_STRIP_RE = re.compile(r"[^а-яёa-z0-9\s]+", re.IGNORECASE)

# Дефисное растягивание гласных внутри слов («Не-е-ет!» -> «Нет!», «Сто-о-ой!» -> «Стой!», «Быстре-ее!» -> «Быстрее!»)
DRAWN_OUT_VOWEL_WORD_RE = re.compile(
    r"\b(?!по-о|ко-о|во-о|до-о|за-а|на-а|про-о|со-о|авто-о|мото-о|фото-о|аэро-о|нео-о|гео-о|био-о|зоо-)"
    r"([А-ЯЁа-яё]*?)([аеёиоуыэюяАЕЁИОУЫЭЮЯ])(?:-\2)+([А-ЯЁа-яё]*)\b",
    re.IGNORECASE,
)

# Логическое ударение: отдельное кириллическое слово капсом (>= 3 букв), опционально с предшествующим «НЕ»/«НИ» капсом
UPPER_EMPHASIS_SPAN_RE = re.compile(
    r"(?<![А-ЯЁа-яё\*\+])((?:(?:НЕ|НИ)\s+)?[А-ЯЁ]{3,}(?:\s+[А-ЯЁ]{3,})*)(?![А-ЯЁа-яё\*\+])"
)

# Шаблоны чисел, времени, порядковых числительных и валют
THOUSANDS_COMMA_NUMBER_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
DOLLAR_MAGNITUDE_RE = re.compile(
    r"\$\s*(\d+(?:[.,]\d+)?)\s*(тыс\.?|тысяч[аи]?|млн\.?|миллион(?:а|ов)?|млрд\.?|миллиард(?:а|ов)?|[kKmMкКмМ])\b",
    re.IGNORECASE,
)
DOLLAR_PREFIX_RE = re.compile(r"\$\s*(\d+(?:[.,]\d+)?)")
DOLLAR_SUFFIX_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*\$")
EURO_PREFIX_RE = re.compile(r"€\s*(\d+(?:[.,]\d+)?)")
PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
NUMBER_SIGN_RE = re.compile(r"№\s*(\d+)")
TIME_HH_MM_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
ORDINAL_SUFFIX_RE = re.compile(
    r"\b(\d{1,4})\s*-\s*(й|ый|ий|ой|го|ого|его|му|ому|ему|м|ом|ем|ым|им|я|ая|ю|ую|е|ое|ее|ые|ие|х|ых|их|ми|ыми|ими)\b",
    re.IGNORECASE,
)
NUMBER_MAGNITUDE_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(тыс\.|тыс\b|млн\.?|млрд\.?)(?![А-ЯЁа-яё])",
    re.IGNORECASE,
)
UNIT_KMH_RE = re.compile(r"\bкм\s*/\s*ч\b", re.IGNORECASE)
UNIT_MS_RE = re.compile(r"\bм\s*/\s*с\b", re.IGNORECASE)
SLASH_24_7_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
PLAIN_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9']*?\b")

_ORDINAL_GRAM_MAP: dict[str, set[str]] = {
    "й": {"masc", "nomn"},
    "ый": {"masc", "nomn"},
    "ий": {"masc", "nomn"},
    "ой": {"masc", "nomn"},
    "го": {"masc", "gent"},
    "ого": {"masc", "gent"},
    "его": {"masc", "gent"},
    "му": {"masc", "datv"},
    "ому": {"masc", "datv"},
    "ему": {"masc", "datv"},
    "м": {"masc", "loct"},
    "ом": {"masc", "loct"},
    "ем": {"masc", "loct"},
    "ым": {"masc", "ablt"},
    "им": {"masc", "ablt"},
    "я": {"femn", "nomn"},
    "ая": {"femn", "nomn"},
    "ю": {"femn", "accs"},
    "ую": {"femn", "accs"},
    "е": {"neut", "nomn"},
    "ое": {"neut", "nomn"},
    "ее": {"neut", "nomn"},
    "ые": {"plur", "nomn"},
    "ие": {"plur", "nomn"},
    "х": {"plur", "gent"},
    "ых": {"plur", "gent"},
    "их": {"plur", "gent"},
    "ми": {"plur", "ablt"},
    "ыми": {"plur", "ablt"},
    "ими": {"plur", "ablt"},
}

_SHORT_COMMON_WORDS = {
    "да",
    "нет",
    "что",
    "кто",
    "где",
    "как",
    "там",
    "тут",
    "стой",
    "беги",
    "иди",
    "жми",
    "алло",
    "ладно",
    "понял",
    "поняла",
    "хорошо",
    "спасибо",
    "привет",
    "пока",
}


def _ru_plural(n: int, form1: str, form2: str, form5: str) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 19:
        return form5
    last = n_abs % 10
    if last == 1:
        return form1
    if 2 <= last <= 4:
        return form2
    return form5


def _num_to_ru_words(num_str: str, feminine: bool = False) -> str:
    try:
        from num2words import num2words

        normalized = num_str.replace(",", ".")
        if "." in normalized:
            return num2words(float(normalized), lang="ru")
        val_i = int(normalized)
        if val_i > 999_999_999:
            return num_str
        words = num2words(val_i, lang="ru")
        if feminine:
            tokens = words.split()
            if tokens and tokens[-1] == "один":
                tokens[-1] = "одна"
            elif tokens and tokens[-1] == "два":
                tokens[-1] = "две"
            words = " ".join(tokens)
        return words
    except Exception:
        return num_str


def _expand_ordinal_ru(num_str: str, suffix: str) -> str:
    """Expand Russian ordinal numbers like '2-го' -> 'второго', '95-му' -> 'девяносто пятому', '90-х' -> 'девяностых'."""
    try:
        from num2words import num2words

        n = int(num_str)
        if n <= 0 or n > 9999:
            return f"{_num_to_ru_words(num_str)}"
        base = num2words(n, lang="ru", to="ordinal")
        words = base.split()
        if not words:
            return base
        grams = _ORDINAL_GRAM_MAP.get(suffix.lower())
        if grams:
            morph = get_shared_morph()
            if morph is not None:
                last = words[-1]
                for p in morph.parse(last):
                    if "ADJF" in p.tag or "Anum" in p.tag:
                        inf = p.inflect(grams)
                        if inf:
                            words[-1] = inf.word
                            break
        return " ".join(words)
    except Exception:
        return _num_to_ru_words(num_str)


_EN_LETTER_NAMES = {
    "a": "эй",
    "b": "би",
    "c": "си",
    "d": "ди",
    "e": "и",
    "f": "эф",
    "g": "джи",
    "h": "эйч",
    "i": "ай",
    "j": "джей",
    "k": "кей",
    "l": "эл",
    "m": "эм",
    "n": "эн",
    "o": "оу",
    "p": "пи",
    "q": "кью",
    "r": "ар",
    "s": "эс",
    "t": "ти",
    "u": "ю",
    "v": "ви",
    "w": "дабл-ю",
    "x": "икс",
    "y": "вай",
    "z": "зед",
}

_EN_PHONETIC_RULES: tuple[tuple[str, str], ...] = (
    ("igh", "ай"),
    ("tion", "шн"),
    ("sion", "жн"),
    ("tch", "тч"),
    ("sh", "ш"),
    ("ch", "ч"),
    ("th", "т"),
    ("ph", "ф"),
    ("ck", "к"),
    ("qu", "кв"),
    ("ee", "и"),
    ("ea", "и"),
    ("oo", "у"),
    ("ou", "ау"),
    ("ow", "оу"),
    ("ai", "эй"),
    ("ay", "эй"),
    ("oi", "ой"),
    ("oy", "ой"),
    ("au", "о"),
    ("aw", "о"),
)

_EN_CHAR_MAP = str.maketrans(
    {
        "a": "а",
        "b": "б",
        "c": "к",
        "d": "д",
        "e": "е",
        "f": "ф",
        "g": "г",
        "h": "х",
        "i": "и",
        "j": "дж",
        "k": "к",
        "l": "л",
        "m": "м",
        "n": "н",
        "o": "о",
        "p": "п",
        "q": "к",
        "r": "р",
        "s": "с",
        "t": "т",
        "u": "у",
        "v": "в",
        "w": "в",
        "x": "кс",
        "y": "и",
        "z": "з",
    }
)


def _transliterate_english_token(word: str) -> str:
    """Преобразует оставшиеся английские названия брендов, радио или улиц (например 'V-Rock', 'Beach') в русскую фонетику."""
    w = word.strip("'")
    if not w:
        return ""
    if len(w) == 1:
        return _EN_LETTER_NAMES.get(w.lower(), "")
    # Короткие аббревиатуры капсом без гласных (2–4 буквы: 'NYPD', 'DJ') -> читаем по буквам
    if w.isupper() and 2 <= len(w) <= 4 and not re.search(r"[AEIOUY]", w):
        return " ".join(_EN_LETTER_NAMES.get(ch.lower(), "") for ch in w).strip()

    s = w.lower()
    # Немая конечная 'e' после согласной в словах >= 4 символов ('drive' -> 'driv', 'vice' -> 'vis')
    if s.endswith("ce") and len(s) >= 4:
        s = s[:-2] + "s"
    elif s.endswith("e") and len(s) >= 4 and s[-2] not in "aeiouy":
        s = s[:-1]

    for pat, repl in _EN_PHONETIC_RULES:
        s = s.replace(pat, repl)
    return s.translate(_EN_CHAR_MAP)


def fix_ocr_cyrillic_homoglyphs(text: str) -> str:
    """Восстанавливает смешанные латинско-кириллические OCR-слова в обе стороны:
    1. Английские слова с кириллическими двойниками ('Тіте Јаскеrs' -> 'Time Jackers')
    2. Русские слова со случайными латинскими буквами ('Пoехали' -> 'Поехали', 'cлечь' -> 'слечь')
    Чисто латинские и чисто русские слова не затрагиваются.
    """
    if not text or (not _LATIN_CHAR_RE.search(text) and not _EXT_CYR_OCR_RE.search(text)):
        return text

    def _fix_word(m: re.Match) -> str:
        tok = m.group(0)
        has_cyr = bool(_CYR_CHAR_RE.search(tok) or _EXT_CYR_OCR_RE.search(tok))
        if not has_cyr:
            return tok
        # 1. Английский токен с >=1 уникальной латинской буквой, где все кириллические символы — визуальные двойники
        if _UNIQUE_LATIN_OR_EXT_OCR_RE.search(tok):
            cyr_chars = [ch for ch in tok if ch in _CYR_COMPAT_LATIN_CHARS or ("\u0400" <= ch <= "\u04ff")]
            if cyr_chars and all(ch in _CYR_COMPAT_LATIN_CHARS for ch in cyr_chars):
                return tok.translate(_CYR_TO_LAT_HOMOGLYPHS)
        # 2. Русский токен со случайными латинскими двойниками
        if _CYR_CHAR_RE.search(tok) and _LAT_HOMOGLYPH_RE.search(tok):
            return tok.translate(_LATIN_TO_CYR_HOMOGLYPHS)
        return tok

    return _MIXED_WORD_RE.sub(_fix_word, text)


def collapse_drawn_out_vowels(text: str) -> str:
    """Схлопывает дефисные растяжения гласных внутри слов перед согласным/суффиксом
    ('Не-е-ет!' -> 'Нет!', 'Сто-о-ой!' -> 'Стой!', 'Быстре-ее!' -> 'Быстрее!', 'Помо-огите!' -> 'Помогите!'),
    чтобы акцентор SileroStress в v5_6_ru.pt не портил их на 'Не-е-ёт!' или 'Б+ыстре-е+ё!'.
    Междометия ('О-о-о...', 'А-а-а!', 'Э-э-э', 'Ну-у-у', 'Да-а-а!', 'Пф-ф-ф', 'Тс-с-с', 'Хм-м-м', 'Бр-р-р')
    и обычные дефисные слова ('по-особому') сохраняются нетронутыми.
    """
    if "-" not in text:
        return text

    def _repl_vowel(m: re.Match) -> str:
        prefix, vowel, suffix = m.group(1), m.group(2), m.group(3)
        # Сохраняем конечные тянущиеся возгласы ('Ну-у-у', 'Да-а-а!', 'О-о-о...'), где SileroStress не ошибается
        if not suffix:
            return m.group(0)
        combined = f"{prefix}{vowel}{suffix}"
        if not _RU_CONSONANT_RE.search(combined):
            return m.group(0)
        # Избегаем удвоения гласной, если суффикс уже начинается с неё ('Быстре-ее')
        if suffix[0].lower() == vowel.lower() and prefix.lower().endswith(vowel.lower()):
            return f"{prefix}{suffix}"
        return combined

    return DRAWN_OUT_VOWEL_WORD_RE.sub(_repl_vowel, text)


def normalize_ocr_text(text: str) -> str:
    """Очищает пробелы OCR, перевёрнутые испанские знаки, смешанные гомоглифы, кавычки и пунктуацию."""
    cleaned = text.replace("\r", " ").replace("\n", " ").replace("¡", "").replace("¿", "").strip()
    cleaned = _SPANISH_HOLA_OCR_RE.sub("Ола", cleaned)
    cleaned = fix_ocr_cyrillic_homoglyphs(cleaned)
    cleaned = re.sub(r"([А-ЯЁа-яёA-Za-z0-9,:;!?])«", r"\1 «", cleaned)
    cleaned = re.sub(r"»([А-ЯЁа-яёA-Za-z0-9])", r"» \1", cleaned)
    cleaned = DUP_COMMA_RE.sub(",", cleaned)
    cleaned = re.sub(r"\s+([,.:;!?…])", r"\1", cleaned)
    cleaned = MULTI_SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def strip_bracketed_annotations(text: str) -> str:
    """Удаляет ремарки и теги языка в квадратных скобках вроде '[По-испански]', '[смеётся]', '[радио]'
    (включая опечатки OCR, когда ']' распознаётся как ')' — '[По-испански)').
    """
    s = OCR_BROKEN_LANG_TAG_RE.sub(" ", text)
    for _ in range(4):
        new_s = BRACKETED_ANNOTATION_RE.sub(" ", s)
        if new_s == s:
            break
        s = new_s
    s = normalize_ocr_text(s)
    return s.strip()


def apply_logical_emphasis(text: str) -> str:
    """Преобразует выделенные КАПСОМ русские слова (>= 3 кириллических букв с хотя бы одной гласной)
    в обычных строках диалога в разметку Silero v5_6_ru **слово**.
    Пример: 'Я сказал НЕ ТРОГАЙ это' -> 'Я сказал **не** **трогай** это'.
    Аббревиатуры без гласных ('ФБР', 'КПП', 'МВД', 'ДТП') никогда не оборачиваются в **...**.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    lower_count = sum(1 for c in letters if c.islower())
    # Применяем только к обычным репликам, где преобладают строчные буквы
    if lower_count / len(letters) < 0.50:
        return text

    def _repl(m: re.Match) -> str:
        words = m.group(1).split()
        # Не оборачиваем согласные аббревиатуры ('ФБР', 'КПП') и известные сокращения ('США', 'ЦРУ')
        content_words = [w for w in words if w not in ("НЕ", "НИ")]
        if not content_words:
            return m.group(0)
        for cw in content_words:
            if cw in _KNOWN_RU_ABBREVIATIONS or not _RU_VOWEL_RE.search(cw):
                return m.group(0)
        # Silero v5_6_ru строго требует маркеры **слово** по одному слову
        return " ".join(f"**{w.lower()}**" for w in words)

    return UPPER_EMPHASIS_SPAN_RE.sub(_repl, text)


_RU_VOWELS_SET = frozenset("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")


def _stress_lookup_key(text: str) -> str:
    """Канонический ключ поиска в словаре без знаков '+' и с уравниванием 'е'/'ё'."""
    return MULTI_SPACE_RE.sub(" ", text.replace("+", "").lower().replace("ё", "е")).strip()


def _build_stress_key_fragment(key: str) -> str:
    """Строит фрагмент регулярного выражения для key независимо от уже проставленных '+' и вариантов е/ё."""
    parts: list[str] = []
    i = 0
    while i < len(key):
        ch = key[i]
        if ch == "+":
            i += 1
            continue
        if ch.isspace():
            while i < len(key) and key[i].isspace():
                i += 1
            parts.append(r"\s+")
            continue
        low = ch.lower()
        if low in ("е", "ё"):
            parts.append(r"\+?[её]")
        elif ch in _RU_VOWELS_SET:
            parts.append(r"\+?" + re.escape(ch))
        else:
            parts.append(re.escape(ch))
        i += 1
    return "".join(parts)


def _compile_combined_stress_regex(ordered_keys: list[str]) -> Optional[re.Pattern]:
    """Компилирует однопроходный regex с альтернативами от длинных к коротким, чтобы многословные фразы не ударялись дважды."""
    frags: list[str] = []
    seen: set[str] = set()
    for key in ordered_keys:
        frag = _build_stress_key_fragment(key)
        if frag and frag not in seen:
            seen.add(frag)
            frags.append(frag)
    if not frags:
        return None
    alt = "|".join(frags)
    return re.compile(rf"(?<![А-ЯЁа-яёA-Za-z0-9\+])(?:{alt})(?![А-ЯЁа-яёA-Za-z0-9\+])", re.IGNORECASE)


def _cap_first_alpha(word: str) -> str:
    chars = list(word)
    for idx, ch in enumerate(chars):
        if ch.isalpha():
            chars[idx] = ch.upper()
            break
    return "".join(chars)


def _preserve_case_replacement(matched: str, replacement: str, keep_dict_case: bool) -> str:
    """Сохраняет регистр слов из совпавшего текста, не ломая начальный '+' (например, '+Ола', 'В+айс-С+ити'),
    оставляя строчные слова в нижнем регистре, если не включён флаг keep_dict_case.
    """
    alphas = [c for c in matched if c.isalpha()]
    if not alphas:
        return replacement

    # Совпадение с заглавной буквы (например, 'Фига', 'Вайс-Сити', 'Ола', 'Ёбаный'), но НЕ аббревиатуры капсом вроде 'ФБР' / 'США' / 'OK'
    is_title_cap = alphas[0].isupper() and (len(alphas) == 1 or not all(c.isupper() for c in alphas))
    if is_title_cap:
        m_words = [w for w in re.split(r"[\s\-]+", matched) if any(c.isalpha() for c in w)]
        r_parts = re.split(r"([\s\-]+)", replacement)
        r_word_indices = [i for i, p in enumerate(r_parts) if i % 2 == 0 and any(c.isalpha() for c in p)]
        if len(m_words) > 1 and len(m_words) == len(r_word_indices):
            for mw, r_idx in zip(m_words, r_word_indices):
                mw_alphas = [c for c in mw if c.isalpha()]
                if mw_alphas and mw_alphas[0].isupper() and (len(mw_alphas) == 1 or not all(c.isupper() for c in mw_alphas)):
                    r_parts[r_idx] = _cap_first_alpha(r_parts[r_idx])
            return "".join(r_parts)
        return _cap_first_alpha(replacement)

    if alphas[0].islower() and not keep_dict_case:
        chars = list(replacement)
        for idx, ch in enumerate(chars):
            if ch.isalpha():
                chars[idx] = ch.lower()
                break
        return "".join(chars)

    return replacement


class StressDictManager:
    """Редактируемый однопроходный регистронезависимый словарь замен по границам слов (data/stress_dict.json).
    Применяется ПОСЛЕ базовой нормализации, чтобы пользовательские и сленговые переопределения
    побеждали без наложения многопроходного искажения символов '+'.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (DATA_DIR / "stress_dict.json")
        self._bundled = find_bundled_data_file("stress_dict.json")
        self._lock = threading.RLock()
        self._entries: dict[str, str] = {}
        self._lookup_map: dict[str, tuple[str, bool]] = {}
        self._latin_lookup_map: dict[str, tuple[str, bool]] = {}
        self._combined_re: Optional[re.Pattern] = None
        self._combined_latin_re: Optional[re.Pattern] = None
        self.load()

    def load(self) -> None:
        with self._lock:
            self._entries.clear()
            # Сначала объединяем встроенный словарь по умолчанию, затем накладываем пользовательский stress_dict.json, чтобы новые записи релиза всегда присутствовали
            for src in (self._bundled, self.path):
                if src and src.is_file():
                    try:
                        with open(src, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                        if isinstance(raw, dict):
                            for k, v in raw.items():
                                ks = str(k).strip()
                                vs = str(v).strip()
                                if ks and vs:
                                    self._entries[ks] = vs
                        else:
                            logger.warning("Словарь ударений %s имеет некорректный формат (ожидался JSON-объект).", src)
                    except Exception as exc:
                        logger.warning("Не удалось загрузить словарь ударений %s: %s", src, exc)
            self._rebuild_patterns()

    def _rebuild_patterns(self) -> None:
        ordered = sorted(self._entries.items(), key=lambda kv: len(kv[0]), reverse=True)
        lookup_map: dict[str, tuple[str, bool]] = {}
        latin_lookup_map: dict[str, tuple[str, bool]] = {}
        all_keys: list[str] = []
        latin_keys: list[str] = []

        for k, v in ordered:
            # У имён собственных в словаре замены с заглавной буквы (а не аббревиатуры капсом вроде 'ФБР', 'OK')
            v_alphas = [c for c in v if c.isalpha()]
            keep_dict_case = bool(v_alphas and v_alphas[0].isupper() and not all(c.isupper() for c in v_alphas))
            lk = _stress_lookup_key(k)
            if lk:
                lookup_map[lk] = (v, keep_dict_case)
                all_keys.append(k)
                if _LATIN_CHAR_RE.search(k):
                    latin_lookup_map[lk] = (v, keep_dict_case)
                    latin_keys.append(k)

        self._lookup_map = lookup_map
        self._latin_lookup_map = latin_lookup_map
        self._combined_re = _compile_combined_stress_regex(all_keys)
        self._combined_latin_re = _compile_combined_stress_regex(latin_keys)

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self._entries)

    def list_entries(self) -> dict[str, str]:
        with self._lock:
            return dict(self._entries)

    def set_entry(self, word: str, replacement: str) -> None:
        w = word.strip()
        r = replacement.strip()
        if not w or not r:
            return
        with self._lock:
            self._entries[w] = r
            self._rebuild_patterns()
            self.save()

    def delete_entry(self, word: str) -> bool:
        with self._lock:
            if word in self._entries:
                del self._entries[word]
                self._rebuild_patterns()
                self.save()
                return True
            return False

    def replace_all(self, entries: dict[str, str]) -> None:
        with self._lock:
            self._entries.clear()
            for k, v in entries.items():
                ks = str(k).strip()
                vs = str(v).strip()
                if ks and vs:
                    self._entries[ks] = vs
            self._rebuild_patterns()
            self.save()

    def apply_latin_only(self, text: str) -> str:
        """Применяет за один проход только содержащие латиницу записи словаря (например, 'Vice City', 'OK', 'FBI')."""
        if not text or not _LATIN_CHAR_RE.search(text):
            return text
        with self._lock:
            if self._combined_latin_re is None:
                return text

            def _repl(m: re.Match) -> str:
                matched = m.group(0)
                entry = self._latin_lookup_map.get(_stress_lookup_key(matched))
                if entry is None:
                    return matched
                repl, keep_dict_case = entry
                return _preserve_case_replacement(matched, repl, keep_dict_case)

            return self._combined_latin_re.sub(_repl, text)

    def apply(self, text: str) -> str:
        """Применяет все замены из словаря ударений за один непересекающийся проход регулярным выражением."""
        if not text:
            return text
        with self._lock:
            if self._combined_re is None:
                return text

            def _repl(m: re.Match) -> str:
                matched = m.group(0)
                entry = self._lookup_map.get(_stress_lookup_key(matched))
                if entry is None:
                    return matched
                repl, keep_dict_case = entry
                return _preserve_case_replacement(matched, repl, keep_dict_case)

            return self._combined_re.sub(_repl, text)


def _expand_magnitude_word(raw_num: str, mag_raw: str) -> tuple[str, bool]:
    """Возвращает кортеж (развёрнутое_число_с_разрядом, является_ли_тысячей)."""
    mag_low = mag_raw.lower().rstrip(".")
    is_thousand = mag_low in ("тыс", "тысяча", "тысячи", "тысяч", "k", "к")
    words = _num_to_ru_words(raw_num, feminine=is_thousand)
    try:
        val_f = float(raw_num.replace(",", "."))
        is_fraction = "." in raw_num.replace(",", ".") and not val_f.is_integer()
        n_int = int(val_f)
    except Exception:
        is_fraction = False
        n_int = 0

    if is_thousand:
        unit = "тысячи" if is_fraction else _ru_plural(n_int, "тысяча", "тысячи", "тысяч")
    elif mag_low in ("млрд", "миллиард", "миллиарда", "миллиардов"):
        unit = "миллиарда" if is_fraction else _ru_plural(n_int, "миллиард", "миллиарда", "миллиардов")
    else:
        unit = "миллиона" if is_fraction else _ru_plural(n_int, "миллион", "миллиона", "миллионов")
    return f"{words} {unit}", is_thousand


_OCR_DIGIT_6_RE = re.compile(
    r"(?<![А-ЯЁа-яёA-Za-z0-9.,:])([А-ЯЁа-яё]+)\s+6(?=\s+([А-ЯЁа-яё]+)(?![А-ЯЁа-яё0-9]))"
)
_CONDITIONAL_RIGHT_WORDS = frozenset(
    {
        "был", "была", "было", "были", "не", "ни",
        "мог", "могла", "могли", "стал", "стала", "стали",
        "хотел", "хотела", "хотели", "знал", "знала", "знали",
        "сделал", "сделала", "сделали", "пошел", "пошла", "пошли",
        "я", "ты", "он", "она", "оно", "мы", "вы", "они",
    }
)
_COUNTABLE_NUMBER_RIGHT_WORDS = frozenset(
    {
        "час", "часа", "часов", "минут", "минута", "минуты", "секунд", "секунда", "секунды",
        "день", "дня", "дней", "неделя", "недели", "недель", "месяц", "месяца", "месяцев",
        "год", "года", "лет", "раз", "раза", "доллар", "доллара", "долларов",
        "бакс", "бакса", "баксов", "евро", "рубль", "рубля", "рублей",
        "процент", "процента", "процентов", "пуля", "пули", "пуль",
        "патрон", "патрона", "патронов", "человек", "человека", "людей",
        "штук", "штука", "штуки", "кг", "км", "метр", "метра", "метров",
        "миля", "мили", "миль", "фут", "фута", "футов", "этаж", "этажа", "этажей",
        "мест", "место", "места", "звезд", "звезды", "шагов", "машин", "яблок", "копов",
    }
)


def _fix_ocr_digit_6_particle_b(text: str) -> str:
    """Исправляет ошибку OCR, принявшего кириллическую частицу 'б' за цифру '6' (например, 'беременная 6 была?' -> 'беременная б была?'),
    не ломая настоящие числа ('6 часов', '6 долларов', '6 пуль', 'у него 6 патронов').
    """
    if "6" not in text:
        return text

    morph = get_shared_morph()

    def _repl(m: re.Match) -> str:
        left_word = m.group(1)
        right_word = m.group(2)
        right_low = right_word.lower().replace("ё", "е")
        if right_low in _COUNTABLE_NUMBER_RIGHT_WORDS:
            return m.group(0)
        if right_low in _CONDITIONAL_RIGHT_WORDS:
            return f"{left_word} б"
        if left_word and left_word[-1] in _RU_VOWELS_SET and morph is not None:
            parses = morph.parse(right_low)[:3]
            has_num_noun = any("NOUN" in p.tag and ("gent" in p.tag or "loct" in p.tag) for p in parses)
            has_non_num_pos = any(
                p.tag.POS in {"VERB", "INFN", "ADJS", "PRTS", "PRED", "ADVB", "PRCL", "NPRO"}
                for p in parses
            )
            if has_non_num_pos and not has_num_noun:
                return f"{left_word} б"
        return m.group(0)

    return _OCR_DIGIT_6_RE.sub(_repl, text)


def normalize_text_base(text: str, stress_dict: Optional[StressDictManager] = None) -> str:
    """Базовая нормализация текста перед разметкой фокуса вопроса и применением StressDictManager:
    - очистка мусора OCR и исправление смешанных латино-кириллических гомоглифов в обе стороны
    - схлопывание дефисных растяжений гласных и согласных междометий ('Не-е-ет!' -> 'Нет!', 'Пф-ф-ф' -> 'Пф')
    - преобразование выделенных КАПСОМ слов эмфазы в разметку **слово**
    - нормализация чисел с американскими запятыми, валют, порядковых числительных, времени, единиц и OCR '6' -> 'б'
    - замена латинских записей из словаря (например, 'Vice City', 'FBI', 'OK') и транслитерация оставшейся латиницы
    """
    s = OCR_GARBAGE_RE.sub(" ", text)
    s = strip_bracketed_annotations(s)
    if not s:
        return ""

    s = collapse_drawn_out_vowels(s)
    s = apply_logical_emphasis(s)
    if stress_dict is not None:
        s = stress_dict.apply_latin_only(s)

    # 1. Удаляем запятые — разделители тысяч американского формата внутри чисел ($1,500 -> $1500, 250,000 -> 250000)
    s = THOUSANDS_COMMA_NUMBER_RE.sub(lambda m: m.group(0).replace(",", ""), s)

    # 2. Валюты с разрядностью ($500 тыс., $2 млн, $50K)
    def _replace_dollar_mag(m: re.Match) -> str:
        expanded, _ = _expand_magnitude_word(m.group(1), m.group(2))
        return f"{expanded} долларов"

    def _replace_dollars(m: re.Match) -> str:
        raw_num = m.group(1)
        words = _num_to_ru_words(raw_num)
        try:
            n_int = int(float(raw_num.replace(",", ".")))
            unit = _ru_plural(n_int, "доллар", "доллара", "долларов")
        except Exception:
            unit = "долларов"
        return f"{words} {unit}"

    def _replace_euros(m: re.Match) -> str:
        return f"{_num_to_ru_words(m.group(1))} евро"

    def _replace_percent(m: re.Match) -> str:
        raw_num = m.group(1)
        words = _num_to_ru_words(raw_num)
        try:
            n_int = int(float(raw_num.replace(",", ".")))
            unit = _ru_plural(n_int, "процент", "процента", "процентов")
        except Exception:
            unit = "процентов"
        return f"{words} {unit}"

    def _replace_time(m: re.Match) -> str:
        hh = int(m.group(1))
        mm = int(m.group(2))
        h_words = _num_to_ru_words(str(hh))
        if mm == 0:
            return f"{h_words} ноль ноль"
        if mm < 10:
            return f"{h_words} ноль {_num_to_ru_words(str(mm))}"
        return f"{h_words} {_num_to_ru_words(str(mm))}"

    s = DOLLAR_MAGNITUDE_RE.sub(_replace_dollar_mag, s)
    s = DOLLAR_PREFIX_RE.sub(_replace_dollars, s)
    s = DOLLAR_SUFFIX_RE.sub(_replace_dollars, s)
    s = EURO_PREFIX_RE.sub(_replace_euros, s)
    s = PERCENT_RE.sub(_replace_percent, s)
    s = NUMBER_SIGN_RE.sub(lambda m: f"номер {_num_to_ru_words(m.group(1))}", s)
    s = TIME_HH_MM_RE.sub(_replace_time, s)
    s = ORDINAL_SUFFIX_RE.sub(lambda m: _expand_ordinal_ru(m.group(1), m.group(2)), s)
    s = NUMBER_MAGNITUDE_RE.sub(lambda m: _expand_magnitude_word(m.group(1), m.group(2))[0], s)
    s = UNIT_KMH_RE.sub("килом+етров в час", s)
    s = UNIT_MS_RE.sub("м+етров в сек+унду", s)
    s = SLASH_24_7_RE.sub(lambda m: f"{_num_to_ru_words(m.group(1))} на {_num_to_ru_words(m.group(2))}", s)
    s = _fix_ocr_digit_6_particle_b(s)
    s = PLAIN_NUMBER_RE.sub(lambda m: _num_to_ru_words(m.group(0)), s)

    s = s.replace("$", " долларов ").replace("%", " процентов ").replace("€", " евро ")

    # 3. Транслитерируем оставшиеся необработанные английские слова ('Time Jackers' -> 'тим джакерс')
    if _LATIN_CHAR_RE.search(s):
        s = LATIN_WORD_RE.sub(lambda m: _transliterate_english_token(m.group(0)), s)

    s = MULTI_SPACE_RE.sub(" ", s).strip()
    return s


# Синхронизировано напрямую со списками WH_FORMS и LEADING_FILLERS из multi_acc_v3_package.py модели Silero v5.6
_QUESTION_SPECIAL_WORDS = frozenset(
    {
        "кто", "кого", "кому", "кем", "ком",
        "что", "чего", "чему", "чем", "чём", "че", "чё", "чо",
        "где", "куда", "откуда", "когда",
        "почему", "зачем", "как", "почём", "почем", "отчего", "насколько",
        "сколько", "скольких", "скольким", "сколькими",
        "какой", "какая", "какое", "какие", "какого", "каких", "какому", "каким", "какими", "каком", "какую",
        "чей", "чья", "чьё", "чье", "чьи", "чьего", "чьей", "чьих", "чьему", "чьим", "чьими", "чьём", "чьем", "чью",
        "который", "которая", "которое", "которые", "которого", "которой", "которых",
        "которому", "которым", "которыми", "котором", "которую",
        "каков", "какова", "каково", "каковы",
    }
)
_QUESTION_WH_LEADING_FILLERS = frozenset(
    {
        "а", "ну", "и", "так", "вот", "слышь", "слушай", "скажите", "скажи",
        "пожалуйста", "вобще", "вообще", "типа", "короче",
    }
)
_QUESTION_TAIL_TAGS = frozenset({"а", "да", "нет", "правда", "верно", "окей", "ладно"})
_QUESTION_INTRO_BEFORE_COMMA = frozenset(
    {
        "может", "значит", "кажется", "думаешь", "знаешь", "видишь",
        "пожалуй", "слушай", "смотри", "правда", "кстати", "ладно", "допустим",
    }
)
_QUESTION_SKIP_CONTENT_WORDS = frozenset(
    {
        "а", "да", "нет", "но", "и", "или", "же", "ж", "ли", "ль", "бы", "б",
        "ну", "ой", "эй", "ага", "угу", "хм", "эх", "ох", "ах", "уф",
        "в", "во", "на", "с", "со", "к", "ко", "о", "об", "обо", "у",
        "от", "до", "из", "за", "по", "под", "над", "при", "про", "для", "без", "не", "ни",
    }
)
_SINGLE_STAR_WORD_RE = re.compile(r"(?<!\*)\*[^*\s]+\*(?!\*)")
_SENTENCE_CHUNK_RE = re.compile(r"[^.!?…]+(?:[?!\.…]+)(?:\s*[»\"\)]*)?|\S.*$")
_QUESTION_END_RE = re.compile(r"(?:\?|\?!|\!\?|\?\.\.\.?)\s*[»\"\)]*$")
_TOKEN_EDGE_PUNCT_RE = re.compile(r"^([^А-ЯЁа-яёA-Za-z0-9\+\*]*)(.*?)([^А-ЯЁа-яёA-Za-z0-9\+\*]*)$")
_COPULA_BE_LEMMAS = frozenset({"быть"})
_AUX_INTERJ_PREFIX_WORDS = frozenset({"эй", "ну", "ох", "ах", "ой", "э", "ээ", "эээ", "ого"})


def _split_token_edges(tok: str) -> tuple[str, str, str]:
    m = _TOKEN_EDGE_PUNCT_RE.match(tok)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return "", tok, ""


def _is_pure_interjection(clean_word: str) -> bool:
    letters = [c for c in clean_word if c.isalpha()]
    if not letters:
        return True
    if not _RU_CONSONANT_RE.search(clean_word):
        return True
    return clean_word.replace("-", "") in _AUX_INTERJ_PREFIX_WORDS


def _has_clause_predicate(tokens: list[dict[str, Any]], morph: Any) -> bool:
    if morph is None:
        return False
    predicate_pos = {"VERB", "INFN", "ADJS", "PRTS", "PRED"}
    for tk in tokens:
        w = tk["clean"]
        if not w or w in _QUESTION_WH_LEADING_FILLERS:
            continue
        parses = morph.parse(w)[:2]
        if parses and parses[0].tag.POS in predicate_pos:
            return True
    return False


def _is_wh_special_question(cyr_tokens: list[dict[str, Any]], morph: Any = None) -> bool:
    """Повторяет логику функции `_has_wh_prefix()` из Silero v5.6: проверяет первые 4 значащих слова
    открывающей клаузы (останавливаясь на запятой после слов) и начало клаузы сразу после
    начального обращения/вводного слова без глагола-сказуемого (например, 'Так, народ, что закажете?'),
    чтобы придаточные вопросы вроде 'Ты знаешь, что случилось вчера?' сохраняли общий фокус.
    """
    content: list[str] = []
    for t in cyr_tokens[:8]:
        w = t["clean"]
        if w in _QUESTION_WH_LEADING_FILLERS:
            if "," in t["trail"] and content:
                break
            continue
        content.append(w)
        if len(content) >= 4 or "," in t["trail"]:
            break
    if any(w in _QUESTION_SPECIAL_WORDS for w in content):
        return True
    if morph is None:
        morph = get_shared_morph()
    # Также проверяем начало сразу после вводной запятой/обращения, если до неё нет главного сказуемого
    for pos, t in enumerate(cyr_tokens[:-1]):
        if "," in t["trail"] and pos <= 2:
            if _has_clause_predicate(cyr_tokens[: pos + 1], morph):
                break
            after = [
                tk["clean"]
                for tk in cyr_tokens[pos + 1 : pos + 6]
                if tk["clean"] not in _QUESTION_WH_LEADING_FILLERS
            ][:3]
            if after and after[0] in _QUESTION_SPECIAL_WORDS:
                return True
    return False


def _mark_single_question_sentence(sentence: str) -> str:
    stripped = sentence.rstrip()
    if not stripped or not _QUESTION_END_RE.search(stripped):
        return sentence
    if _SINGLE_STAR_WORD_RE.search(stripped):
        return sentence

    matches = list(re.finditer(r"\S+", sentence))
    if not matches:
        return sentence

    tokens_info: list[dict[str, Any]] = []
    for idx, m in enumerate(matches):
        raw_tok = m.group(0)
        lead, core, trail = _split_token_edges(raw_tok)
        clean = core.replace("*", "").replace("+", "").lower()
        has_cyr = bool(_CYR_CHAR_RE.search(core))
        tokens_info.append(
            {
                "idx": idx,
                "start": m.start(),
                "end": m.end(),
                "raw": raw_tok,
                "lead": lead,
                "core": core,
                "trail": trail,
                "clean": clean,
                "has_cyr": has_cyr,
                "is_emph": "**" in raw_tok,
            }
        )

    cyr_tokens = [t for t in tokens_info if t["has_cyr"] and t["clean"]]
    if not cyr_tokens:
        return sentence

    # Специальные вопросы (с вопросительным словом) совпадают с правилом wh_q в Silero v5.6 -> не ставим *слово*
    if _is_wh_special_question(cyr_tokens):
        return sentence

    # Альтернативные вопросы ('Ты пойдешь или останешься?') совпадают с правилом alternative_q в Silero v5.6 -> оставляем нативный баланс
    for pos, t in enumerate(cyr_tokens):
        if t["clean"] == "или" and 0 < pos < len(cyr_tokens) - 1:
            return sentence

    active_end = len(tokens_info)

    # 1. Игнорируем хвостовые переспросы вроде ', а?', ', да?', ', нет?', ', правда?'
    last_cyr = cyr_tokens[-1]
    if last_cyr["idx"] > 0 and last_cyr["clean"] in _QUESTION_TAIL_TAGS:
        prev_tok = tokens_info[last_cyr["idx"] - 1]
        if any(ch in prev_tok["trail"] for ch in (",", "-", "–", "—")):
            if any(t["has_cyr"] for t in tokens_info[: last_cyr["idx"]]):
                active_end = last_cyr["idx"]

    morph = get_shared_morph()

    # 2. Игнорируем обращения в конце после последней запятой (например, 'Все хорошо, мистер Уокер?')
    comma_idx = -1
    for i in range(active_end - 1):
        if "," in tokens_info[i]["trail"]:
            comma_idx = i
    if comma_idx >= 0:
        prefix_cyr = [t for t in tokens_info[: comma_idx + 1] if t["has_cyr"]]
        tail_cyr = [t for t in tokens_info[comma_idx + 1 : active_end] if t["has_cyr"]]
        if prefix_cyr and 1 <= len(tail_cyr) <= 3:
            has_meaningful_prefix = any(not _is_pure_interjection(t["clean"]) for t in prefix_cyr)
            if has_meaningful_prefix and morph is not None:
                tail_is_vocative = True
                for t in tail_cyr:
                    if t["clean"] in _QUESTION_TAIL_TAGS:
                        continue
                    parses = morph.parse(t["clean"])[:3]
                    if any(
                        p.is_known and p.tag.POS in {"VERB", "INFN", "ADJS", "PRTS", "PRED", "NPRO", "CONJ", "PREP", "ADVB"}
                        for p in parses
                    ):
                        tail_is_vocative = False
                        break
                    if not any(
                        p.tag.POS in {"NOUN", "ADJF"} or ("Name" in p.tag) or ("Surn" in p.tag) or t["core"][:1].isupper()
                        for p in parses
                    ):
                        tail_is_vocative = False
                        break
                if tail_is_vocative:
                    active_end = comma_idx + 1

    scope = [t for t in tokens_info[:active_end] if t["has_cyr"] and t["clean"]]
    if not scope:
        scope = cyr_tokens

    chosen: Optional[dict[str, Any]] = None

    # 3. Частица 'ли' / 'ль' -> фокус вопроса ставится на слово непосредственно перед ней
    for pos, t in enumerate(scope):
        if t["clean"] in ("ли", "ль") and pos > 0:
            prev_t = scope[pos - 1]
            if not prev_t["is_emph"] and prev_t["has_cyr"]:
                chosen = prev_t
                break

    # 4. Иначе выбираем главное сказуемое (VERB > ADJS/PRTS/PRED > INFN) через pymorphy3
    if chosen is None and morph is not None:
        best_rank = 0
        for pos, t in enumerate(scope):
            if t["is_emph"]:
                continue
            clean = t["clean"]
            # Пропускаем вводные глаголы в начале предложения до запятой (например, 'Может, повторишь, а?')
            if pos == 0 and "," in t["trail"] and clean in _QUESTION_INTRO_BEFORE_COMMA and len(scope) > 1:
                continue

            parses = morph.parse(clean)
            if not parses:
                continue
            top_score = parses[0].score
            rank = 0
            for p in parses[:4]:
                if p.score < max(0.08, top_score * 0.20):
                    continue
                pos_tag = p.tag.POS
                if pos_tag == "VERB":
                    # Понижаем приоритет связки 'быть' ниже кратких прилагательных/предикативов ('Все было хорошо?')
                    cand_rank = 1 if p.normal_form in _COPULA_BE_LEMMAS else 3
                elif pos_tag in ("ADJS", "PRTS", "PRED") and clean not in ("да", "нет"):
                    if pos_tag == "ADJS" and parses[0].tag.POS == "NOUN" and parses[0].score > 0.85 and clean != "прав":
                        cand_rank = 0
                    else:
                        cand_rank = 2
                elif pos_tag == "INFN":
                    cand_rank = 1
                else:
                    cand_rank = 0
                if cand_rank > rank:
                    rank = cand_rank
            if rank > best_rank:
                best_rank = rank
                chosen = t

    # 5. Резервный вариант для безглагольных общих вопросов ('это ты мне?', 'Все путем?') -> последнее значащее слово
    if chosen is None:
        for t in reversed(scope):
            if t["is_emph"]:
                continue
            if t["clean"] in _QUESTION_SKIP_CONTENT_WORDS or _is_pure_interjection(t["clean"]):
                continue
            chosen = t
            break
    if chosen is None:
        for t in reversed(scope):
            if not t["is_emph"]:
                chosen = t
                break

    if chosen is None or not chosen["core"] or "*" in chosen["core"]:
        return sentence

    wrapped = f"{chosen['lead']}*{chosen['core']}*{chosen['trail']}"
    return sentence[: chosen["start"]] + wrapped + sentence[chosen["end"] :]


def mark_question_focus(text: str) -> str:
    """Ставит официальный маркер интонационного фокуса Silero v5.6 '*слово*' в общих вопросах без вопросительного слова ('?'),
    не затрагивая специальные вопросы ('кто', 'что', 'где', 'как' и т.д.).
    """
    if not text or "?" not in text:
        return text

    out_parts: list[str] = []
    last_end = 0
    for m in _SENTENCE_CHUNK_RE.finditer(text):
        if m.start() > last_end:
            out_parts.append(text[last_end : m.start()])
        out_parts.append(_mark_single_question_sentence(m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        out_parts.append(text[last_end:])
    return "".join(out_parts)


def normalize_text_for_tts(
    text: str,
    stress_dict: Optional[StressDictManager] = None,
) -> str:
    """Строгий конвейер нормализации текста для синтеза речи:
    1. normalize_text_base() — очистка OCR, гомоглифы, растяжки букв, числа/валюты/время, транслитерация, **эмфаза**
    2. StressDictManager.apply() — однопроходная подстановка словаря data/stress_dict.json
    3. mark_question_focus() — маркер вопросительной интонации '*слово*' на общих вопросах
    """
    s = normalize_text_base(text, stress_dict=stress_dict)
    if not s:
        return ""
    if stress_dict is not None:
        s = stress_dict.apply(s)
    s = mark_question_focus(s)
    return s


def _dedup_canonical(text: str) -> str:
    s = text.lower().replace("ё", "е")
    s = DEDUP_STRIP_RE.sub(" ", s)
    return MULTI_SPACE_RE.sub(" ", s).strip()


@dataclass
class FilterResult:
    accepted: bool
    reason: str
    raw_text: str
    cleaned_text: str = ""
    speaker_name: Optional[str] = None
    speech_text: str = ""
    similarity: float = 0.0


def strip_garbage_prefix(text: str) -> str:
    """Удаляет начальный мусор OCR вроде 'RK Рэнди: Славно.' -> 'Рэнди: Славно.'."""
    return GARBAGE_LINE_PREFIX_RE.sub("", text).strip()


def _fix_ocr_name_homoglyphs(candidate: str) -> str:
    """Исправляет частую путаницу кириллицы/латиницы в русских именах говорящих (например, 'Бpaйaн' -> 'Брайан')."""
    if re.search(r"[А-ЯЁа-яё]", candidate) and re.search(r"[A-Za-z]", candidate):
        return candidate.translate(_LATIN_TO_CYR_HOMOGLYPHS)
    return candidate


def _normalize_speaker_case(name: str) -> str:
    """Приводит имена говорящих КАПСОМ ('БРАЙАН') к виду с заглавной буквы ('Брайан') для аккуратного отображения в таблице."""
    letters = [c for c in name if c.isalpha()]
    if len(letters) >= 2 and all(c.isupper() for c in letters):
        return name.title()
    return name


def _extract_speaker_with_flag(text: str) -> tuple[Optional[str], str, bool]:
    """Внутренний помощник, возвращающий (имя_говорящего_или_None, чистая_речь, был_ли_префикс_говорящего).
    Гарантирует, что обрезанные OCR-префиксы вроде 'райан: Привет', 'ян: Эй!', 'естный житель: Паскуда!'
    или 'Брайан; Алло' ВСЕГДА удаляются из текста речи, даже если фрагмент имени слишком повреждён для сохранения.
    """
    sanitized = strip_garbage_prefix(normalize_ocr_text(text))
    if not sanitized:
        return None, "", False

    candidates_to_try = [sanitized]
    without_brackets = strip_garbage_prefix(strip_bracketed_annotations(sanitized))
    if without_brackets and without_brackets != sanitized:
        candidates_to_try.append(without_brackets)

    for cand in candidates_to_try:
        # 1. Стандартный формат 'Имя с заглавной: реплика' (до 4 слов / 39 символов, например, 'Сотрудник береговой охраны:')
        m = SPEAKER_PATTERN.match(cand)
        if m:
            raw_left = _normalize_speaker_case(_fix_ocr_name_homoglyphs(clean_character_name(m.group(1))))
            speech = strip_bracketed_annotations(m.group(2))
            words = raw_left.split()
            if words and words[0].lower() not in _NON_SPEAKER_COLON_WORDS and len(words) <= 4:
                if is_valid_character_name(raw_left):
                    return raw_left, speech, True
                # Повреждённый/смешанный префикс перед ':' (например, '2Б: Привет') -> удаляем префикс, чтобы он НИКОГДА не озвучивался!
                if speech:
                    return None, speech, True

        # 2. Гибкий запасной вариант OCR: обрезанное имя со строчной ('райан: Привет', 'естный житель: Паскуда!') или ';' вместо ':'
        mf = SPEAKER_FLEX_PATTERN.match(cand)
        if mf:
            raw_left = _fix_ocr_name_homoglyphs(clean_character_name(mf.group(1)))
            sep = mf.group(2)
            speech = strip_bracketed_annotations(mf.group(3))
            words = raw_left.split()
            if not raw_left or not speech or len(words) > 4:
                continue
            # Никогда не путаем с временем вроде '12:30' или обычными вводными словами ('Слушай: ...')
            if raw_left.isdigit() or (words[0].lower() in _NON_SPEAKER_COLON_WORDS):
                continue
            if sep == ";":
                if raw_left[0].isupper() and is_valid_character_name(raw_left) and (
                    speech[0].isupper() or speech[0] in ("-", "—", "«", '"')
                ):
                    return _normalize_speaker_case(raw_left), speech, True
                # Обрезанное имя говорящего со строчной перед ';', за которым следует реплика с заглавной ('райан; Привет!')
                if not raw_left[0].isupper() and len(words) <= 3 and len(raw_left) <= 28 and speech[0].isupper():
                    return None, speech, True
            else:
                # Обрезанный фрагмент говорящего перед ':' (строчная первая буква или обрывок в 1 букву вроде 'н: Привет')
                if not raw_left[0].isupper() or len([c for c in raw_left if c.isalpha()]) < 2:
                    if len(words) <= 3 and len(raw_left) <= 28:
                        return None, speech, True

    return None, strip_bracketed_annotations(sanitized), False


def _split_inline_speakers(line: str) -> list[str]:
    parts: list[str] = []
    last_idx = 0
    for m in INLINE_NEXT_SPEAKER_RE.finditer(line):
        candidate_spk = m.group(1).strip()
        first_word = candidate_spk.split()[0].lower() if candidate_spk else ""
        if first_word in _NON_SPEAKER_COLON_WORDS:
            continue
        split_at = m.start(1)
        left = line[last_idx:split_at].strip()
        if left:
            parts.append(left)
        last_idx = split_at
    tail = line[last_idx:].strip()
    if tail:
        parts.append(tail)
    return parts or [line]


def _is_ui_menu_option(line: str) -> bool:
    """Возвращает True, если строка является пунктом/переключателем меню настроек ('Вкл. 3 / 3 <', 'Откл.', '$3,334'), а не репликой."""
    s = line.strip()
    if not s:
        return True
    if UI_CURRENCY_ONLY_RE.match(s) or UI_TOGGLE_ONLY_RE.match(s):
        return True
    if UI_MENU_COUNTER_RE.search(s) and len(s.split()) <= 5:
        return True
    return False


def _is_non_speech_label(text: str) -> bool:
    """Слой 4 лингвистического фильтра (независимый от координат и цвета):
    Отсеивает голые названия районов карты ('Рокридж', 'Кросстаун', 'Пейнос Плаза', 'Маленькая Куба', 'Деловой район'),
    заголовки меню настроек ('Субтитры', 'Скрытые субтитры', 'Размер', 'Имена говорящих', 'Фон')
    и имена на плашках звонков ('Мия', 'Брайан'), если нет префикса 'Имя:' и речевой пунктуации.
    Настоящие короткие реплики ('Да', 'Стой', 'Погнали', 'Ты куда') проходят свободно.
    """
    s = text.strip()
    if not s:
        return True
    if _dedup_canonical(s) in _MAP_UI_PROMPT_PHRASES:
        return True
    if _SPEECH_PUNCT_RE.search(s):
        return False
    words = re.findall(r"[А-ЯЁа-яё]+", s)
    if not words:
        return True
    if len(words) > 3:
        return False

    low_words = [w.lower() for w in words]
    if any(w in _CONVERSATIONAL_NO_PUNCT_WORDS for w in low_words):
        return False

    morph = get_shared_morph()
    if morph is not None:
        # Многословная фраза с заглавных букв без пунктуации ('Остров Олдби', 'Пейнос Плаза', 'Маленькая Куба') -> название на карте / имя собственное
        if len(words) >= 2 and any(w[0].isupper() for w in words[1:]):
            has_known_verb = False
            for w in low_words:
                for p in morph.parse(w)[:2]:
                    if p.is_known and p.tag.POS in ("VERB", "INFN"):
                        has_known_verb = True
                        break
                if has_known_verb:
                    break
            if not has_known_verb:
                return True

        for w in low_words:
            parses = morph.parse(w)
            if not parses:
                continue
            # Доверяем частям речи ТОЛЬКО для известных словарных слов (p.is_known=True), чтобы незнакомые иностранные названия
            # вроде 'Олдби' НИКОГДА не распознавались pymorphy3 ошибочно как русские глаголы!
            top_score = parses[0].score
            for p in parses[:3]:
                if p.score < top_score * 0.35:
                    break
                if p.is_known and p.tag.POS in _SPEECH_POS_TAGS:
                    return False
    # 1–3 слова, без префикса говорящего, без пунктуации, без известных глаголов/местоимений/междометий -> название места на карте / метка интерфейса
    return True


def _is_standalone_ui_noise_line(line: str) -> bool:
    """Возвращает True, если строка без говорящего — очевидная подсказка UI, таймер, пункт меню или заголовок КАПСОМ,
    чтобы она НИКОГДА не приклеивалась к концу реплики предыдущего персонажа.
    """
    if BUTTON_PREFIX_RE.match(line) or _is_ui_menu_option(line):
        return True
    if TIMER_RE.search(line):
        without_timer = TIMER_RE.sub("", line).strip(" :-|•—–")
        if len(without_timer.split()) <= 2:
            return True
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return True
    if not CYRILLIC_LOWER_RE.search(line) and len(letters) >= 3:
        return True
    return False


_DANGLING_SPEAKER_RE = re.compile(r"^([А-ЯЁа-яёA-Za-z0-9\-\.\s]{1,40})([:;])\s*$")


def _extract_dangling_speaker_prefix(line: str) -> tuple[Optional[str], bool]:
    """Определяет отдельно стоящий заголовок говорящего, чей текст реплики был разбит или утерян (например, 'Местные жители:').
    Возвращает (валидное_имя_или_None, является_ли_префиксом_говорящего).
    """
    m = _DANGLING_SPEAKER_RE.match(line.strip())
    if not m:
        return None, False
    raw_name = _fix_ocr_name_homoglyphs(clean_character_name(m.group(1)))
    words = raw_name.split()
    if not raw_name or raw_name.isdigit() or len(words) > 4:
        return None, False
    if words[0].lower() in _NON_SPEAKER_COLON_WORDS:
        return None, False
    norm = _normalize_speaker_case(raw_name)
    if raw_name[0].isupper() and is_valid_character_name(norm):
        return norm, True
    if not raw_name[0].isupper() or len([c for c in raw_name if c.isalpha()]) < 2:
        if len(words) <= 3 and len(raw_name) <= 28:
            return None, True
    return None, False


def parse_subtitle_utterances(lines: list[str]) -> list[tuple[Optional[str], str, str]]:
    """Разбирает упорядоченные сверху вниз визуальные строки OCR в последовательность реплик."""
    expanded_lines: list[str] = []
    for raw in lines:
        cleaned = strip_garbage_prefix(strip_bracketed_annotations(normalize_ocr_text(raw)))
        if not cleaned:
            continue
        expanded_lines.extend(_split_inline_speakers(cleaned))

    if not expanded_lines:
        return []

    utterances: list[list] = []
    pending_speaker: Optional[str] = None
    pending_anon_prefix = False
    for line in expanded_lines:
        dangling_spk, is_dangling = _extract_dangling_speaker_prefix(line)
        if is_dangling:
            pending_speaker = dangling_spk
            pending_anon_prefix = dangling_spk is None
            continue

        speaker, speech, had_prefix = _extract_speaker_with_flag(line)
        if speaker is not None:
            pending_speaker = None
            pending_anon_prefix = False
            if speech:
                utterances.append([speaker, speech, f"{speaker}: {speech}"])
        elif had_prefix:
            pending_speaker = None
            pending_anon_prefix = False
            if speech:
                utterances.append([None, speech, speech])
        elif pending_speaker is not None:
            spk = pending_speaker
            pending_speaker = None
            pending_anon_prefix = False
            utterances.append([spk, line, f"{spk}: {line}"])
        elif pending_anon_prefix:
            pending_speaker = None
            pending_anon_prefix = False
            utterances.append([None, line, line])
        elif utterances and not _is_standalone_ui_noise_line(line):
            utterances[-1][1] = f"{utterances[-1][1]} {line}".strip()
            utterances[-1][2] = f"{utterances[-1][2]} {line}".strip()
        else:
            utterances.append([None, line, line])

    return [(u[0], u[1], u[2]) for u in utterances]


class SubtitleTextFilter:
    """Субмиллисекундный фильтр, отсеивающий подсказки действий, таймеры интерфейса и дублирующиеся/обрезанные субтитры."""

    def __init__(
        self,
        caps_ratio_threshold: float = 0.72,
        dedup_threshold: float = 82.0,
        history_size: int = 8,
        history_ttl_sec: float = 18.0,
    ) -> None:
        self.caps_ratio_threshold = caps_ratio_threshold
        self.dedup_threshold = dedup_threshold
        self.history_ttl_sec = history_ttl_sec
        self._lock = threading.Lock()
        # Скользящая история кортежей (канонический_текст, говорящий_в_нижнем_регистре_или_пусто, метка_времени)
        self._recent_spoken: collections.deque[tuple[str, str, float]] = collections.deque(maxlen=history_size)

    def reset_dedup(self) -> None:
        with self._lock:
            self._recent_spoken.clear()

    def mark_spoken(self, text: str, speaker: Optional[str] = None) -> None:
        key = _dedup_canonical(text)
        if not key:
            return
        spk = (speaker or "").strip().lower()
        now = time.monotonic()
        with self._lock:
            for idx, (prev_key, prev_spk, _ts) in enumerate(self._recent_spoken):
                if prev_key == key and prev_spk == spk:
                    self._recent_spoken[idx] = (key, spk, now)
                    return
            self._recent_spoken.append((key, spk, now))

    def _strip_already_spoken_prefix(
        self,
        speech_text: str,
        speaker_name: Optional[str] = None,
    ) -> tuple[str, bool]:
        """Если speech_text продолжает недавно озвученную фразу того же говорящего (например, ниже уже озвученной
        1-й строки появилась 2-я строка субтитров: 'А-ах, да как!' -> 'А-ах, да как! Ладно.'),
        обрезает уже озвученный префикс и возвращает ТОЛЬКО добавленный хвост, чтобы:
        1) Новая 2-я строка НИКОГДА не проглатывалась нечёткой дедупликацией (сходство 83–96%);
        2) Уже озвученная 1-я строка НИКОГДА не читалась повторно; и
        3) Короткие возгласы ('Эй!', 'Да!', 'Нет!') или реплики ДРУГОГО известного говорящего НИКОГДА не обрезали новые реплики.
        """
        cur_words = speech_text.split()
        if len(cur_words) < 2:
            return speech_text, False

        full_canon = _dedup_canonical(speech_text)
        if not full_canon:
            return speech_text, False

        cur_spk = (speaker_name or "").strip().lower()
        now = time.monotonic()
        with self._lock:
            snapshot = list(self._recent_spoken)

        # Если вся реплика целиком УЖЕ была озвучена (точное совпадение или >= 94%), никогда не обрезаем её!
        for prev_key, prev_spk, ts in snapshot:
            if (now - ts) > self.history_ttl_sec or not prev_key:
                continue
            if prev_spk and cur_spk and prev_spk != cur_spk:
                continue
            if full_canon == prev_key or (len(prev_key) >= 10 and float(fuzz.ratio(full_canon, prev_key)) >= 94.0):
                return speech_text, False

        best_cut_idx = 0
        best_prev_len = 0
        for prev_key, prev_spk, ts in snapshot:
            if (now - ts) > 10.0 or not prev_key:
                continue
            # Никогда не обрезаем строку префиксом, который произнёс ДРУГОЙ известный говорящий
            if prev_spk and cur_spk and prev_spk != cur_spk:
                continue
            # Требуем минимум 2 слова ИЛИ >= 8 символов, чтобы короткие выкрики ('Эй!', 'Да!', 'Нет!') не отрезали начало следующих строк
            if len(prev_key.split()) < 2 and len(prev_key) < 8:
                continue
            # Рассматриваем в качестве префиксов только действительно более короткие ранее озвученные строки
            if len(full_canon) < len(prev_key) + 3:
                continue
            # Сканируем исходные префиксы токенов по пробелам, чтобы слова с дефисом ('Да-да.', 'А-ах,')
            # сопоставлялись 1:1 с канонизированным prev_key без смещений на один индекс.
            for k in range(1, len(cur_words)):
                prefix_canon = _dedup_canonical(" ".join(cur_words[:k]))
                if not prefix_canon:
                    continue
                if len(prefix_canon) > len(prev_key) + 6:
                    break
                if prefix_canon == prev_key or (
                    len(prev_key) >= 8
                    and abs(len(prefix_canon) - len(prev_key)) <= max(3, int(len(prev_key) * 0.15))
                    and float(fuzz.ratio(prefix_canon, prev_key)) >= 90.0
                ):
                    if len(prev_key) > best_prev_len:
                        best_prev_len = len(prev_key)
                        best_cut_idx = k
                    break

        if best_cut_idx <= 0:
            return speech_text, False

        tail = " ".join(cur_words[best_cut_idx:]).strip()
        tail_letters = [c for c in tail if c.isalpha()]
        if len(tail_letters) < 2 or not CYRILLIC_LOWER_RE.search(tail):
            return speech_text, False
        return tail, True

    def check_duplicate(self, text: str, speaker: Optional[str] = None) -> tuple[bool, float]:
        key = _dedup_canonical(text)
        if not key:
            return True, 100.0

        cur_spk = (speaker or "").strip().lower()
        is_short_phrase = len(key) <= 16
        eff_ttl = min(self.history_ttl_sec, 5.5) if is_short_phrase else self.history_ttl_sec

        now = time.monotonic()
        best_sim = 0.0
        with self._lock:
            snapshot = list(self._recent_spoken)
        for prev_key, prev_spk, ts in snapshot:
            age = now - ts
            if age > eff_ttl:
                continue
            # Разные известные говорящие могут вполне естественно произносить одинаковые короткие приветствия/ответы ('Привет!', 'Да!', 'Спасибо!')
            different_known_speaker = bool(cur_spk and prev_spk and cur_spk != prev_spk)
            if different_known_speaker:
                if is_short_phrase or age > 3.5:
                    continue

            ratio = float(fuzz.ratio(key, prev_key))
            if ratio > best_sim:
                best_sim = ratio
            if ratio >= self.dedup_threshold:
                return True, ratio

            min_len = min(len(key), len(prev_key))
            max_len = max(len(key), len(prev_key))
            if min_len >= 14 and (min_len / max(1, max_len)) >= 0.70:
                p_ratio = float(fuzz.partial_ratio(key, prev_key))
                if p_ratio >= 92.0:
                    return True, max(ratio, p_ratio)

            # Защита от обрывков начала/конца строки: если 2-строчный субтитр только что был озвучен целиком
            # (например, 'фига ты слечь что ли хочешь'), а в течение 7 с OCR прочитал только его 2-ю строку ('хочешь'),
            # подавляем дублирующийся фрагмент, чтобы хвостовое слово не озвучилось повторно.
            if age <= 7.0 and len(prev_key) >= len(key) + 6 and len(key) >= 5 and key not in _SHORT_COMMON_WORDS:
                if prev_key.endswith(" " + key) or prev_key.startswith(key + " "):
                    return True, 95.0

            # Защита от мерцания перекрытого слова у ОДНОГО И ТОГО ЖЕ известного говорящего в течение 2.5 с:
            # если OCR увидел 'Прекрати. Стой!', а через 0.3 с среднее слово оказалось перекрыто и осталось 'Стой!',
            # подавляем даже короткое частое слово начала/конца у того же говорящего на том же кадре субтитров.
            same_known_speaker = bool(cur_spk and prev_spk and cur_spk == prev_spk)
            if age <= 2.5 and same_known_speaker and len(prev_key) >= len(key) + 4 and len(key) >= 3:
                if prev_key.endswith(" " + key) or prev_key.startswith(key + " "):
                    return True, 95.0

        return False, best_sim

    def filter(self, raw_text: str, update_dedup: bool = True) -> FilterResult:
        cleaned = strip_garbage_prefix(normalize_ocr_text(raw_text))
        if not cleaned:
            return FilterResult(False, "empty", raw_text, cleaned)

        # 1. Префикс кнопки геймпада + подсказка КАПСОМ (проверяем ДО удаления квадратных скобок [...])
        btn_match = BUTTON_PREFIX_RE.match(cleaned)
        if btn_match:
            rest = btn_match.group(1).strip()
            rest_letters = [c for c in rest if c.isalpha()]
            if not rest_letters:
                return FilterResult(False, "ui_button_prompt", raw_text, cleaned)
            upper_rest = sum(1 for c in rest_letters if c.isupper()) / len(rest_letters)
            if upper_rest >= self.caps_ratio_threshold or not CYRILLIC_LOWER_RE.search(rest):
                return FilterResult(False, "ui_button_prompt", raw_text, cleaned)

        # 1b. Удаляем любые ремарки в квадратных скобках вроде '[По-испански]', '[смеётся]', '[по рации]'
        cleaned_no_brackets = strip_bracketed_annotations(cleaned)
        if not cleaned_no_brackets:
            return FilterResult(False, "bracket_only", raw_text, cleaned)
        if cleaned_no_brackets.endswith(":") and cleaned_no_brackets.count(":") == 1:
            return FilterResult(False, "bracket_only", raw_text, cleaned_no_brackets)
        cleaned = cleaned_no_brackets

        # 2. Проверка числа букв (допускаем 2-буквенные кириллические реплики вроде 'Да.', 'Ну?', 'Эй!')
        letters = [c for c in cleaned if c.isalpha()]
        if len(letters) < 2:
            return FilterResult(False, "too_short", raw_text, cleaned)

        # 3. Проверка на таймер / плашку звонка / переключатель меню настроек UI
        if _is_ui_menu_option(cleaned):
            return FilterResult(False, "ui_menu_option", raw_text, cleaned)
        if TIMER_RE.search(cleaned):
            without_timer = TIMER_RE.sub("", cleaned).strip(" :-|•—–")
            words_without_timer = [w for w in without_timer.split() if any(ch.isalpha() for ch in w)]
            if len(words_without_timer) <= 2 and ":" not in without_timer:
                return FilterResult(False, "ui_timer", raw_text, cleaned)

        # 4. Проверка доли КАПСА и наличия строчной кириллицы (отсеивает подсказки действий Xbox/PS/PC в верхнем регистре)
        upper_count = sum(1 for c in letters if c.isupper())
        caps_ratio = upper_count / len(letters)
        if caps_ratio >= self.caps_ratio_threshold:
            return FilterResult(False, f"caps_ratio({caps_ratio:.2f})", raw_text, cleaned)
        if not CYRILLIC_LOWER_RE.search(cleaned):
            return FilterResult(False, "no_lowercase_cyrillic", raw_text, cleaned)

        # 5. Извлекаем чистое имя говорящего и текст самой реплики
        speaker_name, speech_text, had_speaker_prefix = _extract_speaker_with_flag(cleaned)
        speech_letters = [c for c in speech_text if c.isalpha()]
        if len(speech_letters) < 2:
            return FilterResult(False, "speech_too_short", raw_text, cleaned)

        speech_upper_ratio = sum(1 for c in speech_letters if c.isupper()) / len(speech_letters)
        if speech_upper_ratio >= self.caps_ratio_threshold or not CYRILLIC_LOWER_RE.search(speech_text):
            return FilterResult(False, f"speech_caps({speech_upper_ratio:.2f})", raw_text, cleaned)

        # 5b. Слой 4 лингвистического фильтра: отсеиваем голые названия мест карты, заголовки меню и имена плашек звонков
        if not had_speaker_prefix and _is_non_speech_label(speech_text):
            return FilterResult(False, "non_speech_label", raw_text, cleaned)

        # 6. Отрезание уже озвученного префикса (когда под 1-й строкой субтитров появилась 2-я строка)
        full_speech_text = speech_text
        stripped_tail, was_prefix_stripped = self._strip_already_spoken_prefix(
            speech_text,
            speaker_name=speaker_name,
        )
        if was_prefix_stripped:
            speech_text = stripped_tail
            # Защита: проверяем, что выделенный хвост сам по себе не был только что озвучен
            tail_dup, tail_sim = self.check_duplicate(speech_text, speaker=speaker_name)
            if tail_dup:
                if update_dedup:
                    self.mark_spoken(full_speech_text, speaker=speaker_name)
                return FilterResult(
                    False,
                    f"duplicate({tail_sim:.0f}%)",
                    raw_text,
                    cleaned,
                    speaker_name=speaker_name,
                    speech_text=speech_text,
                    similarity=tail_sim,
                )
            if update_dedup:
                self.mark_spoken(full_speech_text, speaker=speaker_name)
                self.mark_spoken(speech_text, speaker=speaker_name)
            return FilterResult(
                True,
                "ok_delta",
                raw_text,
                cleaned,
                speaker_name=speaker_name,
                speech_text=speech_text,
                similarity=0.0,
            )

        # 7. Дедупликация по скользящей истории (с учётом говорящего для коротких приветствий/ответов)
        is_dup, similarity = self.check_duplicate(speech_text, speaker=speaker_name)
        if is_dup:
            return FilterResult(
                False,
                f"duplicate({similarity:.0f}%)",
                raw_text,
                cleaned,
                speaker_name=speaker_name,
                speech_text=speech_text,
                similarity=similarity,
            )

        if update_dedup:
            self.mark_spoken(speech_text, speaker=speaker_name)

        return FilterResult(
            True,
            "ok",
            raw_text,
            cleaned,
            speaker_name=speaker_name,
            speech_text=speech_text,
            similarity=similarity,
        )
