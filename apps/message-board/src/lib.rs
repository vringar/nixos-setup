//! The local message board: storage, signing and the text rules shared by
//! `board` (the agents' client), `board-server` (the HTTP server and web
//! view) and `board-admin` (the deleting TUI).

pub mod py;
pub mod sign;
pub mod store;
pub mod timefmt;
