#!/usr/bin/env python3
"""Phase 1 proof-of-concept: read Zoom's live transcript via Accessibility API.

Run DURING a live Zoom meeting with the Transcript panel open.
(Live Transcript button → View Full Transcript)
"""

import time, sys

try:
    import atomacos
except ImportError:
    print("❌  pip3 install atomacos"); sys.exit(1)


def find_zoom():
    try:
        return atomacos.getAppRefByBundleId("us.zoom.xos")
    except Exception as e:
        print(f"❌  Zoom not found: {e}"); return None


def dump_tree(elem, depth=0, max_depth=15, out=None):
    """Walk the full accessibility tree and collect all text."""
    if out is None:
        out = []
    if depth > max_depth:
        return out
    try:
        role  = getattr(elem, 'AXRole', '?') or '?'
        title = ""
        value = ""
        desc  = ""
        try: title = str(getattr(elem, 'AXTitle', '') or '')[:120]
        except: pass
        try: value = str(getattr(elem, 'AXValue', '') or '')[:300]
        except: pass
        try: desc  = str(getattr(elem, 'AXDescription', '') or '')[:120]
        except: pass

        indent = "  " * depth
        line = f"{indent}[{role}]"
        if title: line += f" title={title!r}"
        if value: line += f" value={value!r}"
        if desc:  line += f" desc={desc!r}"
        print(line)
        if title or value or desc:
            out.append({'role': role, 'title': title, 'value': value, 'desc': desc, 'depth': depth})

        children = []
        try: children = getattr(elem, 'AXChildren', None) or []
        except: pass
        for child in children:
            dump_tree(child, depth+1, max_depth, out)
    except Exception as e:
        print("  " * depth + f"[err: {e}]")
    return out


def find_window(app, *title_fragments):
    """Return first window whose AXTitle contains any of the fragments (case-insensitive)."""
    try:
        windows = app.windows() or []
    except:
        return None
    for w in windows:
        try:
            t = (w.AXTitle or "").lower()
            if any(f.lower() in t for f in title_fragments):
                return w
        except:
            pass
    return None


def live_poll(app, interval=1.0, duration=120):
    """Poll the Transcript window every interval seconds, printing NEW text."""
    print(f"\n─── Live poll every {interval}s for {duration}s — speak now ───\n")
    seen = set()
    end  = time.monotonic() + duration

    while time.monotonic() < end:
        # Re-find the Transcript window each tick in case it was just opened
        transcript_win = find_window(app, "transcript")
        if transcript_win is None:
            print("  (Transcript window not found — is the panel open?)")
            time.sleep(interval)
            continue

        try:
            elems = []
            for role in ("AXStaticText", "AXTextField", "AXTextArea", "AXCell", "AXGroup"):
                try: elems += list(transcript_win.findAll(AXRole=role))
                except: pass

            for elem in elems:
                for attr in ('AXValue', 'AXTitle', 'AXDescription'):
                    try:
                        val = str(getattr(elem, attr, '') or '').strip()
                        if val and len(val) > 3 and val not in seen:
                            seen.add(val)
                            role = getattr(elem, 'AXRole', '?')
                            print(f"  NEW [{role}/{attr}]: {val!r}")
                    except: pass
        except Exception as e:
            print(f"  scan error: {e}")

        time.sleep(interval)

    print(f"\nDone. {len(seen)} unique text elements seen total.")
    return seen


if __name__ == "__main__":
    print("ZoomScribe 2 — Accessibility API Test v3")
    print("Transcript panel must be open (Live Transcript → View Full Transcript)\n")

    app = find_zoom()
    if not app:
        sys.exit(1)
    print(f"✅  Zoom found\n")

    # ── Step 1: list ALL windows ──────────────────────────────────────────────
    print("─── Window list ─────────────────────────────────────────────────────\n")
    try:
        windows = app.windows() or []
        for i, w in enumerate(windows):
            try: t = w.AXTitle or "(no title)"
            except: t = "(error)"
            print(f"  Window {i}: {t!r}")
    except Exception as e:
        print(f"  Error: {e}")

    # ── Step 2: deep dump of 'Transcript' window ──────────────────────────────
    print("\n─── Deep dump of 'Transcript' window ────────────────────────────────\n")
    tw = find_window(app, "transcript")
    if tw:
        print(f"  Found: {getattr(tw, 'AXTitle', '?')!r}\n")
        results = dump_tree(tw, max_depth=15)
        print(f"\n  Total elements with text: {len(results)}")
    else:
        print("  ⚠️  Transcript window not found — open the panel first.")

    # ── Step 3: deep dump of 'Zoom Workplace' window ──────────────────────────
    print("\n─── Deep dump of 'Zoom Workplace' window ────────────────────────────\n")
    mw = find_window(app, "workplace", "meeting")
    if mw:
        print(f"  Found: {getattr(mw, 'AXTitle', '?')!r}\n")
        results = dump_tree(mw, max_depth=8)
        print(f"\n  Total elements with text: {len(results)}")
    else:
        print("  ⚠️  Zoom Workplace window not found.")

    # ── Step 4: live poll ─────────────────────────────────────────────────────
    print()
    input("\nPress Enter to start live polling of Transcript window (2 mins)...")
    live_poll(app, interval=1.0, duration=120)
