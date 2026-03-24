#!/usr/bin/env python3

import argparse
import array
import collections
import importlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
import wave
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOCAL_PYTHON_DEPS = APP_DIR / ".python-deps"
GUI_STATE_PATH = APP_DIR / ".codex-gui-state.json"
DEFAULT_CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEFAULT_CODEX_ARGS = os.environ.get("CODEX_GUI_CODEX_ARGS", "")
DEFAULT_STT_LANGUAGE = os.environ.get("CODEX_GUI_STT_LANGUAGE", "de-DE")
DEFAULT_APP_TITLE = "Codex Terminal GUI"

if LOCAL_PYTHON_DEPS.exists():
    sys.path.insert(0, str(LOCAL_PYTHON_DEPS))

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Vte", "2.91")

from gi.repository import Gio, GLib, Gtk, Pango, Vte


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GTK GUI with embedded Codex CLI terminal."
    )
    parser.add_argument(
        "--working-dir",
        default=os.getcwd(),
        help="Working directory passed to Codex with -C. Defaults to the current directory.",
    )
    parser.add_argument(
        "--codex-bin",
        default=DEFAULT_CODEX_BIN,
        help="Path to the codex executable. Defaults to $CODEX_BIN or 'codex'.",
    )
    parser.add_argument(
        "--codex-args",
        default=DEFAULT_CODEX_ARGS,
        help="Extra arguments appended to the Codex startup command.",
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_STT_LANGUAGE,
        help="Language code used by the optional SpeechRecognition backend.",
    )
    parser.add_argument(
        "--title",
        default=DEFAULT_APP_TITLE,
        help="Window title.",
    )
    return parser.parse_args()


class DictationBackend:
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2
    CHANNELS = 1
    CHUNK_MS = 100
    CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHUNK_MS // 1000
    DEFAULT_SILENCE_SECONDS = 1.5
    MAX_UTTERANCE_SECONDS = 30
    PRE_ROLL_CHUNKS = 4
    DEFAULT_START_THRESHOLD = 550

    def __init__(
        self,
        language: str,
        *,
        provider: str = "standard",
        command_template: str | None = None,
        api_base_url: str = "",
        api_key: str = "",
        api_model: str = "gpt-4o-mini-transcribe",
        recorder_preference: str = "auto",
        alsa_device: str = "",
        silence_seconds: float = DEFAULT_SILENCE_SECONDS,
        start_threshold: int = DEFAULT_START_THRESHOLD,
    ) -> None:
        self.language = language
        self.provider = provider
        self.command_template = (
            command_template
            if command_template is not None
            else os.environ.get("CODEX_GUI_TRANSCRIBE_CMD", "").strip()
        )
        self.api_base_url = api_base_url.strip()
        self.api_key = api_key.strip()
        self.api_model = api_model.strip() or "gpt-4o-mini-transcribe"
        self.recorder_preference = recorder_preference
        self.alsa_device = alsa_device.strip()
        self.silence_seconds = silence_seconds
        self.start_threshold = start_threshold
        self.recorder_cmd = self._detect_recorder_stream()
        self.recorder_process: subprocess.Popen[str] | None = None
        self.installing = False
        self.bootstrap_error: str | None = None
        self.live_mode = False
        self.stop_event = threading.Event()
        self.live_thread: threading.Thread | None = None

    def apply_settings(
        self,
        *,
        language: str,
        provider: str,
        command_template: str,
        api_base_url: str,
        api_key: str,
        api_model: str,
        recorder_preference: str,
        alsa_device: str,
        silence_seconds: float,
        start_threshold: int,
    ) -> None:
        self.language = language
        self.provider = provider
        self.command_template = command_template.strip()
        self.api_base_url = api_base_url.strip()
        self.api_key = api_key.strip()
        self.api_model = api_model.strip() or "gpt-4o-mini-transcribe"
        self.recorder_preference = recorder_preference
        self.alsa_device = alsa_device.strip()
        self.silence_seconds = silence_seconds
        self.start_threshold = start_threshold
        self.recorder_cmd = self._detect_recorder_stream()

    def available(self) -> bool:
        if not self.recorder_cmd:
            return False
        if self.provider == "standard":
            return self._speech_recognition_available()
        if self.provider == "command":
            return bool(self.command_template)
        if self.provider in {"openai_compatible", "elevenlabs"}:
            return bool(self.api_base_url.strip() or self._default_api_endpoint())
        return False

    def description(self) -> str:
        if not self._has_capture_device():
            return "Kein Aufnahmegeraet erkannt. Bitte Mikrofon oder Audio-Quelle pruefen."
        if self.provider == "command":
            return (
                "Diktat bereit. Aufnahme ueber lokales Mikrofon, Transkription ueber externes Kommando."
            )
        if self.provider == "openai_compatible":
            return "Diktat bereit. Aufnahme lokal, Transkription ueber OpenAI-kompatiblen Endpoint."
        if self.provider == "elevenlabs":
            return "Diktat bereit. Aufnahme lokal, Transkription ueber ElevenLabs."
        if self._speech_recognition_available():
            return (
                "Live-Diktat bereit. Klick startet dauerhaftes Lauschen mit Auto-Senden."
            )
        if self.installing:
            return "Installiere automatisch die Spracherkennungs-Abhaengigkeit SpeechRecognition."
        if self.bootstrap_error:
            return self.bootstrap_error
        if self.provider == "standard" and self._pip_available():
            return (
                "Diktier-Engine wird automatisch nachinstalliert. "
                "Klick zum Starten, erneut klicken zum Stoppen."
            )
        return "Transkriptions-Provider ist noch nicht vollstaendig konfiguriert."

    def uses_toggle_recording(self) -> bool:
        return True

    def is_live(self) -> bool:
        return self.live_mode

    def start_live(self, on_text, on_status, on_error, on_state_change, on_level) -> None:
        if self.live_mode:
            return
        if not self.available():
            on_error(self.description())
            return

        self.live_mode = True
        self.stop_event.clear()
        self.live_thread = threading.Thread(
            target=self._live_loop,
            args=(on_text, on_status, on_error, on_state_change, on_level),
            daemon=True,
        )
        self.live_thread.start()
        on_state_change(True)

    def stop_live(self) -> None:
        self.live_mode = False
        self.stop_event.set()
        if self.recorder_process and self.recorder_process.poll() is None:
            self.recorder_process.terminate()

    def bootstrap_async(self, on_ready, on_status, on_error) -> None:
        if self.available():
            on_ready(self.description())
            return
        if not self._has_capture_device():
            on_error(self.description())
            return
        if self.provider != "standard":
            on_error(self.description())
            return
        if self.installing:
            on_status(self.description())
            return
        if not self._pip_available():
            on_error(self.description())
            return

        self.installing = True
        self.bootstrap_error = None
        on_status("Installiere SpeechRecognition fuer Diktat...")

        def worker() -> None:
            try:
                LOCAL_PYTHON_DEPS.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--target",
                        str(LOCAL_PYTHON_DEPS),
                        "--upgrade",
                        "SpeechRecognition",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if str(LOCAL_PYTHON_DEPS) not in sys.path:
                    sys.path.insert(0, str(LOCAL_PYTHON_DEPS))
                importlib.invalidate_caches()
                self.installing = False
                if result.returncode != 0 or not self._speech_recognition_available():
                    stderr = result.stderr.strip()
                    stdout = result.stdout.strip()
                    details = stderr or stdout or "pip lieferte keinen Fehlertext."
                    self.bootstrap_error = f"Automatische Installation fehlgeschlagen: {details}"
                    GLib.idle_add(on_error, self.bootstrap_error)
                    return
                GLib.idle_add(on_ready, "SpeechRecognition automatisch installiert. Diktat ist bereit.")
            except Exception as exc:
                self.installing = False
                self.bootstrap_error = f"Automatische Installation fehlgeschlagen: {exc}"
                GLib.idle_add(on_error, self.bootstrap_error)

        threading.Thread(target=worker, daemon=True).start()

    def _speech_recognition_available(self) -> bool:
        return importlib.util.find_spec("speech_recognition") is not None

    def _detect_recorder_stream(self) -> list[str] | None:
        prefer_arecord = self.recorder_preference == "arecord"
        auto_arecord = self.recorder_preference == "auto" and self._arecord_has_capture_devices()

        if (prefer_arecord or auto_arecord) and shutil.which("arecord"):
            command = [
                "arecord",
                "-q",
            ]
            if self.alsa_device:
                command.extend(["-D", self.alsa_device])
            command.extend(
                [
                    "-f",
                    "S16_LE",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "-t",
                    "raw",
                ]
            )
            return command
        if self.recorder_preference in ("auto", "pw-record") and shutil.which("pw-record"):
            command = [
                "pw-record",
            ]
            if self.alsa_device:
                command.extend(["--target", self.alsa_device])
            command.extend(
                [
                "--rate",
                "16000",
                "--channels",
                "1",
                "--format",
                "s16",
                "-",
                ]
            )
            return command
        if self.recorder_preference == "auto" and shutil.which("arecord"):
            command = [
                "arecord",
                "-q",
            ]
            if self.alsa_device:
                command.extend(["-D", self.alsa_device])
            command.extend(
                [
                    "-f",
                    "S16_LE",
                    "-r",
                    "16000",
                    "-c",
                    "1",
                    "-t",
                    "raw",
                ]
            )
            return command
        return None

    def _has_capture_device(self) -> bool:
        if not self.recorder_cmd:
            return False
        if self.recorder_cmd[0] == "arecord":
            return self._arecord_has_capture_devices() or bool(self.alsa_device)
        return True

    def _arecord_has_capture_devices(self) -> bool:
        if not shutil.which("arecord"):
            return False
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        output = (result.stdout + "\n" + result.stderr).lower()
        return "card " in output and "device " in output

    def _pip_available(self) -> bool:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _live_loop(self, on_text, on_status, on_error, on_state_change, on_level) -> None:
        GLib.idle_add(on_status, "Live-Diktat aktiv. Sprich jetzt. 1.5 Sekunden Stille senden automatisch ab.")
        try:
            while not self.stop_event.is_set():
                try:
                    audio_bytes = self._capture_utterance(on_level)
                except Exception as exc:
                    if self.stop_event.is_set():
                        break
                    GLib.idle_add(on_error, f"Audioaufnahme fehlgeschlagen: {exc}")
                    break

                if self.stop_event.is_set():
                    break
                if not audio_bytes:
                    continue

                GLib.idle_add(on_status, "Transkribiere Diktat...")
                try:
                    text = self._transcribe_audio(audio_bytes)
                except Exception as exc:
                    if self.stop_event.is_set():
                        break
                    GLib.idle_add(on_error, f"Transkription fehlgeschlagen: {exc}")
                    GLib.idle_add(
                        on_status,
                        "Live-Diktat aktiv. Sprich erneut. 1.5 Sekunden Stille senden automatisch ab.",
                    )
                    continue

                if self.stop_event.is_set():
                    break
                if not text:
                    GLib.idle_add(
                        on_status,
                        "Keine Sprache erkannt. Live-Diktat lauscht weiter.",
                    )
                    continue

                GLib.idle_add(on_text, text)
                GLib.idle_add(
                    on_status,
                    "Live-Diktat aktiv. Sprich erneut. 1.5 Sekunden Stille senden automatisch ab.",
                )
        finally:
            self.live_mode = False
            self.stop_event.clear()
            GLib.idle_add(on_level, 0.0)
            GLib.idle_add(on_state_change, False)

    def _capture_utterance(self, on_level=None) -> bytes | None:
        if not self.recorder_cmd:
            raise RuntimeError("Kein Recorder verfuegbar.")

        self.recorder_process = subprocess.Popen(
            self.recorder_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        process = self.recorder_process
        pre_roll = collections.deque(maxlen=self.PRE_ROLL_CHUNKS)
        frames: list[bytes] = []
        speech_started = False
        silence_chunks = 0
        max_chunks = self.MAX_UTTERANCE_SECONDS * 1000 // self.CHUNK_MS

        try:
            while not self.stop_event.is_set():
                chunk = process.stdout.read(self.CHUNK_BYTES)
                if not chunk:
                    if speech_started:
                        break
                    stderr_output = self._read_stderr(process)
                    raise RuntimeError(stderr_output or f"Recorder beendet mit Exit-Code {process.poll()}.")

                level = self._chunk_level(chunk)
                is_speech = level >= self.start_threshold
                if on_level is not None:
                    GLib.idle_add(on_level, level)

                if speech_started:
                    frames.append(chunk)
                    if is_speech:
                        silence_chunks = 0
                    else:
                        silence_chunks += 1

                    if silence_chunks * self.CHUNK_MS >= self.silence_seconds * 1000:
                        break
                    if len(frames) >= max_chunks:
                        break
                    continue

                pre_roll.append(chunk)
                if is_speech:
                    speech_started = True
                    frames.extend(pre_roll)
                    silence_chunks = 0
        finally:
            self._stop_recorder_process(process)

        if self.stop_event.is_set():
            return None
        if not speech_started or not frames:
            return None
        return b"".join(frames)

    def _transcribe_audio(self, audio_bytes: bytes) -> str:
        if self.provider == "command":
            return self._transcribe_audio_with_command(audio_bytes)

        if self.provider in {"openai_compatible", "elevenlabs"}:
            return self._transcribe_audio_with_http_provider(audio_bytes)

        if self.provider == "standard" and self._speech_recognition_available():
            import speech_recognition as sr

            recognizer = sr.Recognizer()
            audio = sr.AudioData(audio_bytes, self.SAMPLE_RATE, self.SAMPLE_WIDTH)
            return recognizer.recognize_google(audio, language=self.language).strip()

        raise RuntimeError(self.description())

    def _default_api_endpoint(self) -> str:
        if self.provider == "elevenlabs":
            return "https://api.elevenlabs.io/v1/speech-to-text"
        return "https://api.openai.com/v1/audio/transcriptions"

    def _transcribe_audio_with_http_provider(self, audio_bytes: bytes) -> str:
        fd, path = tempfile.mkstemp(prefix="codex-gui-dictation-", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as wav_file:
                wav_file.setnchannels(self.CHANNELS)
                wav_file.setsampwidth(self.SAMPLE_WIDTH)
                wav_file.setframerate(self.SAMPLE_RATE)
                wav_file.writeframes(audio_bytes)

            endpoint = self.api_base_url.strip() or self._default_api_endpoint()
            headers: dict[str, str] = {}
            if self.api_key:
                if self.provider == "elevenlabs":
                    headers["xi-api-key"] = self.api_key
                else:
                    headers["Authorization"] = f"Bearer {self.api_key}"

            fields: list[tuple[str, str]] = []
            if self.api_model:
                fields.append(
                    ("model_id" if self.provider == "elevenlabs" else "model", self.api_model)
                )
            if self.provider != "elevenlabs" and self.language:
                fields.append(("language", self.language))

            body, content_type = self._build_multipart_request(fields, "file", path, "audio/wav")
            headers["Content-Type"] = content_type
            request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = self._extract_transcript_text(payload)
            if not text:
                raise RuntimeError("HTTP-Provider lieferte keinen Transkriptions-Text.")
            return text.strip()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(details or f"HTTP-Fehler {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _build_multipart_request(
        self,
        fields: list[tuple[str, str]],
        file_field: str,
        file_path: str,
        mime_type: str,
    ) -> tuple[bytes, str]:
        boundary = f"codexgui{uuid.uuid4().hex}"
        lines: list[bytes] = []
        for key, value in fields:
            lines.extend(
                [
                    f"--{boundary}".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"),
                    b"",
                    str(value).encode("utf-8"),
                ]
            )

        filename = os.path.basename(file_path)
        with open(file_path, "rb") as handle:
            file_bytes = handle.read()
        lines.extend(
            [
                f"--{boundary}".encode("utf-8"),
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(
                    "utf-8"
                ),
                f"Content-Type: {mime_type}".encode("utf-8"),
                b"",
                file_bytes,
                f"--{boundary}--".encode("utf-8"),
                b"",
            ]
        )
        return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"

    def _extract_transcript_text(self, payload) -> str:
        if isinstance(payload, dict):
            for key in ("text", "transcript"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            for value in payload.values():
                extracted = self._extract_transcript_text(value)
                if extracted:
                    return extracted
        if isinstance(payload, list):
            for item in payload:
                extracted = self._extract_transcript_text(item)
                if extracted:
                    return extracted
        return ""

    def _transcribe_audio_with_command(self, audio_bytes: bytes) -> str:
        fd, path = tempfile.mkstemp(prefix="codex-gui-dictation-", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as wav_file:
                wav_file.setnchannels(self.CHANNELS)
                wav_file.setsampwidth(self.SAMPLE_WIDTH)
                wav_file.setframerate(self.SAMPLE_RATE)
                wav_file.writeframes(audio_bytes)

            quoted = shlex.quote(path)
            command = self.command_template.format(input=quoted)
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "STT-Kommando lieferte Fehler.")
            return result.stdout.strip()
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _stop_recorder_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        finally:
            self.recorder_process = None

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> str:
        try:
            if process.stderr is None:
                return ""
            return process.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    def _chunk_level(self, chunk: bytes) -> float:
        samples = array.array("h")
        samples.frombytes(chunk[: len(chunk) - (len(chunk) % self.SAMPLE_WIDTH)])
        if not samples:
            return 0.0
        total = sum(abs(sample) for sample in samples)
        return total / len(samples)


class CodexTerminalWindow(Gtk.ApplicationWindow):
    VOICE_ENTER_PATTERN = re.compile(r"\benter\b", re.IGNORECASE)
    VOICE_SCREENSHOT_PATTERN = re.compile(
        r"\b(?:shotscreen|shot\s+screen|touchscreen|touch\s+screen)\b",
        re.IGNORECASE,
    )
    VOICE_COMMAND_PATTERN = re.compile(
        r"\b(?:enter|shotscreen|shot\s+screen|touchscreen|touch\s+screen)\b",
        re.IGNORECASE,
    )

    def __init__(self, app: Gtk.Application, args: argparse.Namespace) -> None:
        super().__init__(application=app, title=args.title)
        self.args = args
        self.set_default_size(1200, 760)
        self.supermode_active = os.environ.get("CODEX_GUI_SUPERMODE") == "1"
        self.supervisor_pid = self._read_supervisor_pid()

        self.codex_pid: int | None = None
        self.pending_prompt: str | None = None
        self.pending_voice_transcript: str | None = None
        self.restart_pending = False
        self.session_refresh_source_id: int | None = None
        self.session_temp_files: list[str] = []
        self.state = self._load_gui_state()
        self._apply_saved_app_settings()
        self.dictation = DictationBackend(
            language=self.args.language,
            provider=self.dictation_provider,
            command_template=self.dictation_command_template,
            api_base_url=self.dictation_api_url,
            api_key=self.dictation_api_key,
            api_model=self.dictation_api_model,
            recorder_preference=self.dictation_recorder_preference,
            alsa_device=self.dictation_alsa_device,
            silence_seconds=self.dictation_silence_seconds,
            start_threshold=self.dictation_start_threshold,
        )
        self.args.working_dir = self._resolve_initial_working_dir(self.args.working_dir)
        self.resume_enabled = False
        self.current_session_id: str | None = None
        self._load_workspace_preferences()
        self._remember_project(self.args.working_dir)

        self._build_ui()
        self._refresh_workspace_ui()
        self._refresh_dictation_controls()
        self._bootstrap_dictation()
        self._spawn_codex()

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(10)
        self.add(root)
        self.connect("destroy", self._on_window_destroy)

        self.header = Gtk.HeaderBar()
        self.header.set_show_close_button(True)
        self.header.props.title = self.args.title
        self.header.props.subtitle = self.args.working_dir
        self.set_titlebar(self.header)

        self.project_menu_button = Gtk.MenuButton()
        self.project_menu_button.set_tooltip_text("Projekt wechseln oder verwalten")
        self.project_menu_button.set_popover(self._build_project_popover())
        self.header.pack_start(self.project_menu_button)

        if self.supermode_active:
            self.stop_supermode_button = Gtk.Button(label="Supermode beenden")
            self.stop_supermode_button.set_image(
                Gtk.Image.new_from_icon_name("process-stop-symbolic", Gtk.IconSize.BUTTON)
            )
            self.stop_supermode_button.set_always_show_image(True)
            self.stop_supermode_button.set_tooltip_text(
                "Beendet den Supervisor und stoppt damit den automatischen Neustart."
            )
            self.stop_supermode_button.connect("clicked", self._on_stop_supermode_clicked)
            self.header.pack_end(self.stop_supermode_button)

        self.restart_button = Gtk.Button(label="Neu starten")
        self.restart_button.set_image(
            Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        )
        self.restart_button.set_always_show_image(True)
        self.restart_button.connect("clicked", self._on_restart_clicked)
        self.header.pack_end(self.restart_button)

        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        root.pack_start(top_bar, False, False, 0)

        project_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        top_bar.pack_start(project_bar, True, True, 0)

        project_badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        project_bar.pack_start(project_badge, True, True, 0)

        project_badge.pack_start(
            Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.DIALOG),
            False,
            False,
            0,
        )

        project_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        project_badge.pack_start(project_text, True, True, 0)

        self.project_name_label = Gtk.Label()
        self.project_name_label.set_xalign(0)
        self.project_name_label.get_style_context().add_class("title-4")
        project_text.pack_start(self.project_name_label, False, False, 0)

        self.project_path_label = Gtk.Label()
        self.project_path_label.set_xalign(0)
        self.project_path_label.set_selectable(True)
        self.project_path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        project_text.pack_start(self.project_path_label, False, False, 0)

        project_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        project_bar.pack_end(project_actions, False, False, 0)

        self.open_project_button = self._create_icon_button(
            "document-open-symbolic",
            "Ordner oeffnen",
            self._on_open_project_clicked,
        )
        project_actions.pack_start(self.open_project_button, False, False, 0)

        self.new_project_button = self._create_icon_button(
            "folder-new-symbolic",
            "Neues Projekt anlegen",
            self._on_new_project_clicked,
        )
        project_actions.pack_start(self.new_project_button, False, False, 0)

        self.clone_project_button = self._create_icon_button(
            "folder-download-symbolic",
            "GitHub-Repo klonen",
            self._on_clone_project_clicked,
        )
        project_actions.pack_start(self.clone_project_button, False, False, 0)

        self.reveal_project_button = self._create_icon_button(
            "system-file-manager-symbolic",
            "Projektordner im Dateimanager zeigen",
            self._on_reveal_project_clicked,
        )
        project_actions.pack_start(self.reveal_project_button, False, False, 0)

        self.settings_button = self._create_icon_button(
            "emblem-system-symbolic",
            "Einstellungen",
            self._on_settings_clicked,
        )
        project_actions.pack_start(self.settings_button, False, False, 0)

        codex_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_bar.pack_start(codex_bar, False, False, 0)

        codex_badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        codex_bar.pack_start(codex_badge, False, False, 0)

        codex_badge.pack_start(
            Gtk.Image.new_from_icon_name("utilities-terminal-symbolic", Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )

        self.codex_mode_label = Gtk.Label()
        self.codex_mode_label.set_xalign(0)
        self.codex_mode_label.set_ellipsize(Pango.EllipsizeMode.END)
        codex_badge.pack_start(self.codex_mode_label, False, False, 0)

        self.codex_search_button = Gtk.ToggleButton(label="Search")
        self.codex_search_button.set_tooltip_text(
            "Startet Codex mit --search und startet die laufende Session neu."
        )
        self.codex_search_button_handler_id = self.codex_search_button.connect(
            "toggled", self._on_codex_search_toggled
        )
        codex_bar.pack_start(self.codex_search_button, False, False, 0)

        self.codex_bypass_button = Gtk.ToggleButton(label="Full Access")
        self.codex_bypass_button.set_tooltip_text(
            "Startet Codex mit --dangerously-bypass-approvals-and-sandbox. Vorsicht: volle Freigabe im Codex-Prozess."
        )
        self.codex_bypass_button_handler_id = self.codex_bypass_button.connect(
            "toggled", self._on_codex_bypass_toggled
        )
        codex_bar.pack_start(self.codex_bypass_button, False, False, 0)

        self.resume_button = Gtk.ToggleButton()
        self.resume_button.set_active(self.resume_enabled)
        self.resume_button_handler_id = self.resume_button.connect(
            "toggled", self._on_resume_toggled
        )
        codex_bar.pack_start(self.resume_button, False, False, 0)
        self._refresh_resume_button()

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(controls, False, False, 0)

        self.prompt_entry = Gtk.Entry()
        self.prompt_entry.set_hexpand(True)
        self.prompt_entry.set_placeholder_text("Prompt fuer Codex")
        self.prompt_entry.connect("activate", self._on_send_clicked)
        controls.pack_start(self.prompt_entry, True, True, 0)

        self.send_button = Gtk.Button(label="Senden")
        self.send_button.connect("clicked", self._on_send_clicked)
        controls.pack_start(self.send_button, False, False, 0)

        self.prompt_library_button = Gtk.Button(label="Prompts")
        self.prompt_library_button.set_tooltip_text(
            "Globale Prompt-Verwaltung fuer bereits gesendete Prompts oeffnen."
        )
        self.prompt_library_button.connect("clicked", self._on_prompt_library_clicked)
        controls.pack_start(self.prompt_library_button, False, False, 0)

        voice_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        controls.pack_start(voice_controls, False, False, 0)

        voice_button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        voice_controls.pack_start(voice_button_row, False, False, 0)

        self.dictation_button = Gtk.Button()
        self.dictation_button.connect("clicked", self._on_dictation_clicked)
        self._update_dictation_button_visuals()
        voice_button_row.pack_start(self.dictation_button, False, False, 0)

        self.voice_enter_button = Gtk.ToggleButton(label="⏎")
        self.voice_enter_button.set_tooltip_text(
            "Aktiv: gesprochenes 'Enter' sendet echtes Enter. Aus: jedes Diktat sendet automatisch echtes Enter."
        )
        self.voice_enter_button_handler_id = self.voice_enter_button.connect(
            "toggled", self._on_voice_enter_toggled
        )
        voice_button_row.pack_start(self.voice_enter_button, False, False, 0)
        self._refresh_voice_enter_button()

        self.clear_terminal_input_button = self._create_icon_button(
            "edit-clear-symbolic",
            "Aktuelle Eingabe im Codex-Terminal loeschen",
            self._on_clear_terminal_input_clicked,
        )
        voice_button_row.pack_start(self.clear_terminal_input_button, False, False, 0)

        self.mic_level_bar = Gtk.ProgressBar()
        self.mic_level_bar.set_size_request(120, -1)
        self.mic_level_bar.set_show_text(False)
        self.mic_level_bar.set_fraction(0.0)
        self.mic_level_bar.set_tooltip_text(
            "Kleiner Live-Pegel fuer das aktive Diktat-Mikrofon."
        )
        voice_controls.pack_start(self.mic_level_bar, False, False, 0)

        self.status_label = Gtk.Label(label="Starte Codex...")
        self.status_label.set_xalign(0)
        root.pack_start(self.status_label, False, False, 0)

        terminal_frame = Gtk.Frame()
        terminal_frame.set_hexpand(True)
        terminal_frame.set_vexpand(True)
        root.pack_start(terminal_frame, True, True, 0)

        self.terminal = Vte.Terminal()
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        self.terminal.set_scrollback_lines(20000)
        self.terminal.connect("child-exited", self._on_terminal_child_exited)
        self.terminal.connect("window-title-changed", self._on_terminal_title_changed)
        terminal_frame.add(self.terminal)

        self.show_all()

    def _build_project_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        popover.set_border_width(10)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        popover.add(content)

        title = Gtk.Label(label="Projekte")
        title.set_xalign(0)
        content.pack_start(title, False, False, 0)

        self.project_menu_current_label = Gtk.Label()
        self.project_menu_current_label.set_xalign(0)
        self.project_menu_current_label.set_line_wrap(True)
        content.pack_start(self.project_menu_current_label, False, False, 0)

        quick_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.pack_start(quick_actions, False, False, 0)

        quick_actions.pack_start(
            self._create_icon_button(
                "document-open-symbolic",
                "Ordner oeffnen",
                self._on_open_project_clicked,
            ),
            False,
            False,
            0,
        )
        quick_actions.pack_start(
            self._create_icon_button(
                "folder-new-symbolic",
                "Neues Projekt anlegen",
                self._on_new_project_clicked,
            ),
            False,
            False,
            0,
        )
        quick_actions.pack_start(
            self._create_icon_button(
                "folder-download-symbolic",
                "GitHub-Repo klonen",
                self._on_clone_project_clicked,
            ),
            False,
            False,
            0,
        )

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        content.pack_start(separator, False, False, 0)

        recent_title = Gtk.Label(label="Zuletzt geoeffnet")
        recent_title.set_xalign(0)
        content.pack_start(recent_title, False, False, 0)

        self.recent_projects_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        content.pack_start(self.recent_projects_box, False, False, 0)

        self.recent_projects_empty_label = Gtk.Label(
            label="Noch keine weiteren Projekte gespeichert."
        )
        self.recent_projects_empty_label.set_xalign(0)
        content.pack_start(self.recent_projects_empty_label, False, False, 0)

        return popover

    def _create_icon_button(self, icon_name: str, tooltip: str, handler) -> Gtk.Button:
        button = Gtk.Button()
        button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
        button.set_always_show_image(True)
        button.set_label("")
        button.set_tooltip_text(tooltip)
        button.connect("clicked", handler)
        return button

    def _refresh_dictation_controls(self) -> None:
        self.dictation_button.set_sensitive(self.dictation.available() or self.dictation.is_live())
        self.dictation_button.set_tooltip_text(self.dictation.description())
        self._update_dictation_button_visuals()

    def _refresh_voice_enter_button(self) -> None:
        style_context = self.voice_enter_button.get_style_context()
        style_context.remove_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
        if self.voice_enter_enabled:
            style_context.add_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
            self.voice_enter_button.set_tooltip_text(
                "Aktiv: gesprochenes 'Enter' sendet echtes Enter."
            )
        else:
            self.voice_enter_button.set_tooltip_text(
                "Aus: jedes Diktat sendet automatisch echtes Enter."
            )

    def _update_dictation_button_visuals(self) -> None:
        if self.dictation.is_live():
            icon_name = "media-playback-stop-symbolic"
            tooltip = "Live-Diktat stoppen"
        else:
            icon_name = "audio-input-microphone-symbolic"
            tooltip = self.dictation.description()

        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        self.dictation_button.set_image(image)
        self.dictation_button.set_always_show_image(True)
        self.dictation_button.set_label("")
        self.dictation_button.set_tooltip_text(tooltip)

    def _bootstrap_dictation(self) -> None:
        self.dictation.bootstrap_async(
            self._on_dictation_backend_ready,
            self._on_dictation_backend_status,
            self._on_dictation_error,
        )

    def _on_dictation_backend_ready(self, message: str) -> bool:
        self._refresh_dictation_controls()
        self._set_status(message)
        return False

    def _on_dictation_backend_status(self, message: str) -> bool:
        self._refresh_dictation_controls()
        self._set_status(message)
        return False

    def _build_codex_argv(self) -> list[str]:
        codex_bin = shutil.which(self.args.codex_bin) or self.args.codex_bin
        extra_args = shlex.split(self.args.codex_args)
        argv = [
            codex_bin,
            "--no-alt-screen",
            "-C",
            self.args.working_dir,
        ]
        if self.codex_search_enabled:
            argv.append("--search")
        if self.codex_bypass_enabled:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv.extend(extra_args)
        if self.resume_enabled and self.current_session_id:
            argv.extend(["resume", self.current_session_id])
        return argv

    def _read_supervisor_pid(self) -> int | None:
        raw_pid = os.environ.get("CODEX_GUI_SUPERVISOR_PID", "").strip()
        if not raw_pid:
            return None
        try:
            pid = int(raw_pid)
        except ValueError:
            return None
        return pid if pid > 0 else None

    def _resolve_initial_working_dir(self, requested_dir: str) -> str:
        fallback = str(Path(requested_dir).expanduser().resolve())
        remembered = self.state.get("last_working_dir")
        if (
            isinstance(remembered, str)
            and remembered
            and fallback == str(APP_DIR)
            and self.start_in_last_project
            and Path(remembered).expanduser().exists()
        ):
            return str(Path(remembered).expanduser().resolve())
        return fallback

    def _load_gui_state(self) -> dict:
        if not GUI_STATE_PATH.exists():
            return {}
        try:
            with GUI_STATE_PATH.open("r", encoding="utf-8") as state_file:
                data = json.load(state_file)
        except (json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, dict):
            return data
        return {}

    def _save_gui_state(self) -> None:
        try:
            with GUI_STATE_PATH.open("w", encoding="utf-8") as state_file:
                json.dump(self.state, state_file, indent=2, sort_keys=True)
        except OSError:
            pass

    def _get_app_settings(self) -> dict:
        settings = self.state.get("settings")
        if not isinstance(settings, dict):
            settings = {}
            self.state["settings"] = settings
        return settings

    def _apply_saved_app_settings(self) -> None:
        settings = self._get_app_settings()
        self.start_in_last_project = bool(settings.get("start_in_last_project", True))
        self.codex_search_enabled = bool(settings.get("codex_search_enabled", False))
        self.codex_bypass_enabled = bool(settings.get("codex_bypass_enabled", False))
        self.dictation_provider = settings.get("dictation_provider", "standard")
        self.dictation_command_template = settings.get("dictation_command_template", "")
        self.dictation_api_url = settings.get("dictation_api_url", "")
        self.dictation_api_key = settings.get("dictation_api_key", "")
        self.dictation_api_model = settings.get("dictation_api_model", "gpt-4o-mini-transcribe")
        self.dictation_recorder_preference = settings.get("dictation_recorder_preference", "auto")
        self.dictation_alsa_device = settings.get("dictation_alsa_device", "")
        self.dictation_silence_seconds = float(
            settings.get("dictation_silence_seconds", DictationBackend.DEFAULT_SILENCE_SECONDS)
        )
        self.dictation_start_threshold = int(
            settings.get("dictation_start_threshold", DictationBackend.DEFAULT_START_THRESHOLD)
        )

        if self.args.codex_bin == DEFAULT_CODEX_BIN:
            self.args.codex_bin = settings.get("codex_bin", self.args.codex_bin)
        if self.args.codex_args == DEFAULT_CODEX_ARGS:
            self.args.codex_args = settings.get("codex_args", self.args.codex_args)
        if self.args.language == DEFAULT_STT_LANGUAGE:
            self.args.language = settings.get("language", self.args.language)
        if self.args.title == DEFAULT_APP_TITLE:
            self.args.title = settings.get("title", self.args.title)

    def _save_app_settings(self) -> None:
        settings = self._get_app_settings()
        settings["start_in_last_project"] = self.start_in_last_project
        settings["codex_bin"] = self.args.codex_bin
        settings["codex_args"] = self.args.codex_args
        settings["codex_search_enabled"] = self.codex_search_enabled
        settings["codex_bypass_enabled"] = self.codex_bypass_enabled
        settings["language"] = self.args.language
        settings["title"] = self.args.title
        settings["dictation_provider"] = self.dictation_provider
        settings["dictation_command_template"] = self.dictation_command_template
        settings["dictation_api_url"] = self.dictation_api_url
        settings["dictation_api_key"] = self.dictation_api_key
        settings["dictation_api_model"] = self.dictation_api_model
        settings["dictation_recorder_preference"] = self.dictation_recorder_preference
        settings["dictation_alsa_device"] = self.dictation_alsa_device
        settings["dictation_silence_seconds"] = self.dictation_silence_seconds
        settings["dictation_start_threshold"] = self.dictation_start_threshold
        self._save_gui_state()

    def _load_workspace_preferences(self) -> None:
        workspace_state = self._get_workspace_state()
        self.resume_enabled = bool(workspace_state.get("resume_enabled", False))
        self.current_session_id = workspace_state.get("session_id")
        self.voice_enter_enabled = bool(workspace_state.get("voice_enter_enabled", True))
        if not self.resume_enabled and self.current_session_id:
            self.current_session_id = None
            self._save_workspace_state()

    def _get_projects_state(self) -> list[dict]:
        projects = self.state.get("projects")
        if not isinstance(projects, list):
            projects = []
            self.state["projects"] = projects
        normalized: list[dict] = []
        seen: set[str] = set()
        for item in projects:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path:
                continue
            resolved = str(Path(path).expanduser().resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            normalized.append(
                {
                    "path": resolved,
                    "name": item.get("name") or Path(resolved).name or resolved,
                    "source": item.get("source") or "local",
                    "github_repo": item.get("github_repo"),
                    "last_opened": item.get("last_opened", 0),
                }
            )
        self.state["projects"] = normalized
        return normalized

    def _remember_project(
        self,
        path: str,
        *,
        source: str = "local",
        github_repo: str | None = None,
    ) -> None:
        normalized = str(Path(path).expanduser().resolve())
        projects = self._get_projects_state()
        projects[:] = [item for item in projects if item.get("path") != normalized]
        projects.insert(
            0,
            {
                "path": normalized,
                "name": Path(normalized).name or normalized,
                "source": source,
                "github_repo": github_repo,
                "last_opened": int(time.time()),
            },
        )
        del projects[20:]
        self.state["last_working_dir"] = normalized
        self._save_gui_state()

    def _get_current_project_record(self) -> dict | None:
        current_path = str(Path(self.args.working_dir).expanduser().resolve())
        for item in self._get_projects_state():
            if item.get("path") == current_path:
                return item
        return None

    def _get_prompt_library_state(self) -> list[dict]:
        prompts = self.state.get("prompt_library")
        if not isinstance(prompts, list):
            prompts = []
            self.state["prompt_library"] = prompts

        normalized: list[dict] = []
        seen: set[str] = set()
        for item in prompts:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            clean_text = text.strip()
            if not clean_text:
                continue
            key = clean_text.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "text": clean_text,
                    "source": item.get("source") or "text",
                    "project_path": item.get("project_path") or "",
                    "folder": str(item.get("folder") or "").strip(),
                    "favorite": bool(item.get("favorite", False)),
                    "created_at": int(item.get("created_at", 0) or 0),
                    "last_used": int(item.get("last_used", 0) or 0),
                    "use_count": int(item.get("use_count", 1) or 1),
                }
            )
        self.state["prompt_library"] = normalized
        return normalized

    def _record_prompt_library_entry(self, text: str, *, source: str) -> None:
        clean_text = text.strip()
        if not clean_text:
            return

        prompts = self._get_prompt_library_state()
        now = int(time.time())
        current_project = str(Path(self.args.working_dir).expanduser().resolve())
        key = clean_text.casefold()

        for item in prompts:
            if item.get("text", "").casefold() != key:
                continue
            item["source"] = source or item.get("source") or "text"
            item["project_path"] = current_project
            item["last_used"] = now
            item["use_count"] = int(item.get("use_count", 1) or 1) + 1
            self._save_gui_state()
            return

        prompts.insert(
            0,
            {
                "text": clean_text,
                "source": source or "text",
                "project_path": current_project,
                "folder": "",
                "favorite": False,
                "created_at": now,
                "last_used": now,
                "use_count": 1,
            },
        )
        del prompts[200:]
        self._save_gui_state()

    def _delete_prompt_library_entry(self, text: str) -> None:
        prompts = self._get_prompt_library_state()
        key = text.strip().casefold()
        prompts[:] = [item for item in prompts if item.get("text", "").casefold() != key]
        self._save_gui_state()

    def _format_prompt_library_excerpt(self, text: str, limit: int = 110) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1].rstrip() + "…"

    def _update_prompt_library_entry(
        self,
        text: str,
        *,
        favorite: bool | None = None,
        folder: str | None = None,
    ) -> None:
        prompts = self._get_prompt_library_state()
        key = text.strip().casefold()
        for item in prompts:
            if item.get("text", "").casefold() != key:
                continue
            if favorite is not None:
                item["favorite"] = bool(favorite)
            if folder is not None:
                item["folder"] = folder.strip()
            self._save_gui_state()
            return

    def _list_prompt_library_folders(self) -> list[str]:
        folders = {
            str(item.get("folder") or "").strip()
            for item in self._get_prompt_library_state()
            if str(item.get("folder") or "").strip()
        }
        return sorted(folders, key=str.casefold)

    def _prompt_prompt_folder(self, current_folder: str) -> str | None:
        dialog = Gtk.Dialog(title="Prompt-Ordner", transient_for=self, flags=0)
        dialog.add_button("_Abbrechen", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Speichern", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        content.add(box)

        hint = Gtk.Label(
            label="Ordner fuer diesen Prompt. Du kannst auch Unterordner wie Workflows/UI oder Voice/Favoriten verwenden."
        )
        hint.set_xalign(0)
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)

        entry = Gtk.Entry(text=current_folder or "")
        entry.set_placeholder_text("z. B. Favoriten, Workflows/UI, Templates")
        entry.set_activates_default(True)
        box.pack_start(entry, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        folder = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        return folder

    def _refresh_workspace_ui(self) -> None:
        current_path = self.args.working_dir
        current_name = Path(current_path).name or current_path
        project_record = self._get_current_project_record() or {}
        project_source = project_record.get("source") or "local"
        github_repo = project_record.get("github_repo")
        self.header.props.subtitle = current_path
        if project_source == "github" and github_repo:
            self.project_name_label.set_text(f"Projekt: {current_name}  |  GitHub: {github_repo}")
        elif project_source == "github":
            self.project_name_label.set_text(f"Projekt: {current_name}  |  GitHub")
        else:
            self.project_name_label.set_text(f"Projekt: {current_name}")
        self.project_path_label.set_text(current_path)
        self.project_menu_current_label.set_text(
            f"Aktuelles Projekt: {current_name}\n{current_path}"
        )
        self._refresh_project_menu_button()
        self._refresh_recent_projects()
        self._set_codex_search_button_active(self.codex_search_enabled)
        self._set_codex_bypass_button_active(self.codex_bypass_enabled)
        self._refresh_codex_mode_ui()
        self._set_resume_button_active(self.resume_enabled)
        self._refresh_resume_button()
        self._set_voice_enter_button_active(self.voice_enter_enabled)
        self._refresh_voice_enter_button()

    def _refresh_project_menu_button(self) -> None:
        current_child = self.project_menu_button.get_child()
        if current_child is not None:
            self.project_menu_button.remove(current_child)
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.pack_start(
            Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )
        project_name = Gtk.Label(label=Path(self.args.working_dir).name or self.args.working_dir)
        button_box.pack_start(project_name, False, False, 0)
        button_box.pack_start(
            Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON),
            False,
            False,
            0,
        )
        self.project_menu_button.add(button_box)
        self.project_menu_button.show_all()

    def _set_resume_button_active(self, active: bool) -> None:
        self.resume_button.handler_block(self.resume_button_handler_id)
        self.resume_button.set_active(active)
        self.resume_button.handler_unblock(self.resume_button_handler_id)

    def _set_codex_search_button_active(self, active: bool) -> None:
        self.codex_search_button.handler_block(self.codex_search_button_handler_id)
        self.codex_search_button.set_active(active)
        self.codex_search_button.handler_unblock(self.codex_search_button_handler_id)

    def _set_codex_bypass_button_active(self, active: bool) -> None:
        self.codex_bypass_button.handler_block(self.codex_bypass_button_handler_id)
        self.codex_bypass_button.set_active(active)
        self.codex_bypass_button.handler_unblock(self.codex_bypass_button_handler_id)

    def _set_voice_enter_button_active(self, active: bool) -> None:
        self.voice_enter_button.handler_block(self.voice_enter_button_handler_id)
        self.voice_enter_button.set_active(active)
        self.voice_enter_button.handler_unblock(self.voice_enter_button_handler_id)

    def _refresh_codex_mode_ui(self) -> None:
        mode_parts = []
        if self.codex_search_enabled:
            mode_parts.append("search")
        if self.codex_bypass_enabled:
            mode_parts.append("full-access")
        if self.resume_enabled:
            mode_parts.append("resume")
        mode_text = ", ".join(mode_parts) if mode_parts else "standard"
        self.codex_mode_label.set_text(f"Codex: {mode_text}")

        search_style = self.codex_search_button.get_style_context()
        bypass_style = self.codex_bypass_button.get_style_context()
        search_style.remove_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
        bypass_style.remove_class(Gtk.STYLE_CLASS_DESTRUCTIVE_ACTION)
        if self.codex_search_enabled:
            search_style.add_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
        if self.codex_bypass_enabled:
            bypass_style.add_class(Gtk.STYLE_CLASS_DESTRUCTIVE_ACTION)

    def _refresh_recent_projects(self) -> None:
        for child in self.recent_projects_box.get_children():
            self.recent_projects_box.remove(child)

        recent_projects = [
            item
            for item in self._get_projects_state()
            if item.get("path") != self.args.working_dir and Path(item.get("path", "")).exists()
        ]
        if not recent_projects:
            self.recent_projects_empty_label.show()
            return

        self.recent_projects_empty_label.hide()
        for item in recent_projects[:8]:
            self.recent_projects_box.pack_start(
                self._build_recent_project_button(item),
                False,
                False,
                0,
            )
        self.recent_projects_box.show_all()

    def _build_recent_project_button(self, item: dict) -> Gtk.Button:
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.pack_start(
            Gtk.Image.new_from_icon_name("folder-symbolic", Gtk.IconSize.MENU),
            False,
            False,
            0,
        )
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name_label = Gtk.Label(label=item.get("name") or item["path"])
        name_label.set_xalign(0)
        path_label = Gtk.Label(label=item["path"])
        path_label.set_xalign(0)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        text_box.pack_start(name_label, False, False, 0)
        text_box.pack_start(path_label, False, False, 0)
        row.pack_start(text_box, True, True, 0)
        button.add(row)
        button.connect("clicked", self._on_recent_project_clicked, item["path"])
        return button

    def _activate_project(
        self,
        path: str,
        *,
        source: str = "local",
        github_repo: str | None = None,
        restart_codex: bool = True,
    ) -> None:
        normalized = str(Path(path).expanduser().resolve())
        project_path = Path(normalized)
        if not project_path.exists() or not project_path.is_dir():
            self._set_status(f"Projektordner nicht gefunden: {normalized}")
            return

        if normalized == self.args.working_dir:
            self._remember_project(normalized, source=source, github_repo=github_repo)
            self._refresh_workspace_ui()
            self._set_status("Projekt bereits aktiv.")
            return

        self.args.working_dir = normalized
        self._remember_project(normalized, source=source, github_repo=github_repo)
        self._load_workspace_preferences()
        self._refresh_workspace_ui()

        if self.codex_pid is None or not restart_codex:
            self._set_status(f"Projekt gewechselt zu {normalized}.")
            self._spawn_codex()
            return

        self._set_status(f"Wechsle zu Projekt {normalized} und starte Codex neu...")
        self._restart_codex(force_fresh_session=not self.resume_enabled)

    def _choose_project_folder(self, title: str, current_folder: str | None = None) -> str | None:
        dialog = Gtk.FileChooserNative.new(
            title,
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Oeffnen",
            "_Abbrechen",
        )
        if current_folder:
            dialog.set_current_folder(current_folder)
        response = dialog.run()
        filename = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and filename:
            return filename
        return None

    def _prompt_new_project(self) -> tuple[str, bool] | None:
        dialog = Gtk.Dialog(
            title="Neues Projekt anlegen",
            transient_for=self,
            flags=0,
        )
        dialog.add_button("_Abbrechen", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Anlegen", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        content.add(grid)

        grid.attach(Gtk.Label(label="Projektname"), 0, 0, 1, 1)
        name_entry = Gtk.Entry()
        name_entry.set_activates_default(True)
        grid.attach(name_entry, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Zielordner"), 0, 1, 1, 1)
        chooser = Gtk.FileChooserButton(
            title="Projektordner auswaehlen",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.set_filename(str(Path.home()))
        grid.attach(chooser, 1, 1, 1, 1)

        git_check = Gtk.CheckButton(label="Git-Repository initialisieren")
        git_check.set_sensitive(shutil.which("git") is not None)
        grid.attach(git_check, 1, 2, 1, 1)

        dialog.show_all()
        response = dialog.run()
        project_name = name_entry.get_text().strip()
        parent_dir = chooser.get_filename()
        init_git = git_check.get_active()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return None
        if not project_name:
            self._set_status("Projektname fehlt.")
            return None
        if not parent_dir:
            self._set_status("Zielordner fehlt.")
            return None
        project_path = str(Path(parent_dir).expanduser() / project_name)
        return project_path, init_git

    def _prompt_github_clone(self) -> tuple[str, str | None] | None:
        dialog = Gtk.Dialog(
            title="GitHub-Projekt klonen",
            transient_for=self,
            flags=0,
        )
        dialog.add_button("_Abbrechen", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Klonen", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        content.add(grid)

        grid.attach(Gtk.Label(label="Repository"), 0, 0, 1, 1)
        repo_entry = Gtk.Entry()
        repo_entry.set_placeholder_text("owner/repo oder https://github.com/owner/repo")
        repo_entry.set_activates_default(True)
        grid.attach(repo_entry, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Zielordner"), 0, 1, 1, 1)
        chooser = Gtk.FileChooserButton(
            title="Zielordner auswaehlen",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.set_filename(str(Path.home()))
        grid.attach(chooser, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Ordnername"), 0, 2, 1, 1)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("optional, sonst Repo-Name")
        grid.attach(name_entry, 1, 2, 1, 1)

        dialog.show_all()
        response = dialog.run()
        repo_spec = repo_entry.get_text().strip()
        target_parent = chooser.get_filename()
        folder_name = name_entry.get_text().strip() or None
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return None
        if not repo_spec or not target_parent:
            self._set_status("Repository und Zielordner werden benoetigt.")
            return None
        return repo_spec, str(Path(target_parent).expanduser() / (folder_name or self._derive_repo_name(repo_spec)))

    def _prompt_prompt_library(self) -> tuple[str, str] | None:
        dialog = Gtk.Dialog(title="Prompt-Verwaltung", transient_for=self, flags=0)
        dialog.add_button("_Schliessen", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(860, 520)

        content = dialog.get_content_area()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        content.add(outer)

        help_label = Gtk.Label(
            label=(
                "Globale Prompt-Bibliothek. Hier erscheinen bereits an Codex gesendete Prompts "
                "aus allen Projekten, egal ob getippt oder diktiert. "
                "Du kannst Favoriten markieren und Ordner oder Unterordner vergeben."
            )
        )
        help_label.set_xalign(0)
        help_label.set_line_wrap(True)
        outer.pack_start(help_label, False, False, 0)

        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(filter_row, False, False, 0)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text("Prompts durchsuchen")
        search_entry.set_hexpand(True)
        filter_row.pack_start(search_entry, True, True, 0)

        favorites_only_check = Gtk.CheckButton(label="Nur Favoriten")
        filter_row.pack_start(favorites_only_check, False, False, 0)

        folder_combo = Gtk.ComboBoxText()
        filter_row.pack_start(folder_combo, False, False, 0)

        action_hint = Gtk.Label(
            label="Doppelklick laedt einen Prompt ins Eingabefeld. Unten gibt es auch Direkt-Senden und Loeschen."
        )
        action_hint.set_xalign(0)
        action_hint.set_line_wrap(True)
        outer.pack_start(action_hint, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        outer.pack_start(scroller, True, True, 0)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroller.add(list_box)

        result: dict[str, str] = {}
        filter_state = {"updating_folder_combo": False}

        def rebuild_rows() -> None:
            for child in list_box.get_children():
                list_box.remove(child)

            available_folders = self._list_prompt_library_folders()
            current_folder_id = folder_combo.get_active_id()
            filter_state["updating_folder_combo"] = True
            try:
                folder_combo.remove_all()
                folder_combo.append("__all__", "Alle Ordner")
                folder_combo.append("__none__", "Ohne Ordner")
                for folder in available_folders:
                    folder_combo.append(folder, folder)
                if current_folder_id and (
                    current_folder_id in {"__all__", "__none__"}
                    or current_folder_id in available_folders
                ):
                    folder_combo.set_active_id(current_folder_id)
                else:
                    folder_combo.set_active_id("__all__")
            finally:
                filter_state["updating_folder_combo"] = False

            query = search_entry.get_text().strip().casefold()
            selected_folder = folder_combo.get_active_id() or "__all__"
            only_favorites = favorites_only_check.get_active()
            prompts = sorted(
                self._get_prompt_library_state(),
                key=lambda item: (
                    1 if item.get("favorite") else 0,
                    int(item.get("last_used", 0) or 0),
                ),
                reverse=True,
            )

            matched_any = False
            for item in prompts:
                haystack = " ".join(
                    [
                        item.get("text", ""),
                        item.get("project_path", ""),
                        item.get("source", ""),
                    ]
                ).casefold()
                if query and query not in haystack:
                    continue
                item_folder = str(item.get("folder") or "").strip()
                if only_favorites and not bool(item.get("favorite")):
                    continue
                if selected_folder == "__none__" and item_folder:
                    continue
                if selected_folder not in {"__all__", "__none__"} and item_folder != selected_folder:
                    continue
                matched_any = True
                list_box.add(self._build_prompt_library_row(dialog, result, item, rebuild_rows))

            if not matched_any:
                empty_row = Gtk.ListBoxRow()
                empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=10)
                empty_label = Gtk.Label(label="Keine Prompts gefunden.")
                empty_label.set_xalign(0)
                empty_box.pack_start(empty_label, False, False, 0)
                empty_row.add(empty_box)
                list_box.add(empty_row)

            list_box.show_all()

        def on_row_activated(_list_box, row: Gtk.ListBoxRow) -> None:
            prompt_text = getattr(row, "prompt_text", "").strip()
            if not prompt_text:
                return
            result["action"] = "load"
            result["text"] = prompt_text
            dialog.response(Gtk.ResponseType.OK)

        def on_folder_combo_changed(_combo) -> None:
            if filter_state["updating_folder_combo"]:
                return
            rebuild_rows()

        search_entry.connect("search-changed", lambda _entry: rebuild_rows())
        favorites_only_check.connect("toggled", lambda _button: rebuild_rows())
        folder_combo.connect("changed", on_folder_combo_changed)
        list_box.connect("row-activated", on_row_activated)
        rebuild_rows()

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return None
        action = result.get("action")
        text = result.get("text", "").strip()
        if not action or not text:
            return None
        return action, text

    def _build_prompt_library_row(
        self,
        dialog: Gtk.Dialog,
        result: dict[str, str],
        item: dict,
        rebuild_rows,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.prompt_text = item.get("text", "")

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        row.add(container)

        title = Gtk.Label(label=self._format_prompt_library_excerpt(item.get("text", "")))
        title.set_xalign(0)
        title.set_line_wrap(True)
        title.get_style_context().add_class("title-4")
        container.pack_start(title, False, False, 0)

        meta_parts = [
            f"Quelle: {'Diktat' if item.get('source') == 'voice' else 'Text'}",
            f"Verwendet: {int(item.get('use_count', 1) or 1)}x",
        ]
        if item.get("favorite"):
            meta_parts.append("Favorit")
        folder = str(item.get("folder") or "").strip()
        if folder:
            meta_parts.append(f"Ordner: {folder}")
        project_path = item.get("project_path", "")
        if project_path:
            meta_parts.append(f"Letztes Projekt: {project_path}")
        last_used = int(item.get("last_used", 0) or 0)
        if last_used > 0:
            meta_parts.append(
                "Zuletzt: "
                + time.strftime("%Y-%m-%d %H:%M", time.localtime(last_used))
            )

        meta = Gtk.Label(label=" | ".join(meta_parts))
        meta.set_xalign(0)
        meta.set_line_wrap(True)
        container.pack_start(meta, False, False, 0)

        body = Gtk.Label(label=item.get("text", ""))
        body.set_xalign(0)
        body.set_line_wrap(True)
        body.set_max_width_chars(120)
        body.set_selectable(True)
        container.pack_start(body, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        container.pack_start(actions, False, False, 0)

        favorite_button = Gtk.Button(
            label="★ Favorit" if item.get("favorite") else "☆ Favorit"
        )
        favorite_button.connect(
            "clicked",
            lambda _button: (
                self._update_prompt_library_entry(
                    item.get("text", ""),
                    favorite=not bool(item.get("favorite")),
                ),
                rebuild_rows(),
            ),
        )
        actions.pack_start(favorite_button, False, False, 0)

        folder_button = Gtk.Button(label="Ordner…")
        folder_button.connect(
            "clicked",
            lambda _button: (
                folder_value := self._prompt_prompt_folder(str(item.get("folder") or "")),
                self._update_prompt_library_entry(item.get("text", ""), folder=folder_value)
                if folder_value is not None
                else None,
                rebuild_rows(),
            ),
        )
        actions.pack_start(folder_button, False, False, 0)

        load_button = Gtk.Button(label="Ins Feld laden")
        load_button.connect(
            "clicked",
            lambda _button: (
                result.__setitem__("action", "load"),
                result.__setitem__("text", item.get("text", "")),
                dialog.response(Gtk.ResponseType.OK),
            ),
        )
        actions.pack_start(load_button, False, False, 0)

        send_button = Gtk.Button(label="Direkt senden")
        send_button.connect(
            "clicked",
            lambda _button: (
                result.__setitem__("action", "send"),
                result.__setitem__("text", item.get("text", "")),
                dialog.response(Gtk.ResponseType.OK),
            ),
        )
        actions.pack_start(send_button, False, False, 0)

        delete_button = Gtk.Button(label="Loeschen")
        delete_button.connect(
            "clicked",
            lambda _button: (
                self._delete_prompt_library_entry(item.get("text", "")),
                rebuild_rows(),
            ),
        )
        actions.pack_start(delete_button, False, False, 0)

        return row

    def _prompt_settings(self) -> dict | None:
        dialog = Gtk.Dialog(title="Einstellungen", transient_for=self, flags=0)
        dialog.add_button("_Abbrechen", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Speichern", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        notebook = Gtk.Notebook()
        notebook.set_margin_top(10)
        notebook.set_margin_bottom(10)
        notebook.set_margin_start(10)
        notebook.set_margin_end(10)
        content.add(notebook)

        general_grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        codex_grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)
        voice_grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin=10)

        notebook.append_page(general_grid, Gtk.Label(label="General"))
        notebook.append_page(codex_grid, Gtk.Label(label="Codex"))
        notebook.append_page(voice_grid, Gtk.Label(label="Voice"))

        general_grid.attach(Gtk.Label(label="Fenstertitel"), 0, 0, 1, 1)
        title_entry = Gtk.Entry(text=self.args.title)
        general_grid.attach(title_entry, 1, 0, 1, 1)

        remember_check = Gtk.CheckButton(label="Beim Standardstart letztes Projekt wieder oeffnen")
        remember_check.set_active(self.start_in_last_project)
        general_grid.attach(remember_check, 1, 1, 1, 1)

        codex_grid.attach(Gtk.Label(label="Codex Binary"), 0, 0, 1, 1)
        codex_bin_entry = Gtk.Entry(text=self.args.codex_bin)
        codex_grid.attach(codex_bin_entry, 1, 0, 1, 1)

        codex_grid.attach(Gtk.Label(label="Extra Args"), 0, 1, 1, 1)
        codex_args_entry = Gtk.Entry(text=self.args.codex_args)
        codex_args_entry.set_placeholder_text("--model gpt-5.4")
        codex_grid.attach(codex_args_entry, 1, 1, 1, 1)

        search_check = Gtk.CheckButton(label="Start with --search")
        search_check.set_active(self.codex_search_enabled)
        codex_grid.attach(search_check, 1, 2, 1, 1)

        bypass_check = Gtk.CheckButton(
            label="Start with --dangerously-bypass-approvals-and-sandbox"
        )
        bypass_check.set_active(self.codex_bypass_enabled)
        codex_grid.attach(bypass_check, 1, 3, 1, 1)

        codex_warning = Gtk.Label(
            label=(
                "Warning: the bypass mode removes normal approval and sandbox boundaries "
                "inside the Codex CLI process."
            )
        )
        codex_warning.set_xalign(0)
        codex_warning.set_line_wrap(True)
        codex_grid.attach(codex_warning, 0, 4, 2, 1)

        transparency = Gtk.Label(
            label=(
                "Transcription modes: Built-in Standard, External Command, "
                "OpenAI/OpenRouter-compatible endpoint, ElevenLabs. "
                "Anything else should be wired through External Command or a compatible API endpoint."
            )
        )
        transparency.set_xalign(0)
        transparency.set_line_wrap(True)
        voice_grid.attach(transparency, 0, 0, 2, 1)

        voice_grid.attach(Gtk.Label(label="Provider"), 0, 1, 1, 1)
        provider_combo = Gtk.ComboBoxText()
        provider_combo.append("standard", "Standard")
        provider_combo.append("command", "External Command")
        provider_combo.append("openai_compatible", "OpenAI/OpenRouter-compatible")
        provider_combo.append("elevenlabs", "ElevenLabs")
        provider_combo.set_active_id(self.dictation_provider)
        voice_grid.attach(provider_combo, 1, 1, 1, 1)

        voice_grid.attach(Gtk.Label(label="Sprache"), 0, 2, 1, 1)
        language_entry = Gtk.Entry(text=self.args.language)
        voice_grid.attach(language_entry, 1, 2, 1, 1)

        voice_grid.attach(Gtk.Label(label="Recorder"), 0, 3, 1, 1)
        recorder_combo = Gtk.ComboBoxText()
        recorder_combo.append("auto", "Auto")
        recorder_combo.append("arecord", "ALSA arecord")
        recorder_combo.append("pw-record", "PipeWire pw-record")
        recorder_combo.set_active_id(self.dictation_recorder_preference)
        voice_grid.attach(recorder_combo, 1, 3, 1, 1)

        voice_grid.attach(Gtk.Label(label="Device"), 0, 4, 1, 1)
        device_combo = Gtk.ComboBoxText.new_with_entry()
        voice_grid.attach(device_combo, 1, 4, 1, 1)

        voice_grid.attach(Gtk.Label(label="Input Level"), 0, 5, 1, 1)
        level_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        voice_grid.attach(level_box, 1, 5, 1, 1)

        level_bar = Gtk.ProgressBar()
        level_bar.set_show_text(True)
        level_bar.set_text("Kein Signal")
        level_box.pack_start(level_bar, False, False, 0)

        level_status = Gtk.Label(label="Pegel des ausgewaehlten Mikrofons")
        level_status.set_xalign(0)
        level_box.pack_start(level_status, False, False, 0)

        monitor_state: dict[str, object] = {"stop_event": None, "process": None}

        def get_selected_device() -> str:
            if device_combo.get_active() >= 0:
                return device_combo.get_active_id() or ""
            return device_combo.get_child().get_text().strip()

        def update_level_ui(fraction: float, text: str, status: str) -> bool:
            level_bar.set_fraction(max(0.0, min(1.0, fraction)))
            level_bar.set_text(text)
            level_status.set_text(status)
            return False

        def stop_level_monitor() -> None:
            stop_event = monitor_state.get("stop_event")
            if isinstance(stop_event, threading.Event):
                stop_event.set()
            process = monitor_state.get("process")
            if isinstance(process, subprocess.Popen) and process.poll() is None:
                process.terminate()
            monitor_state["stop_event"] = None
            monitor_state["process"] = None

        def refill_device_combo(*_args) -> None:
            active_recorder = recorder_combo.get_active_id() or "auto"
            device_combo.remove_all()
            for value, label in self._list_capture_devices(active_recorder):
                device_combo.append(value, label)
            if self.dictation_alsa_device:
                device_combo.get_child().set_text(self.dictation_alsa_device)
            else:
                device_combo.set_active(0)
            start_level_monitor()

        def start_level_monitor(*_args) -> None:
            stop_level_monitor()
            recorder = recorder_combo.get_active_id() or "auto"
            device = get_selected_device()
            probe_backend = DictationBackend(
                language=language_entry.get_text().strip() or DEFAULT_STT_LANGUAGE,
                provider="standard",
                recorder_preference=recorder,
                alsa_device=device,
                silence_seconds=self.dictation_silence_seconds,
                start_threshold=self.dictation_start_threshold,
            )
            if not probe_backend.recorder_cmd:
                update_level_ui(0.0, "Kein Recorder", "Kein passender Recorder gefunden.")
                return

            stop_event = threading.Event()
            monitor_state["stop_event"] = stop_event

            def worker() -> None:
                process: subprocess.Popen[bytes] | None = None
                try:
                    process = subprocess.Popen(
                        probe_backend.recorder_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    monitor_state["process"] = process
                    GLib.idle_add(update_level_ui, 0.0, "Lauscht...", "Sprich ins Mikrofon.")
                    while not stop_event.is_set():
                        chunk = process.stdout.read(probe_backend.CHUNK_BYTES)
                        if not chunk:
                            break
                        level = probe_backend._chunk_level(chunk)
                        scale = max(float(probe_backend.start_threshold) * 3.0, 1000.0)
                        fraction = min(level / scale, 1.0)
                        GLib.idle_add(
                            update_level_ui,
                            fraction,
                            f"{int(level)}",
                            "Live-Pegel des ausgewaehlten Mikrofons",
                        )
                except Exception as exc:
                    GLib.idle_add(update_level_ui, 0.0, "Fehler", str(exc))
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                    monitor_state["process"] = None

            threading.Thread(target=worker, daemon=True).start()

        refill_device_combo()
        recorder_combo.connect("changed", refill_device_combo)
        device_combo.connect("changed", start_level_monitor)
        device_combo.get_child().connect("changed", start_level_monitor)

        voice_grid.attach(Gtk.Label(label="Silence Seconds"), 0, 6, 1, 1)
        silence_spin = Gtk.SpinButton.new_with_range(0.3, 10.0, 0.1)
        silence_spin.set_value(self.dictation_silence_seconds)
        voice_grid.attach(silence_spin, 1, 6, 1, 1)

        voice_grid.attach(Gtk.Label(label="Speech Threshold"), 0, 7, 1, 1)
        threshold_spin = Gtk.SpinButton.new_with_range(50, 5000, 10)
        threshold_spin.set_value(self.dictation_start_threshold)
        voice_grid.attach(threshold_spin, 1, 7, 1, 1)

        voice_grid.attach(Gtk.Label(label="External Command"), 0, 8, 1, 1)
        command_entry = Gtk.Entry(text=self.dictation_command_template)
        command_entry.set_placeholder_text("my-transcriber --input {input}")
        voice_grid.attach(command_entry, 1, 8, 1, 1)

        voice_grid.attach(Gtk.Label(label="API Endpoint"), 0, 9, 1, 1)
        api_url_entry = Gtk.Entry(text=self.dictation_api_url)
        api_url_entry.set_placeholder_text("leer = Provider-Default")
        voice_grid.attach(api_url_entry, 1, 9, 1, 1)

        voice_grid.attach(Gtk.Label(label="API Key"), 0, 10, 1, 1)
        api_key_entry = Gtk.Entry(text=self.dictation_api_key)
        api_key_entry.set_visibility(False)
        voice_grid.attach(api_key_entry, 1, 10, 1, 1)

        voice_grid.attach(Gtk.Label(label="API Model"), 0, 11, 1, 1)
        api_model_entry = Gtk.Entry(text=self.dictation_api_model)
        voice_grid.attach(api_model_entry, 1, 11, 1, 1)

        dialog.show_all()
        response = dialog.run()
        stop_level_monitor()
        selected_device = ""
        if device_combo.get_active() >= 0:
            selected_device = device_combo.get_active_id() or ""
        else:
            selected_device = device_combo.get_child().get_text().strip()
        values = {
            "title": title_entry.get_text().strip() or DEFAULT_APP_TITLE,
            "start_in_last_project": remember_check.get_active(),
            "codex_bin": codex_bin_entry.get_text().strip() or DEFAULT_CODEX_BIN,
            "codex_args": codex_args_entry.get_text().strip(),
            "codex_search_enabled": search_check.get_active(),
            "codex_bypass_enabled": bypass_check.get_active(),
            "provider": provider_combo.get_active_id() or "standard",
            "language": language_entry.get_text().strip() or DEFAULT_STT_LANGUAGE,
            "recorder_preference": recorder_combo.get_active_id() or "auto",
            "alsa_device": selected_device,
            "silence_seconds": silence_spin.get_value(),
            "start_threshold": threshold_spin.get_value_as_int(),
            "command_template": command_entry.get_text().strip(),
            "api_url": api_url_entry.get_text().strip(),
            "api_key": api_key_entry.get_text().strip(),
            "api_model": api_model_entry.get_text().strip() or "gpt-4o-mini-transcribe",
        }
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        return values

    def _list_capture_devices(self, recorder_preference: str) -> list[tuple[str, str]]:
        devices: list[tuple[str, str]] = [("", "System Default")]
        seen = {""}

        def add_device(value: str, label: str) -> None:
            normalized = value.strip()
            if normalized in seen:
                return
            seen.add(normalized)
            devices.append((normalized, label))

        if recorder_preference in {"auto", "arecord"} and shutil.which("arecord"):
            for value, label in self._list_arecord_hardware_devices():
                add_device(value, label)

        if recorder_preference in {"auto", "pw-record"} and shutil.which("pactl"):
            for value, label in self._list_pulse_sources():
                add_device(value, label)

        if recorder_preference in {"auto", "pw-record"} and shutil.which("wpctl"):
            for value, label in self._list_wpctl_sources():
                add_device(value, label)

        return devices

    def _list_arecord_hardware_devices(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
        devices: list[tuple[str, str]] = []
        pattern = re.compile(
            r"card\s+(?P<card>\d+):\s+[^\[]+\[(?P<card_name>[^\]]+)\],\s+device\s+(?P<device>\d+):\s+[^\[]+\[(?P<device_name>[^\]]+)\]"
        )
        for line in result.stdout.splitlines():
            match = pattern.search(line)
            if not match:
                continue
            card = match.group("card")
            device = match.group("device")
            card_name = match.group("card_name").strip()
            device_name = match.group("device_name").strip()
            value = f"hw:{card},{device}"
            label = f"ALSA: {card_name} / {device_name} ({value})"
            devices.append((value, label))
        return devices

    def _list_pulse_sources(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            check=False,
        )
        devices: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            source_name = parts[1].strip()
            if not source_name:
                continue
            devices.append((source_name, f"PipeWire/Pulse: {source_name}"))
        return devices

    def _list_wpctl_sources(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            ["wpctl", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        devices: list[tuple[str, str]] = []
        in_sources = False
        pattern = re.compile(r"[\*\s]*[\.\d]+\.\s+(?P<name>.+?)\s+\[vol:", re.IGNORECASE)
        for raw_line in result.stdout.splitlines():
            stripped = raw_line.strip()
            if "Sources:" in stripped:
                in_sources = True
                continue
            if in_sources and not stripped:
                break
            if not in_sources:
                continue
            match = pattern.search(stripped)
            if not match:
                continue
            name = match.group("name").strip()
            if not name:
                continue
            devices.append((name, f"PipeWire: {name}"))
        return devices

    def _derive_repo_name(self, repo_spec: str) -> str:
        cleaned = repo_spec.rstrip("/").rsplit("/", 1)[-1]
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        return cleaned or "project"

    def _build_clone_command(self, repo_spec: str, target_path: str) -> tuple[list[str], str | None]:
        repo_slug = None
        if repo_spec.startswith("http://") or repo_spec.startswith("https://") or repo_spec.startswith("git@"):
            clone_source = repo_spec
            if "github.com/" in repo_spec:
                repo_slug = repo_spec.split("github.com/", 1)[1].rstrip("/")
                if repo_slug.endswith(".git"):
                    repo_slug = repo_slug[:-4]
        else:
            repo_slug = repo_spec
            clone_source = f"https://github.com/{repo_spec}.git"

        if shutil.which("gh") is not None and repo_slug and "/" in repo_slug:
            return ["gh", "repo", "clone", repo_slug, target_path], repo_slug
        if shutil.which("git") is not None:
            return ["git", "clone", clone_source, target_path], repo_slug
        raise RuntimeError("Weder gh noch git sind installiert.")

    def _get_workspace_state(self) -> dict:
        workspaces = self.state.get("workspaces")
        if not isinstance(workspaces, dict):
            workspaces = {}
            self.state["workspaces"] = workspaces
        workspace_state = workspaces.get(self.args.working_dir)
        if not isinstance(workspace_state, dict):
            workspace_state = {}
            workspaces[self.args.working_dir] = workspace_state
        return workspace_state

    def _save_workspace_state(self) -> None:
        workspace_state = self._get_workspace_state()
        workspace_state["resume_enabled"] = self.resume_enabled
        workspace_state["session_id"] = self.current_session_id
        workspace_state["voice_enter_enabled"] = self.voice_enter_enabled
        self._save_gui_state()

    def _refresh_resume_button(self) -> None:
        style_context = self.resume_button.get_style_context()
        style_context.remove_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
        if self.resume_enabled:
            self.resume_button.set_label("Resume beim Neustart: AN")
            if self.current_session_id:
                tooltip = (
                    "Naechster Start verwendet "
                    f"'codex resume {self.current_session_id}'."
                )
            else:
                tooltip = (
                    "Resume ist aktiv. Die Session-ID der laufenden Sitzung "
                    "wird noch ermittelt."
                )
            style_context.add_class(Gtk.STYLE_CLASS_SUGGESTED_ACTION)
        else:
            self.resume_button.set_label("Resume beim Neustart: AUS")
            tooltip = "Naechster Start beginnt eine neue Codex-Session."
        self.resume_button.set_tooltip_text(tooltip)

    def _spawn_codex(self) -> None:
        argv = self._build_codex_argv()
        executable = argv[0]
        if shutil.which(executable) is None and not Path(executable).exists():
            self._set_status(f"Codex nicht gefunden: {executable}")
            return

        envv = [f"{key}={value}" for key, value in os.environ.items()]
        if self.resume_enabled and self.current_session_id:
            self._set_status(f"Starte Codex mit Resume {self.current_session_id}...")
        else:
            self._set_status("Starte neue Codex-Session...")
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    category=DeprecationWarning,
                    message=r".*Vte\.Terminal\.spawn_sync.*",
                )
                _started, pid = self.terminal.spawn_sync(
                    Vte.PtyFlags.DEFAULT,
                    self.args.working_dir,
                    argv,
                    envv,
                    GLib.SpawnFlags.DEFAULT,
                    None,
                    None,
                    None,
                )
        except GLib.Error as error:
            self.codex_pid = None
            self._set_status(f"Codex konnte nicht gestartet werden: {error.message}")
            return

        self.codex_pid = pid
        self.restart_pending = False
        self._set_status("Codex bereit.")
        self.prompt_entry.grab_focus()
        self._schedule_session_refresh()

        if self.pending_prompt:
            prompt = self.pending_prompt
            self.pending_prompt = None
            GLib.timeout_add(900, self._send_pending_prompt, prompt)

        if self.pending_voice_transcript:
            transcript = self.pending_voice_transcript
            self.pending_voice_transcript = None
            GLib.timeout_add(1100, self._send_pending_voice_transcript, transcript)

    def _send_pending_prompt(self, prompt: str) -> bool:
        self._feed_terminal(prompt + "\n")
        return False

    def _send_pending_voice_transcript(self, transcript: str) -> bool:
        self._apply_voice_transcript(transcript)
        return False

    def _feed_terminal(self, text: str) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=DeprecationWarning,
                message=r".*Vte\.Terminal\.feed_child_binary.*",
            )
            self.terminal.feed_child_binary(text.encode("utf-8"))

    def _send_terminal_enter(self) -> None:
        self._feed_terminal("\r")

    def _clear_terminal_input(self) -> None:
        # Ctrl+U clears the current editable input line in terminal-style prompts.
        self._feed_terminal("\x15")

    def _on_terminal_child_exited(self, _terminal, status: int) -> None:
        self.codex_pid = None
        if self.session_refresh_source_id is not None:
            GLib.source_remove(self.session_refresh_source_id)
            self.session_refresh_source_id = None
        if self.restart_pending:
            self._set_status("Starte Codex neu...")
            return
        self._set_status(f"Codex beendet (Exit-Status {status}).")

    def _on_terminal_title_changed(self, terminal) -> None:
        title = terminal.get_window_title()
        if title:
            self.set_title(f"{self.args.title} - {title}")

    def _on_window_destroy(self, _window) -> None:
        if self.dictation.is_live():
            self.dictation.stop_live()
        for path in self.session_temp_files:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _on_restart_clicked(self, _button) -> None:
        self._restart_codex(force_fresh_session=not self.resume_enabled)

    def _on_stop_supermode_clicked(self, _button) -> None:
        if not self.supermode_active:
            self._set_status("Supermode ist hier nicht aktiv.")
            return
        if self.supervisor_pid is None:
            self._set_status("Supervisor-PID ist unbekannt. Supermode kann nicht sauber beendet werden.")
            return

        try:
            os.kill(self.supervisor_pid, signal.SIGTERM)
        except ProcessLookupError:
            self._set_status("Supervisor laeuft nicht mehr. Schliesse die App direkt.")
            self.close()
            return
        except PermissionError:
            self._set_status("Keine Berechtigung, den Supervisor zu beenden.")
            return

        self._set_status("Supermode wird beendet. Die App schliesst jetzt.")
        GLib.timeout_add(150, self._close_window_after_supermode_stop)

    def _close_window_after_supermode_stop(self) -> bool:
        self.close()
        return False

    def _on_open_project_clicked(self, _button) -> None:
        selected = self._choose_project_folder("Projektordner auswaehlen", self.args.working_dir)
        if selected:
            self._activate_project(selected)

    def _on_new_project_clicked(self, _button) -> None:
        project_data = self._prompt_new_project()
        if project_data is None:
            return
        project_path, init_git = project_data
        path = Path(project_path)
        if path.exists():
            self._set_status(f"Projektordner existiert bereits: {path}")
            return
        path.mkdir(parents=True, exist_ok=False)
        if init_git and shutil.which("git") is not None:
            result = subprocess.run(
                ["git", "init"],
                cwd=str(path),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                self._set_status(
                    f"Projekt erstellt, aber Git-Init fehlgeschlagen: {result.stderr.strip() or result.stdout.strip()}"
                )
        self._activate_project(str(path))

    def _on_clone_project_clicked(self, _button) -> None:
        clone_data = self._prompt_github_clone()
        if clone_data is None:
            return
        repo_spec, target_path = clone_data
        target = Path(target_path)
        if target.exists():
            self._set_status(f"Zielordner existiert bereits: {target}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            command, repo_slug = self._build_clone_command(repo_spec, str(target))
        except RuntimeError as error:
            self._set_status(str(error))
            return

        self._set_status(f"Klonen laeuft: {' '.join(command)}")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            error_text = result.stderr.strip() or result.stdout.strip() or "Unbekannter Fehler"
            self._set_status(f"GitHub-Klonen fehlgeschlagen: {error_text}")
            return
        self._activate_project(str(target), source="github", github_repo=repo_slug)

    def _on_reveal_project_clicked(self, _button) -> None:
        if shutil.which("xdg-open") is None:
            self._set_status("xdg-open ist nicht installiert.")
            return
        subprocess.Popen(
            ["xdg-open", self.args.working_dir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _on_recent_project_clicked(self, _button, path: str) -> None:
        self._activate_project(path)

    def _on_clear_terminal_input_clicked(self, _button) -> None:
        self.prompt_entry.set_text("")
        if self.codex_pid is None:
            self._set_status("Codex ist nicht aktiv. Nichts zu loeschen.")
            return
        self._clear_terminal_input()
        self._set_status("Aktuelle Codex-Eingabe im Terminal geloescht.")

    def _on_settings_clicked(self, _button) -> None:
        settings = self._prompt_settings()
        if settings is None:
            return

        codex_restart_required = (
            settings["codex_bin"] != self.args.codex_bin
            or settings["codex_args"] != self.args.codex_args
            or settings["codex_search_enabled"] != self.codex_search_enabled
            or settings["codex_bypass_enabled"] != self.codex_bypass_enabled
        )

        self.start_in_last_project = settings["start_in_last_project"]
        self.args.title = settings["title"]
        self.args.codex_bin = settings["codex_bin"]
        self.args.codex_args = settings["codex_args"]
        self.codex_search_enabled = settings["codex_search_enabled"]
        self.codex_bypass_enabled = settings["codex_bypass_enabled"]
        self.args.language = settings["language"]

        self.dictation_provider = settings["provider"]
        self.dictation_command_template = settings["command_template"]
        self.dictation_api_url = settings["api_url"]
        self.dictation_api_key = settings["api_key"]
        self.dictation_api_model = settings["api_model"]
        self.dictation_recorder_preference = settings["recorder_preference"]
        self.dictation_alsa_device = settings["alsa_device"]
        self.dictation_silence_seconds = settings["silence_seconds"]
        self.dictation_start_threshold = settings["start_threshold"]

        if self.dictation.is_live():
            self.dictation.stop_live()

        self.dictation.apply_settings(
            language=self.args.language,
            provider=self.dictation_provider,
            command_template=self.dictation_command_template,
            api_base_url=self.dictation_api_url,
            api_key=self.dictation_api_key,
            api_model=self.dictation_api_model,
            recorder_preference=self.dictation_recorder_preference,
            alsa_device=self.dictation_alsa_device,
            silence_seconds=self.dictation_silence_seconds,
            start_threshold=self.dictation_start_threshold,
        )
        self._save_app_settings()
        self.header.props.title = self.args.title
        self.set_title(self.args.title)
        self._refresh_codex_mode_ui()
        self._refresh_dictation_controls()

        if codex_restart_required:
            self._set_status("Einstellungen gespeichert. Codex wird mit neuen Startparametern neu gestartet...")
            self._restart_codex(force_fresh_session=not self.resume_enabled)
            return

        self._set_status("Einstellungen gespeichert.")

    def _restart_codex(self, force_fresh_session: bool) -> None:
        self.restart_pending = True
        if force_fresh_session:
            self.current_session_id = None
            self._save_workspace_state()
            self._refresh_resume_button()
        if self.codex_pid is not None:
            try:
                os.kill(self.codex_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.pending_prompt = None
        GLib.timeout_add(250, self._restart_after_exit)

    def _restart_after_exit(self) -> bool:
        if self.codex_pid is not None:
            return True
        self._spawn_codex()
        return False

    def _schedule_session_refresh(self) -> None:
        if self.session_refresh_source_id is not None:
            GLib.source_remove(self.session_refresh_source_id)
            self.session_refresh_source_id = None
        if self.resume_enabled and self.current_session_id:
            self._save_workspace_state()
            self._refresh_resume_button()
            return
        self.session_refresh_source_id = GLib.timeout_add_seconds(1, self._refresh_current_session_id)

    def _refresh_current_session_id(self) -> bool:
        self.session_refresh_source_id = None
        if self.codex_pid is None:
            return False
        session_id = self._find_latest_session_id_for_working_dir()
        if session_id is None:
            self.session_refresh_source_id = GLib.timeout_add_seconds(
                1, self._refresh_current_session_id
            )
            return False
        if session_id != self.current_session_id:
            self.current_session_id = session_id
            self._save_workspace_state()
            self._refresh_resume_button()
            if self.resume_enabled:
                self._set_status(
                    f"Resume aktiv. Naechster Start verwendet Session {session_id}."
                )
        return False

    def _find_latest_session_id_for_working_dir(self) -> str | None:
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.exists():
            return None

        session_files = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for session_file in session_files[:50]:
            try:
                with session_file.open("r", encoding="utf-8") as handle:
                    first_line = handle.readline()
            except OSError:
                continue
            if not first_line:
                continue
            try:
                payload = json.loads(first_line)
            except json.JSONDecodeError:
                continue
            meta = payload.get("payload")
            if payload.get("type") != "session_meta" or not isinstance(meta, dict):
                continue
            if meta.get("cwd") != self.args.working_dir:
                continue
            session_id = meta.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def _on_send_clicked(self, _widget) -> None:
        prompt = self.prompt_entry.get_text().strip()
        if not prompt:
            return

        self._submit_prompt(prompt)

    def _on_prompt_library_clicked(self, _button) -> None:
        prompt_action = self._prompt_prompt_library()
        if prompt_action is None:
            return

        action, text = prompt_action
        if action == "send":
            self._submit_prompt(text)
            return

        self.prompt_entry.set_text(text)
        self.prompt_entry.grab_focus()
        self.prompt_entry.set_position(-1)
        self._set_status("Prompt aus der globalen Prompt-Verwaltung geladen.")

    def _on_dictation_clicked(self, _button) -> None:
        if self.dictation.is_live():
            self.dictation.stop_live()
            self._set_status("Live-Diktat ausgeschaltet.")
            self._refresh_dictation_controls()
            return

        if not self.dictation.available():
            self._on_dictation_error(self.dictation.description())
            return

        self.dictation.start_live(
            self._on_dictation_text,
            self._set_status,
            self._on_dictation_error,
            self._on_live_mode_changed,
            self._on_dictation_level,
        )

    def _on_dictation_text(self, text: str) -> bool:
        self._refresh_dictation_controls()
        self.prompt_entry.set_text(text)
        self.prompt_entry.grab_focus()
        self.prompt_entry.set_position(-1)
        self._apply_voice_transcript(text)
        return False

    def _on_dictation_error(self, message: str) -> bool:
        self._refresh_dictation_controls()
        self._set_status(message)
        return False

    def _on_live_mode_changed(self, active: bool) -> bool:
        self._refresh_dictation_controls()
        if active:
            self._set_status("Live-Diktat aktiv. Sprich jetzt. 1.5 Sekunden Stille senden automatisch ab.")
        else:
            self._on_dictation_level(0.0)
            self._set_status("Live-Diktat ausgeschaltet.")
        return False

    def _on_dictation_level(self, level: float) -> bool:
        scale = max(float(self.dictation.start_threshold) * 3.0, 1000.0)
        self.mic_level_bar.set_fraction(max(0.0, min(level / scale, 1.0)))
        return False

    def _on_resume_toggled(self, button: Gtk.ToggleButton) -> None:
        self.resume_enabled = button.get_active()
        self._save_workspace_state()
        self._refresh_resume_button()
        if self.resume_enabled:
            self._schedule_session_refresh()
            self._set_status(
                "Resume aktiviert. Der naechste Start verwendet die aktuelle Session."
            )
            return
        self._set_status("Resume deaktiviert. Starte frische Codex-Session...")
        self._restart_codex(force_fresh_session=True)

    def _on_codex_search_toggled(self, button: Gtk.ToggleButton) -> None:
        self.codex_search_enabled = button.get_active()
        self._save_app_settings()
        self._refresh_codex_mode_ui()
        if self.codex_search_enabled:
            self._set_status("Codex-Startmodus --search aktiviert. Codex wird neu gestartet...")
        else:
            self._set_status("Codex-Startmodus --search deaktiviert. Codex wird neu gestartet...")
        self._restart_codex(force_fresh_session=not self.resume_enabled)

    def _on_codex_bypass_toggled(self, button: Gtk.ToggleButton) -> None:
        self.codex_bypass_enabled = button.get_active()
        self._save_app_settings()
        self._refresh_codex_mode_ui()
        if self.codex_bypass_enabled:
            self._set_status(
                "Codex-Startmodus --dangerously-bypass-approvals-and-sandbox aktiviert. Codex wird neu gestartet..."
            )
        else:
            self._set_status(
                "Codex-Startmodus --dangerously-bypass-approvals-and-sandbox deaktiviert. Codex wird neu gestartet..."
            )
        self._restart_codex(force_fresh_session=not self.resume_enabled)

    def _on_voice_enter_toggled(self, button: Gtk.ToggleButton) -> None:
        self.voice_enter_enabled = button.get_active()
        self._save_workspace_state()
        self._refresh_voice_enter_button()
        if self.voice_enter_enabled:
            self._set_status("Sprachmodus aktiv: 'Enter' muss gesagt werden.")
        else:
            self._set_status("Sprachmodus auto-send: jedes Diktat sendet echtes Enter.")

    def _submit_prompt(self, prompt: str) -> None:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return

        self._record_prompt_library_entry(clean_prompt, source="text")

        self.prompt_entry.set_text("")
        if self.codex_pid is None:
            self.pending_prompt = clean_prompt
            self._set_status("Codex ist nicht aktiv. Starte neue Session...")
            self._spawn_codex()
            return

        self._feed_terminal(clean_prompt + "\n")
        self._set_status("Prompt an Codex gesendet.")

    def _apply_voice_transcript(self, transcript: str) -> None:
        if self.codex_pid is None:
            self.pending_voice_transcript = transcript
            self._set_status("Codex ist nicht aktiv. Starte neue Session...")
            self._spawn_codex()
            return

        cursor = 0
        handled_anything = False
        saw_enter = False
        saw_screenshot = False
        auto_sent_enter = False
        screenshot_error: str | None = None
        prompt_parts: list[str] = []
        command_pattern = (
            self.VOICE_COMMAND_PATTERN
            if self.voice_enter_enabled
            else self.VOICE_SCREENSHOT_PATTERN
        )

        for match in command_pattern.finditer(transcript):
            text_part = transcript[cursor:match.start()]
            cleaned = text_part.strip()
            if cleaned:
                prompt_parts.append(cleaned)
                self._feed_terminal(cleaned + "\n\n")
                handled_anything = True

            command = match.group(0).strip().lower().replace(" ", "")
            if command == "enter":
                self._send_terminal_enter()
                saw_enter = True
                handled_anything = True
            elif command in {"shotscreen", "touchscreen"}:
                try:
                    self._attach_screenshot_to_codex()
                    saw_screenshot = True
                    handled_anything = True
                except Exception as exc:
                    screenshot_error = str(exc)
            cursor = match.end()

        tail = transcript[cursor:]
        cleaned_tail = tail.strip()
        if cleaned_tail:
            prompt_parts.append(cleaned_tail)
            self._feed_terminal(cleaned_tail + "\n\n")
            handled_anything = True

        recorded_prompt = " ".join(prompt_parts).strip()
        if recorded_prompt:
            self._record_prompt_library_entry(recorded_prompt, source="voice")

        if handled_anything and not self.voice_enter_enabled:
            self._send_terminal_enter()
            auto_sent_enter = True

        if screenshot_error and saw_enter:
            self._set_status(
                f"Screenshot fehlgeschlagen ({screenshot_error}). Restlicher Text und 'Enter' wurden trotzdem gesendet."
            )
        elif screenshot_error and auto_sent_enter:
            self._set_status(
                f"Screenshot fehlgeschlagen ({screenshot_error}). Restlicher Text wurde trotzdem gesendet und bestaetigt."
            )
        elif screenshot_error:
            self._set_status(
                f"Screenshot fehlgeschlagen ({screenshot_error}). Restlicher Text wurde trotzdem gesendet."
            )
        elif saw_screenshot and auto_sent_enter:
            self._set_status(
                "Screenshot angehaengt und Diktat automatisch mit echtem Enter gesendet."
            )
        elif saw_screenshot and saw_enter:
            self._set_status("Screenshot angehaengt und Sprachbefehl 'Enter' an Codex gesendet.")
        elif saw_screenshot:
            self._set_status("Screenshot an Codex angehaengt.")
        elif auto_sent_enter:
            self._set_status("Diktat ins Terminal uebertragen und automatisch mit echtem Enter gesendet.")
        elif saw_enter:
            self._set_status("Sprachbefehl 'Enter' erkannt und an Codex gesendet.")
        elif handled_anything:
            self._set_status("Diktat ins Terminal uebertragen. Fuer Senden einfach 'Enter' sagen.")

    def _attach_screenshot_to_codex(self) -> None:
        screenshot_path = self._capture_screenshot_to_file()
        self.session_temp_files.append(screenshot_path)
        self._feed_terminal(f"[local_image:{screenshot_path}]\n\n")

    def _capture_screenshot_to_file(self) -> str:
        path = self._capture_via_gnome_shell()
        if path:
            return path
        path = self._capture_via_gnome_screenshot_command()
        if path:
            return path
        path = self._capture_via_portal()
        if path:
            return path
        path = self._capture_via_ximagesrc()
        if path:
            return path
        raise RuntimeError("Kein funktionierender Screenshot-Kanal verfuegbar.")

    def _capture_via_gnome_shell(self) -> str | None:
        output_path = self._make_temp_screenshot_path()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            0,
            None,
            "org.gnome.Shell",
            "/org/gnome/Shell/Screenshot",
            "org.gnome.Shell.Screenshot",
            None,
        )
        try:
            result = proxy.call_sync(
                "Screenshot",
                GLib.Variant("(bbs)", (False, False, output_path)),
                Gio.DBusCallFlags.NONE,
                10000,
                None,
            )
        except GLib.Error:
            return None

        success, filename_used = result.unpack()
        if not success:
            raise RuntimeError("GNOME-Shell-Screenshot fehlgeschlagen.")
        if not os.path.exists(filename_used):
            raise RuntimeError("GNOME-Shell-Screenshot lieferte keine Datei.")
        return filename_used

    def _capture_via_gnome_screenshot_command(self) -> str | None:
        if shutil.which("gnome-screenshot") is None:
            return None

        output_path = self._make_temp_screenshot_path()
        result = subprocess.run(
            ["gnome-screenshot", "-f", output_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        if not os.path.exists(output_path):
            return None
        if os.path.getsize(output_path) == 0:
            try:
                os.unlink(output_path)
            except FileNotFoundError:
                pass
            return None
        return output_path

    def _capture_via_portal(self) -> str:
        self._ensure_portal_services()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = self._make_portal_proxy(bus)

        token = f"codexgui{os.getpid()}{int(time.time() * 1000)}"
        response_holder: dict[str, object] = {"done": False}
        loop = GLib.MainLoop()

        def on_response(_conn, _sender, object_path, _iface, _signal, params, _user_data) -> None:
            if response_holder.get("handle_path") != object_path:
                return
            code, results = params.unpack()
            response_holder["code"] = code
            response_holder["results"] = results
            response_holder["done"] = True
            loop.quit()

        subscription_id = bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
        try:
            try:
                handle = self._call_portal_screenshot(proxy, token)
            except GLib.Error as exc:
                # The portal may have been started before the GNOME backend was active.
                if "UnknownMethod" not in exc.message:
                    raise
                subprocess.run(
                    ["systemctl", "--user", "restart", "xdg-desktop-portal.service"],
                    capture_output=True,
                    check=False,
                )
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                proxy = self._make_portal_proxy(bus)
                handle = self._call_portal_screenshot(proxy, token)
            handle_path = handle.unpack()[0]
            response_holder["handle_path"] = handle_path
            GLib.timeout_add_seconds(30, self._on_portal_timeout, loop, response_holder)
            loop.run()
        finally:
            bus.signal_unsubscribe(subscription_id)

        if not response_holder.get("done"):
            raise RuntimeError("Screenshot-Portal hat nicht geantwortet.")

        code = response_holder.get("code")
        results = response_holder.get("results", {})
        if code != 0:
            raise RuntimeError(f"Screenshot-Portal abgebrochen oder fehlgeschlagen (Code {code}).")

        uri = results.get("uri")
        if hasattr(uri, "unpack"):
            uri = uri.unpack()
        if not uri:
            raise RuntimeError("Screenshot-Portal lieferte keine Bild-URI.")
        source_path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
        if not source_path or not os.path.exists(source_path):
            raise RuntimeError("Screenshot-Datei aus dem Portal ist nicht lesbar.")
        temp_path = self._make_temp_screenshot_path()
        shutil.copy2(source_path, temp_path)
        return temp_path

    def _make_portal_proxy(self, bus: Gio.DBusConnection) -> Gio.DBusProxy:
        return Gio.DBusProxy.new_sync(
            bus,
            0,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Screenshot",
            None,
        )

    def _call_portal_screenshot(self, proxy: Gio.DBusProxy, token: str):
        return proxy.call_sync(
            "Screenshot",
            GLib.Variant(
                "(sa{sv})",
                (
                    "",
                    {
                        "handle_token": GLib.Variant("s", token),
                        "interactive": GLib.Variant("b", False),
                        "modal": GLib.Variant("b", False),
                    },
                ),
            ),
            Gio.DBusCallFlags.NONE,
            15000,
            None,
        )

    def _ensure_portal_services(self) -> None:
        for unit in ("xdg-desktop-portal-gnome.service", "xdg-desktop-portal.service"):
            subprocess.run(
                ["systemctl", "--user", "start", unit],
                capture_output=True,
                check=False,
            )

    def _capture_via_ximagesrc(self) -> str | None:
        if shutil.which("gst-launch-1.0") is None:
            return None

        output_path = self._make_temp_screenshot_path(".jpg")
        result = subprocess.run(
            [
                "gst-launch-1.0",
                "-q",
                "ximagesrc",
                "use-damage=false",
                "num-buffers=1",
                "!",
                "videoconvert",
                "!",
                "jpegenc",
                "!",
                "fdsink",
                "fd=1",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        if not result.stdout:
            return None

        with open(output_path, "wb") as screenshot_file:
            screenshot_file.write(result.stdout)

        if os.path.getsize(output_path) == 0:
            try:
                os.unlink(output_path)
            except FileNotFoundError:
                pass
            return None
        return output_path

    def _on_portal_timeout(self, loop: GLib.MainLoop, response_holder: dict[str, object]) -> bool:
        if response_holder.get("done"):
            return False
        response_holder["done"] = False
        loop.quit()
        return False

    def _make_temp_screenshot_path(self, suffix: str = ".png") -> str:
        return os.path.join(
            tempfile.gettempdir(),
            f"codex-gui-screenshot-{int(time.time() * 1000)}{suffix}",
        )

    def _set_status(self, message: str) -> bool:
        self.status_label.set_text(message)
        return False


class CodexTerminalApp(Gtk.Application):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(application_id="local.codex.terminal.gui")
        self.args = args

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = CodexTerminalWindow(self, self.args)
        window.present()


def main() -> int:
    args = parse_args()
    app = CodexTerminalApp(args)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
