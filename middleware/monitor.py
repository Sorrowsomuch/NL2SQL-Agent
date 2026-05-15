from __future__ import annotations

from abc import ABC, abstractmethod
from functools import wraps
import time
from typing import Any, Callable, Dict


class BaseMonitor(ABC):
    """监控中间件抽象基类。"""

    @abstractmethod
    def monitor(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """返回装饰器，用于拦截目标函数执行过程。"""


class ConsoleMonitor(BaseMonitor):
    """控制台监控实现，记录耗时与关键上下文。"""

    def monitor(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                print(f"[Monitor] start={name} args={self._brief_args(args, kwargs)}")
                try:
                    result = func(*args, **kwargs)
                    duration = (time.perf_counter() - start) * 1000
                    print(f"[Monitor] end={name} status=success duration_ms={duration:.2f}")
                    return result
                except Exception as ex:  # pragma: no cover - 仅用于日志
                    duration = (time.perf_counter() - start) * 1000
                    print(
                        f"[Monitor] end={name} status=error duration_ms={duration:.2f} error={ex}"
                    )
                    raise

            return wrapper

        return decorator

    @staticmethod
    def _brief_args(args: Any, kwargs: Dict[str, Any]) -> str:
        if kwargs:
            return f"kwargs={list(kwargs.keys())}"
        return f"args_len={len(args)}"
