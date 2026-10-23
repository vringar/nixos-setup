//! Keys: registration, approval, revocation and the owner key.

use super::*;

#[test]
fn each_kind_of_key_gets_its_own_refusal() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let newcomer = key(2)?;
    let err = refused(f.post(&newcomer, "rev/t", "hi"))?;
    assert!(matches!(&err, Error::NotRegistered { fingerprint } if *fingerprint == newcomer.fingerprint()));
    assert!(err.is_identity());
    assert_eq!(err.to_json().get("command"), Some(&serde_json::json!(REGISTER_COMMAND)));
    assert_eq!((err.code(), err.status()), ("not-registered", 403));

    let pending = f.register(&newcomer, "newcomer")?;
    let err = refused(f.post(&newcomer, "rev/t", "hi"))?;
    assert!(matches!(err, Error::RegistrationPending { item } if item == pending.item));

    f.resolve(&f.owner, pending.item, ItemState::Declined, "who are you?")?;
    let err = refused(f.post(&newcomer, "rev/t", "hi"))?;
    assert!(matches!(err, Error::RegistrationDeclined { item } if item == pending.item));
    assert_eq!(f.board.key(&newcomer.fingerprint())?.map(|k| k.state), Some(KeyState::Declined));

    // declined may try again, with a new registration
    let again = f.register(&newcomer, "newcomer")?;
    assert_ne!(again.item, pending.item);
    f.approve(again.item, "newcomer", &newcomer)?;
    f.post(&newcomer, "rev/t", "in")?;
    let revoked = f.revoke(&f.owner, &newcomer.fingerprint(), "left")?;
    let err = refused(f.post(&newcomer, "rev/t", "still here?"))?;
    assert!(matches!(err, Error::KeyRevoked { revoked_at } if revoked_at == revoked.id));
    assert!(err.is_identity());
    let err = refused(f.register(&newcomer, "newcomer"))?;
    assert!(matches!(err, Error::KeyRevoked { .. }), "a revoked key does not come back: {err}");
    Ok(())
}

#[test]
fn registering_is_idempotent_per_key_and_answers_a_trusted_one() -> TestResult {
    let f = fixture()?;
    let agent = key(2)?;
    let first = f.register(&agent, "agent")?;
    assert_eq!((first.name.as_str(), first.fingerprint.as_str()), ("agent", agent.fingerprint().as_str()));
    let head = f.board.head()?;
    assert_eq!(f.register(&agent, "agent")?, first);
    let err = refused(f.register(&agent, "another-name"))?;
    assert!(matches!(&err, Error::Conflict(m) if m.contains("another note or repo")), "{err}");
    assert_eq!(err.status(), 409);
    let other_note = submit!(f.board, &agent, register, Register { name: "agent", note: "else", repo: "/src/test" });
    assert!(matches!(refused(other_note)?, Error::Conflict(_)));
    let other_repo = submit!(f.board, &agent, register, Register { name: "agent", note: "for a test", repo: "/x" });
    assert!(matches!(refused(other_repo)?, Error::Conflict(_)));
    assert_eq!(f.board.head()?, head);
    f.approve(first.item, "agent", &agent)?;
    let err = refused(f.register(&agent, "agent"))?;
    assert!(matches!(&err, Error::AlreadyRegistered { name } if name == "agent"));
    assert_eq!(err.status(), 409);
    let err = refused(f.register(&f.owner, "Stefan2"))?;
    assert!(matches!(err, Error::AlreadyRegistered { name } if name == OWNER));
    Ok(())
}

#[test]
fn names_are_checked_before_they_reach_the_owner() -> TestResult {
    let f = fixture()?;
    f.agent(2, "psp-db")?;
    for (seed, name) in
        (10..).zip(["psp-DB", "psp-d", "psp_db", "pspdb2", "Stefan", "stefan", "Stefn", "board", "b0ard"])
    {
        let err = refused(f.register(&key(seed)?, name))?;
        assert!(matches!(err, Error::Forbidden(_)), "{name}: {err}");
    }
    for name in ["", "-flag", "_board", "two words", "na\u{ef}ve", &"x".repeat(NAME_MAX + 1)] {
        assert!(matches!(refused(f.register(&key(30)?, name))?, Error::BadRequest(_)), "{name:?}");
    }
    let big = "n".repeat(REGISTER_TEXT_MAX + 1);
    let err = refused(submit!(f.board, &key(31)?, register, Register { name: "agent", note: &big, repo: "" }))?;
    assert!(matches!(err, Error::BadRequest(_)));
    // three edits from every bound name, and the exact bound name (a replacement)
    f.register(&key(32)?, "psp-etl")?;
    f.register(&key(33)?, "psp-db")?;
    Ok(())
}

#[test]
fn registrations_stop_at_twenty_waiting() -> TestResult {
    let f = fixture()?;
    let mut first = None;
    for seed in 0..20_u8 {
        let registered = f.register(&key(seed.saturating_add(100))?, &format!("agent-{seed:02}-xyz"))?;
        first.get_or_insert(registered);
    }
    let err = refused(f.register(&key(200)?, "one-too-many"))?;
    assert!(matches!(err, Error::RegisterFull));
    assert_eq!((err.code(), err.status()), ("register-full", 429));
    // a waiting key still gets its own back
    let first = first.ok_or("none registered")?;
    assert_eq!(f.register(&key(100)?, "agent-00-xyz")?, first);
    f.resolve(&f.owner, first.item, ItemState::Declined, "make room")?;
    f.register(&key(200)?, "one-too-many")?;
    Ok(())
}

#[test]
fn approving_replaces_the_names_key_and_declines_its_rivals() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let old = f.agent(2, "session")?;
    let before = f.post(&old, "rev/t", "from the old key")?.id;
    let new = key(3)?;
    let rival = key(4)?;
    let replacing = f.register(&new, "session")?;
    let rivalling = f.register(&rival, "session")?;
    let waiting = f.board.registrations()?;
    let replaces: Vec<_> = waiting.iter().map(|r| r.replaces.clone()).collect();
    assert_eq!(replaces, [Some(old.fingerprint()), Some(old.fingerprint())]);

    let approved = f.approve(replacing.item, "session", &new)?;
    let state = |k: &PrivateKey| f.board.key(&k.fingerprint()).map(|k| k.map(|k| k.state));
    assert_eq!(
        (state(&old)?, state(&new)?, state(&rival)?),
        (Some(KeyState::Revoked), Some(KeyState::Trusted), Some(KeyState::Declined))
    );
    let rival_item = f.board.item(rivalling.item)?.ok_or("no item")?.item;
    assert_eq!(rival_item.state, ItemState::Declined);
    assert_eq!(rival_item.resolution.map(|r| r.body), Some(format!("name session bound at #{}", approved.id)));
    assert_eq!(f.board.registrations()?, Vec::<Registration>::new());
    // the old key's events stay, and say when it was revoked
    let revoked_at = f.board.key(&old.fingerprint())?.and_then(|k| k.revoked).ok_or("not revoked")?;
    let event = f.board.event(before)?.ok_or("no event")?;
    assert_eq!((event.signed_by, event.revoked_at), (Some(old.fingerprint()), Some(revoked_at)));
    let page = f.board.thread("rev/t", Range::All)?;
    assert_eq!(page.messages.first().map(|m| m.revoked_at), Some(Some(revoked_at)));
    // the new key writes as the name
    let after = f.post(&new, "rev/t", "from the new key")?.id;
    assert_eq!(f.board.event(after)?.and_then(|e| e.from).as_deref(), Some("session"));
    // the trust list's version that did it is the revocation
    let trust = f.board.note(TRUST_NOTE, None)?.ok_or("no trust list")?;
    assert_eq!(trust.id, revoked_at);
    assert!(trust.body.contains(&format!("trusted session {} approved #{}", new.fingerprint(), approved.id)));
    assert!(trust.body.contains(&format!("change: revoked {} (session): replaced by", old.fingerprint())));
    assert!(!trust.body.contains(&format!("trusted session {}", old.fingerprint())));
    Ok(())
}

#[test]
fn the_inbox_flags_look_alike_names() -> TestResult {
    let f = fixture()?;
    let (one, two) = (key(2)?, key(3)?);
    f.register(&one, "ghidra")?;
    f.register(&two, "ghidr4")?;
    f.register(&key(4)?, "unrelated")?;
    f.register(&key(5)?, "ghidr4")?;
    let flags: Vec<(String, Vec<String>)> =
        f.board.registrations()?.into_iter().map(|r| (r.name, r.look_alikes)).collect();
    assert_eq!(
        flags,
        [
            ("ghidra".to_owned(), vec!["ghidr4".to_owned()]),
            ("ghidr4".to_owned(), vec!["ghidra".to_owned()]),
            ("unrelated".to_owned(), vec![]),
            ("ghidr4".to_owned(), vec!["ghidra".to_owned()])
        ]
    );
    Ok(())
}

#[test]
fn a_name_bound_since_makes_a_waiting_look_alike_unapprovable() -> TestResult {
    let f = fixture()?;
    let squatter = key(2)?;
    let waiting = f.register(&squatter, "ghidr4")?;
    f.agent(3, "ghidra")?;
    let flagged: Vec<_> = f.board.registrations()?.into_iter().map(|r| (r.name, r.look_alikes)).collect();
    assert_eq!(flagged, [("ghidr4".to_owned(), vec!["ghidra".to_owned()])]);
    let err = refused(f.approve(waiting.item, "ghidr4", &squatter))?;
    assert!(matches!(&err, Error::Forbidden(m) if m.contains("too close") && m.contains("ghidra")), "{err}");
    assert_eq!(f.board.key(&squatter.fingerprint())?.map(|k| k.state), Some(KeyState::Pending));
    // a replacement under the bound name itself is no look-alike of it
    let session = f.agent(4, "session")?;
    let other = key(5)?;
    let replacement = f.register(&other, "session")?;
    f.approve(replacement.item, "session", &other)?;
    assert_eq!(f.board.key(&session.fingerprint())?.map(|k| k.state), Some(KeyState::Revoked));
    Ok(())
}

#[test]
fn only_the_owner_key_approves_and_revokes() -> TestResult {
    let f = fixture()?;
    let agent = f.agent(2, "agent")?;
    let newcomer = key(3)?;
    let waiting = f.register(&newcomer, "newcomer")?;
    let approve = Approve { item: waiting.item, name: "newcomer", fingerprint: &newcomer.fingerprint() };
    let err = refused(submit!(f.board, &agent, approve, approve))?;
    assert!(matches!(err, Error::Forbidden(_)));
    assert!(matches!(refused(f.revoke(&agent, &f.owner.fingerprint(), "coup"))?, Error::Forbidden(_)));
    let err = refused(f.revoke(&f.owner, &f.owner.fingerprint(), "oops"))?;
    assert!(matches!(&err, Error::Forbidden(m) if m.contains("owner key")), "{err}");
    let err = refused(f.revoke(&f.owner, &newcomer.fingerprint(), "not yet trusted"))?;
    assert!(matches!(err, Error::BadRequest(_)));
    assert!(matches!(refused(f.revoke(&f.owner, "SHA256:nobody", "x"))?, Error::NotFound(_)));
    // revoking twice gives the first revocation
    let revoked = f.revoke(&f.owner, &agent.fingerprint(), "done")?;
    assert_eq!(f.revoke(&f.owner, &agent.fingerprint(), "again")?, revoked);
    assert_eq!(revoked.name, "agent");
    let trust = f.board.note(TRUST_NOTE, None)?.ok_or("no trust list")?;
    assert!(trust.body.contains(&format!("change: revoked {} (agent): done", agent.fingerprint())), "{}", trust.body);
    Ok(())
}

#[test]
fn an_approval_must_name_the_registrations_key() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let newcomer = key(2)?;
    let waiting = f.register(&newcomer, "newcomer")?;
    let impostor = key(3)?;
    let err = refused(f.approve(waiting.item, "newcomer", &impostor))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("is for newcomer")), "{err}");
    assert!(matches!(refused(f.approve(waiting.item, "other-name", &newcomer))?, Error::BadRequest(_)));
    f.queue(&f.owner, "rev/q", "")?;
    let item = f.file(&f.owner, "rev/q", "not a registration")?.item;
    assert!(matches!(refused(f.approve(item, "newcomer", &newcomer))?, Error::BadRequest(_)));
    // nor is a registration answered except by approving it
    let err = refused(f.resolve(&f.owner, waiting.item, ItemState::Answered, "ok"))?;
    assert!(matches!(&err, Error::BadRequest(m) if m.contains("approve")), "{err}");
    f.approve(waiting.item, "newcomer", &newcomer)?;
    assert!(matches!(refused(f.approve(waiting.item, "newcomer", &newcomer))?, Error::ItemNotOpen { .. }));
    Ok(())
}

#[test]
fn the_owner_key_is_bound_from_the_configuration_and_rotated() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let old_post = f.post(&f.owner, "rev/t", "as the old owner key")?.id;
    // the same configuration again: nothing to do, nothing written
    let head = f.board.head()?;
    assert_eq!(f.board.set_owner(OWNER, &verified(&f.owner))?, OwnerChange::default());
    assert_eq!(f.board.head()?, head);

    let new = key(9)?;
    let change = f.board.set_owner(OWNER, &verified(&new))?;
    assert_eq!(change, OwnerChange { changed: true, revoked: vec![f.owner.fingerprint()] });
    assert_eq!(f.board.owner()?, Some(Owner { name: OWNER.to_owned(), fingerprint: new.fingerprint() }));
    let event = f.board.event(old_post)?.ok_or("no event")?;
    let revoked_at = event.revoked_at.ok_or("not marked revoked")?;
    let rotation = f.board.event(revoked_at)?.ok_or("no rotation event")?;
    assert_eq!(
        (rotation.event, rotation.from.as_deref(), rotation.signed_by),
        (EventType::NoteVersion, Some(SERVER_NAME), None)
    );
    assert!(matches!(refused(f.post(&f.owner, "rev/t", "old key"))?, Error::KeyRevoked { .. }));
    f.post(&new, "rev/t", "new key")?;
    // the new key approves
    let agent = key(2)?;
    let waiting = f.register(&agent, "agent")?;
    submit!(f.board, &new, approve, Approve { item: waiting.item, name: "agent", fingerprint: &agent.fingerprint() })?;
    // a revoked key cannot become the owner's again
    let err = refused(f.board.set_owner(OWNER, &verified(&f.owner)))?;
    assert!(matches!(&err, Error::Forbidden(m) if m.contains("was revoked")), "{err}");
    // a new owner name takes the register queue with it
    f.board.set_owner("stefan-z", &verified(&new))?;
    assert_eq!(f.board.node(REGISTER_QUEUE)?.owner.as_deref(), Some("stefan-z"));
    assert!(matches!(refused(f.board.set_owner("board", &verified(&new)))?, Error::Forbidden(_)));
    assert!(matches!(refused(f.board.set_owner("no spaces", &verified(&new)))?, Error::BadRequest(_)));
    Ok(())
}

#[test]
fn without_an_owner_nothing_can_be_approved() -> TestResult {
    let dir = tempfile::tempdir()?;
    let board = Board::open(&dir.path().join("board-v2.db"))?.board;
    assert_eq!(board.owner()?, None);
    assert_eq!(board.node(REGISTER_QUEUE)?.owner.as_deref(), Some(SERVER_NAME));
    assert_eq!(board.note(TRUST_NOTE, None)?, None);
    let agent = key(2)?;
    submit!(board, &agent, register, Register { name: "agent", note: "", repo: "" })?;
    assert_eq!(board.registrations()?.len(), 1);
    Ok(())
}
