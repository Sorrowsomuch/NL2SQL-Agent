from __future__ import annotations

from DataAnalyze.schemas.models import PerfettoAgentResponse, PerfettoReviewResult


class PerfettoReviewer:
    """Lightweight rule reviewer for Perfetto agent responses.

    This mirrors the existing Reviewer idea without involving LLMs yet; it only
    checks invariants that should hold for every Perfetto analysis response.
    """

    _MS_METRIC_NAMES = ("duration_ms", "cpu_time_ms", "dur_ms", "threshold_ms")

    def run(self, response: PerfettoAgentResponse) -> PerfettoReviewResult:
        if not response.success:
            return PerfettoReviewResult(
                approved=False,
                reason=response.error_reason or "Perfetto analysis failed",
            )
        if response.source_type not in {"trace_processor", "database"}:
            return PerfettoReviewResult(approved=False, reason="Invalid source_type")
        if not response.sql.strip():
            return PerfettoReviewResult(approved=False, reason="Missing SQL")
        if not response.metrics:
            return PerfettoReviewResult(approved=False, reason="Missing metrics")
        if not response.evidence and not response.conclusion.strip():
            return PerfettoReviewResult(
                approved=False,
                reason="Missing both evidence and conclusion",
            )
        unit_error = self._validate_metric_units(response)
        if unit_error:
            return PerfettoReviewResult(approved=False, reason=unit_error)
        if not response.evidence and self._conclusion_claims_issue(response.conclusion):
            return PerfettoReviewResult(
                approved=False,
                reason="Conclusion claims an issue without evidence",
            )
        return PerfettoReviewResult(approved=True, reason="Perfetto review passed")

    def _validate_metric_units(self, response: PerfettoAgentResponse) -> str:
        for metric in response.metrics:
            name = (metric.name or "").lower()
            if any(marker in name for marker in self._MS_METRIC_NAMES):
                if metric.unit != "ms":
                    return f"Metric {metric.name} must use ms unit"
        return ""

    @staticmethod
    def _conclusion_claims_issue(conclusion: str) -> bool:
        text = (conclusion or "").lower()
        claim_markers = ["found", "longer than", "concentrated", "hot spot", "issue"]
        no_issue_markers = ["no ", "does not show", "cannot be identified"]
        return any(marker in text for marker in claim_markers) and not any(
            marker in text for marker in no_issue_markers
        )
