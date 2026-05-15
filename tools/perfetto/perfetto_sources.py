from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol

from DataAnalyze.schemas.models import PerfettoAnalyzeResponse
from DataAnalyze.tools.perfetto.perfetto_tool import PerfettoTool


class PerfettoDataSource(Protocol):
    """Minimal source contract used by PerfettoAgent.

    The agent talks to this protocol so the backing source can move from direct
    Trace Processor queries to database-backed extracted datasets later.
    """

    source_type: str
    dataset_id: str

    def execute_sql(self, sql: str) -> Dict[str, Any]:
        ...

    def analyze_problem(
        self,
        problem: str,
        threshold_ms: float = 16.0,
        limit: int = 20,
    ) -> PerfettoAnalyzeResponse:
        ...


@dataclass
class TraceProcessorPerfettoSource:
    """Current source implementation: query one trace through Trace Processor."""

    tool: PerfettoTool
    dataset_id: str = "default-output-pb"
    source_type: str = "trace_processor"

    def execute_sql(self, sql: str) -> Dict[str, Any]:
        return self.tool.execute_sql(sql)

    def analyze_problem(
        self,
        problem: str,
        threshold_ms: float = 16.0,
        limit: int = 20,
    ) -> PerfettoAnalyzeResponse:
        return self.tool.analyze_problem(
            problem=problem,
            threshold_ms=threshold_ms,
            limit=limit,
        )


@dataclass
class DatabasePerfettoSource:
    """Future source implementation for extracted Perfetto datasets in PostgreSQL."""

    dataset_id: str
    source_type: str = "database"

    def execute_sql(self, sql: str) -> Dict[str, Any]:
        raise NotImplementedError("Database-backed Perfetto datasets are not implemented yet")

    def analyze_problem(
        self,
        problem: str,
        threshold_ms: float = 16.0,
        limit: int = 20,
    ) -> PerfettoAnalyzeResponse:
        raise NotImplementedError("Database-backed Perfetto datasets are not implemented yet")
