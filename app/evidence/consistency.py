"""M2: cross-modal consistency scanning (prose vs. figure/table).

For every figure/table that the paper prose *mentions*, compare what the prose
claims against what is actually visible in the crop image - independent of the
analysis stage (no claim is needed). Conflicts are written to evidence_reviews
as automatic doubts so the human review queue surfaces them.

Methodology lives in the ``cross-modal-consistency`` skill; this module only
orchestrates the single structured call, the consensus mapping and storage.

Design notes:
- Runs only for visuals that have prose mentions (mentions are the anchor the
  check needs); visuals without mentions are skipped.
- Never overwrites an existing review (human or automatic).
- Best effort: a failing visual is skipped and left for the human.
"""

import logging
from time import perf_counter

from app.evidence.visual_verifier import resolve_visual_source
from app.schemas import (
    CrossModalConsistencyReport,
    EvidenceNode,
    ParsedDocument,
)
from app.skills import SkillRegistry

logger = logging.getLogger(__name__)

FALLBACK_SYSTEM = (
    "Compare what the paper prose claims about this figure/table against what is "
    "actually visible in the image. For each numbered prose mention decide: "
    "consistent (visible content directly supports it), inconsistent (visible "
    "content contradicts it), or unverifiable (image lacks the detail to tell). "
    "Base every decision on visible content only. Optional normalized regions "
    "(0..1) when you can point to the exact spot."
)

MAX_CHECKS = 8


def _consistency_user_content(caption: str | None, mentions) -> str:
    lines = [f"Caption: {caption or 'not available'}"]
    numbered = [
        f"[{index}] (p{item.page_number}) {' '.join(item.sentence.split())}"
        for index, item in enumerate(mentions[:MAX_CHECKS], start=1)
    ]
    if numbered:
        lines.append("Prose mentions to check against the image (one per line):\n"
                     + "\n".join(numbered))
    return "\n".join(lines)


def consensus_status(statuses: list[str]) -> str | None:
    """Map per-mention verdicts to one evidence review status.

    inconsistent anywhere -> doubted (contradiction needs human eyes)
    all consistent       -> confirmed
    otherwise (only unverifiable / mixed without contradiction) -> None (leave as is)
    """
    if not statuses:
        return None
    if "inconsistent" in statuses:
        return "doubted"
    if all(status == "consistent" for status in statuses):
        return "confirmed"
    return None


class CrossModalConsistencyChecker:
    """Scan prose-vs-image consistency for one evidence node."""

    def __init__(self, provider, documents, research, skills: SkillRegistry | None = None,
                 traces=None) -> None:
        self.provider = provider
        self.documents = documents
        self.research = research
        self.skills = skills
        self.traces = traces
        self.system = self._load_system()

    def _load_system(self) -> str:
        if self.skills is None:
            return FALLBACK_SYSTEM
        try:
            return self.skills.load_skill("cross-modal-consistency").content
        except Exception:
            logger.warning("cross-modal-consistency skill unavailable; using fallback",
                           exc_info=True)
            return FALLBACK_SYSTEM

    async def check_evidence(
        self,
        project_id: str,
        parsed: ParsedDocument,
        node: EvidenceNode,
        trace_id: str | None = None,
    ) -> dict[str, int]:
        """Run the consistency scan for one visual evidence.

        Returns counts; never raises for model/IO failures. Does not write
        anything when the evidence already has a review or the visual has no
        prose mentions.
        """
        summary = {"consistent": 0, "inconsistent": 0, "unverifiable": 0, "checked": 0}
        path, caption, mentions = resolve_visual_source(
            self.documents, project_id, parsed, node
        )
        if not mentions:
            return summary
        already = await self.research.reviewed_evidence_ids(project_id, [node.evidence_id])
        if node.evidence_id in already:
            return summary

        started = perf_counter()
        try:
            report = await self.provider.structured_output_with_images(
                [
                    {"role": "system", "content": self.system},
                    {"role": "user", "content": _consistency_user_content(caption, mentions)},
                ],
                [path.read_bytes()],
                CrossModalConsistencyReport,
            )
        except Exception as exc:  # noqa: BLE001 - scan is best effort
            logger.warning("cross-modal consistency failed for %s: %s",
                           node.evidence_id, exc)
            return summary

        statuses = [check.status for check in report.checks]
        for status in statuses:
            if status in summary:
                summary[status] += 1
        summary["checked"] = len(statuses)
        decision = consensus_status(statuses)
        if decision is None:
            return summary
        try:
            note = self._note_for(decision, report)
            await self.research.review_evidence(
                project_id, node.evidence_id, decision, note=note,
                source="auto_consistency",
            )
        except Exception as exc:  # noqa: BLE001 - keep the scan non-fatal
            logger.warning("consistency verdict could not be stored for %s: %s",
                           node.evidence_id, exc)
        if self.traces is not None:
            try:
                await self.traces.append(
                    project_id,
                    trace_id or f"consistency:{project_id}",
                    "cross_modal_consistency",
                    success=True,
                    agent="consistency_checker",
                    latency_ms=round((perf_counter() - started) * 1_000),
                    summary={
                        "evidence_id": node.evidence_id,
                        "decision": decision,
                        **{key: summary[key] for key in
                           ("consistent", "inconsistent", "unverifiable")},
                    },
                )
            except Exception:  # telemetry must never break the scan
                logger.debug("consistency trace append failed", exc_info=True)
        return summary

    @staticmethod
    def _note_for(decision: str, report: CrossModalConsistencyReport) -> str:
        prefix = "图文一致：确认" if decision == "confirmed" else "图文一致：存疑"
        hints: list[str] = []
        for check in report.checks:
            if decision == "confirmed" and check.status == "consistent":
                hints.append(check.note or check.visible_evidence)
                break
            if decision == "doubted" and check.status == "inconsistent":
                hints.append(
                    f"[{check.mention_index}] {check.note or check.visible_evidence}"
                )
                if len(hints) >= 2:
                    break
        suffix = ("；".join(hints))[:360] if hints else ""
        return f"{prefix}：{suffix}" if suffix else prefix
