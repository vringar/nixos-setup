"""Report whether Paperless's IMAP fetcher is actually doing anything.

The failure this exists to catch is a quiet one. When the fetcher works and
finds nothing, it produces no documents; when the mail password has been
changed, or a filter no longer matches, or the rule was left disabled, it also
produces no documents. From the document list the two are indistinguishable,
and the archive looks fine right up until the point you need something that was
never ingested.

paperless_mail.ProcessedMail is the authoritative record of every message the
fetcher has looked at, including the ones it failed on, so this reads that
rather than grepping logs for the absence of errors.

One thing it cannot tell you: ProcessedMail only gains rows when a message is
actually processed, so a quiet mailbox and a fetcher that stopped running look
the same here too. The tail of mail.log at the end covers that gap.

Run it inside Paperless's Django context:

    sudo paperless-manage shell < scripts/check-paperless-mail.py
"""

import os
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from paperless_mail.models import MailAccount
from paperless_mail.models import MailRule
from paperless_mail.models import ProcessedMail

LOG_TAIL_LINES = 15


def describe_age(when):
    """Render a timestamp as an absolute time plus how long ago that was."""
    delta = timezone.now() - when
    hours, seconds = divmod(int(delta.total_seconds()), 3600)
    if hours >= 24:
        ago = f"{hours // 24}d {hours % 24}h ago"
    elif hours:
        ago = f"{hours}h {seconds // 60}m ago"
    else:
        ago = f"{seconds // 60}m ago"
    return f"{when:%Y-%m-%d %H:%M} ({ago})"


def report_accounts():
    accounts = list(MailAccount.objects.all())
    print(f"Mail accounts: {len(accounts)}")
    for account in accounts:
        print(f"  - {account.name}: {account.username}@{account.imap_server}")
    if not accounts:
        print("  none configured - the fetcher has nothing to do")
    return accounts


def report_rules():
    """List rules, calling out disabled ones: a disabled rule fails silently."""
    rules = list(MailRule.objects.all())
    print(f"\nRules: {len(rules)}")
    for rule in rules:
        state = "enabled" if rule.enabled else "DISABLED"
        print(f"  - {rule.name} [{state}] folder={rule.folder!r} account={rule.account.name}")
    if rules and not any(rule.enabled for rule in rules):
        print("  every rule is disabled - nothing will ever be fetched")
    return rules


def report_processed():
    """Summarise what the fetcher has seen, newest failures first."""
    total = ProcessedMail.objects.count()
    print(f"\nProcessed mails: {total}")
    if not total:
        print("  nothing processed yet - either the mailbox is quiet, the")
        print("  filters match nothing, or the fetcher is not running")
        return

    counts = Counter(ProcessedMail.objects.values_list("status", flat=True))
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    newest = ProcessedMail.objects.order_by("-processed").first()
    print(f"  most recent: {describe_age(newest.processed)} - {newest.subject!r}")

    failures = ProcessedMail.objects.filter(status="FAILED").order_by("-processed")[:5]
    if failures:
        print("\nRecent failures:")
        for mail in failures:
            print(f"  {describe_age(mail.processed)} {mail.subject!r}")
            print(f"    {mail.error}")


def report_log_tail():
    """ProcessedMail stays empty on a quiet mailbox; the log shows polling."""
    log = Path(settings.LOGGING_DIR) / "mail.log"
    cron = os.environ.get("PAPERLESS_EMAIL_TASK_CRON", "*/10 * * * *")
    print(f"\nSchedule: {cron}")
    print(f"Last {LOG_TAIL_LINES} lines of {log}:")
    if not log.exists():
        print("  (no mail.log yet - the fetcher has never run)")
        return
    lines = log.read_text(errors="replace").splitlines()
    for line in lines[-LOG_TAIL_LINES:] or ["  (empty)"]:
        print(f"  {line}")


report_accounts()
report_rules()
report_processed()
report_log_tail()
