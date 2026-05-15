from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict
from urllib import error, request


@dataclass(frozen=True)
class LLMEndpointConfig:
    """OpenAI-compatible API 配置。"""

    base_url: str
    api_key: str
    model: str
    timeout_sec: int = 60


class LLMClient:
    """轻量 LLM 客户端，默认按 OpenAI Chat Completions 协议发送请求。"""

    def __init__(self, config: LLMEndpointConfig) -> None:
        self.config = config

    def is_enabled(self) -> bool:
        if not self.config.base_url.strip() or not self.config.api_key.strip():
            return False
        # 默认占位值视为未启用
        if "YOUR_" in self.config.api_key or "your_" in self.config.api_key:
            return False
        if "YOUR_" in self.config.base_url or "your_" in self.config.base_url:
            return False
        return True

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> Dict[str, Any]:
        if not self.is_enabled():
            raise RuntimeError("LLM 未启用：请在 DataAnalyze/config.py 填写 API 配置")

        base_payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        payload_with_format = dict(base_payload)
        payload_with_format["response_format"] = {"type": "json_object"}

        body = self._chat_body(base_payload=base_payload, prefer_json_object=True)

        try:
            data = json.loads(body)
            content = self._extract_choice_content(data)
            return self._parse_json_text(content)
        except Exception as ex:
            raise RuntimeError(f"LLM 返回解析失败: {ex}; body={body[:500]}") from ex

    def chat_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> str:
        if not self.is_enabled():
            raise RuntimeError("LLM 未启用：请在 DataAnalyze/config.py 填写 API 配置")

        base_payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body = self._chat_body(base_payload=base_payload, prefer_json_object=False)
        try:
            data = json.loads(body)
            return self._extract_choice_content(data)
        except Exception as ex:
            raise RuntimeError(f"LLM 文本返回解析失败: {ex}; body={body[:500]}") from ex

    def _extract_choice_content(self, data: Dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("响应缺少 choices")

        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            raise RuntimeError("响应缺少 message")

        content = message.get("content")
        reasoning = message.get("reasoning_content")

        def _norm(val: Any) -> str:
            if val is None:
                return ""
            if isinstance(val, list):
                return "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in val)
            return str(val)

        content_text = _norm(content).strip()
        reasoning_text = _norm(reasoning).strip()
        if content_text:
            return content_text
        if reasoning_text:
            return reasoning_text

        finish_reason = str(first.get("finish_reason", "")).strip()
        raise RuntimeError(f"模型返回为空内容，finish_reason={finish_reason or 'unknown'}")

    def _chat_body(self, base_payload: Dict[str, Any], prefer_json_object: bool) -> str:
        payload = dict(base_payload)
        if prefer_json_object:
            payload["response_format"] = {"type": "json_object"}

        try:
            return self._post_chat(payload)
        except RuntimeError as ex:
            msg = str(ex).lower()
            if prefer_json_object and any(k in msg for k in ["response_format", "invalid", "400"]):
                return self._post_chat(base_payload)
            raise

    def _post_chat(self, payload: Dict[str, Any]) -> str:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_sec) as resp:
                return resp.read().decode("utf-8")
        except error.HTTPError as ex:
            detail = ex.read().decode("utf-8", errors="ignore") if hasattr(ex, "read") else str(ex)
            raise RuntimeError(f"LLM HTTP 错误: {ex.code} {detail}") from ex
        except Exception as ex:
            raise RuntimeError(f"LLM 请求失败: {ex}") from ex

    def _parse_json_text(self, text: str) -> Dict[str, Any]:
        text = text.strip()
        try:
            return json.loads(text)
        except Exception:
            pass

        # 兼容 ```json ... ``` 包裹格式。
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text, flags=re.IGNORECASE)
        if match:
            return json.loads(match.group(1))

        # 兼容前后有说明文字，仅提取首个 JSON 对象。
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            return json.loads(text[start : end + 1])

        raise RuntimeError("模型返回中未发现可解析 JSON")
