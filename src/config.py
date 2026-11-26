from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.audio_dsp import SPEECH_PRESETS, normalize_speed_preset
from src.ducking import DEFAULT_DUCKING_PROCESSES, DEFAULT_TARGET_PROCESS
from src.paths import DATA_DIR, ensure_data_dirs, find_silero_model, to_portable_path

VALID_SPEAKERS = ("eugene", "aidar", "kseniya", "xenia", "baya")
VALID_VOICE_MODES = ("single", "dual_mj", "multi")
VALID_SPEECH_PRESETS = SPEECH_PRESETS


def atomic_write_json(path: Path, data: Any) -> None:
    """Атомарно записывает JSON на диск через временный файл и os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass
class RoiConfig:
    x_pct: float = 0.20
    y_pct: float = 0.78
    w_pct: float = 0.60
    h_pct: float = 0.14
    monitor_index: int = 1
    bind_to_xbox: bool = True


@dataclass
class HotkeysConfig:
    toggle_pause: str = "F8"
    select_roi: str = "F9"
    interrupt: str = "F10"


@dataclass
class AppConfig:
    ui_mode: str = "basic"  # "basic" (по умолчанию) | "expert"
    roi: RoiConfig = field(default_factory=RoiConfig)
    voice_mode: str = "dual_mj"  # "single" | "dual_mj" (по умолчанию) | "multi"
    single_speaker: str = "eugene"
    dual_male_speaker: str = "eugene"
    dual_female_speaker: str = "kseniya"
    speech_speed: str = "medium"  # «medium» (1.0x текст) | «fast» (1.2x SSML) | «x-fast» (1.5x SSML)
    adaptive_tempo: bool = False  # Автоускорение (по умолчанию выключено: база 'medium', ускорение до 'fast')
    always_on_top: bool = True  # Держать окно и оверлей ROI поверх окон игры/Xbox
    volume: int = 100  # 0 .. 200 (%) — 100% = исходный нативный уровень выхода Silero
    audio_device: Optional[str] = None
    target_process: str = ""  # Пусто, пока пользователь не выберет окно / процесс игры
    only_when_target_active: bool = True  # Ставить захват и озвучку на паузу, если целевое окно не активно
    ducking_enabled: bool = True
    ducking_depth_pct: int = 75  # 10 .. 95 (%)
    mute_target_window: bool = False  # Полностью отключать звук выбранного окна во время чтения субтитров
    ducking_processes: list[str] = field(default_factory=list)
    silero_model_path: str = ""
    torch_num_threads: int = 2  # Оптимум ~51 мс @ 48 кГц без лишней нагрузки на ядра для сохранения FPS игры
    sample_rate: int = 48000
    scan_fps: int = 8
    debounce_ms: int = 50
    ocr_engine: str = "rapidocr"  # «rapidocr» | «winrt»
    caps_ratio_threshold: float = 0.72
    dedup_threshold: float = 82.0
    enable_punctuation: bool = True
    font_classifier_enabled: bool = True  # Компактный ONNX-классификатор шрифта (< 1 мс CPU) против текста карты/UI
    announce_status: bool = True
    debug_capture_log: bool = False  # Сохранять JSONL/TXT озвученных субтитров и JPEG-кадры ROI в data/debug_logs/
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> tuple["AppConfig", bool]:
        """Разбирает JSON конфигурации, автоматически мигрируя и очищая устаревшие поля. Возвращает (cfg, migrated)."""
        cfg = cls()
        migrated = False
        if not isinstance(raw, dict):
            return cfg, True

        legacy_keys = {
            "dsp_quality",
            "dsp_backend",
            "use_formants",
            "enable_voice_shades",
            "archetype_default",
            "studio_mastering",
            "enable_visual_filter",
            "interrupt_mode",
        }
        if any(k in raw for k in legacy_keys):
            migrated = True

        ui_mode = str(raw.get("ui_mode", cfg.ui_mode))
        cfg.ui_mode = ui_mode if ui_mode in ("basic", "expert") else "basic"

        roi_raw = raw.get("roi")
        if isinstance(roi_raw, dict):
            cfg.roi = RoiConfig(
                x_pct=float(roi_raw.get("x_pct", cfg.roi.x_pct)),
                y_pct=float(roi_raw.get("y_pct", cfg.roi.y_pct)),
                w_pct=float(roi_raw.get("w_pct", cfg.roi.w_pct)),
                h_pct=float(roi_raw.get("h_pct", cfg.roi.h_pct)),
                monitor_index=int(roi_raw.get("monitor_index", cfg.roi.monitor_index)),
                bind_to_xbox=bool(roi_raw.get("bind_to_xbox", cfg.roi.bind_to_xbox)),
            )

        hk_raw = raw.get("hotkeys")
        if isinstance(hk_raw, dict):
            cfg.hotkeys = HotkeysConfig(
                toggle_pause=str(hk_raw.get("toggle_pause", cfg.hotkeys.toggle_pause)),
                select_roi=str(hk_raw.get("select_roi", cfg.hotkeys.select_roi)),
                interrupt=str(hk_raw.get("interrupt", cfg.hotkeys.interrupt)),
            )

        raw_mode = str(raw.get("voice_mode", cfg.voice_mode))
        if raw_mode == "eugene_only":
            cfg.voice_mode = "single"
            migrated = True
        elif raw_mode == "auto_characters":
            cfg.voice_mode = "dual_mj"
            migrated = True
        elif raw_mode in VALID_VOICE_MODES:
            cfg.voice_mode = raw_mode
        else:
            cfg.voice_mode = "dual_mj"
            migrated = True

        sp_single = str(raw.get("single_speaker", cfg.single_speaker))
        cfg.single_speaker = sp_single if sp_single in VALID_SPEAKERS else "eugene"
        sp_m = str(raw.get("dual_male_speaker", cfg.dual_male_speaker))
        cfg.dual_male_speaker = sp_m if sp_m in VALID_SPEAKERS else "eugene"
        sp_f = str(raw.get("dual_female_speaker", cfg.dual_female_speaker))
        cfg.dual_female_speaker = sp_f if sp_f in VALID_SPEAKERS else "kseniya"

        if "speech_speed" in raw:
            raw_sp = raw.get("speech_speed")
            preset = normalize_speed_preset(raw_sp)
            if not (isinstance(raw_sp, str) and raw_sp.strip().lower() == preset):
                migrated = True
            cfg.speech_speed = preset
        else:
            cfg.speech_speed = "medium"
        cfg.adaptive_tempo = bool(raw.get("adaptive_tempo", cfg.adaptive_tempo))
        if cfg.adaptive_tempo and cfg.speech_speed != "medium":
            cfg.speech_speed = "medium"
            migrated = True
        cfg.always_on_top = bool(raw.get("always_on_top", cfg.always_on_top))
        cfg.volume = max(0, min(200, int(raw.get("volume", cfg.volume))))
        cfg.audio_device = raw.get("audio_device", cfg.audio_device)
        cfg.only_when_target_active = bool(raw.get("only_when_target_active", cfg.only_when_target_active))
        cfg.ducking_enabled = bool(raw.get("ducking_enabled", cfg.ducking_enabled))
        cfg.ducking_depth_pct = max(10, min(95, int(raw.get("ducking_depth_pct", cfg.ducking_depth_pct))))
        cfg.mute_target_window = bool(raw.get("mute_target_window", cfg.mute_target_window))

        has_target_key = "target_process" in raw
        raw_target = str(raw.get("target_process") or "").strip()
        raw_procs = raw.get("ducking_processes")
        legacy_xbox_set = {
            "xboxpcapp.exe",
            "xboxpcappft.exe",
            "gamingservices.exe",
            "xboxgamebarwidgets.exe",
        }
        if raw_target:
            cfg.target_process = raw_target
            cfg.ducking_processes = [raw_target]
        elif has_target_key:
            cfg.target_process = ""
            cfg.ducking_processes = []
        elif isinstance(raw_procs, list) and raw_procs:
            cleaned_procs = [str(p).strip() for p in raw_procs if str(p).strip()]
            if cleaned_procs and {p.lower() for p in cleaned_procs}.issubset(legacy_xbox_set):
                cfg.target_process = DEFAULT_TARGET_PROCESS
                cfg.ducking_processes = [DEFAULT_TARGET_PROCESS]
                migrated = True
            elif cleaned_procs:
                cfg.target_process = cleaned_procs[0]
                cfg.ducking_processes = [cleaned_procs[0]]
                if len(cleaned_procs) > 1:
                    migrated = True
        else:
            cfg.target_process = ""
            cfg.ducking_processes = []

        cfg.silero_model_path = str(raw.get("silero_model_path", cfg.silero_model_path))
        cfg.torch_num_threads = max(1, min(16, int(raw.get("torch_num_threads", cfg.torch_num_threads))))

        # Всегда запускаем Silero v5.6 на нативных 48 000 Гц для чистого звука без ресемплинга WASAPI
        raw_sr = int(raw.get("sample_rate", 48000))
        if raw_sr != 48000:
            migrated = True
        cfg.sample_rate = 48000

        cfg.scan_fps = max(2, min(30, int(raw.get("scan_fps", cfg.scan_fps))))
        cfg.debounce_ms = max(0, min(500, int(raw.get("debounce_ms", cfg.debounce_ms))))
        cfg.ocr_engine = str(raw.get("ocr_engine", cfg.ocr_engine))
        if cfg.ocr_engine not in ("rapidocr", "winrt"):
            cfg.ocr_engine = "rapidocr"

        cfg.caps_ratio_threshold = max(0.3, min(1.0, float(raw.get("caps_ratio_threshold", cfg.caps_ratio_threshold))))
        cfg.dedup_threshold = max(50.0, min(100.0, float(raw.get("dedup_threshold", cfg.dedup_threshold))))
        cfg.enable_punctuation = bool(raw.get("enable_punctuation", cfg.enable_punctuation))
        cfg.font_classifier_enabled = bool(raw.get("font_classifier_enabled", cfg.font_classifier_enabled))
        cfg.announce_status = bool(raw.get("announce_status", cfg.announce_status))
        cfg.debug_capture_log = bool(raw.get("debug_capture_log", cfg.debug_capture_log))
        return cfg, migrated


class ConfigManager:
    def __init__(self, config_path: Optional[Path] = None) -> None:
        ensure_data_dirs()
        self.path = config_path or (DATA_DIR / "config.json")
        self.config = self.load()

    def load(self) -> AppConfig:
        migrated = False
        if self.path.is_file():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cfg, migrated = AppConfig.from_dict(data)
            except Exception:
                cfg = AppConfig()
                migrated = True
        else:
            cfg = AppConfig()
            migrated = True

        detected = find_silero_model(cfg.silero_model_path)
        if detected is not None:
            portable = to_portable_path(detected)
            if cfg.silero_model_path != portable:
                cfg.silero_model_path = portable
                migrated = True

        if migrated or not self.path.is_file():
            self.save(cfg)
        return cfg

    def save(self, cfg: Optional[AppConfig] = None) -> None:
        if cfg is not None:
            self.config = cfg
        if self.config.adaptive_tempo:
            self.config.speech_speed = "medium"
        self.config.target_process = (self.config.target_process or "").strip()
        if self.config.target_process:
            self.config.ducking_processes = [self.config.target_process]
        else:
            self.config.ducking_processes = []
        if self.config.silero_model_path:
            self.config.silero_model_path = to_portable_path(self.config.silero_model_path)
        atomic_write_json(self.path, self.config.to_dict())

    def reset_to_defaults(self, keep_ui_mode: bool = True) -> AppConfig:
        """Сбрасывает все настройки к заводским по умолчанию, сохраняя найденный локальный путь к модели Silero."""
        old_mode = self.config.ui_mode
        fresh = AppConfig()
        if keep_ui_mode:
            fresh.ui_mode = old_mode
        detected = find_silero_model(None)
        if detected is not None:
            fresh.silero_model_path = to_portable_path(detected)
        self.save(fresh)
        return fresh


def export_profile_bundle(
    target_path: Path,
    config: AppConfig,
    stress_entries: dict[str, str],
    characters: dict[str, dict],
) -> None:
    """Экспортирует все настройки, параметры моделей, словарь ударений и голоса персонажей в один JSON-файл."""
    from datetime import datetime

    cfg_dict = config.to_dict()
    bundle = {
        "format": "GTA6SubVoiceProfile",
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "models": {
            "silero_model_path": cfg_dict.get("silero_model_path", ""),
            "ocr_engine": cfg_dict.get("ocr_engine", "rapidocr"),
            "torch_num_threads": cfg_dict.get("torch_num_threads", 2),
            "sample_rate": cfg_dict.get("sample_rate", 48000),
        },
        "config": cfg_dict,
        "stress_dict": dict(stress_entries),
        "characters": dict(characters),
    }
    atomic_write_json(target_path, bundle)


def load_profile_bundle(source_path: Path) -> tuple[AppConfig, Optional[dict[str, str]], Optional[dict[str, dict]]]:
    """Загружает и проверяет единый профиль JSON (модели + словарь ударений + персонажи + настройки)."""
    with open(source_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("Некорректный формат файла настроек (ожидался JSON-объект).")

    raw_cfg = dict(raw.get("config")) if isinstance(raw.get("config"), dict) else dict(raw)
    models_sec = raw.get("models")
    if isinstance(models_sec, dict):
        for k in ("silero_model_path", "ocr_engine", "torch_num_threads", "sample_rate"):
            if k in models_sec:
                raw_cfg[k] = models_sec[k]

    cfg, _migrated = AppConfig.from_dict(raw_cfg)
    stress_raw = raw.get("stress_dict")
    stress_dict = (
        {str(k).strip(): str(v).strip() for k, v in stress_raw.items() if str(k).strip() and str(v).strip()}
        if isinstance(stress_raw, dict)
        else None
    )
    chars_raw = raw.get("characters")
    characters = chars_raw if isinstance(chars_raw, dict) else None
    return cfg, stress_dict, characters
