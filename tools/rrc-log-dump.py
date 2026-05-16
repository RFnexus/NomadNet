#!/usr/bin/env python3
"""Dump the contents of rrc on-disk room history logs.

Usage:
    rrc-log-dump.py <path>

<path> may be:
  - a single .log file
  - a hub directory under rrc_history/
  - the rrc_history/ root (or any ancestor; .log files are found recursively)
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "nomadnet", "vendor"))
import cbor


H_KIND    = "k"
H_SRC     = "s"
H_NICK    = "n"
H_TEXT    = "t"
H_TS      = "ts"
H_MENTION = "m"

KIND_COLOR = {
    "msg":    "",
    "system": "\033[2m",
    "notice": "\033[36m",
    "error":  "\033[31m",
}
RESET = "\033[0m"


def _fmt_ts(ts_ms):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts_ms) // 1000))
    except Exception:
        return "????-??-?? ??:??:??"


def _fmt_speaker(entry):
    nick = entry.get(H_NICK)
    src  = entry.get(H_SRC)
    src_hex = src.hex()[:12] if isinstance(src, (bytes, bytearray)) else None
    if nick and src_hex:
        return "%s (%s)" % (nick, src_hex)
    if nick:
        return nick
    if src_hex:
        return src_hex
    return ""


def dump_file(path, use_color):
    print("=== %s ===" % path)
    count = 0
    truncated = False
    with open(path, "rb") as f:
        while True:
            try:
                entry = cbor.load(f)
            except EOFError:
                break
            except Exception as ex:
                truncated = True
                print("  [corrupt record after entry %d: %s]" % (count, ex), file=sys.stderr)
                break
            if not isinstance(entry, dict):
                print("  [skipping non-dict entry: %r]" % (entry,), file=sys.stderr)
                continue
            kind    = entry.get(H_KIND) or "msg"
            ts      = _fmt_ts(entry.get(H_TS, 0))
            speaker = _fmt_speaker(entry)
            text    = entry.get(H_TEXT) or ""
            mention = " *" if entry.get(H_MENTION) else "  "
            color   = KIND_COLOR.get(kind, "") if use_color else ""
            end     = RESET if (color and use_color) else ""
            line = "%s  %-6s%s  %-32s  %s" % (ts, kind, mention, speaker, text)
            print(color + line + end)
            count += 1
    suffix = " (truncated)" if truncated else ""
    print("--- %d entries%s ---" % (count, suffix))


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 2
    target = argv[1]
    use_color = sys.stdout.isatty()
    if os.path.isfile(target):
        dump_file(target, use_color)
        return 0
    if os.path.isdir(target):
        files = []
        for root, _dirs, names in os.walk(target):
            for name in names:
                if name.endswith(".log"):
                    files.append(os.path.join(root, name))
        files.sort()
        if not files:
            print("no .log files found under %s" % target, file=sys.stderr)
            return 1
        for i, path in enumerate(files):
            if i > 0:
                print()
            dump_file(path, use_color)
        return 0
    print("not a file or directory: %s" % target, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
