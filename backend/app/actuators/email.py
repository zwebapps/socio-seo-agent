"""The email actuator: the cheapest channel that publishes for real, and the only one
whose failure mode is a fine.

`docs/CHANNELS.md` §2 rates email "Direct send -- the easiest real channel": there is no
App Review, no quota, no token refresh dance. §6 says why that is misleading -- **"Legal
before creative: consent basis recorded per recipient, unsubscribe in every send, sender
identity, SPF/DKIM/DMARC on the sending domain. Never send to a scraped or purchased
list -- this is the one channel where a mistake is a fine, not a bad metric."**

So the interesting part of this module is not the HTTP call. It is which of those four
constraints code can ENFORCE, and the honesty about the rest.

Enforced, by refusing
---------------------
Each of these is a mechanical property of the request in hand, so each one is an
`ActuationRefusedError` and not a comment:

* **An unsubscribe mechanism, present in the BODY of every send.** Not "an
  `unsubscribe_url` field was supplied" -- the URL has to actually appear in every body
  part that goes out, because a recipient cannot click a field. This is the constraint
  most worth enforcing here: it is the one that is trivially checkable and the one a
  renderer silently drops when a template changes.
* **A sender identity that is a real address.** An unidentified sender is the CAN-SPAM /
  GDPR failure that needs no lawyer to spot.
* **A consent basis, drawn from a closed vocabulary.** See the honest note below on what
  this does and does not prove.
* **A declared bad provenance.** `purchased_list`, `scraped`, `rented_list` are refused
  by name. We cannot detect an undeclared scraped list, but an upstream that honestly
  says where the address came from must be stopped rather than obeyed.
* **One recipient per actuation.** A comma-separated `to` would make "consent basis per
  recipient" and the audit row both a lie by aggregation, and would collapse N sends
  into one idempotency key.
* **A subject and at least one body part.** Cheap, and an empty send is never intended.

Documented, because it CANNOT be enforced here
----------------------------------------------
Saying otherwise would be the more dangerous mistake:

* **Whether the recorded consent basis is TRUE.** `consent_basis` is a *claim made by
  the caller and preserved in the audit row*, not a verified fact. The closed vocabulary
  buys three real things -- the field cannot be forgotten, the claim cannot be vague, and
  after an incident the ledger says what was asserted and by whom -- and it buys nothing
  else. Verification needs the double-opt-in record, which lives in whatever owns the
  list.
* **List provenance.** As above: a refused declaration is enforcement; an absent one is
  not detection.
* **SPF / DKIM / DMARC.** A property of DNS for the sending domain, not of this request.
  Resend refuses an unverified domain itself, which arrives here as a 403 and is mapped
  non-retryable -- a real check, just not ours and not before the call.
* **Prior unsubscribes and suppression lists.** This actuator is handed one recipient and
  holds no list. Honouring an unsubscribe is the responsibility of whatever selects
  recipients; nothing here can notice.
* **A physical postal address in the body** (CAN-SPAM). Not reliably detectable in
  arbitrary HTML, and a regex that pretended to would be worse than the gap.

Three further decisions worth stating
-------------------------------------
**The legal checks run BEFORE the fake/real branch, so they run without a credential.**
A refusal that only fires once `RESEND_API_KEY` is set is a refusal nobody has ever seen
work -- it would be exercised for the first time in front of a real recipient. Running a
whole pipeline on the fake and getting a refusal on a body with no unsubscribe link is
the entire point of having a fake.

**Refusal is OUR policy; failure is the PROVIDER's verdict.** A missing unsubscribe link
is `ActuationRefusedError` (the system working, `contract.py` rule 3). A key Resend
rejects, or a recipient Resend refuses, is `ActuatorError(retryable=False)` -- it FAILED,
and calling it a refusal would hide a broken integration inside a status that is supposed
to be uneventful.

**No message text and no recipient address is ever logged.** `core/rate_limit.py` keys
its counters on HMAC digests so a Redis dump is not an address book; `obs/tracing.py`
redacts text inside the tracer because that is the one chokepoint no call site can
forget. The same posture applies here, and it constrains something easy to miss: the
`ActuatorError` message becomes `Outcome.error`, which `actuate()` LOGS. So the error
text carries a status code and the provider's error *name* only -- never the provider's
`message` field, which routinely quotes the address it rejected.

**Which forces one design decision worth reading before you use this: `target` is a
recipient FINGERPRINT, not the address.** `contract.py` offers "an address" as an example
target, and that is exactly what cannot be done here -- `actuate()` logs `target` on the
failure path (`"actuation failed: ... target=%s"`) and `Outcome.summary()` interpolates it
into a line meant for a timeline or a log. With the address in `target`, "never log a
recipient" is unsatisfiable without editing `actuate.py` or `contract.py`, which this
module may not touch. So `target` is `recipient_target(address)` -- a stable derived
handle -- and the address itself travels in `payload["to"]`, which is persisted verbatim
in the audit row (an audit that cannot say who was written to is not an audit) and is
never logged by anything. The idempotency property `contract.py` asks for is untouched:
the handle is a function of the address, so two recipients are still two keys and the same
body to two people is still two sends. Use `build_email_actuation()` and this is automatic;
build the `Actuation` by hand with an address in `target` and it is REFUSED, with a message
saying why.

Resend is the vendor (`docs/ROADMAP.md`), reached over plain `httpx` rather than its SDK:
the surface is one POST, and the seam below is what makes Postmark or SES an adapter
rather than a rewrite.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel

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
    ActuatorError,
    Outcome,
    OutcomeStatus,
)
from backend.app.actuators.fake import FakeActuator

logger: Final = logging.getLogger(__name__)

__all__ = [
    "ACTION_TYPE",
    "CONSENT_BASES",
    "DISQUALIFYING_CONSENT_BASES",
    "RESEND_API_KEY_ENV",
    "RESEND_ENDPOINT",
    "TARGET_PREFIX",
    "EmailActuator",
    "EmailConfigStatus",
    "EmailMessage",
    "EmailSender",
    "ResendSender",
    "build_email_actuation",
    "build_email_actuator",
    "email_config_status",
    "parse_email_payload",
    "recipient_fingerprint",
    "recipient_target",
]

#: The dotted name this actuator answers to. Defined here rather than imported from
#: `agents.tools`: actuators sit BELOW agents, and importing upward would invert the
#: dependency the architecture is built on.
ACTION_TYPE: Final = "notify.email"

RESEND_API_KEY_ENV: Final = "RESEND_API_KEY"
RESEND_ENDPOINT: Final = "https://api.resend.com/emails"
RESEND_PROVIDER: Final = "resend"

#: One POST with a small body. Longer than the login-path timeouts in
#: `core/rate_limit.py` because nothing is waiting on this synchronously, and short
#: enough that a hung provider does not hold a graph node open indefinitely.
DEFAULT_TIMEOUT_S: Final = 10.0

#: Consent bases this actuator will act on. A closed set so the field cannot be a
#: free-text shrug: "recorded per recipient" is only meaningful if the recording is
#: comparable across sends. Every value is a CLAIM -- see the module docstring.
#:
#: `transactional` is deliberately ABSENT. A transactional message is genuinely exempt
#: from the unsubscribe requirement, so admitting it here would mean admitting an
#: exemption to the one rule this module enforces hardest. A password reset is a
#: different action type with different rules, not this one with a flag.
#:
#: That action type now exists: `owner_notice.py` performs `notify.owner`, and it is the
#: proof that this comment was a design statement rather than a deferral. Note what would
#: have happened had `existing_customer` been borrowed for it instead -- that basis is a
#: soft-opt-in MARKETING basis, so an owner service notice would have been recorded in the
#: ledger as marketing to a customer, and would have had to carry an unsubscribe link
#: offering to switch off "your run published 3 of 4".
CONSENT_BASES: Final[frozenset[str]] = frozenset(
    {
        # The recipient asked, once.
        "explicit_optin",
        # The recipient asked and confirmed by email. The only basis that can actually
        # be evidenced later.
        "double_optin",
        # An existing customer relationship (PECR soft opt-in / GDPR Art. 6(1)(f) for a
        # closely related product). Narrower than it looks, and the caller owns proving
        # the relationship exists.
        "existing_customer",
    }
)

#: Provenances that are refused BY NAME. Enforcement of "never a scraped or purchased
#: list" in the only form it can take: we cannot detect one that is not declared, but an
#: honest declaration must be a stop rather than a note.
DISQUALIFYING_CONSENT_BASES: Final[frozenset[str]] = frozenset(
    {"purchased_list", "purchased", "scraped", "rented_list", "rented", "harvested", "none"}
)

#: Statuses worth another attempt later. 429 is the provider asking us to slow down and
#: 5xx is the provider being broken; both are the same request succeeding at a different
#: time. 408 is the provider's own timeout.
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 429})

#: Marks an `Actuation.target` as a recipient handle rather than an address. See
#: `recipient_target` and the module docstring for why the address cannot go there.
TARGET_PREFIX: Final = "rcpt:"

#: Header carrying the unsubscribe URL for mail clients (RFC 2369). Emitted in addition
#: to the in-body link, never instead of it: the header serves the client, the link
#: serves the person.
LIST_UNSUBSCRIBE_HEADER: Final = "List-Unsubscribe"
#: RFC 8058 one-click. Emitted ONLY when the caller declares the URL accepts POST --
#: advertising one-click against a GET-only endpoint turns every mailbox provider's
#: unsubscribe button into a silent failure.
LIST_UNSUBSCRIBE_POST_HEADER: Final = "List-Unsubscribe-Post"
ONE_CLICK: Final = "List-Unsubscribe=One-Click"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def recipient_fingerprint(address: str) -> str:
    """A short, stable handle for a recipient, for logs and metrics.

    Kept as this module's name for the shared `address_fingerprint`: the transactional
    actuator needs the identical digest, and two copies of a hash would be two handles
    for one person the moment either changed. See `addresses.py` for what it is and,
    more importantly, what it is not.
    """
    return address_fingerprint(address)


def recipient_target(address: str) -> str:
    """The `Actuation.target` for one recipient: a handle, never the address.

    Prefixed so a human reading the `actions` table can tell at a glance that this is a
    derived handle rather than a truncated address, and so a raw address in the column is
    obvious on sight.

    Why a handle at all is in the module docstring: `actuate()` logs `target`, and
    `Outcome.summary()` interpolates it, so an address there could not be kept out of a
    log from inside this module. Being a pure function of the address, it keeps every
    idempotency property `contract.py` asks of a target.
    """
    return address_handle(TARGET_PREFIX, address)


# --------------------------------------------------------------------------- #
# The message, and the seam that sends one
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One validated email, ready to hand to a provider.

    Constructed only by a payload parser -- `parse_email_payload` here, or
    `parse_owner_notice_payload` in `owner_notice.py` -- which is where every check lives,
    so the existence of this object means the checks for its own action type passed.
    Nothing downstream re-validates and nothing downstream may skip it.

    Shared by the two mail-shaped action types deliberately: the RULES differ (one demands
    a consent basis and an in-body unsubscribe link, the other refuses both), while the
    wire shape a provider takes is identical, so `ResendSender` is written once.
    """

    sender: str
    recipient: str
    subject: str
    html: str | None = None
    text: str | None = None
    reply_to: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def body_parts(self) -> tuple[str, ...]:
        """Every body part that will actually be transmitted."""
        return tuple(part for part in (self.html, self.text) if part)


class EmailSender(Protocol):
    """Send exactly one email. The seam, and the whole seam.

    Deliberately narrower than the actuator: no idempotency, no approval, no policy, no
    audit -- those belong to `actuate()` and to `parse_email_payload` respectively. An
    implementation's only job is to move one already-legal message to a provider and
    return the provider's id for it.

    Returns the provider message id, which becomes `Outcome.external_ref`: the thing
    somebody can quote to support when a send is disputed.
    """

    name: str

    async def send(self, message: EmailMessage) -> str: ...


class ResendSender:
    """Real sending, via Resend's `POST /emails` over plain `httpx`.

    No vendor SDK on purpose. The surface is one POST with a JSON body, so an SDK would
    add a dependency (and a lockfile change) to wrap six lines, and the mapping from HTTP
    status to `retryable` -- the only part with any judgement in it -- would still have to
    be written here.

    `client` is injectable for the same reason it is in `llm/catalogue.py` and
    `llm/ollama_provider.py`: the tests drive an `httpx.MockTransport` and there is no
    socket for a request to escape through.
    """

    name = RESEND_PROVIDER

    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
        endpoint: str = RESEND_ENDPOINT,
    ) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client = client
        self._endpoint = endpoint

    def _payload(self, message: EmailMessage) -> dict[str, Any]:
        """Resend's wire shape. The only place this vendor's field names appear."""
        payload: dict[str, Any] = {
            "from": message.sender,
            # A list of exactly one. `parse_email_payload` guarantees the one.
            "to": [message.recipient],
            "subject": message.subject,
        }
        if message.html is not None:
            payload["html"] = message.html
        if message.text is not None:
            payload["text"] = message.text
        if message.reply_to is not None:
            payload["reply_to"] = message.reply_to
        if message.headers:
            payload["headers"] = dict(message.headers)
        return payload

    async def send(self, message: EmailMessage) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._endpoint, json=self._payload(message), headers=headers
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.post(
                        self._endpoint, json=self._payload(message), headers=headers
                    )
        except httpx.HTTPError as exc:
            # Retryable: a timeout or a dropped connection says nothing about whether the
            # request was acceptable, only that this attempt did not complete.
            #
            # `type(exc).__name__` and not `str(exc)`: an httpx message can carry the
            # request URL, and this string ends up in `Outcome.error`, which `actuate()`
            # logs. The class name is enough to tell a timeout from a DNS failure.
            raise ActuatorError(
                f"email provider unreachable ({type(exc).__name__})", retryable=True
            ) from exc

        if response.status_code >= 400:
            raise self._failure(response)

        return self._message_id(response)

    @staticmethod
    def _error_name(response: httpx.Response) -> str:
        """The provider's machine-readable error name, and nothing else from the body.

        Resend answers `{"name": "validation_error", "message": "..."}`, and the
        `message` regularly quotes the address or field it objected to. That string must
        not reach a log, so only `name` is read and anything unexpected degrades to a
        generic label rather than to the raw body.
        """
        try:
            body = response.json()
        except ValueError:
            return "unparseable_error_body"
        if not isinstance(body, dict):
            return "unexpected_error_body"
        name = body.get("name")
        return name if isinstance(name, str) and name else "unnamed_error"

    def _failure(self, response: httpx.Response) -> ActuatorError:
        """Map an HTTP status onto `retryable`, which is the field callers branch on.

        The split is "would the identical request succeed later?":

        * **429, 408, 5xx -- yes.** Rate limited, or the provider is having a bad day.
        * **401 / 403 -- no.** A rejected key, or a sending domain Resend has not
          verified. Retrying hammers a permanent failure and the fix is configuration.
        * **422 / 400 / other 4xx -- no.** A refused recipient or a malformed payload.
          The request has to change, not the timing.
        """
        status = response.status_code
        retryable = status >= 500 or status in RETRYABLE_STATUSES
        return ActuatorError(
            f"email provider rejected the send: HTTP {status} ({self._error_name(response)})",
            retryable=retryable,
        )

    @staticmethod
    def _message_id(response: httpx.Response) -> str:
        """Resend's id for the accepted message.

        A 200 with no id is treated as a failure rather than shrugged off with an empty
        `external_ref`: "we sent it, we cannot tell you where" is exactly the outcome
        `contract.py` says `external_ref` exists to prevent. Non-retryable, because
        repeating a send the provider already accepted is how one email becomes two.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise ActuatorError(
                "email provider returned a non-JSON success body", retryable=False
            ) from exc
        message_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise ActuatorError(
                "email provider accepted the send but returned no message id",
                retryable=False,
            )
        return message_id


def _satisfies_sender_protocol(sender: ResendSender) -> EmailSender:
    """Compile-time proof that the real sender satisfies the seam. mypy checks this."""
    return sender


# --------------------------------------------------------------------------- #
# The legal gate: one pure function, so the policy is testable on its own
# --------------------------------------------------------------------------- #


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    """A non-blank string field, or a refusal naming the FIELD and never its value."""
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ActuationRefusedError(
            f"email payload is missing a usable {key!r}. Every send needs one, and a "
            "blank is a missing field rather than an empty one."
        )
    return raw.strip()


def _check_single_recipient(recipient: str) -> None:
    """One actuation, one recipient. Structural, and it carries two guarantees.

    A consent basis is recorded *per recipient*, so a batch behind one basis records a
    claim about a group that was never made about a person. And `contract.py` derives the
    idempotency key from the target, so N addresses behind one target would be one key: a
    partial failure could not be retried for the people it missed without re-sending to
    everyone it reached.
    """
    if "," in recipient or ";" in recipient:
        raise ActuationRefusedError(
            "more than one recipient in a single actuation. One send, one recipient: a "
            "consent basis is recorded per recipient, and a batch behind one "
            "idempotency key cannot be retried for the addresses it missed."
        )
    if not looks_like_address(recipient):
        raise ActuationRefusedError(
            "payload['to'] is not a usable email address (expected one local@domain). "
            "The value itself is withheld from this message deliberately."
        )


def _check_target_is_a_handle(target: str, recipient: str) -> None:
    """The target must be `recipient_target(recipient)`, and this is why it is enforced.

    `actuate()` logs `target` and `Outcome.summary()` interpolates it, so an address there
    leaves this module with no way to keep a recipient out of a log. Refusing is the only
    place that can be guaranteed from here -- and a refusal that explains itself is far
    better than a leak nobody notices. `build_email_actuation` makes it automatic.
    """
    expected = recipient_target(recipient)
    if target == expected:
        return
    if "@" in target:
        raise ActuationRefusedError(
            "Actuation.target must be recipient_target(address), not the address itself. "
            "actuate() logs the target and Outcome.summary() renders it, so an address "
            "there would be logged. Build the actuation with build_email_actuation()."
        )
    raise ActuationRefusedError(
        "Actuation.target does not match the recipient in payload['to']. The target is "
        "part of the idempotency key, so a mismatch would let one key stand for a "
        "different recipient's send. Build the actuation with build_email_actuation()."
    )


def _check_sender_identity(sender: str) -> None:
    """Sender identity, the third of §6's four constraints and the easiest to check.

    Accepts `Name <addr@domain>` as well as a bare address, because a display name is
    part of identifying yourself rather than an obstacle to it.
    """
    if not looks_like_address(identity_address(sender)):
        raise ActuationRefusedError(
            "the sender identity is not a usable email address. Every send must say who "
            "it is from -- an unidentifiable sender is the plainest legal failure there "
            "is, and it is also what gets a sending domain blocked."
        )


def _check_consent_basis(payload: Mapping[str, Any]) -> str:
    """Require a recorded basis from the closed vocabulary, and refuse a declared bad one.

    What this proves is narrow and is stated in the module docstring: the field cannot be
    forgotten, the claim cannot be vague, and the ledger records what was asserted. It
    does NOT prove the claim is true.
    """
    raw = payload.get("consent_basis")
    if not isinstance(raw, str) or not raw.strip():
        raise ActuationRefusedError(
            "no consent_basis recorded for this recipient. docs/CHANNELS.md section 6 "
            "requires a consent basis per recipient, and this is the one channel where "
            f"a mistake is a fine. Use one of: {', '.join(sorted(CONSENT_BASES))}."
        )
    basis = raw.strip().lower()
    if basis in DISQUALIFYING_CONSENT_BASES:
        raise ActuationRefusedError(
            f"consent_basis {basis!r} is a declared bad provenance. Never send to a "
            "scraped, purchased or rented list. This refusal is the only enforceable "
            "half of that rule: an undeclared list cannot be detected here, so a "
            "declaration of one is a stop."
        )
    if basis not in CONSENT_BASES:
        raise ActuationRefusedError(
            f"consent_basis {basis!r} is not a recognised basis. The vocabulary is "
            "closed on purpose -- free text makes 'recorded per recipient' unauditable. "
            f"Use one of: {', '.join(sorted(CONSENT_BASES))}."
        )
    return basis


def _check_unsubscribe(payload: Mapping[str, Any], parts: tuple[str, ...]) -> str:
    """The constraint most worth enforcing, and it is checked in the BODY, not the field.

    A supplied `unsubscribe_url` proves nothing: a recipient cannot click a payload key.
    The URL has to appear in every body part that goes out, which is what catches the
    real failure -- a template edit that drops the footer while the field stays set.
    """
    raw = payload.get("unsubscribe_url")
    if not isinstance(raw, str) or not raw.strip():
        raise ActuationRefusedError(
            "no unsubscribe_url. Every send needs an unsubscribe mechanism "
            "(docs/CHANNELS.md section 6) -- there is no marketing send this is "
            "optional for."
        )
    url = raw.strip()
    if not _is_absolute_http_url(url):
        raise ActuationRefusedError(
            "unsubscribe_url is not an absolute http(s) URL with a host. A relative "
            "link in an email resolves against the mail client and unsubscribes nobody."
        )
    missing = [part for part in parts if url not in part]
    if missing:
        raise ActuationRefusedError(
            "the unsubscribe URL does not appear in every body part being sent "
            f"({len(missing)} of {len(parts)} are missing it). A field is not a link: "
            "the recipient has to be able to click it in the message they receive."
        )
    return url


def _is_absolute_http_url(value: str) -> bool:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return False
    return url.scheme in {"http", "https"} and bool(url.host)


def _unsubscribe_headers(url: str, payload: Mapping[str, Any]) -> dict[str, str]:
    """RFC 2369 always; RFC 8058 one-click only when the caller says POST is supported."""
    headers = {LIST_UNSUBSCRIBE_HEADER: f"<{url}>"}
    if payload.get("unsubscribe_one_click") is True:
        headers[LIST_UNSUBSCRIBE_POST_HEADER] = ONE_CLICK
    return headers


def parse_email_payload(actuation: Actuation) -> EmailMessage:
    """Validate one actuation and build the message, or refuse it.

    Every legal check lives here, in one pure function, for two reasons: the policy can
    be tested without an HTTP layer or a store, and the checks cannot be skipped by a
    code path that reaches a sender directly -- an `EmailMessage` can only be built by
    passing through this function.

    Raises `ActuationRefusedError` only, never `ActuatorError`: nothing in here has
    talked to anybody, so nothing in here can be a failure. Refusals name the RULE and
    the FIELD and never the value, because the message becomes `Outcome.error` and
    `actuate()` logs that.
    """
    payload = actuation.payload

    recipient = _require_text(payload, "to")
    _check_single_recipient(recipient)
    _check_target_is_a_handle(actuation.target.strip(), recipient)

    sender = _require_text(payload, "sender")
    _check_sender_identity(sender)

    subject = _require_text(payload, "subject")

    html = payload.get("html")
    text = payload.get("text")
    html_part = html.strip() if isinstance(html, str) and html.strip() else None
    text_part = text.strip() if isinstance(text, str) and text.strip() else None
    parts = tuple(part for part in (html_part, text_part) if part is not None)
    if not parts:
        raise ActuationRefusedError(
            "the email has no body: at least one of 'html' or 'text' must carry "
            "content. docs/CHANNELS.md section 6 also asks for a plain-text alternative "
            "on every send."
        )

    _check_consent_basis(payload)
    unsubscribe_url = _check_unsubscribe(payload, parts)

    reply_to = payload.get("reply_to")

    return EmailMessage(
        sender=sender,
        recipient=recipient,
        subject=subject,
        html=html_part,
        text=text_part,
        reply_to=reply_to.strip() if isinstance(reply_to, str) and reply_to.strip() else None,
        headers=_unsubscribe_headers(unsubscribe_url, payload),
    )


def build_email_actuation(
    *,
    business_id: UUID,
    to: str,
    sender: str,
    subject: str,
    unsubscribe_url: str,
    consent_basis: str,
    approved_by: str,
    html: str | None = None,
    text: str | None = None,
    reply_to: str | None = None,
    unsubscribe_one_click: bool = False,
    run_id: UUID | None = None,
) -> Actuation:
    """Build a correctly-shaped `notify.email` actuation.

    Every legally required field is a keyword-only REQUIRED argument, so the common
    mistakes are caught by the type checker instead of by a refusal at run time -- and
    `target` is derived here, which is the whole reason this helper exists rather than
    leaving callers to remember `recipient_target`.

    `None` values are omitted rather than stored as null, because `contract.py` hashes the
    payload: an explicit `"reply_to": None` and an absent key would otherwise be two
    different idempotency keys for the same email.
    """
    payload: dict[str, Any] = {
        "to": to,
        "sender": sender,
        "subject": subject,
        "unsubscribe_url": unsubscribe_url,
        "consent_basis": consent_basis,
    }
    if html is not None:
        payload["html"] = html
    if text is not None:
        payload["text"] = text
    if reply_to is not None:
        payload["reply_to"] = reply_to
    if unsubscribe_one_click:
        payload["unsubscribe_one_click"] = True

    return Actuation(
        business_id=business_id,
        action_type=ACTION_TYPE,
        target=recipient_target(to),
        payload=payload,
        approved_by=approved_by,
        run_id=run_id,
    )


# --------------------------------------------------------------------------- #
# The actuator
# --------------------------------------------------------------------------- #


class EmailActuator:
    """Performs `notify.email`: checks the legal constraints, then sends -- or simulates.

    `sender=None` is the unconfigured case and it is not an error. It degrades to
    `FakeActuator`'s posture -- an outcome that is marked `fake` and carries a reason --
    which is the same rule `CLAUDE.md` states for the model router and
    `engines/serp/provider.py` states for search: a missing credential means the fake,
    plus a status that says so. Never a silent no-op, never a crash.

    The checks run BEFORE that branch on purpose. A refusal that only fires with a real
    key would be a refusal first exercised on a real recipient.
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
        message = parse_email_payload(actuation)

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
                        f"{RESEND_API_KEY_ENV} is not set, so no email left this process. "
                        "The legal checks still ran and passed."
                    ),
                    "provider": "fake",
                    "checks_passed": True,
                },
                fake=True,
            )

        message_id = await sender.send(message)

        # The only log line on the success path, and it carries no address, no subject
        # and no body -- a fingerprint is enough to correlate two sends to one person.
        logger.info(
            "email sent: provider=%s recipient_fp=%s message_id=%s",
            sender.name,
            recipient_fingerprint(message.recipient),
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


def _satisfies_actuator_protocol(actuator: EmailActuator) -> Actuator:
    """Compile-time proof that this satisfies the port. mypy checks this line."""
    return actuator


# --------------------------------------------------------------------------- #
# Configuration reporting and construction
# --------------------------------------------------------------------------- #


class EmailConfigStatus(BaseModel):
    """Whether email sends for real. Mirrors `llm.router.ConfigStatus` on purpose.

    The point of both is that a UI can say "simulated -- no credential configured" out
    loud, rather than leaving somebody to believe an email arrived.
    """

    configured: bool
    provider: str
    using_fake: bool
    message: str


def _read_key(env: Mapping[str, str], name: str) -> str | None:
    """A usable key, treating blank and whitespace-only as absent.

    `RESEND_API_KEY=` in a `.env` file is a very common way to "unset" a variable, and
    treating it as present would send an unauthenticated request and get a 401 instead of
    the fake.
    """
    value = env.get(name, "").strip()
    return value or None


def email_config_status(env: Mapping[str, str] | None = None) -> EmailConfigStatus:
    """Report whether email is real, without constructing a client."""
    environ = env if env is not None else os.environ
    key = _read_key(environ, RESEND_API_KEY_ENV)

    if key is None:
        return EmailConfigStatus(
            configured=False,
            provider="fake",
            using_fake=True,
            message=(
                f"No {RESEND_API_KEY_ENV} is set, so every email is SIMULATED: the legal "
                "checks run and the outcome is recorded, but nothing is delivered and no "
                f"recipient sees anything. Set {RESEND_API_KEY_ENV} to send for real."
            ),
        )
    return EmailConfigStatus(
        configured=True,
        provider=RESEND_PROVIDER,
        using_fake=False,
        message=(
            "Email sends for real, via Resend. Consent basis, unsubscribe link and "
            "sender identity are enforced per send; SPF/DKIM/DMARC on the sending "
            "domain are not checkable here and are verified at Resend."
        ),
    )


def build_email_actuator(
    env: Mapping[str, str] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> EmailActuator:
    """The email actuator this environment can actually use.

    Selection is by credential and by nothing else: no flag, no setting, no separate
    "enable email" switch that could disagree with whether a key exists.
    """
    environ = env if env is not None else os.environ
    key = _read_key(environ, RESEND_API_KEY_ENV)
    if key is None:
        return EmailActuator()
    return EmailActuator(ResendSender(key, timeout_s=timeout_s, client=client))
