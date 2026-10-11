#!/usr/bin/env python3
"""Agent-facing client for the local message board. Stdlib only.

    board post <area> <topic> <from> <body>  post a markdown message
    board watch [--area A] [--topic T]       long-poll -> one line per message
                [--since ID|head]            'head' = only messages from now on
    board read [--area A] [--topic T]        one-shot: pins + full backlog, then exit
    board pin <area> [topic] [--set]         show a pinned note, or replace it from stdin
    board areas [--archived]                 list areas
    board archive|unarchive <area>           hide/restore an area (nothing is deleted)

The address is two levels:

    area   -- a whole workstream (e.g. "reversing", "openwpm"). The isolation
              boundary: a watcher subscribed to one area never sees another.
    topic  -- a channel within an area (e.g. "ghidra", "fuzz", "crashes").

This only speaks HTTP to `board-server`; it cannot start one or touch the
database.

Config via env: BOARD_PORT (8777), BOARD_URL (endpoint, default
http://127.0.0.1:<BOARD_PORT>), BOARD_AREA / BOARD_TOPIC / BOARD_SINCE
(defaults for watch and read).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

PORT = int(os.environ.get("BOARD_PORT", "8777"))
BASE_URL = os.environ.get("BOARD_URL", f"http://127.0.0.1:{PORT}")


def _client_get(path):
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return resp.read().decode("utf-8")


def _client_post(path, data):
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(data).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def cmd_post(args):
    print(_client_post("/post", {
        "area": args.area, "topic": args.topic,
        "from": args.sender, "body": " ".join(args.body),
    }))


def _pin_header(p):
    where = f"{p['area']}/{p['topic']}" if p["topic"] else p["area"]
    return f"=== pinned to {where} · v{p['version']} · {p['editor']} · {p['ts']} ==="


def cmd_pin(args):
    """Show a pin, or with --set replace it from stdin (empty input removes it)."""
    if args.set:
        out = json.loads(_client_post("/pin", {
            "area": args.area, "topic": args.topic or "",
            "from": args.sender, "body": sys.stdin.read(),
        }))
        print(f"board: pin saved as v{out['version']}", file=sys.stderr)
        return
    try:
        pin = json.loads(_client_get(f"/pin?{urlencode({'area': args.area, 'topic': args.topic or ''})}"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        sys.exit(f"board: no pin on {args.area}{'/' + args.topic if args.topic else ''}")
    print(_pin_header(pin))
    print(pin["body"].rstrip())


def read_all(since, area, topic, archived=False):
    """Every message after `since`, oldest first, paging past the poll cap."""
    out = []
    while True:
        params = urlencode({"since": since, "area": area, "topic": topic,
                            "timeout": 0, "archived": "1" if archived else ""})
        page = json.loads(_client_get(f"/poll?{params}"))
        if not page:
            return out
        out.extend(page)
        since = page[-1]["id"]


def cmd_read(args):
    """One-shot backlog read: full bodies for context, not doorbell lines.

    Ends by reporting a resume cursor on stderr. The head is taken *before*
    reading, so `watch --since <cursor>` misses nothing posted after the read
    and repeats nothing the read already showed.
    """
    if not args.area:
        # No silent "every area merged": area is the isolation boundary, and
        # an unscoped read is exactly how an unrelated agent's first action
        # used to pull in another workstream's pinned instructions.
        sys.exit("board: no --area given; an unscoped read shows nothing. "
                 "See `board areas`, then `board read --area <area>`.")
    if not args.archived:
        archived = {a["name"] for a in json.loads(_client_get("/areas?archived=1"))
                    if a["archived_ts"]}
        if args.area in archived:  # say so, rather than print an empty read
            sys.exit(f"board: area {args.area} is archived; add --archived to read it")
    head = _head()  # one-shot: a down board fails fast instead of retrying
    since = head if args.since == "head" else args.since
    scope = urlencode({"area": args.area or "", "topic": args.topic or "",
                       "archived": "1" if args.archived else ""})
    pins = json.loads(_client_get(f"/pins?{scope}"))
    msgs = read_all(since, args.area or "", args.topic or "", args.archived)
    cursor = max([head] + [m["id"] for m in msgs])
    if args.json:
        print(json.dumps({"pins": pins, "messages": msgs}, indent=2))
    else:
        for p in pins:  # standing instructions come before the log
            print(_pin_header(p))
            print(p["body"].rstrip())
            print()
        for m in msgs:
            print(f"#{m['id']}  {m['ts']}  [{m['area']}/{m['topic']}] {m['from']}")
            print(m["body"].rstrip())
            print()
    print(f"board: read through id {cursor}; follow with --since {cursor}",
          file=sys.stderr)


def cmd_areas(args):
    for a in json.loads(_client_get(f"/areas?archived={'1' if args.archived else ''}")):
        desc = f" — {a['description']}" if a["description"] else ""
        mark = f"  [archived {a['archived_ts']}]" if a["archived_ts"] else ""
        print(f"{a['name']}{desc}  ({a['created_ts']}){mark}")


def cmd_archive(args):
    try:
        out = json.loads(_client_post("/archive", {"area": args.area,
                                                   "archived": args.archived}))
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 404):
            raise
        sys.exit(f"board: {json.loads(exc.read())['error']}: {args.area}")
    state = "archived" if args.archived else "unarchived"
    print(f"board: {args.area} {state}" if out["changed"]
          else f"board: {args.area} was already {state}", file=sys.stderr)


def _since_arg(value):
    """--since takes a message id, or 'head' for "only what happens next"."""
    if value == "head":
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a message id or 'head'")


def _head():
    """The board's current head id (one request, no retry)."""
    return int(json.loads(_client_get("/healthz"))["head"])


def _resolve_since(value):
    """Turn 'head' into the board's current head id, for a watcher.

    Retries rather than failing: a persistent watcher is routinely started
    before the service is up, and dying there would look like "no news".
    """
    if value != "head":
        return value
    while True:
        try:
            return _head()
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print(f"board-unreachable: {exc}", flush=True)
            time.sleep(2)


def cmd_watch(args):
    """Long-poll and print one line per new message: a Monitor event stream."""
    since = _resolve_since(args.since)
    while True:
        params = urlencode({
            "since": since, "area": args.area or "", "topic": args.topic or "",
            "timeout": args.timeout, "format": "lines",
        })
        try:
            with urllib.request.urlopen(
                f"{BASE_URL}/poll?{params}", timeout=args.timeout + 10
            ) as resp:
                text = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            # Emit on failure too: silence must never look like "no news"
            # (Monitor coverage rule). Back off so a down server doesn't spin.
            print(f"board-unreachable: {exc}", flush=True)
            time.sleep(2)
            continue
        lines = text.splitlines()
        if not lines:
            continue  # long-poll returned empty: no new messages
        for line in lines:
            print(line, flush=True)  # one line per message -> one event each
        since = int(lines[-1].split("\t", 1)[0])


def main(argv=None):
    p = argparse.ArgumentParser(prog="board", description="local long-poll message board")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("post", help="post a markdown message")
    sp.add_argument("area")
    sp.add_argument("topic")
    sp.add_argument("sender", metavar="from")
    sp.add_argument("body", nargs="+")
    sp.set_defaults(fn=cmd_post)

    sw = sub.add_parser("watch", help="long-poll one area/topic (Monitor stream)")
    sw.add_argument("--area", default=os.environ.get("BOARD_AREA", ""))
    sw.add_argument("--topic", default=os.environ.get("BOARD_TOPIC", ""))
    sw.add_argument("--since", type=_since_arg, metavar="ID|head",
                    default=os.environ.get("BOARD_SINCE", "0"),
                    help="start after this message id, or 'head' for only new messages")
    sw.add_argument("--timeout", type=int, default=50)
    sw.set_defaults(fn=cmd_watch)

    sr = sub.add_parser("read", help="print the full backlog of an area/topic and exit")
    sr.add_argument("--area", default=os.environ.get("BOARD_AREA", ""))
    sr.add_argument("--topic", default=os.environ.get("BOARD_TOPIC", ""))
    sr.add_argument("--since", type=_since_arg, metavar="ID|head", default="0",
                    help="start after this message id (default: everything)")
    sr.add_argument("--json", action="store_true", help="emit a JSON object")
    sr.add_argument("--archived", action="store_true",
                    help="include archived areas (needed to read one by name)")
    sr.set_defaults(fn=cmd_read)

    spin = sub.add_parser("pin", help="show an area/topic pin, or --set it from stdin")
    spin.add_argument("area")
    spin.add_argument("topic", nargs="?", default="",
                      help="omit for the area-wide pin")
    spin.add_argument("--set", action="store_true",
                      help="replace the pin with stdin (empty stdin removes it)")
    spin.add_argument("--from", dest="sender", default=os.environ.get("USER", "anon"))
    spin.set_defaults(fn=cmd_pin)

    sa = sub.add_parser("areas", help="list areas")
    sa.add_argument("--archived", action="store_true", help="include archived areas")
    sa.set_defaults(fn=cmd_areas)

    for name, archived, text in (
            ("archive", True, "hide an area from listings and unscoped reads"),
            ("unarchive", False, "bring an archived area back")):
        sx = sub.add_parser(name, help=text)
        sx.add_argument("area")
        sx.set_defaults(fn=cmd_archive, archived=archived)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except urllib.error.URLError as exc:
        # A down board is an expected condition, not a crash: one line, no trace.
        sys.exit(f"board: cannot reach {BASE_URL}: {exc.reason}")


if __name__ == "__main__":
    main()