from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Literal, Optional

from src.paths import find_bundled_data_file

Gender = Literal["male", "female"]

_SHARED_MORPH_LOCK = threading.Lock()
_SHARED_MORPH = None
_SHARED_MORPH_LOADED = False


def get_shared_morph():
    """Возвращает единый для всего процесса экземпляр pymorphy3.MorphAnalyzer (экономит ~15 МБ ОЗУ)."""
    global _SHARED_MORPH, _SHARED_MORPH_LOADED
    if _SHARED_MORPH_LOADED:
        return _SHARED_MORPH
    with _SHARED_MORPH_LOCK:
        if not _SHARED_MORPH_LOADED:
            _SHARED_MORPH_LOADED = True
            try:
                import pymorphy3

                _SHARED_MORPH = pymorphy3.MorphAnalyzer()
            except Exception:
                _SHARED_MORPH = None
    return _SHARED_MORPH


# Известные мужские имена, оканчивающиеся на -а / -я (морфологические исключения)
MALE_A_YA_EXCEPTIONS = {
    "никита",
    "илья",
    "лука",
    "фома",
    "кузьма",
    "джошуа",
    "гарсия",
    "данила",
    "савва",
    "ваня",
    "саша",
    "миша",
    "коля",
    "петя",
    "вася",
    "дима",
    "лёша",
    "леша",
    "серёжа",
    "сережа",
    "гоша",
    "боря",
    "костя",
    "юра",
    "толя",
    "слава",
    "стёпа",
    "степа",
    "гриша",
    "вова",
    "паша",
    "рома",
    "тима",
    "витя",
    "гена",
    "бубба",
    "хозия",
    "мика",
    "мустафа",
    "абдулла",
    "коста",
    "санта",
    "папа",
    "дедушка",
    "дядя",
    "дядюшка",
    "мужчина",
    "юноша",
}

FEMALE_SOFT_SIGN_NAMES = {
    "любовь",
    "нинель",
    "адель",
    "рахиль",
    "эсфирь",
    "юдифь",
    "изабель",
    "мишель",
    "николь",
    "ракель",
    "марисоль",
    "гизель",
    "шантель",
    "мать",
    "дочь",
}

CYRILLIC_CONSONANTS = set("бвгджзйклмнпрстфхцчшщ")
NAME_TOKEN_RE = re.compile(r"[а-яёa-z]+", re.IGNORECASE)


class GenderDetector:
    """Трёхслойный определитель пола по русским и международным именам:
    1. Редактируемый JSON-словарь (data/names_gender.json)
    2. Русская морфология + исключения + общий pymorphy3
    3. Запасной вариант -> 'male'
    """

    def __init__(self, dict_path: Optional[Path] = None, use_pymorphy: bool = True) -> None:
        self.dict_path = dict_path or find_bundled_data_file("names_gender.json")
        self._male_names: set[str] = set(MALE_A_YA_EXCEPTIONS)
        self._female_names: set[str] = set(FEMALE_SOFT_SIGN_NAMES)
        self._use_pymorphy = use_pymorphy
        self._load_dictionary()

    def _load_dictionary(self) -> None:
        if not self.dict_path.is_file():
            return
        try:
            with open(self.dict_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name in data.get("male", []):
                w = str(name).strip().lower()
                if w:
                    self._male_names.add(w)
            for name in data.get("female", []):
                w = str(name).strip().lower()
                if w:
                    self._female_names.add(w)
                    self._male_names.discard(w)
        except Exception:
            pass

    def _get_morph(self):
        if not self._use_pymorphy:
            return None
        return get_shared_morph()

    def _detect_token(self, token: str) -> Optional[Gender]:
        t = token.strip().lower()
        if not t:
            return None

        # Слой 1: поиск по словарю
        if t in self._female_names:
            return "female"
        if t in self._male_names:
            return "male"

        # Слой 2: морфологические правила и исключения
        if t in MALE_A_YA_EXCEPTIONS:
            return "male"

        # Имена на -а / -я в русском языке обычно женские (если нет в исключениях)
        if t.endswith(("а", "я")) and len(t) >= 3:
            morph = self._get_morph()
            if morph is not None:
                parses = morph.parse(t)
                for p in parses[:4]:
                    if "nomn" in p.tag and ("Name" in p.tag or "Surn" in p.tag or "anim" in p.tag):
                        if "femn" in p.tag:
                            return "female"
                        if "masc" in p.tag and "femn" not in p.tag:
                            return "male"
            return "female"

        # Мягкий знак -ь -> проверяем словарь и pymorphy3
        if t.endswith("ь"):
            if t in FEMALE_SOFT_SIGN_NAMES:
                return "female"
            morph = self._get_morph()
            if morph is not None:
                parses = morph.parse(t)
                for p in parses[:4]:
                    if "nomn" in p.tag:
                        if "femn" in p.tag:
                            return "female"
                        if "masc" in p.tag:
                            return "male"
            return "male"

        # Оканчивается на согласную -> мужской (кроме несклоняемых женских вроде 'мадам', 'мисс')
        if t[-1] in CYRILLIC_CONSONANTS:
            morph = self._get_morph()
            if morph is not None:
                parses = morph.parse(t)
                for p in parses[:2]:
                    if "nomn" in p.tag and ("Name" in p.tag or "anim" in p.tag) and "femn" in p.tag and "masc" not in p.tag:
                        return "female"
            return "male"

        # Прочие окончания (-и, -у, -о, -е, 'леди') -> проверяем через pymorphy3
        morph = self._get_morph()
        if morph is not None:
            parses = morph.parse(t)
            for p in parses[:4]:
                if "nomn" in p.tag and ("Name" in p.tag or "Surn" in p.tag or "anim" in p.tag or "ADJF" in p.tag):
                    if "femn" in p.tag and "masc" not in p.tag:
                        return "female"
                    if "masc" in p.tag and "femn" not in p.tag:
                        return "male"

        return None

    def detect(self, speaker_name: Optional[str]) -> Gender:
        if not speaker_name or not speaker_name.strip():
            return "male"

        normalized_full = speaker_name.strip().lower()
        if normalized_full in self._female_names:
            return "female"
        if normalized_full in self._male_names:
            return "male"

        tokens = NAME_TOKEN_RE.findall(normalized_full)
        if not tokens:
            return "male"

        # Приоритет первому слову (имя или согласованное определение: "Брайан Хедер", "Местная жительница")
        for token in tokens:
            res = self._detect_token(token)
            if res is not None:
                return res

        # Слой 3: значение по умолчанию
        return "male"
