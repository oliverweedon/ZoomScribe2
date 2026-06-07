#!/usr/bin/env python3
from __future__ import annotations
"""ZoomScribe 2 — menu bar entry point.

Run this instead of main.py to get a proper menu bar app with no Terminal.
Double-click ZoomScribe2.app (which calls this file) to launch.
"""

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import rumps

# ── Ensure ZoomScribe2 modules are importable regardless of cwd ───────────────
_HERE = Path(__file__).parent.resolve()
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import config
from corrections import Corrections
import enhancer
import gdocs
from zoom_reader import ZoomTranscriptReader

_SILENCE_TIMEOUT = 8
_FLUSH_INTERVAL  = 45
_ICON            = str(_HERE / "ZoomScribe2.icns")


class ZoomScribeApp(rumps.App):
    def __init__(self):
        super().__init__(
            name="ZoomScribe 2",
            icon=_ICON if Path(_ICON).exists() else None,
            quit_button=None,
        )

        self._start_item = rumps.MenuItem("Start Session…", callback=self._start)
        self._stop_item  = rumps.MenuItem("Stop",           callback=None)  # disabled
        self._dict_item  = rumps.MenuItem("Open Dictionary", callback=self._open_dict)
        self._quit_item  = rumps.MenuItem("Quit ZoomScribe 2", callback=rumps.quit_application)

        self.menu = [
            self._start_item,
            self._stop_item,
            None,
            self._dict_item,
            None,
            self._quit_item,
        ]

        self._stop_event   = threading.Event()
        self._session_thread: threading.Thread | None = None
        self._para_count   = 0
        self._start_time:  float | None = None
        self._doc_url:     str | None   = None
        self._dict_srv     = None

        self._launch_dict_server()

        # Auto-prompt for session name on startup (mirrors main.py behaviour)
        rumps.Timer(self._auto_start, 1.0).start()

    # ── Auto-start on launch ──────────────────────────────────────────────────

    def _auto_start(self, timer):
        timer.stop()           # one-shot
        self._start(None)

    # ── Dictionary server ─────────────────────────────────────────────────────

    def _launch_dict_server(self):
        try:
            from dictionary_server import DictionaryServer
            self._dict_srv = DictionaryServer()
            self._dict_srv.start()
        except OSError:
            pass  # port already in use (HaSofer running) — that's fine
        except Exception:
            pass

    # ── Menu actions ──────────────────────────────────────────────────────────

    @staticmethod
    def _ask_session_name() -> str | None:
        """AppleScript dialog — always appears in front regardless of app focus."""
        result = subprocess.run(
            ["osascript", "-e",
             'display dialog "Session name:\\n(leave blank to skip Google Doc)" '
             'default answer "" with title "ZoomScribe 2" '
             'buttons {"Cancel", "Start"} default button "Start"'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None  # user cancelled
        for part in result.stdout.strip().split(", "):
            if part.startswith("text returned:"):
                return part[len("text returned:"):].strip()
        return ""

    def _start(self, _):
        if self._session_thread and self._session_thread.is_alive():
            return

        session_name = self._ask_session_name()
        if session_name is None:
            return  # cancelled

        self._stop_event.clear()
        self._para_count = 0
        self._start_time = time.time()
        self._doc_url    = None

        self._start_item.set_callback(None)    # disable while running
        self._stop_item.set_callback(self._stop)
        self.title = "⏺"

        self._session_thread = threading.Thread(
            target=self._run_session,
            args=(session_name,),
            daemon=True,
            name="zoomscribe-session",
        )
        self._session_thread.start()

    def _stop(self, _):
        self._stop_event.set()

    def _open_dict(self, _):
        webbrowser.open("http://localhost:7878")

    # ── Status timer (every 3 s) ──────────────────────────────────────────────

    @rumps.timer(3)
    def _tick(self, _):
        running = bool(self._session_thread and self._session_thread.is_alive())
        if running and self._start_time:
            self.title = "⏺"
        elif not running and self._start_time is not None:
            # Session just ended — reset UI
            self.title = None
            self._start_time = None
            self._start_item.set_callback(self._start)
            self._stop_item.set_callback(None)

    # ── Session thread ────────────────────────────────────────────────────────

    def _run_session(self, session_name: str):
        """Full ZoomScribe2 session, runs on a background thread."""
        stop = self._stop_event

        # ── Corrections ───────────────────────────────────────────────────────
        corr = Corrections()

        # ── Google Doc ────────────────────────────────────────────────────────
        writer = None
        if session_name:
            template_id = config.get("template_doc_id", "")
            if template_id:
                try:
                    doc_id, title = gdocs.copy_template(template_id, session_name)
                    gdocs.fill_header(doc_id, title)
                    writer        = gdocs.DocsWriter(doc_id)
                    self._doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
                    rumps.notification(
                        "ZoomScribe 2", "Session started",
                        f"{title}",
                        sound=False,
                    )
                    webbrowser.open(self._doc_url)
                except Exception as e:
                    rumps.notification("ZoomScribe 2", "GDoc setup failed", str(e), sound=False)
            else:
                rumps.notification(
                    "ZoomScribe 2", "No template configured",
                    "Set template_doc_id in ~/.zoomscribe/config.json",
                    sound=False,
                )

        # ── Reader + auto-open Transcript ─────────────────────────────────────
        reader = ZoomTranscriptReader(poll_interval=1.5)

        if not reader.try_open_transcript_panel():
            rumps.notification(
                "ZoomScribe 2", "Open Transcript panel",
                "In Zoom: Live Transcript → View Full Transcript",
                sound=True,
            )

        # ── Turn-buffer state ─────────────────────────────────────────────────
        pending_speaker:  str | None = None
        pending_ts:       str        = ""
        pending_text:     str        = ""
        last_update_t:    float      = time.monotonic()
        first_pending_t:  float      = time.monotonic()
        written_text:     dict[tuple, str] = {}
        finalized_keys:   set[tuple]      = set()
        last_doc_speaker: str             = ""

        def _process(raw_text: str) -> str:
            corrected = corr.apply(raw_text)
            polished, _ = enhancer.enhance(corrected)
            return polished

        def flush(full_reset: bool = True):
            nonlocal pending_speaker, pending_ts, pending_text
            nonlocal last_update_t, first_pending_t, last_doc_speaker, finalized_keys

            if not pending_speaker or not pending_text:
                return

            key     = (pending_speaker, pending_ts)
            already = written_text.get(key, "")
            if pending_text.startswith(already):
                delta = pending_text[len(already):].strip()
            else:
                # Zoom revised earlier words. Never re-write what's already in
                # the doc — take only characters past the previously-written length.
                delta = pending_text[len(already):].strip()

            if not delta:
                if full_reset:
                    pending_speaker = None
                    pending_ts      = ""
                    pending_text    = ""
                    first_pending_t = time.monotonic()
                return

            try:
                polished = _process(delta)
            except Exception:
                polished = corr.apply(delta)

            speaker_label = corr.apply(pending_speaker)
            written_text[key] = pending_text

            if writer:
                if speaker_label != last_doc_speaker:
                    writer.append(polished, bold_prefix=speaker_label)
                    last_doc_speaker = speaker_label
                else:
                    writer.append(polished)
            self._para_count += 1

            if full_reset:
                finalized_keys.add(key)
                pending_speaker = None
                pending_ts      = ""
                pending_text    = ""
            first_pending_t = time.monotonic()
            last_update_t   = time.monotonic()

        # ── Main polling loop ─────────────────────────────────────────────────
        try:
            for item in reader.poll_forever():
                if stop.is_set():
                    break

                corr.reload_if_changed()
                now = time.monotonic()

                if pending_speaker:
                    silence = now - last_update_t
                    age     = now - first_pending_t
                    if silence > _SILENCE_TIMEOUT:
                        flush(full_reset=True)
                    elif age > _FLUSH_INTERVAL:
                        flush(full_reset=False)

                if item is None:
                    continue

                speaker, ts, text = item

                # Skip turns already written — ignore Zoom's post-hoc revisions
                if (speaker, ts) in finalized_keys:
                    continue

                if written_text.get((speaker, ts)) == text:
                    continue

                if pending_speaker is None:
                    pending_speaker = speaker
                    pending_ts      = ts
                    pending_text    = text
                    last_update_t   = now
                    first_pending_t = now

                elif (speaker, ts) == (pending_speaker, pending_ts):
                    pending_text  = text
                    last_update_t = now

                else:
                    flush(full_reset=True)
                    pending_speaker = speaker
                    pending_ts      = ts
                    pending_text    = text
                    last_update_t   = now
                    first_pending_t = now

        except Exception:
            pass

        # ── Cleanup ───────────────────────────────────────────────────────────
        flush(full_reset=True)

        if writer:
            writer.set_stopped()
            writer.close()

        summary = config.session_summary()
        rumps.notification("ZoomScribe 2", "Session ended", summary, sound=False)


if __name__ == "__main__":
    import traceback
    _LOG = Path.home() / ".zoomscribe" / "app_launch.log"
    try:
        _LOG.parent.mkdir(exist_ok=True)
        _LOG.write_text(f"Starting ZoomScribeApp at {time.time()}\n")
        app = ZoomScribeApp()
        _LOG.write_text(f"App created OK, calling run()\n")
        app.run()
    except Exception as _e:
        _LOG.write_text(f"CRASH: {_e}\n{traceback.format_exc()}")
