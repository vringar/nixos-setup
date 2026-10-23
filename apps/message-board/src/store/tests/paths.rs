//! Paths and the shape of the tree: what may be made where, by whom, and
//! how a listing shows it.

use super::*;

#[test]
fn segments_follow_the_rule() -> TestResult {
    let f = fixture()?;
    let head = f.board.head()?;
    let long = "x".repeat(65);
    for bad in ["", "Rev", "a b", "-x", "a//b", "x/", "/x", "a.b", "r\u{e9}v", &long] {
        let err = refused(f.create(&f.owner, bad, NodeKind::Container))?;
        assert!(matches!(&err, Error::BadRequest(m) if m.contains("paths are")), "{bad:?}: {err}");
    }
    for bad in ["_x", "rev/_y"] {
        let err = refused(f.create(&f.owner, bad, NodeKind::Container))?;
        assert!(err.to_string().contains("only the board makes names starting with '_'"), "{err}");
    }
    assert_eq!(f.board.head()?, head, "a refusal writes nothing");
    for good in ["rev", "0day", "psp_db-2", &"x".repeat(64)] {
        assert_eq!(f.create(&f.owner, good, NodeKind::Container)?.path, good);
    }
    Ok(())
}

#[test]
fn containers_nest_two_deep_and_leaves_sit_in_them() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&agent, "rev/emu", NodeKind::Container)?;
    f.create(&agent, "rev/charter", NodeKind::Note)?;
    f.create(&agent, "rev/emu/log", NodeKind::Thread)?;
    let third = refused(f.create(&agent, "rev/emu/deeper", NodeKind::Container))?;
    assert!(matches!(&third, Error::BadRequest(m) if m.contains("third level")), "{third}");
    let fourth = refused(f.create(&agent, "rev/emu/log/x", NodeKind::Note))?;
    assert!(matches!(&fourth, Error::BadRequest(m) if m.contains("too deep")), "{fourth}");
    let under_leaf = refused(f.create(&agent, "rev/charter/x", NodeKind::Note))?;
    assert!(matches!(&under_leaf, Error::BadRequest(m) if m.contains("nothing goes under a leaf")), "{under_leaf}");
    let top_leaf = refused(f.create(&f.owner, "notes", NodeKind::Note))?;
    assert!(matches!(&top_leaf, Error::BadRequest(m) if m.contains("top-level nodes are containers")), "{top_leaf}");
    let orphan = refused(f.create(&agent, "nowhere/x", NodeKind::Thread))?;
    assert!(matches!(orphan, Error::NotFound(_)));
    let again = refused(f.create(&agent, "rev/emu", NodeKind::Container))?;
    assert!(matches!(&again, Error::Conflict(m) if m.contains("exists already")), "{again}");
    Ok(())
}

#[test]
fn only_the_owner_makes_top_level_containers_and_others_queues() -> TestResult {
    let f = fixture()?;
    let agent = f.agent(2, "agent")?;
    let top = refused(f.create(&agent, "mine", NodeKind::Container))?;
    assert!(matches!(&top, Error::Forbidden(m) if m.contains("top-level")), "{top}");
    f.rev()?;
    let theirs = refused(f.queue(&agent, "rev/q", "someone"))?;
    assert!(matches!(theirs, Error::Forbidden(_)));
    f.queue(&agent, "rev/mine", "")?;
    f.queue(&agent, "rev/also-mine", "agent")?;
    f.queue(&f.owner, "rev/for-later", "not-registered-yet")?;
    let owners: Vec<_> = f
        .board
        .children("rev")?
        .into_iter()
        .map(|c| match c.status {
            Status::Queue { owner, .. } => owner,
            _ => String::new(),
        })
        .collect();
    assert_eq!(owners, ["agent", "not-registered-yet", "agent"]);
    let bad_owner = refused(f.queue(&f.owner, "rev/x", "no spaces"))?;
    assert!(matches!(bad_owner, Error::BadRequest(_)));
    Ok(())
}

#[test]
fn owners_bodies_and_descriptions_fit_their_kind() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let thread_owner =
        refused(submit!(f.board, &f.owner, create, Create { owner: "x", ..create("rev/t", NodeKind::Thread) }))?;
    assert!(matches!(&thread_owner, Error::BadRequest(m) if m.contains("only queues have an owner")), "{thread_owner}");
    let thread_body =
        refused(submit!(f.board, &f.owner, create, Create { body: "x", ..create("rev/t", NodeKind::Thread) }))?;
    assert!(matches!(&thread_body, Error::BadRequest(m) if m.contains("only notes have a body")), "{thread_body}");
    let long = "d".repeat(DESCRIPTION_MAX + 1);
    for description in ["two\nlines", long.as_str()] {
        let err =
            refused(submit!(f.board, &f.owner, create, Create { description, ..create("rev/t", NodeKind::Thread) }))?;
        assert!(matches!(err, Error::BadRequest(_)));
    }
    let described = Create { description: "the log, ✓", ..create("rev/t", NodeKind::Thread) };
    submit!(f.board, &f.owner, create, described)?;
    assert_eq!(f.board.node("rev/t")?.description, "the log, ✓");
    Ok(())
}

#[test]
fn missing_paths_are_not_found_and_kinds_are_checked() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    assert!(matches!(refused(f.post(&f.owner, "rev/typo", "lost"))?, Error::NotFound(_)));
    assert!(matches!(refused(f.file(&f.owner, "rev/typo", "lost"))?, Error::NotFound(_)));
    assert!(matches!(refused(f.set_note(&f.owner, "rev/typo", "lost", 0, None))?, Error::NotFound(_)));
    assert!(matches!(refused(f.board.node("rev/typo"))?, Error::NotFound(_)));
    assert!(matches!(refused(f.board.children("rev/t"))?, Error::BadRequest(_)));
    assert!(matches!(refused(f.board.note("rev/t", None))?, Error::BadRequest(_)));
    assert!(matches!(refused(f.board.queue("rev/t", StateFilter::All, 0))?, Error::BadRequest(_)));
    let wrong = refused(f.file(&f.owner, "rev/t", "a thread is no queue"))?;
    assert_eq!(wrong.to_string(), "\"rev/t\" is a thread, not a queue");
    Ok(())
}

#[test]
fn the_boards_own_nodes_are_there_and_closed_to_writes() -> TestResult {
    let f = fixture()?;
    let top: Vec<_> = f.board.children("")?.into_iter().map(|c| (c.path, c.kind)).collect();
    assert_eq!(top, [(BOARD_NODE.to_owned(), NodeKind::Container)]);
    let own: Vec<_> = f.board.children(BOARD_NODE)?.into_iter().map(|c| (c.path, c.kind, c.creator)).collect();
    assert_eq!(
        own,
        [
            (REGISTER_QUEUE.to_owned(), NodeKind::Queue, SERVER_NAME.to_owned()),
            (TRUST_NOTE.to_owned(), NodeKind::Note, SERVER_NAME.to_owned())
        ]
    );
    assert_eq!(f.board.node(REGISTER_QUEUE)?.owner.as_deref(), Some(OWNER));
    assert!(f.board.node(TRUST_NOTE)?.generated);
    let agent = f.agent(2, "agent")?;
    assert!(matches!(refused(f.file(&agent, REGISTER_QUEUE, "let me in"))?, Error::Forbidden(_)));
    assert!(matches!(refused(f.set_note(&f.owner, TRUST_NOTE, "trust me", 2, None))?, Error::Forbidden(_)));
    assert!(matches!(refused(f.create(&f.owner, "_board/x", NodeKind::Thread))?, Error::BadRequest(_)));
    Ok(())
}

#[test]
fn a_listing_shows_how_each_child_stands() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&agent, "rev/emu", NodeKind::Container)?;
    f.create(&agent, "rev/emu/x", NodeKind::Thread)?;
    f.create(&agent, "rev/log", NodeKind::Thread)?;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    f.queue(&agent, "rev/q", "")?;
    let listing =
        |path| -> Result<Vec<Status>, Error> { Ok(f.board.children(path)?.into_iter().map(|c| c.status).collect()) };
    let empty = listing("rev")?;
    assert_eq!(
        empty,
        [
            Status::Container { children: 1 },
            Status::Thread { last_id: None, last_ts: None, messages: 0 },
            Status::Queue { owner: "agent".to_owned(), open: 0, oldest_open_ts: None },
            Status::Note { version: 0, ts: None, from: None, thread: None, through: None, behind: None },
        ]
    );
    let first = f.post(&agent, "rev/log", "one")?.id;
    f.set_note(&agent, "rev/state", "summary", 0, Some(first))?;
    let last = f.post(&agent, "rev/log", "two")?.id;
    f.post(&agent, "rev/emu/x", "elsewhere")?;
    let filed = f.file(&f.owner, "rev/q", "please")?.item;
    f.file(&f.owner, "rev/q", "and this")?;
    let children = f.board.children("rev")?;
    let get = |path: &str| children.iter().find(|c| c.path == path).map(|c| &c.status);
    assert!(matches!(get("rev/log"), Some(Status::Thread { last_id: Some(id), messages: 2, .. }) if *id == last));
    assert!(
        matches!(get("rev/state"), Some(Status::Note { version: 1, through: Some(t), behind: Some(1), from: Some(by), .. }) if *t == first && by == "agent")
    );
    let filed_ts = f.board.item(filed)?.ok_or("no item")?.item.ts;
    assert!(matches!(get("rev/q"), Some(Status::Queue { open: 2, oldest_open_ts: Some(ts), .. }) if *ts == filed_ts));
    // the listing's JSON: the status's fields beside the node's
    let json = serde_json::to_value(children.iter().find(|c| c.path == "rev/state").ok_or("no note")?)?;
    assert_eq!(json.get("version"), Some(&serde_json::json!(1)));
    assert_eq!(json.get("kind"), Some(&serde_json::json!("note")));
    Ok(())
}
