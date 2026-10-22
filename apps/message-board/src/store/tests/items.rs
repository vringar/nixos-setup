//! Queue items: who may resolve them how, and that a resolved item stays so.

use super::*;

/// A queue `rev/q` owned by `owner-agent`, an `asker` and an `other`.
struct Roles {
    f: Fixture,
    queue_owner: PrivateKey,
    asker: PrivateKey,
    other: PrivateKey,
}

fn roles() -> Result<Roles, Box<dyn StdError>> {
    let f = fixture()?;
    f.rev()?;
    let queue_owner = f.agent(2, "owner-agent")?;
    let asker = f.agent(3, "asker")?;
    let other = f.agent(4, "bystander")?;
    f.queue(&queue_owner, "rev/q", "")?;
    f.create(&queue_owner, "rev/t", NodeKind::Thread)?;
    Ok(Roles { f, queue_owner, asker, other })
}

#[test]
fn the_permission_matrix() -> TestResult {
    let r = roles()?;
    let actors = [("queue owner", &r.queue_owner), ("asker", &r.asker), ("human", &r.f.owner), ("other", &r.other)];
    let allowed = |state, who| match state {
        ItemState::Answered => who == "queue owner",
        ItemState::Declined => who == "queue owner" || who == "human",
        _ => who == "asker",
    };
    for state in [ItemState::Answered, ItemState::Declined, ItemState::Cancelled] {
        for (who, actor) in actors {
            let item = r.f.file(&r.asker, "rev/q", "please")?.item;
            let outcome = r.f.resolve(actor, item, state, "because");
            match outcome {
                Ok(resolved) => {
                    assert!(allowed(state, who), "{who} may not {state}");
                    assert_eq!((resolved.item, resolved.state), (item, state));
                    let detail = r.f.board.item(item)?.ok_or("no item")?;
                    assert_eq!(detail.item.state, state);
                    assert_eq!(detail.item.resolution.map(|res| res.id), Some(resolved.id));
                }
                Err(Error::Forbidden(_)) => assert!(!allowed(state, who), "{who} may {state}"),
                Err(err) => return Err(format!("{who} {state}: {err}").into()),
            }
        }
    }
    Ok(())
}

#[test]
fn a_resolved_item_stays_resolved() -> TestResult {
    let r = roles()?;
    let item = r.f.file(&r.asker, "rev/q", "please")?.item;
    let answered = r.f.resolve(&r.queue_owner, item, ItemState::Answered, "done")?;
    for (actor, state) in
        [(&r.queue_owner, ItemState::Answered), (&r.f.owner, ItemState::Declined), (&r.asker, ItemState::Cancelled)]
    {
        let err = refused(r.f.resolve(actor, item, state, "again"))?;
        assert!(
            matches!(err, Error::ItemNotOpen { item: i, state: ItemState::Answered, resolved: Some(at) } if i == item && at == answered.id),
            "{err}"
        );
        assert_eq!(
            err.to_json(),
            serde_json::json!({ "error": "item-not-open", "message": err.to_string(), "item": item,
                                "state": "answered", "resolved": answered.id })
        );
    }
    Ok(())
}

#[test]
fn resolving_needs_a_reason_and_only_an_answer_refers() -> TestResult {
    let r = roles()?;
    let item = r.f.file(&r.asker, "rev/q", "please")?.item;
    assert!(matches!(refused(r.f.file(&r.asker, "rev/q", " "))?, Error::BadRequest(_)));
    for state in [ItemState::Answered, ItemState::Declined] {
        assert!(matches!(refused(r.f.resolve(&r.queue_owner, item, state, ""))?, Error::BadRequest(_)));
    }
    for state in [ItemState::Open, ItemState::Reassigned] {
        assert!(matches!(refused(r.f.resolve(&r.queue_owner, item, state, "x"))?, Error::BadRequest(_)));
    }
    let message = r.f.post(&r.queue_owner, "rev/t", "the long answer")?.id;
    let declined_with_ref = Resolve { item, state: ItemState::Declined, body: "no", reference: Some(message) };
    assert!(matches!(refused(submit!(r.f.board, &r.queue_owner, resolve, declined_with_ref))?, Error::BadRequest(_)));
    let not_a_message = Resolve { item, state: ItemState::Answered, body: "see", reference: Some(item) };
    assert!(matches!(refused(submit!(r.f.board, &r.queue_owner, resolve, not_a_message))?, Error::BadRequest(_)));
    let answer = Resolve { item, state: ItemState::Answered, body: "see the thread", reference: Some(message) };
    submit!(r.f.board, &r.queue_owner, resolve, answer)?;
    let resolution = r.f.board.item(item)?.ok_or("no item")?.item.resolution.ok_or("unresolved")?;
    assert_eq!((resolution.event, resolution.reference), (EventType::ItemAnswered, Some(message)));
    assert_eq!(resolution.from.as_deref(), Some("owner-agent"));
    // a cancel needs no reason
    let second = r.f.file(&r.asker, "rev/q", "never mind")?.item;
    r.f.resolve(&r.asker, second, ItemState::Cancelled, "")?;
    assert!(matches!(refused(r.f.resolve(&r.asker, 99_999, ItemState::Cancelled, ""))?, Error::NotFound(_)));
    Ok(())
}

#[test]
fn items_list_by_queue_owner_asker_and_state() -> TestResult {
    let r = roles()?;
    r.f.queue(&r.other, "rev/other-q", "")?;
    let one = r.f.file(&r.asker, "rev/q", "one")?.item;
    let two = r.f.file(&r.asker, "rev/other-q", "two")?.item;
    let three = r.f.file(&r.other, "rev/q", "three")?.item;
    r.f.resolve(&r.queue_owner, one, ItemState::Answered, "done")?;
    let ids = |items: Vec<Item>| items.into_iter().map(|i| i.id).collect::<Vec<_>>();
    assert_eq!(ids(r.f.board.queue("rev/q", StateFilter::Open)?), [three]);
    assert_eq!(ids(r.f.board.queue("rev/q", StateFilter::All)?), [one, three]);
    let asked = ItemQuery { asker: Some("asker"), ..ItemQuery::default() };
    assert_eq!(ids(r.f.board.items(&asked)?), [two]);
    assert_eq!(ids(r.f.board.items(&ItemQuery { state: StateFilter::All, ..asked })?), [one, two]);
    let owned = ItemQuery { owner: Some("owner-agent"), state: StateFilter::All, ..ItemQuery::default() };
    assert_eq!(ids(r.f.board.items(&owned)?), [one, three]);
    let detail = r.f.board.item(one)?.ok_or("no item")?;
    let events: Vec<EventType> = detail.events.iter().map(|e| e.event).collect();
    assert_eq!(events, [EventType::ItemFiled, EventType::ItemAnswered]);
    let item = detail.item;
    assert_eq!(
        (item.path.as_str(), item.owner.as_str(), item.asker.as_deref(), item.body.as_str()),
        ("rev/q", "owner-agent", Some("asker"), "one")
    );
    Ok(())
}
