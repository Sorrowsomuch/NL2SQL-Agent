from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseAgent(ABC):
    """Agent 抽象基类，统一 Agent 调用协议。"""

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> Any:
        """执行 Agent 逻辑。"""
