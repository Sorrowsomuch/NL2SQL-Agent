from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from DataAnalyze.agents.executor import ExecutorAgent
from DataAnalyze.agents.reviewer import ReviewerAgent
from DataAnalyze.core.memory import MemoryManager
from DataAnalyze.schemas.models import AgentResponse, WorkflowEvent


class WorkflowState(str, Enum):
    INIT = "INIT"
    COMPRESS_CONTEXT = "COMPRESS_CONTEXT"
    EXECUTE = "EXECUTE"
    REVIEW = "REVIEW"
    RETRY_PREPARE = "RETRY_PREPARE"
    DONE = "DONE"
    FAILED = "FAILED"


class WorkflowEngine:
    """状态机调度引擎：压缩上下文、执行、审核、失败回写与重试。"""

    def __init__(
        self,
        executor: ExecutorAgent,
        reviewer: ReviewerAgent,
        memory_manager: MemoryManager,
        max_retries: int = 2,
    ) -> None:
        self.executor = executor
        self.reviewer = reviewer
        self.memory_manager = memory_manager
        self.max_retries = max_retries

    def process(
        self,
        session_id: str,
        query: str,
        max_retries: Optional[int] = None,
    ) -> AgentResponse:
        retry_limit = self.max_retries if max_retries is None else max_retries
        state = WorkflowState.INIT
        attempt = 0
        last_error: Optional[str] = None
        events: List[WorkflowEvent] = [WorkflowEvent(state=state.value, detail="初始化")]

        # 每次请求先写入当前用户消息；如果服务刚重启，这里会触发懒加载，
        # 把该 session 在数据库中的历史记忆读回进程内存。
        self.memory_manager.add_message(
            session_id=session_id,
            role="user",
            content=query,
            layer="L0_RAW",
            memory_type="raw",
        )

        while attempt <= retry_limit:
            state = WorkflowState.COMPRESS_CONTEXT
            events.append(WorkflowEvent(state=state.value, detail="压缩上下文并提取事实"))
            self.memory_manager.compress_context(session_id)
            self.memory_manager.extract_facts_if_needed(session_id)

            state = WorkflowState.EXECUTE
            events.append(
                WorkflowEvent(state=state.value, detail=f"执行器开始执行，attempt={attempt}")
            )
            result = self.executor.run(
                session_id=session_id,
                user_query=query,
                retry_count=attempt,
                last_error=last_error,
            )

            state = WorkflowState.REVIEW
            events.append(WorkflowEvent(state=state.value, detail="Reviewer 审核结果"))
            review = self.reviewer.run(result, query)
            merged_debug = self._merge_review_debug(
                review.debug,
                self.memory_manager.get_last_debug(session_id),
            )

            if review.approved:
                state = WorkflowState.DONE
                events.append(WorkflowEvent(state=state.value, detail="审核通过，流程结束"))
                result.state = state.value
                result.review_reason = review.reason
                result.review_debug = merged_debug
                result.workflow_events = events
                self.memory_manager.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=result.text_reply,
                    layer="L0_RAW",
                    memory_type="raw",
                )
                return result

            state = WorkflowState.RETRY_PREPARE
            events.append(WorkflowEvent(state=state.value, detail=f"审核未通过: {review.reason}"))
            last_error = review.reason
            self.memory_manager.add_message(
                session_id=session_id,
                role="system",
                content=f"审核失败原因: {review.reason}",
                layer="L2_FACT",
                memory_type="fact",
            )

            if not review.should_retry or attempt >= retry_limit:
                return AgentResponse(
                    success=False,
                    text_reply=f"执行失败：{review.reason}",
                    chart=None,
                    sql=result.sql,
                    rows=result.rows,
                    columns=result.columns,
                    tool_calls=result.tool_calls,
                    workflow_events=events,
                    review_reason=review.reason,
                    review_debug=merged_debug,
                    error_reason=review.reason,
                    state=WorkflowState.FAILED.value,
                    retry_count=attempt,
                )

            attempt += 1

        return AgentResponse(
            success=False,
            text_reply="执行失败：超过最大重试次数",
            chart=None,
            sql=None,
            rows=[],
            columns=[],
            tool_calls=[],
            workflow_events=events,
            error_reason="超过最大重试次数",
            state=WorkflowState.FAILED.value,
            retry_count=attempt,
        )

    @staticmethod
    def _merge_review_debug(
        review_debug: Optional[Dict[str, Any]],
        memory_debug: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        if review_debug:
            merged.update(review_debug)
        if memory_debug:
            merged.update(memory_debug)
        return merged
