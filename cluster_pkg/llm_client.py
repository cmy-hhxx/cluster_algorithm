# -*- coding: utf-8 -*-
"""OpenAI-Responses 协议客户端（纯标准库 urllib，零第三方依赖）。

支持：
  - 自动规范化端点 URL（兼容 `.../v1` 与 `.../v1/responses`）
  - 多端点轮询容灾 + 429/502/503/504 指数退避与 Retry-After 遵循
  - 目标模型不支持 `reasoning.effort` 参数时自动剥离并重试（兼容各类 OpenAI-Responses 网关）
  - 提取标准 `output[].content[].output_text`、`output_text` 便捷字段以及流外文本
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
    parsed = urllib.parse.urlparse(u)
    if parsed.path in ("", "/") and parsed.hostname == "api.openai.com":
        return u + "/v1/responses"
    return u + "/responses"


def normalize_models_url(endpoint: str) -> str:
    r = normalize_responses_url(endpoint)
    return r[: -len("/responses")] + "/models"


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
        if isinstance(msg.get("content"), str):
            chunks.append(msg["content"])
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
        self.endpoints = [normalize_responses_url(u) for u in cfg.endpoints]
        self._rr = itertools.count()
        self._lock = threading.Lock()
        self._allow_reasoning = cfg.reasoning_effort not in ("", "omit", "default", "null", "None")
        self.calls = 0

    def _build_opener(self):
        if self.cfg.proxy == "env":
            return urllib.request.build_opener()
        if self.cfg.proxy:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.cfg.proxy, "https": self.cfg.proxy})
            )
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def check_ready(self):
        """可选探活：仅当开启 probe_models 时检查 /models；404/405 视为端点仅开放 /responses，放行。"""
        if not self.endpoints:
            raise RuntimeError("未配置任何 OpenAI-Responses API 端点")
        if not self.cfg.probe_models:
            return list(self.endpoints)
        healthy = []
        opener = self._build_opener()
        for ep in self.cfg.endpoints:
            murl = normalize_models_url(ep)
            for attempt in range(3):
                req = urllib.request.Request(
                    murl, headers={"Authorization": f"Bearer {self.cfg.api_key}", **self.cfg.extra_headers}
                )
                try:
                    with opener.open(req, timeout=8) as r:
                        if r.status == 200:
                            healthy.append(normalize_responses_url(ep))
                            break
                except urllib.error.HTTPError as e:
                    if e.code in (404, 405):
                        healthy.append(normalize_responses_url(ep))
                        break
                    if e.code == 401:
                        raise RuntimeError(f"API 鉴权失败 (401): {murl}，请检查 api.api_key") from e
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if not healthy:
            raise RuntimeError("所有配置的 API 端点探活均失败: " + ", ".join(self.cfg.endpoints))
        self.endpoints = healthy
        return healthy

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
        """发送一次 OpenAI-Responses 请求并返回解析后的响应字典。"""
        if not self.endpoints:
            raise RuntimeError("没有可用的 OpenAI-Responses 端点")
        attempts = max(1, self.cfg.request_attempts)
        idx = next(self._rr) % len(self.endpoints)
        order = self.endpoints[idx:] + self.endpoints[:idx]
        opener = self._build_opener()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
            **self.cfg.extra_headers,
        }
        last_err = None

        for attempt in range(attempts):
            throttled = False
            for url in order:
                use_reasoning = self._allow_reasoning
                payload = self._make_payload(prompt, use_reasoning)
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                try:
                    with opener.open(req, timeout=self.cfg.timeout) as resp:
                        raw = resp.read()
                    with self._lock:
                        self.calls += 1
                    return json.loads(raw)
                except urllib.error.HTTPError as e:
                    err_body = ""
                    err_code = e.code
                    wait_ra = self._retry_after(e)
                    try:
                        err_body = e.read().decode("utf-8", errors="replace")[:500]
                    except Exception:
                        pass
                    finally:
                        try:
                            e.close()
                        except Exception:
                            pass
                    if err_code == 400 and use_reasoning and (
                        "reasoning" in err_body.lower() or "unsupported" in err_body.lower()
                        or "unknown" in err_body.lower() or "extra_forbidden" in err_body.lower()
                    ):
                        with self._lock:
                            if self._allow_reasoning:
                                self._allow_reasoning = False
                                self.log("  提示：目标模型不支持 reasoning 参数，已自动切换为无 reasoning 参数重试")
                        req2 = urllib.request.Request(
                            url, data=self._make_payload(prompt, False), headers=headers, method="POST"
                        )
                        try:
                            with opener.open(req2, timeout=self.cfg.timeout) as resp2:
                                raw2 = resp2.read()
                            with self._lock:
                                self.calls += 1
                            return json.loads(raw2)
                        except urllib.error.HTTPError as e2:
                            try:
                                e2.close()
                            except Exception:
                                pass
                            last_err = e2
                            continue
                        except Exception as e2:
                            last_err = e2
                            continue
                    if err_code == 401:
                        raise RuntimeError(f"OpenAI-Responses 鉴权失败 (HTTP 401): {err_body}") from None
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
        raise RuntimeError(f"OpenAI-Responses 调用失败（已重试 {attempts} 轮）: {last_err}")

    def complete_text(self, prompt: str) -> str:
        return extract_output_text(self.complete(prompt))
