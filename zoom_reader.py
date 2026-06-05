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

    def _get_transcript_window(self):
        try:
            for w in (self._get_app().windows() or []):
                try:
                    if (w.AXTitle or "").lower() == "transcript":
                        return w
                except Exception:
                    pass
        except Exception:
            self._app = None
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

    # ── Public API ────────────────────────────────────────────────────────────

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

    def poll_forever(self):
        """
        Yield (speaker, timestamp, text) for every new or updated utterance,
        or yield None as a heartbeat when nothing changed (lets callers check
        timeouts without a separate thread).

        On first connect (first non-empty snapshot), ALL existing entries are
        silently loaded as the historical baseline — nothing is yielded for them.
        Only turns that appear AFTER that baseline are yielded. This prevents
        flooding the caller with a full meeting backlog on startup.

        The last entry in the table is often still 'live' — Zoom keeps appending
        to it as the speaker continues. We re-yield it each time its text grows.
        """
        print(f"ZoomTranscriptReader: polling every {self.poll_interval}s — waiting for Transcript window…")
        connected = False
        while True:
            changed = False
            try:
                entries = self.read_snapshot()

                if not connected:
                    if entries:
                        # First non-empty snapshot — treat everything as historical baseline
                        for sp, ts, text in entries:
                            key = (sp, ts)
                            self._emitted[key] = text
                            self._order.append(key)
                        print(f"  Transcript connected — {len(entries)} historical turns skipped.")
                        connected = True
                    # Either way, yield a heartbeat and move on — nothing to emit yet
                    yield None
                    time.sleep(self.poll_interval)
                    continue

                # Normal operation: yield new or updated entries only
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
