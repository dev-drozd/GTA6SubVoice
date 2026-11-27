from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

APP_NAME = "GTA6SubVoice"
APP_VERSION = "1.1.0"

REPO_URL = "https://github.com/dev-drozd/GTA6SubVoice"
AUTHOR_NAME = "Oleksandr Drozd"
AUTHOR_URL = "https://github.com/dev-drozd"

SPONSORS_URL = "https://github.com/sponsors/dev-drozd"
REVOLUT_URL = "https://revolut.me/devdrozd"
BINANCE_PAY_ID = "483490297"


def build_release_notes_ru(tag: str, manual_changelog: str = "", changelog_path: Path | None = None) -> str:
    """Формирует описание релиза для GitHub Releases со списком изменений только на русском языке."""
    tag = (tag or f"v{APP_VERSION}").strip()
    if not tag.startswith("v"):
        tag = f"v{tag}"

    cyr_re = re.compile(r"[А-ЯЁа-яё]")
    changelog_body = (manual_changelog or "").strip()

    # 1. Аннотация тега на русском, если она задана
    if not changelog_body:
        try:
            tag_msg = subprocess.check_output(
                ["git", "for-each-ref", f"refs/tags/{tag}", "--format=%(contents)"],
                text=True,
                encoding="utf-8",
                errors="replace",
            ).strip()
            if tag_msg and cyr_re.search(tag_msg):
                changelog_body = tag_msg
        except Exception:
            pass

    # 2. Раздел версии из CHANGELOG.txt
    if not changelog_body:
        p = changelog_path or (Path(__file__).resolve().parent.parent / "CHANGELOG.txt")
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            tag_clean = tag.lstrip("v")
            pat = re.compile(
                rf"^##\s+v?{re.escape(tag_clean)}\b[^\n]*\n(.*?)(?=^##\s+|\Z)",
                re.MULTILINE | re.DOTALL,
            )
            m = pat.search(text)
            if m and m.group(1).strip():
                changelog_body = m.group(1).strip()

    # 3. Русские сообщения коммитов с предыдущего тега
    if not changelog_body:
        try:
            tags = subprocess.check_output(
                ["git", "tag", "--sort=-v:refname"],
                text=True,
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            prev_tag = next((t.strip() for t in tags if t.strip() and t.strip() != tag), "")
            rev_range = f"{prev_tag}..{tag}" if prev_tag else tag
            commits = subprocess.check_output(
                ["git", "log", rev_range, "--pretty=format:%s"],
                text=True,
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            ru_commits = [f"- {c.strip()}" for c in commits if c.strip() and cyr_re.search(c)]
            if ru_commits:
                changelog_body = "\n".join(ru_commits)
        except Exception:
            pass

    if not changelog_body:
        changelog_body = "- Обновление и улучшение стабильности программы."

    lines = [
        f"## {APP_NAME} {tag} (Windows x64)",
        "",
        f"Автономная портативная сборка — скачайте **`{APP_NAME}-{tag}-win64.zip`**, распакуйте архив и запустите `{APP_NAME}.exe` (все модели Silero v5.6 и RapidOCR уже включены).",
        "",
        "### Что изменилось",
        changelog_body,
        "",
        "### Минимальные требования",
        "- Windows 10 (1903+) / Windows 11 (64-bit)",
        "- Процессор от 4 потоков с поддержкой AVX2",
        "- От 8 ГБ ОЗУ",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--release-notes":
        _tag = sys.argv[2]
        _out = Path(sys.argv[3])
        _manual = os.environ.get("MANUAL_CHANGELOG", "")
        _notes = build_release_notes_ru(_tag, manual_changelog=_manual)
        _out.write_text(_notes, encoding="utf-8")
        print(_notes)
