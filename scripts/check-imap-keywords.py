"""Report whether Paperless's mail tags survive on the IMAP server.

Paperless's "tag" action sets a custom IMAP keyword on the message. Custom
keywords are optional in RFC 3501: a server may accept the STORE, return OK,
and never persist it. That failure is silent from Paperless's side, because the
mail is recorded as processed either way.

Two independent checks are made, because either alone can mislead:

  * the folder's PERMANENTFLAGS, which is the server's own statement about
    whether clients may create keywords (the ``\\*`` entry), and
  * the flags actually on messages Paperless claims to have tagged, taken from
    its ProcessedMail rows. This is the real evidence; some servers advertise
    ``\\*`` and still drop keywords in particular folders.

This reads the SQLite database directly rather than going through
paperless-manage, whose generated wrapper re-invokes sudo in a way that does
not work on this host. Root can read the database; nothing else is needed.

    sudo python3 scripts/check-imap-keywords.py
    sudo python3 scripts/check-imap-keywords.py --write

Read-only by default: folders are opened with EXAMINE, so nothing is written
and no message is marked read. --write additionally round-trips a throwaway
keyword on the newest message, which is the definitive test when Paperless has
not tagged anything yet. The account password is used but never printed.

A keyword present here but invisible in Thunderbird is a Thunderbird problem,
not a server one: it only renders keywords that have a matching tag definition,
the pref mailnews.tags.<keyword>.tag.
"""

import argparse
import imaplib
import sqlite3
from pathlib import Path

DEFAULT_DB = "/var/lib/paperless/db.sqlite3"
PROBE_KEYWORD = "paperless-keyword-probe"
SAMPLE = 10

# paperless_mail.models.MailRule.MailAction / MailAccount.ImapSecurity.
ACTION_TAG = 5
SECURITY_NONE = 1
SECURITY_SSL = 2
SECURITY_STARTTLS = 3


def text(value):
    """imaplib hands back bytes, lists of bytes, and None interchangeably."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(text(item) for item in value)
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def assess_permanentflags(perm):
    """Turn a PERMANENTFLAGS line into (supported, explanation).

    None means the server said nothing useful, which is not the same as no.
    """
    if not perm:
        return None, "no PERMANENTFLAGS advertised - the server commits to nothing"
    if "\\*" in perm:
        return True, "\\* present - clients may create new keywords"
    return False, "\\* absent - only the listed flags persist; keywords get dropped"


def default_port(account):
    """imap_port is nullable in the schema, and None reaches imaplib as a crash."""
    if account["imap_port"]:
        return account["imap_port"], None
    port = 993 if account["imap_security"] == SECURITY_SSL else 143
    return port, f"imap_port is unset in the database; assuming {port}"


def usable_uid(mail, uidvalidity):
    """A stored uid only means anything while UIDVALIDITY is unchanged."""
    return not mail["uid_validity"] or mail["uid_validity"] == uidvalidity


def parse_flag_lines(lines):
    """Map uid -> raw FLAGS line from an untagged UID FETCH response."""
    found = {}
    for item in lines:
        line = text(item)
        parts = line.replace("(", " ").replace(")", " ").split()
        if "UID" in parts:
            index = parts.index("UID")
            if index + 1 < len(parts):
                found[parts[index + 1]] = line
    return found


def connect(db_path):
    """Open the live database without writing to it."""
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        pass
    try:
        # A WAL database can refuse read-only opens when it needs recovery.
        return sqlite3.connect(db_path)
    except sqlite3.OperationalError as exc:
        if not Path(db_path).exists():
            raise SystemExit(f"no database at {db_path} - pass --db") from exc
        raise SystemExit(f"cannot read {db_path} ({exc}); run this as root") from exc


def rows(connection, table, where="", params=()):
    connection.row_factory = sqlite3.Row
    names = {r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'",
    )}
    if table not in names:
        found = sorted(n for n in names if n.startswith("paperless_mail"))
        raise SystemExit(f"no table {table!r}; paperless_mail tables present: {found}")
    sql = f"SELECT * FROM {table} {where}"  # noqa: S608 - table name is a literal
    return [dict(r) for r in connection.execute(sql, params)]


def load_tag_rules(connection):
    """Rules using the tag action, each with its account inlined."""
    accounts = {a["id"]: a for a in rows(connection, "paperless_mail_mailaccount")}
    tagged = rows(
        connection,
        "paperless_mail_mailrule",
        "WHERE action = ?",
        (ACTION_TAG,),
    )
    for rule in tagged:
        rule["account"] = accounts[rule["account_id"]]
    return tagged


def processed_for(connection, rule_id):
    return rows(
        connection,
        "paperless_mail_processedmail",
        "WHERE rule_id = ? AND status = 'SUCCESS' ORDER BY processed DESC LIMIT ?",
        (rule_id, SAMPLE),
    )


def open_mailbox(account):
    """Mirror Paperless's own connection handling, including OAuth accounts."""
    port, warning = default_port(account)
    server = account["imap_server"]
    security = account["imap_security"]
    if security == SECURITY_NONE:
        client = imaplib.IMAP4(server, port)
    elif security == SECURITY_STARTTLS:
        client = imaplib.IMAP4(server, port)
        client.starttls()
    else:
        client = imaplib.IMAP4_SSL(server, port)

    user, secret = account["username"], account["password"]
    if account["is_token"]:
        auth = f"user={user}\1auth=Bearer {secret}\1\1"
        client.authenticate("XOAUTH2", lambda _: auth.encode())
    else:
        client.login(user, secret)
    return client, warning


def select(client, folder, *, writable):
    typ, data = client.select(folder, not writable)
    if typ != "OK":
        raise RuntimeError(f"cannot open {folder!r}: {text(data)}")
    untagged = client.untagged_responses
    count = text(data)
    return {
        "count": int(count) if count.isdigit() else 0,
        "flags": text(untagged.get("FLAGS")),
        "permanentflags": text(untagged.get("PERMANENTFLAGS")),
        "uidvalidity": text(untagged.get("UIDVALIDITY")),
    }


def fetch_flags(client, uids):
    if not uids:
        return {}
    typ, data = client.uid("FETCH", ",".join(uids), "(FLAGS)")
    return parse_flag_lines(data) if typ == "OK" else {}


def report_processed(client, connection, rule, keyword, uidvalidity):
    """Cross-check Paperless's own record against the flags now on the server."""
    mails = processed_for(connection, rule["id"])
    if not mails:
        print("    Paperless has not tagged anything for this rule yet, so there")
        print("    is nothing to cross-check. Re-run with --write.")
        return

    fresh = [m for m in mails if usable_uid(m, uidvalidity)]
    if len(fresh) < len(mails):
        print(f"    note: {len(mails) - len(fresh)}/{len(mails)} rows predate a")
        print("    UIDVALIDITY change; their uids no longer name the same messages")

    flags = fetch_flags(client, [m["uid"] for m in fresh])
    tagged = missing = gone = 0
    for mail in fresh:
        line = flags.get(mail["uid"])
        if line is None:
            gone += 1
        elif keyword.lower() in line.lower():
            tagged += 1
        else:
            missing += 1
            print(f"    untagged on server: uid {mail['uid']} {mail['subject']!r}")
    print(
        f"    Of the last {len(fresh)} processed successfully: {tagged} carry "
        f"{keyword!r}, {missing} do not, {gone} no longer exist",
    )
    if missing and not tagged:
        print("    => Paperless set the keyword and the server discarded it")
    elif tagged and not missing:
        print("    => the keyword is on the server; Thunderbird is hiding it")


def write_probe(client, count):
    """Set and immediately clear a throwaway keyword on the newest message."""
    if not count:
        print("    folder is empty - nothing to probe")
        return
    typ, data = client.uid("SEARCH", None, "ALL")
    uids = text(data).split()
    if typ != "OK" or not uids:
        print("    could not find a message to probe")
        return
    uid = uids[-1]
    client.uid("STORE", uid, "+FLAGS", f"({PROBE_KEYWORD})")
    after = text(fetch_flags(client, [uid]).get(uid))
    client.uid("STORE", uid, "-FLAGS", f"({PROBE_KEYWORD})")
    kept = PROBE_KEYWORD.lower() in after.lower()
    print(f"    round trip on uid {uid}: {'KEPT' if kept else 'DROPPED'}")
    print(f"      flags after STORE: {after}")


def check_rule(connection, rule, *, writable):
    keyword = rule["action_parameter"] or ""
    account = rule["account"]
    state = "enabled" if rule["enabled"] else "DISABLED"
    print(f"\nRule {rule['name']!r} [{state}]")
    print(f"  account {account['name']} ({account['username']}@{account['imap_server']})")
    print(f"  folder {rule['folder']!r}, tag {keyword!r}")

    if keyword.lower().startswith("apple:"):
        print("  Apple Mail colour tag: this sets \\Flagged plus $MailFlagBits, not")
        print("  a custom keyword. Thunderbird renders it as a plain star.")
        return

    client, warning = open_mailbox(account)
    if warning:
        print(f"  WARNING: {warning}")
    try:
        if "X-GM-EXT-1" in client.capabilities:
            print("  Gmail: Paperless uses X-GM-LABELS here, not an IMAP keyword. The")
            print("  tag becomes a label, which Thunderbird shows as a folder under")
            print("  [Gmail] rather than as a tag.")

        info = select(client, rule["folder"], writable=writable)
        print(f"  {info['count']} messages, UIDVALIDITY {info['uidvalidity']}")
        print(f"  FLAGS:          {info['flags'] or '(none)'}")
        print(f"  PERMANENTFLAGS: {info['permanentflags'] or '(none)'}")
        supported, why = assess_permanentflags(info["permanentflags"])
        print(f"  => {why}")

        typ, data = client.uid("SEARCH", None, "KEYWORD", keyword)
        if typ != "OK":
            print(f"  SEARCH KEYWORD {keyword} failed - the server rejects the keyword")
        else:
            print(f"  messages carrying {keyword!r}: {len(text(data).split())}")

        report_processed(client, connection, rule, keyword, info["uidvalidity"])

        if writable:
            write_probe(client, info["count"])
        elif supported is not True:
            print("    (re-run with --write to settle it by round trip)")
    finally:
        try:
            client.logout()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB, help=f"default {DEFAULT_DB}")
    parser.add_argument(
        "--write",
        action="store_true",
        help="also round-trip a throwaway keyword on the newest message",
    )
    args = parser.parse_args()

    connection = connect(args.db)
    rules = load_tag_rules(connection)
    print(f"Rules using the tag action: {len(rules)}")
    if not rules:
        print("None, so no rule sets an IMAP keyword and no tag can appear anywhere.")
    for rule in rules:
        try:
            check_rule(connection, rule, writable=args.write)
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
