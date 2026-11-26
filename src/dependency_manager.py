from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

import httpx

from src.audio_player import list_output_devices
from src.paths import (
    BUNDLED_MODELS_DIR,
    MODELS_DIR,
    USER_ROOT,
    ensure_data_dirs,
    is_dir_writable,
)

ComponentStatus = Literal["installed", "missing", "corrupted"]


@dataclass(frozen=True)
class ComponentFileSpec:
    rel_path: str  # Путь относительно папки models/ (например "v5_6_ru.pt" или "ocr/ch_PP-OCRv5_mobile_det.onnx")
    url: str
    expected_size: int
    sha256: str
    kind: Literal["torch_package", "onnx", "text"]


@dataclass(frozen=True)
class ComponentSpec:
    id: str
    name: str
    description: str
    required: bool
    files: tuple[ComponentFileSpec, ...]

    @property
    def total_size_bytes(self) -> int:
        return sum(f.expected_size for f in self.files)

    @property
    def total_size_mb(self) -> float:
        return self.total_size_bytes / (1024.0 * 1024.0)

    @property
    def source_summary(self) -> str:
        return self.files[0].url


COMPONENT_MANIFEST: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        id="silero_v5_6",
        name="Silero TTS v5.6 (Русская речь)",
        description="Локальная нейросеть синтеза речи (5 спикеров: eugene, aidar, baya, kseniya, xenia).",
        required=True,
        files=(
            ComponentFileSpec(
                rel_path="v5_6_ru.pt",
                url="https://models.silero.ai/models/tts/ru/v5_6_ru.pt",
                expected_size=147664410,
                sha256="04cc9b1d08ce38bcaa9d02c488d80d3e6806d17f7eaa7c95982f36a3cbf50b2e",
                kind="torch_package",
            ),
        ),
    ),
    ComponentSpec(
        id="rapidocr_cyrillic",
        name="RapidOCR PP-OCRv5 (Кириллица ONNX)",
        description="Точный локальный OCR для цветных имён и мелкой пунктуации (можно пропустить при использовании Windows OCR).",
        required=False,
        files=(
            ComponentFileSpec(
                rel_path="ocr/ch_PP-OCRv5_mobile_det.onnx",
                url="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
                expected_size=4819576,
                sha256="4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
                kind="onnx",
            ),
            ComponentFileSpec(
                rel_path="ocr/cyrillic_PP-OCRv5_rec_mobile.onnx",
                url="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile.onnx",
                expected_size=8074092,
                sha256="90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711",
                kind="onnx",
            ),
            ComponentFileSpec(
                rel_path="ocr/ch_ppocr_mobile_v2.0_cls_infer.onnx",
                url="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                expected_size=585532,
                sha256="e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
                kind="onnx",
            ),
            ComponentFileSpec(
                rel_path="ocr/ppocrv5_cyrillic_dict.txt",
                url="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/paddle/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile/ppocrv5_cyrillic_dict.txt",
                expected_size=2781,
                sha256="db40aa52ceb112055be80c694afdf655d5d2c4f7873704524cc16a447ca913ba",
                kind="text",
            ),
        ),
    ),
)

MANIFEST_BY_ID: dict[str, ComponentSpec] = {c.id: c for c in COMPONENT_MANIFEST}


@dataclass
class ComponentCheckResult:
    spec: ComponentSpec
    status: ComponentStatus
    target_path: Path
    actual_size_bytes: int
    message: str = ""


@dataclass
class PreflightReport:
    components: list[ComponentCheckResult] = field(default_factory=list)
    writable_ok: bool = True
    audio_devices_count: int = 0
    winrt_ru_available: bool = False
    ocr_engine: str = "rapidocr"

    @property
    def missing_for_launch(self) -> list[ComponentCheckResult]:
        needed: list[ComponentCheckResult] = []
        for item in self.components:
            if item.status == "installed":
                continue
            if item.spec.required:
                needed.append(item)
            elif item.spec.id == "rapidocr_cyrillic" and self.ocr_engine == "rapidocr":
                needed.append(item)
        return needed

    @property
    def all_ready(self) -> bool:
        if self.missing_for_launch:
            return False
        if self.ocr_engine == "winrt" and not self.winrt_ru_available:
            return False
        return True


@dataclass
class DownloadProgress:
    component_id: str
    file_name: str
    file_downloaded: int
    file_total: int
    overall_downloaded: int
    overall_total: int
    speed_mbps: float
    eta_seconds: float
    percent: float


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def validate_file_header(path: Path, kind: str) -> tuple[bool, str]:
    """Быстрая структурная проверка заголовка скачанной модели перед атомарным переименованием."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False, "Пустой файл"
        with open(path, "rb") as f:
            head = f.read(64)
        if kind == "torch_package":
            # Пакет PyTorch (.pt) — это ZIP-архив, начинающийся с PK\x03\x04
            if not head.startswith(b"PK\x03\x04"):
                return False, "Неверный заголовок Torch Package (.pt)"
        elif kind == "onnx":
            if len(head) < 16 or head.lstrip().startswith(b"<"):
                return False, "Повреждённый ONNX-файл (получен HTML/мусор)"
        elif kind == "text":
            text = path.read_text(encoding="utf-8")
            if len(text.splitlines()) < 30:
                return False, "Словарь OCR слишком короткий"
        return True, "OK"
    except Exception as exc:
        return False, str(exc)


def check_winrt_ru_ocr_available() -> bool:
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        lang = Language("ru")
        return bool(OcrEngine.is_language_supported(lang))
    except Exception:
        return False


class DependencyManager:
    """Управляет проверкой локальных моделей, ручным импортом, удалением и докачкой по HTTP Range."""

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        bundled_models_dir: Optional[Path] = None,
        manifest: tuple[ComponentSpec, ...] = COMPONENT_MANIFEST,
    ) -> None:
        self.models_dir = models_dir or MODELS_DIR
        self.bundled_models_dir = bundled_models_dir or (models_dir or BUNDLED_MODELS_DIR)
        self.manifest = manifest
        self._manifest_by_id = {c.id: c for c in manifest}

    def _resolve_existing_file(self, rel_path: str) -> Optional[Path]:
        for base in (self.bundled_models_dir, self.models_dir):
            cand = base / rel_path
            if cand.is_file():
                return cand
        return None

    def target_file_path(self, rel_path: str) -> Path:
        return self.models_dir / rel_path

    def check_component(self, comp_id: str, verify_hash: bool = False) -> ComponentCheckResult:
        spec = self._manifest_by_id[comp_id]
        if len(spec.files) == 1:
            target_root = self.target_file_path(spec.files[0].rel_path)
        else:
            top_dir = Path(spec.files[0].rel_path).parts[0]
            target_root = self.models_dir / top_dir
        total_actual = 0
        missing_files: list[str] = []
        corrupt_msgs: list[str] = []

        for fspec in spec.files:
            found = self._resolve_existing_file(fspec.rel_path)
            if found is None:
                missing_files.append(fspec.rel_path)
                continue
            size = found.stat().st_size
            total_actual += size
            # Быстрая проверка целостности: непустой файл + проверка заголовка + сверка размера
            ok_hdr, hdr_msg = validate_file_header(found, fspec.kind)
            if not ok_hdr:
                corrupt_msgs.append(f"{fspec.rel_path}: {hdr_msg}")
                continue
            if fspec.expected_size > 0 and size != fspec.expected_size:
                # Для пользовательских моделей Silero (например другой версии v5.x) допускаем валидный заголовок без verify_hash
                if verify_hash:
                    corrupt_msgs.append(f"{fspec.rel_path}: размер {size} != {fspec.expected_size}")
                    continue
            if verify_hash and fspec.sha256:
                actual_hash = compute_sha256(found)
                if actual_hash.lower() != fspec.sha256.lower():
                    corrupt_msgs.append(f"{fspec.rel_path}: несовпадение SHA-256")

        if missing_files:
            return ComponentCheckResult(
                spec=spec,
                status="missing",
                target_path=target_root,
                actual_size_bytes=total_actual,
                message=f"Отсутствуют файлы: {', '.join(missing_files)}",
            )
        if corrupt_msgs:
            return ComponentCheckResult(
                spec=spec,
                status="corrupted",
                target_path=target_root,
                actual_size_bytes=total_actual,
                message="; ".join(corrupt_msgs),
            )
        return ComponentCheckResult(
            spec=spec,
            status="installed",
            target_path=target_root,
            actual_size_bytes=total_actual,
            message="Установлен и проверен",
        )

    def run_preflight(self, ocr_engine: str = "rapidocr", verify_hash: bool = False) -> PreflightReport:
        ensure_data_dirs()
        results = [self.check_component(spec.id, verify_hash=verify_hash) for spec in self.manifest]
        writable = is_dir_writable(USER_ROOT)
        audio_cnt = len(list_output_devices())
        winrt_ok = check_winrt_ru_ocr_available()
        return PreflightReport(
            components=results,
            writable_ok=writable,
            audio_devices_count=audio_cnt,
            winrt_ru_available=winrt_ok,
            ocr_engine=ocr_engine,
        )

    def install_local_file(self, comp_id: str, local_source: Path) -> tuple[bool, str]:
        """Позволяет офлайн установить выбранный пользователем локальный файл модели (например v5_6_ru.pt)."""
        spec = self._manifest_by_id[comp_id]
        if not local_source.is_file():
            return False, "Выбранный файл не существует."
        if len(spec.files) != 1:
            return False, "Ручной выбор одного файла поддерживается только для одиночных моделей."
        fspec = spec.files[0]
        ok_hdr, hdr_msg = validate_file_header(local_source, fspec.kind)
        if not ok_hdr:
            return False, f"Файл не прошёл проверку: {hdr_msg}"

        target = self.target_file_path(fspec.rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        try:
            shutil.copy2(local_source, part)
            os.replace(part, target)
            return True, f"Установлено в {target}"
        except Exception as exc:
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            return False, str(exc)

    def remove_component(self, comp_id: str) -> int:
        """Удаляет установленные файлы компонента из записываемой папки models_dir. Возвращает число удалённых файлов."""
        spec = self._manifest_by_id[comp_id]
        removed = 0
        for fspec in spec.files:
            target = self.target_file_path(fspec.rel_path)
            for p in (target, target.with_suffix(target.suffix + ".part")):
                if p.is_file():
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    def download_components(
        self,
        component_ids: list[str],
        progress_cb: Optional[Callable[[DownloadProgress], None]] = None,
        pause_event: Optional[threading.Event] = None,
        cancel_event: Optional[threading.Event] = None,
        verify_sha256: bool = True,
        client_factory: Optional[Callable[[], httpx.Client]] = None,
    ) -> tuple[bool, str]:
        """Скачивает выбранные компоненты атомарно через файлы *.part с поддержкой докачки HTTP Range.
        - pause_event: при установке загрузка ставится на паузу и затем возобновляется через HTTP Range.
        - cancel_event: при установке загрузка немедленно отменяется, а незавершённые *.part файлы удаляются.
        """
        specs = [self._manifest_by_id[cid] for cid in component_ids if cid in self._manifest_by_id]
        if not specs:
            return True, "Нет компонентов для загрузки."

        overall_total = sum(s.total_size_bytes for s in specs)
        overall_done_before = 0
        active_part_files: list[Path] = []
        t_start = time.perf_counter()
        session_downloaded = 0

        def _cleanup_parts() -> None:
            for pf in active_part_files:
                try:
                    if pf.exists():
                        pf.unlink()
                except OSError:
                    pass

        make_client = client_factory or (
            lambda: httpx.Client(
                follow_redirects=True,
                timeout=httpx.Timeout(45.0, connect=20.0),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GTA6SubVoice/1.0"},
            )
        )

        try:
            with make_client() as client:
                for spec in specs:
                    for fspec in spec.files:
                        if cancel_event and cancel_event.is_set():
                            _cleanup_parts()
                            return False, "Загрузка отменена пользователем."

                        target = self.target_file_path(fspec.rel_path)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        part = target.with_suffix(target.suffix + ".part")
                        if part not in active_part_files:
                            active_part_files.append(part)

                        while True:
                            # Ждём снятия паузы перед стартом или возобновлением запроса
                            while pause_event and pause_event.is_set():
                                if cancel_event and cancel_event.is_set():
                                    _cleanup_parts()
                                    return False, "Загрузка отменена пользователем."
                                time.sleep(0.1)

                            existing_bytes = part.stat().st_size if part.is_file() else 0
                            if fspec.expected_size > 0 and existing_bytes > fspec.expected_size:
                                existing_bytes = 0
                                part.unlink(missing_ok=True)

                            headers: dict[str, str] = {}
                            if existing_bytes > 0:
                                headers["Range"] = f"bytes={existing_bytes}-"

                            paused_mid_stream = False
                            with client.stream("GET", fspec.url, headers=headers) as resp:
                                if resp.status_code == 416 and existing_bytes > 0:
                                    # Файл уже полностью скачан на диск
                                    break
                                if resp.status_code not in (200, 206):
                                    return False, f"HTTP {resp.status_code} при загрузке {fspec.rel_path}"

                                # Если сервер проигнорировал Range (вернул 200 вместо 206), перезаписываем файл с 0
                                if resp.status_code == 200 and existing_bytes > 0:
                                    existing_bytes = 0
                                    mode = "wb"
                                elif resp.status_code == 206:
                                    mode = "ab"
                                else:
                                    mode = "wb"

                                content_len = int(resp.headers.get("content-length") or 0)
                                file_total = (
                                    (existing_bytes + content_len)
                                    if content_len > 0
                                    else max(fspec.expected_size, existing_bytes)
                                )
                                file_downloaded = existing_bytes

                                with open(part, mode) as out_f:
                                    for chunk in resp.iter_bytes(chunk_size=131072):
                                        if cancel_event and cancel_event.is_set():
                                            out_f.close()
                                            _cleanup_parts()
                                            return False, "Загрузка отменена пользователем."
                                        if pause_event and pause_event.is_set():
                                            out_f.flush()
                                            paused_mid_stream = True
                                            break
                                        if not chunk:
                                            continue
                                        out_f.write(chunk)
                                        n = len(chunk)
                                        file_downloaded += n
                                        session_downloaded += n

                                        if progress_cb:
                                            elapsed = max(0.05, time.perf_counter() - t_start)
                                            speed_bps = session_downloaded / elapsed
                                            speed_mbps = speed_bps / (1024.0 * 1024.0)
                                            overall_now = overall_done_before + file_downloaded
                                            rem_bytes = max(0, overall_total - overall_now)
                                            eta = rem_bytes / speed_bps if speed_bps > 100 else 0.0
                                            pct = (
                                                min(100.0, (overall_now / overall_total) * 100.0)
                                                if overall_total > 0
                                                else 100.0
                                            )
                                            progress_cb(
                                                DownloadProgress(
                                                    component_id=spec.id,
                                                    file_name=Path(fspec.rel_path).name,
                                                    file_downloaded=file_downloaded,
                                                    file_total=max(file_total, file_downloaded),
                                                    overall_downloaded=overall_now,
                                                    overall_total=max(overall_total, overall_now),
                                                    speed_mbps=speed_mbps,
                                                    eta_seconds=eta,
                                                    percent=pct,
                                                )
                                            )

                            if paused_mid_stream:
                                # Повторно ждём снятия паузы и продолжаем докачку по Range
                                continue
                            break

                        # Проверяем скачанный .part файл перед атомарной заменой
                        actual_size = part.stat().st_size if part.is_file() else 0
                        if fspec.expected_size > 0 and actual_size != fspec.expected_size:
                            return (
                                False,
                                f"Неверный размер {fspec.rel_path}: получено {actual_size}, ожидалось {fspec.expected_size}",
                            )
                        ok_hdr, hdr_msg = validate_file_header(part, fspec.kind)
                        if not ok_hdr:
                            return False, f"Файл {fspec.rel_path} повреждён: {hdr_msg}"
                        if verify_sha256 and fspec.sha256:
                            actual_hash = compute_sha256(part)
                            if actual_hash.lower() != fspec.sha256.lower():
                                return (
                                    False,
                                    f"Ошибка контрольной суммы SHA-256 для {fspec.rel_path}",
                                )

                        os.replace(part, target)
                        if part in active_part_files:
                            active_part_files.remove(part)
                        overall_done_before += max(fspec.expected_size, actual_size)

            return True, "Все выбранные компоненты успешно установлены."
        except httpx.TimeoutException:
            return False, "Таймаут сети. Нажмите «Повторить» — загрузка продолжится с места обрыва."
        except httpx.HTTPError as exc:
            return False, f"Ошибка сети: {exc}. Можно продолжить докачку кнопкой «Повторить»."
        except Exception as exc:
            return False, f"Ошибка установки: {exc}"


def _cli_download_all() -> int:
    """Скачивает и проверяет по SHA-256 все модели (для сборки полной версии в CI)."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass

    def _make_ci_client() -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=30.0),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GTA6SubVoice-CI/1.0"},
        )

    dm = DependencyManager()
    for comp_id in ("silero_v5_6", "rapidocr_cyrillic"):
        ok = False
        last_msg = ""
        for attempt in range(1, 5):
            print(f"Downloading {comp_id} (attempt {attempt}/4)...", flush=True)
            ok, last_msg = dm.download_components(
                [comp_id],
                verify_sha256=True,
                client_factory=_make_ci_client,
            )
            print(f"  result: ok={ok}, msg={last_msg}", flush=True)
            if ok:
                break
            time.sleep(3 * attempt)
        if not ok:
            print(f"::error::Model download failed ({comp_id}): {last_msg}", flush=True)
            return 1

    report = dm.run_preflight(ocr_engine="rapidocr", verify_hash=True)
    for item in report.components:
        print(f"  {item.spec.id}: {item.status} ({item.message})", flush=True)
    if not report.all_ready:
        print("::error::Downloaded models failed SHA-256 preflight!", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    if "--download-all" in sys.argv:
        raise SystemExit(_cli_download_all())

