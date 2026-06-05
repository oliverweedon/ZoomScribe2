#!/usr/bin/env python3
from __future__ import annotations

# Suppress Python 3.9 EOL / LibreSSL warnings from Google/urllib3 packages
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*urllib3.*")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

"""ZoomScribe 2 — main orchestrator.

Reads Zoom's live transcript via the macOS Accessibility API, applies the
corrections dictionary and Claude enhancement, then writes speaker-labelled
paragraphs to a Google Doc in real time.

Usage:
    python3 main.py
"""

import subprocess
import time
import webbrowser

import config
from corrections import Corrections
import enhancer
import gdocs
from zoom_reader import ZoomTranscriptReader

# After this many seconds of silence (no new text), flush the pending turn.
_SILENCE_TIMEOUT = 8

# Force-flush a long ongoing turn after this many seconds, even if the speaker
# is still actively talking.  Prevents multi-minute monologues from never writing.
_FLUSH_INTERVAL = 45


def _process(speaker: str, raw_text: str, corr: Corrections) -> str:
    """Apply corrections + Claude, return formatted paragraph."""
    speaker   = corr.apply(speaker)
    corrected = corr.apply(raw_text)
    polished, cost = enhancer.enhance(corrected)
    if cost:
        print(f"    [${cost:.5f}]", flush=True)
    return f"{speaker}: {polished}"


def main():
    print("ZoomScribe 2")
    print("=" * 40)
    print()

    session_name = input("Session name (Enter to skip GDoc): ").strip()
    use_gdoc = bool(session_name)

    corr = Corrections()
    print(f"  {corr.rule_count} correction rules loaded.")

    # ── Dictionary UI ─────────────────────────────────────────────────────────
    try:
        from dictionary_server import DictionaryServer
        _dict_srv = DictionaryServer()
        _dict_srv.start()
        _dict_srv.open_in_browser()
        print("  📖  Dictionary open at http://localhost:7878")
    except OSError:
        # Port already in use — HaSofer is running; just open the existing server
        webbrowser.open("http://localhost:7878")
        print("  📖  Dictionary open at http://localhost:7878 (HaSofer server)")
    except Exception as e:
        print(f"  ⚠️  Dictionary UI unavailable: {e}")

    # ── Google Doc setup ──────────────────────────────────────────────────────
    writer = None
    if use_gdoc:
        template_id = config.get("template_doc_id", "")
        if not template_id:
            print("  ⚠️  No template_doc_id set in ~/.zoomscribe/config.json")
            print("  Continuing without GDoc — output to terminal only.")
        else:
            print("  Creating Google Doc…", flush=True)
            try:
                doc_id, title = gdocs.copy_template(template_id, session_name)
                gdocs.fill_header(doc_id, title)
                writer = gdocs.DocsWriter(doc_id)
                print(f"  Doc: {title}\n")
            except Exception as e:
                print(f"  ⚠️  GDoc setup failed: {e}\n  Continuing in terminal-only mode.")

    # ── Transcript reader ─────────────────────────────────────────────────────
    reader = ZoomTranscriptReader(poll_interval=1.5)

    # ── Turn-buffer state ─────────────────────────────────────────────────────
    pending_speaker:  str | None = None
    pending_ts:       str        = ""
    pending_text:     str        = ""
    last_update_t:    float      = time.monotonic()
    first_pending_t:  float      = time.monotonic()

    # written_text tracks the full text we LAST wrote for each (speaker, ts) key.
    # Allows periodic partial flushes: next write sends only the delta.
    written_text: dict[tuple, str] = {}

    def flush(full_reset: bool = True):
        """Write pending content to the doc.

        full_reset=True  — speaker changed; clear pending entirely.
        full_reset=False — periodic flush mid-turn; keep accumulating.
        """
        nonlocal pending_speaker, pending_ts, pending_text
        nonlocal last_update_t, first_pending_t

        if not pending_speaker or not pending_text:
            return

        key = (pending_speaker, pending_ts)

        # Delta: only text added since the last write for this key
        already = written_text.get(key, "")
        if pending_text.startswith(already):
            delta = pending_text[len(already):].strip()
        else:
            delta = pending_text  # fallback: write everything

        if not delta:
            if full_reset:
                pending_speaker = None
                pending_ts      = ""
                pending_text    = ""
                first_pending_t = time.monotonic()
            return

        print(f"\n  [{pending_ts}] {pending_speaker}: {delta[:70]}…", flush=True)
        try:
            para = _process(pending_speaker, delta, corr)
        except Exception as e:
            print(f"  ⚠️  Processing error: {e}")
            para = f"{corr.apply(pending_speaker)}: {delta}"

        written_text[key] = pending_text  # mark full text as written

        if writer:
            writer.append(para)
        else:
            print(f"  → {para}\n", flush=True)

        if full_reset:
            pending_speaker = None
            pending_ts      = ""
            pending_text    = ""
        first_pending_t = time.monotonic()
        last_update_t   = time.monotonic()

    # ── Auto-open Zoom Transcript panel ──────────────────────────────────────
    print("  Opening Zoom Transcript panel…", flush=True)
    if reader.try_open_transcript_panel():
        print("  ✅  Transcript panel open.\n", flush=True)
    else:
        print("  ⚠️  Could not auto-open — please open it manually:", flush=True)
        print("      In the meeting → Live Transcript → View Full Transcript\n", flush=True)
        subprocess.run(
            ["osascript", "-e",
             'display notification "Click Live Transcript → View Full Transcript in your Zoom meeting" '
             'with title "ZoomScribe 2" subtitle "Open the Transcript panel now" sound name "Ping"'],
            capture_output=True,
        )

    print("Listening… (Ctrl-C to stop)\n", flush=True)

    try:
        for item in reader.poll_forever():
            corr.reload_if_changed()

            now = time.monotonic()

            # ── Timeout checks (run every tick, not just on heartbeats) ────────
            if pending_speaker:
                silence = now - last_update_t
                age     = now - first_pending_t
                if silence > _SILENCE_TIMEOUT:
                    flush(full_reset=True)
                elif age > _FLUSH_INTERVAL:
                    flush(full_reset=False)   # partial flush; keep accumulating

            if item is None:
                continue  # heartbeat — no new content this tick

            speaker, ts, text = item

            # Skip if we've already written this exact text for this key
            if written_text.get((speaker, ts)) == text:
                continue

            if pending_speaker is None:
                # Start a new pending turn
                pending_speaker = speaker
                pending_ts      = ts
                pending_text    = text
                last_update_t   = now
                first_pending_t = now

            elif (speaker, ts) == (pending_speaker, pending_ts):
                # Same turn growing
                pending_text  = text
                last_update_t = now

            else:
                # Different turn — flush previous, start new
                flush(full_reset=True)
                pending_speaker = speaker
                pending_ts      = ts
                pending_text    = text
                last_update_t   = now
                first_pending_t = now

    except KeyboardInterrupt:
        print("\n\nStopped.")
        flush(full_reset=True)

    if writer:
        writer.set_stopped()
        writer.close()

    print(config.session_summary())


if __name__ == "__main__":
    main()
