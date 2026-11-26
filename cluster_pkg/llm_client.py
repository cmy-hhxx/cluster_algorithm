# -*- coding: utf-8 -*-
"""OpenAI-Responses / Chat-Completions 双协议客户端（纯标准库 urllib，零第三方依赖）。

支持：
  - 自动规范化端点 URL（兼容 `.../v1`、`.../v1/responses` 与 `.../v1/chat/completions`）
  - `protocol=auto` 时优先 `/responses`，遇 404/405/501 或不支持时自动降级 `/chat/completions`
  - 多端点轮询容灾 + 429/502/503/504 指数退避与 Retry-After 遵循
  - 目标模型不支持 `reasoning.effort` 或 `max_tokens` 时自动切换重试
  - 提取标准 Responses `output[].content[].output_text`、`output_text` 及 Chat `choices[].message.content`
"""
import itertools
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .cfg import ApiConfig


def normalize_responses_url(endpoint: str) -> str:
    u = endpoint.strip().rstrip("/")
    if u.endswith("/responses"):
        return u
    if u.endswith("/chat/completions"):
        return u[: -len("/chat/completions")] + "/responses"
    parsed = urllib.parse.urlparse(u)
    if parsed.path in ("", "/") and parsed.hostname == "api.openai.com":
        return u + "/v1/responses"
    return u + "/responses"


def normalize_chat_url(endpoint: str) -> str:
    u = endpoint.strip().rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/responses"):
        return u[: -len("/responses")] + "/chat/completions"
    parsed = urllib.parse.urlparse(u)
    if parsed.path in ("", "/") and parsed.hostname == "api.openai.com":
        return u + "/v1/chat/completions"
    return u + "/chat/completions"


def normalize_models_url(endpoint: str) -> str:
    u = endpoint.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    parsed = urllib.parse.urlparse(u)
    if parsed.path in ("", "/") and parsed.hostname == "api.openai.com":
        return u + "/v1/models"
    return u + "/models"


def extract_output_text(resp: dict) -> str:
    if not isinstance(resp, dict):
        return ""
    if isinstance(resp.get("output_text"), str) and resp["output_text"]:
        return resp["output_text"]
    chunks = []
    for item in resp.get("output") or []:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("output_text", "text") and isinstance(item.get("text"), str):
            chunks.append(item["text"])
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                chunks.append(c.get("text") or "")
    if chunks:
        return "".join(chunks)
    for ch in resp.get("choices") or []:
        msg = (ch or {}).get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    chunks.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "".join(chunks)


def parse_jsonl_rows(text: str):
    rows = []
    for line in (text or "").splitlines():
        s = line.strip().rstrip(",")
        if not s.startswith("{"):
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    return rows


def extract_json_object(text: str):
    """从模型文本中提取单个顶层 JSON 对象（容忍 ```json 代码块包裹）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if 0 <= start < end:
        obj = json.loads(s[start: end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("模型未返回合法的 JSON 对象")


class ResponsesClient:
    def __init__(self, cfg: ApiConfig, logger=None):
        self.cfg = cfg
        self.log = logger or (lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True))
        self.raw_endpoints = [u.strip().rstrip("/") for u in cfg.endpoints if u.strip()]
        explicit_chat = (
            cfg.protocol == "chat_completions"
            or (cfg.protocol == "auto" and self.raw_endpoints and all(
                u.endswith("/chat/completions") for u in self.raw_endpoints
            ))
        )
        self._protocol = "chat_completions" if explicit_chat else "responses"
        self._can_fallback_chat = (cfg.protocol == "auto" and self._protocol == "responses")
        self._use_max_completion_tokens = False
        self.endpoints = self._normalize_endpoints(self.raw_endpoints, self._protocol)
        self._rr = itertools.count()
        self._lock = threading.Lock()
        self._allow_reasoning = cfg.reasoning_effort not in ("", "omit", "default", "null", "None")
        self.calls = 0

    @staticmethod
    def _normalize_endpoints(raw_eps, protocol: str):
        fn = normalize_chat_url if protocol == "chat_completions" else normalize_responses_url
        return [fn(u) for u in raw_eps]

    def _build_opener(self):
        if self.cfg.proxy == "env":
            return urllib.request.build_opener()
        if self.cfg.proxy:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.cfg.proxy, "https": self.cfg.proxy})
            )
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def check_ready(self):
        """可选探活：仅当开启 probe_models 时检查 /models；404/405 视为端点仅开放推理接口，放行。"""
        if not self.raw_endpoints:
            raise RuntimeError("未配置任何 OpenAI 协议 API 端点")
        if not self.cfg.probe_models:
            return list(self.endpoints)
        healthy_raw = []
        opener = self._build_opener()
        for ep in self.raw_endpoints:
            murl = normalize_models_url(ep)
            for attempt in range(3):
                req = urllib.request.Request(
                    murl, headers={"Authorization": f"Bearer {self.cfg.api_key}", **self.cfg.extra_headers}
                )
                try:
                    with opener.open(req, timeout=8) as r:
                        if r.status == 200:
                            healthy_raw.append(ep)
                            break
                except urllib.error.HTTPError as e:
                    if e.code in (404, 405):
                        healthy_raw.append(ep)
                        break
                    if e.code == 401:
                        raise RuntimeError(f"API 鉴权失败 (401): {murl}，请检查 api.api_key") from e
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if not healthy_raw:
            raise RuntimeError("所有配置的 API 端点探活均失败: " + ", ".join(self.raw_endpoints))
        self.raw_endpoints = healthy_raw
        self.endpoints = self._normalize_endpoints(healthy_raw, self._protocol)
        return self.endpoints

    def _make_payload(self, prompt: str, include_reasoning: bool) -> bytes:
        body = {
            "model": self.cfg.model,
            "input": prompt,
            "max_output_tokens": self.cfg.max_output_tokens,
        }
        if include_reasoning and self.cfg.reasoning_effort not in ("", "omit", "default"):
            body["reasoning"] = {"effort": self.cfg.reasoning_effort}
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _make_chat_payload(self, prompt: str, use_max_completion_tokens: bool) -> bytes:
        body = {
            "model": self.cfg.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        tok_key = "max_completion_tokens" if use_max_completion_tokens else "max_tokens"
        body[tok_key] = self.cfg.max_output_tokens
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _build_request_for_endpoint(self, raw_ep: str, prompt: str):
        with self._lock:
            proto = self._protocol
            allow_reasoning = self._allow_reasoning
            use_mct = self._use_max_completion_tokens
        if proto == "chat_completions":
            return proto, normalize_chat_url(raw_ep), self._make_chat_payload(prompt, use_mct)
        return proto, normalize_responses_url(raw_ep), self._make_payload(prompt, allow_reasoning)

    @staticmethod
    def _should_fallback_to_chat(err_code: int, err_body: str) -> bool:
        if err_code in (404, 405, 501):
            return True
        if err_code == 400:
            low = (err_body or "").lower()
            if "reasoning" in low:
                return False
            return any(
                kw in low for kw in ("not found", "unrecognized", "unknown", "unsupported", "responses", "messages")
            )
        return False

    def _post_json(self, opener, url: str, payload: bytes, headers: dict) -> dict:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with opener.open(req, timeout=self.cfg.timeout) as resp:
            raw = resp.read()
        with self._lock:
            self.calls += 1
        return json.loads(raw)

    @staticmethod
    def _read_http_error(err: urllib.error.HTTPError):
        body = ""
        try:
            body = err.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        finally:
            try:
                err.close()
            except Exception:
                pass
        return err.code, body

    @staticmethod
    def _retry_after(err) -> float:
        try:
            hdrs = getattr(err, "headers", None)
            if hdrs and hdrs.get("Retry-After"):
                return float(hdrs.get("Retry-After"))
        except Exception:
            pass
        return 0.0

    def complete(self, prompt: str) -> dict:
        """发送一次 LLM 请求并返回解析后的响应字典（自动兼容 Responses 与 Chat Completions）。"""
        if not self.raw_endpoints:
            raise RuntimeError("没有可用的 OpenAI 协议端点")
        attempts = max(1, self.cfg.request_attempts)
        idx = next(self._rr) % len(self.raw_endpoints)
        order = self.raw_endpoints[idx:] + self.raw_endpoints[:idx]
        opener = self._build_opener()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
            **self.cfg.extra_headers,
        }
        last_err = None

        for attempt in range(attempts):
            throttled = False
            for raw_ep in order:
                proto, url, payload = self._build_request_for_endpoint(raw_ep, prompt)
                try:
                    return self._post_json(opener, url, payload, headers)
                except urllib.error.HTTPError as e:
                    wait_ra = self._retry_after(e)
                    err_code, err_body = self._read_http_error(e)
                    if err_code == 401:
                        raise RuntimeError(f"OpenAI API 鉴权失败 (HTTP 401): {err_body}") from None

                    low = err_body.lower()
                    # 1) Responses 模式下不支持 reasoning 参数：剥离后原地重试
                    if proto == "responses" and err_code == 400 and self._allow_reasoning and (
                        "reasoning" in low or "unsupported" in low or "unknown" in low or "extra_forbidden" in low
                    ):
                        with self._lock:
                            if self._allow_reasoning:
                                self._allow_reasoning = False
                                self.log("  提示：目标模型不支持 reasoning 参数，已自动切换为无 reasoning 参数重试")
                        try:
                            return self._post_json(opener, url, self._make_payload(prompt, False), headers)
                        except urllib.error.HTTPError as e2:
                            err_code, err_body = self._read_http_error(e2)
                            wait_ra = self._retry_after(e2) or wait_ra
                            if err_code == 401:
                                raise RuntimeError(f"OpenAI API 鉴权失败 (HTTP 401): {err_body}") from None
                        except Exception as e2:
                            last_err = e2
                            continue

                    # 2) protocol=auto 时 Responses 端点不可用/不支持：自动降级为 Chat Completions
                    if proto == "responses" and self._can_fallback_chat and self._should_fallback_to_chat(err_code, err_body):
                        with self._lock:
                            if self._protocol == "responses":
                                self._protocol = "chat_completions"
                                self._can_fallback_chat = False
                                self.endpoints = self._normalize_endpoints(self.raw_endpoints, self._protocol)
                                self.log("  提示：端点不支持 /responses，已自动降级切换至 /chat/completions 协议")
                            use_mct = self._use_max_completion_tokens
                        try:
                            return self._post_json(
                                opener, normalize_chat_url(raw_ep), self._make_chat_payload(prompt, use_mct), headers
                            )
                        except urllib.error.HTTPError as e2:
                            err_code, err_body = self._read_http_error(e2)
                            wait_ra = self._retry_after(e2) or wait_ra
                            if err_code == 401:
                                raise RuntimeError(f"OpenAI API 鉴权失败 (HTTP 401): {err_body}") from None
                            proto = "chat_completions"
                        except Exception as e2:
                            last_err = e2
                            continue

                    # 3) Chat Completions 模式下不支持 max_tokens：切换 max_completion_tokens 重试
                    if proto == "chat_completions" and err_code == 400 and not self._use_max_completion_tokens:
                        low2 = err_body.lower()
                        if "max_tokens" in low2 and ("max_completion_tokens" in low2 or "unsupported" in low2):
                            with self._lock:
                                if not self._use_max_completion_tokens:
                                    self._use_max_completion_tokens = True
                                    self.log("  提示：目标模型要求使用 max_completion_tokens，已自动切换重试")
                            try:
                                return self._post_json(
                                    opener, normalize_chat_url(raw_ep), self._make_chat_payload(prompt, True), headers
                                )
                            except urllib.error.HTTPError as e2:
                                err_code, err_body = self._read_http_error(e2)
                                wait_ra = self._retry_after(e2) or wait_ra
                                if err_code == 401:
                                    raise RuntimeError(f"OpenAI API 鉴权失败 (HTTP 401): {err_body}") from None
                            except Exception as e2:
                                last_err = e2
                                continue

                    if err_code in (429, 502, 503, 504):
                        wait = wait_ra or min(60.0, 2.0 * (2 ** attempt))
                        self.log(f"  端点限流/繁忙 HTTP {err_code}，{wait:.0f}s 后重试（第 {attempt + 1}/{attempts} 轮）")
                        time.sleep(wait)
                        throttled = True
                        last_err = f"HTTP {err_code}: {err_body}"
                        break
                    last_err = f"HTTP {err_code}: {err_body}"
                except Exception as e:
                    last_err = e
            if throttled:
                continue
            if attempt + 1 < attempts:
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"OpenAI API 调用失败（已重试 {attempts} 轮）: {last_err}")

    def complete_text(self, prompt: str) -> str:
        return extract_output_text(self.complete(prompt))
