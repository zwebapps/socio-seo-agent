"""The review projection: four tabs out of one checkpoint.

Every test here is hermetic — the projection is a pure function of a mapping, which is
the whole reason it lives in a service rather than in the route.

The load-bearing test is
``test_the_projection_reads_a_checkpoint_the_real_state_produced``. It builds a real
``AgentState``, puts it through the real ``to_checkpoint``, and projects THAT. A test
using a hand-written dict would keep passing on the day the state's field names change,
and the review screen would go blank in production while the suite stayed green — which
is exactly the "feature existed on both sides of a gap with nothing across it" failure
this codebase has already had once, with ``remembered``.

The second theme is absence. A review screen that fabricates content would be a
straightforward lie about a product whose entire claim is that output is grounded, so
"nothing here" must be a designed, tested answer that also says WHY.
"""

from decimal import Decimal
from typing import Any
from uuid import uuid4

from backend.app.agents.state import new_state, to_checkpoint
from backend.app.engines.seo import SeoScoreRequest, score_page
from backend.app.services.review_service import ExportPack, project_review

# --------------------------------------------------------------------------- #
# Absence is an answer, and it names the node
# --------------------------------------------------------------------------- #


def test_an_empty_checkpoint_reports_no_output_rather_than_raising() -> None:
    review = project_review({})

    assert review.has_output is False
    assert review.draft is None
    assert review.seo is None
    assert review.social == ()
    assert review.ai_blocks is None


def test_a_missing_checkpoint_is_the_same_answer_as_an_empty_one() -> None:
    """``runs.checkpoint`` has a ``'{}'::jsonb`` server default, but a None must not 500."""
    assert project_review(None).has_output is False


def test_every_empty_tab_carries_a_note_naming_the_node_that_fills_it() -> None:
    """ "No data" is not an answer anyone can act on; "GENERATE has not run" is."""
    review = project_review({"fact_gaps": [], "errors": []})

    assert review.draft_note is not None and "GENERATE" in review.draft_note
    assert review.seo_note is not None and "VALIDATE" in review.seo_note
    assert review.social_note is not None and "REPACK" in review.social_note
    assert review.ai_blocks_note is not None and "PLAN" in review.ai_blocks_note


def test_a_draft_with_no_title_and_no_body_is_reported_absent_not_present() -> None:
    """Otherwise the owner is handed an empty page to approve."""
    review = project_review({"draft": {"title": "", "html": "", "meta_description": "x"}})

    assert review.draft is None
    assert review.draft_note is not None


def test_an_outline_that_carried_no_answer_blocks_says_the_model_returned_none() -> None:
    """Distinct from "PLAN never ran": one is a missing step, the other is a choice.

    ``answer_blocks`` is optional on the PLAN tool schema, so an empty list is a real
    outcome and the screen must not imply the field was lost.
    """
    review = project_review({"outline": {"target_keyword": "notar koblenz", "headings": ["h"]}})

    assert review.ai_blocks is not None
    assert review.ai_blocks.blocks == ()
    assert review.ai_blocks_note is not None
    assert "optional" in review.ai_blocks_note
    assert "PLAN" in review.ai_blocks_note


# --------------------------------------------------------------------------- #
# A malformed checkpoint degrades; it never raises
# --------------------------------------------------------------------------- #


def test_junk_in_the_checkpoint_degrades_rather_than_making_a_run_unreviewable() -> None:
    """``checkpoint`` is JSONB: it can hold whatever an older version of the state, a
    partial write or a hand-run statement put there. A 500 here is indistinguishable
    from an outage to the person trying to review their content."""
    review = project_review(
        {
            "draft": "not a mapping",
            "seo_report": 42,
            "renderings": ["not", "a", "mapping"],
            "outline": None,
            "fact_gaps": "not a list",
            "errors": {"not": "a list"},
        }
    )

    assert review.has_output is False
    assert review.social == ()
    assert review.fact_gaps == ()
    assert review.errors == ()


def test_a_finding_without_a_code_is_dropped_rather_than_rendered_blank() -> None:
    review = project_review(
        {
            "seo_report": {
                "score": 70,
                "passed": False,
                "findings": [
                    {"code": "", "severity": "error", "message": "m", "fix_hint": "h"},
                    {"code": "title_length", "severity": "error", "message": "m", "fix_hint": "h"},
                ],
            }
        }
    )

    assert review.seo is not None
    assert [f.code for f in review.seo.findings] == ["title_length"]


def test_a_blank_rendering_is_not_offered_as_a_publishable_post() -> None:
    review = project_review({"renderings": {"linkedin": "   ", "x": "real copy"}})

    assert [p.channel for p in review.social] == ["x"]


# --------------------------------------------------------------------------- #
# The content that is there
# --------------------------------------------------------------------------- #


def test_the_fix_hint_survives_verbatim_because_it_is_the_actionable_half() -> None:
    """``message`` says something is wrong; ``fix_hint`` says what to change. A screen
    that dropped the hint would be a screen nobody can act on."""
    hint = "The title is 21 characters; write 50-60."
    review = project_review(
        {
            "seo_report": {
                "score": 62,
                "passed": False,
                "findings": [
                    {
                        "code": "title_length",
                        "severity": "error",
                        "message": "Title too short.",
                        "fix_hint": hint,
                        "measured": 21.0,
                        "expected": "50-60 characters",
                    }
                ],
            }
        }
    )

    assert review.seo is not None
    finding = review.seo.findings[0]
    assert finding.fix_hint == hint
    assert finding.measured == 21.0
    assert finding.expected == "50-60 characters"


def test_a_social_post_carries_the_character_count_the_server_measured() -> None:
    """Arithmetic over the text in hand."""
    review = project_review({"renderings": {"linkedin": "abcde"}})

    assert review.social[0].characters == 5


def test_an_old_flat_string_rendering_still_projects() -> None:
    """REPACK stored a bare body string before hashtags were kept, and those rows are
    still in the database. Nothing migrates a JSONB display field, so an older run must
    keep rendering -- with no hashtag information, which is true of it."""
    review = project_review({"renderings": {"linkedin": "Kurz erklärt."}})

    post = review.social[0]
    assert post.body == "Kurz erklärt."
    assert post.hashtags == ()
    assert post.hashtags_removed == 0


def test_a_social_post_carries_the_hashtags_and_the_limits_it_was_held_to() -> None:
    """The limits ship now because there is one spec table the runtime renders to and
    the rubric grades against -- so the number on the screen is the number the post was
    actually held to, rather than a third copy that can contradict either."""
    review = project_review(
        {
            "renderings": {
                "linkedin": {
                    "body": "Kurz erklärt. #Notar",
                    "hashtags": ["#Notar"],
                    "hashtags_removed": 4,
                    "hashtags_shortfall": 0,
                    "over_target": False,
                }
            }
        }
    )

    post = review.social[0]
    assert post.hashtags == ("#Notar",)
    assert post.hashtags_removed == 4, "what code had to cut is evidence about the model"
    assert post.character_target == 1_700
    assert post.character_limit == 3_000
    assert post.hashtag_limit == 3


def test_a_channel_with_no_spec_reports_no_limits_rather_than_zeros() -> None:
    """`0 / 0` would be a false limit, and a screen showing one is worse than a screen
    showing none."""
    review = project_review({"renderings": {"pigeon_post": {"body": "hello"}}})

    post = review.social[0]
    assert post.character_limit is None
    assert post.hashtag_limit is None


def test_the_projection_does_not_re_sort_the_channels() -> None:
    """It preserves whatever order the mapping gives it, and imposes none of its own.

    Note what this does NOT prove: that the UI sees insertion order. The checkpoint is a
    JSONB column and Postgres normalises object key order, so in production these arrive
    as `facebook, linkedin, instagram` regardless of what REPACK wrote — checked against a
    real row. The order is arbitrary but deterministic, and inventing a sort here would
    present a channel priority the product does not actually have.
    """
    review = project_review({"renderings": {"linkedin": "a", "facebook": "b", "instagram": "c"}})

    assert [p.channel for p in review.social] == ["linkedin", "facebook", "instagram"]


def test_fact_gaps_reach_the_screen_so_it_can_say_what_was_missing() -> None:
    """Without this the screen implies research that did not happen, which is the
    claims-discipline rule in docs/CRITERIA_MAP.md section 7."""
    review = project_review(
        {"fact_gaps": ["search results (no provider configured)", "uploaded documents"]}
    )

    assert review.fact_gaps == (
        "search results (no provider configured)",
        "uploaded documents",
    )


def test_node_errors_reach_the_screen_with_their_node_and_code() -> None:
    review = project_review(
        {"errors": [{"node": "HARVEST", "code": "crawl_failed", "message": "site timed out"}]}
    )

    assert review.errors == (
        {"node": "HARVEST", "code": "crawl_failed", "message": "site timed out"},
    )


# --------------------------------------------------------------------------- #
# The contract with the real state. This is the test that matters.
# --------------------------------------------------------------------------- #


def test_the_projection_reads_a_checkpoint_the_real_state_produced() -> None:
    """End to end through the REAL serialiser, and through the REAL SEO engine.

    If a field is renamed in ``AgentState`` or in ``SeoScoreResult``, this fails. A test
    built from a hand-written dict would not, and the review screen would silently go
    blank while the suite stayed green.
    """
    state = new_state(business_id=uuid4(), goal="more local leads")
    state["outline"] = {
        "target_keyword": "notar koblenz",
        "secondary_keywords": ["beurkundung"],
        "headings": ["Was ein Notar beurkundet", "Kosten"],
        "answer_blocks": [
            "Ein Notar in Koblenz beurkundet Grundstückskaufverträge.",
            "Die Gebühren richten sich nach dem GNotKG.",
        ],
        "cta": "Termin anfragen",
    }
    state["opportunity"] = {
        "title": "Notarkosten erklären",
        "rationale": "Three competitors rank with thin pages on this term.",
        "score": 82,
    }
    state["draft"] = {
        "title": "Notar in Koblenz: Beurkundung und Kosten erklärt",
        "meta_description": "Was ein Notar in Koblenz beurkundet und was es kostet.",
        "html": "<h1>Notar in Koblenz</h1><p>Beurkundung von Kaufverträgen.</p>",
    }
    # The real deterministic engine, so `findings` has the real shape and real codes.
    state["seo_report"] = score_page(
        SeoScoreRequest(
            html=(
                "<html><head><title>Notar in Koblenz: Beurkundung und Kosten</title>"
                '<meta name="description" content="Was ein Notar in Koblenz macht.">'
                "</head><body><h1>Notar in Koblenz</h1><p>Beurkundung.</p></body></html>"
            ),
            target_keyword="notar koblenz",
            locale="de",
        )
    ).model_dump(mode="json")
    state["renderings"] = {
        "linkedin": {
            "body": "Was ein Notar beurkundet — kurz erklärt. #Notar",
            "hashtags": ["#Notar"],
            "hashtags_removed": 0,
            "hashtags_shortfall": 0,
            "over_target": False,
        }
    }
    state["fact_gaps"] = ["uploaded documents"]
    state["cost_usd"] = Decimal("0.0123")

    checkpoint: dict[str, Any] = to_checkpoint(state)
    review = project_review(checkpoint)

    assert review.has_output is True
    assert review.draft is not None
    assert review.draft.title == "Notar in Koblenz: Beurkundung und Kosten erklärt"
    assert "<h1>" in review.draft.html
    assert review.draft.meta_description.startswith("Was ein Notar")

    assert review.seo is not None
    assert review.seo.findings, "the real engine always returns findings, passes included"
    # Every finding the engine can emit must survive the projection with a usable code.
    assert all(f.code for f in review.seo.findings)

    assert review.ai_blocks is not None
    assert review.ai_blocks.target_keyword == "notar koblenz"
    assert len(review.ai_blocks.blocks) == 2
    assert review.ai_blocks.cta == "Termin anfragen"
    assert review.ai_blocks_note is None, "blocks are present, so there is nothing to explain"

    assert [p.channel for p in review.social] == ["linkedin"]
    assert review.opportunity is not None
    assert review.opportunity.score == 82
    assert review.fact_gaps == ("uploaded documents",)


def test_a_fresh_state_projects_as_nothing_to_review_not_as_empty_content() -> None:
    """A run that has only just been created must not look like a run that produced
    blank output — those are different things to the person waiting for a draft."""
    checkpoint = to_checkpoint(new_state(business_id=uuid4(), goal="g"))
    review = project_review(checkpoint)

    assert review.has_output is False
    assert review.draft is None and review.draft_note is not None
    assert review.seo is None and review.seo_note is not None


# --------------------------------------------------------------------------- #
# What EXPORT did, and what MEASURE could not do
# --------------------------------------------------------------------------- #


def test_an_unapproved_run_says_the_gate_is_why_nothing_published() -> None:
    """The normal state of most runs, and it must not read as a fault.

    REVIEW is an interrupt and EXPORT sits after it, so "nothing published" is the
    expected condition of every run nobody has approved. A note naming the GATE is
    actionable; a blank section or a zero would read as broken software.
    """
    review = project_review({"draft": {"title": "t", "html": "<h1>t</h1>"}})

    assert review.published is None
    assert review.published_note is not None
    assert "approves" in review.published_note
    assert review.measurement is None
    assert "after EXPORT" in (review.measurement_note or "")


def test_a_simulated_publish_can_never_be_rendered_as_a_real_one() -> None:
    """The single most important assertion on this screen. Verified against the exact
    shape a real run wrote: a landing page simulated because no credential is configured,
    and two channels REFUSED because the business has no connection — which is the honest
    answer and not a cheerful fake success."""
    review = project_review(
        {
            "published": {
                "note": (
                    "Published 1 of 3; nothing was published to facebook, linkedin -- "
                    "SIMULATED: at least one destination has no credential configured"
                ),
                "attempted": 3,
                "simulated": True,
                "notified": False,
                "notify_note": (
                    "Nobody was told: this business profile has no email address on record."
                ),
                "refs": [
                    {
                        "action_type": "publish.page",
                        "target": "landing_page",
                        "status": "succeeded",
                        "external_ref": "fake://publish.page/landing_page#8fe88144",
                        "error": None,
                        "fake": True,
                        "summary": (
                            "publish.page → landing_page: done "
                            "(SIMULATED — no credential configured)"
                        ),
                    },
                    {
                        "action_type": "social.post",
                        "target": "linkedin",
                        "status": "refused",
                        "external_ref": None,
                        "error": "this business has no linkedin connection",
                        "fake": True,
                        "summary": (
                            "social.post → linkedin: refused "
                            "(this business has no linkedin connection)"
                        ),
                    },
                ],
            }
        }
    )

    assert review.published is not None
    assert review.published.simulated is True
    assert review.published.succeeded == 1
    assert review.published.attempted == 3
    # Every target carries its own flag, so a row cannot be rendered as real either.
    assert all(target.simulated for target in review.published.targets)
    # And the refusal keeps its reason, which is the thing the owner can act on.
    refused = [t for t in review.published.targets if t.status == "refused"]
    assert refused and "no linkedin connection" in (refused[0].error or "")
    assert "SIMULATED" in review.published.note
    assert review.published.notified is False
    assert "no email address" in (review.published.notify_note or "")


def test_leads_are_reported_as_unmeasured_rather_than_zero() -> None:
    """The product's whole argument is that attribution must be trustworthy, and "zero
    leads" and "nobody has arrived through a tracked link yet" are the same number and
    different claims. Only the second one is true minutes after publishing."""
    review = project_review(
        {
            "measurement": {
                "published_refs": 1,
                "channels": ["landing_page"],
                "simulated": True,
                "gaps": ["Google Search Console / GA4 (cut from this build)"],
                "attribution": {
                    "leads_measured": False,
                    "note": (
                        "No leads are attributable yet: the tracked links were "
                        "published moments ago."
                    ),
                    "channels": ["landing_page"],
                },
            }
        }
    )

    assert review.measurement is not None
    assert review.measurement.leads_measured is False
    # The note has to explain WHY there is no number, or the screen shows an empty
    # attribution panel and the reader supplies "it does not work" themselves.
    assert "attributable yet" in (review.measurement.attribution_note or "")
    assert review.measurement.gaps, "what was NOT measured has to reach the screen"
    assert review.measurement.simulated is True


def test_the_export_pack_carries_no_publish_status() -> None:
    """Deliberate. The pack is what a human pastes into a composer; a publish claim on
    the one surface whose point is that it publishes nothing would be the most misleading
    field in the product."""
    fields = set(ExportPack.model_fields)

    assert "published" not in fields
    assert "measurement" not in fields


# --------------------------------------------------------------------------- #
# The retrieval trace: the agentic-RAG evidence, projected
# --------------------------------------------------------------------------- #


def _trace_entry(**over: Any) -> dict[str, Any]:
    """A stored trace as `agents.nodes.summarise_retrieval` writes one.

    Hand-written HERE on purpose, unlike the round-trip test above: this function is
    the DEFENSIVE reader, and its job is to survive shapes the summariser would never
    write. The contract with the real summariser is asserted in
    `tests/agents/test_nodes.py`, through the real state and the real serialiser.
    """
    entry: dict[str, Any] = {
        "seq": 1,
        "node": "GENERATE",
        "question": "notdienst koblenz",
        "needed": True,
        "need_reason": "The page states a price.",
        "outcome": "fallback_to_web",
        "outcome_reason": "No passage graded relevant, so the run continues on live research.",
        "prompt_version": "kb_retrieve.v1",
        "attempts": [
            {
                "attempt": 1,
                "query": "notdienst anfahrt kosten",
                "query_rationale": "The words the price list would use.",
                "decision": "exhausted",
                "decision_reason": "Two attempts is the ceiling.",
                "relevant": 0,
                "partial": 1,
                "irrelevant": 1,
                "grades": [
                    {
                        "chunk_id": "chunk-a",
                        "document_id": "doc-1",
                        "ordinal": 2,
                        "grade": "partial",
                        "reason": "Mentions a fee but not this one.",
                        "distance": 0.23,
                    }
                ],
                "grades_total": 2,
                "notes": [],
            }
        ],
        "attempts_total": 1,
        "grounding_chunk_ids": [],
        "chunk_count": 1,
        "model_calls": 3,
        "cost_usd": "0.0042",
        "notes": [],
    }
    entry.update(over)
    return entry


def test_the_retrieval_panel_carries_the_query_the_grades_and_the_decision() -> None:
    """The three things that were being discarded at the call site. Without them the
    "agentic RAG" claim is about code nobody can look at."""
    review = project_review({"retrieval_traces": [_trace_entry()]})

    assert review.retrieval_note is None
    (trace,) = review.retrieval
    assert trace.node == "GENERATE"
    assert trace.outcome == "fallback_to_web"
    assert trace.outcome_reason is not None and "live research" in trace.outcome_reason
    (attempt,) = trace.attempts
    assert attempt.query == "notdienst anfahrt kosten"
    assert attempt.decision == "exhausted"
    (grade,) = attempt.grades
    assert (grade.chunk_id, grade.grade) == ("chunk-a", "partial")
    assert grade.reason == "Mentions a fee but not this one."


def test_no_documents_reads_as_a_normal_run_and_names_the_node_that_recorded_it() -> None:
    """The empty state that is easiest to get wrong. A business that uploaded nothing
    has nothing to retrieve, so nothing was retrieved -- that is a complete run, and a
    note that implied a fault would send the owner hunting a bug instead of uploading
    a PDF."""
    review = project_review({"fact_gaps": ["uploaded documents"], "errors": []})

    assert review.retrieval == ()
    note = review.retrieval_note
    assert note is not None
    assert "HARVEST" in note, "absence names the node that recorded it, as every note here does"
    assert "normal run" in note
    for word in ("fail", "error", "unavailable", "broke", "could not"):
        assert word not in note.lower(), (
            f"{word!r} would report the absence of a knowledge base as a defect in the agent"
        )


def test_a_checkpoint_written_before_the_key_existed_projects_without_raising() -> None:
    """Nothing migrates a JSONB column. An older run simply has no `retrieval_traces`,
    and from the reader's side that is the same answer as a business with no documents:
    there is no retrieval evidence, and nothing failed."""
    assert project_review({"draft": {"title": "t", "html": "<p>x</p>"}}).retrieval == ()
    assert project_review({"retrieval_traces": "not a list"}).retrieval == ()
    assert project_review({"retrieval_traces": None}).retrieval_note is not None


def test_a_trace_that_cannot_say_which_node_asked_is_dropped_not_rendered() -> None:
    """The panel is organised per node, so a trace with no node is evidence about
    nothing -- and one malformed entry must not make a run unreviewable."""
    review = project_review(
        {
            "retrieval_traces": [
                "junk",
                {"node": ""},
                {"attempts": []},
                _trace_entry(node="PLAN", seq=4),
            ]
        }
    )

    assert [t.node for t in review.retrieval] == ["PLAN"]
    assert review.retrieval[0].seq == 4, (
        "a first entry numbered 4 is how a trimmed panel says it is not the whole story"
    )


def test_a_trace_missing_its_totals_reports_what_it_listed_rather_than_zero() -> None:
    """`attempts_total` and `grades_total` exist to REVEAL a trim. A zero default would
    claim everything was trimmed on a checkpoint that simply never wrote the count."""
    entry = _trace_entry()
    del entry["attempts_total"]
    del entry["attempts"][0]["grades_total"]
    del entry["seq"]

    (trace,) = project_review({"retrieval_traces": [entry]}).retrieval

    assert trace.attempts_total == 1
    assert trace.attempts[0].grades_total == 1
    assert trace.seq == 1, "position in the list is the fallback ordinal"


def test_no_chunk_body_text_can_reach_the_wire_because_none_is_read() -> None:
    """The summariser drops chunk bodies before the state is written, and this reader
    does not look for them either -- so a hand-edited checkpoint that smuggled one in
    still cannot get it onto the review screen."""
    entry = _trace_entry()
    entry["attempts"][0]["grades"][0]["excerpt"] = "SECRET DOCUMENT TEXT"
    entry["attempts"][0]["grades"][0]["content"] = "SECRET DOCUMENT TEXT"

    review = project_review({"retrieval_traces": [entry]})

    assert "SECRET DOCUMENT TEXT" not in review.model_dump_json()
