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
import urllib.parse
import warnings
import wave
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOCAL_PYTHON_DEPS = APP_DIR / ".python-deps"
GUI_STATE_PATH = APP_DIR / ".codex-gui-state.json"

if LOCAL_PYTHON_DEPS.exists():
    sys.path.insert(0, str(LOCAL_PYTHON_DEPS))

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

from gi.repository import Gio, GLib, Gtk, Vte


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
        default=os.environ.get("CODEX_BIN", "codex"),
        help="Path to the codex executable. Defaults to $CODEX_BIN or 'codex'.",
    )
    parser.add_argument(
        "--codex-args",
        default=os.environ.get("CODEX_GUI_CODEX_ARGS", ""),
        help="Extra arguments appended to the Codex startup command.",
    )
    parser.add_argument(
        "--language",
        default=os.environ.get("CODEX_GUI_STT_LANGUAGE", "de-DE"),
        help="Language code used by the optional SpeechRecognition backend.",
    )
    parser.add_argument(
        "--title",
        default="Codex Terminal GUI",
        help="Window title.",
    )
    return parser.parse_args()


class DictationBackend:
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2
    CHANNELS = 1
    CHUNK_MS = 100
    CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHUNK_MS // 1000
    SILENCE_SECONDS = 1.5
    MAX_UTTERANCE_SECONDS = 30
    PRE_ROLL_CHUNKS = 4
    START_THRESHOLD = 550

    def __init__(self, language: str) -> None:
        self.language = language
        self.command_template = os.environ.get("CODEX_GUI_TRANSCRIBE_CMD", "").strip()
        self.recorder_cmd = self._detect_recorder_stream()
        self.recorder_process: subprocess.Popen[str] | None = None
        self.installing = False
        self.bootstrap_error: str | None = None
        self.live_mode = False
        self.stop_event = threading.Event()
        self.live_thread: threading.Thread | None = None

    def available(self) -> bool:
        return bool(self.recorder_cmd) and (
            self._speech_recognition_available() or bool(self.command_template)
        )

    def description(self) -> str:
        if not self._has_capture_device():
            return "Kein Aufnahmegeraet erkannt. Bitte Mikrofon oder Audio-Quelle pruefen."
        if self.command_template:
            return (
                "Diktat bereit. Aufnahme ueber lokales Mikrofon, Transkription ueber "
                "CODEX_GUI_TRANSCRIBE_CMD."
            )
        if self._speech_recognition_available():
            return (
                "Live-Diktat bereit. Klick startet dauerhaftes Lauschen mit Auto-Senden."
            )
        if self.installing:
            return "Installiere automatisch die Spracherkennungs-Abhaengigkeit SpeechRecognition."
        if self.bootstrap_error:
            return self.bootstrap_error
        if self._pip_available():
            return (
                "Diktier-Engine wird automatisch nachinstalliert. "
                "Klick zum Starten, erneut klicken zum Stoppen."
            )
        return (
            "Keine Diktier-Engine gefunden und keine automatische Installation moeglich. "
            "Setze CODEX_GUI_TRANSCRIBE_CMD oder pruefe pip."
        )

    def uses_toggle_recording(self) -> bool:
        return True

    def is_live(self) -> bool:
        return self.live_mode

    def start_live(self, on_text, on_status, on_error, on_state_change) -> None:
        if self.live_mode:
            return
        if not self.available():
            on_error(self.description())
            return

        self.live_mode = True
        self.stop_event.clear()
        self.live_thread = threading.Thread(
            target=self._live_loop,
            args=(on_text, on_status, on_error, on_state_change),
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
        if shutil.which("arecord"):
            return [
                "arecord",
                "-q",
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "1",
                "-t",
                "raw",
            ]
        if shutil.which("pw-record"):
            return [
                "pw-record",
                "--rate",
                "16000",
                "--channels",
                "1",
                "--format",
                "s16",
                "-",
            ]
        return None

    def _has_capture_device(self) -> bool:
        if not self.recorder_cmd:
            return False
        if shutil.which("arecord"):
            result = subprocess.run(
                ["arecord", "-l"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout + "\n" + result.stderr).lower()
            return "capture hardware devices" in output and "card " in output
        return True

    def _pip_available(self) -> bool:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _live_loop(self, on_text, on_status, on_error, on_state_change) -> None:
        GLib.idle_add(on_status, "Live-Diktat aktiv. Sprich jetzt. 1.5 Sekunden Stille senden automatisch ab.")
        try:
            while not self.stop_event.is_set():
                try:
                    audio_bytes = self._capture_utterance()
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
            GLib.idle_add(on_state_change, False)

    def _capture_utterance(self) -> bytes | None:
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
                is_speech = level >= self.START_THRESHOLD

                if speech_started:
                    frames.append(chunk)
                    if is_speech:
                        silence_chunks = 0
                    else:
                        silence_chunks += 1

                    if silence_chunks * self.CHUNK_MS >= self.SILENCE_SECONDS * 1000:
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
        if self.command_template:
            return self._transcribe_audio_with_command(audio_bytes)

        if self._speech_recognition_available():
            import speech_recognition as sr

            recognizer = sr.Recognizer()
            audio = sr.AudioData(audio_bytes, self.SAMPLE_RATE, self.SAMPLE_WIDTH)
            return recognizer.recognize_google(audio, language=self.language).strip()

        raise RuntimeError(self.description())

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
        r"\b(?:shotscreen|shot\s+screen)\b",
        re.IGNORECASE,
    )
    VOICE_COMMAND_PATTERN = re.compile(
        r"\b(?:enter|shotscreen|shot\s+screen)\b",
        re.IGNORECASE,
    )

    def __init__(self, app: Gtk.Application, args: argparse.Namespace) -> None:
        super().__init__(application=app, title=args.title)
        self.args = args
        self.args.working_dir = str(Path(self.args.working_dir).resolve())
        self.set_default_size(1200, 760)

        self.codex_pid: int | None = None
        self.pending_prompt: str | None = None
        self.pending_voice_transcript: str | None = None
        self.restart_pending = False
        self.session_refresh_source_id: int | None = None
        self.session_temp_files: list[str] = []
        self.dictation = DictationBackend(language=args.language)
        self.state = self._load_gui_state()
        workspace_state = self._get_workspace_state()
        self.resume_enabled = bool(workspace_state.get("resume_enabled", False))
        self.current_session_id = workspace_state.get("session_id")

        if not self.resume_enabled and self.current_session_id:
            self.current_session_id = None
            self._save_workspace_state()

        self._build_ui()
        self._refresh_dictation_controls()
        self._bootstrap_dictation()
        self._spawn_codex()

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_border_width(10)
        self.add(root)
        self.connect("destroy", self._on_window_destroy)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = "Codex"
        header.props.subtitle = self.args.working_dir
        self.set_titlebar(header)

        self.restart_button = Gtk.Button(label="Neu starten")
        self.restart_button.connect("clicked", self._on_restart_clicked)
        header.pack_end(self.restart_button)

        intro = Gtk.Label(
            label=(
                "Prompt hier eingeben oder diktieren. "
                "Beim Absenden wird der Text in die laufende Codex-CLI im Terminal geschickt."
            )
        )
        intro.set_xalign(0)
        intro.set_line_wrap(True)
        root.pack_start(intro, False, False, 0)

        resume_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.pack_start(resume_controls, False, False, 0)

        self.resume_button = Gtk.ToggleButton()
        self.resume_button.set_active(self.resume_enabled)
        self.resume_button.connect("toggled", self._on_resume_toggled)
        resume_controls.pack_end(self.resume_button, False, False, 0)
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

        self.dictation_button = Gtk.Button()
        self.dictation_button.connect("clicked", self._on_dictation_clicked)
        self._update_dictation_button_visuals()
        controls.pack_start(self.dictation_button, False, False, 0)

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

    def _refresh_dictation_controls(self) -> None:
        self.dictation_button.set_sensitive(self.dictation.available() or self.dictation.is_live())
        self.dictation_button.set_tooltip_text(self.dictation.description())
        self._update_dictation_button_visuals()

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
            *extra_args,
        ]
        if self.resume_enabled and self.current_session_id:
            argv.extend(["resume", self.current_session_id])
        return argv

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
            self._set_status("Live-Diktat ausgeschaltet.")
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

    def _submit_prompt(self, prompt: str) -> None:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return

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
        screenshot_error: str | None = None

        for match in self.VOICE_COMMAND_PATTERN.finditer(transcript):
            text_part = transcript[cursor:match.start()]
            cleaned = text_part.strip()
            if cleaned:
                self._feed_terminal(cleaned + "\n\n")
                handled_anything = True

            command = match.group(0).strip().lower().replace(" ", "")
            if command == "enter":
                self._send_terminal_enter()
                saw_enter = True
                handled_anything = True
            elif command == "shotscreen":
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
            self._feed_terminal(cleaned_tail + "\n\n")
            handled_anything = True

        if screenshot_error and saw_enter:
            self._set_status(
                f"Screenshot fehlgeschlagen ({screenshot_error}). Restlicher Text und 'Enter' wurden trotzdem gesendet."
            )
        elif screenshot_error:
            self._set_status(
                f"Screenshot fehlgeschlagen ({screenshot_error}). Restlicher Text wurde trotzdem gesendet."
            )
        elif saw_screenshot and saw_enter:
            self._set_status("Screenshot angehaengt und Sprachbefehl 'Enter' an Codex gesendet.")
        elif saw_screenshot:
            self._set_status("Screenshot an Codex angehaengt.")
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
