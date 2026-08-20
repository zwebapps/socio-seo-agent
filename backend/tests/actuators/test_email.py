"""The email actuator: the legal gate, the credential seam, and what never reaches a log.

`docs/CHANNELS.md` §6 calls email "the one channel where a mistake is a fine, not a bad
metric", so most of what is worth asserting here is not the HTTP call. Four properties
carry the file:

* **A refusal is not a failure.** A missing unsubscribe link comes back `refused`; a
  provider that rejects the key comes back `failed`. Collapsing the two would either
  alert on policy working or hide a broken integration.
* **The checks run without a credential.** Every refusal is asserted on the FAKE
  actuator, which is the only way to know they would fire before a real recipient sees
  anything.
* **`retryable` is asserted per status, not assumed.** It is the field `actuate()`
  branches on, so getting 429 and 401 the wrong way round means either giving up on a
  blip or hammering a permanent failure.
* **Nothing sensitive reaches a log.** Asserted against `caplog` with a provider whose
  error body deliberately quotes the recipient, because that is exactly how an address
  leaks in real life.

Hermetic: HTTP is an injected `httpx.MockTransport` -- `pyproject.toml` notes that respx
cannot intercept the OpenAI SDK because that SDK is built on `httpx2`, but this module
uses plain `httpx`, so `httpx.MockTransport` is the right tool and there is no socket
either way. `backend/tests/conftest.py` strips `RESEND_API_KEY`, so the configured cases
pass `env={...}` explicitly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID, uuid4

import httpx
import pytest

from backend.app.actuators.actuate import actuate
from backend.app.actuators.contract import (
    Actuation,
    ActuationRefusedError,
    ActuatorError,
    Outcome,
    OutcomeStatus,
)
from backend.app.actuators.email import (
    ACTION_TYPE,
    LIST_UNSUBSCRIBE_HEADER,
    LIST_UNSUBSCRIBE_POST_HEADER,
    RESEND_API_KEY_ENV,
    EmailActuator,
    EmailMessage,
    ResendSender,
    build_email_actuation,
    build_email_actuator,
    email_config_status,
    parse_email_payload,
    recipient_fingerprint,
    recipient_target,
)

# An obviously fake key. Never a real credential, not even in a fixture.
FAKE_KEY: Final = "re_test-key-not-real"
CONFIGURED: Final[dict[str, str]] = {RESEND_API_KEY_ENV: FAKE_KEY}

BUSINESS_ID: Final[UUID] = UUID("11111111-2222-3333-4444-555555555555")

#: Strings that must never appear in a log record. `.invalid` and `.example` are
#: reserved, so nothing here can accidentally name a real person or domain.
RECIPIENT: Final = "annika.mueller@kunde.example"
SUBJECT: Final = "Your quarterly roof inspection is due"
SECRET_SENTENCE: Final = "Hallo Annika, your gutters were last cleaned in March."
UNSUBSCRIBE_URL: Final = "https://links.example/u/abc123"


def payload(**overrides: Any) -> dict[str, Any]:
    """A legally complete payload, so each test can break exactly one thing."""
    base: dict[str, Any] = {
        "to": RECIPIENT,
        "sender": "Dach & Co <hallo@dachundco.example>",
        "subject": SUBJECT,
        "html": f"<p>{SECRET_SENTENCE}</p><a href='{UNSUBSCRIBE_URL}'>Abmelden</a>",
        "text": f"{SECRET_SENTENCE}\n\nAbmelden: {UNSUBSCRIBE_URL}",
        "unsubscribe_url": UNSUBSCRIBE_URL,
        "consent_basis": "double_optin",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not _OMIT}


class _Omit:
    """Sentinel so a test can remove a key rather than blank it."""


_OMIT: Final = _Omit()


def actuation(**overrides: Any) -> Actuation:
    """A valid `notify.email` actuation, with `target` derived the way callers must.

    Built through `Actuation` directly rather than through `build_email_actuation` so that
    a test can produce a deliberately WRONG shape -- which the helper, by design, cannot.
    """
    payload_overrides: dict[str, Any] = overrides.pop("payload_overrides", {})
    body = payload(**payload_overrides)
    fields: dict[str, Any] = {
        "business_id": BUSINESS_ID,
        "action_type": ACTION_TYPE,
        "target": recipient_target(str(body.get("to", ""))),
        "payload": body,
        "approved_by": "user:owner-1",
    }
    fields.update(overrides)
    return Actuation(**fields)


# --------------------------------------------------------------------------- #
# Hermetic transport and a store stub
# --------------------------------------------------------------------------- #


@dataclass
class SenderStub:
    """A `ResendSender` wired to a transport that cannot reach a network."""

    sender: ResendSender
    requests: list[httpx.Request] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(self.requests[index].content)
        return parsed


def sender_stub(
    *,
    status: int = 200,
    body: Any = None,
    error: Exception | None = None,
) -> SenderStub:
    stub = SenderStub(sender=ResendSender(FAKE_KEY))

    def handler(request: httpx.Request) -> httpx.Response:
        stub.requests.append(request)
        if error is not None:
            raise error
        return httpx.Response(status, json=body if body is not None else {"id": "msg_abc123"})

    stub.sender = ResendSender(
        FAKE_KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return stub


class StoreStub:
    """The `actions` ledger, in a dict.

    `claim` reproduces the contract's unusual shape: it either reserves the key
    (returning None) or hands back the outcome that key already holds. That is the
    behaviour a replay depends on, so faking it any other way would test nothing.
    """

    def __init__(self) -> None:
        self.settled: dict[str, Outcome] = {}
        self.claims: list[str] = []

    async def claim(self, actuation: Actuation) -> Outcome | None:
        key = actuation.idempotency_key()
        self.claims.append(key)
        existing = self.settled.get(key)
        if existing is not None and existing.status is OutcomeStatus.SUCCEEDED:
            return existing
        return None

    async def settle(self, actuation: Actuation, outcome: Outcome) -> None:
        self.settled[actuation.idempotency_key()] = outcome


# --------------------------------------------------------------------------- #
# No credential: the fake, and it says so
# --------------------------------------------------------------------------- #


def test_no_key_selects_the_fake_and_the_status_says_so() -> None:
    """A missing credential is the fake plus a status, never a crash and never silence."""
    status = email_config_status(env={})

    assert status.using_fake is True
    assert status.configured is False
    assert status.provider == "fake"
    assert RESEND_API_KEY_ENV in status.message
    # The word a UI needs in order to be honest on screen.
    assert "SIMULATED" in status.message

    actuator = build_email_actuator(env={})
    assert actuator.fake is True
    assert actuator.provider == "fake"


def test_a_key_selects_resend() -> None:
    status = email_config_status(env=CONFIGURED)

    assert status.using_fake is False
    assert status.configured is True
    assert status.provider == "resend"
    assert build_email_actuator(env=CONFIGURED).fake is False


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_key_counts_as_absent(value: str) -> None:
    """`RESEND_API_KEY=` in a .env is how people unset things.

    Treating it as present would send an unauthenticated request and get a 401 where the
    fake was wanted.
    """
    assert email_config_status(env={RESEND_API_KEY_ENV: value}).using_fake is True


async def test_the_fake_outcome_says_out_loud_that_it_is_fake() -> None:
    """The whole point of the fake posture: a simulated send must be unmistakable."""
    store = StoreStub()

    outcome = await actuate(actuation(), actuator=build_email_actuator(env={}), store=store)

    assert outcome.succeeded
    assert outcome.fake is True
    assert outcome.detail["simulated"] is True
    # Actionable: it names the credential to set, not just "no credential".
    assert RESEND_API_KEY_ENV in str(outcome.detail["reason"])
    # And it is carried into the one-line summary a timeline renders.
    assert "SIMULATED" in outcome.summary()


async def test_the_fake_still_ran_the_legal_checks() -> None:
    """Reported, not merely done -- otherwise a reader cannot tell the fake skipped them."""
    outcome = await EmailActuator().perform(actuation())

    assert outcome.detail["checks_passed"] is True


# --------------------------------------------------------------------------- #
# The unsubscribe rule: enforced in the BODY, and enforced without a credential
# --------------------------------------------------------------------------- #


async def test_a_send_with_no_unsubscribe_mechanism_is_refused() -> None:
    """docs/CHANNELS.md section 6: unsubscribe in every send. No exceptions, no flag."""
    store = StoreStub()
    request = actuation(payload_overrides={"unsubscribe_url": _OMIT})

    outcome = await actuate(request, actuator=EmailActuator(), store=store)

    assert outcome.status is OutcomeStatus.REFUSED
    assert "unsubscribe" in (outcome.error or "")
    # A refusal is an auditable event: it leaves a row, it does not raise.
    assert store.settled[request.idempotency_key()].status is OutcomeStatus.REFUSED


async def test_an_unsubscribe_url_that_is_not_in_the_body_is_refused() -> None:
    """The failure this rule exists for: the field is set, the footer was dropped.

    A recipient cannot click a payload key, so checking the field alone would pass the
    exact template regression that matters.
    """
    request = actuation(
        payload_overrides={
            "html": f"<p>{SECRET_SENTENCE}</p>",
            "text": f"{SECRET_SENTENCE}\n\nAbmelden: {UNSUBSCRIBE_URL}",
        }
    )

    outcome = await actuate(request, actuator=EmailActuator(), store=StoreStub())

    assert outcome.status is OutcomeStatus.REFUSED
    assert "every body part" in (outcome.error or "")


def test_a_relative_unsubscribe_link_is_refused() -> None:
    """It resolves against the mail client and unsubscribes nobody."""
    with pytest.raises(ActuationRefusedError, match="absolute"):
        parse_email_payload(
            actuation(
                payload_overrides={
                    "unsubscribe_url": "/unsubscribe",
                    "html": "<p>hi</p><a href='/unsubscribe'>Abmelden</a>",
                    "text": "hi /unsubscribe",
                }
            )
        )


def test_the_unsubscribe_header_is_emitted_alongside_the_link() -> None:
    """RFC 2369 for the client, the in-body link for the person. Both, never one."""
    message = parse_email_payload(actuation())

    assert message.headers[LIST_UNSUBSCRIBE_HEADER] == f"<{UNSUBSCRIBE_URL}>"
    # One-click is NOT claimed by default: advertising it against a GET-only endpoint
    # turns every mailbox provider's unsubscribe button into a silent failure.
    assert LIST_UNSUBSCRIBE_POST_HEADER not in message.headers


def test_one_click_is_claimed_only_when_the_caller_declares_it() -> None:
    message = parse_email_payload(actuation(payload_overrides={"unsubscribe_one_click": True}))

    assert message.headers[LIST_UNSUBSCRIBE_POST_HEADER] == "List-Unsubscribe=One-Click"


# --------------------------------------------------------------------------- #
# The rest of the legal gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"consent_basis": _OMIT}, "consent_basis"),
        ({"consent_basis": "because we felt like it"}, "not a recognised basis"),
        ({"consent_basis": "purchased_list"}, "declared bad provenance"),
        ({"consent_basis": "scraped"}, "declared bad provenance"),
        # Deliberately absent from the vocabulary: a transactional message is genuinely
        # exempt from the unsubscribe rule, so admitting it here would admit an
        # exemption to the rule this module enforces hardest.
        ({"consent_basis": "transactional"}, "not a recognised basis"),
        ({"to": _OMIT}, "missing a usable 'to'"),
        ({"sender": _OMIT}, "sender"),
        ({"sender": "Dach & Co"}, "sender identity"),
        ({"subject": _OMIT}, "subject"),
        ({"subject": "   "}, "subject"),
        ({"html": _OMIT, "text": _OMIT}, "no body"),
    ],
)
def test_the_legal_gate_refuses(overrides: dict[str, Any], expected: str) -> None:
    with pytest.raises(ActuationRefusedError, match=expected):
        parse_email_payload(actuation(payload_overrides=overrides))


def test_more_than_one_recipient_is_refused() -> None:
    """One send, one recipient.

    A consent basis is recorded per RECIPIENT, and `contract.py` derives the idempotency
    key from `target` -- so a batch behind one target could not be retried for the
    addresses it missed without re-sending to everyone it reached.
    """
    with pytest.raises(ActuationRefusedError, match="more than one recipient"):
        parse_email_payload(
            actuation(payload_overrides={"to": f"{RECIPIENT},someone.else@kunde.example"})
        )


def test_a_display_name_is_a_valid_sender_identity() -> None:
    """A display name is part of identifying yourself, not an obstacle to it."""
    message = parse_email_payload(actuation())

    assert message.sender == "Dach & Co <hallo@dachundco.example>"


def test_a_refusal_names_the_rule_and_never_the_value() -> None:
    """Refusal text becomes `Outcome.error`, which `actuate()` logs.

    So it may say which rule failed and must not quote the address that failed it.
    """
    with pytest.raises(ActuationRefusedError) as caught:
        parse_email_payload(actuation(payload_overrides={"to": "not-an-address"}))

    assert "not-an-address" not in str(caught.value)


# --------------------------------------------------------------------------- #
# The target is a handle, not an address -- the rule that makes redaction possible
# --------------------------------------------------------------------------- #


def test_an_address_in_the_target_is_refused() -> None:
    """`actuate()` LOGS the target, so an address there cannot be kept out of a log.

    This module cannot edit `actuate.py` or `contract.py`, so refusing is the only place
    the guarantee can be made -- and a refusal that explains itself beats a leak nobody
    notices. Asserted here because it is the load-bearing half of "never log a recipient".
    """
    with pytest.raises(ActuationRefusedError, match="recipient_target"):
        parse_email_payload(actuation(target=RECIPIENT))


def test_a_target_for_a_different_recipient_is_refused() -> None:
    """The target is part of the idempotency key.

    A mismatch would let one key stand for a send to somebody else -- which is a replay
    that suppresses a real email, not a harmless inconsistency.
    """
    with pytest.raises(ActuationRefusedError, match="does not match"):
        parse_email_payload(actuation(target=recipient_target("someone.else@kunde.example")))


def test_the_target_carries_no_part_of_the_address() -> None:
    target = recipient_target(RECIPIENT)

    assert target.startswith("rcpt:")
    assert RECIPIENT not in target
    assert "annika" not in target
    assert "kunde.example" not in target


def test_build_email_actuation_derives_the_target_so_a_caller_cannot_get_it_wrong() -> None:
    """The helper exists precisely so the rule above is not something to remember."""
    built = build_email_actuation(
        business_id=BUSINESS_ID,
        to=RECIPIENT,
        sender="hallo@dachundco.example",
        subject=SUBJECT,
        unsubscribe_url=UNSUBSCRIBE_URL,
        consent_basis="double_optin",
        approved_by="user:owner-1",
        text=f"{SECRET_SENTENCE}\n\nAbmelden: {UNSUBSCRIBE_URL}",
    )

    assert built.action_type == ACTION_TYPE
    assert built.target == recipient_target(RECIPIENT)
    # Accepted by the gate it was built for: the helper and the validator agree.
    assert parse_email_payload(built).recipient == RECIPIENT


def test_build_email_actuation_omits_unset_fields_rather_than_storing_nulls() -> None:
    """`contract.py` hashes the payload.

    An explicit `"reply_to": None` and an absent key would be two idempotency keys for
    one email, so the same send assembled twice must produce the same payload.
    """
    kwargs: dict[str, Any] = {
        "business_id": BUSINESS_ID,
        "to": RECIPIENT,
        "sender": "hallo@dachundco.example",
        "subject": SUBJECT,
        "unsubscribe_url": UNSUBSCRIBE_URL,
        "consent_basis": "double_optin",
        "approved_by": "user:owner-1",
        "text": f"Abmelden: {UNSUBSCRIBE_URL}",
    }

    built = build_email_actuation(**kwargs)

    assert "reply_to" not in built.payload
    assert "html" not in built.payload
    assert "unsubscribe_one_click" not in built.payload
    assert built.idempotency_key() == build_email_actuation(**kwargs).idempotency_key()


# --------------------------------------------------------------------------- #
# The real send
# --------------------------------------------------------------------------- #


async def test_a_successful_send_returns_the_provider_message_id() -> None:
    """`external_ref` is the difference between "we sent it" and "we sent it, here"."""
    stub = sender_stub(body={"id": "msg_9f8e7d"})

    outcome = await actuate(actuation(), actuator=EmailActuator(stub.sender), store=StoreStub())

    assert outcome.succeeded
    assert outcome.fake is False
    assert outcome.external_ref == "msg_9f8e7d"
    assert stub.calls == 1


async def test_the_wire_payload_carries_one_recipient_and_both_body_parts() -> None:
    """A plain-text alternative on every send (docs/CHANNELS.md section 6)."""
    stub = sender_stub()

    await actuate(actuation(), actuator=EmailActuator(stub.sender), store=StoreStub())

    body = stub.body()
    # The address reaches the PROVIDER, which is the one place it has to.
    assert body["to"] == [RECIPIENT]
    assert body["from"] == "Dach & Co <hallo@dachundco.example>"
    assert body["html"] and body["text"]
    assert body["headers"][LIST_UNSUBSCRIBE_HEADER] == f"<{UNSUBSCRIBE_URL}>"
    assert stub.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"


async def test_an_accepted_send_with_no_message_id_is_a_non_retryable_failure() -> None:
    """Sent-but-we-cannot-say-where is exactly what `external_ref` exists to prevent.

    Non-retryable on purpose: the provider accepted it, so repeating the call is how one
    email becomes two.
    """
    stub = sender_stub(body={"ok": True})

    with pytest.raises(ActuatorError) as caught:
        await EmailActuator(stub.sender).perform(actuation())

    assert caught.value.retryable is False


# --------------------------------------------------------------------------- #
# retryable: the field callers actually branch on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [429, 408, 500, 502, 503])
async def test_a_rate_limit_or_a_bad_day_is_retryable(status: int) -> None:
    """The identical request would succeed at a different time, so say so."""
    stub = sender_stub(status=status, body={"name": "rate_limit_exceeded"})

    with pytest.raises(ActuatorError) as caught:
        await EmailActuator(stub.sender).perform(actuation())

    assert caught.value.retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_a_rejected_key_or_a_refused_recipient_is_not_retryable(status: int) -> None:
    """The request has to change, not the timing.

    401/403 is a rejected key or an unverified sending domain; 422 is a recipient Resend
    will not accept. Retrying any of them hammers a permanent failure.
    """
    stub = sender_stub(status=status, body={"name": "validation_error"})

    with pytest.raises(ActuatorError) as caught:
        await EmailActuator(stub.sender).perform(actuation())

    assert caught.value.retryable is False


async def test_a_transport_failure_is_retryable() -> None:
    """A dropped connection says nothing about whether the request was acceptable."""
    stub = sender_stub(error=httpx.ConnectTimeout("timed out"))

    with pytest.raises(ActuatorError) as caught:
        await EmailActuator(stub.sender).perform(actuation())

    assert caught.value.retryable is True


async def test_actuate_records_retryable_on_the_outcome() -> None:
    """`actuate()` is what a graph node sees, and it must carry the distinction through."""
    stub = sender_stub(status=429, body={"name": "rate_limit_exceeded"})

    outcome = await actuate(actuation(), actuator=EmailActuator(stub.sender), store=StoreStub())

    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.detail["retryable"] is True


# --------------------------------------------------------------------------- #
# Logging: no body, no subject, no address
# --------------------------------------------------------------------------- #


async def test_the_body_and_the_recipient_never_reach_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asserted on the SUCCESS path and on the failure path, at DEBUG.

    The failure case is the one that catches real leaks: this provider's error body
    quotes the recipient the way Resend's `message` field does, and the mapping reads only
    `name`. A version that echoed the provider's message would put the address into
    `Outcome.error`, which `actuate()` logs.
    """
    leaky_error = {
        "name": "validation_error",
        "message": f"Invalid `to` field: {RECIPIENT} is not verified",
    }

    with caplog.at_level(logging.DEBUG):
        await actuate(actuation(), actuator=EmailActuator(sender_stub().sender), store=StoreStub())
        await actuate(
            actuation(),
            actuator=EmailActuator(sender_stub(status=422, body=leaky_error).sender),
            store=StoreStub(),
        )

    assert caplog.records, "the assertion below would pass vacuously with no log records"
    logged = "\n".join(
        f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for record in caplog.records
    )
    for secret in (RECIPIENT, SUBJECT, SECRET_SENTENCE, "annika"):
        assert secret.lower() not in logged.lower(), f"{secret!r} leaked into a log record"


async def test_the_success_log_carries_a_fingerprint_instead_of_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Correlation without an address: two sends to one person are still tied together.

    Without this the previous test would also pass if the module simply logged nothing,
    and a channel with no operational log at all is not the goal.
    """
    with caplog.at_level(logging.INFO):
        await actuate(actuation(), actuator=EmailActuator(sender_stub().sender), store=StoreStub())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert recipient_fingerprint(RECIPIENT) in logged


def test_the_fingerprint_is_stable_and_case_insensitive() -> None:
    """Otherwise two sends to one person would not correlate, which is its only job."""
    assert recipient_fingerprint(RECIPIENT) == recipient_fingerprint(f"  {RECIPIENT.upper()}  ")
    assert recipient_fingerprint(RECIPIENT) != recipient_fingerprint("someone.else@kunde.example")
    assert RECIPIENT not in recipient_fingerprint(RECIPIENT)


# --------------------------------------------------------------------------- #
# Idempotency, through actuate()
# --------------------------------------------------------------------------- #


async def test_a_replay_does_not_send_twice() -> None:
    """The property this whole layer exists for: two calls, one email.

    The replayed outcome carries the FIRST send's `external_ref` -- reporting it as new
    would tell a customer they have two emails, and reporting an error would tell them
    they have none.
    """
    stub = sender_stub(body={"id": "msg_once"})
    actuator = EmailActuator(stub.sender)
    store = StoreStub()
    request = actuation()

    first = await actuate(request, actuator=actuator, store=store)
    second = await actuate(request, actuator=actuator, store=store)

    assert stub.calls == 1, "the provider was called twice for one logical send"
    assert first.replayed is False
    assert second.replayed is True
    assert second.external_ref == "msg_once"


async def test_an_edited_email_is_a_new_send_rather_than_a_replay() -> None:
    """The key is derived from CONTENT, so a corrected email does go out."""
    stub = sender_stub()
    actuator = EmailActuator(stub.sender)
    store = StoreStub()

    await actuate(actuation(), actuator=actuator, store=store)
    await actuate(
        actuation(payload_overrides={"subject": "Corrected: your roof inspection"}),
        actuator=actuator,
        store=store,
    )

    assert stub.calls == 2


async def test_a_send_with_no_approval_is_refused_before_the_provider() -> None:
    """`actuate()` owns this, and the email actuator must not be reachable around it."""
    stub = sender_stub()

    outcome = await actuate(
        actuation(approved_by="  "), actuator=EmailActuator(stub.sender), store=StoreStub()
    )

    assert outcome.status is OutcomeStatus.REFUSED
    assert stub.calls == 0


async def test_a_wrong_action_type_never_reaches_the_email_provider() -> None:
    """Publishing a LinkedIn post through the email actuator must not "succeed"."""
    stub = sender_stub()

    outcome = await actuate(
        actuation(action_type="social.post"), actuator=EmailActuator(stub.sender), store=StoreStub()
    )

    assert outcome.status is OutcomeStatus.REFUSED
    assert stub.calls == 0


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_the_actuator_answers_to_the_contract_name() -> None:
    """`notify.email` is the name `contract.py` documents; a typo here is a dead route."""
    assert EmailActuator().action_type == "notify.email"
    assert ACTION_TYPE == "notify.email"


def test_body_parts_counts_only_what_is_transmitted() -> None:
    """The unsubscribe check iterates this, so an empty part must not be checkable."""
    assert EmailMessage(
        sender="a@b.example", recipient="c@d.example", subject="s", text="only text"
    ).body_parts == ("only text",)


def test_an_actuation_for_a_different_business_is_a_different_key() -> None:
    """Tenant scoping reaches the idempotency key, or two businesses share a send."""
    mine = actuation()
    theirs = actuation(business_id=uuid4())

    assert mine.idempotency_key() != theirs.idempotency_key()
