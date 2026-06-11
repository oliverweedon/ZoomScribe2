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

import datetime
import subprocess
import sys
import threading
import time
import traceback
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
            self.title = f"⏺ {self._para_count}p" if self._para_count else "⏺"
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

        # ── Session log (tee stdout → file so the user can tail -f it) ──────────
        _log_dir = Path.home() / ".zoomscribe"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _log_path = _log_dir / f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        _log_file = open(_log_path, "w", encoding="utf-8", buffering=1)

        class _Tee:
            def __init__(self, orig, log):
                self._orig, self._log = orig, log
            def write(self, data):
                self._orig.write(data)
                self._log.write(data)
            def flush(self):
                self._orig.flush()
                self._log.flush()
            def fileno(self):
                return self._orig.fileno()

        _orig_stdout = sys.stdout
        sys.stdout = _Tee(_orig_stdout, _log_file)

        # Open a Terminal window that tails the log so the user can see live output.
        _tail_sh = _log_dir / "tail_log.sh"
        _tail_sh.write_text(f"#!/bin/bash\necho 'ZoomScribe2 — {_log_path.name}'\ntail -f '{_log_path}'\n")
        _tail_sh.chmod(0o755)
        subprocess.Popen(["open", "-a", "Terminal", str(_tail_sh)])

        # Record wall-clock start time (HH:MM:SS, same format Zoom uses).
        # Any transcript entry with a timestamp before this is historical and
        # will be skipped — even if the baseline read was partial or missed entries.
        session_start_ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"  Session start timestamp: {session_start_ts}", flush=True)

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
        submitted_text:   dict[tuple, str] = {}   # optimistic baseline for delta calcs
        finalized_keys:   set[tuple]       = set()

        def _process(raw_text: str) -> str:
            corrected = corr.apply(raw_text)
            polished, _ = enhancer.enhance(corrected)
            return polished

        # ── Write queue — polling never blocks on API latency ─────────────────
        import queue as _queue_mod
        _write_q: _queue_mod.Queue = _queue_mod.Queue()

        def _writer_worker():
            """Drains write queue on its own thread. Claude + GDocs happen here."""
            _last_speaker = ""
            while True:
                job = _write_q.get()
                if job is None:          # sentinel — time to exit
                    _write_q.task_done()
                    break
                delta, speaker_label = job
                try:
                    try:
                        polished = _process(delta)
                    except Exception:
                        polished = corr.apply(delta)
                    if writer:
                        try:
                            if speaker_label != _last_speaker:
                                writer.append(polished, bold_prefix=speaker_label)
                                _last_speaker = speaker_label
                            else:
                                writer.append(polished)
                        except Exception as _we:
                            print(f"  [write failed] {_we}", flush=True)
                except Exception as _workerexc:
                    print(f"  [worker error] {_workerexc}", flush=True)
                finally:
                    _write_q.task_done()

        _write_thread = threading.Thread(
            target=_writer_worker, daemon=True, name="zs2-writer",
        )
        _write_thread.start()

        def flush(full_reset: bool = True):
            nonlocal pending_speaker, pending_ts, pending_text
            nonlocal last_update_t, first_pending_t, finalized_keys

            if not pending_speaker or not pending_text:
                return

            key    = (pending_speaker, pending_ts)
            already = submitted_text.get(key, "")
            if pending_text.startswith(already):
                delta = pending_text[len(already):].strip()
            else:
                # Zoom revised earlier words — write full current text, reset baseline
                delta = pending_text.strip()
                submitted_text[key] = ""

            if not delta:
                if full_reset:
                    pending_speaker = None
                    pending_ts      = ""
                    pending_text    = ""
                    first_pending_t = time.monotonic()
                return

            speaker_label = corr.apply(pending_speaker)
            submitted_text[key] = pending_text  # optimistic: treat as submitted
            _write_q.put((delta, speaker_label))
            self._para_count += 1
            print(f"  [queued] {speaker_label}: {delta[:60]}", flush=True)

            if full_reset:
                finalized_keys.add(key)
                pending_speaker = None
                pending_ts      = ""
                pending_text    = ""
            first_pending_t = time.monotonic()
            last_update_t   = time.monotonic()

        # ── Main polling loop ─────────────────────────────────────────────────
        try:
            for item in reader.poll_forever(skip_before=session_start_ts):
                if stop.is_set():
                    break

                corr.reload_if_changed()
                now = time.monotonic()

                if pending_speaker:
                    silence = now - last_update_t
                    age     = now - first_pending_t
                    if silence > _SILENCE_TIMEOUT:
                        print(f"  [silence timeout {silence:.1f}s → flush] {pending_speaker}", flush=True)
                        flush(full_reset=True)
                    elif age > _FLUSH_INTERVAL:
                        print(f"  [age timeout {age:.1f}s → partial flush] {pending_speaker}", flush=True)
                        flush(full_reset=False)

                if item is None:
                    continue

                if item[0] == "__disconnected__":
                    rumps.notification(
                        "ZoomScribe 2",
                        "⚠️ Transcript closed",
                        "Zoom closed the Transcript panel — trying to reopen…",
                        sound=True,
                    )
                    continue

                speaker, ts, text = item

                # Skip turns already written — ignore Zoom's post-hoc revisions.
                # But if the entry has grown beyond what we submitted, un-finalize
                # it so the new tail gets written (same-timestamp continuation speech).
                if (speaker, ts) in finalized_keys:
                    already = submitted_text.get((speaker, ts), "")
                    if len(text) <= len(already):
                        continue  # same length or shorter — correction only, skip
                    # Entry genuinely grew — remove from finalized so it flows through
                    finalized_keys.discard((speaker, ts))
                    print(f"  [un-finalized] {ts} {speaker}: +{len(text)-len(already)} chars", flush=True)

                if submitted_text.get((speaker, ts)) == text:
                    continue

                print(f"  [recv] {ts} {speaker}: {text[:60]}", flush=True)

                if pending_speaker is None:
                    print(f"  [start pending] {speaker} @ {ts}", flush=True)
                    pending_speaker = speaker
                    pending_ts      = ts
                    pending_text    = text
                    last_update_t   = now
                    first_pending_t = now

                elif (speaker, ts) == (pending_speaker, pending_ts):
                    pending_text  = text
                    last_update_t = now

                else:
                    print(f"  [speaker change → flush] {pending_speaker} → {speaker}", flush=True)
                    flush(full_reset=True)
                    pending_speaker = speaker
                    pending_ts      = ts
                    pending_text    = text
                    last_update_t   = now
                    first_pending_t = now

        except Exception as _loop_exc:
            _crash_log = Path.home() / ".zoomscribe" / "crash.log"
            try:
                _crash_log.parent.mkdir(parents=True, exist_ok=True)
                with open(_crash_log, "a", encoding="utf-8") as _f:
                    _f.write(
                        f"\n--- {datetime.datetime.now().isoformat()} ---\n"
                        f"{traceback.format_exc()}\n"
                    )
            except Exception:
                pass
            try:
                rumps.notification(
                    "ZoomScribe 2", "Session error — check crash.log",
                    str(_loop_exc)[:80], sound=True,
                )
            except Exception:
                pass

        # ── Cleanup ───────────────────────────────────────────────────────────
        flush(full_reset=True)

        # Drain the write queue fully before closing the doc
        _write_q.put(None)
        _write_q.join()

        if writer:
            writer.set_stopped()
            writer.close()

        summary = config.session_summary()
        print(f"\n  Session ended. {summary}", flush=True)
        rumps.notification("ZoomScribe 2", "Session ended", summary, sound=False)

        # Restore stdout and close log
        sys.stdout = _orig_stdout
        _log_file.close()


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
