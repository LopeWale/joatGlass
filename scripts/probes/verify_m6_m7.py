"""Execute the M6 (relationship book) and M7 (long-draft review) claims.

Drives the production MessageReplyController with a fake sender, a
scripted confirmation and a recording reviewer. Nothing is sent.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "mac"))

from joat.channels.contacts import RelationshipBook
from joat.core.protocol import CONFIRM_PROMPT_MAX_UTF16_UNITS, utf16_units
from joat.services.message_reply import MessageReplyController


class Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))


class Confirm:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.accept = True

    async def __call__(self, action_id: str, prompt: str, timeout_s: float, **_: Any) -> bool:
        self.prompts.append(prompt)
        return self.accept


CONTACTS = {"sam": [("Sam Okafor", "+14155550111")], "alex": [("Alex Rivera", "+1"), ("Alex Chen", "+2")]}


ALL = [pair for pairs in CONTACTS.values() for pair in pairs]


def search(query: str) -> list[tuple[str, str]]:
    """Like ContactResolver.search: exact full name wins, else word prefixes."""
    words = query.casefold().split()
    exact = [p for p in ALL if p[0].casefold() == " ".join(words)]
    if exact:
        return exact
    return [p for p in ALL if all(any(n.startswith(w) for n in p[0].casefold().split()) for w in words)]


async def m6() -> None:
    print("\n=== M6: 'my wife' resolves only after the wearer says who she is ===")
    with tempfile.TemporaryDirectory() as tmp:
        book = RelationshipBook(str(Path(tmp) / "relationships.json"))
        sender, confirm = Sender(), Confirm()
        replies = MessageReplyController(sender, confirm, contact_search=search, relationships=book)  # type: ignore[arg-type]
        first = await replies.prepare_new_message(action_id="a1", trace_id="t", recipient_name="my wife", body="home at six")
        print("before remembering :", first.state, "|", first.message[:70])
        try:
            await replies.remember_relationship("wife", "Alex")
        except ValueError as exc:
            print("ambiguous name refused:", exc)
        receipt = await replies.remember_relationship("my wife", "Sam")
        print("remember           :", receipt, "| file:", json.loads(Path(book._path).read_text()))
        replies.cancel_message_action("a1")
        second = await replies.prepare_new_message(action_id="a2", trace_id="t", recipient_name="my wife", body="home at six")
        await asyncio.sleep(0.3)
        status = replies.message_action_status("a2") if hasattr(replies, "message_action_status") else second
        print("after remembering  :", status.state, "| recipient:", status.recipient_name, "| prompt:", confirm.prompts[-1:] )
        print("sent               :", sender.sent)
        ok = (first.state == "awaiting_input" and first.needs_input == "recipient"
              and status.recipient_name == "Sam Okafor" and sender.sent == [("+14155550111", "home at six")])
        print("M6 verdict:", "PASS" if ok else "FAIL")
        await replies.aclose()


async def m7() -> None:
    print("\n=== M7: a long draft is reviewed in full, then confirmed with an opening ===")
    long_body = ("Hey, about Saturday: the venue moved to the Riverside hall, doors at six, bring the "
                 "projector cable and the second HDMI adapter because theirs is broken, and let me know "
                 "either way by tonight so I can tell the others.")
    reviews: list[tuple[str, str]] = []

    async def review(label: str, body: str) -> bool:
        reviews.append((label, body))
        return True

    sender, confirm = Sender(), Confirm()
    replies = MessageReplyController(sender, confirm, contact_search=search, review=review)  # type: ignore[arg-type]
    status = await replies.prepare_new_message(action_id="m1", trace_id="t", recipient_name="Sam", body=long_body)
    await asyncio.sleep(0.3)
    prompt = confirm.prompts[-1] if confirm.prompts else ""
    print("reviewed           :", [(l, len(b)) for l, b in reviews])
    print("confirmation prompt:", prompt, f"({utf16_units(prompt)} units, cap {CONFIRM_PROMPT_MAX_UTF16_UNITS})")
    print("sent               :", [(r, len(t)) for r, t in sender.sent])
    ok = (reviews == [("Sam Okafor", long_body)] and utf16_units(prompt) <= CONFIRM_PROMPT_MAX_UTF16_UNITS
          and "(reviewed)" in prompt and sender.sent == [("+14155550111", long_body)])
    print("M7 verdict:", "PASS" if ok else "FAIL")
    await replies.aclose()

    print("\n--- and a declined review sends nothing ---")
    async def decline(label: str, body: str) -> bool:
        return False
    sender2, confirm2 = Sender(), Confirm()
    replies2 = MessageReplyController(sender2, confirm2, contact_search=search, review=decline)  # type: ignore[arg-type]
    await replies2.prepare_new_message(action_id="m2", trace_id="t", recipient_name="Sam", body=long_body)
    await asyncio.sleep(0.3)
    final = replies2.message_action_status("m2") if hasattr(replies2, "message_action_status") else None
    print("state after decline:", getattr(final, "state", "?"), "| prompts:", confirm2.prompts, "| sent:", sender2.sent)
    print("M7-decline verdict:", "PASS" if not confirm2.prompts and not sender2.sent else "FAIL")
    await replies2.aclose()


async def main() -> None:
    await m6()
    await m7()


if __name__ == "__main__":
    asyncio.run(main())
