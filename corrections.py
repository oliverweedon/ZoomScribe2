from __future__ import annotations

"""Word/phrase corrections applied to raw Zoom transcript text.

Loads dictionaries/corrections.json (symlinked from ZoomScribe1).
Rules sorted longest-first so longer phrases match before their sub-strings.
Supports live reload — call reload_if_changed() before each use.
"""

import json
import re
from pathlib import Path

_DICT_PATH = Path(__file__).parent / "dictionaries" / "corrections.json"


class Corrections:
    def __init__(self, json_path: Path | str = _DICT_PATH):
        self._path  = Path(json_path)
        self._mtime = 0.0
        self._rules: list[tuple[re.Pattern, str]] = []
        self._load()

    def _load(self):
        with open(self._path, encoding="utf-8") as f:
            items = json.load(f)

        seen: set[str] = set()
        rules: list[tuple[re.Pattern, str]] = []

        for item in sorted(items, key=lambda x: len(x["original"]), reverse=True):
            orig = item["original"].strip()
            repl = (item.get("replacement") or orig).strip()
            if not orig:
                continue
            key = orig.lower()
            if key in seen:
                continue
            seen.add(key)

            escaped = re.escape(orig)
            pattern = re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.IGNORECASE)

            rules.append((pattern, repl))

        self._rules = rules
        self._mtime = self._path.stat().st_mtime

    def reload_if_changed(self):
        try:
            if self._path.stat().st_mtime > self._mtime:
                self._load()
                print(f"[ZoomScribe2] Dictionary reloaded — {self.rule_count} rules")
        except Exception:
            pass

    def apply(self, text: str) -> str:
        for pattern, replacement in self._rules:
            text = pattern.sub(replacement, text)
        return text

    @property
    def rule_count(self) -> int:
        return len(self._rules)
