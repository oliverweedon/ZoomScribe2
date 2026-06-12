#!/usr/bin/env python3
from __future__ import annotations

"""ZoomScribe 2 — Zoom transcript reader.

Polls Zoom's live Transcript panel via macOS Accessibility API.
Yields new utterances as (speaker, timestamp, text) tuples.

Usage:
    reader = ZoomTranscriptReader()
    for speaker, timestamp, text in reader.poll_forever():
        print(f"[{timestamp}] {speaker}: {text}")
"""

import subprocess
import time
import atomacos


class ZoomTranscriptReader:
    def __init__(self, poll_interval: float = 1.5):
        self.poll_interval = poll_interval
        self._app = None
        # Track emitted turns: list of (speaker, timestamp, text) in order
        # We re-parse all rows each tick and compare by (speaker, timestamp) key.
        # The last entry is "live" and may grow — we re-emit it when its text changes.
        self._emitted: dict[tuple, str] = {}  # (speaker, timestamp) → last emitted text
        self._order: list[tuple] = []          # ordered keys for stable iteration

    # ── Zoom app ──────────────────────────────────────────────────────────────

    def _get_app(self):
        if self._app is None:
            self._app = atomacos.getAppRefByBundleId("us.zoom.xos")
        return self._app

    def _get_zoom_app_refs(self):
        """Return atomacos app refs for ALL running Zoom-related processes.

        Zoom's Transcript panel lives in a helper process (CptHost, caphost,
        aomhost, etc.) inside the zoom.us.app bundle.  Querying only the main
        bundle ID or processes named "zoom" misses those helpers, so we also
        enumerate every process whose executable path is inside zoom.us.app.
        """
        refs = []
        seen_pids: set[int] = set()

        # Always try the cached main app reference first
        try:
            ref = self._get_app()
            pid = ref.AXPid
            seen_pids.add(pid)
            refs.append(ref)
        except Exception:
            pass

        # Collect PIDs via two strategies and de-duplicate.
        candidate_pids: list[int] = []

        # Strategy A: pgrep -il zoom (catches zoom.us, ZoomClips, …)
        try:
            out = subprocess.run(
                ["pgrep", "-il", "zoom"],
                capture_output=True, text=True,
            ).stdout
            for line in out.strip().splitlines():
                parts = line.split(None, 1)
                if parts:
                    try:
                        candidate_pids.append(int(parts[0]))
                    except ValueError:
                        pass
        except Exception:
            pass

        # Strategy B: ps aux filtered by zoom.us.app bundle path.
        # This catches CptHost, caphost, aomhost, and other helpers that
        # don't have "zoom" in their process name but live inside the bundle.
        try:
            ps_out = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True,
            ).stdout
            for line in ps_out.splitlines():
                if "/zoom.us.app/" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            candidate_pids.append(int(parts[1]))
                        except ValueError:
                            pass
        except Exception:
            pass

        for pid in candidate_pids:
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            try:
                ref = atomacos.getAppRefByPid(pid)
                refs.append(ref)
            except Exception:
                pass

        return refs

    def _log_zoom_windows(self):
        """Print what each Zoom process sees (diagnostic, called while waiting)."""
        try:
            out = subprocess.run(
                ["pgrep", "-il", "zoom"],
                capture_output=True, text=True,
            ).stdout.strip()
            print(f"  [diag] Zoom processes: {out or '(none)'}", flush=True)
        except Exception:
            pass
        for ref in self._get_zoom_app_refs():
            try:
                pid   = getattr(ref, "AXPid", "?")
                wins  = ref.windows() or []
                titles = [getattr(w, "AXTitle", "?") for w in wins]
                print(f"  [diag] PID {pid}: {len(wins)} window(s) → {titles}", flush=True)
            except Exception as e:
                print(f"  [diag] ref error: {e}", flush=True)
        # Quartz view — always runs; kCGWindowName is None without Screen Recording
        print("  [diag] Quartz: scanning...", flush=True)
        try:
            import Quartz
            wl = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly |
                Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            zoom_wins = [
                (w.get('kCGWindowOwnerPID'),
                 w.get('kCGWindowOwnerName'),
                 w.get('kCGWindowName'),
                 w.get('kCGWindowBounds'))
                for w in (wl or [])
                if 'zoom' in (w.get('kCGWindowOwnerName') or '').lower()
            ]
            print(f"  [diag] Quartz: {len(wl or [])} total windows, "
                  f"{len(zoom_wins)} zoom-owned", flush=True)
            for pid, name, title, bnds in zoom_wins:
                print(f"    PID {pid} «{name}» title={title!r} bounds={bnds}",
                      flush=True)
        except Exception as qe:
            print(f"  [diag] Quartz error: {qe}", flush=True)

    def _get_transcript_window(self):
        """Find Zoom's Transcript panel window across all Zoom processes.

        Tries every running Zoom-related process (not just the main bundle),
        because the Transcript panel may live in a helper process mid-meeting.

        Per process, two strategies:
        1. Title match  — fast; catches 'Transcript', 'Live Transcript', etc.
        2. Content match — finds any window containing the transcript AXTable.
        """
        for app_ref in self._get_zoom_app_refs():
            try:
                windows = app_ref.windows() or []

                # Strategy 1: title contains "transcript"
                for w in windows:
                    try:
                        if "transcript" in (w.AXTitle or "").lower():
                            return w
                    except Exception:
                        pass

                # Strategy 2: window contains the transcript AXTable
                for w in windows:
                    try:
                        if self._find_transcript_table(w) is not None:
                            return w
                    except Exception:
                        pass

            except Exception:
                pass

        # Strategy 3: Quartz window list — finds Transcript regardless of process
        win = self._find_window_via_quartz()
        if win is not None:
            return win

        return None

    # ── Tree search ───────────────────────────────────────────────────────────

    def _find_transcript_table(self, window):
        """Return the AXTable(desc='Transcript list') element, or None."""
        def _search(elem, depth):
            if depth > 6:
                return None
            try:
                role = getattr(elem, 'AXRole', '') or ''
                desc = (getattr(elem, 'AXDescription', '') or '').lower()
                if role == 'AXTable' and 'transcript' in desc:
                    return elem
                for child in (getattr(elem, 'AXChildren', None) or []):
                    result = _search(child, depth + 1)
                    if result:
                        return result
            except Exception:
                pass
            return None

        return _search(window, 0)

    # ── Row parser ────────────────────────────────────────────────────────────

    def _parse_table(self, table) -> list[tuple[str, str, str]]:
        """
        Parse all rows into ordered list of (speaker, timestamp, text).

        Zoom's row pattern inside AXTable:
          Speaker row:      AXCell > [AXImage, AXStaticText(name)]
          First utter. row: AXCell > [AXTextArea(HH:MM:SS), AXTextArea(text)]
          Continuation row: AXCell > [AXTextArea(text)]          ← no timestamp
          'Muted.' row:     AXCell > [AXTextArea('Muted.')]       ← skip

        Continuation rows are appended to the previous entry's text so each
        speaker turn becomes one (speaker, timestamp, joined_text) triple.
        """
        entries: list[tuple[str, str, str]] = []
        current_speaker = ""

        try:
            rows = getattr(table, 'AXChildren', None) or []
        except Exception:
            return entries

        for row in rows:
            try:
                cells = getattr(row, 'AXChildren', None) or []
                if not cells:
                    continue
                cell = cells[0]
                children = getattr(cell, 'AXChildren', None) or []

                roles_elems = []
                for child in children:
                    try:
                        roles_elems.append((getattr(child, 'AXRole', '') or '', child))
                    except Exception:
                        pass

                roles = {r for r, _ in roles_elems}

                # ── Speaker row ──────────────────────────────────────────────
                if 'AXImage' in roles and 'AXStaticText' in roles:
                    for r, e in roles_elems:
                        if r == 'AXStaticText':
                            try:
                                current_speaker = str(e.AXValue or '').strip()
                            except Exception:
                                pass
                            break
                    continue

                # ── Utterance / continuation row ─────────────────────────────
                text_areas = [(r, e) for r, e in roles_elems if r == 'AXTextArea']
                if not text_areas:
                    continue

                if len(text_areas) >= 2:
                    # First utterance row: AXTextArea(timestamp), AXTextArea(text)
                    try:
                        timestamp = str(text_areas[0][1].AXValue or '').strip()
                        text      = str(text_areas[1][1].AXValue or '').strip()
                    except Exception:
                        continue
                    if not text or text == 'Muted.':
                        continue
                    entries.append((current_speaker, timestamp, text))
                else:
                    # Continuation row
                    try:
                        text = str(text_areas[0][1].AXValue or '').strip()
                    except Exception:
                        continue
                    if not text or text == 'Muted.':
                        continue
                    if entries:
                        s, ts, prev = entries[-1]
                        entries[-1] = (s, ts, prev + ' ' + text)
                    else:
                        entries.append((current_speaker, '', text))

            except Exception:
                continue

        return entries

    def _find_window_via_quartz(self):
        """Find the Transcript window via Quartz position + AX element lookup.

        ref.windows() returns [] because zoom.us hides its floating panels from
        AXWindows. We enumerate Quartz on-screen windows (always accurate),
        filter to Zoom-owned ones, get the AX element at each window's centre
        via AXUIElementCopyElementAtPosition (bypasses AXWindows entirely),
        walk up via AXParent to the window, then verify with _find_transcript_table.

        kCGWindowName is None without Screen Recording so we cannot filter by title.
        We scan every Zoom-owned window and probe each one.
        """
        try:
            import Quartz
            from ApplicationServices import (
                AXUIElementCreateSystemWide,
                AXUIElementCopyElementAtPosition,
                kAXErrorSuccess,
            )
            options = (Quartz.kCGWindowListOptionOnScreenOnly |
                       Quartz.kCGWindowListExcludeDesktopElements)
            window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
            if not window_list:
                return None

            systemWide = AXUIElementCreateSystemWide()

            for info in window_list:
                owner = (info.get('kCGWindowOwnerName') or '').lower()
                title = (info.get('kCGWindowName') or '').lower()
                # Without Screen Recording, title is always ''. Fall back to owner filter.
                if 'zoom' not in owner and 'transcript' not in title:
                    continue

                bounds = info.get('kCGWindowBounds')
                if not bounds or bounds.get('Width', 0) < 10 or bounds.get('Height', 0) < 10:
                    continue

                # Probe window centre first, top-left area as fallback
                cx = float(bounds['X'] + bounds['Width'] / 2)
                cy = float(bounds['Y'] + bounds['Height'] / 2)
                raw_elem = None
                for px, py in [(cx, cy),
                               (float(bounds['X'] + 20), float(bounds['Y'] + 60))]:
                    err, elem = AXUIElementCopyElementAtPosition(systemWide, px, py, None)
                    if err == kAXErrorSuccess and elem is not None:
                        raw_elem = elem
                        break
                if raw_elem is None:
                    continue

                # Walk up the AX tree to the window element
                try:
                    ax = atomacos.NativeUIElement(raw_elem)
                    for _ in range(12):
                        role = getattr(ax, 'AXRole', '') or ''
                        if role == 'AXWindow':
                            if self._find_transcript_table(ax) is not None:
                                return ax
                            break  # it's a window but not the transcript — move on
                        parent = getattr(ax, 'AXParent', None)
                        if parent is None:
                            break
                        ax = parent
                except Exception:
                    pass
        except Exception:
            pass
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def _exit_fullscreen_if_needed(self) -> bool:
        """If the main Zoom meeting window is fullscreen, exit it.

        When Zoom is windowed (not fullscreen) the Transcript panel auto-docks
        to the right side of the meeting window and appears in AXWindows as a
        normal titled window — the fast, reliable detection path.

        When Zoom is fullscreen, macOS hides its windows from AXWindows entirely,
        causing all ref.windows() calls to return [] and making the Transcript
        unreachable via both AX and the Quartz position probe.

        Returns True if fullscreen was detected and exited.
        """
        try:
            for app_ref in self._get_zoom_app_refs():
                try:
                    for w in (app_ref.windows() or []):
                        try:
                            if getattr(w, 'AXFullScreen', False):
                                print("  [reader] Zoom is fullscreen — exiting so Transcript can dock…", flush=True)
                                w.AXFullScreen = False
                                time.sleep(1.5)   # let Zoom settle into windowed mode
                                return True
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def try_open_transcript_panel(self) -> bool:
        """Auto-click Zoom's Live Transcript → View Full Transcript buttons.

        Returns True if the Transcript window is open (or was already open).
        Falls back gracefully if the button cannot be found.

        Zoom uses a two-step flow:
          1. Click the 'Live Transcript' / 'CC' button in the meeting toolbar
          2. Click 'View Full Transcript' in the popup that appears
        Some Zoom versions open the panel directly after step 1.
        """
        if self._get_transcript_window():
            return True   # already open — nothing to do

        # Exit fullscreen if needed — windowed mode lets the Transcript dock
        # and become reliably accessible via AXWindows.
        self._exit_fullscreen_if_needed()

        STEP1_KW = ["live transcript", "closed caption", "captions", "caption"]
        STEP2_KW = ["full transcript", "view transcript"]
        ROLES    = ("AXButton", "AXMenuItem")

        def _find_and_press(keywords):
            try:
                app = self._get_app()
                for window in (app.windows() or []):
                    for role in ROLES:
                        try:
                            elems = window.findAll(AXRole=role) or []
                        except Exception:
                            continue
                        for elem in elems:
                            try:
                                desc  = (getattr(elem, 'AXDescription', '') or '').lower()
                                title = (getattr(elem, 'AXTitle',       '') or '').lower()
                                label = desc + " " + title
                                if any(kw in label for kw in keywords):
                                    print(f"  Auto-click: {(desc or title).strip()!r}")
                                    elem.Press()
                                    time.sleep(1.0)
                                    return True
                            except Exception:
                                pass
            except Exception:
                pass
            return False

        # Step 1 — press the Live Transcript / CC button
        if not _find_and_press(STEP1_KW):
            return False

        # The panel may open directly after step 1
        if self._get_transcript_window():
            return True

        # Step 2 — press "View Full Transcript" in the dropdown
        _find_and_press(STEP2_KW)
        time.sleep(0.5)
        return bool(self._get_transcript_window())

    def read_snapshot(self) -> list[tuple[str, str, str]]:
        """Return current full transcript as list of (speaker, timestamp, text)."""
        win = self._get_transcript_window()
        if win is None:
            return []
        table = self._find_transcript_table(win)
        if table is None:
            return []
        return self._parse_table(table)

    def poll_forever(self, skip_before: str = ""):
        """
        Yield (speaker, timestamp, text) for every new or updated utterance,
        or yield None as a heartbeat when nothing changed (lets callers check
        timeouts without a separate thread).

        On first connect (first non-empty snapshot), entries OLDER than
        skip_before are silently loaded as the historical baseline. Entries
        AT or AFTER skip_before are NOT baselined — they will be yielded
        normally on the next poll, so speech that occurred between session
        start and the first snapshot is never lost.

        The last entry in the table is often still 'live' — Zoom keeps appending
        to it as the speaker continues. We re-yield it each time its text grows.
        """
        print(f"ZoomTranscriptReader: polling every {self.poll_interval}s — waiting for Transcript window…")
        connected = False
        _diag_t = 0.0
        _reopen_t = 0.0
        _empty_streak = 0
        _RECONNECT_AFTER = 5  # ~7.5 s at 1.5 s poll interval
        while True:
            changed = False
            try:
                entries = self.read_snapshot()

                if not connected and not entries:
                    # Log what Zoom processes/windows are visible every 10s
                    _now = time.monotonic()
                    if _now - _diag_t >= 10.0:
                        _diag_t = _now
                        self._log_zoom_windows()
                    # Try to reopen the Transcript panel every 30s
                    # (_reopen_t starts at 0.0 so the first attempt fires immediately)
                    if _now - _reopen_t >= 30.0:
                        _reopen_t = _now
                        print("  [reader] attempting to reopen Transcript panel…", flush=True)
                        try:
                            self.try_open_transcript_panel()
                        except Exception as _e:
                            print(f"  [reader] reopen failed: {_e}", flush=True)

                if not connected:
                    if entries:
                        baselined = 0
                        for sp, ts, text in entries:
                            # Only baseline entries that are definitely before
                            # this session started. Entries at or after
                            # skip_before are left out of _emitted so they
                            # get yielded on the next normal poll pass.
                            if not skip_before or not ts or ts < skip_before:
                                key = (sp, ts)
                                self._emitted[key] = text
                                self._order.append(key)
                                baselined += 1
                        print(f"  Transcript connected — {baselined} historical turns skipped.")
                        connected = True
                    # Either way, yield a heartbeat and move on
                    yield None
                    time.sleep(self.poll_interval)
                    continue

                # Normal operation: yield new or updated entries only
                if not entries:
                    _empty_streak += 1
                    if _empty_streak >= _RECONNECT_AFTER:
                        print(
                            f"  [reader] Transcript window gone for {_empty_streak} polls"
                            f" (~{_empty_streak * self.poll_interval:.0f}s) — reconnecting…",
                            flush=True,
                        )
                        connected = False
                        _empty_streak = 0
                        _reopen_t = 0.0  # fire first reopen attempt immediately
                        self._emitted.clear()
                        self._order.clear()
                        yield ("__disconnected__", None, None)
                        time.sleep(self.poll_interval)
                        continue
                else:
                    _empty_streak = 0

                for speaker, timestamp, text in entries:
                    key = (speaker, timestamp)
                    prev_text = self._emitted.get(key)
                    if prev_text is None:
                        self._emitted[key] = text
                        self._order.append(key)
                        yield (speaker, timestamp, text)
                        changed = True
                    elif text != prev_text:
                        self._emitted[key] = text
                        yield (speaker, timestamp, text)
                        changed = True

            except Exception as e:
                print(f"  [reader error: {e}]")

            if not changed:
                yield None  # heartbeat — no new content this tick

            time.sleep(self.poll_interval)


# ── Standalone demo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    reader = ZoomTranscriptReader(poll_interval=1.5)
    print("Live transcript — Ctrl-C to stop\n")
    try:
        for speaker, ts, text in reader.poll_forever():
            print(f"[{ts}] {speaker}: {text}")
    except KeyboardInterrupt:
        print("\nStopped.")
