from __future__ import annotations

"""Song detector for ZoomScribe 2.

Watches the rolling stream of transcript utterances and fires when a repeated
lyric phrase is detected.  Designed to be called on every utterance that flows
through app.py's main polling loop — zero extra threads, zero extra queues.

Usage:
    detector = SongDetector(catalog)
    # In main loop:
    title = detector.feed(speaker, timestamp, text)
    if title:
        writer.append(f"Song found: {title}")
"""

import re
import time
from collections import deque

from song_catalog import SongCatalog

# Number of utterances kept in the rolling window (~60s at 1.5s poll interval)
_WINDOW_SIZE = 40

# How many distinct timestamps must contain the same n-gram to trigger detection
_REPEAT_THRESHOLD = 4

# N-gram sizes to check (words)
_NGRAM_SIZES = (4, 5, 6, 7)

# Seconds to wait before detecting another song (avoids repeated fires mid-chorus)
_COOLDOWN_SECS = 60.0


def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


class SongDetector:
    def __init__(self, catalog: SongCatalog):
        self._catalog = catalog
        # deque of (timestamp, normalised_text)
        self._window: deque[tuple[str, str]] = deque(maxlen=_WINDOW_SIZE)
        # Start as if the last detection happened well before the session began,
        # so the detector is immediately ready to fire.
        self._last_detection_t: float = -_COOLDOWN_SECS

    def feed(self, speaker: str, timestamp: str, text: str) -> str | None:
        """Add an utterance to the rolling window.

        Returns the matched song title if a song is detected, otherwise None.
        """
        norm = _normalise(text)
        self._window.append((timestamp, norm))

        # Enforce cooldown
        if time.monotonic() - self._last_detection_t < _COOLDOWN_SECS:
            return None

        candidate = self._top_repeated_ngram()
        if candidate is None:
            return None

        title = self._catalog.match(candidate)
        if title:
            self._last_detection_t = time.monotonic()
            print(
                f"  [song detected] \"{candidate}\" → {title}",
                flush=True,
            )
            return title

        # Candidate phrase repeated enough but not matched in catalog — log and skip
        print(
            f"  [song?] repeated phrase not matched in catalog: \"{candidate}\"",
            flush=True,
        )
        return None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _top_repeated_ngram(self) -> str | None:
        """Return the n-gram with the highest count of distinct timestamps, if
        that count meets the threshold. Returns None otherwise."""
        # Map ngram → set of distinct timestamps that contain it
        ngram_timestamps: dict[str, set[str]] = {}

        for ts, norm_text in self._window:
            words = norm_text.split()
            for n in _NGRAM_SIZES:
                for gram in _ngrams(words, n):
                    if gram not in ngram_timestamps:
                        ngram_timestamps[gram] = set()
                    ngram_timestamps[gram].add(ts)

        # Find the best candidate
        best_gram = None
        best_count = 0
        for gram, ts_set in ngram_timestamps.items():
            count = len(ts_set)
            # Prefer higher count; on a tie, prefer the longer n-gram — longer
            # phrases pin the catalog title more precisely and avoid accidental
            # matches against shorter, similarly-worded titles.
            if count > best_count or (
                count == best_count
                and best_gram is not None
                and len(gram.split()) > len(best_gram.split())
            ):
                best_count = count
                best_gram = gram

        if best_count >= _REPEAT_THRESHOLD:
            return best_gram
        return None
