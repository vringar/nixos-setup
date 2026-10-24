//! The blob index: what pins a blob, how references are checked and recorded, who deletes what, and
//! what is evicted when. The files are the server's (see the harness tests); here a blob is a row.

use super::*;
use crate::blob;

fn h(n: u8) -> String {
    format!("{n:064x}")
}

fn reference(n: u8) -> String {
    blob::reference(&h(n))
}

fn put(hash: &str, size: u64) -> PutBlob<'_> {
    PutBlob { sha256: hash, size, media_type: "text/plain", filename: "f.txt" }
}

fn limits(cap: u64, grace: u64) -> Limits {
    Limits { cap, max: 1 << 30, grace }
}

impl Fixture {
    /// `begin`, and for a blob that needs landing, make room and land it: what the server does.
    fn put_blob(&self, by: &PrivateKey, hash: &str, size: u64, limits: &Limits) -> Result<BlobInfo, Error> {
        let request = Request::new(&self.board, by, put(hash, size))?;
        match self.board.blob_begin(&request.signed(), limits, 0)? {
            Begun::Existed(info) => Ok(*info),
            Begun::Proceed(uploader) => {
                self.board.blob_make_room(size, 0, limits)?;
                self.board.blob_land(&Landing {
                    put: put(hash, size),
                    uploader: &uploader,
                    nonce: &request.nonce,
                    signature: &request.signature,
                })
            }
        }
    }

    fn delete_blob(&self, by: &PrivateKey, hash: &str) -> Result<(), Error> {
        submit!(self.board, by, blob_delete, DeleteBlob { sha256: hash })
    }

    fn held(&self) -> Result<Vec<String>, Box<dyn StdError>> {
        let page = self.board.blobs(&BlobQuery {
            unreferenced: false,
            evictable: false,
            mine: None,
            limit: 1000,
            after: None,
            grace: 3600,
        })?;
        let mut hashes: Vec<String> = page.blobs.into_iter().map(|b| b.hash).collect();
        hashes.sort();
        Ok(hashes)
    }

    /// A board with `rev`, alice, bob, a thread `rev/log`, bob's queue and a note `rev/state`.
    fn desk(&self) -> Result<(PrivateKey, PrivateKey), Box<dyn StdError>> {
        self.rev()?;
        let (alice, bob) = (self.agent(2, "alice")?, self.agent(3, "bob")?);
        self.create(&alice, "rev/log", NodeKind::Thread)?;
        self.create(&alice, "rev/state", NodeKind::Note)?;
        self.queue(&bob, "rev/bob", "")?;
        Ok((alice, bob))
    }
}

fn in_use(result: Result<(), Error>) -> Result<usize, String> {
    match refused(result)? {
        Error::BlobInUse { total, .. } => Ok(total),
        other => Err(format!("not blob-in-use: {other}")),
    }
}

#[test]
fn a_message_an_open_item_and_a_notes_latest_version_pin_and_nothing_else_does() -> TestResult {
    let f = fixture()?;
    let (alice, bob) = f.desk()?;
    let all = limits(1 << 30, 3600);
    for n in 1..=4 {
        f.put_blob(&alice, &h(n), 100, &all)?;
    }
    // a thread message pins until it is archived
    let message = f.post(&alice, "rev/log", &format!("see {}", reference(1)))?.id;
    assert_eq!(in_use(f.delete_blob(&alice, &h(1)))?, 1);
    assert_eq!(in_use(f.delete_blob(&f.owner, &h(1)))?, 1);
    f.archive(&f.owner, message, true, "")?;
    f.delete_blob(&alice, &h(1))?;
    // an open item pins; the answer's own reference pins nothing, and answering releases the filing's
    let item = f.file(&alice, "rev/bob", &format!("look {}", reference(2)))?.item;
    assert_eq!(in_use(f.delete_blob(&alice, &h(2)))?, 1);
    let answer = f.resolve(&bob, item, ItemState::Answered, &format!("see {}", reference(3)))?.id;
    f.delete_blob(&alice, &h(2))?;
    f.delete_blob(&alice, &h(3))?;
    // the latest version of a note pins, and an older one does not
    let v1 = f.set_note(&alice, "rev/state", &format!("v1 {}", reference(4)), 0, None)?.id;
    assert_eq!(in_use(f.delete_blob(&alice, &h(4)))?, 1);
    let v2 = f.set_note(&alice, "rev/state", &format!("v2 {}", reference(4)), 1, None)?.id;
    assert_eq!(in_use(f.delete_blob(&alice, &h(4)))?, 1);
    f.set_note(&alice, "rev/state", "no files", 2, None)?;
    f.delete_blob(&alice, &h(4))?;
    // the rows stay, for display: every one of them reads as gone now
    let refs = f.board.blob_refs(&[message, item, answer, v1, v2])?;
    assert_eq!(refs.len(), 5);
    assert!(refs.iter().all(|r| r.state == RefState::Gone && r.size.is_none() && r.filename.is_none()));
    assert!(refs.iter().filter(|r| r.event == message).all(|r| r.archived));
    assert_eq!(f.held()?, Vec::<String>::new());
    Ok(())
}

#[test]
fn a_reference_reads_pinned_released_or_gone_by_what_pins_the_blob_now() -> TestResult {
    let f = fixture()?;
    let (alice, bob) = f.desk()?;
    let all = limits(1 << 30, 3600);
    f.put_blob(&alice, &h(1), 100, &all)?;
    let item = f.file(&alice, "rev/bob", &reference(1))?.item;
    let state = |event| -> Result<RefState, Box<dyn StdError>> {
        Ok(f.board.blob_refs(&[event])?.first().map(|r| r.state).ok_or("no reference")?)
    };
    assert_eq!(state(item)?, RefState::Pinned);
    f.resolve(&bob, item, ItemState::Declined, "no")?;
    assert_eq!(state(item)?, RefState::Released);
    // another holder pins the same blob: the file cannot go, so it reads pinned
    f.post(&alice, "rev/log", &reference(1))?;
    assert_eq!(state(item)?, RefState::Pinned);
    let present = f.board.blobs_present(&[h(1), h(2), "nonsense".to_owned()])?;
    assert_eq!(present.get(&h(1)), Some(&(100, "f.txt".to_owned())));
    assert_eq!(present.len(), 1);
    Ok(())
}

#[test]
fn a_body_with_a_bad_or_unknown_reference_writes_nothing() -> TestResult {
    let f = fixture()?;
    let (alice, bob) = f.desk()?;
    f.put_blob(&alice, &h(1), 100, &limits(1 << 30, 3600))?;
    let item = f.file(&alice, "rev/bob", "plain")?.item;
    let head = f.board.head()?;
    let unknown = refused(f.post(&alice, "rev/log", &reference(9)))?;
    assert!(matches!(&unknown, Error::UnknownBlob { hash } if *hash == h(9)), "{unknown}");
    assert_eq!((unknown.code(), unknown.status()), ("unknown-blob", 400));
    for body in [format!("{}0", reference(1)), "board-blob://abc".to_owned(), format!("{}.bin", reference(1))] {
        let bad = refused(f.post(&alice, "rev/log", &body))?;
        assert_eq!((bad.code(), bad.status()), ("bad-reference", 400), "{body}");
    }
    assert!(matches!(refused(f.resolve(&bob, item, ItemState::Answered, &reference(9)))?, Error::UnknownBlob { .. }));
    assert!(matches!(refused(f.file(&alice, "rev/bob", &reference(9)))?, Error::UnknownBlob { .. }));
    assert!(matches!(refused(f.set_note(&alice, "rev/state", &reference(9), 0, None))?, Error::UnknownBlob { .. }));
    let first = Create { body: &reference(9), ..create("rev/fresh", NodeKind::Note) };
    assert!(matches!(refused(submit!(f.board, &alice, create, first))?, Error::UnknownBlob { .. }));
    assert_eq!(f.board.head()?, head);
    // the board's own notes are never scanned: a revoked key's name could be anything
    assert!(f.board.note(TRUST_NOTE, None)?.is_some());
    Ok(())
}

#[test]
fn a_blob_is_deleted_by_its_first_uploader_or_the_owner() -> TestResult {
    let f = fixture()?;
    let (alice, bob) = f.desk()?;
    let all = limits(1 << 30, 3600);
    f.put_blob(&alice, &h(1), 100, &all)?;
    f.put_blob(&bob, &h(1), 100, &all)?; // the same bytes again: alice stays the uploader
    assert!(matches!(refused(f.delete_blob(&bob, &h(1)))?, Error::Forbidden(_)));
    f.delete_blob(&alice, &h(1))?;
    assert!(matches!(refused(f.delete_blob(&alice, &h(1)))?, Error::NotFound(_)));
    f.put_blob(&bob, &h(2), 100, &all)?;
    f.delete_blob(&f.owner, &h(2))?;
    assert!(matches!(refused(f.delete_blob(&alice, "nonsense"))?, Error::BadRequest(_)));
    Ok(())
}

#[test]
fn a_blob_must_be_nonempty_within_the_limit_and_addressed_by_a_hash() -> TestResult {
    let f = fixture()?;
    let (alice, _bob) = f.desk()?;
    let small = Limits { cap: 1 << 30, max: 1000, grace: 3600 };
    assert!(matches!(refused(f.put_blob(&alice, &h(1), 0, &small))?, Error::BadRequest(_)));
    assert!(matches!(refused(f.put_blob(&alice, "ABC", 10, &small))?, Error::BadRequest(_)));
    let big = refused(f.put_blob(&alice, &h(1), 1001, &small))?;
    assert!(matches!(big, Error::BlobTooLarge { size: 1001, max: 1000 }));
    assert_eq!((big.code(), big.status()), ("blob-too-large", 413));
    f.put_blob(&alice, &h(1), 1000, &small)?;
    Ok(())
}

#[test]
fn room_is_made_by_evicting_the_oldest_unpinned_and_pinned_blobs_stay() -> TestResult {
    let f = fixture()?;
    let (alice, _bob) = f.desk()?;
    let tight = limits(1000, 0); // no grace: every unpinned blob may go
    f.put_blob(&alice, &h(1), 400, &tight)?;
    f.put_blob(&alice, &h(2), 400, &tight)?;
    // 800 used: a third needs 200 freed, and the oldest upload goes (a tie is the smaller hash)
    f.put_blob(&alice, &h(3), 400, &tight)?;
    assert_eq!(f.held()?, [h(2), h(3)]);
    // pinned ones are never taken
    f.post(&alice, "rev/log", &reference(2))?;
    f.put_blob(&alice, &h(4), 400, &tight)?;
    assert_eq!(f.held()?, [h(2), h(4)]);
    f.post(&alice, "rev/log", &reference(4))?;
    let full = refused(f.put_blob(&alice, &h(5), 400, &tight))?;
    let Error::BlobStoreFull(full) = full else { return Err("not blob-store-full".into()) };
    assert_eq!((full.used, full.cap, full.needed, full.evictable, full.pinned), (800, 1000, 400, 0, 800));
    assert_eq!(full.pinned_by, [Pinner { path: "rev/log".to_owned(), bytes: 800, blobs: 2 }]);
    assert_eq!(f.held()?, [h(2), h(4)]);
    // what other uploads in flight promised counts
    let six = h(6);
    let request = Request::new(&f.board, &alice, put(&six, 100))?;
    assert!(matches!(f.board.blob_begin(&request.signed(), &limits(1000, 0), 0)?, Begun::Proceed(_)));
    let busy = Request::new(&f.board, &alice, put(&six, 100))?;
    assert!(matches!(refused(f.board.blob_begin(&busy.signed(), &limits(1000, 0), 300))?, Error::BlobStoreFull(_)));
    Ok(())
}

#[test]
fn nothing_is_evicted_within_the_grace_or_at_planning() -> TestResult {
    let f = fixture()?;
    let (alice, _bob) = f.desk()?;
    let kept = limits(1000, 3600);
    f.put_blob(&alice, &h(1), 600, &kept)?;
    let full = refused(f.put_blob(&alice, &h(2), 600, &kept))?;
    let Error::BlobStoreFull(full) = full else { return Err("not blob-store-full".into()) };
    assert_eq!((full.evictable, full.in_grace, full.pinned), (0, 600, 0));
    assert!(full.to_string().contains("under the 1 h grace"), "{full}");
    // begin plans only: with no grace the blob could go, and still it is there until make_room
    let two = h(2);
    let request = Request::new(&f.board, &alice, put(&two, 600))?;
    assert!(matches!(f.board.blob_begin(&request.signed(), &limits(1000, 0), 0)?, Begun::Proceed(_)));
    assert_eq!(f.held()?, [h(1)]);
    assert!(matches!(refused(f.board.blob_make_room(600, 0, &kept).map(drop))?, Error::BlobStoreFull(_)));
    assert_eq!(f.board.blob_make_room(600, 0, &limits(1000, 0))?.len(), 1);
    assert_eq!(f.held()?, Vec::<String>::new());
    Ok(())
}

#[test]
fn hard_deleting_a_message_or_a_branch_drops_its_references() -> TestResult {
    let f = fixture()?;
    let (alice, _bob) = f.desk()?;
    let all = limits(1 << 30, 3600);
    f.put_blob(&alice, &h(1), 100, &all)?;
    let message = f.post(&alice, "rev/log", &reference(1))?.id;
    assert_eq!(in_use(f.delete_blob(&alice, &h(1)))?, 1);
    f.board.delete_message(message)?;
    assert_eq!(f.board.blob_refs(&[message])?, []);
    f.delete_blob(&alice, &h(1))?;
    f.put_blob(&alice, &h(1), 100, &all)?;
    f.post(&alice, "rev/log", &reference(1))?;
    f.board.delete_branch("rev/log")?;
    f.delete_blob(&alice, &h(1))?;
    Ok(())
}

#[test]
fn unarchiving_refuses_to_bring_back_a_reference_to_a_file_that_is_gone() -> TestResult {
    let f = fixture()?;
    let (alice, bob) = f.desk()?;
    let all = limits(1 << 30, 3600);
    f.put_blob(&alice, &h(1), 100, &all)?;
    let message = f.post(&alice, "rev/log", &reference(1))?.id;
    f.archive(&f.owner, message, true, "")?;
    f.delete_blob(&alice, &h(1))?;
    let gone = refused(f.archive(&f.owner, message, false, ""))?;
    assert!(matches!(&gone, Error::BlobGone { hashes } if *hashes == [h(1)]), "{gone}");
    assert_eq!((gone.code(), gone.status()), ("blob-gone", 409));
    f.put_blob(&alice, &h(1), 100, &all)?;
    assert!(!f.archive(&f.owner, message, false, "")?.hidden);
    // an item: its resolution's references count too
    f.put_blob(&alice, &h(2), 100, &all)?;
    let item = f.file(&alice, "rev/bob", "plain")?.item;
    f.resolve(&bob, item, ItemState::Answered, &reference(2))?;
    f.archive(&f.owner, item, true, "")?;
    f.delete_blob(&alice, &h(2))?;
    assert!(matches!(refused(f.archive(&f.owner, item, false, ""))?, Error::BlobGone { .. }));
    Ok(())
}

#[test]
fn blobs_list_newest_first_by_filter_and_page() -> TestResult {
    let f = fixture()?;
    let (alice, bob) = f.desk()?;
    let all = limits(1 << 30, 0);
    for n in 1..=3 {
        f.put_blob(&alice, &h(n), 100, &all)?;
    }
    f.put_blob(&bob, &h(4), 200, &all)?;
    f.post(&alice, "rev/log", &reference(1))?;
    let query = |unreferenced, evictable, mine: Option<&str>, limit, after: Option<&str>| {
        let page = f.board.blobs(&BlobQuery { unreferenced, evictable, mine, limit, after, grace: 0 })?;
        Ok::<_, Error>((page.blobs.into_iter().map(|b| b.hash).collect::<Vec<_>>(), page.truncated))
    };
    // one second resolution: the upload order inside a second is by hash, newest (largest) first
    assert_eq!(query(false, false, None, 10, None)?.0, [h(4), h(3), h(2), h(1)]);
    assert_eq!(query(true, false, None, 10, None)?.0, [h(4), h(3), h(2)]);
    assert_eq!(query(false, true, None, 10, None)?.0, [h(4), h(3), h(2)]);
    let mine = alice.fingerprint();
    assert_eq!(query(false, false, Some(&mine), 10, None)?.0, [h(3), h(2), h(1)]);
    assert_eq!(query(false, false, None, 2, None)?, (vec![h(4), h(3)], true));
    assert_eq!(query(false, false, None, 2, Some(&h(3)))?, (vec![h(2), h(1)], false));
    assert!(matches!(refused(query(false, false, None, 2, Some("zz")).map(drop))?, Error::BadRequest(_)));
    let stats = f.board.blob_stats()?;
    assert_eq!((stats.used, stats.count, stats.pinned_bytes), (500, 4, 100));
    let info = f.board.blob(&h(1))?.ok_or("no blob")?;
    assert_eq!((info.refs, info.pinned, info.from.as_str(), info.filename.as_str()), (1, true, "alice", "f.txt"));
    assert_eq!(info.url, format!("/sha256/{}", h(1)));
    Ok(())
}

#[test]
fn the_operator_deletes_a_blob_nothing_pins_with_no_key_and_never_one_that_is_pinned() -> TestResult {
    let f = fixture()?;
    let (alice, _bob) = f.desk()?;
    let all = limits(1 << 30, 3600);
    f.put_blob(&alice, &h(1), 100, &all)?;
    f.put_blob(&alice, &h(2), 100, &all)?;
    let message = f.post(&alice, "rev/log", &reference(1))?.id;
    let in_use = refused(f.board.blob_delete_unsigned(&h(1)))?;
    assert!(matches!(in_use, Error::BlobInUse { total: 1, .. }), "{in_use}");
    f.board.blob_delete_unsigned(&h(2))?;
    assert!(matches!(refused(f.board.blob_delete_unsigned(&h(2)))?, Error::NotFound(_)));
    assert!(matches!(refused(f.board.blob_delete_unsigned("nonsense"))?, Error::BadRequest(_)));
    // once its holder is archived the blob can go
    f.archive(&f.owner, message, true, "")?;
    f.board.blob_delete_unsigned(&h(1))?;
    assert_eq!(f.held()?, Vec::<String>::new());
    Ok(())
}

#[test]
fn a_blob_names_the_events_that_reference_it_and_whether_each_still_pins_it() -> TestResult {
    let f = fixture()?;
    let (alice, bob) = f.desk()?;
    f.put_blob(&alice, &h(1), 100, &limits(1 << 30, 3600))?;
    let item = f.file(&alice, "rev/bob", &reference(1))?.item;
    let message = f.post(&alice, "rev/log", &reference(1))?.id;
    f.resolve(&bob, item, ItemState::Declined, "no")?;
    let referrers = f.board.blob_referrers(&h(1))?;
    let seen: Vec<(i64, &str, bool)> = referrers.iter().map(|r| (r.event, r.path.as_str(), r.pinning)).collect();
    assert_eq!(seen, [(item, "rev/bob", false), (message, "rev/log", true)]);
    assert_eq!(f.board.blob_referrers(&h(2))?, []);
    Ok(())
}

#[test]
fn the_index_lists_every_blob_and_a_pinned_reference_without_one_is_found() -> TestResult {
    let f = fixture()?;
    let (alice, _bob) = f.desk()?;
    let all = limits(1 << 30, 3600);
    f.put_blob(&alice, &h(2), 20, &all)?;
    f.put_blob(&alice, &h(1), 10, &all)?;
    assert_eq!(f.board.blob_index()?, [(h(1), 10), (h(2), 20)]);
    let message = f.post(&alice, "rev/log", &reference(1))?.id;
    assert_eq!(f.board.pinned_without_blob()?, []);
    // a row that vanishes from under a holder, as only a fault could make it
    let conn = rusqlite::Connection::open(&f.path)?;
    conn.execute("DELETE FROM blobs WHERE sha256 = ?", [h(1)])?;
    assert_eq!(f.board.pinned_without_blob()?, [(message, h(1))]);
    Ok(())
}
