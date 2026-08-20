"""``publish.page``: the one real publish in this product, and the reason it is real.

Every other actuator here is gated on somebody else's approval queue — Meta App Review,
LinkedIn Marketing Developer Platform, a TikTok audit (``docs/CHANNELS.md`` §2) — so
``social.post`` is complete up to an empty seam and honest about it. The landing page is
different in the way that matters: **this application serves it**
(``backend/app/api/pages.py``), so publishing needs no credential, no third party and no
network. There was never a reason for it to be simulated; there was only a missing caller.

That gap was larger than it looked. Nothing in the application called
``content_store.create_landing_page``, so no landing ``content_pieces`` row had ever been
written by a run, ``GET /p/{piece_id}`` could never serve anything, and **no tracked short
link was minted in any real run**. Run → page → tracked link → click → lead → attribution
is what ``docs/BUILD_ORDER.md`` Phase 8 calls the only screen that proves the product's
actual promise, and its first link was missing while EXPORT reported a cheerful simulated
publish over the hole.

Why this is an actuator and not four lines in the EXPORT node
------------------------------------------------------------

``docs/ARCHITECTURE.md`` §3 puts publishing to a surface under ``publish_cms``, and the
actuator layer is what buys the three things a node would have to reimplement:

* **Idempotency, which is not optional here.** EXPORT is reachable only by resuming a run
  past the REVIEW interrupt, and a human who resumes will resume twice. Without the
  content-derived key in ``actuate()`` every retry mints another page and another full set
  of short links — and the *first* page's links keep pointing at the first page, so half a
  campaign's clicks land on an orphan nobody is reading. The key holds because the payload
  is the spec: an edited page is a different effect and does publish.
* **The audit row**, written before the call, so a crash mid-publish is an ``in_flight``
  row somebody can chase rather than an invisible gap.
* **The approval gate.** ``Actuation.approved_by`` is required, so a page cannot go live
  without the human decision REVIEW exists to collect.

And one thing it avoids: a node calling ``publish_landing_page`` directly would put a
database write inside a graph node, which ``tests/test_engine_boundary.py`` is right to
object to.

``fake`` is ``False``, permanently
----------------------------------

Not a credential check that happens to pass — there is no credential to check. This is
the first outcome in the product for which ``Outcome.fake`` is legitimately ``False``, and
that is precisely what makes the Delivery tab useful: a single run can now carry a real
publish beside a simulated post, and the two no longer render identically.

Refusals
--------

Both possible refusals are ``ActuationRefusedError`` — policy, never retryable, never
alerted — because in both cases the thing that must change is the request, not the timing:

1. **The page cannot capture a lead.** ``LandingPageNotPublishableError`` carries the whole
   deterministic verdict, and the fix hints go back to the model. Publishing anyway would
   put a URL in somebody's bio that converts nothing while looking finished.
2. **The stored spec is not a spec.** ``landing_page`` is read back out of a JSONB
   checkpoint that an older version of this code may have written, and a reader of a JSONB
   column does not get to assume its own version wrote it. A malformed one is refused with
   the validation error rather than crashing the node that produced good content.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from backend.app.actuators.contract import (
    Actuation,
    ActuationRefusedError,
    Actuator,
    Outcome,
    OutcomeStatus,
)
from backend.app.engines.landing.contract import LandingPageSpec
from backend.app.services.landing_service import (
    ContentStore,
    LandingPageNotPublishableError,
    LinkStore,
    publish_landing_page,
)

__all__ = ["LandingPageActuator"]

#: Published, not `approved`. The human decision has already happened — `actuate()`
#: refuses without `approved_by` — so stopping at `approved` would leave the page needing
#: a second, invented approval that no screen asks for.
_PUBLISHED: Final = "published"


class LandingPageActuator:
    """Performs ``publish.page`` by writing the page and minting its tracked links."""

    def __init__(
        self,
        *,
        content_store: ContentStore,
        link_store: LinkStore,
        base_url: str | None = None,
    ) -> None:
        self._content_store = content_store
        self._link_store = link_store
        # None means "read `public_base_url` from settings at publish time". Injected
        # only by tests, which must not depend on the deployment's own base URL.
        self._base_url = base_url

    @property
    def action_type(self) -> str:
        return "publish.page"

    @property
    def fake(self) -> bool:
        """Never fake. There is no credential to be missing — we serve this page."""
        return False

    async def perform(self, actuation: Actuation) -> Outcome:
        """Publish the page, or refuse and say which rule stopped it."""
        spec = self._spec(actuation.payload)

        try:
            published = await publish_landing_page(
                business_id=actuation.business_id,
                spec=spec,
                content_store=self._content_store,
                link_store=self._link_store,
                run_id=actuation.run_id,
                status=_PUBLISHED,
                base_url=self._base_url,
            )
        except LandingPageNotPublishableError as exc:
            # The verdict, not just a sentence: the caller's next move is to feed the fix
            # hints back to the model, and a caller given only a message would have to
            # re-run the audit to get them.
            raise ActuationRefusedError(str(exc)) from exc

        return Outcome(
            status=OutcomeStatus.SUCCEEDED,
            action_type=actuation.action_type,
            target=actuation.target,
            # A real URL, because there is a real page at it. This is the field a
            # customer can check.
            external_ref=published.url,
            detail={
                "content_piece_id": str(published.content_piece_id),
                "path": published.path,
                "status": published.status,
                "score": published.report.score,
                # One entry per channel CTA, each with the tracked code that attributes
                # its own clicks. A1b carries these into the export pack.
                "ctas": [
                    {
                        "channel": cta.channel,
                        "text": cta.text,
                        "code": cta.code,
                        "path": cta.path,
                        "url": cta.url,
                    }
                    for cta in published.ctas
                ],
            },
        )

    @staticmethod
    def _spec(payload: Any) -> LandingPageSpec:
        """Validate the stored page back into a spec, refusing a malformed one."""
        try:
            return LandingPageSpec.model_validate(dict(payload))
        except (ValidationError, TypeError, ValueError) as exc:
            raise ActuationRefusedError(
                "the stored landing page is not a valid page spec, so there is nothing "
                f"safe to publish: {exc}"
            ) from exc


if TYPE_CHECKING:  # pragma: no cover - a compile-time conformance check

    def _satisfies_protocol(actuator: LandingPageActuator) -> Actuator:
        """Fails type checking the moment this drifts from the port."""
        return actuator
