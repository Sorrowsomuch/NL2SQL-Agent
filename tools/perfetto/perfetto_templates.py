from __future__ import annotations

from typing import Any, Dict, List

from DataAnalyze.schemas.models import PerfettoMetric


def build_plan_from_problem(
    problem: str,
    threshold_ms: float = 16.0,
    limit: int = 20,
    analysis_mode: str = "auto",
    dataset_id: str = "default-output-pb",
    source_type: str = "trace_processor",
) -> Dict[str, Any]:
    """Build a deterministic Perfetto analysis plan from a user problem.

    Keep LLMs out of SQL construction for v1: the agent selects a template-shaped
    plan, then the system renders SQL from known-safe pieces.
    """

    safe_limit = max(1, min(int(limit), 200))
    safe_threshold_ms = max(float(threshold_ms), 0.001)
    analysis_type = _select_analysis_type(problem)
    primary_metric = "cpu_time_ms" if analysis_type == "cpu_time" else "duration_ms"
    return {
        "analysis_mode": analysis_mode or "auto",
        "analysis_type": analysis_type,
        "primary_metric": primary_metric,
        "threshold_ms": safe_threshold_ms,
        "limit": safe_limit,
        "source_type": source_type,
        "dataset_id": dataset_id or "default-output-pb",
    }


def build_sql_from_plan(plan: Dict[str, Any]) -> str:
    """Render SQL from a validated template plan."""
    analysis_type = str(plan.get("analysis_type") or "long_slice")
    limit = max(1, min(int(plan.get("limit") or 20), 200))
    if analysis_type == "cpu_time":
        return _build_cpu_time_sql(limit)
    return _build_long_slice_sql(
        threshold_ms=float(plan.get("threshold_ms") or 16.0),
        limit=limit,
    )


def summarize_result(
    plan: Dict[str, Any],
    rows: List[Dict[str, Any]],
) -> tuple[List[PerfettoMetric], List[str], str, List[str]]:
    """Convert raw rows into the stable metrics/evidence/conclusion contract."""
    analysis_type = str(plan.get("analysis_type") or "long_slice")
    if analysis_type == "cpu_time":
        return _summarize_cpu_time(rows)
    return _summarize_long_slices(
        rows=rows,
        threshold_ms=float(plan.get("threshold_ms") or 16.0),
    )


def _select_analysis_type(problem: str) -> str:
    normalized = (problem or "").lower()
    if any(token in normalized for token in ["cpu", "sched", "调度", "占用"]):
        return "cpu_time"
    return "long_slice"


def _build_long_slice_sql(threshold_ms: float, limit: int) -> str:
    threshold_ns = int(max(threshold_ms, 0.001) * 1_000_000)
    return (
        "SELECT "
        "process.name AS process_name, "
        "thread.name AS thread_name, "
        "slice.name AS slice_name, "
        "slice.ts / 1e6 AS ts_ms, "
        "slice.dur / 1e6 AS dur_ms "
        "FROM slice "
        "JOIN thread_track ON slice.track_id = thread_track.id "
        "JOIN thread USING (utid) "
        "LEFT JOIN process USING (upid) "
        f"WHERE slice.dur > {threshold_ns} "
        "ORDER BY slice.dur DESC "
        f"LIMIT {int(limit)}"
    )


def _build_cpu_time_sql(limit: int) -> str:
    return (
        "SELECT "
        "process.name AS process_name, "
        "thread.name AS thread_name, "
        "SUM(sched.dur) / 1e6 AS cpu_time_ms "
        "FROM sched "
        "JOIN thread USING (utid) "
        "LEFT JOIN process USING (upid) "
        "GROUP BY process.name, thread.name "
        "ORDER BY cpu_time_ms DESC "
        f"LIMIT {int(limit)}"
    )


def _summarize_long_slices(
    rows: List[Dict[str, Any]],
    threshold_ms: float,
) -> tuple[List[PerfettoMetric], List[str], str, List[str]]:
    durations = [float(row.get("dur_ms") or 0.0) for row in rows]
    max_duration = max(durations) if durations else 0.0
    metrics = [
        PerfettoMetric(
            name="long_slice_count",
            value=len(rows),
            unit="count",
            interpretation=f"Number of slices longer than {threshold_ms:g} ms in the inspected top window.",
        ),
        PerfettoMetric(
            name="max_duration_ms",
            value=round(max_duration, 3),
            unit="ms",
            interpretation="Longest slice duration returned by the query.",
        ),
        PerfettoMetric(
            name="threshold_ms",
            value=threshold_ms,
            unit="ms",
            interpretation="Current long-task threshold.",
        ),
    ]
    evidence = [
        (
            f"{row.get('process_name') or 'unknown_process'} / "
            f"{row.get('thread_name') or 'unknown_thread'} / "
            f"{row.get('slice_name') or 'unknown_slice'}: "
            f"{float(row.get('dur_ms') or 0.0):.3f} ms"
        )
        for row in rows[:5]
    ]

    if not rows:
        conclusion = (
            f"No slice longer than {threshold_ms:g} ms was found in this trace query. "
            "The current evidence does not show an obvious long synchronous task."
        )
    else:
        top = rows[0]
        conclusion = (
            f"Found {len(rows)} slices longer than {threshold_ms:g} ms. "
            f"The longest one is {float(top.get('dur_ms') or 0.0):.3f} ms on "
            f"{top.get('process_name') or 'unknown_process'} / "
            f"{top.get('thread_name') or 'unknown_thread'}, slice="
            f"{top.get('slice_name') or 'unknown_slice'}."
        )

    recommendations = [
        "Filter the result by target process and main/UI thread to confirm user-visible impact.",
        "Inspect neighboring slices around the top timestamp to identify the blocking phase.",
        "If frame timeline tables exist, correlate long slices with frame/jank events.",
    ]
    return metrics, evidence, conclusion, recommendations


def _summarize_cpu_time(
    rows: List[Dict[str, Any]],
) -> tuple[List[PerfettoMetric], List[str], str, List[str]]:
    top_cpu = float(rows[0].get("cpu_time_ms") or 0.0) if rows else 0.0
    total_top_cpu = sum(float(row.get("cpu_time_ms") or 0.0) for row in rows)
    metrics = [
        PerfettoMetric(
            name="top_thread_cpu_time_ms",
            value=round(top_cpu, 3),
            unit="ms",
            interpretation="CPU scheduled time of the highest ranked thread in the returned rows.",
        ),
        PerfettoMetric(
            name="returned_threads",
            value=len(rows),
            unit="count",
            interpretation="Number of process/thread groups returned by the CPU query.",
        ),
        PerfettoMetric(
            name="top_rows_cpu_time_ms",
            value=round(total_top_cpu, 3),
            unit="ms",
            interpretation="Sum of CPU scheduled time across returned top rows.",
        ),
    ]
    evidence = [
        (
            f"{row.get('process_name') or 'unknown_process'} / "
            f"{row.get('thread_name') or 'unknown_thread'}: "
            f"{float(row.get('cpu_time_ms') or 0.0):.3f} ms CPU"
        )
        for row in rows[:5]
    ]

    if not rows:
        conclusion = "No scheduler CPU rows were returned, so CPU hot spots cannot be identified from this query."
    else:
        top = rows[0]
        conclusion = (
            "CPU time is concentrated at the top returned thread: "
            f"{top.get('process_name') or 'unknown_process'} / "
            f"{top.get('thread_name') or 'unknown_thread'} with "
            f"{float(top.get('cpu_time_ms') or 0.0):.3f} ms scheduled CPU time."
        )

    recommendations = [
        "Compare top CPU threads with long slice results to separate busy CPU work from blocking waits.",
        "Filter by target process if the trace contains multiple apps or system services.",
        "Drill into sched slices near the peak window if the issue is time-localized.",
    ]
    return metrics, evidence, conclusion, recommendations
