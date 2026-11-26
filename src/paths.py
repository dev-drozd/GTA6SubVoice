from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional


def get_app_dir() -> Path:
    """Возвращает корневой каталог приложения (папка GTA6SubVoice.exe или корень репозитория)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def is_dir_writable(path: Path) -> bool:
    """Проверяет, может ли текущий пользователь создавать и изменять файлы внутри path."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".write_probe_", dir=str(path))
        os.close(fd)
        os.unlink(probe)
        return True
    except OSError:
        return False


def get_fallback_appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "GTA6SubVoice"


APP_DIR: Path = get_app_dir()
PORTABLE_MODE: bool = is_dir_writable(APP_DIR)
USER_ROOT: Path = APP_DIR if PORTABLE_MODE else get_fallback_appdata_dir()

DATA_DIR: Path = USER_ROOT / "data"
BUNDLED_DATA_DIR: Path = APP_DIR / "data"
MODELS_DIR: Path = USER_ROOT / "models"
BUNDLED_MODELS_DIR: Path = APP_DIR / "models"
OCR_MODELS_DIR: Path = MODELS_DIR / "ocr"
BUNDLED_OCR_MODELS_DIR: Path = BUNDLED_MODELS_DIR / "ocr"
ICONS_DIR: Path = APP_DIR / "icons"
LOGS_DIR: Path = USER_ROOT / "logs"


def find_app_icon_path() -> Optional[Path]:
    """Ищет иконку приложения (предпочтительно .ico, запасной вариант .png) в папках icons/ и data/."""
    search_dirs = [ICONS_DIR, APP_DIR / "_internal" / "icons", BUNDLED_DATA_DIR]
    for directory in search_dirs:
        for name in ("GTA6SubVoice_icon.ico", "GTA6SubVoice_icon.png"):
            cand = directory / name
            if cand.is_file():
                return cand.resolve()
    return None


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    OCR_MODELS_DIR.mkdir(parents=True, exist_ok=True)


def to_portable_path(path: Optional[str | Path]) -> str:
    """Сохраняет путь относительно APP_DIR, если он внутри папки приложения, чтобы перенос папки не ломал конфиг."""
    if not path:
        return ""
    p = Path(path)
    try:
        rel = p.resolve().relative_to(APP_DIR.resolve())
        return rel.as_posix()
    except Exception:
        return str(p)


def resolve_user_path(raw: Optional[str | Path]) -> Optional[Path]:
    """Преобразует относительный (к APP_DIR / USER_ROOT) или абсолютный путь в существующий файл."""
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p if p.is_file() else None
    for root in (APP_DIR, USER_ROOT):
        cand = root / p
        if cand.is_file():
            return cand.resolve()
    return None


def find_bundled_data_file(filename: str) -> Path:
    """Ищет статический файл данных (например names_gender.json), отдавая приоритет записываемой DATA_DIR, затем APP_DIR/data."""
    user_file = DATA_DIR / filename
    if user_file.is_file():
        return user_file
    bundled = BUNDLED_DATA_DIR / filename
    if bundled.is_file():
        return bundled
    return user_file


def find_silero_model(configured_path: Optional[str] = None) -> Optional[Path]:
    """Ищет локальную модель Silero TTS 5.6 СТРОГО по настроенному пути или в управляемых папках models/.
    Никогда не сканирует кэш разработчика (torch.hub) и посторонние папки профиля пользователя.
    """
    if configured_path:
        resolved = resolve_user_path(configured_path)
        if resolved is not None:
            return resolved

    search_dirs: list[Path] = [BUNDLED_MODELS_DIR]
    if MODELS_DIR != BUNDLED_MODELS_DIR:
        search_dirs.append(MODELS_DIR)

    for directory in search_dirs:
        for name in ("v5_6_ru.pt", "v5_5_ru.pt", "v5_ru.pt"):
            cand = directory / name
            if cand.is_file():
                return cand.resolve()
    return None


def find_ocr_model_paths() -> dict[str, Optional[Path]]:
    """Ищет локальные ONNX-модели кириллического PP-OCRv5 строго внутри управляемых каталогов models/ocr/."""
    names = {
        "det": "ch_PP-OCRv5_mobile_det.onnx",
        "rec": "cyrillic_PP-OCRv5_rec_mobile.onnx",
        "keys": "ppocrv5_cyrillic_dict.txt",
        "cls": "ch_ppocr_mobile_v2.0_cls_infer.onnx",
    }
    search_dirs: list[Path] = [BUNDLED_OCR_MODELS_DIR]
    if OCR_MODELS_DIR != BUNDLED_OCR_MODELS_DIR:
        search_dirs.append(OCR_MODELS_DIR)

    result: dict[str, Optional[Path]] = {k: None for k in names}
    for key, filename in names.items():
        for directory in search_dirs:
            cand = directory / filename
            if cand.is_file():
                result[key] = cand.resolve()
                break
    return result
