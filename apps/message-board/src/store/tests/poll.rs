//! Watching and the startup brief.

use std::thread;
use std::time::{Duration, Instant};

use super::*;

fn paths(paths: &[&str]) -> Vec<String> {
    paths.iter().map(|p| (*p).to_owned()).collect()
}

fn names(names: &[&str]) -> Vec<String> {
    paths(names)
}

/// (id, why) per event delivered.
type Deliveries = Vec<(i64, Vec<Why>)>;

/// What a poll from `since` delivers at once.
fn delivered(f: &Fixture, watch: &Watch, since: i64) -> Result<Deliveries, Box<dyn StdError>> {
    let page = f.board.poll(watch, since, Duration::ZERO, || true)?.ok_or("gave up")?;
    Ok(page.events.into_iter().map(|d| (d.event.id, d.why)).collect())
}

/// `rev` with `rev/emu` (and `rev/emu/log` in it), `rev/log`, a note
/// `rev/state`, and `rev/q`, a queue `agent` owns; `agent` and `asker`.
struct Tree {
    f: Fixture,
    agent: PrivateKey,
    asker: PrivateKey,
}

fn tree() -> Result<Tree, Box<dyn StdError>> {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    let asker = f.agent(3, "asker")?;
    f.create(&agent, "rev/emu", NodeKind::Container)?;
    f.create(&agent, "rev/emu/log", NodeKind::Thread)?;
    f.create(&agent, "rev/log", NodeKind::Thread)?;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    f.queue(&agent, "rev/q", "")?;
    Ok(Tree { f, agent, asker })
}

#[test]
fn each_subscription_on_its_own() -> TestResult {
    let Tree { f, agent, asker } = tree()?;
    let since = f.board.head()?;
    let log = f.post(&agent, "rev/log", "plain")?.id;
    let mention = f.post(&asker, "rev/emu/log", "hey @agent, and @agent-x")?.id;
    let note = f.set_note(&agent, "rev/state", "@asker is not woken by a note", 0, None)?.id;
    let child = f.create(&agent, "rev/new", NodeKind::Thread)?.id;
    let grandchild = f.create(&agent, "rev/emu/deep", NodeKind::Thread)?.id;
    let filed = f.file(&asker, "rev/q", "question")?.item;
    let answered = f.resolve(&agent, filed, ItemState::Answered, "answer")?.id;

    let only = |watch: Watch| delivered(&f, &watch, since);
    let leaf = only(Watch { paths: paths(&["rev/log"]), ..Watch::default() })?;
    assert_eq!(leaf, [(log, vec![Why::Path])]);
    // a container: its direct children's structure, never their content
    let container = only(Watch { paths: paths(&["rev"]), ..Watch::default() })?;
    assert_eq!(container, [(note, vec![Why::Child]), (child, vec![Why::Child])]);
    let inner = only(Watch { paths: paths(&["rev/emu"]), ..Watch::default() })?;
    assert_eq!(inner, [(grandchild, vec![Why::Child])]);
    let mentions = only(Watch { mentions: names(&["agent"]), ..Watch::default() })?;
    assert_eq!(mentions, [(mention, vec![Why::Mention])]);
    assert!(only(Watch { mentions: names(&["asker"]), ..Watch::default() })?.is_empty(), "note bodies are not scanned");
    let asked = only(Watch { asked_by: names(&["asker"]), ..Watch::default() })?;
    assert_eq!(asked, [(filed, vec![Why::Asked]), (answered, vec![Why::Asked])]);
    let owned = only(Watch { owners: names(&["agent"]), ..Watch::default() })?;
    assert_eq!(owned, [(filed, vec![Why::Owner]), (answered, vec![Why::Owner])]);
    let item = only(Watch { items: vec![filed], ..Watch::default() })?;
    assert_eq!(item, [(filed, vec![Why::Item]), (answered, vec![Why::Item])]);
    Ok(())
}

#[test]
fn the_root_hears_of_top_level_nodes_only() -> TestResult {
    let Tree { f, agent, .. } = tree()?;
    let since = f.board.head()?;
    let top = f.create(&f.owner, "ops", NodeKind::Container)?.id;
    f.create(&agent, "rev/below", NodeKind::Thread)?;
    f.create(&agent, "ops/log", NodeKind::Thread)?;
    let root = Watch { paths: paths(&[ROOT]), ..Watch::default() };
    assert_eq!(delivered(&f, &root, since)?, [(top, vec![Why::Child])]);
    // with the root, a container's children are still its own watchers'
    let both = Watch { paths: paths(&[ROOT, "rev"]), ..Watch::default() };
    assert_eq!(delivered(&f, &both, since)?.len(), 2);
    Ok(())
}

#[test]
fn subscriptions_combine_and_not_from_subtracts() -> TestResult {
    let Tree { f, agent, asker } = tree()?;
    let since = f.board.head()?;
    let own = f.post(&agent, "rev/log", "my own @agent")?.id;
    let theirs = f.post(&asker, "rev/log", "for @agent")?.id;
    let filed = f.file(&asker, "rev/q", "for you")?.item;
    let me = Watch {
        paths: paths(&["rev", "rev/log"]),
        mentions: names(&["agent"]),
        owners: names(&["agent"]),
        asked_by: names(&["agent"]),
        ..Watch::default()
    };
    assert_eq!(
        delivered(&f, &me, since)?,
        [(own, vec![Why::Path, Why::Mention]), (theirs, vec![Why::Path, Why::Mention]), (filed, vec![Why::Owner])]
    );
    let not_me = Watch { not_from: names(&["agent"]), ..me.clone() };
    assert_eq!(delivered(&f, &not_me, since)?, [(theirs, vec![Why::Path, Why::Mention]), (filed, vec![Why::Owner])]);
    // a registration has no actor: not_from never hides it
    let newcomer = key(5)?;
    let registration = f.register(&newcomer, "newcomer")?.item;
    let inbox = Watch { owners: names(&[OWNER]), not_from: names(&[OWNER, "agent"]), ..Watch::default() };
    assert_eq!(delivered(&f, &inbox, since)?, [(registration, vec![Why::Owner])]);
    Ok(())
}

#[test]
fn a_delivered_event_is_the_whole_event_with_its_items_state() -> TestResult {
    let Tree { f, agent, asker } = tree()?;
    let since = f.board.head()?;
    let filed = f.file(&asker, "rev/q", "question\nwith detail")?.item;
    f.resolve(&agent, filed, ItemState::Declined, "no")?;
    let page = f.board.poll(&Watch { items: vec![filed], ..Watch::default() }, since, Duration::ZERO, || true)?;
    let page = page.ok_or("gave up")?;
    let first = page.events.first().ok_or("nothing delivered")?;
    assert_eq!(first.state, Some(ItemState::Declined), "the state now, not as filed");
    assert_eq!(first.event.summary(), "question");
    assert_eq!((first.event.from.as_deref(), first.event.path.as_str()), (Some("asker"), "rev/q"));
    let json = serde_json::to_value(first)?;
    assert_eq!(json.get("event"), Some(&serde_json::json!("item_filed")));
    assert_eq!(json.get("why"), Some(&serde_json::json!(["item"])));
    assert_eq!(json.get("state"), Some(&serde_json::json!("declined")));
    Ok(())
}

#[test]
fn the_cursor_passes_what_did_not_match() -> TestResult {
    let Tree { f, agent, .. } = tree()?;
    let since = f.board.head()?;
    f.post(&agent, "rev/emu/log", "not watched")?;
    let unrelated = f.post(&agent, "rev/emu/log", "nor this")?.id;
    let watch = Watch { paths: paths(&["rev/log"]), ..Watch::default() };
    let page = f.board.poll(&watch, since, Duration::ZERO, || true)?.ok_or("gave up")?;
    assert_eq!(page.events, Vec::<Delivered>::new());
    assert_eq!(page.cursor, unrelated);
    let matched = f.post(&agent, "rev/log", "watched")?.id;
    let page = f.board.poll(&watch, page.cursor, Duration::ZERO, || true)?.ok_or("gave up")?;
    assert_eq!(page.events.iter().map(|d| d.event.id).collect::<Vec<_>>(), [matched]);
    assert_eq!(page.cursor, matched);
    // a cursor past the head stays where it was
    let ahead = f.board.poll(&watch, matched + 50, Duration::ZERO, || true)?.ok_or("gave up")?;
    assert_eq!(ahead.cursor, matched + 50);
    Ok(())
}

#[test]
fn a_watch_needs_a_subscription_and_existing_paths() -> TestResult {
    let Tree { f, .. } = tree()?;
    let poll = |watch: Watch| f.board.poll(&watch, 0, Duration::ZERO, || true);
    let err = refused(poll(Watch::default()))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("no board-wide stream")), "{err}");
    assert!(matches!(refused(poll(Watch { not_from: names(&["agent"]), ..Watch::default() }))?, Error::BadRequest(_)));
    assert!(matches!(refused(poll(Watch { paths: paths(&["rev/typo"]), ..Watch::default() }))?, Error::NotFound(_)));
    Ok(())
}

#[test]
fn a_poll_wakes_for_a_matching_write_and_sleeps_through_others() -> TestResult {
    let Tree { f, .. } = tree()?;
    let since = f.board.head()?;
    let board = Arc::clone(&f.board);
    let started = Instant::now();
    let writer = thread::spawn(move || -> Result<(i64, i64), Box<dyn StdError + Send + Sync>> {
        let agent = key(2)?;
        let post = |path, body| -> Result<i64, Error> { Ok(submit!(board, &agent, post, Post { path, body })?.id) };
        thread::sleep(Duration::from_millis(200));
        let other = post("rev/emu/log", "not for the watcher")?;
        thread::sleep(Duration::from_millis(200));
        Ok((other, post("rev/log", "woke you")?))
    });
    let watch = Watch { paths: paths(&["rev/log"]), ..Watch::default() };
    let page = f.board.poll(&watch, since, Duration::from_secs(60), || true)?.ok_or("gave up")?;
    let (other, woke) = writer.join().map_err(|_panicked| "writer panicked")?.map_err(|err| err.to_string())?;
    assert_eq!(page.events.iter().map(|d| d.event.id).collect::<Vec<_>>(), [woke]);
    assert!(other < woke);
    assert_eq!(page.cursor, woke);
    assert!(started.elapsed() < Duration::from_secs(60));
    Ok(())
}

#[test]
fn a_poll_from_ahead_of_the_head_never_goes_back() -> TestResult {
    let Tree { f, .. } = tree()?;
    let since = f.board.head()? + 1;
    let board = Arc::clone(&f.board);
    let writer = thread::spawn(move || -> Result<(i64, i64), Box<dyn StdError + Send + Sync>> {
        let agent = key(2)?;
        let post =
            |body| -> Result<i64, Error> { Ok(submit!(board, &agent, post, Post { path: "rev/log", body })?.id) };
        thread::sleep(Duration::from_millis(200));
        let at_since = post("at the cursor: seen already")?;
        thread::sleep(Duration::from_millis(200));
        Ok((at_since, post("after it")?))
    });
    let watch = Watch { paths: paths(&["rev/log"]), ..Watch::default() };
    let page = f.board.poll(&watch, since, Duration::from_secs(60), || true)?.ok_or("gave up")?;
    let (at_since, after) = writer.join().map_err(|_panicked| "writer panicked")?.map_err(|err| err.to_string())?;
    assert_eq!(at_since, since);
    assert_eq!(page.events.iter().map(|d| d.event.id).collect::<Vec<_>>(), [after]);
    Ok(())
}

#[test]
fn a_poll_gives_up_once_its_client_is_gone() -> TestResult {
    let Tree { f, .. } = tree()?;
    let started = Instant::now();
    let asked = std::cell::Cell::new(0);
    let wanted = || {
        asked.set(asked.get() + 1);
        asked.get() < 3
    };
    let watch = Watch { paths: paths(&["rev/log"]), ..Watch::default() };
    assert_eq!(f.board.poll(&watch, f.board.head()?, Duration::from_secs(300), wanted)?, None);
    assert_eq!(asked.get(), 3);
    assert!(started.elapsed() < Duration::from_secs(60), "{:?}", started.elapsed());
    Ok(())
}

#[test]
fn a_page_stops_at_500_events_or_about_a_megabyte() -> TestResult {
    let Tree { f, agent, .. } = tree()?;
    let since = f.board.head()?;
    let log = f.board.node("rev/log")?;
    // 501 messages at once, by another program: signing each would take long
    let conn = rusqlite::Connection::open(&f.path)?;
    conn.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 501)
         INSERT INTO events(ts, type, node, actor, actor_fp, body)
         SELECT '2026-10-22T00:00:00Z', 'message', ?, 'agent', ?, 'm' || i FROM n",
        rusqlite::params![log.id, agent.fingerprint()],
    )?;
    let watch = Watch { paths: paths(&["rev/log"]), ..Watch::default() };
    let page = f.board.poll(&watch, since, Duration::ZERO, || true)?.ok_or("gave up")?;
    assert_eq!(page.events.len(), 500);
    let last = page.events.last().map(|d| d.event.id).ok_or("empty")?;
    assert_eq!(page.cursor, last, "a full page's cursor is its last event");
    let rest = f.board.poll(&watch, page.cursor, Duration::ZERO, || true)?.ok_or("gave up")?;
    assert_eq!(rest.events.len(), 1);

    let since = f.board.head()?;
    let big = "x".repeat(300 * 1024);
    for _ in 0..5 {
        f.post(&agent, "rev/log", &big)?;
    }
    let page = f.board.poll(&watch, since, Duration::ZERO, || true)?.ok_or("gave up")?;
    assert_eq!(page.events.len(), 4, "the fourth reaches a megabyte");
    assert_eq!(page.cursor, page.events.last().map(|d| d.event.id).ok_or("empty")?);
    Ok(())
}

#[test]
fn a_brief_is_the_notes_on_the_path_and_the_open_items() -> TestResult {
    let Tree { f, agent, asker } = tree()?;
    f.set_note(&agent, "rev/state", "rev state", 0, None)?;
    submit!(f.board, &f.owner, create, Create { body: "the rules", ..create("rev/charter", NodeKind::Note) })?;
    f.create(&agent, "rev/emu/state", NodeKind::Note)?;
    f.set_note(&agent, "rev/emu/state", "emu state", 0, None)?;
    f.create(&agent, "rev/kb", NodeKind::Container)?;
    submit!(f.board, &agent, create, Create { body: "not on the path", ..create("rev/kb/topic", NodeKind::Note) })?;
    f.queue(&f.owner, "rev/stefan", "")?;
    f.queue(&agent, "rev/emu/aspfuzz", "")?;
    let owned = f.file(&asker, "rev/emu/aspfuzz", "for agent")?.item;
    let answered = f.file(&asker, "rev/emu/aspfuzz", "done already")?.item;
    f.resolve(&agent, answered, ItemState::Answered, "yes")?;
    let asked = f.file(&agent, "rev/stefan", "from agent")?.item;
    let own_ask = f.file(&agent, "rev/q", "owned and asked")?.item;
    let head = f.board.head()?;

    let brief = f.board.brief(&["rev/emu/log", "rev/emu/aspfuzz"], Some("agent"))?;
    assert_eq!(brief.head, head);
    let containers: Vec<&str> = brief.containers.iter().map(|c| c.path.as_str()).collect();
    assert_eq!(containers, ["rev", "rev/emu"]);
    let notes: Vec<Vec<&str>> =
        brief.containers.iter().map(|c| c.notes.iter().map(|n| n.body.as_str()).collect()).collect();
    assert_eq!(notes, [vec!["the rules", "rev state"], vec!["emu state"]]);
    let listed: Vec<&str> =
        brief.containers.first().map(|c| c.children.iter().map(|ch| ch.path.as_str()).collect()).unwrap_or_default();
    assert_eq!(listed, ["rev/charter", "rev/emu", "rev/kb", "rev/log", "rev/q", "rev/state", "rev/stefan"]);
    let ids = |items: &[Item]| items.iter().map(|i| i.id).collect::<Vec<_>>();
    assert_eq!(ids(&brief.owned), [owned, own_ask]);
    assert_eq!(ids(&brief.asked), [asked], "an item both owned and asked is listed once");
    assert_eq!(brief.watch, ["rev", "rev/emu", "rev/emu/log", "rev/emu/aspfuzz"]);

    let anonymous = f.board.brief(&["rev"], None)?;
    assert!(anonymous.owned.is_empty() && anonymous.asked.is_empty());
    assert_eq!((anonymous.containers.len(), anonymous.watch.as_slice()), (1, ["rev".to_owned()].as_slice()));
    assert!(matches!(refused(f.board.brief(&["rev/typo"], None))?, Error::NotFound(_)));
    assert!(matches!(refused(f.board.brief(&[], None))?, Error::BadRequest(_)));
    Ok(())
}
