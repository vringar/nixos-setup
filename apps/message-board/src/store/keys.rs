//! Keys: who may write, and under which name.
//!
//! A name is bound by the owner approving a specific key. A key the board
//! has never seen may only [`Board::register`], which files a registration
//! in the owner's queue (`_board/register`); [`Board::approve`] binds the
//! name to the key, and [`Board::revoke`] withdraws it. Events a key
//! signed before its revocation stay, and show `revoked_at`.
//!
//! One name per key, one live key per name: approving a key for a bound
//! name replaces (revokes) the key that had it, and declines every other
//! registration waiting for that name.
//!
//! The owner's key is the one in the board's configuration
//! ([`Board::set_owner`], at every start), never a registration. It is the
//! "human" of the permission rules.
//!
//! `_board/trust` is the trust list as a note, rewritten by the board at
//! every change: the event of the version that revoked a key is that key's
//! `revoked_at`.

use std::fmt;
use std::str::FromStr;

use rusqlite::{Connection, OptionalExtension as _, Row, params};
use serde::{Deserialize, Serialize};

use super::actions::{Approve, Register, Registered, Resolved, Revoke, Revoked, Signed, Stamp, Writer};
use super::events::{self, EventType, NewEvent, parsed};
use super::items::{self, ItemState};
use super::nodes;
use super::{Board, DbError, Error, REGISTER_QUEUE, TRUST_NOTE, check_name, check_reserved, looks_alike, quoted, text};
use crate::sign::Verified;
use crate::text::one_line;

/// The most registrations waiting at once, board-wide: the board listens on
/// the tailnet, and this bounds a flood from there as well as from the host.
pub(super) const REGISTER_OPEN_MAX: i64 = 20;
/// The longest registration note and repo, in bytes.
pub const REGISTER_TEXT_MAX: usize = 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum KeyState {
    /// The configured owner key.
    Owner,
    /// Registered, waiting for the owner.
    Pending,
    /// Approved: its name is bound to it.
    Trusted,
    /// Its registration was declined; it may register again.
    Declined,
    /// No longer trusted, for good: a new key must register.
    Revoked,
}

impl KeyState {
    pub const ALL: [Self; 5] = [Self::Owner, Self::Pending, Self::Trusted, Self::Declined, Self::Revoked];

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Owner => "owner",
            Self::Pending => "pending",
            Self::Trusted => "trusted",
            Self::Declined => "declined",
            Self::Revoked => "revoked",
        }
    }
}

impl fmt::Display for KeyState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for KeyState {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        Self::ALL.into_iter().find(|state| state.as_str() == s).ok_or_else(|| format!("no key state {s:?}"))
    }
}

/// A key the board knows.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Key {
    pub fingerprint: String,
    /// `ssh-ed25519 AAAA...`
    pub pubkey: String,
    /// The name it asked for, or is bound to.
    pub name: String,
    pub state: KeyState,
    pub repo: String,
    pub note: String,
    /// Its latest registration.
    pub item: Option<i64>,
    /// The event that approved it.
    pub approved: Option<i64>,
    /// The event that revoked it.
    pub revoked: Option<i64>,
}

/// The owner key: `name` and `fingerprint`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Owner {
    pub name: String,
    pub fingerprint: String,
}

/// A registration waiting for the owner, with what the owner should know.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Registration {
    pub item: i64,
    pub ts: String,
    pub name: String,
    pub fingerprint: String,
    pub repo: String,
    pub note: String,
    /// The key bound to `name` now, which approving this one revokes.
    pub replaces: Option<String>,
    /// Other names waiting that look like this one: a squatter, or a typo.
    pub look_alikes: Vec<String>,
}

/// What [`Board::set_owner`] did.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct OwnerChange {
    /// False if the configured key already was the owner's, under this name.
    pub changed: bool,
    /// The keys revoked to make way: a previous owner key, or a key
    /// approved for the owner's name.
    pub revoked: Vec<String>,
}

/// A key that may write: trusted, or the owner's.
#[derive(Debug, Clone)]
pub(super) struct Author {
    pub name: String,
    pub fingerprint: String,
    /// The owner key.
    pub human: bool,
}

impl Writer for Author {
    fn actor(&self) -> Option<&str> {
        Some(&self.name)
    }
    fn fingerprint(&self) -> &str {
        &self.fingerprint
    }
}

const KEY_SELECT: &str = "SELECT fingerprint, pubkey, name, state, repo, note, item, approved, revoked FROM keys";

fn key_row(r: &Row<'_>) -> rusqlite::Result<Key> {
    Ok(Key {
        fingerprint: text(r, 0)?,
        pubkey: text(r, 1)?,
        name: text(r, 2)?,
        state: parsed(r, 3)?,
        repo: text(r, 4)?,
        note: text(r, 5)?,
        item: r.get(6)?,
        approved: r.get(7)?,
        revoked: r.get(8)?,
    })
}

fn get(conn: &Connection, fingerprint: &str) -> rusqlite::Result<Option<Key>> {
    conn.query_row(&format!("{KEY_SELECT} WHERE fingerprint=?"), [fingerprint], key_row).optional()
}

fn select(conn: &Connection, filter: &str, args: impl rusqlite::Params) -> rusqlite::Result<Vec<Key>> {
    conn.prepare(&format!("{KEY_SELECT} {filter}"))?.query_map(args, key_row)?.collect()
}

/// The keys names are bound to: the owner's first, then by name.
fn bound(conn: &Connection) -> rusqlite::Result<Vec<Key>> {
    select(conn, "WHERE state IN ('owner', 'trusted') ORDER BY state <> 'owner', name", [])
}

fn owner(conn: &Connection) -> rusqlite::Result<Option<Key>> {
    Ok(select(conn, "WHERE state='owner'", [])?.into_iter().next())
}

/// Let a trusted key write, as its bound name; refuse any other, saying
/// what it would take.
pub(super) fn admit(conn: &Connection, verified: Verified) -> Result<Author, Error> {
    let Some(key) = get(conn, &verified.fingerprint)? else {
        return Err(Error::NotRegistered { fingerprint: verified.fingerprint });
    };
    // The schema's CHECKs give every pending and declined key its item and
    // every revoked key its event, so the defaults are never taken.
    match key.state {
        KeyState::Owner | KeyState::Trusted => {
            Ok(Author { human: key.state == KeyState::Owner, name: key.name, fingerprint: key.fingerprint })
        }
        KeyState::Pending => Err(Error::RegistrationPending { item: key.item.unwrap_or_default() }),
        KeyState::Declined => Err(Error::RegistrationDeclined { item: key.item.unwrap_or_default() }),
        KeyState::Revoked => Err(Error::KeyRevoked { revoked_at: key.revoked.unwrap_or_default() }),
    }
}

/// Decline the key whose registration `item` was declined.
pub(super) fn decline_registration(conn: &Connection, item: i64) -> rusqlite::Result<()> {
    conn.execute("UPDATE keys SET state='declined' WHERE item=? AND state='pending'", [item])?;
    Ok(())
}

/// A new trust list version, in three steps, since its event, the key
/// changes and its body each need the one before: [`begin_trust`] appends
/// the event, the caller changes keys ([`revoke_keys`] with the event's
/// id), and [`finish_trust`] writes the list as it now stands.
fn begin_trust(conn: &Connection, stamp: &Stamp<'_>) -> Result<i64, Error> {
    let note = nodes::require(conn, TRUST_NOTE)?;
    let version = nodes::current_version(conn, note.id)?.checked_add(1).ok_or(DbError::Exhausted)?;
    let event = NewEvent { version: Some(version), ..NewEvent::new(EventType::NoteVersion, note.id, "") };
    Ok(events::insert(conn, stamp, &event)?)
}

fn revoke_keys(conn: &Connection, fingerprints: &[String], at: i64) -> rusqlite::Result<()> {
    for fingerprint in fingerprints {
        conn.execute("UPDATE keys SET state='revoked', revoked=? WHERE fingerprint=?", params![at, fingerprint])?;
    }
    Ok(())
}

/// The trust list as of now, headed by what changed in this version.
fn finish_trust(conn: &Connection, event: i64, changes: &[String]) -> rusqlite::Result<()> {
    let mut lines =
        vec!["The keys the board trusts; the board rewrites this note at every approve and revoke.".to_owned()];
    lines.push(String::new());
    lines.extend(changes.iter().map(|change| format!("change: {change}")));
    lines.push(String::new());
    lines.extend(bound(conn)?.into_iter().map(|key| match key.approved {
        Some(approved) => format!("{} {} {} approved #{approved}", key.state, key.name, key.fingerprint),
        None => format!("{} {} {}", key.state, key.name, key.fingerprint),
    }));
    conn.execute("UPDATE events SET body=? WHERE id=?", params![lines.join("\n") + "\n", event])?;
    Ok(())
}

fn check_owner_key(author: &Author, what: &str) -> Result<(), Error> {
    if !author.human {
        return Err(Error::Forbidden(format!("only the owner key {what}")));
    }
    Ok(())
}

impl Board {
    /// Every key the board knows, by name.
    ///
    /// # Errors
    /// On a database fault.
    pub fn keys(&self) -> Result<Vec<Key>, DbError> {
        self.read_db(|conn| select(conn, "ORDER BY name, fingerprint", []))
    }

    /// One key.
    ///
    /// # Errors
    /// On a database fault.
    pub fn key(&self, fingerprint: &str) -> Result<Option<Key>, DbError> {
        self.read_db(|conn| get(conn, fingerprint))
    }

    /// The owner key, if one is configured.
    ///
    /// # Errors
    /// On a database fault.
    pub fn owner(&self) -> Result<Option<Owner>, DbError> {
        self.read_db(|conn| Ok(owner(conn)?.map(|key| Owner { name: key.name, fingerprint: key.fingerprint })))
    }

    /// The registrations waiting for the owner, oldest first.
    ///
    /// # Errors
    /// On a database fault.
    pub fn registrations(&self) -> Result<Vec<Registration>, DbError> {
        self.read_db(|conn| {
            let pending = select(conn, "WHERE state='pending' ORDER BY item", [])?;
            let bound = bound(conn)?;
            let mut out = Vec::with_capacity(pending.len());
            for key in &pending {
                let item = key.item.unwrap_or_default();
                let ts = conn.query_row("SELECT ts FROM events WHERE id=?", [item], |r| text(r, 0)).optional()?;
                let mut look_alikes: Vec<String> = pending
                    .iter()
                    .filter(|other| other.name != key.name && looks_alike(&other.name, &key.name))
                    .map(|other| other.name.clone())
                    .collect();
                look_alikes.sort();
                look_alikes.dedup();
                out.push(Registration {
                    item,
                    ts: ts.unwrap_or_default(),
                    name: key.name.clone(),
                    fingerprint: key.fingerprint.clone(),
                    repo: key.repo.clone(),
                    note: key.note.clone(),
                    replaces: bound.iter().find(|b| b.name == key.name).map(|b| b.fingerprint.clone()),
                    look_alikes,
                });
            }
            Ok(out)
        })
    }

    /// Ask the owner to bind a name to the signing key: the one write a key
    /// the board does not know may make. A key that is waiting already gets
    /// its waiting registration back.
    ///
    /// # Errors
    /// A refusal of the signature; [`Error::BadRequest`] for a name outside
    /// the rule or a note or repo over [`REGISTER_TEXT_MAX`];
    /// [`Error::Forbidden`] for the board's or the owner's name and for one
    /// that looks like a bound name; [`Error::AlreadyRegistered`] for a
    /// trusted key; [`Error::KeyRevoked`]; [`Error::RegisterFull`].
    pub fn register(&self, signed: &Signed<'_, Register<'_>>) -> Result<Registered, Error> {
        let register = &signed.action;
        check_name(register.name)?;
        check_reserved(register.name)?;
        for (what, value) in [("note", register.note), ("repo", register.repo)] {
            if value.len() > REGISTER_TEXT_MAX {
                return Err(Error::BadRequest(format!("a registration's {what} is at most {REGISTER_TEXT_MAX} bytes")));
            }
        }
        self.signed(
            signed,
            |_, verified| Ok(verified),
            |conn, stamp, verified| {
                let fingerprint = verified.fingerprint.as_str();
                match get(conn, fingerprint)? {
                    Some(key) if key.state == KeyState::Pending => return Ok(key.item.unwrap_or_default()),
                    Some(key) if matches!(key.state, KeyState::Owner | KeyState::Trusted) => {
                        return Err(Error::AlreadyRegistered { name: key.name });
                    }
                    Some(key) if key.state == KeyState::Revoked => {
                        return Err(Error::KeyRevoked { revoked_at: key.revoked.unwrap_or_default() });
                    }
                    _ => {}
                }
                let name = register.name;
                for key in bound(conn)? {
                    if key.state == KeyState::Owner && key.name == name {
                        return Err(Error::Forbidden(format!(
                            "name {} is the board owner's, bound by the configured owner key",
                            quoted(name)
                        )));
                    }
                    if key.name != name && looks_alike(&key.name, name) {
                        return Err(Error::Forbidden(format!(
                            "name {} is too close to the bound name {}; use one at least {} edits from every bound name",
                            quoted(name),
                            quoted(&key.name),
                            super::MIN_DISTANCE
                        )));
                    }
                }
                let queue = nodes::require(conn, REGISTER_QUEUE)?;
                let open: i64 =
                    conn.query_row("SELECT count(*) FROM items WHERE queue=? AND state='open'", [queue.id], |r| r.get(0))?;
                if open >= REGISTER_OPEN_MAX {
                    return Err(Error::RegisterFull);
                }
                let body = format!(
                    "register {name} {fingerprint}\nrepo: {}\nnote: {}",
                    one_line(register.repo),
                    register.note
                );
                let item = items::insert(conn, stamp, queue.id, &body, None)?;
                conn.execute(
                    "INSERT INTO keys(fingerprint, pubkey, name, state, repo, note, item) VALUES(?,?,?,'pending',?,?,?)
                     ON CONFLICT(fingerprint) DO UPDATE SET name=excluded.name, state='pending', repo=excluded.repo,
                       note=excluded.note, item=excluded.item",
                    params![fingerprint, verified.public_line, name, register.repo, register.note, item],
                )?;
                Ok(item)
            },
            |conn, item| {
                let filed = nodes::event_of(conn, item)?;
                let fingerprint = filed.signed_by.unwrap_or_default();
                let name = get(conn, &fingerprint)?.map(|key| key.name).unwrap_or_default();
                Ok(Registered { item, name, fingerprint })
            },
        )
    }

    /// The owner binds a registration's name to its key: in one write the
    /// registration is answered, a key bound to the name before is revoked,
    /// other registrations for the name are declined, and the trust list
    /// gets a new version.
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::Forbidden`] unless the
    /// owner key signed; [`Error::NotFound`]; [`Error::BadRequest`] for an
    /// item that is not a registration, or one for another name or key than
    /// signed; [`Error::ItemNotOpen`].
    pub fn approve(&self, signed: &Signed<'_, Approve<'_>>) -> Result<Resolved, Error> {
        let approve = &signed.action;
        self.signed(
            signed,
            admit,
            |conn, stamp, author| {
                check_owner_key(author, "approves registrations")?;
                let item = items::get(conn, approve.item)?
                    .ok_or_else(|| Error::NotFound(format!("no item #{}", approve.item)))?;
                if item.path != REGISTER_QUEUE {
                    return Err(Error::BadRequest(format!("#{} is not a registration", item.id)));
                }
                items::check_open(&item)?;
                let key = select(conn, "WHERE item=? AND state='pending'", [item.id])?
                    .pop()
                    .ok_or_else(|| Error::NotFound(format!("no key waits on registration #{}", item.id)))?;
                if (key.name.as_str(), key.fingerprint.as_str()) != (approve.name, approve.fingerprint) {
                    return Err(Error::BadRequest(format!(
                        "registration #{} is for {} {}, not {} {}",
                        item.id,
                        key.name,
                        key.fingerprint,
                        quoted(approve.name),
                        approve.fingerprint
                    )));
                }
                if owner(conn)?.is_some_and(|owner| owner.name == key.name) {
                    return Err(Error::Forbidden(format!(
                        "name {} is the board owner's, bound by the configured owner key",
                        quoted(&key.name)
                    )));
                }
                let queue = nodes::require(conn, REGISTER_QUEUE)?;
                let (name, fingerprint) = (key.name.as_str(), key.fingerprint.as_str());
                let approved = format!("approved {name} {fingerprint}");
                let id = items::close(conn, stamp, &item, queue.id, ItemState::Answered, &approved, None)?;
                let later = stamp.later();
                let rivals = select(conn, "WHERE name=? AND state='pending' AND fingerprint<>?", [name, fingerprint])?;
                for rival in rivals {
                    let rival_item = rival.item.and_then(|id| items::get(conn, id).transpose()).transpose()?;
                    if let Some(rival_item) = rival_item.filter(|i| i.state == ItemState::Open) {
                        let why = format!("name {name} bound at #{id}");
                        items::close(conn, &later, &rival_item, queue.id, ItemState::Declined, &why, None)?;
                    }
                    conn.execute("UPDATE keys SET state='declined' WHERE fingerprint=?", [&rival.fingerprint])?;
                }
                let replaced: Vec<String> = select(conn, "WHERE name=? AND state='trusted'", [name])?
                    .into_iter()
                    .map(|k| k.fingerprint)
                    .collect();
                let trust = begin_trust(conn, &later)?;
                revoke_keys(conn, &replaced, trust)?;
                conn.execute(
                    "UPDATE keys SET state='trusted', approved=? WHERE fingerprint=?",
                    params![id, fingerprint],
                )?;
                let mut changes = vec![format!("approved {name} {fingerprint} (registration #{})", item.id)];
                changes.extend(replaced.iter().map(|old| format!("revoked {old} ({name}): replaced by {fingerprint}")));
                finish_trust(conn, trust, &changes)?;
                Ok(id)
            },
            items::resolved,
        )
    }

    /// The owner withdraws a trusted key. Revoking a key that is revoked
    /// already returns that revocation.
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::Forbidden`] unless the
    /// owner key signed, and for the owner key itself; [`Error::NotFound`];
    /// [`Error::BadRequest`] for a key that is not trusted.
    pub fn revoke(&self, signed: &Signed<'_, Revoke<'_>>) -> Result<Revoked, Error> {
        let revoke = &signed.action;
        self.signed(
            signed,
            admit,
            |conn, stamp, author| {
                check_owner_key(author, "revokes keys")?;
                let key = get(conn, revoke.fingerprint)?
                    .ok_or_else(|| Error::NotFound(format!("no key {}", quoted(revoke.fingerprint))))?;
                match key.state {
                    KeyState::Trusted => {}
                    KeyState::Revoked => return Ok(key.revoked.unwrap_or_default()),
                    KeyState::Owner => {
                        return Err(Error::Forbidden(
                            "the owner key is not revoked here: configure another as the owner's and restart the board"
                                .to_owned(),
                        ));
                    }
                    KeyState::Pending | KeyState::Declined => {
                        return Err(Error::BadRequest(format!(
                            "key {} is {}, not trusted; decline its registration instead",
                            key.fingerprint, key.state
                        )));
                    }
                }
                let trust = begin_trust(conn, stamp)?;
                revoke_keys(conn, std::slice::from_ref(&key.fingerprint), trust)?;
                let reason = if revoke.reason.trim().is_empty() {
                    String::new()
                } else {
                    format!(": {}", one_line(revoke.reason.trim()))
                };
                finish_trust(conn, trust, &[format!("revoked {} ({}){reason}", key.fingerprint, key.name)])?;
                Ok(trust)
            },
            |conn, id| {
                let name = get(conn, revoke.fingerprint)?.map(|key| key.name).unwrap_or_default();
                Ok(Revoked { fingerprint: revoke.fingerprint.to_owned(), name, id })
            },
        )
    }

    /// Make `key` the owner key, under `name`, and the register queue the
    /// name's. A previous owner key, and a key approved for the name, are
    /// revoked by the board. At every start but the first (or a change of
    /// configuration) there is nothing to do, and nothing is written, so a
    /// read-only database still starts.
    ///
    /// # Errors
    /// [`Error::BadRequest`] or [`Error::Forbidden`] for a name that cannot
    /// be an owner's, [`Error::Forbidden`] for a revoked key, and on a
    /// database fault.
    pub fn set_owner(&self, name: &str, key: &Verified) -> Result<OwnerChange, Error> {
        check_name(name)?;
        check_reserved(name)?;
        let unchanged = self.read(|conn| {
            let owners = select(conn, "WHERE state='owner'", [])?;
            let register_owner = nodes::require(conn, REGISTER_QUEUE)?.owner;
            Ok(matches!(owners.as_slice(), [only] if only.fingerprint == key.fingerprint && only.name == name)
                && register_owner.as_deref() == Some(name))
        })?;
        if unchanged {
            return Ok(OwnerChange::default());
        }
        self.write(|conn, now| {
            let stamp = Stamp::board(now);
            if let Some(revoked) = get(conn, &key.fingerprint)?.and_then(|k| k.revoked) {
                return Err(Error::Forbidden(format!(
                    "the owner key {} was revoked at #{revoked}; configure a new one",
                    key.fingerprint
                )));
            }
            let replaced: Vec<String> = select(
                conn,
                "WHERE fingerprint<>?1 AND (state='owner' OR (state='trusted' AND name=?2))",
                [key.fingerprint.as_str(), name],
            )?
            .into_iter()
            .map(|k| k.fingerprint)
            .collect();
            let trust = begin_trust(conn, &stamp)?;
            revoke_keys(conn, &replaced, trust)?;
            conn.execute(
                "INSERT INTO keys(fingerprint, pubkey, name, state) VALUES(?,?,?,'owner')
                 ON CONFLICT(fingerprint) DO UPDATE SET pubkey=excluded.pubkey, name=excluded.name, state='owner'",
                params![key.fingerprint, key.public_line, name],
            )?;
            conn.execute("UPDATE nodes SET owner=? WHERE path=?", [name, REGISTER_QUEUE])?;
            let mut changes = vec![format!("owner {name} {}", key.fingerprint)];
            changes.extend(replaced.iter().map(|old| format!("revoked {old}: replaced by the owner key")));
            finish_trust(conn, trust, &changes)?;
            Ok(OwnerChange { changed: true, revoked: replaced })
        })
    }
}
