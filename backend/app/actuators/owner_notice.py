"""``notify.owner``: the transactional action type ``email.py`` said would be needed.

EXPORT ends by telling the owner what went live and what did not. It has never once
managed to: the node built a ``notify.email`` actuation with no sender, no body, no
unsubscribe link and no consent basis, and the email actuator refused it — correctly,
every time, for reasons that are not fixable by adding fields.

The choice was between widening ``CONSENT_BASES`` and writing this module. It is this
module, and the reasoning is `email.py`'s own: *"A password reset is a different action
type with different rules, not this one with a flag."* Two specific consequences make
that more than a preference:

* **The unsubscribe rule would have to be broken to keep the product working.** The one
  constraint `email.py` enforces hardest is that an unsubscribe URL appears in every body
  part actually transmitted. A service notice that offers to unsubscribe you from *"your
  run published 3 of 4"* is a product defect: the next run publishes and nobody is told,
  and the person who clicked it did not ask to stop being told what their own agent did.
* **Borrowing ``existing_customer`` would be the same widening in disguise.** It is a
  soft-opt-in MARKETING basis (PECR / GDPR Art. 6(1)(f) for a closely related product).
  Recording an operational notice under it would put a marketing claim in the audit row
  for a message that is not marketing, which is worse than a missing field: a wrong entry
  in a ledger is believed.

So the two types share a transport and share nothing else.

What this type enforces, and it is not a weaker list
----------------------------------------------------
Different, and in two places stricter. Each is an `ActuationRefusedError`, so each is
exercised without a credential — the checks run BEFORE the fake/real branch, for the
reason `email.py` gives: a refusal that only fires once ``RESEND_API_KEY`` is set is a
refusal first exercised on a real recipient.

* **A sender identity that is a real address.** Unchanged. Transactional is not an
  exemption from saying who you are; it is an exemption from the unsubscribe link.
* **A subject and at least one body part.** An empty notice is never intended, and a
  notice nobody can read is the same as not sending one.
* **One recipient.** Structural, and here it is nearly the whole point: there is exactly
  one account holder for a run, so a comma in this field means a bug upstream, not a
  batch.
* **The target must be a HANDLE, never the address** — see below. This is the defect that
  motivated the module as much as the refusals did.
* **NO unsubscribe URL and NO consent basis** — refused *by name*, which is this type's
  own strictness rather than a relaxation. It is what stops ``notify.owner`` becoming the
  door marketing walks through when it does not want to record a consent basis. A payload
  carrying either field is asking for the wrong action type, and the refusal says so.
* **The recipient must be DECLARED as the account address** (``recipient_source``, from a
  closed vocabulary that refuses ``crawled_website``/``dna`` by name). See the next
  section for why a claim field earns its place here.

Two defects this module exists to fix
-------------------------------------
**(i) A bare address was reaching the checkpoint.** The node passed ``target=<address>``.
``actuate()`` logs ``target``, ``Outcome.summary()`` renders it, ``_outcome_row`` copies
it into ``runs.checkpoint``, and the Delivery tab reads that — so the owner's address was
on a path to a JSONB column and a log line. `email.py` refuses that shape on purpose;
this type refuses it the same way, and `build_owner_notice_actuation` derives the handle
so a caller cannot get it wrong. **A test asserts the target contains no ``@``.**

**(ii) The recipient came from crawled data.** It was ``state["dna"]["email"]`` — an
address extracted from a homepage we do not control. Our own operational mail must go to
the **authenticated account**, or a page we crawled can redirect it. The address is
therefore resolved from ``businesses.owner_id -> users.email`` in ``run_executor`` and
injected on ``NodeDeps`` (the ``actuator_store`` pattern), so the node still touches no
database. The actuator cannot verify provenance — it holds no database and must not — so
what it can do is make the claim explicit and refuse the wrong one by name: a payload
declaring ``recipient_source: "dna"`` is stopped, and one declaring ``"account"`` is
recorded as having said so. Exactly the posture `email.py` documents for
``consent_basis``: the field cannot be forgotten, the claim cannot be vague, and after an
incident the ledger says what was asserted. It buys nothing else, and nothing else is
claimed for it.

Not enforced here, and stated rather than implied
-------------------------------------------------
* **That the address really is the account holder's.** A claim, as above.
* **SPF / DKIM / DMARC**, prior bounces, suppression: properties of DNS and of whatever
  holds the list, not of this request. Same as `email.py`.
* **Auto-reply loops** are *discouraged*, not prevented: every notice carries
  ``Auto-Submitted: auto-generated`` (RFC 3834), which well-behaved vacation responders
  honour and a broken one ignores.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import httpx

from backend.app.actuators.addresses import (
    address_fingerprint,
    address_handle,
    identity_address,
    looks_like_address,
)
from backend.app.actuators.contract import (
    Actuation,
    ActuationRefusedError,
    Actuator,
    Outcome,
    OutcomeStatus,
)
from backend.app.actuators.email import (
    DEFAULT_TIMEOUT_S,
    RESEND_API_KEY_ENV,
    EmailMessage,
    EmailSender,
    ResendSender,
)
from backend.app.actuators.fake import FakeActuator

logger: Final = logging.getLogger(__name__)

__all__ = [
    "ACTION_TYPE",
    "AUTO_SUBMITTED_HEADER",
    "MARKETING_ONLY_FIELDS",
    "RECIPIENT_SOURCES",
    "REFUSED_RECIPIENT_SOURCES",
    "SENDER_ENV",
    "TARGET_PREFIX",
    "OwnerNoticeActuator",
    "OwnerNoticeIdentity",
    "build_owner_notice_actuation",
    "build_owner_notice_actuator",
    "owner_notice_sender",
    "owner_target",
    "parse_owner_notice_payload",
]

#: The dotted name this actuator answers to. Defined here rather than imported from
#: `agents.tools` for the same reason `email.py` defines its own: actuators sit BELOW
#: agents, and importing upward would invert the dependency the architecture is built on.
ACTION_TYPE: Final = "notify.owner"

#: Marks an `Actuation.target` as an ACCOUNT handle. A different prefix from `email.py`'s
#: `rcpt:` on purpose: the same person may be both a marketing recipient and an account
#: holder, and a reader of the `actions` table should be able to tell which relationship
#: a row is about without joining anything.
TARGET_PREFIX: Final = "acct:"

#: Who our own operational mail comes from. Read from the environment by the actuator
#: layer, exactly as `RESEND_API_KEY` is, rather than from `core.config`: this is the same
#: kind of deployment fact and it belongs beside the credential it travels with.
#:
#: Unset means NO owner notice is attempted, and EXPORT says so in a named note. A default
#: would have to invent a sending domain, and an invented sender is the one failure this
#: type refuses hardest.
SENDER_ENV: Final = "OWNER_NOTICE_FROM"

#: How the recipient was obtained. A closed vocabulary, for the reason
#: `email.py::CONSENT_BASES` gives: a claim that cannot be vague is auditable, and a field
#: that cannot be forgotten is the half of the rule code can hold.
RECIPIENT_SOURCES: Final[frozenset[str]] = frozenset({"account"})

#: Provenances refused BY NAME. `dna` and `crawled_website` are the actual defect this
#: module was written against, so they are stopped explicitly rather than falling through
#: the "unrecognised" branch: a refusal that names the mistake is what stops it recurring.
REFUSED_RECIPIENT_SOURCES: Final[frozenset[str]] = frozenset(
    {"dna", "crawled_website", "crawl", "website", "scraped", "purchased_list", "unknown"}
)

#: Fields whose PRESENCE is a refusal here. Not "ignored" -- a caller supplying them has
#: the wrong action type, and silently dropping them would let a marketing send be
#: recorded as an operational notice with its consent basis quietly deleted.
MARKETING_ONLY_FIELDS: Final[tuple[str, ...]] = (
    "unsubscribe_url",
    "unsubscribe_one_click",
    "consent_basis",
)

#: RFC 3834. Tells a well-behaved autoresponder not to reply to a machine, which is the
#: most this can do -- it is a request, not a guarantee.
AUTO_SUBMITTED_HEADER: Final = "Auto-Submitted"
AUTO_GENERATED: Final = "auto-generated"


def owner_target(address: str) -> str:
    """The `Actuation.target` for one account holder: a handle, never the address.

    The whole reason defect (i) was possible is that `Actuation.target` is a free string
    and the obvious value to put in it is the address. It cannot be: `actuate()` logs the
    target, `Outcome.summary()` renders it, and `agents.nodes._outcome_row` copies it into
    `runs.checkpoint`, so the address would reach a JSONB column and a log line by three
    routes at once. `build_owner_notice_actuation` derives this so a caller cannot forget.
    """
    return address_handle(TARGET_PREFIX, address)


@dataclass(frozen=True, slots=True)
class OwnerNoticeIdentity:
    """Who an owner notice goes to, and who it comes from. Both, or neither.

    One value rather than two loose fields on `NodeDeps` because a sender with no account
    address, or an address with no sender identity, is not half a notifier — it is a
    notifier that will be refused at the actuator. Making that structural means the node's
    one check ("is this wired?") cannot answer yes to a configuration that cannot work.

    Resolved in `services/run_executor.py`: `account_email` from
    `businesses.owner_id -> users.email` (never from crawled site data — defect (ii)), and
    `sender` from `OWNER_NOTICE_FROM`.
    """

    #: The AUTHENTICATED account holder's address.
    account_email: str
    #: Our own sending identity. `Name <addr@domain>` is accepted.
    sender: str


def owner_notice_sender(env: Mapping[str, str] | None = None) -> str | None:
    """Our sending identity for owner notices, or None when none is configured.

    Blank and whitespace-only are treated as absent, for the reason `email.py` gives about
    `RESEND_API_KEY=`: an empty variable in a `.env` file is a very common way to unset
    one, and treating it as present would send mail from an unidentifiable sender — which
    is the refusal this module enforces first.
    """
    environ = env if env is not None else os.environ
    value = environ.get(SENDER_ENV, "").strip()
    return value or None


# --------------------------------------------------------------------------- #
# The gate: one pure function, so the policy is testable on its own
# --------------------------------------------------------------------------- #


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    """A non-blank string field, or a refusal naming the FIELD and never its value."""
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ActuationRefusedError(
            f"owner notice payload is missing a usable {key!r}. Every notice needs one, "
            "and a blank is a missing field rather than an empty one."
        )
    return raw.strip()


def _check_single_account(recipient: str) -> None:
    """One notice, one account holder.

    A run belongs to one business and a business has one owner, so a separator in this
    field is a bug upstream rather than a batch — and `contract.py` derives the idempotency
    key from the target, so two addresses behind one target would be one key that could
    never be retried for the address it missed.
    """
    if "," in recipient or ";" in recipient:
        raise ActuationRefusedError(
            "more than one recipient in a single owner notice. A run has one account "
            "holder, so a separator here is an upstream mistake -- and a batch behind one "
            "idempotency key cannot be retried for the address it missed."
        )
    if not looks_like_address(recipient):
        raise ActuationRefusedError(
            "payload['to'] is not a usable email address (expected one local@domain). "
            "The value itself is withheld from this message deliberately."
        )


def _check_target_is_a_handle(target: str, recipient: str) -> None:
    """The target must be `owner_target(recipient)`. This is defect (i), refused.

    `actuate()` logs `target`, `Outcome.summary()` interpolates it, and the node copies it
    into `runs.checkpoint`, so an address there leaves this module with no way to keep the
    account holder's address out of a log or out of the Delivery tab. Refusing is the only
    control that can be guaranteed from here, and a refusal that explains itself beats a
    leak nobody notices.
    """
    expected = owner_target(recipient)
    if target == expected:
        return
    if "@" in target:
        raise ActuationRefusedError(
            "Actuation.target must be owner_target(address), not the address itself. "
            "actuate() logs the target, Outcome.summary() renders it and the run's "
            "checkpoint stores it, so an address there would be logged and persisted. "
            "Build the actuation with build_owner_notice_actuation()."
        )
    raise ActuationRefusedError(
        "Actuation.target does not match the recipient in payload['to']. The target is "
        "part of the idempotency key, so a mismatch would let one key stand for a "
        "different account's notice. Build the actuation with "
        "build_owner_notice_actuation()."
    )


def _check_sender_identity(sender: str) -> None:
    """Sender identity. Transactional exempts the unsubscribe link, never this.

    Accepts `Name <addr@domain>` as well as a bare address, because a display name is part
    of identifying yourself rather than an obstacle to it.
    """
    if not looks_like_address(identity_address(sender)):
        raise ActuationRefusedError(
            "the sender identity is not a usable email address. Every send must say who "
            "it is from, transactional included -- being exempt from an unsubscribe link "
            f"is not an exemption from identifying yourself. Set {SENDER_ENV}."
        )


def _check_not_marketing(payload: Mapping[str, Any]) -> None:
    """Refuse the marketing apparatus by name. This type's own strictness.

    `notify.owner` is exempt from the unsubscribe requirement because it is transactional.
    That exemption is exactly what would make it attractive as a route for a marketing
    send that does not want to record a consent basis, so the fields that belong to
    `notify.email` are a STOP here rather than something to ignore. Ignoring them would let
    a marketing message be recorded in the ledger as an operational notice with its
    consent basis silently dropped.
    """
    present = [field for field in MARKETING_ONLY_FIELDS if field in payload]
    if present:
        raise ActuationRefusedError(
            f"{ACTION_TYPE} does not carry marketing fields ({', '.join(present)}). This "
            "is a transactional notice to the account holder: it must NOT offer to "
            "unsubscribe from the product's own operational mail, and it records no "
            "consent basis because it rests on none. A message that needs those fields is "
            "a notify.email, and routing it here would hide a marketing send in an "
            "operational action type."
        )


def _check_recipient_source(payload: Mapping[str, Any]) -> str:
    """Require a declared provenance, and refuse crawled data by name. Defect (ii).

    What this proves is narrow and is stated in the module docstring: the field cannot be
    forgotten, the claim cannot be vague, and the ledger records what was asserted. It does
    NOT prove the address is the account holder's — that is guaranteed by where
    `run_executor` reads it from, and this field is how the claim reaches the audit row.
    """
    raw = payload.get("recipient_source")
    if not isinstance(raw, str) or not raw.strip():
        raise ActuationRefusedError(
            "no recipient_source recorded. Our own operational mail must go to the "
            "AUTHENTICATED account address, and this field is how the actuation says that "
            f"is where its recipient came from. Use one of: "
            f"{', '.join(sorted(RECIPIENT_SOURCES))}."
        )
    source = raw.strip().lower()
    if source in REFUSED_RECIPIENT_SOURCES:
        raise ActuationRefusedError(
            f"recipient_source {source!r} is refused: that address came from data we do "
            "not control (a crawled homepage), and a page we crawled must never be able "
            "to redirect our own operational mail. Resolve the account holder's address "
            "from the authenticated account instead."
        )
    if source not in RECIPIENT_SOURCES:
        raise ActuationRefusedError(
            f"recipient_source {source!r} is not a recognised provenance. The vocabulary "
            "is closed on purpose -- free text makes the claim unauditable. Use one of: "
            f"{', '.join(sorted(RECIPIENT_SOURCES))}."
        )
    return source


def parse_owner_notice_payload(actuation: Actuation) -> EmailMessage:
    """Validate one owner notice and build the message, or refuse it.

    Every check lives here, in one pure function, for two reasons: the policy can be
    tested without an HTTP layer or a store, and the checks cannot be skipped by a code
    path that reaches a sender directly — an `EmailMessage` can only be built by passing
    through a parser.

    Raises `ActuationRefusedError` only, never `ActuatorError`: nothing in here has talked
    to anybody, so nothing in here can be a failure. Refusals name the RULE and the FIELD
    and never the value, because the message becomes `Outcome.error` and `actuate()` logs
    that.
    """
    payload = actuation.payload

    if actuation.action_type != ACTION_TYPE:
        raise ActuationRefusedError(
            f"{actuation.action_type!r} is not {ACTION_TYPE!r}. The rules differ per action "
            "type -- this parser exempts the unsubscribe link and refuses a consent basis -- "
            "so applying them to another type would apply the wrong ones."
        )

    recipient = _require_text(payload, "to")
    _check_single_account(recipient)
    _check_target_is_a_handle(actuation.target.strip(), recipient)
    _check_recipient_source(payload)

    sender = _require_text(payload, "sender")
    _check_sender_identity(sender)

    subject = _require_text(payload, "subject")

    _check_not_marketing(payload)

    html = payload.get("html")
    text = payload.get("text")
    html_part = html.strip() if isinstance(html, str) and html.strip() else None
    text_part = text.strip() if isinstance(text, str) and text.strip() else None
    if html_part is None and text_part is None:
        raise ActuationRefusedError(
            "the owner notice has no body: at least one of 'html' or 'text' must carry "
            "content. A notice nobody can read is the same as not telling them."
        )

    reply_to = payload.get("reply_to")

    return EmailMessage(
        sender=sender,
        recipient=recipient,
        subject=subject,
        html=html_part,
        text=text_part,
        reply_to=reply_to.strip() if isinstance(reply_to, str) and reply_to.strip() else None,
        # No `List-Unsubscribe`, deliberately: advertising an unsubscribe for operational
        # mail is the product defect this action type exists to avoid.
        headers={AUTO_SUBMITTED_HEADER: AUTO_GENERATED},
    )


def build_owner_notice_actuation(
    *,
    business_id: UUID,
    identity: OwnerNoticeIdentity,
    subject: str,
    approved_by: str,
    html: str | None = None,
    text: str | None = None,
    reply_to: str | None = None,
    run_id: UUID | None = None,
) -> Actuation:
    """Build a correctly-shaped `notify.owner` actuation.

    Takes the whole `OwnerNoticeIdentity` rather than a loose address and sender, so the
    two cannot be supplied from different places — and the recipient therefore cannot come
    from anywhere but whatever resolved the account. `recipient_source` is stamped here for
    the same reason `target` is derived here: a required field a caller has to remember is
    a field a caller forgets.

    `None` values are omitted rather than stored as null, because `contract.py` hashes the
    payload: an explicit `"reply_to": None` and an absent key would otherwise be two
    different idempotency keys for the same notice.
    """
    payload: dict[str, Any] = {
        "to": identity.account_email,
        "sender": identity.sender,
        "subject": subject,
        "recipient_source": "account",
    }
    if html is not None:
        payload["html"] = html
    if text is not None:
        payload["text"] = text
    if reply_to is not None:
        payload["reply_to"] = reply_to

    return Actuation(
        business_id=business_id,
        action_type=ACTION_TYPE,
        target=owner_target(identity.account_email),
        payload=payload,
        approved_by=approved_by,
        run_id=run_id,
    )


# --------------------------------------------------------------------------- #
# The actuator
# --------------------------------------------------------------------------- #


class OwnerNoticeActuator:
    """Performs `notify.owner`: checks the transactional rules, then sends — or simulates.

    `sender=None` is the unconfigured case and it is not an error. It degrades to
    `FakeActuator`'s posture — an outcome marked `fake` that carries a reason — which is
    the rule `CLAUDE.md` states for every provider: a missing credential means the fake,
    plus a status that says so. Never a silent no-op, never a crash.

    The checks run BEFORE that branch, for the reason `email.py` states and this type
    inherits without dilution: a refusal that only fires with a real key is a refusal first
    exercised on a real recipient.
    """

    def __init__(self, sender: EmailSender | None = None) -> None:
        self._sender = sender
        self._fake = FakeActuator(ACTION_TYPE)

    @property
    def action_type(self) -> str:
        return ACTION_TYPE

    @property
    def fake(self) -> bool:
        return self._sender is None

    @property
    def provider(self) -> str:
        """Which sender is behind this actuator, for a status screen."""
        return "fake" if self._sender is None else self._sender.name

    async def perform(self, actuation: Actuation) -> Outcome:
        message = parse_owner_notice_payload(actuation)

        sender = self._sender
        if sender is None:
            outcome = await self._fake.perform(actuation)
            # Rebuilt rather than returned as-is so the reason names THIS integration's
            # missing credential. "No credential is configured" is not actionable;
            # "RESEND_API_KEY is not set" is.
            return Outcome(
                status=outcome.status,
                action_type=outcome.action_type,
                target=outcome.target,
                external_ref=outcome.external_ref,
                detail={
                    "simulated": True,
                    "reason": (
                        f"{RESEND_API_KEY_ENV} is not set, so no owner notice left this "
                        "process. The transactional checks still ran and passed."
                    ),
                    "provider": "fake",
                    "checks_passed": True,
                },
                fake=True,
            )

        message_id = await sender.send(message)

        # The only log line on the success path, and it carries no address, no subject and
        # no body -- a fingerprint is enough to correlate two notices to one account.
        logger.info(
            "owner notice sent: provider=%s account_fp=%s message_id=%s",
            sender.name,
            address_fingerprint(message.recipient),
            message_id,
        )

        return Outcome(
            status=OutcomeStatus.SUCCEEDED,
            action_type=ACTION_TYPE,
            target=actuation.target,
            external_ref=message_id,
            detail={"provider": sender.name},
            fake=False,
        )


def build_owner_notice_actuator(
    env: Mapping[str, str] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> OwnerNoticeActuator:
    """The owner-notice actuator this environment can actually use.

    Selection is by credential and by nothing else — no flag, no separate "enable
    notifications" switch that could disagree with whether a key exists. The transport is
    `ResendSender`, the same one `notify.email` uses: the rules differ, the wire shape does
    not, and a second HTTP client for the same POST would be a second place for the
    status-to-`retryable` mapping to be wrong.
    """
    environ = env if env is not None else os.environ
    key = environ.get(RESEND_API_KEY_ENV, "").strip()
    if not key:
        return OwnerNoticeActuator()
    return OwnerNoticeActuator(ResendSender(key, timeout_s=timeout_s, client=client))


if TYPE_CHECKING:  # pragma: no cover - compile-time conformance checks

    def _satisfies_actuator_protocol(actuator: OwnerNoticeActuator) -> Actuator:
        """Fails type checking the moment this drifts from the port."""
        return actuator
