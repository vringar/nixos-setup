//! Reads that cap what they return, and say so.

use super::*;

fn ok(n: usize) -> impl Iterator<Item = rusqlite::Result<usize>> {
    (0..n).map(Ok)
}

#[test]
fn a_cap_cuts_by_rows_or_bytes_but_always_returns_one_row() -> TestResult {
    assert_eq!(capped_at(ok(3), |_| 1, 3, 100)?, (vec![0, 1, 2], false));
    assert_eq!(capped_at(ok(4), |_| 1, 3, 100)?, (vec![0, 1, 2], true));
    assert_eq!(capped_at(ok(4), |_| 10, 100, 25)?, (vec![0, 1], true));
    assert_eq!(capped_at(ok(2), |_| 1000, 100, 25)?, (vec![0], true), "one row, however big");
    assert_eq!(capped_at(ok(0), |_| 1, 3, 100)?, (vec![], false));
    Ok(())
}

/// Bodies near the request limit, so that a few of them pass the byte cap.
fn big() -> String {
    "x".repeat(900 * 1024)
}

#[test]
fn a_thread_read_stops_at_the_byte_cap_and_pages_on() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let ids: Vec<i64> = (0..10).map(|_| f.post(&f.owner, "rev/t", &big()).map(|p| p.id)).collect::<Result<_, _>>()?;
    let first = f.board.thread("rev/t", Range::All)?;
    assert!(first.truncated);
    assert!(!first.messages.is_empty() && first.messages.len() < ids.len());
    let last = first.messages.last().map(|m| m.id).ok_or("no messages")?;
    let rest = f.board.thread("rev/t", Range::Since(last))?;
    assert_eq!(first.messages.len() + rest.messages.len(), ids.len(), "nothing lost between the pages");
    assert_eq!(rest.earlier, i64::try_from(first.messages.len())?);
    // the tail of a short thread is whole
    assert!(!f.board.thread("rev/t", Range::Last(2))?.truncated);
    Ok(())
}

#[test]
fn an_item_read_stops_at_the_byte_cap_and_pages_on_with_after() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.queue(&f.owner, "rev/q", "")?;
    let ids: Vec<i64> = (0..10).map(|_| f.file(&f.owner, "rev/q", &big()).map(|p| p.item)).collect::<Result<_, _>>()?;
    let all = ItemQuery { state: StateFilter::All, ..ItemQuery::default() };
    let first = f.board.items(&all)?;
    assert!(first.truncated && first.items.len() < ids.len());
    let last = first.items.last().map(|i| i.id).ok_or("no items")?;
    let rest = f.board.items(&ItemQuery { after: last, ..all })?;
    assert_eq!(first.items.len() + rest.items.len(), ids.len());
    let queue = f.board.queue("rev/q", StateFilter::All, last)?;
    assert_eq!(queue.items.len(), rest.items.len());
    Ok(())
}

#[test]
fn event_refs_say_what_an_id_points_at_and_skip_what_is_no_event() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let hidden = f.post(&f.owner, "rev/t", "secret\nsecond line")?.id;
    let shown = f.post(&f.owner, "rev/t", "hello\nsecond line")?.id;
    f.archive(&f.owner, hidden, true, "noise")?;
    let refs = f.board.event_refs(&[shown, hidden, 99_999, 1])?;
    assert_eq!(refs.len(), 2);
    let shown = refs.get(&shown).ok_or("no ref")?;
    assert_eq!((shown.event, shown.path.as_str(), shown.kind), (EventType::Message, "rev/t", NodeKind::Thread));
    assert!(shown.snippet.starts_with("hello"));
    // an archived event is an id that resolves, but its text is not given away
    assert_eq!(refs.get(&hidden).map(|r| r.snippet.as_str()), Some(""));
    assert!(f.board.event_refs(&[])?.is_empty());
    // more ids than one lookup takes
    let many: Vec<i64> = (10_000..11_200).collect();
    assert!(f.board.event_refs(&many)?.len() >= 2);
    Ok(())
}

#[test]
fn a_window_starts_before_an_old_message_and_is_none_for_a_new_one() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let ids: Vec<i64> =
        (0..30).map(|n| f.post(&f.owner, "rev/t", &format!("m{n}")).map(|p| p.id)).collect::<Result<_, _>>()?;
    let at = |n: usize| ids.get(n).copied().ok_or("no message");
    // among the newest 10: no window
    assert_eq!(f.board.message_window("rev/t", at(29)?, false, 10, 3)?, None);
    assert_eq!(f.board.message_window("rev/t", at(20)?, false, 10, 3)?, None);
    // the 11th newest is out; the window starts 3 messages before it, so since is one less than the third before
    assert_eq!(f.board.message_window("rev/t", at(19)?, false, 10, 3)?, Some(at(16)? - 1));
    let since = f.board.message_window("rev/t", at(19)?, false, 10, 3)?.ok_or("no window")?;
    let page = f.board.thread("rev/t", Range::Since(since))?;
    assert_eq!(page.messages.first().map(|m| m.id), Some(at(16)?));
    // near the start there are not that many before it
    assert_eq!(f.board.message_window("rev/t", at(3)?, false, 10, 3)?, Some(at(0)? - 1));
    assert_eq!(f.board.message_window("rev/t", at(1)?, false, 10, 3)?, Some(0));
    Ok(())
}

#[test]
fn a_queue_window_starts_before_an_item_only_when_many_come_first() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.queue(&f.owner, "rev/q", "")?;
    let items: Vec<i64> = (0..12).map(|_| f.file(&f.owner, "rev/q", "q").map(|p| p.item)).collect::<Result<_, _>>()?;
    let at = |n: usize| items.get(n).copied().ok_or("no item");
    assert_eq!(f.board.item_window("rev/q", at(5)?, false, 8, 2)?, None);
    assert_eq!(f.board.item_window("rev/q", at(10)?, false, 8, 2)?, Some(at(8)? - 1));
    Ok(())
}
