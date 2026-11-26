from __future__ import annotations

import argparse
import json
import os
import sys
import time

import psutil


def _write_report(report: dict, out_path: str | None) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(payload)
    try:
        print(payload)
    except Exception:
        pass


def run_smoke_test(out_path: str | None = None) -> int:
    """Автономная самопроверка собранного GTA6SubVoice.exe: проверяет Silero 5.6, словарь ударений и OCR."""
    from src.audio_dsp import process_audio
    from src.config import ConfigManager
    from src.dependency_manager import DependencyManager
    from src.ocr_engine import OcrEngineManager
    from src.paths import APP_DIR, PORTABLE_MODE, USER_ROOT
    from src.punctuation import PunctuationRestorer
    from src.text_filter import StressDictManager, SubtitleTextFilter, normalize_text_for_tts
    from src.tts_engine import SileroTTSEngine
    from src.voice_manager import VoiceManager

    t_cold_start = time.perf_counter()
    proc = psutil.Process(os.getpid())
    cfg_mgr = ConfigManager()
    cfg = cfg_mgr.config

    dep_mgr = DependencyManager()
    preflight = dep_mgr.run_preflight(ocr_engine=cfg.ocr_engine, verify_hash=False)

    if not preflight.all_ready:
        report = {
            "status": "SETUP_REQUIRED",
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
            "app_dir": str(APP_DIR),
            "user_root": str(USER_ROOT),
            "portable_mode": PORTABLE_MODE,
            "missing_components": [c.spec.id for c in preflight.missing_for_launch],
            "ram_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
        }
        _write_report(report, out_path)
        return 0

    stress_dict = StressDictManager()
    tts = SileroTTSEngine(
        model_path=cfg.silero_model_path,
        num_threads=cfg.torch_num_threads,
        sample_rate=48000,
        stress_dict=stress_dict,
    )
    tts.load()
    cold_start_ms = (time.perf_counter() - t_cold_start) * 1000.0
    ram_idle_mb = proc.memory_info().rss / (1024 * 1024)

    # Прогретый синтез с эмфазой и словарём ударений (чистый голос Silero + нативный SSML-темп)
    audio_short, tts_short_ms = tts.synthesize("Куда ты идёшь?", speaker="eugene", sample_rate=48000, speed="medium")
    processed_short, dsp_short_ms = process_audio(
        audio_short,
        sample_rate=48000,
        volume=1.0,
        trim=True,
    )

    audio_med, tts_med_ms = tts.synthesize(
        "Я сказал НЕ ТРОГАЙ эти $500 в Вайс-Сити!",
        speaker="kseniya",
        sample_rate=48000,
        speed="fast",
    )
    processed_med, dsp_med_ms = process_audio(
        audio_med,
        sample_rate=48000,
        volume=1.0,
        trim=True,
    )

    # Проверка удаления мусорного префикса и словаря ударений
    filt = SubtitleTextFilter()
    res = filt.filter("RK Рэнди: Славно.")
    assert res.accepted and res.speaker_name == "Рэнди" and res.speech_text == "Славно."
    assert "В+айс-С+ити" in normalize_text_for_tts("Мы едем в Вайс-Сити", stress_dict=stress_dict)

    punct = PunctuationRestorer(enabled=True)
    assert punct.restore("Куда ты идёшь") == "Куда ты идёшь?"

    vm = VoiceManager(available_speakers=tts.speakers)
    assert "eugene" in tts.speakers and "kseniya" in tts.speakers

    ocr = OcrEngineManager(engine_name=cfg.ocr_engine, num_threads=cfg.torch_num_threads)
    ocr.warmup()
    ocr.close()

    # Проверка стека Windows WASAPI Auto-Ducking COM внутри собранного exe
    from src.ducking import AutoDucker, list_active_audio_processes

    active_audio_procs = list_active_audio_processes()
    ducker_check = AutoDucker(enabled=True, depth_pct=cfg.ducking_depth_pct, duck_attack_ms=20.0, release_delay_ms=20.0, release_fade_ms=20.0)
    ducker_check.on_speech_start()
    time.sleep(0.08)
    ducker_error = ducker_check.last_error
    ducker_matched = list(ducker_check.last_ducked_processes)
    ducker_check.close()

    ram_loaded_mb = proc.memory_info().rss / (1024 * 1024)
    report = {
        "status": "OK",
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "app_dir": str(APP_DIR),
        "portable_mode": PORTABLE_MODE,
        "ui_mode_default": cfg.ui_mode,
        "voice_mode_default": cfg.voice_mode,
        "sample_rate": 48000,
        "model_path": str(tts.model_path),
        "speakers": tts.speakers,
        "cold_start_ms": round(cold_start_ms, 1),
        "load_time_ms": round(tts.load_time_ms, 1),
        "warmup_time_ms": round(tts.warmup_time_ms, 1),
        "tts_short_ms": round(tts_short_ms, 1),
        "dsp_short_ms": round(dsp_short_ms, 2),
        "short_total_response_ms": round(tts_short_ms + dsp_short_ms, 1),
        "tts_medium_ms": round(tts_med_ms, 1),
        "dsp_medium_ms": round(dsp_med_ms, 2),
        "audio_samples_short": int(processed_short.size),
        "audio_samples_med": int(processed_med.size),
        "active_audio_processes": active_audio_procs,
        "ducking_matched": ducker_matched,
        "ducking_error": ducker_error,
        "ram_idle_mb": round(ram_idle_mb, 1),
        "ram_loaded_mb": round(ram_loaded_mb, 1),
    }
    _write_report(report, out_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--smoke-out", type=str, default=None)
    parser.add_argument("--check-wizard-dialog", action="store_true")
    parser.add_argument("--check-ui-modes", action="store_true")
    args, _unknown = parser.parse_known_args()

    if args.smoke_test:
        return run_smoke_test(args.smoke_out)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    # Явный AppUserModelID, чтобы панель задач Windows показывала иконку GTA6SubVoice вместо иконки Python/Qt
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GTA6SubVoice.Studio.App")
        except Exception:
            pass

    from src.config import ConfigManager
    from src.dependency_manager import DependencyManager
    from src.hotkeys import GlobalHotkeyManager
    from src.pipeline import SubtitlePipeline
    from src.single_instance import SingleInstanceGuard
    from src.text_filter import StressDictManager
    from src.ui.main_window import MainWindow
    from src.ui.tray import build_app_icon
    from src.voice_manager import VoiceManager

    app = QApplication(sys.argv)
    app.setApplicationName("GTA6SubVoice")
    app.setWindowIcon(build_app_icon(active=False))
    app.setQuitOnLastWindowClosed(False)

    # Запрещаем одновременный запуск 2+ копий приложения (пропускаем только для автоматических CLI-проверок)
    instance_guard = None
    if not (args.check_ui_modes or args.check_wizard_dialog):
        instance_guard = SingleInstanceGuard("GTA6SubVoice_SingleInstance_v1")
        if not instance_guard.acquire():
            return 0

    cfg_mgr = ConfigManager()
    dep_mgr = DependencyManager()
    voice_mgr = VoiceManager()
    stress_dict = StressDictManager()
    pipeline = SubtitlePipeline(cfg_mgr.config, voice_mgr, stress_dict=stress_dict)
    hotkey_mgr = GlobalHotkeyManager()
    app.installNativeEventFilter(hotkey_mgr.native_filter)

    window = MainWindow(cfg_mgr, voice_mgr, stress_dict, pipeline, hotkey_mgr, dep_mgr)
    if instance_guard is not None:
        instance_guard.activation_requested.connect(window.bring_to_front)
    window.show()
    app.processEvents()

    if args.check_ui_modes:
        def _verify_modes() -> None:
            m1 = window.cfg_mgr.config.ui_mode
            tabs_basic = window.tabs.tabBar().isVisible()
            window._on_toggle_ui_mode()
            app.processEvents()
            m2 = window.cfg_mgr.config.ui_mode
            tabs_expert = window.tabs.tabBar().isVisible()
            window._on_toggle_ui_mode()
            app.processEvents()
            _write_report(
                {
                    "status": "UI_MODES_OK",
                    "initial_mode": m1,
                    "tabs_visible_basic": tabs_basic,
                    "toggled_mode": m2,
                    "tabs_visible_expert": tabs_expert,
                },
                args.smoke_out,
            )
            window.quit_application()

        QTimer.singleShot(400, _verify_modes)
    else:
        QTimer.singleShot(
            25,
            lambda: window.start_bootstrap(
                check_wizard_only=bool(args.check_wizard_dialog),
                smoke_out=args.smoke_out,
            ),
        )

    rc = app.exec()
    hotkey_mgr.unregister_all()
    pipeline.shutdown()
    if instance_guard is not None:
        instance_guard.release()
    return rc


if __name__ == "__main__":
    sys.exit(main())
