from __future__ import annotations

"""Song catalog for ZoomScribe 2.

Loads the Firestarter song catalog from Google Drive, caches lyrics locally,
and provides fuzzy matching of a candidate phrase to a song title.

Usage:
    catalog = SongCatalog()
    catalog.load(gdocs.get_creds())          # ~2s at session start
    title = catalog.match("today's the day a holy day we honour your name")
    # → "Today's the day a holy day"
"""

import difflib
import os
import re
import threading
import time
from pathlib import Path

from googleapiclient.discovery import build

# Google Drive folder containing all song presentations
_CATALOG_FOLDER = "1z0CAXTjYUUJ756G8bIut4l-mDPPKMquu"
_CACHE_DIR = Path.home() / ".zoomscribe" / "song_cache"

# Minimum SequenceMatcher ratio to accept a title match
_TITLE_MATCH_THRESHOLD = 0.45

# Minimum number of words in a sub-phrase for lyrics first-line matching
_MIN_SUBPHRASE_WORDS = 3


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class SongCatalog:
    def __init__(self):
        # title_index: normalised_title → {"id": str, "name": str, "modifiedTime": str}
        self._title_index: dict[str, dict] = {}
        # lyrics_cache: file_id → lyrics plain text (loaded from disk)
        self._lyrics_cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._ready = False

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, creds) -> None:
        """Fetch title index from Drive, load cached lyrics from disk, trigger
        background refresh for any new/changed songs.  Blocks for ~2–3 seconds."""
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

        drive = build("drive", "v3", credentials=creds)

        # Fetch presentations from a folder (one page at a time)
        def _fetch_presentations(folder_id: str) -> list[dict]:
            results, page_token = [], None
            while True:
                resp = drive.files().list(
                    q=f"'{folder_id}' in parents and trashed=false"
                      f" and mimeType='application/vnd.google-apps.presentation'",
                    fields="nextPageToken,files(id,name,modifiedTime)",
                    pageSize=200,
                    pageToken=page_token,
                ).execute()
                results += resp.get("files", [])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            return results

        # Fetch subfolders of a folder
        def _fetch_subfolders(folder_id: str) -> list[dict]:
            resp = drive.files().list(
                q=f"'{folder_id}' in parents and trashed=false"
                  f" and mimeType='application/vnd.google-apps.folder'",
                fields="files(id,name)",
                pageSize=200,
            ).execute()
            return resp.get("files", [])

        # Recursively collect all presentations (root + one level of subfolders)
        all_files = _fetch_presentations(_CATALOG_FOLDER)
        subfolders = _fetch_subfolders(_CATALOG_FOLDER)
        for sf in subfolders:
            sub_files = _fetch_presentations(sf["id"])
            print(f"  [songs] subfolder '{sf['name']}': {len(sub_files)} songs", flush=True)
            all_files += sub_files

        print(f"  [songs] catalog: {len(all_files)} songs total ({len(subfolders)} subfolders scanned)", flush=True)

        # Build title index
        with self._lock:
            self._title_index = {
                _normalise(f["name"]): f for f in all_files
            }

        # Load all locally-cached lyrics into RAM
        self._load_lyrics_from_disk()

        # Background thread: fetch any new or changed songs
        stale = self._find_stale(all_files)
        if stale:
            print(f"  [songs] {len(stale)} song(s) missing/changed in cache — fetching in background…", flush=True)
            t = threading.Thread(
                target=self._refresh_cache_bg,
                args=(drive, stale),
                daemon=True,
                name="zs2-song-cache",
            )
            t.start()
        else:
            print(f"  [songs] lyrics cache is up to date ({len(self._lyrics_cache)} songs loaded)", flush=True)

        self._ready = True

    def match(self, phrase: str) -> str | None:
        """Return the song title best matching phrase, or None if no confident match."""
        if not self._ready:
            return None
        norm = _normalise(phrase)
        result = self._title_match(norm) or self._lyrics_first_line_match(norm)
        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_lyrics_from_disk(self) -> None:
        loaded = 0
        with self._lock:
            for path in _CACHE_DIR.glob("*.txt"):
                fid = path.stem
                try:
                    self._lyrics_cache[fid] = path.read_text(encoding="utf-8")
                    loaded += 1
                except Exception:
                    pass
        print(f"  [songs] loaded {loaded} cached lyrics from disk", flush=True)

    def _find_stale(self, all_files: list[dict]) -> list[dict]:
        """Return files that are missing from local cache or have a newer modifiedTime."""
        stale = []
        for f in all_files:
            fid = f["id"]
            cache_path = _CACHE_DIR / f"{fid}.txt"
            if not cache_path.exists():
                stale.append(f)
                continue
            # Compare Drive modifiedTime (ISO string) with local file mtime
            drive_mtime = f.get("modifiedTime", "")
            try:
                import datetime
                drive_dt = datetime.datetime.fromisoformat(drive_mtime.replace("Z", "+00:00"))
                local_mtime = datetime.datetime.fromtimestamp(
                    cache_path.stat().st_mtime,
                    tz=datetime.timezone.utc,
                )
                if drive_dt > local_mtime:
                    stale.append(f)
            except Exception:
                stale.append(f)  # if in doubt, re-fetch
        return stale

    def _refresh_cache_bg(self, drive, files: list[dict]) -> None:
        """Fetch plain-text lyrics for each file and save to cache directory."""
        fetched = 0
        for f in files:
            fid = f["id"]
            name = f["name"]
            try:
                content = drive.files().export(
                    fileId=fid, mimeType="text/plain"
                ).execute()
                lyrics = content.decode("utf-8").strip()
                cache_path = _CACHE_DIR / f"{fid}.txt"
                cache_path.write_text(lyrics, encoding="utf-8")
                with self._lock:
                    self._lyrics_cache[fid] = lyrics
                fetched += 1
                time.sleep(0.05)  # gentle pacing — don't hammer the API
            except Exception as e:
                print(f"  [songs] failed to cache '{name}': {e}", flush=True)
        print(f"  [songs] background cache refresh complete: {fetched}/{len(files)} songs fetched", flush=True)

    def _title_match(self, norm_phrase: str) -> str | None:
        """Pass 1: fuzzy match candidate phrase against all normalised titles."""
        best_ratio = 0.0
        best_name = None
        with self._lock:
            index_snapshot = dict(self._title_index)
        for norm_title, meta in index_snapshot.items():
            ratio = difflib.SequenceMatcher(None, norm_phrase, norm_title).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_name = meta["name"]
        if best_ratio >= _TITLE_MATCH_THRESHOLD:
            return best_name
        return None

    def _lyrics_first_line_match(self, norm_phrase: str) -> str | None:
        """Pass 2: check if any 3+ word sub-phrase of the candidate appears in the
        first 4 lines of any cached song's lyrics."""
        # Build sub-phrases (3-word windows) from the candidate
        words = norm_phrase.split()
        subphrases = []
        for length in range(len(words), _MIN_SUBPHRASE_WORDS - 1, -1):
            for start in range(len(words) - length + 1):
                subphrases.append(" ".join(words[start:start + length]))

        with self._lock:
            # Build a lookup: file_id → first-4-lines normalised
            first_lines = {
                fid: _normalise("\n".join(lyrics.splitlines()[:4]))
                for fid, lyrics in self._lyrics_cache.items()
            }
            id_to_name = {
                meta["id"]: meta["name"]
                for meta in self._title_index.values()
            }

        # Longest sub-phrase match wins
        for sub in subphrases:
            if len(sub.split()) < _MIN_SUBPHRASE_WORDS:
                continue
            for fid, first in first_lines.items():
                if sub in first:
                    return id_to_name.get(fid)
        return None
