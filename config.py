from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DatabaseConfig:
    """数据库配置（文件配置优先，可按需再用环境变量覆盖）。"""

    host: str = "127.0.0.1"
    port: int = 5432
    name: str = "jchatmind"
    user: str = "admin"
    password: str = "123456"


DB_CONFIG = DatabaseConfig()


@dataclass(frozen=True)
class AgentLLMConfig:
    """每个 Agent 独立 LLM 配置占位。"""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = field(
        default_factory=lambda: os.getenv(
            "DATAANALYZE_EXECUTOR_LLM_API_KEY",
            os.getenv("DATAANALYZE_LLM_API_KEY", ""),
        )
    )
    model: str = "deepseek-chat"
    timeout_sec: int = 60


@dataclass(frozen=True)
class ReviewerLLMConfig:
    """Reviewer 的 LLM 配置占位（当前默认不启用）。"""

    enabled: bool = True
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = field(
        default_factory=lambda: os.getenv(
            "DATAANALYZE_REVIEWER_LLM_API_KEY",
            os.getenv(
                "DATAANALYZE_EXECUTOR_LLM_API_KEY",
                os.getenv("DATAANALYZE_LLM_API_KEY", ""),
            ),
        )
    )
    model: str = "deepseek-chat"
    timeout_sec: int = 60


@dataclass(frozen=True)
class PerfettoLLMConfig:
    """LLM config reserved for the Perfetto double-agent chain.

    It is intentionally independent from executor/reviewer config so enabling
    Perfetto SQL generation later will not change the existing /chat behavior.
    """

    enabled: bool = True
    base_url: str = "https://api.deepseek.com/v1"      
    api_key: str = field(
        default_factory=lambda: os.getenv(
            "DATAANALYZE_EXECUTOR_LLM_API_KEY",
            os.getenv("DATAANALYZE_LLM_API_KEY", ""),
        )
    )
    model: str = "deepseek-chat"
        
    
    timeout_sec: int = 60


EXECUTOR_LLM_CONFIG = AgentLLMConfig()
REVIEWER_LLM_CONFIG = ReviewerLLMConfig()
PERFETTO_LLM_CONFIG = PerfettoLLMConfig()


@dataclass(frozen=True)
class KnowledgeEmbeddingConfig:
    """知识检索 embedding 配置。"""

    # 当前默认启用 Reviewer 的 LLM 复核。
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    model: str = "bge-m3"
    timeout_sec: int = 8
    top_k: int = 6


KNOWLEDGE_EMBEDDING_CONFIG = KnowledgeEmbeddingConfig()


@dataclass(frozen=True)
class SchemaMetadataCacheConfig:
    """Schema 元数据缓存配置。"""

    enabled: bool = True
    ttl_sec: int = 120


SCHEMA_METADATA_CACHE_CONFIG = SchemaMetadataCacheConfig()
