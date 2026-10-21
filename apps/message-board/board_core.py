"""Message-board storage layer: schema, queries, pins, archive, identities, deletion.

Shared by board-server (which exposes it over HTTP) and board-admin (which
opens the database directly). Stdlib only, no HTTP, no argparse. State is a
SQLite database (WAL), so restarts re-derive the board and message ids stay
monotonic. Long-poll delivery is driven by an in-process condition variable:
a post wakes any waiting pollers immediately.

Sender names are 1-64 ASCII letters, digits, '_' and '-' (check_name): no
spaces, dots or unicode, so "Stefan " or a "Stefan" spelled with a Cyrillic
"а" is not a name at all. SERVER_NAME is the board's own, for its notes, and
is held as if claimed with no key: no client write may use it or a name that
looks like it.

Name claims: a sender name is free until a post signed as that name lands,
which binds the name to the signing key ("first use, then locked"). After
that, posts and pin edits as the name must be signed by that key. A free
name fewer than MIN_DISTANCE edits from a claimed one, ignoring case, cannot
be written as, signed or not, so a claim also keeps its look-alikes out.
A name is only an alias for its key, though: a signed pin answers to the
key that signed it (and the owner's), whatever name a later save uses.
Signature checking itself is the caller's job (board-server, via
board_sign); this module only gets the verified result as a Signer.

Replay: a signed write's nonce is the head (head_id) it was signed against,
and it is accepted only while that is still the head. Every post and pin
save appends a message and so moves the head, which makes a signature good
for one write.

Config via env: BOARD_DB (SQLite path, default ./board.db).
"""
import os
import re
import sqlite3
import threading
import time
from typing import NamedTuple

DB = os.environ.get("BOARD_DB", "board.db")

SYSTEM_AREA = "_system"

NAME_MAX = 64
_NAME = re.compile(rf"[A-Za-z0-9_-]{{1,{NAME_MAX}}}")
NAME_RULE = f"1-{NAME_MAX} ASCII letters, digits, '_' and '-'"
# How many edits (ignoring case) a free name must be from every claimed one.
MIN_DISTANCE = 3
# The sender of the board's own notes (new area, archived, ...): reserved, as
# if the server held a claim on it, so no client can write as it or near it.
SERVER_NAME = "board"

# ---------------------------------------------------------------------------
# Server core (importable + unit-testable without the HTTP layer)
# ---------------------------------------------------------------------------

# One connection, reused across request threads. Every access is made while
# holding _cv (a reentrant condition lock), which serializes DB use and lets a
# post atomically insert-then-notify without racing a poller's wait.
_cv = threading.Condition()
_db = None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(path):
    """Open (and migrate) the board database, storing it as the module global."""
    global _db
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS areas(
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            created_ts TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            area TEXT NOT NULL,
            topic TEXT NOT NULL,
            sender TEXT NOT NULL,
            body TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_area ON messages(area, id);
        CREATE TABLE IF NOT EXISTS pins(
            area TEXT NOT NULL,
            topic TEXT NOT NULL,  -- '' = the area-wide pin
            body TEXT NOT NULL,
            version INTEGER NOT NULL,
            editor TEXT NOT NULL,
            ts TEXT NOT NULL,
            PRIMARY KEY(area, topic));
        CREATE TABLE IF NOT EXISTS identities(
            name TEXT PRIMARY KEY,
            pubkey TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('tofu', 'owner')),
            registered_ts TEXT NOT NULL);
        -- Every pin save, with who signed it: the address of one pin version
        -- (versions restart at 1 after a pin is removed, so `id` is the key).
        CREATE TABLE IF NOT EXISTS pin_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area TEXT NOT NULL,
            topic TEXT NOT NULL,
            version INTEGER NOT NULL,
            body TEXT NOT NULL,
            editor TEXT NOT NULL,
            base_version INTEGER,  -- as signed; NULL = saved without a stale check
            signed_by TEXT,
            signature TEXT,
            ts TEXT NOT NULL);
        """
    )
    # Columns added after the first boards were created. SQLite cannot add a
    # UNIQUE column, so client_key's uniqueness is an index (NULLs stay distinct).
    for table, column in (("areas", "archived_ts"), ("messages", "client_key"),
                          ("messages", "signed_by"), ("messages", "signature"),
                          ("pins", "signed_by")):
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_key"
                 " ON messages(client_key)")
    conn.commit()
    _db = conn
    return conn


def rollback():
    """End the transaction a failed statement left open, so the next request
    starts clean (the caller saw a sqlite3.Error)."""
    with _cv:
        _db.rollback()


def head_id():
    """The highest message id ever issued -- not the highest still present.

    SQLite's AUTOINCREMENT already guarantees an id is never handed out
    twice, even after the newest row is deleted (board-admin can do that);
    MAX(id) would let the head fall back below ids that watchers have
    already seen, and every cursor derived from it with it. sqlite_sequence
    holds the real high-water mark.
    """
    with _cv:
        row = _db.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
        return row[0] if row else 0


def _insert(area, topic, sender, body, key=None, signer=None, signature=None):
    """Low-level append. Caller must hold _cv. Does not notify.

    `signer` marks `sender` as authenticated; `signature` is only stored when
    it signs this row's own fields (a pin doorbell has a signer, not one).
    """
    cur = _db.execute(
        "INSERT INTO messages(ts, area, topic, sender, body, client_key, signed_by, signature)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (_now(), area, topic, sender, body, key,
         signer.fingerprint if signer else None, signature),
    )
    _db.commit()
    return cur.lastrowid


class Signer(NamedTuple):
    """Who signed a write, as established by the caller's signature check."""
    pubkey: str       # "ssh-ed25519 AAAA..."
    fingerprint: str  # "SHA256:..."
    signature: str    # the SSHSIG it was verified from, base64
    nonce: str        # the head it was signed against, as signed


class Forbidden(Exception):
    """The sender may not do this: a claimed name without its key, or a
    signed pin that is not theirs."""


class Conflict(Exception):
    """A client key that was already used for a different message."""


class BadName(ValueError):
    """A sender name outside NAME_RULE."""


class StaleNonce(Exception):
    """A signed write whose nonce is not the head: a replay, or another write
    landed between the client reading the head and this one arriving."""

    def __init__(self, head):
        super().__init__(f"stale-nonce: the head is now {head}; sign again with it as the nonce")
        self.head = head


def check_name(name):
    """Raise BadName unless `name` may be written as a sender."""
    if not (isinstance(name, str) and _NAME.fullmatch(name)):
        raise BadName(f"sender name {ascii(name)} is not allowed: names are {NAME_RULE}")


def _distance(a, b):
    """Levenshtein distance: the fewest insertions, deletions and substitutions."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def near_claims(name):
    """The claimed names other than `name` fewer than MIN_DISTANCE edits from
    it, ignoring case."""
    with _cv:
        claimed = [r[0] for r in _db.execute("SELECT name FROM identities ORDER BY name")]
    return [c for c in claimed
            if c != name and _distance(name.casefold(), c.casefold()) < MIN_DISTANCE]


def _check_reserved(name):
    """Raise Forbidden if `name` is SERVER_NAME or looks like it."""
    if name == SERVER_NAME:
        raise Forbidden(f"name {name!r} is the board itself; write as your own name")
    if _distance(name.casefold(), SERVER_NAME) < MIN_DISTANCE:
        raise Forbidden(f"name {name!r} is too close to the reserved name {SERVER_NAME!r};"
                        f" use one at least {MIN_DISTANCE} edits from it")


def _check_nonce(signer):
    """Raise StaleNonce unless a signed write's nonce is the head (caller holds _cv)."""
    if signer is not None and signer.nonce != str(head_id()):
        raise StaleNonce(head_id())


def get_identity(name):
    with _cv:
        r = _db.execute("SELECT name, pubkey, fingerprint, source, registered_ts"
                        " FROM identities WHERE name=?", (name,)).fetchone()
    return None if r is None else dict(
        zip(("name", "pubkey", "fingerprint", "source", "registered_ts"), r))


def _check_claim(sender, signer):
    """Raise Forbidden unless `sender` may write as signed (or unsigned).

    A claimed name answers to its key only; a free one must not look like a
    claimed one, and nobody writes as SERVER_NAME. Returns True if the write
    would claim the name (caller holds _cv and calls _claim once nothing else
    can reject the write).
    """
    _check_reserved(sender)
    ident = get_identity(sender)
    if ident is None:
        near = near_claims(sender)
        if near:
            raise Forbidden(f"name {sender!r} is too close to the claimed name {near[0]!r};"
                            f" use one at least {MIN_DISTANCE} edits from every claimed name")
        return signer is not None
    if signer is None:
        raise Forbidden(f"name {sender!r} is claimed; sign the post")
    if signer.fingerprint != ident["fingerprint"]:
        raise Forbidden(f"name {sender!r} is claimed by key {ident['fingerprint']},"
                        f" not {signer.fingerprint}")
    return False


def _claim(sender, signer):
    """Bind a free name to its first signer (caller holds _cv; no commit)."""
    _db.execute("INSERT INTO identities VALUES(?,?,?,'tofu',?)",
                (sender, signer.pubkey, signer.fingerprint, _now()))


def _owner_fingerprint():
    """The owner's key, or None (caller holds _cv)."""
    row = _db.execute("SELECT fingerprint FROM identities WHERE source='owner'").fetchone()
    return row and row[0]


def set_owner(name, pubkey, fingerprint):
    """Make `name` the owner identity, bound to this key.

    Overrides whatever claim the name had, but never SERVER_NAME's. There is
    one owner: a previous owner identity under another name keeps its claim
    but becomes ordinary.
    """
    check_name(name)
    _check_reserved(name)
    with _cv:
        _db.execute("UPDATE identities SET source='tofu' WHERE source='owner' AND name<>?", (name,))
        ident = get_identity(name)
        if not (ident and ident["source"] == "owner" and ident["fingerprint"] == fingerprint):
            _db.execute("INSERT OR REPLACE INTO identities VALUES(?,?,?,'owner',?)",
                        (name, pubkey, fingerprint, _now()))
        _db.commit()


def _ensure_area(name, description=None):
    """Create the area if new, revive it if archived (caller holds _cv).

    Every write goes through here, so any activity in an archived area brings
    it back: a late agent can never write into an area nobody can see.
    """
    row = _db.execute(
        "SELECT description, archived_ts FROM areas WHERE name=?", (name,)).fetchone()
    if row is not None and row[1] is not None:
        _set_archived(name, False)
    if row is None:
        _db.execute(
            "INSERT INTO areas(name, description, created_ts) VALUES(?,?,?)",
            (name, description or "", _now()),
        )
        _db.commit()
        if name != SYSTEM_AREA:
            _ensure_area(SYSTEM_AREA)
            _insert(SYSTEM_AREA, "areas", SERVER_NAME,
                    f"new area: **{name}**" + (f" — {description}" if description else ""))
    elif description and not row[0]:
        _db.execute("UPDATE areas SET description=? WHERE name=?", (description, name))
        _db.commit()


def post(area, topic, sender, body, key=None, signer=None):
    """Append a message; returns its id.

    `key` makes the post idempotent: re-sending a key with the same content
    returns the original id, with different content raises Conflict.
    `signer` is the verified signer of exactly these fields, or None. Its
    nonce is checked after the key, so a resend of a signed post that did
    land returns the original id rather than StaleNonce.
    """
    check_name(sender)
    with _cv:
        if key is not None:
            row = _db.execute("SELECT id, area, topic, sender, body FROM messages"
                              " WHERE client_key=?", (key,)).fetchone()
            if row is not None:
                if row[1:] == (area, topic, sender, body):
                    return row[0]
                raise Conflict(f"key {key} was already used for a different message")
        _check_nonce(signer)
        claims = _check_claim(sender, signer)
        _ensure_area(area)
        if claims:
            _claim(sender, signer)
        mid = _insert(area, topic, sender, body, key, signer,
                      signer.signature if signer else None)
        _cv.notify_all()
    return mid


def create_area(name, description):
    with _cv:
        _ensure_area(name, description)
        _cv.notify_all()


def _set_archived(name, archived):
    """Flip an existing area's archive state and announce it (caller holds _cv)."""
    _db.execute("UPDATE areas SET archived_ts=? WHERE name=?",
                (_now() if archived else None, name))
    _db.commit()
    _insert(SYSTEM_AREA, "areas", SERVER_NAME,
            f"area {'archived' if archived else 'unarchived'}: **{name}**")


def set_archived(name, archived):
    """Archive or unarchive an area. Returns False if it already was.

    Archiving hides an area from listings and unscoped reads; nothing is
    deleted, and the next post or pin edit in the area unarchives it.
    """
    if name == SYSTEM_AREA:
        raise ValueError(f"{SYSTEM_AREA} cannot be archived")
    with _cv:
        row = _db.execute("SELECT archived_ts FROM areas WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        if (row[0] is not None) == archived:
            return False
        _set_archived(name, archived)
        _cv.notify_all()
    return True


def list_areas(include_archived=False):
    sql = "SELECT name, description, created_ts, archived_ts FROM areas"
    if not include_archived:
        sql += " WHERE archived_ts IS NULL"
    with _cv:
        rows = _db.execute(sql + " ORDER BY created_ts").fetchall()
    return [{"name": r[0], "description": r[1], "created_ts": r[2], "archived_ts": r[3]}
            for r in rows]


_LIVE = " AND area NOT IN (SELECT name FROM areas WHERE archived_ts IS NOT NULL)"


def _query(since, area, topic, limit=500, include_archived=True):
    sql = "SELECT id, ts, area, topic, sender, body, signed_by FROM messages WHERE id>?"
    args = [since]
    if not include_archived:
        sql += _LIVE
    if area:
        sql += " AND area=?"
        args.append(area)
    if topic:  # an area pin applies to every topic, so its updates do too
        sql += " AND topic IN (?, ?)"
        args += [topic, PIN_TOPIC]
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    return [
        {"id": r[0], "ts": r[1], "area": r[2], "topic": r[3],
         "from": r[4], "body": r[5], "signed_by": r[6], "verified": r[6] is not None}
        for r in _db.execute(sql, args).fetchall()
    ]


def query_since(since, area, topic):
    """Non-blocking read (used by tests and /msg)."""
    with _cv:
        return _query(since, area, topic)


def poll(since, area, topic, timeout, include_archived=True):
    deadline = time.monotonic() + timeout
    with _cv:  # query and wait under the same lock -> no lost wakeup
        while True:
            rows = _query(since, area, topic, include_archived=include_archived)
            if rows:
                return rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            _cv.wait(remaining)


def _summary(body):
    first = body.strip().splitlines()[0] if body.strip() else ""
    return first if len(first) <= 200 else first[:197] + "..."


def format_lines(msgs):
    """The watch stream: `id\t[area/topic] from: first line`, with ` ✓` after
    a verified sender."""
    return "".join(
        f"{m['id']}\t[{m['area']}/{m['topic']}] {m['from']}{' ✓' if m['verified'] else ''}:"
        f" {_summary(m['body'])}\n"
        for m in msgs
    )


def recent(area, topic, limit=100):
    """The last `limit` messages for a scope, oldest first (for the web view).

    Archived areas only show when opened by name.
    """
    with _cv:
        rows = _query(0, area, topic, limit=100000, include_archived=bool(area))
    return rows[-limit:]


# Pins: one editable note per area ('' topic) and per topic, shown above the
# log. The log itself stays append-only; every pin save is also posted to it,
# which both wakes watchers and keeps the pin's full history.
PIN_TOPIC = "_pin"  # where area-wide pin updates are announced


class StalePin(Exception):
    """The pin changed since the editor loaded it."""


def get_pin(area, topic=""):
    with _cv:
        r = _db.execute(
            "SELECT area, topic, body, version, editor, ts, signed_by FROM pins"
            " WHERE area=? AND topic=?", (area, topic)).fetchone()
    if r is None:
        return None
    pin = dict(zip(("area", "topic", "body", "version", "editor", "ts", "signed_by"), r))
    pin["verified"] = pin["signed_by"] is not None
    return pin


def pins_for(area="", topic="", include_archived=True):
    """The pins a reader of this scope should see: area-wide first, then the
    topic's own -- or every topic's, when no topic is given.

    No area means no pins, not "every area's" -- a pin is a standing
    instruction for its own workstream, and dumping every workstream's into
    an unscoped read would cross the area isolation boundary the rest of the
    board enforces.
    """
    if not area:
        return []
    sql = "SELECT area, topic FROM pins WHERE area=?"
    args = [area]
    if not include_archived:
        sql += _LIVE
    if topic:
        sql += " AND topic IN ('', ?)"
        args.append(topic)
    with _cv:
        keys = _db.execute(sql + " ORDER BY area, topic", args).fetchall()
        return [get_pin(a, t) for a, t in keys]


def set_pin(area, topic, editor, body, base_version=None, signer=None):
    """Create, replace or (empty body) remove a pin; announce it on the log.

    `base_version` is the version the editor started from; a mismatch raises
    StalePin instead of silently overwriting someone else's edit. `signer`
    is the verified signer of the save, or None. A signed pin stays signed:
    only a save signed by the same key, or by the owner's, may change or
    remove it, whatever name the save is made as.
    """
    check_name(editor)
    with _cv:
        _check_nonce(signer)
        cur = get_pin(area, topic)
        current = cur["version"] if cur else 0
        where = f"{area}/{topic}" if topic else area
        claims = _check_claim(editor, signer)
        if cur and cur["verified"] and not (
                signer and signer.fingerprint in (cur["signed_by"], _owner_fingerprint())):
            raise Forbidden(f"pin {where} v{current} is signed by key {cur['signed_by']}"
                            f" (as {cur['editor']}); only that key or the owner's can change it"
                            + ("" if signer else ", and only signed"))
        if base_version is not None and base_version != current:
            raise StalePin(current)
        if claims:
            _claim(editor, signer)
        fingerprint = signer.fingerprint if signer else None
        if not body.strip():
            _db.execute("DELETE FROM pins WHERE area=? AND topic=?", (area, topic))
            note = f"pin removed: {where}"
            version = current
        else:
            version = current + 1
            _db.execute(
                "INSERT OR REPLACE INTO pins(area, topic, body, version, editor, ts, signed_by)"
                " VALUES(?,?,?,?,?,?,?)",
                (area, topic, body, version, editor, _now(), fingerprint))
            _db.execute(
                "INSERT INTO pin_versions(area, topic, version, body, editor, base_version,"
                " signed_by, signature, ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (area, topic, version, body, editor, base_version, fingerprint,
                 signer.signature if signer else None, _now()))
            # A doorbell, not a copy: the full body used to go straight into
            # every topic's log (pins_for already shows it to a real reader of
            # the area). That meant an agent scoped to one topic saw another
            # topic's -- or the whole area's -- pin text as an ordinary
            # message, out of the context a human reading the pin box gets.
            ref = f"board pin {area} {topic}" if topic else f"board pin {area}"
            note = f"pin updated: {where} v{version} — see `{ref}`"
        _db.commit()
        _ensure_area(area)
        # A doorbell the server writes: it carries the save's signer, so the
        # editor shows as verified, but no signature of its own text.
        _insert(area, topic or PIN_TOPIC, editor, note, signer=signer)
        _cv.notify_all()
    return version


def list_topics(area):
    with _cv:
        return [r[0] for r in _db.execute(
            "SELECT topic FROM messages WHERE area=? UNION"
            " SELECT topic FROM pins WHERE area=? AND topic<>'' ORDER BY 1",
            (area, area))]



# Deletion is permanent and has no HTTP route: only board-admin calls these.

def area_counts(include_archived=True):
    """(area, archived, message_count) for every area, including empty ones."""
    areas = list_areas(include_archived=include_archived)
    with _cv:
        rows = dict(_db.execute("SELECT area, COUNT(*) FROM messages GROUP BY area"))
    return [(a["name"], bool(a["archived_ts"]), rows.get(a["name"], 0)) for a in areas]


def topic_counts(area):
    """(topic, message_count) for every topic that has messages in `area`."""
    with _cv:
        return list(_db.execute(
            "SELECT topic, COUNT(*) FROM messages WHERE area=? GROUP BY topic ORDER BY topic",
            (area,)))


def messages_in(area, topic):
    with _cv:
        return [
            {"id": r[0], "ts": r[1], "sender": r[2], "body": r[3]}
            for r in _db.execute(
                "SELECT id, ts, sender, body FROM messages WHERE area=? AND topic=? ORDER BY id",
                (area, topic))
        ]


def delete_message(mid):
    """True if a row was actually deleted."""
    with _cv:
        cur = _db.execute("DELETE FROM messages WHERE id=?", (mid,))
        _db.commit()
        return cur.rowcount > 0


def list_identities():
    with _cv:
        names = [r[0] for r in _db.execute("SELECT name FROM identities ORDER BY name")]
        return [get_identity(n) for n in names]


def delete_identity(name):
    """Release a name claim; the next signed post as `name` claims it anew.
    True if there was a claim."""
    with _cv:
        cur = _db.execute("DELETE FROM identities WHERE name=?", (name,))
        _db.commit()
        return cur.rowcount > 0


def delete_topic(area, topic):
    """Returns the number of messages deleted. Leaves `pins` untouched --
    a topic's own pin (if any) is a separate row and deletion's scope is
    messages, not pins/areas (archive already covers areas)."""
    with _cv:
        cur = _db.execute("DELETE FROM messages WHERE area=? AND topic=?", (area, topic))
        _db.commit()
        return cur.rowcount
