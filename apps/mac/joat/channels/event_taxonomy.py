"""The single machine taxonomy for an observed notification.

Two consumers need to name the same notification: the card that reaches the
glasses, and the durable observation row that records it. They used to classify
independently — one from the raw bundle identifier, the other from the
humanized channel — which made them disagree about the same event ("com.apple.iCal"
is not the substring "calendar", and an email was being called an incoming text
message). Two names for one fact is worse than one imprecise name.

This module holds no imports of its own so both sides can depend on it without
the import cycle that kept them apart (`persistence.notifications` already
imports `Notification` from `channels.notifications`).
"""

from __future__ import annotations


def classify_event_class(app: str, title: str) -> str:
    """Name what KIND of thing happened, from the raw native identity.

    Deliberately matches on the unhumanized app identifier: humanization is a
    display concern that can change without the taxonomy changing under it.

    Slack and Discord are `notifications.banner`, not `messages.incoming_text`
    — a deliberate choice, pinned by test. Their content reaches this Mac only
    as Notification Center banners, while `messages.incoming_text` is reserved
    for the authoritative iMessage lane; the durable observation rows have
    always said banner for them, and the card must agree with the row.
    (Display is separate: PERSON_CHANNELS still puts the sender's name in the
    card header for Slack/Discord.)
    """
    identity = f"{app} {title}".casefold()
    if "mobilesms" in identity or "messages" in identity:
        return "messages.incoming_text"
    if "reminder" in identity:
        return "reminders.banner"
    if "calendar" in identity or "ical" in identity:
        return "calendar.banner"
    if "mail" in identity:
        return "mail.banner"
    return "notifications.banner"
