from __future__ import annotations

import ctypes
import hmac
import ipaddress
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from src.capture import set_current_thread_below_normal
from src.config import ConfigManager
from src.pipeline import SubtitlePipeline
from src.text_filter import StressDictManager
from src.version import APP_VERSION
from src.voice_manager import (
    ALL_SILERO_SPEAKERS,
    FEMALE_SPEAKERS,
    MALE_SPEAKERS,
    VoiceManager,
    clean_character_name,
    is_valid_character_name,
    speaker_display_name,
)

# Алфавит для короткого кода доступа (без похожих символов 0/O и 1/I)
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_access_token(length: int = 8) -> str:
    """Генерирует короткий безопасный код доступа для подключения телефона в Wi-Fi."""
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(max(6, int(length))))


def _ip_priority(ip_str: str, route_ip: Optional[str]) -> tuple[int, str]:
    """Сортирует локальные IPv4-адреса так, чтобы реальный Wi-Fi/LAN адрес был первым."""
    if route_ip and ip_str == route_ip:
        return (0, ip_str)
    try:
        addr = ipaddress.IPv4Address(ip_str)
    except Exception:
        return (99, ip_str)

    if addr.is_loopback or addr.is_link_local:
        return (90, ip_str)

    octets = ip_str.split(".")
    # Типичные домашние Wi-Fi / LAN подсети 192.168.* (кроме 192.168.56.* от VirtualBox)
    if octets[0] == "192" and octets[1] == "168":
        if octets[2] == "56":
            return (50, ip_str)
        return (10, ip_str)
    if octets[0] == "10":
        return (20, ip_str)
    # 172.16.0.0/12 (часто используется WSL/Hyper-V/Docker, поэтому ниже 192.168 и 10.*)
    if addr.is_private:
        return (35, ip_str)
    return (70, ip_str)


def detect_lan_ips() -> list[str]:
    """Возвращает отсортированный список локальных IPv4-адресов ПК для подключения по Wi-Fi."""
    route_ip: Optional[str] = None
    for target in (("192.168.1.1", 80), ("10.255.255.255", 1), ("8.8.8.8", 80)):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(target)
                cand = sock.getsockname()[0]
            finally:
                sock.close()
            if cand and not cand.startswith("127.") and not cand.startswith("169.254."):
                route_ip = cand
                break
        except OSError:
            continue

    found: set[str] = set()
    if route_ip:
        found.add(route_ip)

    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip_str = str(info[4][0])
            if ip_str and not ip_str.startswith("127.") and not ip_str.startswith("169.254."):
                found.add(ip_str)
    except OSError:
        pass

    if not found:
        return ["127.0.0.1"]

    return sorted(found, key=lambda ip: _ip_priority(ip, route_ip))


def open_firewall_port_windows(port: int) -> tuple[bool, str]:
    """Добавляет входящие правила Windows Firewall (для Private и Public Wi-Fi) на программу и TCP-порт."""
    if sys.platform != "win32":
        return False, "Настройка брандмауэра поддерживается только на Windows."

    port_num = max(1024, min(65535, int(port)))
    port_rule = f"GTA6SubVoice Wi-Fi ({port_num})"
    app_rule = "GTA6SubVoice App"
    exe_path = str(Path(sys.executable).resolve())
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # Сначала пробуем без запроса UAC (если программа уже запущена с правами администратора)
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={port_rule}"],
            capture_output=True,
            creationflags=no_win,
        )
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={app_rule}"],
            capture_output=True,
            creationflags=no_win,
        )
        res_app = subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={app_rule}", "dir=in", "action=allow",
                f"program={exe_path}", "profile=any", "enable=yes",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=no_win,
        )
        res_port = subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={port_rule}", "dir=in", "action=allow",
                "protocol=TCP", f"localport={port_num}", "profile=any", "enable=yes",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=no_win,
        )
        if res_app.returncode == 0 and res_port.returncode == 0:
            return (
                True,
                f"Доступ для программы и порта TCP {port_num} разрешён в брандмауэре Windows для всех профилей сети.",
            )
    except Exception:
        pass

    cmd_line = (
        f'/c netsh advfirewall firewall delete rule name="{port_rule}" >nul 2>&1 & '
        f'netsh advfirewall firewall delete rule name="{app_rule}" >nul 2>&1 & '
        f'netsh advfirewall firewall add rule name="{app_rule}" '
        f'dir=in action=allow program="{exe_path}" profile=any enable=yes & '
        f'netsh advfirewall firewall add rule name="{port_rule}" '
        f"dir=in action=allow protocol=TCP localport={port_num} profile=any enable=yes"
    )
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd_line, None, 0)
        if int(rc) > 32:
            return (
                True,
                f"Запрошено разрешение для программы и порта TCP {port_num} (все профили сети, включая Public Wi-Fi).\n\n"
                "Подтвердите окно прав администратора Windows.",
            )
        return False, "Не удалось запустить настройку брандмауэра Windows (операция отменена)."
    except Exception as exc:
        return False, f"Ошибка запуска netsh: {exc}"


class _BoundThreadingHTTPServer(ThreadingHTTPServer):
    """Многопоточный HTTP-сервер с эксклюзивной привязкой порта на Windows."""

    # На Windows SO_REUSEADDR позволяет двум сокетам делить один порт, поэтому ставим SO_EXCLUSIVEADDRUSE
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if sys.platform == "win32":
            excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if excl is not None:
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
                except OSError:
                    pass
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # В оконном --windowed приложении sys.stderr отсутствует; безопасно игнорируем обрывы сокетов клиентов
        return


class MobileCompanionServer:
    """Локальный Wi-Fi HTTP-сервер для быстрого редактирования ударений, слово-замен и персонажей со смартфона."""

    def __init__(
        self,
        *,
        cfg_mgr: ConfigManager,
        stress_dict: StressDictManager,
        voice_mgr: VoiceManager,
        pipeline: SubtitlePipeline,
        on_stress_changed: Optional[Callable[[str, str, str], None]] = None,
        on_character_changed: Optional[Callable[[str, str, str, str], None]] = None,
        on_client_activity: Optional[Callable[[str, int, str], None]] = None,
    ) -> None:
        self.cfg_mgr = cfg_mgr
        self.stress_dict = stress_dict
        self.voice_mgr = voice_mgr
        self.pipeline = pipeline
        self.on_stress_changed = on_stress_changed
        self.on_character_changed = on_character_changed
        self.on_client_activity = on_client_activity

        self._lock = threading.Lock()
        self._httpd: Optional[_BoundThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._bound_port: int = 0
        self._bound_host: str = "0.0.0.0"
        self._token: str = ""
        self._last_error: str = ""
        self._external_hits: int = 0
        self._last_peer_ip: str = ""
        self._last_peer_ts: str = ""

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    @property
    def bound_port(self) -> int:
        with self._lock:
            return self._bound_port

    @property
    def bound_host(self) -> str:
        with self._lock:
            return self._bound_host

    @property
    def external_hits(self) -> int:
        with self._lock:
            return self._external_hits

    @property
    def last_peer_info(self) -> tuple[str, str, int]:
        with self._lock:
            return self._last_peer_ip, self._last_peer_ts, self._external_hits

    @property
    def token(self) -> str:
        with self._lock:
            return self._token

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def set_token(self, token: str) -> None:
        with self._lock:
            self._token = "".join(ch for ch in str(token or "") if ch.isalnum())[:32]

    def build_url(self, ip_address: Optional[str] = None, include_token: bool = True) -> str:
        host = (ip_address or "").strip() or detect_lan_ips()[0]
        port = self.bound_port or int(self.cfg_mgr.config.mobile_sync_port or 8765)
        tok = self.token or str(self.cfg_mgr.config.mobile_sync_token or "")
        if include_token and tok:
            return f"http://{host}:{port}/?t={tok}"
        return f"http://{host}:{port}/"

    def start(self, port: int, token: str, auto_fallback_port: bool = True) -> tuple[bool, int, str]:
        """Запускает HTTP-сервер на 0.0.0.0:<port> (при занятости порта пробует соседние)."""
        self.stop()

        clean_token = "".join(ch for ch in str(token or "") if ch.isalnum())[:32]
        if not clean_token:
            clean_token = generate_access_token()

        base_port = max(1024, min(65535, int(port)))
        candidates = [base_port]
        if auto_fallback_port:
            for offset in range(1, 11):
                p = base_port + offset
                if p <= 65535:
                    candidates.append(p)

        httpd: Optional[_BoundThreadingHTTPServer] = None
        chosen_port = base_port
        last_exc: Optional[Exception] = None
        handler_cls = self._make_handler_class()

        for cand_port in candidates:
            try:
                httpd = _BoundThreadingHTTPServer(("0.0.0.0", cand_port), handler_cls)
                chosen_port = cand_port
                break
            except OSError as exc:
                last_exc = exc
                httpd = None

        if httpd is None:
            err = f"Не удалось открыть порт {base_port}: {last_exc}"
            with self._lock:
                self._last_error = err
            return False, 0, err

        def _serve() -> None:
            set_current_thread_below_normal()
            try:
                httpd.serve_forever(poll_interval=0.4)
            except Exception:
                pass

        th = threading.Thread(target=_serve, name="MobileCompanionHTTP", daemon=True)
        addr = httpd.server_address
        with self._lock:
            self._httpd = httpd
            self._thread = th
            self._bound_port = chosen_port
            self._bound_host = str(addr[0]) if addr else "0.0.0.0"
            self._token = clean_token
            self._last_error = ""
            self._external_hits = 0
            self._last_peer_ip = ""
            self._last_peer_ts = ""
        th.start()
        return True, chosen_port, ""

    def _record_client_hit(self, client_ip: str) -> None:
        ip = (client_ip or "").strip()
        if not ip or ip in ("127.0.0.1", "::1"):
            return
        local_ips = set(detect_lan_ips())
        if ip in local_ips:
            return
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self._external_hits += 1
            self._last_peer_ip = ip
            self._last_peer_ts = ts
            cnt = self._external_hits
        if self.on_client_activity is not None:
            try:
                self.on_client_activity(ip, cnt, ts)
            except Exception:
                pass

    def stop(self) -> None:
        """Корректно останавливает HTTP-сервер и освобождает сокет."""
        with self._lock:
            httpd = self._httpd
            th = self._thread
            self._httpd = None
            self._thread = None
            self._bound_port = 0

        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if th is not None and th.is_alive():
            try:
                th.join(timeout=1.5)
            except Exception:
                pass

    # ------------------------------------------------------------------ Обработка HTTP-запросов
    def _is_authorized(self, handler: BaseHTTPRequestHandler, query_params: dict[str, list[str]]) -> bool:
        expected = self.token
        if not expected:
            return True

        provided = ""
        if "t" in query_params and query_params["t"]:
            provided = str(query_params["t"][0]).strip()
        elif "token" in query_params and query_params["token"]:
            provided = str(query_params["token"][0]).strip()

        if not provided:
            hdr_tok = handler.headers.get("X-Auth-Token", "").strip()
            if hdr_tok:
                provided = hdr_tok

        if not provided:
            raw_cookie = handler.headers.get("Cookie", "")
            if raw_cookie:
                try:
                    c = SimpleCookie()
                    c.load(raw_cookie)
                    if "gta6sv_token" in c:
                        provided = c["gta6sv_token"].value.strip()
                except Exception:
                    pass

        if not provided:
            return False
        return hmac.compare_digest(provided.lower(), expected.lower())

    def _build_state_payload(self) -> dict[str, Any]:
        cfg = self.cfg_mgr.config
        speakers = [
            {
                "id": sp,
                "label": speaker_display_name(sp, short=True),
                "full_label": speaker_display_name(sp, short=False),
                "gender": "female" if sp in FEMALE_SPEAKERS else "male",
            }
            for sp in ALL_SILERO_SPEAKERS
        ]
        profiles = self.voice_mgr.list_profiles()
        characters = [
            {
                "name": name,
                "gender": prof.gender,
                "speaker": prof.speaker,
                "speaker_label": speaker_display_name(prof.speaker, short=True),
                "locked": bool(prof.locked),
            }
            for name, prof in sorted(profiles.items(), key=lambda kv: kv[0].lower())
        ]
        entries = self.stress_dict.list_entries()
        stress = [
            {"word": w, "replacement": r}
            for w, r in sorted(entries.items(), key=lambda kv: kv[0].lower())
        ]
        recent = self.pipeline.get_recent_utterances(limit=20)
        return {
            "ok": True,
            "version": APP_VERSION,
            "pipeline_running": self.pipeline.is_running,
            "voice_mode": cfg.voice_mode,
            "dual_male_speaker": cfg.dual_male_speaker,
            "dual_female_speaker": cfg.dual_female_speaker,
            "single_speaker": cfg.single_speaker,
            "speakers": speakers,
            "recent": recent,
            "characters": characters,
            "stress": stress,
        }

    def _handle_post_stress(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        word = str(body.get("word") or "").strip()
        replacement = str(body.get("replacement") or "").strip()
        old_word = str(body.get("old_word") or "").strip()

        if not word or not replacement:
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": "Укажите и исходное слово, и вариант с ударением или заменой.",
            }

        if old_word and old_word != word:
            self.stress_dict.delete_entry(old_word)

        self.stress_dict.set_entry(word, replacement)
        self.pipeline.text_filter.reset_dedup()
        if self.on_stress_changed is not None:
            try:
                self.on_stress_changed("set", word, replacement)
            except Exception:
                pass
        return HTTPStatus.OK, {"ok": True, "word": word, "replacement": replacement}

    def _handle_delete_stress(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        word = str(body.get("word") or "").strip()
        if not word:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Не указано слово для удаления."}
        removed = self.stress_dict.delete_entry(word)
        if removed:
            self.pipeline.text_filter.reset_dedup()
            if self.on_stress_changed is not None:
                try:
                    self.on_stress_changed("delete", word, "")
                except Exception:
                    pass
        return HTTPStatus.OK, {"ok": True, "removed": removed}

    def _handle_post_character(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        raw_name = str(body.get("name") or "").strip()
        cleaned_name = clean_character_name(raw_name)
        if not is_valid_character_name(cleaned_name):
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": "Введите корректное имя персонажа (минимум 2 буквы без смешивания кириллицы и латиницы).",
            }

        raw_gender = body.get("gender")
        gender = raw_gender if raw_gender in ("male", "female") else None

        raw_speaker = body.get("speaker")
        speaker = raw_speaker if raw_speaker in ALL_SILERO_SPEAKERS else None

        locked_raw = body.get("locked")
        locked: Optional[bool] = None if locked_raw is None else bool(locked_raw)

        existing = self.voice_mgr.get_profile(cleaned_name)

        # Если меняют только пол без явного указания нового диктора — подбираем диктора нового пола
        if gender in ("male", "female") and speaker is None:
            if existing is not None:
                cur_sp = existing.speaker
                cur_sp_is_female = cur_sp in FEMALE_SPEAKERS
                want_female = gender == "female"
                if cur_sp_is_female != want_female:
                    speaker = (
                        self.cfg_mgr.config.dual_female_speaker
                        if want_female
                        else self.cfg_mgr.config.dual_male_speaker
                    )
                    if speaker not in ALL_SILERO_SPEAKERS:
                        speaker = "kseniya" if want_female else "eugene"
            else:
                speaker = (
                    self.cfg_mgr.config.dual_female_speaker
                    if gender == "female"
                    else self.cfg_mgr.config.dual_male_speaker
                )

        # Если выбран конкретный диктор, а пол не указан — согласуем пол с диктором
        if speaker is not None and gender is None:
            gender = "female" if speaker in FEMALE_SPEAKERS else "male"

        # Выбор диктора со смартфона сразу закрепляет голос, чтобы он действовал и в 2-голосном режиме
        if speaker is not None and locked is None:
            locked = True

        prof = self.voice_mgr.add_or_update_profile(
            cleaned_name,
            gender=gender,
            speaker=speaker,
            locked=locked,
        )
        if prof is None:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Не удалось сохранить профиль персонажа."}

        self.pipeline.text_filter.reset_dedup()
        if self.on_character_changed is not None:
            try:
                self.on_character_changed("set", cleaned_name, prof.gender, prof.speaker)
            except Exception:
                pass
        return HTTPStatus.OK, {
            "ok": True,
            "character": {
                "name": cleaned_name,
                "gender": prof.gender,
                "speaker": prof.speaker,
                "speaker_label": speaker_display_name(prof.speaker, short=True),
                "locked": prof.locked,
            },
        }

    def _handle_delete_character(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        name = str(body.get("name") or "").strip()
        if not name:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Не указано имя персонажа."}
        removed = self.voice_mgr.delete_profile(name)
        if removed:
            self.pipeline.text_filter.reset_dedup()
            if self.on_character_changed is not None:
                try:
                    self.on_character_changed("delete", name, "", "")
                except Exception:
                    pass
        return HTTPStatus.OK, {"ok": True, "removed": removed}

    def _handle_post_test_speech(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        text = str(body.get("text") or "").strip()[:300]
        if not text:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Введите текст для проверки озвучки."}
        speaker = str(body.get("speaker") or "").strip()
        if speaker not in ALL_SILERO_SPEAKERS:
            speaker = self.cfg_mgr.config.dual_male_speaker
            if speaker not in ALL_SILERO_SPEAKERS:
                speaker = "eugene"
        label = str(body.get("label") or "Тест с телефона").strip()[:40]
        self.pipeline.speak_test_async(text=text, speaker=speaker, label=label)
        return HTTPStatus.OK, {"ok": True}

    def _make_handler_class(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class _MobileHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 10

            def log_message(self, format: str, *args: Any) -> None:
                # Отключаем вывод HTTP-логов в stdout/stderr при работе GUI-приложения
                return

            def _send_bytes(
                self,
                status: int,
                content_type: str,
                payload: bytes,
                extra_headers: Optional[dict[str, str]] = None,
            ) -> None:
                self.close_connection = True
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            def _send_json(self, status: int, obj: dict[str, Any]) -> None:
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self._send_bytes(status, "application/json; charset=utf-8", raw)

            def _read_json_body(self) -> Optional[dict[str, Any]]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return None
                if length <= 0 or length > 262144:
                    return None
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    return None
                return data if isinstance(data, dict) else None

            def do_OPTIONS(self) -> None:
                self._send_bytes(HTTPStatus.NO_CONTENT, "text/plain; charset=utf-8", b"")

            def do_HEAD(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/favicon.ico":
                    self._send_bytes(HTTPStatus.NO_CONTENT, "image/x-icon", b"")
                    return
                if path == "/health" or path == "/" or path == "/index.html":
                    self.close_connection = True
                    self.send_response(int(HTTPStatus.OK))
                    self.send_header("Content-Type", "text/html; charset=utf-8" if path != "/health" else "application/json; charset=utf-8")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                self.send_response(int(HTTPStatus.NOT_FOUND))
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self) -> None:
                if self.client_address:
                    owner._record_client_hit(str(self.client_address[0]))
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                path = parsed.path

                if path == "/favicon.ico":
                    self._send_bytes(HTTPStatus.NO_CONTENT, "image/x-icon", b"")
                    return

                if path == "/health":
                    self._send_json(HTTPStatus.OK, {"ok": True, "version": APP_VERSION})
                    return

                authed = owner._is_authorized(self, query)

                if path == "/" or path == "/index.html":
                    if not authed:
                        html = _render_auth_page().encode("utf-8")
                        self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html)
                        return

                    tok = owner.token
                    headers: dict[str, str] = {}
                    if tok:
                        headers["Set-Cookie"] = f"gta6sv_token={tok}; Path=/; SameSite=Lax"
                    html = _render_mobile_app_html(initial_token=tok).encode("utf-8")
                    self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", html, extra_headers=headers)
                    return

                if not authed:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Требуется код доступа."})
                    return

                if path == "/api/state":
                    self._send_json(HTTPStatus.OK, owner._build_state_payload())
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Маршрут не найден."})

            def do_POST(self) -> None:
                if self.client_address:
                    owner._record_client_hit(str(self.client_address[0]))
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                path = parsed.path

                if not owner._is_authorized(self, query):
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Неверный или отсутствующий код доступа."})
                    return

                body = self._read_json_body()
                if body is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Ожидался корректный JSON."})
                    return

                if path == "/api/stress":
                    st, res = owner._handle_post_stress(body)
                    self._send_json(st, res)
                elif path == "/api/stress/delete":
                    st, res = owner._handle_delete_stress(body)
                    self._send_json(st, res)
                elif path == "/api/character":
                    st, res = owner._handle_post_character(body)
                    self._send_json(st, res)
                elif path == "/api/character/delete":
                    st, res = owner._handle_delete_character(body)
                    self._send_json(st, res)
                elif path == "/api/test":
                    st, res = owner._handle_post_test_speech(body)
                    self._send_json(st, res)
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Маршрут не найден."})

        return _MobileHandler


def _render_auth_page() -> str:
    """Лёгкая страница ввода кода доступа, если телефон открыл адрес без QR-параметра."""
    return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>GTA6SubVoice — Вход</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    padding: 20px; background: #0b0a16; color: #f4f3fa;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .card {
    width: 100%; max-width: 380px; background: #151326; border: 1px solid #2b264a;
    border-radius: 16px; padding: 22px; box-shadow: 0 12px 32px rgba(0,0,0,0.45);
  }
  h1 { margin: 0 0 8px; font-size: 20px; color: #ffffff; }
  p { margin: 0 0 16px; font-size: 14px; color: #b4afc9; line-height: 1.45; }
  input {
    width: 100%; padding: 13px 14px; border-radius: 11px; border: 1px solid #35305a;
    background: #0e0d1d; color: #ffffff; font-size: 18px; letter-spacing: 2px;
    text-transform: uppercase; text-align: center; outline: none;
  }
  input:focus { border-color: #00d9b5; }
  button {
    width: 100%; margin-top: 12px; padding: 13px; border: 0; border-radius: 11px;
    background: #00d9b5; color: #091517; font-size: 16px; font-weight: 700; cursor: pointer;
  }
</style>
</head>
<body>
  <form class="card" method="get" action="/">
    <h1>GTA6SubVoice • Wi-Fi</h1>
    <p>Отсканируйте QR-код на вкладке «Настройки» в программе на ПК или введите код доступа вручную:</p>
    <input name="t" placeholder="КОД ДОСТУПА" autocomplete="off" required />
    <button type="submit">Подключиться</button>
  </form>
</body>
</html>"""


# Векторные SVG-иконки (получены через mcp-universal-icons из коллекций Lucide и Tabler)
_WEB_SVG_ICONS: dict[str, str] = {
    "volume": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298zM16 9a5 5 0 0 1 0 6m3.364 3.364a9 9 0 0 0 0-12.728"/></svg>',
    "pencil": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497zM15 5l4 4"/></svg>',
    "trash": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 11v6m4-6v6m5-11v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    "plus": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14m-7-7v14"/></svg>',
    "x": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
    "male": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 14a5 5 0 1 0 10 0a5 5 0 1 0-10 0m14-9l-5.4 5.4M19 5h-5m5 0v5"/></svg>',
    "female": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 9a5 5 0 1 0 10 0A5 5 0 1 0 7 9m5 5v7m-3-3h6"/></svg>',
    "radio": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16.247 7.761a6 6 0 0 1 0 8.478m2.828-11.306a10 10 0 0 1 0 14.134m-14.15 0a10 10 0 0 1 0-14.134m2.828 11.306a6 6 0 0 1 0-8.478"/><circle cx="12" cy="12" r="2"/></svg>',
    "book": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v16m8.001-2A2 2 0 0 0 22 17V5a2 2 0 0 0-1.999-2L16 3.002A5 5 0 0 0 12 5a5 5 0 0 0-4-2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 1.999 2H8a5 5 0 0 1 4 2a5 5 0 0 1 4-2z"/></svg>',
    "users": '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3.128a4 4 0 0 1 0 7.744M22 21v-2a4 4 0 0 0-3-3.87"/><circle cx="9" cy="7" r="4"/></svg>',
}


def _render_mobile_app_html(initial_token: str) -> str:
    """Возвращает самодостаточный адаптивный веб-пульт для смартфона без внешних ресурсов."""
    safe_tok_json = json.dumps(initial_token)
    safe_icons_json = json.dumps(_WEB_SVG_ICONS, ensure_ascii=False)
    ico_radio = _WEB_SVG_ICONS["radio"]
    ico_book = _WEB_SVG_ICONS["book"]
    ico_users = _WEB_SVG_ICONS["users"]
    ico_plus = _WEB_SVG_ICONS["plus"]
    ico_vol = _WEB_SVG_ICONS["volume"]
    ico_trash = _WEB_SVG_ICONS["trash"]
    ico_male = _WEB_SVG_ICONS["male"]
    ico_female = _WEB_SVG_ICONS["female"]
    ico_x = _WEB_SVG_ICONS["x"]
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>GTA6SubVoice — Пульт Wi-Fi</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0b0a16;
    --card: #151326;
    --card2: #1c1933;
    --border: #2c274d;
    --text: #f4f3fa;
    --muted: #9a96b0;
    --mint: #00d9b5;
    --pink: #ff5b99;
    --orange: #ff9f43;
    --danger: #ff4d6d;
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding-bottom: calc(24px + env(safe-area-inset-bottom));
  }}
  header {{
    position: sticky; top: 0; z-index: 20;
    background: rgba(11, 10, 22, 0.94); backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border); padding: 12px 14px 10px;
  }}
  .top-row {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
  .brand {{ font-size: 16px; font-weight: 800; letter-spacing: 0.2px; }}
  .brand span {{ color: var(--mint); }}
  .status-pill {{
    display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
    border-radius: 999px; background: var(--card2); border: 1px solid var(--border);
    font-size: 12px; color: var(--muted); white-space: nowrap;
  }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--mint); }}
  .dot.off {{ background: var(--orange); }}
  .dot.err {{ background: var(--danger); }}
  .tabs {{
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 10px;
  }}
  .tab-btn {{
    border: 1px solid var(--border); background: var(--card); color: var(--muted);
    border-radius: 10px; padding: 9px 6px; font-size: 13px; font-weight: 700; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  }}
  .ico {{ width: 15px; height: 15px; flex-shrink: 0; display: inline-block; vertical-align: middle; }}
  .tab-btn.active {{
    background: linear-gradient(135deg, rgba(0,217,181,0.18), rgba(255,91,153,0.16));
    border-color: var(--mint); color: #ffffff;
  }}
  main {{ padding: 12px 14px; max-width: 720px; margin: 0 auto; }}
  .pane {{ display: none; }}
  .pane.active {{ display: block; }}
  .hint {{
    font-size: 13px; color: var(--muted); background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 10px 12px; margin-bottom: 12px; line-height: 1.4;
  }}
  .warn-banner {{
    font-size: 13px; color: #ffe3b8; background: rgba(255, 159, 67, 0.14);
    border: 1px solid rgba(255, 159, 67, 0.45); border-radius: 12px; padding: 10px 12px; margin-bottom: 12px;
  }}
  .toolbar {{ display: flex; gap: 8px; margin-bottom: 12px; }}
  input[type="text"], input[type="search"], select {{
    width: 100%; min-height: 44px; padding: 10px 12px; border-radius: 11px;
    border: 1px solid var(--border); background: #0e0d1d; color: #ffffff;
    font-size: 16px; outline: none;
  }}
  input:focus, select:focus {{ border-color: var(--mint); }}
  .btn {{
    min-height: 44px; padding: 0 14px; border-radius: 11px; border: 1px solid var(--border);
    background: var(--card2); color: var(--text); font-size: 14px; font-weight: 700;
    cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    white-space: nowrap;
  }}
  .btn-primary {{ background: var(--mint); color: #081718; border-color: var(--mint); }}
  .btn-accent {{ background: rgba(255, 91, 153, 0.18); border-color: var(--pink); color: #ffd4e5; }}
  .btn-danger {{ background: rgba(255, 77, 109, 0.14); border-color: rgba(255, 77, 109, 0.45); color: #ffb3c1; }}
  .btn-sm {{ min-height: 36px; padding: 0 10px; font-size: 13px; border-radius: 9px; }}
  .card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 14px;
    padding: 12px; margin-bottom: 10px;
  }}
  .card-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    margin-bottom: 8px; flex-wrap: wrap;
  }}
  .speaker-badges {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
  .badge {{
    font-size: 12px; font-weight: 700; padding: 3px 8px; border-radius: 999px;
    background: var(--card2); border: 1px solid var(--border); color: var(--text);
  }}
  .badge.m {{ border-color: rgba(0, 217, 181, 0.5); color: #8af7e3; }}
  .badge.f {{ border-color: rgba(255, 91, 153, 0.55); color: #ffc2da; }}
  .time-lbl {{ font-size: 12px; color: var(--muted); }}
  .word-flow {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
  .word-chip {{
    border: 1px solid #35305a; background: #1d1a36; color: #ffffff;
    border-radius: 9px; padding: 7px 10px; font-size: 15px; line-height: 1.2; cursor: pointer;
  }}
  .word-chip:active {{ transform: scale(0.97); border-color: var(--mint); }}
  .tts-line {{ font-size: 12px; color: var(--muted); word-break: break-word; }}
  .dict-row {{
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
  }}
  .dict-main {{ min-width: 0; flex: 1; }}
  .dict-word {{ font-size: 15px; font-weight: 700; color: #ffffff; word-break: break-word; }}
  .dict-repl {{ font-size: 14px; color: var(--mint); margin-top: 2px; word-break: break-word; }}
  .row-actions {{ display: flex; gap: 6px; flex-shrink: 0; }}
  .char-controls {{ display: grid; grid-template-columns: auto 1fr auto auto; gap: 6px; margin-top: 8px; align-items: center; }}
  .seg {{ display: inline-flex; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
  .seg button {{
    min-height: 38px; padding: 0 12px; border: 0; background: #0e0d1d; color: var(--muted);
    font-size: 14px; font-weight: 700; cursor: pointer;
  }}
  .seg button.active-m {{ background: rgba(0,217,181,0.22); color: #8af7e3; }}
  .seg button.active-f {{ background: rgba(255,91,153,0.25); color: #ffc2da; }}
  .modal-backdrop {{
    position: fixed; inset: 0; z-index: 50; background: rgba(6, 5, 14, 0.78);
    display: none; align-items: flex-end; justify-content: center;
  }}
  .modal-backdrop.open {{ display: flex; }}
  .sheet {{
    width: 100%; max-width: 560px; background: #161429; border-top: 1px solid #3b3568;
    border-radius: 18px 18px 0 0; padding: 16px 16px calc(20px + env(safe-area-inset-bottom));
    max-height: 90vh; overflow-y: auto;
  }}
  .sheet-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }}
  .sheet-title {{ font-size: 17px; font-weight: 800; }}
  .field-lbl {{ display: block; font-size: 12px; color: var(--muted); margin: 10px 0 5px; font-weight: 600; }}
  .vowel-box {{
    display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; padding: 8px;
    border-radius: 11px; background: #0e0d1d; border: 1px solid var(--border);
  }}
  .ch-btn {{
    min-width: 34px; height: 38px; padding: 0 8px; border-radius: 8px; border: 1px solid #2e2952;
    background: #1a1732; color: #b4afc9; font-size: 16px; font-weight: 700; cursor: pointer;
  }}
  .ch-btn.vowel {{
    border-color: rgba(0, 217, 181, 0.5); color: #ffffff; background: rgba(0, 217, 181, 0.14);
  }}
  .ch-btn.stressed {{
    border-color: var(--pink); background: rgba(255, 91, 153, 0.25); color: #ffffff;
  }}
  .voice-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 6px; }}
  .voice-chip {{
    min-height: 44px; padding: 8px 10px; border-radius: 10px; border: 1px solid var(--border);
    background: #0e0d1d; color: var(--text); font-size: 14px; font-weight: 700;
    cursor: pointer; text-align: left;
  }}
  .voice-chip.selected {{ border-color: var(--mint); background: rgba(0, 217, 181, 0.18); color: #ffffff; }}
  .sheet-actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 14px; }}
  .toast {{
    position: fixed; left: 50%; bottom: calc(18px + env(safe-area-inset-bottom));
    transform: translateX(-50%); z-index: 80; background: #1e1b38; color: #ffffff;
    border: 1px solid var(--mint); border-radius: 999px; padding: 10px 16px;
    font-size: 13px; font-weight: 700; box-shadow: 0 8px 24px rgba(0,0,0,0.5);
    display: none; max-width: 92vw; text-align: center;
  }}
  .toast.err {{ border-color: var(--danger); color: #ffd4dc; }}
  .toast.show {{ display: block; }}
  .empty {{ text-align: center; color: var(--muted); padding: 28px 12px; font-size: 14px; }}
</style>
</head>
<body>
<header>
  <div class="top-row">
    <div class="brand">GTA6<span>SubVoice</span> • Wi-Fi</div>
    <div class="status-pill"><span class="dot" id="connDot"></span><span id="connText">Подключение…</span></div>
  </div>
  <div class="tabs">
    <button class="tab-btn active" data-tab="live">{ico_radio}Эфир</button>
    <button class="tab-btn" data-tab="stress">{ico_book}Ударения</button>
    <button class="tab-btn" data-tab="chars">{ico_users}Персонажи</button>
  </div>
</header>

<main>
  <section id="pane-live" class="pane active">
    <div class="hint">Нажмите на <b>любое слово</b> в последней фразе, чтобы поправить ударение или замену, либо на кнопку <b>Пол / Голос</b> рядом с именем говорящего.</div>
    <div id="liveModeWarn" class="warn-banner" style="display:none;"></div>
    <div id="liveList"><div class="empty">Ожидаем озвученные субтитры из игры…</div></div>
  </section>

  <section id="pane-stress" class="pane">
    <div class="toolbar">
      <input type="search" id="stressSearch" placeholder="Поиск слова или замены…">
      <button class="btn btn-primary" id="btnNewStress">{ico_plus}Добавить</button>
    </div>
    <div id="stressList"></div>
  </section>

  <section id="pane-chars" class="pane">
    <div class="toolbar">
      <input type="search" id="charSearch" placeholder="Поиск персонажа…">
      <button class="btn btn-primary" id="btnNewChar">{ico_plus}Персонаж</button>
    </div>
    <div id="charsModeWarn" class="warn-banner" style="display:none;"></div>
    <div id="charsList"></div>
  </section>
</main>

<!-- Шторка редактирования ударения или слово-замены -->
<div class="modal-backdrop" id="stressModal">
  <div class="sheet">
    <div class="sheet-head">
      <div class="sheet-title" id="stressModalTitle">Ударение или замена</div>
      <button class="btn btn-sm" id="btnCloseStress">{ico_x}Закрыть</button>
    </div>
    <label class="field-lbl" for="inpStressWord">Слово в субтитрах (или фраза)</label>
    <input type="text" id="inpStressWord" placeholder="Например: Вайс-Сити">
    <label class="field-lbl" for="inpStressRepl">Как произносить (+ перед ударной гласной или слово-замена)</label>
    <input type="text" id="inpStressRepl" placeholder="Например: В+айс-С+ити">
    <label class="field-lbl">Быстро поставить ударение (нажмите на гласную):</label>
    <div class="vowel-box" id="vowelBox"></div>
    <div class="sheet-actions">
      <button class="btn" id="btnClearPlus">Убрать «+»</button>
      <button class="btn btn-accent" id="btnTestStress">{ico_vol}Тест на ПК</button>
    </div>
    <div class="sheet-actions">
      <button class="btn btn-danger" id="btnDelStressModal" style="display:none;">{ico_trash}Удалить</button>
      <button class="btn btn-primary" id="btnSaveStress" style="grid-column: span 2;">Сохранить и применить</button>
    </div>
  </div>
</div>

<!-- Шторка редактирования персонажа (пол и голос) -->
<div class="modal-backdrop" id="charModal">
  <div class="sheet">
    <div class="sheet-head">
      <div class="sheet-title" id="charModalTitle">Персонаж</div>
      <button class="btn btn-sm" id="btnCloseChar">{ico_x}Закрыть</button>
    </div>
    <label class="field-lbl" for="inpCharName">Имя персонажа (как в субтитрах)</label>
    <input type="text" id="inpCharName" placeholder="Например: Лусия">
    <label class="field-lbl">Пол персонажа</label>
    <div class="seg" style="width:100%;">
      <button type="button" id="btnGenderM" style="flex:1; min-height:44px; display:inline-flex; align-items:center; justify-content:center; gap:6px;">{ico_male}Мужской (М)</button>
      <button type="button" id="btnGenderF" style="flex:1; min-height:44px; display:inline-flex; align-items:center; justify-content:center; gap:6px;">{ico_female}Женский (Ж)</button>
    </div>
    <label class="field-lbl">Голос озвучки (Silero v5.6)</label>
    <div class="voice-grid" id="voiceGrid"></div>
    <div class="sheet-actions">
      <button class="btn btn-accent" id="btnTestChar">{ico_vol}Тест на ПК</button>
      <button class="btn btn-primary" id="btnSaveChar">Сохранить</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const TOKEN = {safe_tok_json};
const ICONS = {safe_icons_json};
const VOWELS = new Set(["а","е","ё","и","о","у","ы","э","ю","я","А","Е","Ё","И","О","У","Ы","Э","Ю","Я"]);
let STATE = {{ speakers: [], recent: [], characters: [], stress: [], voice_mode: "dual_mj" }};
let activeTab = "live";
let editingOldWord = "";
let selectedCharGender = "male";
let selectedCharSpeaker = "eugene";
let editingCharLocked = false;
let toastTimer = null;

function showToast(msg, isErr = false) {{
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {{ el.className = "toast"; }}, 2600);
}}

async function apiFetch(url, options = {{}}) {{
  const headers = Object.assign({{"Content-Type": "application/json"}}, options.headers || {{}});
  if (TOKEN) headers["X-Auth-Token"] = TOKEN;
  const resp = await fetch(url, Object.assign({{}}, options, {{ headers }}));
  const data = await resp.json().catch(() => ({{ ok: false, error: "Ошибка ответа сервера" }}));
  if (!resp.ok || !data.ok) {{
    throw new Error(data.error || ("HTTP " + resp.status));
  }}
  return data;
}}

function setConnStatus(ok, text) {{
  const dot = document.getElementById("connDot");
  const lbl = document.getElementById("connText");
  dot.className = "dot" + (ok ? "" : " err");
  lbl.textContent = text;
}}

function modeLabel(m) {{
  if (m === "single") return "1 голос";
  if (m === "dual_mj") return "2 голоса (М/Ж)";
  return "Многоголосый";
}}

function renderModeBanner(elId) {{
  const el = document.getElementById(elId);
  if (STATE.voice_mode === "single") {{
    el.style.display = "block";
    el.textContent = "На ПК сейчас выбран режим «1 голос» — смена пола и голоса сохранится для персонажа, но начнёт звучать после включения «2 голоса (М/Ж)» или «Многоголосый».";
  }} else {{
    el.style.display = "none";
  }}
}}

function cleanTokenWord(raw) {{
  return raw.replace(/^[«»"'(\\[{{]+|[»"'.,!?;:)\\]}}…]+$/g, "").trim();
}}

function setBtnIconLabel(btn, iconName, text) {{
  btn.innerHTML = (ICONS[iconName] || "") + (text ? ("<span>" + text + "</span>") : "");
}}

function renderLive() {{
  renderModeBanner("liveModeWarn");
  const box = document.getElementById("liveList");
  box.textContent = "";
  if (!STATE.recent || STATE.recent.length === 0) {{
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Пока нет озвученных фраз. Как только в игре пойдут субтитры, они появятся здесь в реальном времени.";
    box.appendChild(empty);
    return;
  }}
  for (const item of STATE.recent) {{
    const card = document.createElement("div");
    card.className = "card";

    const head = document.createElement("div");
    head.className = "card-head";
    const badges = document.createElement("div");
    badges.className = "speaker-badges";

    const nameB = document.createElement("span");
    nameB.className = "badge";
    nameB.textContent = item.speaker ? item.speaker : "Без имени";
    badges.appendChild(nameB);

    const gB = document.createElement("span");
    gB.className = "badge " + (item.gender === "female" ? "f" : "m");
    gB.textContent = (item.gender === "female" ? "Ж" : "М") + " • " + (item.voice_label || item.voice);
    badges.appendChild(gB);

    const tB = document.createElement("span");
    tB.className = "time-lbl";
    tB.textContent = item.ts || "";
    badges.appendChild(tB);

    head.appendChild(badges);

    const editCharBtn = document.createElement("button");
    editCharBtn.className = "btn btn-sm btn-accent";
    setBtnIconLabel(editCharBtn, item.speaker ? "pencil" : "plus", item.speaker ? "Пол / Голос" : "Персонаж");
    editCharBtn.addEventListener("click", () => {{
      const existing = STATE.characters.find(c => c.name.toLowerCase() === (item.speaker || "").toLowerCase());
      if (existing) {{
        openCharModal(existing.name, existing.gender, existing.speaker, false);
      }} else {{
        openCharModal(item.speaker || "", item.gender || "male", item.voice || "eugene", true);
      }}
    }});
    head.appendChild(editCharBtn);
    card.appendChild(head);

    const wordsWrap = document.createElement("div");
    wordsWrap.className = "word-flow";
    const parts = String(item.raw_text || "").split(/\\s+/).filter(Boolean);
    for (const part of parts) {{
      const clean = cleanTokenWord(part);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "word-chip";
      chip.textContent = part;
      chip.addEventListener("click", () => {{
        const targetWord = clean || part;
        const found = STATE.stress.find(s => s.word.toLowerCase() === targetWord.toLowerCase());
        if (found) {{
          openStressModal(found.word, found.replacement, found.word);
        }} else {{
          openStressModal(targetWord, targetWord, "");
        }}
      }});
      wordsWrap.appendChild(chip);
    }}
    card.appendChild(wordsWrap);

    if (item.tts_text && item.tts_text !== item.raw_text) {{
      const tts = document.createElement("div");
      tts.className = "tts-line";
      tts.textContent = "Озвучено как: " + item.tts_text;
      card.appendChild(tts);
    }}
    box.appendChild(card);
  }}
}}

function renderStress() {{
  const q = document.getElementById("stressSearch").value.trim().toLowerCase();
  const box = document.getElementById("stressList");
  box.textContent = "";
  const items = STATE.stress.filter(it => !q || it.word.toLowerCase().includes(q) || it.replacement.toLowerCase().includes(q));
  if (items.length === 0) {{
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = q ? "Ничего не найдено." : "Словарь пуст.";
    box.appendChild(empty);
    return;
  }}
  for (const it of items.slice(0, 150)) {{
    const card = document.createElement("div");
    card.className = "card dict-row";
    const main = document.createElement("div");
    main.className = "dict-main";
    const w = document.createElement("div");
    w.className = "dict-word";
    w.textContent = it.word;
    const r = document.createElement("div");
    r.className = "dict-repl";
    r.textContent = it.replacement;
    main.appendChild(w);
    main.appendChild(r);
    main.addEventListener("click", () => openStressModal(it.word, it.replacement, it.word));
    card.appendChild(main);

    const acts = document.createElement("div");
    acts.className = "row-actions";
    const bt = document.createElement("button");
    bt.className = "btn btn-sm";
    bt.title = "Тест на ПК";
    setBtnIconLabel(bt, "volume", "");
    bt.addEventListener("click", () => testSpeech(it.replacement + ".", STATE.dual_male_speaker || "eugene", it.word));
    const be = document.createElement("button");
    be.className = "btn btn-sm btn-accent";
    be.title = "Редактировать";
    setBtnIconLabel(be, "pencil", "");
    be.addEventListener("click", () => openStressModal(it.word, it.replacement, it.word));
    const bd = document.createElement("button");
    bd.className = "btn btn-sm btn-danger";
    bd.title = "Удалить";
    setBtnIconLabel(bd, "trash", "");
    bd.addEventListener("click", () => deleteStressEntry(it.word, it.replacement));
    acts.appendChild(bt);
    acts.appendChild(be);
    acts.appendChild(bd);
    card.appendChild(acts);
    box.appendChild(card);
  }}
}}

async function deleteStressEntry(word, repl) {{
  if (!word) return false;
  const label = repl ? (word + " → " + repl) : word;
  if (!confirm("Удалить правило «" + label + "» из словаря?")) return false;
  try {{
    await apiFetch("/api/stress/delete", {{ method: "POST", body: JSON.stringify({{ word }}) }});
    showToast("Удалено: " + word);
    await loadState();
    return true;
  }} catch (e) {{
    showToast(e.message, true);
    return false;
  }}
}}

function renderChars() {{
  renderModeBanner("charsModeWarn");
  const q = document.getElementById("charSearch").value.trim().toLowerCase();
  const box = document.getElementById("charsList");
  box.textContent = "";
  const items = STATE.characters.filter(c => !q || c.name.toLowerCase().includes(q));
  if (items.length === 0) {{
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = q ? "Персонажи не найдены." : "Список персонажей пока пуст — добавьте вручную или дождитесь первого диалога.";
    box.appendChild(empty);
    return;
  }}
  for (const ch of items) {{
    const card = document.createElement("div");
    card.className = "card";

    const head = document.createElement("div");
    head.className = "card-head";
    const nm = document.createElement("div");
    nm.style.fontWeight = "800";
    nm.style.fontSize = "16px";
    nm.textContent = ch.name;
    head.appendChild(nm);

    const delBtn = document.createElement("button");
    delBtn.className = "btn btn-sm btn-danger";
    setBtnIconLabel(delBtn, "trash", "Удалить");
    delBtn.addEventListener("click", async () => {{
      if (!confirm("Удалить персонажа «" + ch.name + "»?")) return;
      try {{
        await apiFetch("/api/character/delete", {{ method: "POST", body: JSON.stringify({{ name: ch.name }}) }});
        showToast("Удалён: " + ch.name);
        await loadState();
      }} catch (e) {{ showToast(e.message, true); }}
    }});
    head.appendChild(delBtn);
    card.appendChild(head);

    const ctrls = document.createElement("div");
    ctrls.className = "char-controls";

    const seg = document.createElement("div");
    seg.className = "seg";
    const bm = document.createElement("button");
    bm.textContent = "М";
    if (ch.gender === "male") bm.className = "active-m";
    bm.addEventListener("click", () => quickUpdateChar(ch.name, "male", null));
    const bf = document.createElement("button");
    bf.textContent = "Ж";
    if (ch.gender === "female") bf.className = "active-f";
    bf.addEventListener("click", () => quickUpdateChar(ch.name, "female", null));
    seg.appendChild(bm);
    seg.appendChild(bf);
    ctrls.appendChild(seg);

    const sel = document.createElement("select");
    for (const sp of STATE.speakers) {{
      const opt = document.createElement("option");
      opt.value = sp.id;
      opt.textContent = sp.label + (sp.gender === "female" ? " (Ж)" : " (М)");
      if (sp.id === ch.speaker) opt.selected = true;
      sel.appendChild(opt);
    }}
    sel.addEventListener("change", () => quickUpdateChar(ch.name, null, sel.value));
    ctrls.appendChild(sel);

    const btest = document.createElement("button");
    btest.className = "btn btn-sm";
    btest.title = "Тест на ПК";
    setBtnIconLabel(btest, "volume", "");
    btest.addEventListener("click", () => testSpeech("Привет! Меня зовут " + ch.name + ".", ch.speaker, ch.name));
    ctrls.appendChild(btest);

    card.appendChild(ctrls);
    box.appendChild(card);
  }}
}}

async function quickUpdateChar(name, gender, speaker) {{
  try {{
    const payload = {{ name, gender }};
    if (speaker) {{
      payload.speaker = speaker;
      payload.locked = true;
    }}
    const res = await apiFetch("/api/character", {{ method: "POST", body: JSON.stringify(payload) }});
    const c = res.character;
    showToast(c.name + ": " + (c.gender === "female" ? "Ж" : "М") + ", " + c.speaker_label);
    await loadState();
  }} catch (e) {{
    showToast(e.message, true);
  }}
}}

function renderVowelHelper() {{
  const box = document.getElementById("vowelBox");
  box.textContent = "";
  const val = document.getElementById("inpStressRepl").value;
  if (!val) {{
    box.textContent = "Введите слово выше, чтобы нажать на ударную гласную.";
    return;
  }}
  const chars = Array.from(val);
  chars.forEach((ch, idx) => {{
    if (ch === "+") return;
    const prevWasPlus = idx > 0 && chars[idx - 1] === "+";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ch-btn" + (VOWELS.has(ch) ? " vowel" : "") + (prevWasPlus ? " stressed" : "");
    btn.textContent = ch === " " ? "␣" : ch;
    btn.addEventListener("click", () => {{
      if (!VOWELS.has(ch)) return;
      // Переставляем '+' перед выбранной гласной внутри этого слова/сегмента
      const withoutPlus = [];
      let targetPos = 0;
      for (let i = 0; i < chars.length; i++) {{
        if (chars[i] === "+") continue;
        if (i === idx) targetPos = withoutPlus.length;
        withoutPlus.push(chars[i]);
      }}
      withoutPlus.splice(targetPos, 0, "+");
      document.getElementById("inpStressRepl").value = withoutPlus.join("");
      renderVowelHelper();
    }});
    box.appendChild(btn);
  }});
}}

function openStressModal(word, repl, oldWord) {{
  editingOldWord = oldWord || "";
  document.getElementById("inpStressWord").value = word || "";
  document.getElementById("inpStressRepl").value = repl || "";
  document.getElementById("stressModalTitle").textContent = oldWord ? "Изменить слово / ударение" : "Новое ударение / замена";
  const delBtn = document.getElementById("btnDelStressModal");
  const saveBtn = document.getElementById("btnSaveStress");
  if (oldWord) {{
    delBtn.style.display = "inline-flex";
    saveBtn.style.gridColumn = "auto";
  }} else {{
    delBtn.style.display = "none";
    saveBtn.style.gridColumn = "span 2";
  }}
  renderVowelHelper();
  document.getElementById("stressModal").classList.add("open");
}}

function renderCharVoiceGrid() {{
  const grid = document.getElementById("voiceGrid");
  grid.textContent = "";
  const sorted = [...STATE.speakers].sort((a, b) => {{
    const am = a.gender === selectedCharGender ? 0 : 1;
    const bm = b.gender === selectedCharGender ? 0 : 1;
    return am - bm;
  }});
  for (const sp of sorted) {{
    const b = document.createElement("button");
    b.type = "button";
    b.className = "voice-chip" + (sp.id === selectedCharSpeaker ? " selected" : "");
    b.textContent = sp.label + " • " + (sp.gender === "female" ? "Ж" : "М");
    b.addEventListener("click", () => {{
      selectedCharSpeaker = sp.id;
      selectedCharGender = sp.gender;
      updateCharGenderButtons();
      renderCharVoiceGrid();
    }});
    grid.appendChild(b);
  }}
}}

function updateCharGenderButtons() {{
  const bm = document.getElementById("btnGenderM");
  const bf = document.getElementById("btnGenderF");
  bm.className = selectedCharGender === "male" ? "active-m" : "";
  bf.className = selectedCharGender === "female" ? "active-f" : "";
}}

function openCharModal(name, gender, speaker, isNew) {{
  document.getElementById("inpCharName").value = name || "";
  selectedCharGender = (gender === "female") ? "female" : "male";
  selectedCharSpeaker = speaker || (selectedCharGender === "female" ? "kseniya" : "eugene");
  document.getElementById("charModalTitle").textContent = isNew ? "Добавить персонажа" : ("Настройка: " + name);
  updateCharGenderButtons();
  renderCharVoiceGrid();
  document.getElementById("charModal").classList.add("open");
}}

async function testSpeech(text, speaker, label) {{
  try {{
    await apiFetch("/api/test", {{ method: "POST", body: JSON.stringify({{ text, speaker, label }}) }});
    showToast("Тест отправлен на ПК");
  }} catch (e) {{
    showToast(e.message, true);
  }}
}}

async function loadState() {{
  try {{
    const data = await apiFetch("/api/state");
    STATE = data;
    setConnStatus(true, "ПК онлайн • " + modeLabel(data.voice_mode));
    renderLive();
    renderStress();
    renderChars();
  }} catch (e) {{
    setConnStatus(false, "Нет связи с ПК");
  }}
}}

// Привязка UI-событий
document.querySelectorAll(".tab-btn").forEach(btn => {{
  btn.addEventListener("click", () => {{
    activeTab = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".pane").forEach(p => p.classList.toggle("active", p.id === "pane-" + activeTab));
  }});
}});

document.getElementById("stressSearch").addEventListener("input", renderStress);
document.getElementById("charSearch").addEventListener("input", renderChars);
document.getElementById("inpStressRepl").addEventListener("input", renderVowelHelper);

document.getElementById("btnNewStress").addEventListener("click", () => openStressModal("", "", ""));
document.getElementById("btnNewChar").addEventListener("click", () => openCharModal("", "male", STATE.dual_male_speaker || "eugene", true));
document.getElementById("btnCloseStress").addEventListener("click", () => document.getElementById("stressModal").classList.remove("open"));
document.getElementById("btnCloseChar").addEventListener("click", () => document.getElementById("charModal").classList.remove("open"));
document.getElementById("stressModal").addEventListener("click", (e) => {{
  if (e.target.id === "stressModal") document.getElementById("stressModal").classList.remove("open");
}});
document.getElementById("charModal").addEventListener("click", (e) => {{
  if (e.target.id === "charModal") document.getElementById("charModal").classList.remove("open");
}});

document.getElementById("btnClearPlus").addEventListener("click", () => {{
  const el = document.getElementById("inpStressRepl");
  el.value = el.value.replace(/\\+/g, "");
  renderVowelHelper();
}});

document.getElementById("btnTestStress").addEventListener("click", () => {{
  const repl = document.getElementById("inpStressRepl").value.trim();
  const word = document.getElementById("inpStressWord").value.trim();
  if (!repl) return showToast("Введите произношение или замену", true);
  testSpeech(repl + ".", STATE.dual_male_speaker || "eugene", word || "Слово");
}});

document.getElementById("btnSaveStress").addEventListener("click", async () => {{
  const word = document.getElementById("inpStressWord").value.trim();
  const replacement = document.getElementById("inpStressRepl").value.trim();
  try {{
    await apiFetch("/api/stress", {{
      method: "POST",
      body: JSON.stringify({{ word, replacement, old_word: editingOldWord }}),
    }});
    document.getElementById("stressModal").classList.remove("open");
    showToast("Сохранено: " + word + " → " + replacement);
    await loadState();
  }} catch (e) {{ showToast(e.message, true); }}
}});

document.getElementById("btnDelStressModal").addEventListener("click", async () => {{
  const word = editingOldWord || document.getElementById("inpStressWord").value.trim();
  const repl = document.getElementById("inpStressRepl").value.trim();
  const ok = await deleteStressEntry(word, repl);
  if (ok) {{
    document.getElementById("stressModal").classList.remove("open");
  }}
}});

document.getElementById("btnGenderM").addEventListener("click", () => {{
  selectedCharGender = "male";
  const curSp = STATE.speakers.find(s => s.id === selectedCharSpeaker);
  if (!curSp || curSp.gender !== "male") selectedCharSpeaker = STATE.dual_male_speaker || "eugene";
  updateCharGenderButtons();
  renderCharVoiceGrid();
}});

document.getElementById("btnGenderF").addEventListener("click", () => {{
  selectedCharGender = "female";
  const curSp = STATE.speakers.find(s => s.id === selectedCharSpeaker);
  if (!curSp || curSp.gender !== "female") selectedCharSpeaker = STATE.dual_female_speaker || "kseniya";
  updateCharGenderButtons();
  renderCharVoiceGrid();
}});

document.getElementById("btnTestChar").addEventListener("click", () => {{
  const name = document.getElementById("inpCharName").value.trim() || "Персонаж";
  testSpeech("Привет! Я " + name + ", проверка голоса.", selectedCharSpeaker, name);
}});

document.getElementById("btnSaveChar").addEventListener("click", async () => {{
  const name = document.getElementById("inpCharName").value.trim();
  try {{
    const res = await apiFetch("/api/character", {{
      method: "POST",
      body: JSON.stringify({{
        name,
        gender: selectedCharGender,
        speaker: selectedCharSpeaker,
        locked: true,
      }}),
    }});
    const c = res.character;
    document.getElementById("charModal").classList.remove("open");
    showToast(c.name + ": " + (c.gender === "female" ? "Ж" : "М") + ", " + c.speaker_label);
    await loadState();
  }} catch (e) {{ showToast(e.message, true); }}
}});

loadState();
setInterval(() => {{
  const anyModal = document.getElementById("stressModal").classList.contains("open")
    || document.getElementById("charModal").classList.contains("open");
  if (!anyModal && activeTab === "live") {{
    loadState();
  }}
}}, 1800);
</script>
</body>
</html>"""
