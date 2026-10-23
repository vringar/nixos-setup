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
