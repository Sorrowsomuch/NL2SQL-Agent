from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Dict, Iterable, List, Optional
from urllib import error, request

from DataAnalyze.config import KNOWLEDGE_EMBEDDING_CONFIG


@dataclass
class KnowledgeDocument:
    """知识条目的轻量运行时表示。"""

    doc_id: str
    kind: str
    title: str
    path: str
    raw_text: str
    related_tables: List[str] = field(default_factory=list)
    table_name: str = ""
    column_name: str = ""
    search_text: str = ""
    summary: str = ""


@dataclass
class KnowledgeHit:
    """单条知识命中结果。"""

    doc_id: str
    kind: str
    title: str
    score: float
    related_tables: List[str] = field(default_factory=list)
    table_name: str = ""
    column_name: str = ""
    summary: str = ""


@dataclass
class KnowledgeRetrievalResult:
    """知识检索结果。"""

    strategy: str
    hits: List[KnowledgeHit] = field(default_factory=list)
    table_scores: Dict[str, float] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def hit_ids(self) -> List[str]:
        return [item.doc_id for item in self.hits]

    @property
    def hit_titles(self) -> List[str]:
        return [item.title for item in self.hits]


class OllamaEmbeddingClient:
    """面向本地 Ollama 的 embedding 客户端。"""

    def __init__(self) -> None:
        self.enabled = (
            os.getenv("DATAANALYZE_KNOWLEDGE_EMBEDDING_ENABLED", str(KNOWLEDGE_EMBEDDING_CONFIG.enabled))
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        self.base_url = os.getenv(
            "DATAANALYZE_KNOWLEDGE_EMBEDDING_BASE_URL",
            KNOWLEDGE_EMBEDDING_CONFIG.base_url,
        ).strip()
        self.model = os.getenv(
            "DATAANALYZE_KNOWLEDGE_EMBEDDING_MODEL",
            KNOWLEDGE_EMBEDDING_CONFIG.model,
        ).strip()
        self.timeout_sec = int(
            os.getenv(
                "DATAANALYZE_KNOWLEDGE_EMBEDDING_TIMEOUT_SEC",
                str(KNOWLEDGE_EMBEDDING_CONFIG.timeout_sec),
            )
        )
        self._disabled_until = 0.0

    def is_enabled(self) -> bool:
        return bool(self.enabled and self.base_url and self.model)

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts or not self.is_enabled():
            return []
        if monotonic() < self._disabled_until:
            return []

        payload = {
            "model": self.model,
            "input": texts,
            "truncate": True,
        }
        url = self.base_url.rstrip("/") + "/api/embed"
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = resp.read().decode("utf-8")
            data = json.loads(body)
            embeddings = data.get("embeddings")
            if not isinstance(embeddings, list):
                return []
            normalized: List[List[float]] = []
            for item in embeddings:
                if not isinstance(item, list):
                    continue
                try:
                    normalized.append([float(value) for value in item])
                except Exception:
                    continue
            return normalized
        except error.HTTPError:
            self._disabled_until = monotonic() + 60.0
            return []
        except Exception:
            self._disabled_until = monotonic() + 30.0
            return []


class KnowledgeBaseLoader:
    """从知识库目录加载文档条目。"""

    def __init__(self, knowledge_root: Optional[str] = None) -> None:
        # This module now lives under tools/db/, while the curated knowledge base
        # remains at DataAnalyze/knowledge. Keep the default anchored to the
        # package root so future tool-folder moves do not silently disable RAG.
        root = knowledge_root or str(Path(__file__).resolve().parents[2] / "knowledge")
        self.knowledge_root = Path(root)
        self._cache_signature: Optional[List[tuple[str, int]]] = None
        self._cache_documents: List[KnowledgeDocument] = []

    def load_documents(self) -> List[KnowledgeDocument]:
        if not self.knowledge_root.exists():
            return []

        yaml_files = sorted(
            path
            for path in self.knowledge_root.rglob("*.yaml")
            if path.is_file() and path.name != "manifest.yaml"
        )
        signature = [(str(path), path.stat().st_mtime_ns) for path in yaml_files]
        if signature == self._cache_signature:
            return list(self._cache_documents)

        documents = [self._build_document(path) for path in yaml_files]
        self._cache_signature = signature
        self._cache_documents = documents
        return list(documents)

    def _build_document(self, path: Path) -> KnowledgeDocument:
        raw_text = path.read_text(encoding="utf-8")
        doc_id = self._extract_scalar(raw_text, "id") or self._build_doc_id_from_path(path)
        kind = self._extract_scalar(raw_text, "kind") or path.parent.name
        title = self._extract_scalar(raw_text, "title") or path.stem
        summary = self._extract_scalar(raw_text, "summary") or title
        table_name = self._extract_scalar(raw_text, "table")
        column_name = self._extract_scalar(raw_text, "column")

        related_tables: List[str] = []
        related_tables.extend(self._extract_related_tables(raw_text))
        related_tables = self._deduplicate(related_tables)

        search_text = "\n".join(
            item
            for item in [
                doc_id,
                kind,
                title,
                summary,
                raw_text,
            ]
            if item.strip()
        )

        return KnowledgeDocument(
            doc_id=doc_id,
            kind=kind,
            title=title,
            path=str(path),
            raw_text=raw_text,
            related_tables=related_tables,
            table_name=table_name,
            column_name=column_name,
            search_text=search_text,
            summary=summary,
        )

    def _extract_related_tables(self, raw_text: str) -> List[str]:
        tables: List[str] = []
        for key in ["table", "from_table", "to_table"]:
            value = self._extract_scalar(raw_text, key)
            if value:
                tables.append(value)
        tables.extend(self._extract_list(raw_text, "recommended_tables"))
        return [item for item in tables if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", item)]

    def _extract_scalar(self, raw_text: str, key: str) -> str:
        pattern = re.compile(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$")
        match = pattern.search(raw_text)
        if not match:
            return ""
        value = match.group(1).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        return value

    def _extract_list(self, raw_text: str, key: str) -> List[str]:
        lines = raw_text.splitlines()
        items: List[str] = []
        capture = False
        base_indent = 0
        for line in lines:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" "))
            if not capture:
                if re.match(rf"^{re.escape(key)}:\s*$", stripped):
                    capture = True
                    base_indent = indent
                continue

            if stripped and indent <= base_indent and not stripped.startswith("- "):
                break
            if stripped.startswith("- "):
                item = stripped[2:].strip()
                if item.startswith('"') and item.endswith('"'):
                    item = item[1:-1]
                items.append(item)
        return items

    def _build_doc_id_from_path(self, path: Path) -> str:
        relative = path.relative_to(self.knowledge_root)
        return ".".join(relative.with_suffix("").parts)

    def _deduplicate(self, values: Iterable[str]) -> List[str]:
        ordered: List[str] = []
        seen = set()
        for value in values:
            if value in seen:
                continue
            ordered.append(value)
            seen.add(value)
        return ordered


class KnowledgeRetriever:
    """知识库本地检索器，支持词法与 embedding 混合召回。"""

    def __init__(
        self,
        loader: Optional[KnowledgeBaseLoader] = None,
        embedding_client: Optional[OllamaEmbeddingClient] = None,
    ) -> None:
        self.loader = loader or KnowledgeBaseLoader()
        self.embedding_client = embedding_client or OllamaEmbeddingClient()
        self.top_k = int(
            os.getenv("DATAANALYZE_KNOWLEDGE_RETRIEVAL_TOP_K", str(KNOWLEDGE_EMBEDDING_CONFIG.top_k))
        )
        self._embedding_cache: Dict[str, List[float]] = {}

    def retrieve(
        self,
        query: str,
        allowed_tables: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
    ) -> KnowledgeRetrievalResult:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return KnowledgeRetrievalResult(strategy="disabled", notes=["Empty query."])

        documents = self.loader.load_documents()
        if not documents:
            return KnowledgeRetrievalResult(
                strategy="disabled",
                notes=["Knowledge pack is empty or unavailable."],
            )

        allowed = set(allowed_tables or [])
        allowed_kinds = set(kinds or [])
        filtered_docs = [
            doc
            for doc in documents
            if (
                (not allowed or not doc.related_tables or any(table in allowed for table in doc.related_tables))
                and (not allowed_kinds or doc.kind in allowed_kinds)
            )
        ]
        if not filtered_docs:
            filtered_docs = documents

        lexical_scores = self._build_lexical_scores(normalized_query, filtered_docs)
        vector_scores: Dict[str, float] = {}
        strategy = "lexical"

        if self.embedding_client.is_enabled():
            vector_scores = self._build_vector_scores(normalized_query, filtered_docs)
            if vector_scores:
                strategy = f"hybrid-{self.embedding_client.model}"
            else:
                strategy = "lexical-fallback"

        hits: List[KnowledgeHit] = []
        table_scores: Dict[str, float] = {}
        for doc in filtered_docs:
            lexical_score = lexical_scores.get(doc.doc_id, 0.0)
            vector_score = vector_scores.get(doc.doc_id, 0.0)
            base_signal = lexical_score + (max(vector_score, 0.0) if vector_scores else 0.0)
            if base_signal <= 0.0:
                continue
            score = self._merge_scores(lexical_score, vector_score, bool(vector_scores), doc.kind)
            if score <= 0.0:
                continue
            hit = KnowledgeHit(
                doc_id=doc.doc_id,
                kind=doc.kind,
                title=doc.title,
                score=score,
                related_tables=list(doc.related_tables),
                table_name=doc.table_name,
                column_name=doc.column_name,
                summary=doc.summary,
            )
            hits.append(hit)
            for table_name in doc.related_tables:
                table_scores[table_name] = table_scores.get(table_name, 0.0) + score

        hits.sort(key=lambda item: item.score, reverse=True)
        hits = hits[: self.top_k]

        notes = [
            f"Knowledge retrieval strategy={strategy}.",
            f"Knowledge hits={len(hits)}.",
        ]
        if allowed:
            notes.append(f"Knowledge retrieval scoped to {len(allowed)} allowed table(s).")
        if allowed_kinds:
            notes.append(f"Knowledge retrieval filtered to kinds={sorted(allowed_kinds)}.")

        return KnowledgeRetrievalResult(
            strategy=strategy,
            hits=hits,
            table_scores=table_scores,
            notes=notes,
        )

    def build_prompt_context(
        self,
        retrieval: KnowledgeRetrievalResult,
        selected_tables: List[str],
        max_hits: int = 4,
    ) -> str:
        if not retrieval.hits:
            return ""

        selected = set(selected_tables)
        lines = ["knowledge:"]
        kept = 0
        for hit in retrieval.hits:
            if hit.related_tables and not any(table in selected for table in hit.related_tables):
                continue
            table_text = ", ".join(hit.related_tables) if hit.related_tables else "general"
            if hit.kind == "column_semantics" and hit.table_name and hit.column_name:
                table_text = f"{hit.table_name}.{hit.column_name}"
            lines.append(
                f"- [{hit.kind}] {hit.title} | tables={table_text} | {hit.summary}"
            )
            kept += 1
            if kept >= max_hits:
                break
        return "\n".join(lines) if kept > 0 else ""

    def collect_column_hints(
        self,
        retrieval: KnowledgeRetrievalResult,
        selected_tables: List[str],
        max_hits: int = 4,
    ) -> List[KnowledgeHit]:
        selected = set(selected_tables)
        column_hits: List[KnowledgeHit] = []
        for hit in retrieval.hits:
            if hit.kind != "column_semantics":
                continue
            if not hit.table_name or not hit.column_name:
                continue
            if selected and hit.table_name not in selected:
                continue
            column_hits.append(hit)
            if len(column_hits) >= max_hits:
                break
        return column_hits

    def _build_lexical_scores(
        self,
        query: str,
        documents: List[KnowledgeDocument],
    ) -> Dict[str, float]:
        tokens = self._tokenize(query)
        scores: Dict[str, float] = {}
        for doc in documents:
            searchable = doc.search_text.lower()
            score = 0.0
            if doc.title.lower() in query.lower():
                score += 4.0
            for token in tokens:
                if len(token) <= 1:
                    continue
                if token in searchable:
                    score += 1.0
                if token in doc.title.lower():
                    score += 1.5
            scores[doc.doc_id] = score
        return scores

    def _build_vector_scores(
        self,
        query: str,
        documents: List[KnowledgeDocument],
    ) -> Dict[str, float]:
        query_vectors = self.embedding_client.embed_texts([query])
        if not query_vectors:
            return {}
        query_vector = query_vectors[0]

        missing_docs = [doc for doc in documents if doc.doc_id not in self._embedding_cache]
        if missing_docs:
            embedded = self.embedding_client.embed_texts([doc.search_text for doc in missing_docs])
            if len(embedded) == len(missing_docs):
                for doc, vector in zip(missing_docs, embedded):
                    self._embedding_cache[doc.doc_id] = vector

        scores: Dict[str, float] = {}
        for doc in documents:
            vector = self._embedding_cache.get(doc.doc_id)
            if not vector:
                continue
            scores[doc.doc_id] = self._cosine_similarity(query_vector, vector)
        return scores

    def _merge_scores(
        self,
        lexical_score: float,
        vector_score: float,
        has_vector: bool,
        kind: str,
    ) -> float:
        if has_vector:
            score = (lexical_score * 0.4) + (max(vector_score, 0.0) * 6.0)
        else:
            score = lexical_score

        if kind == "query_pattern":
            score += 0.6
        elif kind == "metric_definition":
            score += 0.4
        elif kind == "column_semantics":
            score += 0.2
        return score

    def _tokenize(self, text: str) -> List[str]:
        tokens: List[str] = []
        for chunk in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]+", text.lower()):
            tokens.append(chunk)
            if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                for size in (2, 3):
                    if len(chunk) < size:
                        continue
                    for index in range(0, len(chunk) - size + 1):
                        tokens.append(chunk[index : index + size])
        return tokens

    def _cosine_similarity(self, left: List[float], right: List[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return numerator / (left_norm * right_norm)
