"""Automatic visual review pass (cross-modal grounding, step B).

After a paper analysis cites figure/table evidence as *supported* for a claim,
this pass re-reads the actual crop image together with that claim (plus caption
and prose mentions) and asks the vision model to confirm, doubt or exclude the
evidence. Verdicts are written into evidence_reviews and later overridable by the
human, so the pass automates the otherwise-manual "证据复核" for visual claims.

The *methodology* (how to audit without self-confirmation bias, when a region is
required, what counts as confirmed) lives in the ``visual-verification`` skill;
this module only orchestrates the two structured phases and the storage.

Design notes:
- Anti-bias protocol: phase A reads the image blind (no claim) and returns
  visible facts; phase B anchors the verdict to those facts and to the claim.
- Best effort by design: any per-item failure leaves the evidence unreviewed
  (for the human) instead of failing the whole analysis job.
- Only evidence with no existing review is touched; human verdicts are never
  overwritten (see ResearchDataRepository.reviewed_evidence_ids).
"""

import logging
from time import perf_counter

from app.schemas import (
    BlindVisualFacts,
    EvidenceNode,
    ParsedDocument,
    VisualVerificationVerdict,
)
from app.skills import SkillRegistry

logger = logging.getLogger(__name__)

# Fallbacks used only when no SkillRegistry is available (unit tests, legacy
# call sites). Production behavior is driven by the visual-verification skill.
FALLBACK_BLIND_SYSTEM = (
    "You are auditing an academic figure/table. List only what is directly visible "
    "in the image (axes, values, structure, legend, cells). Put anything uncertain "
    "in unknowns. Do not guess."
)
FALLBACK_VERDICT_SYSTEM = (
    "Given an analysis claim and the visible facts from the image, decide whether the "
    "image DIRECTLY supports the claim. confirmed: visible facts directly support it; "
    "doubted: insufficient or ambiguous; excluded: visible facts contradict it. "
    "Prefer seeking counter-evidence. If you can point to the exact region, return a "
    "normalized bbox (0..1); otherwise leave regions empty and prefer doubted."
)


def _blind_user_content(caption: str | None, mentions) -> str:
    lines = [f"Caption: {caption or 'not available'}"]
    context = [f"(p{item.page_number}) {' '.join(item.sentence.split())}"
               for item in (mentions or [])[:4]]
    if context:
        lines.append("Prose mentions (context only, not visual facts):\n- "
                     + "\n- ".join(context))
    return "\n".join(lines)


def _verdict_user_content(claim_value: str, caption: str | None, mentions,
                          facts: BlindVisualFacts) -> str:
    lines = [f"Analysis claim to verify: {claim_value}",
             f"Caption: {caption or 'not available'}"]
    context = [f"(p{item.page_number}) {' '.join(item.sentence.split())}"
               for item in (mentions or [])[:4]]
    if context:
        lines.append("Prose mentions (context only):\n- " + "\n- ".join(context))
    facts_block = "\n".join(f"- {item}" for item in facts.visible_facts) or "- (none)"
    unknowns_block = "\n".join(f"- {item}" for item in facts.unknowns) or "- (none)"
    lines.append("Visible facts read before seeing the claim:\n" + facts_block)
    lines.append("Unknowns recorded in the blind pass:\n" + unknowns_block)
    return "\n".join(lines)


def _review_targets(
    claims: list[tuple[str, list[str]]],
    evidence_by_id: dict[str, EvidenceNode],
) -> dict[str, str]:
    """Map each cited visual evidence id to the first claim that cites it."""
    targets: dict[str, str] = {}
    for claim_value, evidence_ids in claims:
        for evidence_id in evidence_ids:
            node = evidence_by_id.get(evidence_id)
            if node is None:
                continue
            targets.setdefault(evidence_id, claim_value)
    return targets


def resolve_visual_source(documents, project_id: str, parsed: ParsedDocument,
                          node: EvidenceNode):
    """Locate the crop image + caption + prose mentions for an evidence node.

    Shared by the visual-verification and cross-modal-consistency passes so both
    resolve figure/table crops with identical semantics.
    """
    caption, mentions = node.excerpt, []
    relative = node.source_path
    if node.evidence_type == "figure":
        for figure in parsed.figures:
            if figure.source_path == node.source_path:
                caption = figure.caption or caption
                mentions = figure.mentions
                break
    elif node.evidence_type == "table":
        for table in parsed.tables:
            if (table.page_number == node.page_number and table.label == node.label
                    and table.source_path):
                relative = table.source_path  # prefer the cropped table image
                caption = table.caption or caption
                mentions = table.mentions
                break
    path = documents.workspace.resolve_safe_path(project_id, relative)
    return path, caption, mentions


class VisualEvidenceVerifier:
    """Verify cited figure/table evidence against its crop image via the VLM."""

    def __init__(self, provider, documents, research, skills: SkillRegistry | None = None,
                 traces=None) -> None:
        self.provider = provider
        self.documents = documents
        self.research = research
        self.skills = skills
        self.traces = traces
        self.blind_system, self.verdict_system = self._load_systems()

    def _load_systems(self) -> tuple[str, str]:
        if self.skills is None:
            return FALLBACK_BLIND_SYSTEM, FALLBACK_VERDICT_SYSTEM
        try:
            content = self.skills.load_skill("visual-verification").content
        except Exception:
            logger.warning("visual-verification skill unavailable; using fallback",
                           exc_info=True)
            return FALLBACK_BLIND_SYSTEM, FALLBACK_VERDICT_SYSTEM
        return content, content

    async def verify_cited_visual_evidence(
        self,
        project_id: str,
        parsed: ParsedDocument,
        claims: list[tuple[str, list[str]]],
        evidence_by_id: dict[str, EvidenceNode],
        trace_id: str | None = None,
    ) -> dict[str, int]:
        """Review every cited visual evidence that has no review yet.

        Returns a {status: count} summary; never raises for model/IO failures.
        """
        summary = {"confirmed": 0, "doubted": 0, "excluded": 0, "failed": 0}
        targets = _review_targets(claims, evidence_by_id)
        if not targets:
            return summary
        already = await self.research.reviewed_evidence_ids(
            project_id, list(targets)
        )
        for evidence_id, claim_value in targets.items():
            if evidence_id in already:
                continue
            started = perf_counter()
            try:
                verdict = await self._verify_one(
                    project_id, parsed, evidence_by_id[evidence_id], claim_value
                )
            except Exception as exc:  # noqa: BLE001 - review is best effort
                summary["failed"] += 1
                logger.warning("visual review failed for %s: %s", evidence_id, exc)
                continue
            try:
                await self.research.review_evidence(
                    project_id,
                    evidence_id,
                    verdict.status,
                    note=f"自动复核：{verdict.reason}",
                    source="auto_visual_verifier",
                )
            except Exception as exc:  # noqa: BLE001 - keep the pass non-fatal
                summary["failed"] += 1
                logger.warning("visual review could not be stored for %s: %s",
                               evidence_id, exc)
                continue
            summary[verdict.status] += 1
            if self.traces is not None:
                await self._append_trace(
                    project_id, trace_id, evidence_id, verdict,
                    round((perf_counter() - started) * 1_000),
                )
        return summary

    async def _verify_one(
        self,
        project_id: str,
        parsed: ParsedDocument,
        node: EvidenceNode,
        claim_value: str,
    ) -> VisualVerificationVerdict:
        image_path, caption, mentions = resolve_visual_source(
            self.documents, project_id, parsed, node
        )
        image = image_path.read_bytes()
        blind_messages = [
            {"role": "system", "content": self.blind_system},
            {"role": "user", "content": _blind_user_content(caption, mentions)},
        ]
        facts = await self.provider.structured_output_with_images(
            blind_messages, [image], BlindVisualFacts
        )
        verdict_messages = [
            {"role": "system", "content": self.verdict_system},
            {"role": "user", "content": _verdict_user_content(
                claim_value, caption, mentions, facts
            )},
        ]
        return await self.provider.structured_output_with_images(
            verdict_messages, [image], VisualVerificationVerdict
        )

    async def _append_trace(self, project_id, trace_id, evidence_id, verdict,
                            latency_ms: int) -> None:
        try:
            await self.traces.append(
                project_id,
                trace_id or f"visual-verify:{project_id}",
                "visual_verify",
                success=True,
                agent="visual_verifier",
                latency_ms=latency_ms,
                summary={
                    "evidence_id": evidence_id,
                    "status": verdict.status,
                    "confidence": verdict.confidence,
                    "regions": len(verdict.regions),
                },
            )
        except Exception:  # telemetry must never break the pass
            logger.debug("visual review trace append failed", exc_info=True)
