from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from src.config import atomic_write_json
from src.gender_detector import Gender, GenderDetector
from src.paths import DATA_DIR, ensure_data_dirs

# Чистые нативные голоса Silero v5_6_ru в строгом порядке приоритета
MALE_SPEAKERS: tuple[str, ...] = ("eugene", "aidar")
FEMALE_SPEAKERS: tuple[str, ...] = ("kseniya", "xenia", "baya")
ALL_SILERO_SPEAKERS: tuple[str, ...] = MALE_SPEAKERS + FEMALE_SPEAKERS

SPEAKER_LABELS: dict[str, str] = {
    "eugene": "Евгений (eugene)",
    "aidar": "Айдар (aidar)",
    "kseniya": "Ксения (kseniya)",
    "xenia": "Ксюша / Xenia (xenia)",
    "baya": "Бая (baya)",
}

SPEAKER_SHORT_LABELS: dict[str, str] = {
    "eugene": "Евгений",
    "aidar": "Айдар",
    "kseniya": "Ксения",
    "xenia": "Ксюша (Xenia)",
    "baya": "Бая",
}

# Главные персонажи, которым в многоголосом режиме отдаются приоритетные голоса (eugene / kseniya)
PRIMARY_MALE_HEROES = {"джейсон", "брайан"}
PRIMARY_FEMALE_HEROES = {"люсия", "лусия"}

GARBAGE_NAME_PREFIX_RE = re.compile(r"^(?:[A-Za-z0-9\[\]\(\)\|]{1,3}\s+)+(?=[А-ЯЁ][а-яё])")
CYRILLIC_RE = re.compile(r"[А-ЯЁа-яё]")
LATIN_RE = re.compile(r"[A-Za-z]")


def clean_character_name(name: str) -> str:
    """Удаляет мусорные OCR-префиксы вроде 'RK Рэнди' -> 'Рэнди' и нормализует пробелы."""
    s = " ".join(name.strip().split())
    s = GARBAGE_NAME_PREFIX_RE.sub("", s).strip()
    return s


def is_valid_character_name(name: str) -> bool:
    """Отсеивает мусорные имена: < 2 букв, одиночные символы или случайную смесь латиницы и кириллицы внутри имени."""
    cleaned = clean_character_name(name)
    letters = [c for c in cleaned if c.isalpha()]
    if len(letters) < 2:
        return False
    # Отсекаем случайные артефакты OCR из смешанной латиницы и кириллицы (например, 'Брайaн')
    if CYRILLIC_RE.search(cleaned) and LATIN_RE.search(cleaned):
        return False
    return True


def speaker_display_name(speaker_id: str, short: bool = False) -> str:
    table = SPEAKER_SHORT_LABELS if short else SPEAKER_LABELS
    return table.get(speaker_id, speaker_id)


@dataclass
class CharacterVoiceProfile:
    gender: Gender
    speaker: str = "eugene"
    locked: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> tuple["CharacterVoiceProfile", bool]:
        """Загружает профиль персонажа и очищает устаревшие поля из прошлых версий."""
        gender = raw.get("gender", "male")
        if gender not in ("male", "female"):
            gender = "male"

        raw_speaker = raw.get("speaker") or raw.get("base_speaker") or ("eugene" if gender == "male" else "kseniya")
        speaker = str(raw_speaker)
        if speaker not in ALL_SILERO_SPEAKERS:
            speaker = "eugene" if gender == "male" else "kseniya"

        locked = bool(raw.get("locked", False))
        clean_keys = {"gender", "speaker", "locked"}
        migrated = set(raw.keys()) != clean_keys
        return cls(gender=gender, speaker=speaker, locked=locked), migrated


class VoiceManager:
    """Управляет чистыми нативными голосами Silero v5_6_ru и сохраняет профили в data/characters.json."""

    def __init__(
        self,
        characters_path: Optional[Path] = None,
        gender_detector: Optional[GenderDetector] = None,
        available_speakers: Optional[list[str]] = None,
    ) -> None:
        ensure_data_dirs()
        self.path = characters_path or (DATA_DIR / "characters.json")
        self.gender_detector = gender_detector or GenderDetector()
        self._lock = threading.RLock()
        self._profiles: dict[str, CharacterVoiceProfile] = {}
        self._key_map: dict[str, str] = {}

        self.male_pool = list(MALE_SPEAKERS)
        self.female_pool = list(FEMALE_SPEAKERS)
        if available_speakers:
            self.set_available_speakers(available_speakers)
        self.load()

    def set_available_speakers(self, speakers: list[str]) -> None:
        with self._lock:
            avail = [s for s in speakers if s in ALL_SILERO_SPEAKERS]
            males = [s for s in MALE_SPEAKERS if s in avail]
            females = [s for s in FEMALE_SPEAKERS if s in avail]
            self.male_pool = males or list(MALE_SPEAKERS)
            self.female_pool = females or list(FEMALE_SPEAKERS)

    def get_all_speakers(self) -> list[str]:
        return list(dict.fromkeys(self.male_pool + self.female_pool))

    def load(self) -> None:
        with self._lock:
            self._profiles.clear()
            self._key_map.clear()
            if not self.path.is_file():
                return
            migrated = False
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for raw_name, pdata in raw.items():
                        if not isinstance(pdata, dict):
                            migrated = True
                            continue
                        cleaned_name = clean_character_name(str(raw_name))
                        if cleaned_name != str(raw_name):
                            migrated = True
                        if not is_valid_character_name(cleaned_name):
                            migrated = True
                            continue
                        prof, prof_migrated = CharacterVoiceProfile.from_dict(pdata)
                        if prof_migrated:
                            migrated = True

                        lower = cleaned_name.lower()
                        # Объединяем дубликаты после очистки вроде 'RK Рэнди' -> 'Рэнди'
                        if lower in self._key_map:
                            migrated = True
                            existing_key = self._key_map[lower]
                            if prof.locked and not self._profiles[existing_key].locked:
                                self._profiles[existing_key] = prof
                        else:
                            self._profiles[cleaned_name] = prof
                            self._key_map[lower] = cleaned_name
            except Exception:
                self._profiles.clear()
                self._key_map.clear()
                return

            if migrated:
                self.save()

    def save(self) -> None:
        with self._lock:
            payload = {name: prof.to_dict() for name, prof in self._profiles.items()}
            atomic_write_json(self.path, payload)

    def _pick_multi_speaker(
        self,
        canon_name: str,
        gender: Gender,
        reserved_male: str = "eugene",
        reserved_female: str = "kseniya",
    ) -> str:
        """Приоритетное распределение голосов в режиме 'multi':
        - Главные герои (PRIMARY_MALE_HEROES / PRIMARY_FEMALE_HEROES) получают зарезервированные основные голоса
          (eugene / kseniya по умолчанию или выбранные в dual_male_speaker / dual_female_speaker).
        - Второстепенные NPC ('Охранник', 'Сотрудник', 'Местные жители', 'Туристы' и т.п.) берут свободные
          нативные голоса (aidar для мужских; xenia/baya для женских), чтобы не звучать одинаково с героями.
        """
        lower = canon_name.lower()
        if gender == "male":
            primary = reserved_male if reserved_male in self.male_pool else self.male_pool[0]
            if lower in PRIMARY_MALE_HEROES:
                return primary
            pool = [sp for sp in self.male_pool if sp != primary] or list(self.male_pool)
            counts = {sp: 0 for sp in pool}
            for prof in self._profiles.values():
                if prof.gender == "male" and prof.speaker in counts:
                    counts[prof.speaker] += 1
            return min(pool, key=lambda sp: (counts[sp], pool.index(sp)))
        else:
            primary = reserved_female if reserved_female in self.female_pool else self.female_pool[0]
            if lower in PRIMARY_FEMALE_HEROES:
                return primary
            pool = [sp for sp in self.female_pool if sp != primary] or list(self.female_pool)
            counts = {sp: 0 for sp in pool}
            for prof in self._profiles.values():
                if prof.gender == "female" and prof.speaker in counts:
                    counts[prof.speaker] += 1
            return min(pool, key=lambda sp: (counts[sp], pool.index(sp)))

    def get_profile(self, speaker_name: str) -> Optional[CharacterVoiceProfile]:
        cleaned = clean_character_name(speaker_name)
        with self._lock:
            canon = self._key_map.get(cleaned.lower())
            if canon is None:
                return None
            return self._profiles.get(canon)

    def get_or_create_profile(
        self,
        speaker_name: Optional[str],
        dual_male_speaker: str = "eugene",
        dual_female_speaker: str = "kseniya",
    ) -> tuple[CharacterVoiceProfile, bool]:
        if not speaker_name or not speaker_name.strip():
            return CharacterVoiceProfile(gender="male", speaker="eugene", locked=False), False

        cleaned = clean_character_name(speaker_name)
        if not is_valid_character_name(cleaned):
            return CharacterVoiceProfile(gender="male", speaker="eugene", locked=False), False

        lower = cleaned.lower()
        with self._lock:
            existing_key = self._key_map.get(lower)
            if existing_key is not None and existing_key in self._profiles:
                return self._profiles[existing_key], False

            gender = self.gender_detector.detect(cleaned)
            speaker = self._pick_multi_speaker(
                cleaned,
                gender,
                reserved_male=dual_male_speaker,
                reserved_female=dual_female_speaker,
            )
            profile = CharacterVoiceProfile(gender=gender, speaker=speaker, locked=False)
            self._profiles[cleaned] = profile
            self._key_map[lower] = cleaned
            self.save()
            return profile, True

    def resolve_speaker(
        self,
        speaker_name: Optional[str],
        voice_mode: str = "dual_mj",
        single_speaker: str = "eugene",
        dual_male_speaker: str = "eugene",
        dual_female_speaker: str = "kseniya",
    ) -> tuple[str, Gender, bool]:
        """Подбирает итоговый чистый голос Silero для любого из 3 режимов:
        - 'single': всегда single_speaker (по умолчанию 'eugene')
        - 'dual_mj': dual_male_speaker / dual_female_speaker по полу персонажа (по умолчанию eugene / kseniya)
        - 'multi': до 5 нативных голосов Silero, сохраняемых для персонажей в characters.json
        Возвращает кортеж (speaker_id, gender, created_new_character).
        """
        created = False
        profile: Optional[CharacterVoiceProfile] = None

        if speaker_name and is_valid_character_name(speaker_name):
            profile, created = self.get_or_create_profile(
                speaker_name,
                dual_male_speaker=dual_male_speaker,
                dual_female_speaker=dual_female_speaker,
            )
            gender: Gender = profile.gender
        else:
            gender = "male"

        if voice_mode == "single":
            sp = single_speaker if single_speaker in ALL_SILERO_SPEAKERS else "eugene"
            return sp, ("female" if sp in FEMALE_SPEAKERS else "male"), created

        if voice_mode == "dual_mj":
            if gender == "female":
                sp = dual_female_speaker if dual_female_speaker in ALL_SILERO_SPEAKERS else "kseniya"
            else:
                sp = dual_male_speaker if dual_male_speaker in ALL_SILERO_SPEAKERS else "eugene"
            return sp, gender, created

        # Режим 'multi'
        if profile is not None:
            return profile.speaker, profile.gender, created
        return (dual_male_speaker if dual_male_speaker in ALL_SILERO_SPEAKERS else "eugene"), "male", False

    def add_or_update_profile(
        self,
        speaker_name: str,
        gender: Optional[Gender] = None,
        speaker: Optional[str] = None,
        locked: Optional[bool] = None,
    ) -> Optional[CharacterVoiceProfile]:
        cleaned = clean_character_name(speaker_name)
        if not is_valid_character_name(cleaned):
            return None
        lower = cleaned.lower()
        with self._lock:
            existing_key = self._key_map.get(lower)
            if existing_key is not None and existing_key in self._profiles:
                prof = self._profiles[existing_key]
                key = existing_key
            else:
                det_gender = gender or self.gender_detector.detect(cleaned)
                sp = speaker if (speaker in ALL_SILERO_SPEAKERS) else self._pick_multi_speaker(cleaned, det_gender)
                prof = CharacterVoiceProfile(gender=det_gender, speaker=sp, locked=bool(locked))
                key = cleaned
                self._profiles[key] = prof
                self._key_map[lower] = key

            if gender in ("male", "female"):
                old_g = prof.gender
                prof.gender = gender
                if speaker is None and old_g != gender:
                    prof.speaker = "eugene" if gender == "male" else "kseniya"
            if speaker is not None and speaker in ALL_SILERO_SPEAKERS:
                prof.speaker = speaker
                prof.locked = True if locked is None else bool(locked)
            elif locked is not None:
                prof.locked = bool(locked)

            self.save()
            return prof

    def delete_profile(self, speaker_name: str) -> bool:
        cleaned = clean_character_name(speaker_name)
        lower = cleaned.lower()
        with self._lock:
            existing_key = self._key_map.pop(lower, None)
            if existing_key is not None and existing_key in self._profiles:
                del self._profiles[existing_key]
                self.save()
                return True
            return False

    def clear_all(self) -> None:
        with self._lock:
            self._profiles.clear()
            self._key_map.clear()
            self.save()

    def list_profiles(self) -> dict[str, CharacterVoiceProfile]:
        with self._lock:
            return dict(self._profiles)

    def export_raw(self) -> dict[str, dict]:
        with self._lock:
            return {name: prof.to_dict() for name, prof in self._profiles.items()}

    def replace_all(self, raw_profiles: dict[str, dict]) -> None:
        with self._lock:
            self._profiles.clear()
            self._key_map.clear()
            for raw_name, pdata in raw_profiles.items():
                if not isinstance(pdata, dict):
                    continue
                cleaned_name = clean_character_name(str(raw_name))
                if not is_valid_character_name(cleaned_name):
                    continue
                prof, _ = CharacterVoiceProfile.from_dict(pdata)
                self._profiles[cleaned_name] = prof
                self._key_map[cleaned_name.lower()] = cleaned_name
            self.save()
