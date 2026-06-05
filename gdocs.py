from __future__ import annotations

"""Google Docs + Drive integration for ZoomScribe 2.

Start-of-session flow:
  1. copy_template() — duplicates the template doc and names it
  2. fill_header()   — fills in date and Hebrew date placeholders
  3. DocsWriter      — locates the "Prophecy / Message" heading and appends below it

Token stored at ~/.zoomscribe/token.pickle (shared with ZoomScribe 1).
credentials.json is symlinked from ZoomScribe 1.
"""

import os
import pickle
import subprocess
import threading
import time
import traceback
from datetime import datetime

from google.auth.transport.requests import AuthorizedSession, Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hebrew_date

_DOCS_BASE = "https://docs.googleapis.com/v1/documents"

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

_CREDS_FILE  = os.path.join(os.path.dirname(__file__), "credentials.json")
_TOKEN_FILE  = os.path.expanduser("~/.zoomscribe/token.pickle")
_MARKER      = "Prophecy / Message"
_PH_TITLE    = "<DATE & TITLE>"
_PH_HEB_DATE = "<Heb DATE>"


def _notify_error(title: str, message: str):
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Basso"'],
        capture_output=True,
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

def _auth():
    creds = None
    if os.path.exists(_TOKEN_FILE):
        with open(_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(_CREDS_FILE):
                raise FileNotFoundError(
                    "credentials.json not found.\n"
                    "See ZoomScribe setup instructions."
                )
            flow  = InstalledAppFlow.from_client_secrets_file(_CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
        with open(_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return creds


# ── Internal helpers ──────────────────────────────────────────────────────────

def _iter_tabs(doc: dict):
    for tab in doc.get("tabs", []):
        yield tab
        for child in tab.get("childTabs", []):
            yield child


def _tab_content(tab: dict) -> list:
    return tab.get("documentTab", {}).get("body", {}).get("content", [])


def _find_marker(doc: dict, marker: str) -> tuple[str | None, int | None]:
    tabs = list(_iter_tabs(doc))
    search_targets = (
        [(tab.get("tabProperties", {}).get("tabId"), _tab_content(tab)) for tab in tabs]
        if tabs
        else [(None, doc.get("body", {}).get("content", []))]
    )
    for tab_id, content in search_targets:
        for elem in content:
            if "paragraph" not in elem:
                continue
            text = "".join(
                pe.get("textRun", {}).get("content", "")
                for pe in elem["paragraph"].get("elements", [])
            )
            if marker.lower() in text.lower():
                return tab_id, elem["endIndex"]
    return None, None


# ── Public helpers ────────────────────────────────────────────────────────────

def get_creds():
    return _auth()


def format_doc_title(session_name: str) -> str:
    return f"{datetime.today().strftime('%d-%m-%Y (%a)')} - {session_name}"


def copy_template(template_id: str, session_name: str) -> tuple[str, str]:
    drive = build("drive", "v3", credentials=_auth())
    title  = format_doc_title(session_name)
    result = drive.files().copy(fileId=template_id, body={"name": title}).execute()
    return result["id"], title


def fill_header(doc_id: str, full_title: str):
    build("docs", "v1", credentials=_auth()).documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [
            {"replaceAllText": {
                "containsText": {"text": _PH_TITLE, "matchCase": False},
                "replaceText": full_title,
            }},
            {"replaceAllText": {
                "containsText": {"text": _PH_HEB_DATE, "matchCase": False},
                "replaceText": hebrew_date.today(),
            }},
        ]},
    ).execute()


# ── Writer ────────────────────────────────────────────────────────────────────

_ECHO_MAX_WORDS   = 8
_STATUS_PLACEHOLDER = "ZOOMSCRIBE_STATUS"


def _strip_para_echo(new_text: str, prev_text: str) -> str:
    if not prev_text or not new_text:
        return new_text

    def norm(w: str) -> str:
        return w.lower().strip('.,!?";:—-')

    prev_words = prev_text.split()
    new_words  = new_text.split()

    for length in range(min(_ECHO_MAX_WORDS, len(prev_words), len(new_words)), 0, -1):
        if [norm(w) for w in prev_words[-length:]] == [norm(w) for w in new_words[:length]]:
            stripped = " ".join(new_words[length:]).strip()
            return stripped if stripped else new_text

    return new_text


class DocsWriter:
    def __init__(self, doc_id: str):
        self.doc_id              = doc_id
        self._session            = AuthorizedSession(_auth())
        self._tab_id: str | None = None
        self._insert_idx: int    = 1
        self._last_para: str     = ""
        self._current_status: str  = _STATUS_PLACEHOLDER
        self._start_time           = datetime.now()
        self._para_count: int      = 0
        self._header_start: int | None = None
        self._update_lock          = threading.Lock()  # guards status text replacement
        self._api_lock             = threading.Lock()  # serialises ALL HTTP calls
        self._pulse_stop           = threading.Event()
        self._delete_thread: threading.Thread | None = None

        doc = self._call_docs(lambda: self._get_doc(includeTabsContent=True))
        tab_id, marker_end = _find_marker(doc, _MARKER)
        self._tab_id = tab_id

        if marker_end is not None:
            location = {"index": marker_end - 1}
            if tab_id:
                location["tabId"] = tab_id
            self._call_docs(lambda: self._batchUpdate([{"insertText": {"location": location, "text": "\n"}}]))
            self._insert_idx = marker_end
            print(f"  Writing to tab: {tab_id or '(root)'}")
        else:
            self._insert_idx = self._tab_end(doc, tab_id)
            print("  'Prophecy / Message' marker not found — appending at doc end.")

        self._cursor_range_id: str | None = None
        self._plant_cursor()
        self._insert_header_status()
        self._start_pulse()

    # ── Transport ─────────────────────────────────────────────────────────────

    def _get_doc(self, **params) -> dict:
        with self._api_lock:
            resp = self._session.get(f"{_DOCS_BASE}/{self.doc_id}", params=params or None)
            resp.raise_for_status()
            return resp.json()

    def _batchUpdate(self, reqs: list) -> dict:
        with self._api_lock:
            resp = self._session.post(
                f"{_DOCS_BASE}/{self.doc_id}:batchUpdate",
                json={"requests": reqs},
            )
            if not resp.ok:
                try:
                    print(f"  [batchUpdate {resp.status_code}] {resp.json()}", flush=True)
                except Exception:
                    print(f"  [batchUpdate {resp.status_code}] {resp.text[:600]}", flush=True)
            resp.raise_for_status()
            return resp.json()

    def _call_docs(self, fn):
        for attempt in range(3):
            try:
                return fn()
            except Exception as exc:
                if getattr(exc, "response", None) is not None:
                    raise
                if attempt < 2:
                    self._session = AuthorizedSession(_auth())
                    time.sleep(0.5 * (attempt + 1))
                else:
                    raise

    # ── Header status ─────────────────────────────────────────────────────────

    def _insert_header_status(self):
        doc_full = self._call_docs(lambda: self._get_doc())
        headers  = doc_full.get("headers", {})

        if headers:
            header_id   = next(iter(headers))
            header_body = headers[header_id].get("content", [])
            end_idx     = header_body[-1].get("endIndex", 2) - 1 if header_body else 1
        else:
            result    = self._call_docs(lambda: self._batchUpdate([{"createHeader": {"type": "DEFAULT"}}]))
            header_id = result["replies"][0]["createHeader"]["headerId"]
            end_idx   = 1
            header_body = []

        prev_para_start = 1
        if header_body:
            for elem in header_body:
                if elem.get("startIndex", 0) < end_idx and "paragraph" in elem:
                    prev_para_start = elem.get("startIndex", prev_para_start)

        status_len = len(_STATUS_PLACEHOLDER)
        try:
            self._call_docs(lambda: self._batchUpdate([
                {"insertText": {
                    "location": {"segmentId": header_id, "index": end_idx},
                    "text": f"{_STATUS_PLACEHOLDER}\n",
                }},
                {"updateParagraphStyle": {
                    "range": {"segmentId": header_id, "startIndex": end_idx, "endIndex": end_idx + 1},
                    "paragraphStyle": {
                        "spaceAbove": {"magnitude": 0, "unit": "PT"},
                        "spaceBelow": {"magnitude": 0, "unit": "PT"},
                        "alignment": "CENTER",
                    },
                    "fields": "spaceAbove,spaceBelow,alignment",
                }},
                {"updateTextStyle": {
                    "range": {"segmentId": header_id, "startIndex": end_idx, "endIndex": end_idx + status_len},
                    "textStyle": {
                        "bold": False,
                        "foregroundColor": {"color": {"rgbColor": {"red": 1.0, "green": 0.55, "blue": 0.0}}},
                        "fontSize": {"magnitude": 14, "unit": "PT"},
                    },
                    "fields": "bold,foregroundColor,fontSize",
                }},
            ]))
            self._header_id = header_id
            self._update_status(recording=True)
            print("  ✅  Status line inserted into page header.")
        except Exception as exc:
            print(f"  ⚠️  Could not insert into header: {exc}")

    def _start_pulse(self):
        def _loop():
            while not self._pulse_stop.wait(1.0):
                self._update_status(recording=True)
        threading.Thread(target=_loop, daemon=True, name="ZS2-pulse").start()

    def _update_status(self, recording: bool = True):
        with self._update_lock:
            self._pulse    = not getattr(self, "_pulse", False)
            dot            = "●" if self._pulse else "○"
            new_status     = f"{dot} ZoomScribe 2 running" if recording else "⏹ ZoomScribe 2 stopped"
            header_id      = getattr(self, "_header_id", None)

            requests = [{"replaceAllText": {
                "containsText": {"text": self._current_status, "matchCase": True},
                "replaceText": new_status,
            }}]

            if header_id:
                if self._header_start is None:
                    try:
                        doc    = self._call_docs(lambda: self._get_doc())
                        h_body = doc.get("headers", {}).get(header_id, {}).get("content", [])
                        for elem in h_body:
                            if "paragraph" not in elem:
                                continue
                            text = "".join(
                                pe.get("textRun", {}).get("content", "")
                                for pe in elem["paragraph"].get("elements", [])
                            )
                            if self._current_status in text:
                                self._header_start = elem["startIndex"]
                                break
                    except Exception:
                        pass

                if self._header_start is not None:
                    new_len = len(new_status.encode("utf-16-le")) // 2
                    requests.append({"updateTextStyle": {
                        "range": {
                            "segmentId":  header_id,
                            "startIndex": self._header_start,
                            "endIndex":   self._header_start + new_len,
                        },
                        "textStyle": {
                            "bold": True,
                            "foregroundColor": {"color": {"rgbColor": {
                                "red": 1.0, "green": 0.6, "blue": 0.0,
                            }}},
                        },
                        "fields": "bold,foregroundColor",
                    }})

            try:
                self._call_docs(lambda: self._batchUpdate(requests))
                self._current_status = new_status
            except Exception:
                pass

    def _delete_header_status(self):
        header_id = getattr(self, "_header_id", None)
        if not header_id:
            return
        try:
            doc    = self._call_docs(lambda: self._get_doc())
            h_body = doc.get("headers", {}).get(header_id, {}).get("content", [])
            for elem in h_body:
                if "paragraph" not in elem:
                    continue
                text = "".join(
                    pe.get("textRun", {}).get("content", "")
                    for pe in elem["paragraph"].get("elements", [])
                )
                if self._current_status in text:
                    self._call_docs(lambda: self._batchUpdate([{"deleteContentRange": {
                        "range": {
                            "segmentId":  header_id,
                            "startIndex": elem["startIndex"],
                            "endIndex":   elem["endIndex"],
                        },
                    }}]))
                    break
        except Exception:
            pass

    def set_stopped(self):
        self._pulse_stop.set()
        self._update_status(recording=False)

        def _delayed_delete():
            time.sleep(30)
            self._delete_header_status()

        self._delete_thread = threading.Thread(
            target=_delayed_delete, daemon=False, name="ZS2-delete"
        )
        self._delete_thread.start()

    def close(self):
        if self._delete_thread and self._delete_thread.is_alive():
            self._delete_thread.join()

    # ── Cursor ────────────────────────────────────────────────────────────────

    _CURSOR_NAME = "zoomscribe2_cursor"

    def _tab_end(self, doc: dict, tab_id: str | None) -> int:
        if tab_id:
            for tab in _iter_tabs(doc):
                if tab.get("tabProperties", {}).get("tabId") == tab_id:
                    content = _tab_content(tab)
                    if content:
                        return content[-1]["endIndex"] - 1
        return doc["body"]["content"][-1]["endIndex"] - 1

    def _plant_cursor(self) -> None:
        reqs = []
        if self._cursor_range_id:
            reqs.append({"deleteNamedRange": {"namedRangeId": self._cursor_range_id}})
        r = {"startIndex": self._insert_idx, "endIndex": self._insert_idx + 1}
        if self._tab_id:
            r["tabId"] = self._tab_id
        reqs.append({"createNamedRange": {"name": self._CURSOR_NAME, "range": r}})
        try:
            resp = self._call_docs(lambda: self._batchUpdate(reqs))
            replies = (resp or {}).get("replies", [])
            for reply in replies:
                if "createNamedRange" in reply:
                    self._cursor_range_id = reply["createNamedRange"]["namedRangeId"]
        except Exception:
            pass

    def _read_cursor(self) -> None:
        try:
            params = {"fields": "namedRanges"}
            resp = self._session.get(f"{_DOCS_BASE}/{self.doc_id}", params=params)
            resp.raise_for_status()
            data = resp.json()
            entry = data.get("namedRanges", {}).get(self._CURSOR_NAME)
            if entry:
                nr = entry.get("namedRanges", [])
                if nr and nr[0].get("ranges"):
                    self._insert_idx = nr[0]["ranges"][0]["startIndex"]
                    self._cursor_range_id = nr[0]["namedRangeId"]
        except Exception:
            pass

    # ── Write ─────────────────────────────────────────────────────────────────

    def _write(self, location: dict, style_range: dict, line: str, char_count: int,
               bold_prefix_len: int = 0):
        # Sync position from the named-range cursor before writing.
        # This keeps _insert_idx correct even when the user co-edits the doc.
        self._read_cursor()
        start = self._insert_idx
        location = {"index": start}
        if self._tab_id:
            location["tabId"] = self._tab_id
        text_count = char_count - 1
        style_range = {"startIndex": start, "endIndex": start + text_count}
        if self._tab_id:
            style_range["tabId"] = self._tab_id

        def _range(s, e):
            r = {"startIndex": s, "endIndex": e}
            if self._tab_id:
                r["tabId"] = self._tab_id
            return r

        def _text_style(rng, bold: bool):
            return {"updateTextStyle": {
                "range": rng,
                "textStyle": {
                    "bold":   bold,
                    "italic": False,
                    "weightedFontFamily": {"fontFamily": "Arial"},
                    "fontSize": {"magnitude": 14, "unit": "PT"},
                    "foregroundColor": {
                        "color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}
                    },
                },
                "fields": "bold,italic,weightedFontFamily,fontSize,foregroundColor",
            }}

        ops = [
            {"insertText": {"location": location, "text": line}},
            {"updateParagraphStyle": {
                "range": style_range,
                "paragraphStyle": {
                    "namedStyleType": "NORMAL_TEXT",
                    "spaceAbove": {"magnitude": 0, "unit": "PT"},
                    "spaceBelow": {"magnitude": 0, "unit": "PT"},
                },
                "fields": "namedStyleType,spaceAbove,spaceBelow",
            }},
        ]

        if bold_prefix_len and bold_prefix_len < text_count:
            # Bold speaker name, normal body text
            ops.append(_text_style(_range(start, start + bold_prefix_len), bold=True))
            ops.append(_text_style(_range(start + bold_prefix_len, start + text_count), bold=False))
        else:
            ops.append(_text_style(style_range, bold=False))

        self._call_docs(lambda: self._batchUpdate(ops))
        self._insert_idx += char_count

    def append(self, text: str, bold_prefix: str = ""):
        """Write one paragraph (Normal Text, Arial 14pt).

        If bold_prefix is given it is written in bold at the start of the
        paragraph (e.g. the speaker name), followed by a colon and a space,
        then the normal-weight paragraph text.
        """
        if not text:
            return
        self._last_para = text

        if bold_prefix:
            full_text  = f"{bold_prefix}: {text}"
            prefix_len = len(f"{bold_prefix}: ".encode("utf-16-le")) // 2
        else:
            full_text  = text
            prefix_len = 0

        line       = full_text + "\n\n"
        char_count = len(line.encode("utf-16-le")) // 2
        text_count = len(full_text.encode("utf-16-le")) // 2
        start      = self._insert_idx

        location = {"index": start}
        if self._tab_id:
            location["tabId"] = self._tab_id
        style_range = {"startIndex": start, "endIndex": start + text_count}
        if self._tab_id:
            style_range["tabId"] = self._tab_id

        try:
            self._write(location, style_range, line, char_count,
                        bold_prefix_len=prefix_len)
            self._para_count += 1
            print(f"    ✓ Written to doc (para {self._para_count})", flush=True)
        except Exception as exc:
            exc_detail = getattr(getattr(exc, 'response', None), 'text', str(exc))
            if "must be less than the end index" in exc_detail:
                print("  ↻  Index drift — resyncing…")
                try:
                    doc = self._call_docs(lambda: self._get_doc(includeTabsContent=True))
                    self._insert_idx = self._tab_end(doc, self._tab_id)
                    start       = self._insert_idx
                    location    = {"index": start}
                    if self._tab_id:
                        location["tabId"] = self._tab_id
                    style_range = {"startIndex": start, "endIndex": start + text_count}
                    if self._tab_id:
                        style_range["tabId"] = self._tab_id
                    self._write(location, style_range, line, char_count)
                    self._para_count += 1
                    print(f"    ✓ Written to doc after resync (para {self._para_count})", flush=True)
                    print("  ✅  Resynced.")
                except Exception:
                    print("  Write failed after resync:")
                    traceback.print_exc()
                    _notify_error("ZoomScribe 2 — Write Failed", "Could not write to Google Doc.")
            else:
                print("  Write to Google Doc failed:")
                traceback.print_exc()
                _notify_error("ZoomScribe 2 — Write Failed", "Could not write to Google Doc.")
