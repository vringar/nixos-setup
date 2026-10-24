"""Message deeplinks in the web view: `GET /m/<id>` sends the browser to the
page that shows the event, highlighted by the anchor `#m<id>`, and an id
mentioned in a body is a link to it when the board has such an event.
See board_harness."""

import html
import re

import pytest

from board_harness import board, isolated, keyring, team  # noqa: F401 (fixtures)

pytestmark = pytest.mark.usefixtures("isolated")


def goes_to(board, eid):
    """Where `/m/<eid>` sends the browser (path, query, anchor)."""
    r = board.get(f"/m/{eid}")
    assert r.status == 302, (r.status, r.text)
    path, _, anchor = r.headers["Location"].partition("#")
    path, _, query = path.partition("?")
    return path, query, anchor


def mentions(page):
    """The ids the page links as mentions inside bodies, with their titles."""
    return re.findall(r'<a class="mref" hx-boost="false" href="/m/(\d+)" title="([^"]*)">', page)


def test_a_message_resolves_to_its_thread_and_is_anchored_on_the_page(board, team):
    mid = team.alice.post("rev/log", "hello")
    assert goes_to(board, mid) == ("/view/rev/log", "", f"m{mid}")
    page = board.get("/view/rev/log").text
    assert f'<div class="msg" id="m{mid}">' in page
    # the visible id is its own permalink
    assert f'href="/m/{mid}" title="permalink">#{mid}</a>' in page
    assert ":target" in page and "var(--accent-bg)" in page


def test_an_old_message_gets_a_window_around_it_that_the_page_serves(board, team):
    ids = [team.alice.post("rev/log", f"message {n}") for n in range(130)]
    old, new = ids[25], ids[-1]
    assert goes_to(board, new) == ("/view/rev/log", "", f"m{new}")
    path, query, anchor = goes_to(board, old)
    assert (path, anchor) == ("/view/rev/log", f"m{old}")
    since = int(re.fullmatch(r"since=(\d+)", query).group(1))
    assert since == ids[5] - 1  # 20 messages of context before it
    page = board.get(f"{path}?{query}").text
    assert f'id="m{old}"' in page and f'id="m{new}"' in page
    assert f'id="m{ids[4]}"' not in page and f'id="m{ids[5]}"' in page
    assert "earlier messages are not shown" in page
    # near the start, the window is everything
    assert goes_to(board, ids[3])[1] == "since=0"
    # the window's own refresh keeps it, so the anchor stays on the page
    assert f"&amp;since={since}" in page


def test_an_archived_message_resolves_and_shows_as_archived(board, team):
    mid = team.alice.post("rev/log", "noise")
    team.owner.archive(mid, "off topic")
    path, query, anchor = goes_to(board, mid)
    assert (path, query, anchor) == ("/view/rev/log", "archived=1", f"m{mid}")
    page = board.get(f"{path}?{query}").text
    assert f'<div class="msg archived" id="m{mid}">' in page and 'class="tag">archived' in page


def test_the_archive_event_resolves_to_what_it_archived(board, team):
    mid = team.alice.post("rev/log", "noise")
    event = team.owner.archive(mid)["id"]
    assert goes_to(board, event) == ("/view/rev/log", "archived=1", f"m{mid}")


def test_a_note_version_resolves_to_that_version(board, team):
    team.alice.create("rev/state", "note", body="v1 text")
    v2 = team.alice.set_note("rev/state", "v2 text", base_version=1)
    [(first,)] = board.sql("SELECT id FROM events WHERE type='note_version' AND version=1 AND node=?",
                           board.node_id("rev/state"))
    assert goes_to(board, v2["id"]) == ("/view/rev/state", "version=2", f"m{v2['id']}")
    assert goes_to(board, first) == ("/view/rev/state", "version=1", f"m{first}")
    page = board.get("/view/rev/state?version=1").text
    assert f'<div class="note" id="m{first}">' in page and f'href="/m/{first}"' in page


def test_items_resolve_to_their_queue_open_or_resolved_and_the_resolution_to_its_item(board, team):
    team.bob.create("rev/bob", "queue")
    open_item = team.alice.ask("rev/bob", "still open")
    done = team.alice.ask("rev/bob", "finished")
    answer = team.bob.answer(done, "all done")["id"]
    assert goes_to(board, open_item) == ("/view/rev/bob", f"focus={open_item}", f"m{open_item}")
    assert goes_to(board, done) == ("/view/rev/bob", f"focus={done}", f"m{done}")
    assert goes_to(board, answer) == ("/view/rev/bob", f"focus={done}", f"m{done}")
    page = board.get(f"/view/rev/bob?focus={done}").text
    assert f'<div class="item" id="m{open_item}">' in page and f'<div class="item" id="m{done}">' in page
    # a resolved item is folded away, but not the one the link is for
    assert "<details open><summary>1 resolved item" in page
    assert "<details><summary>1 resolved item" in board.get("/view/rev/bob").text
    # and the refresh keeps it that way
    assert f"focus={done}" in page
    assert "<details open>" in board.get(f"/ui/live/queue?path=rev/bob&focus={done}").text
    # the resolution's id is a permalink too
    assert f'href="/m/{answer}" title="permalink">#{answer}</a>' in page


def test_an_archived_item_resolves_with_the_archived_view(board, team):
    team.bob.create("rev/bob", "queue")
    item = team.alice.ask("rev/bob", "gone")
    team.owner.archive(item)
    assert goes_to(board, item) == ("/view/rev/bob", f"archived=1&focus={item}", f"m{item}")


def test_other_events_go_to_their_node_and_an_unknown_id_is_a_404_page(board, team):
    created = board.sql("SELECT min(id) FROM events WHERE node=?", board.node_id("rev/log"))[0][0]
    assert goes_to(board, created) == ("/view/rev/log", "", "")
    for bad in ("99999", "1", "abc", "10x", "-10001", "+10001", "0"):
        r = board.get(f"/m/{bad}")
        assert r.status == 404, (bad, r.status)
        assert r.headers["Board-Error"] == "not-found"
        assert "<html" in r.text and "no event" in r.text, bad
    assert board.get("/m/").status == 404


def test_a_valid_id_in_a_body_is_a_link_and_other_numbers_stay_text(board, team):
    target = team.alice.post("rev/log", "the target\nsecond line")
    gone = target + 90_000
    body = f"see {target}, (#{target}) and {gone} or 12345 or v1.{target}.2 `{target}` {target}x"
    team.bob.post("rev/log", body)
    page = board.get("/view/rev/log").text
    found = mentions(page)
    assert [i for i, _ in found] == [str(target)] * 2
    assert all(html.unescape(t) == "message in rev/log: the target" for _, t in found)
    shown = re.sub(r"<[^>]+>", "", page)
    for plain in (str(gone), "12345", f"v1.{target}.2", f"`{target}`", f"{target}x"):
        assert plain in shown
    # the one lookup also covers queues and notes
    team.alice.create("rev/state", "note", body=f"as of {target}")
    team.bob.create("rev/bob", "queue")
    item = team.alice.ask("rev/bob", f"re {target}")
    team.bob.answer(item, f"answered, see {item}")
    assert [i for i, _ in mentions(board.get("/view/rev/state").text)] == [str(target)]
    assert [i for i, _ in mentions(board.get("/view/rev/bob").text)] == [str(target), str(item)]
    assert [i for i, _ in mentions(board.get("/ui/messages?path=rev/log").text)] == [str(target)] * 2


def test_an_id_is_no_link_in_a_url_a_blob_reference_or_a_hash(board, team):
    target = team.alice.post("rev/log", "target")
    hashed = team.alice.put(b"some bytes")["hash"]
    body = (f"http://host/x/{target} https://h/?id={target} board-blob://{hashed} "
            f"commit abc{target} {target}def sha {hashed}")
    team.bob.post("rev/log", body)
    assert mentions(board.get("/view/rev/log").text) == []


def test_an_archived_target_is_linked_without_giving_its_text_away(board, team):
    secret = team.alice.post("rev/log", "private words")
    team.owner.archive(secret)
    team.bob.post("rev/log", f"look at {secret}")
    page = board.get("/view/rev/log").text
    ((eid, title),) = mentions(page)
    assert eid == str(secret) and "private" not in title


def test_what_a_body_says_cannot_become_markup_through_the_link(board, team):
    evil = '"><script>alert(1)</script>'
    mid = team.alice.post("rev/log", evil)
    team.bob.post("rev/log", f"see {mid}")
    page = board.get("/view/rev/log").text
    assert "<script>alert(1)" not in page
    ((_, title),) = mentions(page)
    assert "<" not in title and '"' not in title and "&lt;script&gt;" in title
    for p in ("/view/rev/log", f"/view/rev/log?since={mid - 1}"):
        assert "script-src 'self'" in board.get(p).headers["Content-Security-Policy"]
