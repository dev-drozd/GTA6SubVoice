from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

from src.paths import APP_DIR, DATA_DIR, USER_ROOT, is_dir_writable
from src.version import APP_NAME, APP_VERSION, REPO_URL

GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/dev-drozd/GTA6SubVoice/releases/latest"
_VERSION_PART_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class ReleaseInfo:
    """Метаданные последнего релиза GTA6SubVoice из GitHub Releases."""

    tag_name: str
    version: str
    title: str
    notes: str
    html_url: str
    asset_name: str
    download_url: str
    asset_size: int
    checksum_url: Optional[str] = None


@dataclass(frozen=True)
class UpdateDownloadProgress:
    """Прогресс загрузки архива обновления."""

    downloaded_bytes: int
    total_bytes: int
    percent: float
    speed_mbps: float


def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Преобразует строку версии вроде 'v1.0.2' или '1.10.0' в кортеж целых чисел для сравнения."""
    parts = [int(m.group(0)) for m in _VERSION_PART_RE.finditer(version_str or "")]
    return tuple(parts) if parts else (0,)


def is_newer_version(remote_tag: str, local_version: str = APP_VERSION) -> bool:
    """Возвращает True, если версия remote_tag строго новее локальной local_version."""
    rem = list(parse_version_tuple(remote_tag))
    loc = list(parse_version_tuple(local_version))
    max_len = max(len(rem), len(loc))
    rem.extend([0] * (max_len - len(rem)))
    loc.extend([0] * (max_len - len(loc)))
    return tuple(rem) > tuple(loc)


def is_self_update_supported() -> bool:
    """Самозамена файлов доступна только в собранной .exe версии (PyInstaller), а не при запуске из исходников."""
    return bool(getattr(sys, "frozen", False))


def _default_http_client(timeout_sec: float = 15.0) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_sec, connect=8.0),
        headers={
            "User-Agent": f"GTA6SubVoice/{APP_VERSION}",
            "Accept": "application/vnd.github+json",
        },
    )


def fetch_latest_release(
    api_url: str = GITHUB_LATEST_RELEASE_API,
    client_factory: Optional[Callable[[], httpx.Client]] = None,
) -> Optional[ReleaseInfo]:
    """Запрашивает последний опубликованный релиз с GitHub Releases API."""
    make_client = client_factory or (lambda: _default_http_client(10.0))
    with make_client() as client:
        resp = client.get(api_url)
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, dict):
        return None
    if data.get("draft") or data.get("prerelease"):
        return None

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None

    assets = data.get("assets") or []
    zip_asset = None
    checksum_url: Optional[str] = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        low = name.lower()
        dl = str(asset.get("browser_download_url") or "")
        if not dl:
            continue
        if low == "sha256sums.txt":
            checksum_url = dl
        elif low.endswith(".zip"):
            if zip_asset is None or "win64" in low:
                zip_asset = asset

    if zip_asset is None:
        return None

    return ReleaseInfo(
        tag_name=tag,
        version=tag.lstrip("vV"),
        title=str(data.get("name") or f"{APP_NAME} {tag}"),
        notes=str(data.get("body") or "").strip(),
        html_url=str(data.get("html_url") or f"{REPO_URL}/releases/latest"),
        asset_name=str(zip_asset.get("name") or "update.zip"),
        download_url=str(zip_asset.get("browser_download_url") or ""),
        asset_size=int(zip_asset.get("size") or 0),
        checksum_url=checksum_url,
    )


def _compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def _parse_sha256sums(text: str, target_filename: str) -> Optional[str]:
    target_low = target_filename.strip().lower()
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            fname = parts[-1].lstrip("*").strip().lower()
            if fname == target_low:
                return parts[0].lower()
    return None


def download_and_verify_release(
    release: ReleaseInfo,
    dest_dir: Path,
    progress_cb: Optional[Callable[[UpdateDownloadProgress], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    client_factory: Optional[Callable[[], httpx.Client]] = None,
) -> Path:
    """Скачивает релизный .zip-архив и при наличии SHA256SUMS.txt проверяет его контрольную сумму."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / release.asset_name
    part_path = zip_path.with_suffix(".zip.part")
    make_client = client_factory or (lambda: _default_http_client(45.0))

    expected_sha: Optional[str] = None
    with make_client() as client:
        if release.checksum_url:
            try:
                cs_resp = client.get(release.checksum_url)
                if cs_resp.status_code == 200:
                    expected_sha = _parse_sha256sums(cs_resp.text, release.asset_name)
            except Exception:
                expected_sha = None

        t0 = time.perf_counter()
        downloaded = 0
        with client.stream("GET", release.download_url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or release.asset_size or 0)
            with open(part_path, "wb") as out_f:
                for chunk in resp.iter_bytes(chunk_size=131072):
                    if cancel_event and cancel_event.is_set():
                        out_f.close()
                        part_path.unlink(missing_ok=True)
                        raise InterruptedError("Обновление отменено пользователем.")
                    if not chunk:
                        continue
                    out_f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        elapsed = max(0.05, time.perf_counter() - t0)
                        bps = downloaded / elapsed
                        pct = min(100.0, (downloaded / total) * 100.0) if total > 0 else 0.0
                        progress_cb(
                            UpdateDownloadProgress(
                                downloaded_bytes=downloaded,
                                total_bytes=max(total, downloaded),
                                percent=pct,
                                speed_mbps=bps / (1024.0 * 1024.0),
                            )
                        )

    if release.asset_size > 0 and part_path.stat().st_size != release.asset_size:
        part_path.unlink(missing_ok=True)
        raise ValueError("Размер скачанного архива не совпадает с размером в релизе.")

    if expected_sha:
        actual_sha = _compute_file_sha256(part_path)
        if actual_sha != expected_sha:
            part_path.unlink(missing_ok=True)
            raise ValueError("Контрольная сумма SHA-256 архива обновления не совпала.")

    os.replace(part_path, zip_path)
    return zip_path


def extract_release_zip(zip_path: Path, extract_dir: Path) -> Path:
    """Безопасно распаковывает архив и возвращает корневую папку, где находится GTA6SubVoice.exe."""
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    base_resolved = extract_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            dest_member = (extract_dir / member.filename).resolve()
            if not dest_member.is_relative_to(base_resolved):
                raise ValueError(f"Небезопасный путь внутри архива: {member.filename}")
        zf.extractall(extract_dir)

    exe_name = f"{APP_NAME}.exe"
    if (extract_dir / exe_name).is_file():
        return extract_dir

    for sub in extract_dir.iterdir():
        if sub.is_dir() and (sub / exe_name).is_file():
            return sub

    raise FileNotFoundError(f"В распакованном обновлении не найден файл {exe_name}.")


def prepare_extracted_user_data(source_app_dir: Path, current_data_dir: Path = DATA_DIR) -> None:
    """Защищает локальные настройки и базу персонажей от перезаписи и объединяет словарь ударений."""
    src_data = source_app_dir / "data"
    if not src_data.is_dir():
        return

    # Никогда не затираем пользовательский config.json и characters.json
    for preserve_name in ("config.json", "characters.json"):
        if (current_data_dir / preserve_name).is_file():
            src_file = src_data / preserve_name
            if src_file.is_file():
                src_file.unlink()

    # Объединяем словарь ударений: новые записи из релиза + сохранённые пользовательские правки сверху
    user_stress = current_data_dir / "stress_dict.json"
    src_stress = src_data / "stress_dict.json"
    if user_stress.is_file() and src_stress.is_file():
        try:
            new_entries = json.loads(src_stress.read_text(encoding="utf-8"))
            old_entries = json.loads(user_stress.read_text(encoding="utf-8"))
            if isinstance(new_entries, dict) and isinstance(old_entries, dict):
                merged = dict(new_entries)
                merged.update(old_entries)
                src_stress.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def _ps_quote(value: str) -> str:
    """Экранирует строку для безопасной передачи в PowerShell-литерале '...'."""
    return "'" + str(value).replace("'", "''") + "'"


def _resolve_powershell_exe() -> str:
    """Возвращает полный путь к системному Windows PowerShell 5.1+."""
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(sys_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    return "powershell.exe"


def build_windows_updater_script(
    pid: int,
    source_dir: Path,
    target_dir: Path,
    staging_root: Path,
    exe_name: str = f"{APP_NAME}.exe",
) -> str:
    """Генерирует PowerShell-скрипт (.ps1) с окном прогресса, откатом при сбое и перезапуском."""
    pid_int = int(pid)
    ps_src = _ps_quote(str(source_dir))
    ps_dst = _ps_quote(str(target_dir))
    ps_stg = _ps_quote(str(staging_root))
    ps_exe = _ps_quote(str(exe_name))

    return f"""param(
    [switch]$NoGui,
    [switch]$SkipRelaunch
)

$ErrorActionPreference = 'Stop'
$TargetPid = {pid_int}
$SrcDir = {ps_src}
$DstDir = {ps_dst}
$StgDir = {ps_stg}
$ExeName = {ps_exe}
$SelfScriptPath = $PSCommandPath
$LogPath = Join-Path $env:TEMP 'gta6subvoice_update.log'

function Write-UpdateLog([string]$Message) {{
    try {{
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Add-Content -LiteralPath $LogPath -Value "[$ts] $Message" -Encoding UTF8 -ErrorAction SilentlyContinue
    }} catch {{}}
}}

Write-UpdateLog "=== Старт установщика обновления (PID=$TargetPid) ==="
Write-UpdateLog "SrcDir=$SrcDir | DstDir=$DstDir | StgDir=$StgDir | Exe=$ExeName"

$Form = $null
$LblTitle = $null
$LblStatus = $null
$Progress = $null
$LblHint = $null
$BtnRetry = $null
$BtnClose = $null

function Pump-Gui {{
    if (-not $NoGui -and $null -ne $Form) {{
        [System.Windows.Forms.Application]::DoEvents()
    }}
}}

function Set-Progress([int]$Percent, [string]$Text, [bool]$IsError = $false) {{
    Write-UpdateLog "[$Percent%] $Text"
    if ($NoGui -or $null -eq $Form) {{
        return
    }}
    $p = [Math]::Max(0, [Math]::Min(100, $Percent))
    $Progress.Value = $p
    $LblStatus.Text = $Text
    if ($IsError) {{
        $LblTitle.Text = 'Ошибка обновления'
        $LblTitle.ForeColor = [System.Drawing.Color]::FromArgb(255, 77, 109)
        $LblStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 168, 180)
        $LblHint.Text = "Подробности сохранены в лог: $LogPath"
        $BtnRetry.Visible = $true
        $BtnClose.Visible = $true
    }} else {{
        $LblTitle.Text = 'Обновление GTA VI SubVoice'
        $LblTitle.ForeColor = [System.Drawing.Color]::FromArgb(0, 229, 195)
        $LblStatus.ForeColor = [System.Drawing.Color]::FromArgb(232, 230, 245)
        $LblHint.Text = 'Пожалуйста, подождите — окно закроется автоматически.'
        $BtnRetry.Visible = $false
        $BtnClose.Visible = $false
    }}
    Pump-Gui
}}

function Wait-WithPump([int]$TotalMs) {{
    $elapsed = 0
    while ($elapsed -lt $TotalMs) {{
        Pump-Gui
        $step = [Math]::Min(100, $TotalMs - $elapsed)
        [System.Threading.Thread]::Sleep($step)
        $elapsed += $step
    }}
}}

function Invoke-WithRetry([string]$OpName, [int]$MaxAttempts, [int]$DelayMs, [scriptblock]$Action) {{
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {{
        try {{
            & $Action
            return
        }} catch {{
            $errMsg = $_.Exception.Message
            Write-UpdateLog "Попытка $attempt/$MaxAttempts ($OpName) не удалась: $errMsg"
            if ($attempt -ge $MaxAttempts) {{
                throw
            }}
            Wait-WithPump $DelayMs
        }}
    }}
}}

function Copy-MergedDirectory([string]$FromDir, [string]$ToDir, [int]$BasePercent, [int]$SpanPercent, [bool]$ProtectUserData = $false) {{
    if (-not (Test-Path -LiteralPath $ToDir)) {{
        New-Item -ItemType Directory -Path $ToDir -Force | Out-Null
    }}
    $allFiles = @(Get-ChildItem -LiteralPath $FromDir -Recurse -File -Force)
    $allDirs = @(Get-ChildItem -LiteralPath $FromDir -Recurse -Directory -Force)
    $fromRoot = $FromDir.TrimEnd('\\', '/')

    foreach ($d in $allDirs) {{
        $rel = $d.FullName.Substring($fromRoot.Length).TrimStart('\\', '/')
        $targetSub = Join-Path $ToDir $rel
        if (-not (Test-Path -LiteralPath $targetSub)) {{
            New-Item -ItemType Directory -Path $targetSub -Force | Out-Null
        }}
    }}

    $total = [Math]::Max(1, $allFiles.Count)
    $idx = 0
    foreach ($f in $allFiles) {{
        $idx++
        $rel = $f.FullName.Substring($fromRoot.Length).TrimStart('\\', '/')
        $destFile = Join-Path $ToDir $rel
        if ($ProtectUserData) {{
            $relNorm = $rel.Replace('/', '\\').ToLowerInvariant()
            if (($relNorm -eq 'config.json' -or $relNorm -eq 'characters.json') -and (Test-Path -LiteralPath $destFile)) {{
                continue
            }}
        }}
        $destParent = Split-Path -Path $destFile -Parent
        if (-not (Test-Path -LiteralPath $destParent)) {{
            New-Item -ItemType Directory -Path $destParent -Force | Out-Null
        }}
        Invoke-WithRetry "Копирование $rel" 15 350 {{
            Copy-Item -LiteralPath $f.FullName -Destination $destFile -Force
        }}
        if (($idx % 30 -eq 0) -or ($idx -eq $total)) {{
            $pct = $BasePercent + [int](($idx / $total) * $SpanPercent)
            Set-Progress $pct "Устанавливаю файлы ($idx/$total)..."
        }} else {{
            Pump-Gui
        }}
    }}
}}

function Wait-TargetProcessesExit {{
    Set-Progress 10 'Ожидаю закрытия старой версии...'
    for ($i = 0; $i -lt 40; $i++) {{
        $proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        if ($null -eq $proc) {{
            break
        }}
        Wait-WithPump 200
    }}

    $proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
    if ($null -ne $proc) {{
        Write-UpdateLog "PID $TargetPid всё ещё активен после ожидания — принудительное завершение."
        Set-Progress 15 'Завершаю зависший процесс программы...'
        Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
        for ($j = 0; $j -lt 15; $j++) {{
            $proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
            if ($null -eq $proc) {{
                break
            }}
            Wait-WithPump 200
        }}
    }}

    # Проверяем, не запущена ли другая копия именно из целевой папки установки
    $expectedExe = (Join-Path $DstDir $ExeName)
    $exeBaseName = [System.IO.Path]::GetFileNameWithoutExtension($ExeName)
    try {{
        $others = @(Get-Process -Name $exeBaseName -ErrorAction SilentlyContinue)
        foreach ($p in $others) {{
            try {{
                if ($p.Path -and ($p.Path -ieq $expectedExe)) {{
                    Write-UpdateLog "Закрываю дополнительный экземпляр PID=$($p.Id) из $expectedExe"
                    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                }}
            }} catch {{}}
        }}
    }} catch {{}}

    # Небольшая пауза, чтобы Windows и Defender гарантированно отпустили DLL
    Wait-WithPump 350
}}

function Run-UpdateTransaction {{
    $DstInternal = Join-Path $DstDir '_internal'
    $BackupInternal = Join-Path $DstDir '_internal_backup'
    $SrcInternal = Join-Path $SrcDir '_internal'
    $DstExe = Join-Path $DstDir $ExeName
    $BackupExe = Join-Path $DstDir ($ExeName + '.bak')
    $MovedInternalFromSrc = $false

    try {{
        if (-not (Test-Path -LiteralPath $SrcDir)) {{
            throw "Папка обновления не найдена: $SrcDir"
        }}
        if (-not (Test-Path -LiteralPath $DstDir)) {{
            New-Item -ItemType Directory -Path $DstDir -Force | Out-Null
        }}

        Wait-TargetProcessesExit

        Set-Progress 25 'Создаю резервную копию...'
        if (Test-Path -LiteralPath $BackupInternal) {{
            Invoke-WithRetry 'Очистка старого бэкапа _internal_backup' 10 350 {{
                Remove-Item -LiteralPath $BackupInternal -Recurse -Force
            }}
        }}
        if (Test-Path -LiteralPath $BackupExe) {{
            Invoke-WithRetry 'Очистка старого бэкапа exe' 10 350 {{
                Remove-Item -LiteralPath $BackupExe -Force
            }}
        }}

        if (Test-Path -LiteralPath $DstInternal) {{
            Invoke-WithRetry 'Перенос _internal в резервную копию' 15 400 {{
                Move-Item -LiteralPath $DstInternal -Destination $BackupInternal -Force
            }}
        }}
        if (Test-Path -LiteralPath $DstExe) {{
            Invoke-WithRetry "Перенос $ExeName в резервную копию" 15 400 {{
                Move-Item -LiteralPath $DstExe -Destination $BackupExe -Force
            }}
        }}

        Set-Progress 40 'Устанавливаю новые файлы...'
        if (Test-Path -LiteralPath $SrcInternal) {{
            $sameRoot = ([System.IO.Path]::GetPathRoot($SrcInternal) -ieq [System.IO.Path]::GetPathRoot($DstDir))
            $fastMoved = $false
            if ($sameRoot) {{
                try {{
                    Invoke-WithRetry 'Быстрый перенос _internal' 5 300 {{
                        Move-Item -LiteralPath $SrcInternal -Destination $DstInternal -Force
                    }}
                    $fastMoved = $true
                    $MovedInternalFromSrc = $true
                }} catch {{
                    Write-UpdateLog "Быстрый Move-Item _internal не удался, перехожу к копированию: $($_.Exception.Message)"
                }}
            }}
            if (-not $fastMoved) {{
                Copy-MergedDirectory -FromDir $SrcInternal -ToDir $DstInternal -BasePercent 42 -SpanPercent 32
            }}
        }}

        Set-Progress 75 'Обновляю исполняемый файл и ресурсы...'
        $topItems = @(Get-ChildItem -LiteralPath $SrcDir -Force)
        foreach ($item in $topItems) {{
            if ($item.Name -eq '_internal') {{
                continue
            }}
            $targetPath = Join-Path $DstDir $item.Name
            if ($item.PSIsContainer) {{
                $isData = ($item.Name -ieq 'data')
                Copy-MergedDirectory -FromDir $item.FullName -ToDir $targetPath -BasePercent 76 -SpanPercent 10 -ProtectUserData:$isData
            }} else {{
                Invoke-WithRetry "Копирование $($item.Name)" 15 400 {{
                    Copy-Item -LiteralPath $item.FullName -Destination $targetPath -Force
                }}
            }}
            Pump-Gui
        }}

        if (-not (Test-Path -LiteralPath $DstExe)) {{
            throw "После копирования не найден главный файл: $DstExe"
        }}

        Set-Progress 90 'Очищаю временные файлы...'
        if (Test-Path -LiteralPath $BackupInternal) {{
            Remove-Item -LiteralPath $BackupInternal -Recurse -Force -ErrorAction SilentlyContinue
        }}
        if (Test-Path -LiteralPath $BackupExe) {{
            Remove-Item -LiteralPath $BackupExe -Force -ErrorAction SilentlyContinue
        }}
        if ($StgDir -and (Test-Path -LiteralPath $StgDir)) {{
            Remove-Item -LiteralPath $StgDir -Recurse -Force -ErrorAction SilentlyContinue
        }}

        if (-not $SkipRelaunch) {{
            Set-Progress 96 'Запускаю обновлённую версию...'
            Start-Process -FilePath $DstExe -ArgumentList '--after-update' -WorkingDirectory $DstDir | Out-Null
        }}
        Set-Progress 100 'Обновление успешно установлено!'
        Wait-WithPump 500
        return $true
    }} catch {{
        $errText = $_.Exception.Message
        Write-UpdateLog "ОШИБКА ОБНОВЛЕНИЯ: $errText"
        try {{
            if ($MovedInternalFromSrc -and (Test-Path -LiteralPath $DstInternal) -and (-not (Test-Path -LiteralPath $SrcInternal))) {{
                Move-Item -LiteralPath $DstInternal -Destination $SrcInternal -Force -ErrorAction SilentlyContinue
            }} elseif ((Test-Path -LiteralPath $BackupInternal) -and (Test-Path -LiteralPath $DstInternal)) {{
                Remove-Item -LiteralPath $DstInternal -Recurse -Force -ErrorAction SilentlyContinue
            }}
            if ((Test-Path -LiteralPath $BackupInternal) -and (-not (Test-Path -LiteralPath $DstInternal))) {{
                Move-Item -LiteralPath $BackupInternal -Destination $DstInternal -Force -ErrorAction SilentlyContinue
            }}
            if (Test-Path -LiteralPath $BackupExe) {{
                Move-Item -LiteralPath $BackupExe -Destination $DstExe -Force -ErrorAction SilentlyContinue
            }}
        }} catch {{
            Write-UpdateLog "Ошибка отката: $($_.Exception.Message)"
        }}
        Set-Progress 0 "Не удалось обновить: $errText" $true
        return $false
    }}
}}

$script:ExitCode = 0
if ($NoGui) {{
    $ok = Run-UpdateTransaction
    if (-not $ok) {{
        $script:ExitCode = 1
    }}
}} else {{
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $Form = New-Object System.Windows.Forms.Form
    $Form.Text = 'Обновление GTA VI SubVoice'
    $Form.ClientSize = New-Object System.Drawing.Size(450, 178)
    $Form.StartPosition = 'CenterScreen'
    $Form.FormBorderStyle = 'FixedDialog'
    $Form.MaximizeBox = $false
    $Form.MinimizeBox = $false
    $Form.ControlBox = $false
    $Form.TopMost = $true
    $Form.BackColor = [System.Drawing.Color]::FromArgb(14, 13, 27)
    $Form.ForeColor = [System.Drawing.Color]::FromArgb(244, 243, 250)

    $LblTitle = New-Object System.Windows.Forms.Label
    $LblTitle.Location = New-Object System.Drawing.Point(20, 16)
    $LblTitle.Size = New-Object System.Drawing.Size(410, 24)
    $LblTitle.Font = New-Object System.Drawing.Font('Segoe UI', 12, [System.Drawing.FontStyle]::Bold)
    $LblTitle.ForeColor = [System.Drawing.Color]::FromArgb(0, 229, 195)
    $LblTitle.Text = 'Обновление GTA VI SubVoice'
    $Form.Controls.Add($LblTitle)

    $LblStatus = New-Object System.Windows.Forms.Label
    $LblStatus.Location = New-Object System.Drawing.Point(20, 46)
    $LblStatus.Size = New-Object System.Drawing.Size(410, 34)
    $LblStatus.Font = New-Object System.Drawing.Font('Segoe UI', 9.5)
    $LblStatus.ForeColor = [System.Drawing.Color]::FromArgb(232, 230, 245)
    $LblStatus.Text = 'Подготовка к установке...'
    $Form.Controls.Add($LblStatus)

    $Progress = New-Object System.Windows.Forms.ProgressBar
    $Progress.Location = New-Object System.Drawing.Point(20, 86)
    $Progress.Size = New-Object System.Drawing.Size(410, 20)
    $Progress.Minimum = 0
    $Progress.Maximum = 100
    $Progress.Style = 'Continuous'
    $Form.Controls.Add($Progress)

    $LblHint = New-Object System.Windows.Forms.Label
    $LblHint.Location = New-Object System.Drawing.Point(20, 114)
    $LblHint.Size = New-Object System.Drawing.Size(410, 22)
    $LblHint.Font = New-Object System.Drawing.Font('Segoe UI', 8.5)
    $LblHint.ForeColor = [System.Drawing.Color]::FromArgb(154, 150, 176)
    $LblHint.Text = 'Пожалуйста, подождите — окно закроется автоматически.'
    $Form.Controls.Add($LblHint)

    $BtnRetry = New-Object System.Windows.Forms.Button
    $BtnRetry.Location = New-Object System.Drawing.Point(242, 140)
    $BtnRetry.Size = New-Object System.Drawing.Size(92, 28)
    $BtnRetry.Text = 'Повторить'
    $BtnRetry.FlatStyle = 'Flat'
    $BtnRetry.BackColor = [System.Drawing.Color]::FromArgb(0, 229, 195)
    $BtnRetry.ForeColor = [System.Drawing.Color]::FromArgb(11, 10, 22)
    $BtnRetry.Visible = $false
    $Form.Controls.Add($BtnRetry)

    $BtnClose = New-Object System.Windows.Forms.Button
    $BtnClose.Location = New-Object System.Drawing.Point(340, 140)
    $BtnClose.Size = New-Object System.Drawing.Size(90, 28)
    $BtnClose.Text = 'Закрыть'
    $BtnClose.FlatStyle = 'Flat'
    $BtnClose.BackColor = [System.Drawing.Color]::FromArgb(35, 33, 60)
    $BtnClose.ForeColor = [System.Drawing.Color]::FromArgb(244, 243, 250)
    $BtnClose.Visible = $false
    $Form.Controls.Add($BtnClose)

    $script:IsRunningUpdate = $false
    $BtnRetry.Add_Click({{
        if ($script:IsRunningUpdate) {{ return }}
        $script:IsRunningUpdate = $true
        $ok = Run-UpdateTransaction
        $script:IsRunningUpdate = $false
        if ($ok) {{
            $script:ExitCode = 0
            $Form.Close()
        }}
    }})
    $BtnClose.Add_Click({{
        $script:ExitCode = 1
        $Form.Close()
    }})
    $Form.Add_Shown({{
        if ($script:IsRunningUpdate) {{ return }}
        $script:IsRunningUpdate = $true
        $ok = Run-UpdateTransaction
        $script:IsRunningUpdate = $false
        if ($ok) {{
            $script:ExitCode = 0
            $Form.Close()
        }}
    }})

    [void]$Form.ShowDialog()
}}

if ($script:ExitCode -eq 0 -and $SelfScriptPath -and (Test-Path -LiteralPath $SelfScriptPath)) {{
    Remove-Item -LiteralPath $SelfScriptPath -Force -ErrorAction SilentlyContinue
}}
exit $script:ExitCode
"""


def create_staging_dir() -> Path:
    """Создаёт временную папку для скачивания и распаковки обновления."""
    base = USER_ROOT if is_dir_writable(USER_ROOT) else Path(tempfile.gettempdir())
    staging = base / "_update_staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def launch_self_update(source_app_dir: Path, staging_root: Path, target_app_dir: Path = APP_DIR) -> None:
    """Готовит и запускает внешний PowerShell-установщик обновления с окном прогресса."""
    if sys.platform != "win32":
        raise RuntimeError("Автообновление поддерживается только на Windows.")

    prepare_extracted_user_data(source_app_dir, current_data_dir=DATA_DIR)
    script_text = build_windows_updater_script(
        pid=os.getpid(),
        source_dir=source_app_dir,
        target_dir=target_app_dir,
        staging_root=staging_root,
    )
    script_path = Path(tempfile.gettempdir()) / f"gta6subvoice_update_{os.getpid()}.ps1"
    # BOM обязателен для корректного чтения кириллицы в Windows PowerShell 5.1
    script_path.write_text(script_text, encoding="utf-8-sig")

    ps_exe = _resolve_powershell_exe()
    ps_args = [
        ps_exe,
        "-NoProfile",
        "-STA",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(script_path),
    ]

    # Если целевая папка защищена от записи обычного пользователя (например Program Files), запрашиваем UAC
    if not is_dir_writable(target_app_dir):
        import ctypes

        arg_line = f'-NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_path}"'
        rc = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            ps_exe,
            arg_line,
            str(tempfile.gettempdir()),
            0,
        )
        if int(rc) <= 32:
            raise PermissionError("Не получены права администратора для обновления папки программы.")
        return

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    subprocess.Popen(
        ps_args,
        cwd=str(tempfile.gettempdir()),
        creationflags=creation_flags,
        close_fds=True,
    )
