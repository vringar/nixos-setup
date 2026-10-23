//! What every write goes through: the counter, the signature, the client
//! key and the nonce.

use super::*;

#[test]
fn ids_start_at_10001_and_survive_a_reopen() -> TestResult {
    let f = fixture()?;
    let bootstrap: Vec<i64> = [BOARD_NODE, REGISTER_QUEUE, TRUST_NOTE]
        .into_iter()
        .map(|path| f.board.node(path).map(|n| n.created))
        .collect::<Result<_, _>>()?;
    assert_eq!(bootstrap, [FIRST_ID, FIRST_ID + 1, FIRST_ID + 2]);
    // the owner's first trust list version is the next
    assert_eq!(f.board.note(TRUST_NOTE, None)?.map(|n| (n.version, n.id)), Some((1, FIRST_ID + 3)));
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let one = f.post(&f.owner, "rev/t", "one")?.id;
    let two = f.post(&f.owner, "rev/t", "two")?.id;
    assert_eq!(two, one + 1);
    f.board.close()?;
    let board = Board::open(&f.path)?.board;
    assert_eq!(board.head()?, two, "a reopen bootstraps nothing again");
    let again = submit!(board, &f.owner, post, Post { path: "rev/t", body: "three" })?;
    assert_eq!(again.id, two + 1);
    Ok(())
}

#[test]
fn the_head_is_the_high_water_mark_even_after_a_delete() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let newest = f.post(&f.owner, "rev/t", "soon gone")?.id;
    f.board.delete_message(newest)?;
    assert_eq!(f.board.head()?, newest);
    assert_eq!(f.post(&f.owner, "rev/t", "after")?.id, newest + 1);
    Ok(())
}

#[test]
fn a_signature_is_good_for_one_write() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    // signed against the head of the moment, but sent after another write
    let late = Request::keyed(&f.board, &f.owner, Post { path: "rev/t", body: "late" }, "")?;
    let landed = f.post(&f.owner, "rev/t", "first")?.id;
    let err = refused(f.board.post(&late.signed()))?;
    assert!(matches!(err, Error::StaleNonce { head } if head == landed));
    assert_eq!(
        err.to_json(),
        serde_json::json!({ "error": "stale-nonce", "message": err.to_string(), "head": landed })
    );
    // and once it lands, sending it again is stale too: without a key, a replay
    let once = Request::keyed(&f.board, &f.owner, Post { path: "rev/t", body: "once" }, "")?;
    f.board.post(&once.signed())?;
    assert!(matches!(refused(f.board.post(&once.signed()))?, Error::StaleNonce { .. }));
    Ok(())
}

#[test]
fn a_resend_returns_the_first_result_before_the_nonce_is_checked() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    f.create(&agent, "rev/n", NodeKind::Note)?;
    f.queue(&agent, "rev/q", "")?;

    let post = Request::new(&f.board, &agent, Post { path: "rev/t", body: "hi" })?;
    let first = f.board.post(&post.signed())?;
    f.post(&f.owner, "rev/t", "the head moves on")?;
    assert_eq!(f.board.post(&post.signed())?, first);

    // A note save that landed would now be refused as changed, and an
    // answer as not open: their resends return what they did instead.
    let save = Request::new(
        &f.board,
        &agent,
        SetNote { path: "rev/n", body: "v1", base_version: 0, thread: "", through: None },
    )?;
    let saved = f.board.set_note(&save.signed())?;
    assert_eq!(f.board.set_note(&save.signed())?, saved);
    let item = f.file(&f.owner, "rev/q", "question")?.item;
    let answer = Resolve { item, state: ItemState::Answered, body: "answer", reference: None };
    let answer = Request::new(&f.board, &agent, answer)?;
    let answered = f.board.resolve(&answer.signed())?;
    assert_eq!(f.board.resolve(&answer.signed())?, answered);
    let created = Request::new(&f.board, &agent, create("rev/t2", NodeKind::Thread))?;
    let made = f.board.create(&created.signed())?;
    assert_eq!(f.board.create(&created.signed())?, made);

    // re-signed against the new head, as a client does on a stale nonce: still the same request
    let resigned = Request::keyed(&f.board, &agent, Post { path: "rev/t", body: "hi" }, &post.key)?;
    assert_eq!(f.board.post(&resigned.signed())?, first);
    Ok(())
}

#[test]
fn a_client_key_on_another_request_is_a_conflict() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let key = client_key();
    let first = Request::keyed(&f.board, &agent, Post { path: "rev/t", body: "one" }, &key)?;
    f.board.post(&first.signed())?;
    let other = Request::keyed(&f.board, &agent, Post { path: "rev/t", body: "two" }, &key)?;
    let err = refused(f.board.post(&other.signed()))?;
    assert!(matches!(&err, Error::Conflict(m) if m.contains("already used")), "{err}");
    // the same request by another key: not told of a write it did not make
    let stolen = Request::keyed(&f.board, &f.owner, Post { path: "rev/t", body: "one" }, &key)?;
    assert!(matches!(refused(f.board.post(&stolen.signed()))?, Error::Conflict(_)));
    // nor under another action's name
    let filed = Request::keyed(&f.board, &agent, File { path: "rev/t", body: "one" }, &key)?;
    assert!(matches!(refused(f.board.file(&filed.signed()))?, Error::Conflict(_)));
    Ok(())
}

#[test]
fn a_signature_must_cover_exactly_what_is_written() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let head = f.board.head()?;
    let honest = Request::new(&f.board, &f.owner, Post { path: "rev/t", body: "deploy on friday" })?;
    let tampered = Signed { action: Post { path: "rev/t", body: "deploy now" }, ..honest.signed() };
    let err = refused(f.board.post(&tampered))?;
    assert!(matches!(&err, Error::BadSignature(_)), "{err}");
    assert_eq!((err.code(), err.status()), ("bad-request", 400));
    let other_key = Signed { key: "another", ..honest.signed() };
    assert!(matches!(refused(f.board.post(&other_key))?, Error::BadSignature(_)));
    let unsigned = Signed { signature: "", ..honest.signed() };
    assert!(matches!(refused(f.board.post(&unsigned))?, Error::BadSignature(_)));
    // a post's signature is no file's, over the same fields
    let honest_signed = honest.signed();
    let as_file = Signed {
        action: File { path: "rev/t", body: "deploy on friday" },
        key: honest_signed.key,
        nonce: honest_signed.nonce,
        signature: honest_signed.signature,
    };
    assert!(matches!(refused(f.board.file(&as_file))?, Error::BadSignature(_)));
    assert_eq!(f.board.head()?, head);
    f.board.post(&honest.signed())?;
    Ok(())
}

#[test]
fn every_event_records_its_signer_and_signature() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let request = Request::new(&f.board, &f.owner, Post { path: "rev/t", body: "signed" })?;
    let id = f.board.post(&request.signed())?.id;
    let event = f.board.event(id)?.ok_or("no event")?;
    assert_eq!(event.from.as_deref(), Some(OWNER));
    assert_eq!(event.signed_by, Some(f.owner.fingerprint()));
    assert_eq!((event.event, event.kind, event.path.as_str()), (EventType::Message, NodeKind::Thread, "rev/t"));
    let conn = rusqlite::Connection::open(&f.path)?;
    let (signature, key): (String, String) =
        conn.query_row("SELECT signature, client_key FROM events WHERE id=?", [id], |r| Ok((r.get(0)?, r.get(1)?)))?;
    assert_eq!((signature, key), (request.signature.clone(), request.key.clone()));
    // the board's own carry no signer
    let created = f.board.event(FIRST_ID)?.ok_or("no bootstrap event")?;
    assert_eq!((created.from.as_deref(), created.signed_by), (Some(SERVER_NAME), None));
    Ok(())
}
