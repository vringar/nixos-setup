//! The local message board: the storage and signing shared by
//! `board` (the agents' client), `board-server` (the HTTP server and web
//! view) and `board-admin` (the TUI that browses and deletes).

pub mod sign;
pub mod store;
pub mod text;
pub mod timefmt;
