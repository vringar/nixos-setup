//! Archiving a message or an item: the owner's, reversible, and hidden from
//! every read and watch unless asked for.

use super::*;

/// `rev` with a thread `rev/log` and a queue `rev/q` that `agent` owns; an
/// `asker` and the owner (`f.owner`).
struct Setup {
    f: Fixture,
    agent: PrivateKey,
    asker: PrivateKey,
}

fn setup() -> Result<Setup, Box<dyn StdError>> {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    let asker = f.agent(3, "asker")?;
    f.create(&agent, "rev/log", NodeKind::Thread)?;
    f.queue(&agent, "rev/q", "")?;
    Ok(Setup { f, agent, asker })
}

fn ids(page: &ThreadPage) -> Vec<i64> {
    page.messages.iter().map(|m| m.id).collect()
}

#[test]
fn an_archived_message_leaves_the_reads_and_comes_back() -> TestResult {
    let Setup { f, agent, .. } = setup()?;
    let keep = f.post(&agent, "rev/log", "keep")?.id;
    let noise = f.post(&agent, "rev/log", "noise")?.id;
    let last = f.post(&agent, "rev/log", "last")?.id;

    let done = f.archive(&f.owner, noise, true, "off topic")?;
    assert!(done.hidden && done.event == noise);
    let archive_event = done.id.ok_or("no archive event")?;
    assert!(archive_event > last, "the archive is an event of its own, after everything");

    assert_eq!(ids(&f.board.thread("rev/log", Range::All)?), [keep, last]);
    assert_eq!(ids(&f.board.thread("rev/log", Range::Last(2))?), [keep, last]);
    assert_eq!(ids(&f.board.thread("rev/log", Range::Since(keep))?), [last]);
    // the count of earlier messages leaves it out too
    let tail = f.board.thread("rev/log", Range::Last(1))?;
    assert_eq!((ids(&tail), tail.earlier), ([last].to_vec(), 1));
    let shown = f.board.thread_with("rev/log", Range::All, true)?;
    assert_eq!(ids(&shown), [keep, noise, last]);
    assert_eq!(shown.messages.iter().map(|m| m.archived).collect::<Vec<_>>(), [false, true, false]);

    // the listing counts what is visible
    let children = f.board.children("rev")?;
    let Some(Status::Thread { messages, last_id, .. }) =
        children.iter().find(|c| c.path == "rev/log").map(|c| c.status.clone())
    else {
        return Err("no thread child".into());
    };
    assert_eq!((messages, last_id), (2, Some(last)));

    // show still finds it, marked
    let event = f.board.event(noise)?.ok_or("gone")?;
    assert!(event.archived && event.body == "noise");
    let archive = f.board.event(archive_event)?.ok_or("gone")?;
    assert_eq!(
        (archive.event, archive.reference, archive.body.as_str()),
        (EventType::NodeArchived, Some(noise), "off topic")
    );
    assert_eq!(archive.summary(), format!("archived message #{noise} in rev/log"));
    assert!(!archive.archived);

    let undone = f.archive(&f.owner, noise, false, "")?;
    assert!(!undone.hidden);
    assert!(undone.id.is_some_and(|id| id > archive_event));
    assert_eq!(ids(&f.board.thread("rev/log", Range::All)?), [keep, noise, last]);
    assert!(!f.board.event(noise)?.ok_or("gone")?.archived);
    Ok(())
}

#[test]
fn archiving_is_idempotent_and_writes_nothing_when_nothing_changes() -> TestResult {
    let Setup { f, agent, .. } = setup()?;
    let message = f.post(&agent, "rev/log", "hi")?.id;

    let before = f.board.head()?;
    let unchanged = f.archive(&f.owner, message, false, "")?;
    assert_eq!((unchanged.id, unchanged.event, unchanged.hidden), (None, message, false));
    assert_eq!(f.board.head()?, before, "unarchiving what is not archived writes nothing");

    let first = f.archive(&f.owner, message, true, "a")?;
    let head = f.board.head()?;
    let again = f.archive(&f.owner, message, true, "b")?;
    assert_eq!(again, first, "the archive that holds is reported");
    assert_eq!(f.board.head()?, head);

    let off = f.archive(&f.owner, message, false, "")?;
    let head = f.board.head()?;
    assert_eq!(f.archive(&f.owner, message, false, "")?, off);
    assert_eq!(f.board.head()?, head);
    Ok(())
}

#[test]
fn a_resend_returns_the_first_result() -> TestResult {
    let Setup { f, agent, .. } = setup()?;
    let message = f.post(&agent, "rev/log", "hi")?.id;
    let action = Archive { event: message, archived: true, reason: "" };
    let first = Request::keyed(&f.board, &f.owner, action, "same-key")?;
    let done = f.board.archive(&first.signed())?;
    let resent = Request::keyed(&f.board, &f.owner, action, "same-key")?;
    assert_eq!(f.board.archive(&resent.signed())?, done);
    Ok(())
}

#[test]
fn only_the_owner_key_archives_and_what_is_not_archivable_is_refused() -> TestResult {
    let Setup { f, agent, asker } = setup()?;
    let message = f.post(&agent, "rev/log", "hi")?.id;
    let item = f.file(&asker, "rev/q", "q")?.item;
    let answer = f.resolve(&agent, item, ItemState::Answered, "a")?.id;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    let version = f.set_note(&agent, "rev/state", "text", 0, None)?.id;

    for who in [&agent, &asker] {
        let err = refused(f.archive(who, message, true, ""))?;
        assert!(matches!(err, Error::Forbidden(_)), "{err}");
        assert!(matches!(refused(f.archive(who, message, false, ""))?, Error::Forbidden(_)));
        assert!(matches!(refused(f.archive(who, 99_999, true, ""))?, Error::Forbidden(_)), "no existence leak");
    }
    assert!(!f.board.event(message)?.ok_or("gone")?.archived);

    let err = refused(f.archive(&f.owner, 99_999, true, ""))?;
    assert!(matches!(err, Error::NotFound(_)), "{err}");
    // not a note version, a resolution (which names its item) or a node's creation
    let err = refused(f.archive(&f.owner, version, true, ""))?;
    assert!(matches!(err, Error::BadRequest(_)) && err.to_string().contains("only messages and queue items"), "{err}");
    let err = refused(f.archive(&f.owner, answer, true, ""))?;
    assert!(err.to_string().contains(&format!("archive the item (#{item})")), "{err}");
    let node = f.board.node("rev/log")?.created;
    assert!(matches!(refused(f.archive(&f.owner, node, true, ""))?, Error::BadRequest(_)));
    // a registration is the owner's inbox, not archivable
    let registration = f.register(&key(9)?, "newcomer")?.item;
    let err = refused(f.archive(&f.owner, registration, true, ""))?;
    assert!(err.to_string().contains("registration"), "{err}");
    // a reason is at most 200 bytes
    let long = "x".repeat(ARCHIVE_REASON_MAX + 1);
    assert!(matches!(refused(f.archive(&f.owner, message, true, &long))?, Error::BadRequest(_)));
    f.archive(&f.owner, message, true, &"x".repeat(ARCHIVE_REASON_MAX))?;
    Ok(())
}

#[test]
fn archiving_an_item_hides_the_filing_and_its_resolution() -> TestResult {
    let Setup { f, agent, asker } = setup()?;
    let open = f.file(&asker, "rev/q", "still open")?.item;
    let done = f.file(&asker, "rev/q", "answered")?.item;
    let answer = f.resolve(&agent, done, ItemState::Answered, "the answer")?.id;
    let unseen = ItemQuery { owner: Some("agent"), state: StateFilter::All, ..ItemQuery::default() };

    f.archive(&f.owner, open, true, "")?;
    f.archive(&f.owner, done, true, "")?;

    assert_eq!(f.board.queue("rev/q", StateFilter::All, 0)?.items.len(), 0);
    assert_eq!(f.board.items(&unseen)?.items.len(), 0);
    assert_eq!(f.board.items(&ItemQuery { owner: Some("agent"), ..ItemQuery::default() })?.items.len(), 0);
    let all = f.board.queue_with("rev/q", StateFilter::All, 0, true)?.items;
    assert_eq!(all.iter().map(|i| (i.id, i.archived)).collect::<Vec<_>>(), [(open, true), (done, true)]);
    assert_eq!(all.get(1).and_then(|i| i.resolution.as_ref()).map(|r| r.id), Some(answer));
    assert_eq!(f.board.items(&ItemQuery { archived: true, ..unseen })?.items.len(), 2);

    // the queue's open count, and the brief's owned and asked items
    let Some(Status::Queue { open: count, .. }) =
        f.board.children("rev")?.iter().find(|c| c.path == "rev/q").map(|c| c.status.clone())
    else {
        return Err("no queue child".into());
    };
    assert_eq!(count, 0);
    let brief = f.board.brief(&["rev/q"], Some("agent"))?;
    assert_eq!(brief.owned.len(), 0);
    assert_eq!(f.board.brief(&["rev/q"], Some("asker"))?.asked.len(), 0);

    // by id it is still there, marked, and so is its resolution
    let detail = f.board.item(done)?.ok_or("gone")?;
    assert!(detail.item.archived);
    assert!(f.board.event(done)?.ok_or("gone")?.archived);
    assert!(f.board.event(answer)?.ok_or("gone")?.archived, "a resolution is archived with its item");

    f.archive(&f.owner, done, false, "")?;
    let back = f.board.queue("rev/q", StateFilter::All, 0)?.items;
    assert_eq!(back.iter().map(|i| (i.id, i.archived)).collect::<Vec<_>>(), [(done, false)]);
    assert!(!f.board.event(answer)?.ok_or("gone")?.archived);
    assert_eq!(f.board.brief(&["rev/q"], Some("agent"))?.owned.len(), 0, "the open one is still archived");
    f.archive(&f.owner, open, false, "")?;
    assert_eq!(f.board.brief(&["rev/q"], Some("agent"))?.owned.len(), 1);
    Ok(())
}

#[test]
fn an_archived_item_or_message_refuses_writes() -> TestResult {
    let Setup { f, agent, asker } = setup()?;
    let item = f.file(&asker, "rev/q", "q")?.item;
    f.archive(&f.owner, item, true, "")?;
    for (who, state) in [(&agent, ItemState::Answered), (&f.owner, ItemState::Declined), (&asker, ItemState::Cancelled)]
    {
        let err = refused(f.resolve(who, item, state, "x"))?;
        assert!(matches!(err, Error::Archived { id } if id == item), "{err}");
        assert_eq!((err.code(), err.status()), ("archived", 409));
    }
    assert_eq!(f.board.item(item)?.ok_or("gone")?.item.state, ItemState::Open);
    f.archive(&f.owner, item, false, "")?;
    f.resolve(&agent, item, ItemState::Answered, "now")?;

    // an answer's ref names a visible message
    let second = f.file(&asker, "rev/q", "again")?.item;
    let message = f.post(&agent, "rev/log", "details")?.id;
    f.archive(&f.owner, message, true, "")?;
    let reply = Resolve { item: second, state: ItemState::Answered, body: "see", reference: Some(message) };
    let err = refused(submit!(f.board, &agent, resolve, reply))?;
    assert!(matches!(err, Error::Archived { id } if id == message), "{err}");
    Ok(())
}

#[test]
fn a_watch_does_not_announce_what_is_archived_and_hears_the_archive() -> TestResult {
    let Setup { f, agent, asker } = setup()?;
    f.create(&agent, "rev/emu", NodeKind::Container)?;
    f.create(&agent, "rev/emu/log", NodeKind::Thread)?;
    let since = f.board.head()?;
    let seen = f.post(&agent, "rev/log", "seen")?.id;
    let hidden = f.post(&agent, "rev/log", "never announced @asker")?.id;
    let filed = f.file(&asker, "rev/q", "q")?.item;
    let answered = f.resolve(&agent, filed, ItemState::Answered, "a")?.id;
    let archive_message = f.archive(&f.owner, hidden, true, "noise")?.id.ok_or("no event")?;
    let archive_item = f.archive(&f.owner, filed, true, "")?.id.ok_or("no event")?;
    let inner = f.post(&agent, "rev/emu/log", "inner")?.id;
    let archive_inner = f.archive(&f.owner, inner, true, "")?.id.ok_or("no event")?;

    let watch = |watch: Watch| -> Result<Vec<i64>, Box<dyn StdError>> {
        let page = f.board.poll(&watch, since, Duration::ZERO, || true)?.ok_or("gave up")?;
        Ok(page.events.into_iter().map(|d| d.event.id).collect())
    };
    let leaf = |path: &str| Watch { paths: vec![path.to_owned()], ..Watch::default() };
    assert_eq!(watch(leaf("rev/log"))?, [seen, archive_message]);
    assert_eq!(watch(leaf("rev/q"))?, [archive_item], "neither the filing nor the answer");
    assert_eq!(watch(Watch { mentions: vec!["asker".to_owned()], ..Watch::default() })?, Vec::<i64>::new());
    assert_eq!(watch(Watch { asked_by: vec!["asker".to_owned()], ..Watch::default() })?, [archive_item]);
    assert_eq!(watch(Watch { owners: vec!["agent".to_owned()], ..Watch::default() })?, [archive_item]);
    assert_eq!(watch(Watch { items: vec![filed], ..Watch::default() })?, [archive_item]);
    // a container's watcher never heard of a message, so is not told it left
    assert_eq!(watch(leaf("rev"))?.len() + watch(leaf("rev/emu"))?.len(), 0);
    assert_eq!(watch(leaf("rev/emu/log"))?, [archive_inner]);
    let _ = answered;

    // the doorbell is for the thread, names its target, and unarchiving does
    // not announce the message again to a watcher that is past it
    let page = f.board.poll(&leaf("rev/log"), since, Duration::ZERO, || true)?.ok_or("gave up")?;
    let bell = page.events.last().ok_or("no doorbell")?;
    assert_eq!(
        (bell.event.event, bell.event.reference, bell.why.as_slice()),
        (EventType::NodeArchived, Some(hidden), [Why::Path].as_slice())
    );
    let cursor = f.board.head()?;
    let unarchive = f.archive(&f.owner, hidden, false, "")?.id.ok_or("no event")?;
    let after = f.board.poll(&leaf("rev/log"), cursor, Duration::ZERO, || true)?.ok_or("gave up")?;
    assert_eq!(after.events.iter().map(|d| d.event.id).collect::<Vec<_>>(), [unarchive]);
    Ok(())
}

#[test]
fn event_ids_are_never_reused_and_a_deleted_archived_message_takes_its_archive_events() -> TestResult {
    let Setup { f, agent, .. } = setup()?;
    let message = f.post(&agent, "rev/log", "hi")?.id;
    let archive = f.archive(&f.owner, message, true, "")?.id.ok_or("no event")?;
    f.board.delete_message(message)?;
    assert!(f.board.event(message)?.is_none() && f.board.event(archive)?.is_none());
    let next = f.post(&agent, "rev/log", "next")?.id;
    assert!(next > archive, "{next} is past {archive}");
    // an archived message of a branch goes with the branch
    let doomed = f.post(&agent, "rev/log", "doomed")?.id;
    f.archive(&f.owner, doomed, true, "")?;
    f.board.delete_branch("rev")?;
    Ok(())
}

#[test]
fn archiving_a_message_a_summary_covers_keeps_the_summary() -> TestResult {
    let Setup { f, agent, .. } = setup()?;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    let message = f.post(&agent, "rev/log", "covered")?.id;
    f.set_note_on(&agent, "rev/state", "summary", 0, "rev/log", Some(message))?;
    f.archive(&f.owner, message, true, "")?;
    let note = f.board.note("rev/state", None)?.ok_or("no note")?;
    assert_eq!((note.through, note.behind), (Some(message), Some(0)));
    Ok(())
}
