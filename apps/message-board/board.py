#!/usr/bin/env python3
"""Agent-facing client for the local message board. Stdlib + cryptography.

    board keygen --from NAME                 create this repo's signing key
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

`post` and `pin --set` sign with the repo's key when there is one:
$BOARD_KEY, else the nearest scratch/board_ed25519 above the current
directory (`board keygen` creates it). Without a key they go out unsigned,
which the server refuses for a name someone has claimed by signing. A
signature covers the board's head as its nonce, so it is good for one
write; when another write lands first, they read the head again and re-sign.

A sender name is 1-64 ASCII letters, digits, '_' and '-'; anything else is
refused here, before anything is signed or sent.

This only speaks HTTP to `board-server`; it cannot start one or touch the
database.

Config via env: BOARD_PORT (8777), BOARD_URL (endpoint, default
http://127.0.0.1:<BOARD_PORT>), BOARD_AREA / BOARD_TOPIC / BOARD_SINCE
(defaults for watch and read), BOARD_KEY (signing key path).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_sign  # noqa: E402

PORT = int(os.environ.get("BOARD_PORT", "8777"))
BASE_URL = os.environ.get("BOARD_URL", f"http://127.0.0.1:{PORT}")
KEY_FILE = Path("scratch") / "board_ed25519"
# A post's key makes resending it harmless, so a post survives a slow or
# flapping server: wait longer, then retry with the same key.
POST_TIMEOUT = 30
POST_RETRIES = 3
# Signed writes in a row that may go stale (another write landed between
# reading the head and this one arriving) before giving up.
NONCE_TRIES = 5
# The server's sender name rule (board_core.check_name), checked here too so
# that a bad name fails before anything is signed or sent.
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _client_get(path):
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return resp.read().decode("utf-8")


def _client_post(path, data, timeout=10):
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(data).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _check_name(name):
    if not NAME.fullmatch(name):
        sys.exit(f"board: sender name {ascii(name)} is not allowed: names are"
                 " 1-64 ASCII letters, digits, '_' and '-'")


def find_key():
    """The signing key to use: $BOARD_KEY, else the nearest scratch/board_ed25519
    above the current directory, else None (post unsigned)."""
    if os.environ.get("BOARD_KEY"):
        return Path(os.environ["BOARD_KEY"])
    here = Path.cwd().resolve()
    return next((d / KEY_FILE for d in (here, *here.parents) if (d / KEY_FILE).is_file()), None)


def _submit(path, kind, payload, retry=False, extra=None):
    """POST `payload`, signed if there is a key; returns the response text.

    `extra` holds signed fields that are not sent as such. The signature's
    nonce is the board's head, read just before signing. If another write
    lands before this one arrives, the board answers 409 stale-nonce, and
    this reads the head, signs and sends again (NONCE_TRIES in all).

    `retry` is for payloads with a `key` only: it also sends again after a
    timeout or a dropped connection (POST_RETRIES times, with backoff). The
    server dedupes on the key before it looks at the nonce, so a resend of a
    post that did land returns the original id instead of posting it twice.
    """
    keyfile = find_key()
    signing_key = board_sign.load_private_key(keyfile) if keyfile else None
    stale = dropped = 0
    while True:
        data = dict(payload)
        try:
            if signing_key is not None:
                data["nonce"] = str(_head())
                data["signature"] = board_sign.sign(signing_key, kind, {**data, **(extra or {})})
            return _client_post(path, data, timeout=POST_TIMEOUT if retry else 10)
        except urllib.error.HTTPError as exc:
            if exc.code != 409 or exc.headers.get("Board-Error") != "stale-nonce":
                raise  # the server answered; resending would get the same answer
            stale += 1
            if stale == NONCE_TRIES:
                sys.exit(f"board: gave up after {NONCE_TRIES} tries: each time, another"
                         " write landed between reading the board's head and this one"
                         " arriving (stale-nonce)")
        except OSError as exc:  # URLError, TimeoutError, ConnectionError
            if not retry or dropped == POST_RETRIES:
                raise
            print(f"board: post failed ({exc}); retrying", file=sys.stderr)
            time.sleep(2 ** dropped)
            dropped += 1


def cmd_keygen(args):
    """Create the repo's signing key under <repo root>/scratch/."""
    _check_name(args.sender)
    here = Path.cwd().resolve()
    root = next((d for d in (here, *here.parents)
                 if (d / ".jj").exists() or (d / ".git").exists()), None)
    if root is None:
        sys.exit("board: not inside a repository (no .jj or .git above the current directory)")
    path = root / KEY_FILE
    pub = path.with_name(path.name + ".pub")
    for p in (path, pub):
        if p.exists():
            sys.exit(f"board: {p} already exists; not overwriting it")
    path.parent.mkdir(exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():  # scratch/ is never tracked
        ignore.write_text("*\n")
    private, public, fingerprint = board_sign.generate(f"board:{args.sender}")
    for p, data in ((path, private), (pub, public.encode())):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    print(path)
    print(fingerprint)
    print(f"board: posts from inside {root} are now signed; the first signed post"
          f" as {args.sender} claims that name", file=sys.stderr)


def cmd_post(args):
    _check_name(args.sender)
    print(_submit("/post", board_sign.MESSAGE, {
        "area": args.area, "topic": args.topic,
        "from": args.sender, "body": " ".join(args.body),
        "key": uuid.uuid4().hex,
    }, retry=True))


def _who(name, verified):
    """A sender name, marked when the board verified its signature."""
    return f"{name} ✓" if verified else name


def _pin_header(p):
    where = f"{p['area']}/{p['topic']}" if p["topic"] else p["area"]
    return (f"=== pinned to {where} · v{p['version']} · {_who(p['editor'], p.get('verified'))}"
            f" · {p['ts']} ===")


def cmd_pin(args):
    """Show a pin, or with --set replace it from stdin (empty input removes it)."""
    if args.set:
        _check_name(args.sender)
        body = sys.stdin.read()
        out = json.loads(_submit("/pin", board_sign.PIN, {
            "area": args.area, "topic": args.topic or "",
            "from": args.sender, "body": body,
        }, extra={"base_version": ""}))  # no base_version sent: no stale check
        if body.strip():
            print(f"board: pin saved as v{out['version']}", file=sys.stderr)
        else:  # the board removes a pin saved empty
            print("board: pin removed", file=sys.stderr)
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
            print(f"#{m['id']}  {m['ts']}  [{m['area']}/{m['topic']}]"
                  f" {_who(m['from'], m.get('verified'))}")
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

    sk = sub.add_parser("keygen", help="create this repo's signing key (scratch/board_ed25519)")
    sk.add_argument("--from", dest="sender", required=True,
                    help="the name this key will post as (recorded in its comment)")
    sk.set_defaults(fn=cmd_keygen)

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
    except board_sign.SignatureError as exc:
        sys.exit(f"board: {exc}")
    except urllib.error.HTTPError as exc:  # a refusal, e.g. a claimed name
        try:
            reason = json.loads(exc.read())["error"]
        except (ValueError, KeyError, TypeError):
            reason = exc.reason
        sys.exit(f"board: {exc.code}: {reason}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        # A down board is an expected condition, not a crash: one line, no trace.
        sys.exit(f"board: cannot reach {BASE_URL}: {getattr(exc, 'reason', exc)}")


if __name__ == "__main__":
    main()