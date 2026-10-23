//! Notes and threads.

use super::*;

#[test]
fn notes_go_up_a_version_at_a_time_from_the_version_seen() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    let with_body = Create { body: "# v1\n", ..create("rev/charter", NodeKind::Note) };
    submit!(f.board, &agent, create, with_body)?;
    f.create(&agent, "rev/empty", NodeKind::Note)?;
    assert!(f.board.note("rev/empty", None)?.is_none(), "made without a body: version 0");
    let blank = Create { body: " \n\t", ..create("rev/blank", NodeKind::Note) };
    submit!(f.board, &agent, create, blank)?;
    assert!(f.board.note("rev/blank", None)?.is_none());

    assert_eq!(f.board.note("rev/charter", None)?.map(|n| n.version), Some(1));
    let two = f.set_note(&agent, "rev/charter", "# v2\n", 1, None)?;
    assert_eq!((two.path.as_str(), two.version), ("rev/charter", 2));
    let stale = refused(f.set_note(&f.owner, "rev/charter", "# lost update", 1, None))?;
    assert!(matches!(stale, Error::NoteChanged { version: 2 }));
    assert_eq!(stale.to_json().get("version"), Some(&serde_json::json!(2)));
    f.set_note(&f.owner, "rev/charter", "# v3, by the owner\n", 2, None)?;
    let current = f.board.note("rev/charter", None)?.ok_or("no note")?;
    assert_eq!((current.version, current.from.as_str(), current.body.as_str()), (3, OWNER, "# v3, by the owner\n"));
    let first = f.board.note("rev/charter", Some(1))?.ok_or("no v1")?;
    assert_eq!((first.body.as_str(), first.from.as_str()), ("# v1\n", "agent"));
    assert!(matches!(refused(f.board.note("rev/charter", Some(9)))?, Error::NotFound(_)));
    let history: Vec<(i64, String)> =
        f.board.note_history("rev/charter")?.into_iter().map(|v| (v.version, v.from)).collect();
    assert_eq!(history, [(1, "agent".to_owned()), (2, "agent".to_owned()), (3, OWNER.to_owned())]);
    Ok(())
}

#[test]
fn an_empty_save_never_wipes_a_note() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/n", NodeKind::Note)?;
    for body in ["", "  \n "] {
        let err = refused(f.set_note(&f.owner, "rev/n", body, 0, None))?;
        assert!(matches!(err, Error::EmptyNote));
        assert_eq!((err.code(), err.status()), ("empty-note", 400));
    }
    Ok(())
}

#[test]
fn only_the_creator_and_the_owner_write_a_note() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let author = f.agent(2, "author")?;
    let other = f.agent(3, "other-agent")?;
    f.create(&author, "rev/n", NodeKind::Note)?;
    let err = refused(f.set_note(&other, "rev/n", "mine now", 0, None))?;
    assert!(matches!(&err, Error::Forbidden(m) if m.contains("author's note")), "{err}");
    f.set_note(&author, "rev/n", "v1", 0, None)?;
    f.set_note(&f.owner, "rev/n", "v2", 1, None)?;
    Ok(())
}

#[test]
fn through_is_a_message_and_behind_counts_its_threads_later_ones() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&agent, "rev/log", NodeKind::Thread)?;
    f.create(&agent, "rev/other", NodeKind::Thread)?;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    let summarized = f.post(&agent, "rev/log", "one")?.id;
    let not_a_message = f.board.node("rev/log")?.created;
    let err = refused(f.set_note(&agent, "rev/state", "s", 0, Some(not_a_message)))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("not a message")), "{err}");
    assert!(matches!(refused(f.set_note(&agent, "rev/state", "s", 0, Some(99_999)))?, Error::BadRequest(_)));
    f.set_note(&agent, "rev/state", "summary of one", 0, Some(summarized))?;
    assert_eq!(f.board.note("rev/state", None)?.and_then(|n| n.behind), Some(0));
    f.post(&agent, "rev/log", "two")?;
    f.post(&agent, "rev/other", "not in that thread")?;
    f.post(&agent, "rev/log", "three")?;
    let note = f.board.note("rev/state", None)?.ok_or("no note")?;
    assert_eq!((note.through, note.behind), (Some(summarized), Some(2)));
    Ok(())
}

#[test]
fn a_note_names_the_thread_it_summarizes_and_the_rules_hold() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&agent, "rev/log", NodeKind::Thread)?;
    f.create(&agent, "rev/other", NodeKind::Thread)?;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    let mine = f.post(&agent, "rev/log", "one")?.id;
    let theirs = f.post(&agent, "rev/other", "elsewhere")?.id;
    // through needs a thread
    let err = refused(f.set_note_on(&agent, "rev/state", "s", 0, "", Some(mine)))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("needs a \"thread\"")), "{err}");
    // the thread must exist, and be a thread
    assert!(matches!(refused(f.set_note_on(&agent, "rev/state", "s", 0, "rev/nope", Some(mine)))?, Error::NotFound(_)));
    let err = refused(f.set_note_on(&agent, "rev/state", "s", 0, "rev", Some(mine)))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("not a thread")), "{err}");
    // the message must be in that thread
    let err = refused(f.set_note_on(&agent, "rev/state", "s", 0, "rev/log", Some(theirs)))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("not a message of")), "{err}");
    // a thread without through is refused, and so is the pair's other half
    let err = refused(f.set_note_on(&agent, "rev/state", "s", 0, "rev/log", None))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("needs a \"through\"")), "{err}");
    // the pair is fine, with a leading slash too
    f.set_note_on(&agent, "rev/state", "v1", 0, "/rev/log", Some(mine))?;
    f.set_note_on(&agent, "rev/state", "v2", 1, "rev/log", Some(mine))?;
    let note = f.board.note("rev/state", None)?.ok_or("no note")?;
    assert_eq!((note.thread.as_deref(), note.through, note.behind), (Some("rev/log"), Some(mine), Some(0)));
    let history = f.board.note_history("rev/state")?;
    assert_eq!(history.iter().map(|v| v.thread.as_deref()).collect::<Vec<_>>(), [Some("rev/log"), Some("rev/log")]);
    // neither is fine too
    f.set_note(&agent, "rev/state", "v3", 2, None)?;
    assert_eq!(f.board.note("rev/state", None)?.ok_or("no note")?.thread, None);
    Ok(())
}

#[test]
fn a_thread_reads_its_tail_or_from_an_id() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    assert!(matches!(refused(f.post(&f.owner, "rev/t", " \n"))?, Error::BadRequest(_)));
    let mut ids = Vec::new();
    for i in 0..5 {
        ids.push(f.post(&f.owner, "rev/t", &format!("m{i}"))?.id);
    }
    let bodies = |page: &ThreadPage| page.messages.iter().map(|m| m.body.clone()).collect::<Vec<_>>();
    let tail = f.board.thread("rev/t", Range::Last(3))?;
    assert_eq!((bodies(&tail), tail.earlier), (vec!["m2".to_owned(), "m3".to_owned(), "m4".to_owned()], 2));
    let since = f.board.thread("rev/t", Range::Since(ids.get(3).copied().unwrap_or_default()))?;
    assert_eq!((bodies(&since), since.earlier), (vec!["m4".to_owned()], 4));
    let all = f.board.thread("rev/t", Range::All)?;
    assert_eq!((all.messages.len(), all.earlier), (5, 0));
    let none = f.board.thread("rev/t", Range::Last(-1))?;
    assert_eq!((none.messages.len(), none.earlier), (0, 5));
    let message = all.messages.first().ok_or("empty")?;
    assert_eq!((message.path.as_str(), message.from.as_str()), ("rev/t", OWNER));
    Ok(())
}
