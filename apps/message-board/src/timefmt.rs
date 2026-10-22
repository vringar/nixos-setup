//! The board's timestamps: UTC, to the second, as `2026-10-21T23:58:26Z`,
//! and the HTTP `Date` header's form of the same instant.

use std::time::{SystemTime, SystemTimeError, UNIX_EPOCH};

/// A date in the proleptic Gregorian calendar.
struct Civil {
    year: u64,
    month: u64,
    day: u64,
}

/// The calendar date `days` after 1970-01-01 (Howard Hinnant's
/// `civil_from_days`, for non-negative days).
fn civil_from_days(days: u64) -> Civil {
    let z = days + 719_468;
    let era = z / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + u64::from(month <= 2);
    Civil { year, month, day }
}

/// Seconds since the epoch as `YYYY-MM-DDTHH:MM:SSZ`.
#[must_use]
pub fn format(secs: u64) -> String {
    let Civil { year, month, day } = civil_from_days(secs / 86_400);
    let rem = secs % 86_400;
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

/// Seconds since the epoch as an HTTP date: `Wed, 21 Oct 2026 23:58:26 GMT`.
#[must_use]
pub fn http_date(secs: u64) -> String {
    let days = secs / 86_400;
    let Civil { year, month, day } = civil_from_days(days);
    let weekday = match days % 7 {
        0 => "Thu",
        1 => "Fri",
        2 => "Sat",
        3 => "Sun",
        4 => "Mon",
        5 => "Tue",
        _ => "Wed",
    };
    let month = match month {
        1 => "Jan",
        2 => "Feb",
        3 => "Mar",
        4 => "Apr",
        5 => "May",
        6 => "Jun",
        7 => "Jul",
        8 => "Aug",
        9 => "Sep",
        10 => "Oct",
        11 => "Nov",
        _ => "Dec",
    };
    let rem = secs % 86_400;
    format!("{weekday}, {day:02} {month} {year:04} {:02}:{:02}:{:02} GMT", rem / 3600, rem % 3600 / 60, rem % 60)
}

/// Seconds since the epoch, now.
///
/// # Errors
/// If the system clock is set before 1970.
pub fn unix_now() -> Result<u64, SystemTimeError> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs())
}

/// The current time as a board timestamp.
///
/// # Errors
/// If the system clock is set before 1970.
pub fn now() -> Result<String, SystemTimeError> {
    unix_now().map(format)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_known_instants() {
        assert_eq!(format(0), "1970-01-01T00:00:00Z");
        assert_eq!(format(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(format(1_792_626_706), "2026-10-21T23:51:46Z");
        assert_eq!(format(4_107_542_399), "2100-02-28T23:59:59Z");
        assert_eq!(http_date(784_111_777), "Sun, 06 Nov 1994 08:49:37 GMT");
        assert_eq!(http_date(1_792_626_706), "Wed, 21 Oct 2026 23:51:46 GMT");
    }
}
