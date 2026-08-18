"""Typed contract for the `seo` engine.

This mirrors `docs/ARCHITECTURE.md` section 3. The shapes are a published
contract: the API serialises `SeoScoreResult` to the review screen, and the
agent runtime feeds `SeoFinding.fix_hint` back into `GENERATE` verbatim
(`docs/AGENT_RUNTIME.md` section 7). Renaming a field or a code is therefore a
breaking change to two consumers, not a local refactor.

Every field is deliberately primitive. An engine result crosses a process
boundary as JSON, so nothing here may carry behaviour or a non-serialisable
type.
"""

from typing import Literal

from pydantic import BaseModel, Field

# One code per rule. The set is closed on purpose: the frontend renders a row
# per code and the retry loop keys hints by code, so a new rule is a deliberate
# contract change rather than a new free-text string appearing in the UI.
type SeoFindingCode = Literal[
    "title_length",
    "meta_length",
    "keyword_density",
    "readability",
    "heading_tree",
    "internal_links",
    "external_links",
    "image_alt",
    "schema_invalid",
]

# "info" is a *passing* rule. Keeping passes in the findings list is what lets
# the review screen show a full green/amber/red checklist instead of only the
# problems, and it costs nothing: `info` carries zero score penalty and an
# empty `fix_hint`, so it can never reach the model.
type SeoSeverity = Literal["error", "warn", "info"]


class SeoScoreRequest(BaseModel):
    """Everything the scorer needs, and nothing it could use to reach the world.

    `html` is a string rather than a URL precisely so this engine performs no
    I/O: fetching is the `crawl` engine's job, and passing the already-fetched
    markup keeps scoring deterministic and unit-testable.
    """

    html: str
    target_keyword: str
    secondary_keywords: list[str] = Field(default_factory=list)
    locale: str = "en"


class SeoFinding(BaseModel):
    """One rule's verdict.

    `message` is written for a human reading the review screen. `fix_hint` is
    written for the content model and must be quantitative -- it names the
    measured value and the target, because the retry loop is only as good as
    these hints. A passing rule carries `fix_hint=""`.
    """

    code: SeoFindingCode
    severity: SeoSeverity
    message: str
    fix_hint: str
    measured: float | None
    expected: str


class SeoScoreResult(BaseModel):
    """The deterministic verdict for one page.

    `passed` is stored rather than computed on read so that a persisted result
    can never disagree with the score it was computed from (a later change to
    the threshold must not silently re-judge historical runs).
    """

    score: int
    findings: list[SeoFinding]
    passed: bool

    @property
    def fix_hints(self) -> list[str]:
        """The hints to feed back to `GENERATE`, in rule order.

        A convenience for the validation loop: passing rules are excluded, so
        the model only ever sees what actually failed.
        """
        return [f.fix_hint for f in self.findings if f.severity != "info" and f.fix_hint]

    @property
    def errors(self) -> list[SeoFinding]:
        """Error-severity findings -- any one of these blocks `passed`."""
        return [f for f in self.findings if f.severity == "error"]
