# -*- coding: utf-8 -*-
"""cluster_pkg 配置模型与校验：将极简 YAML/dict 归一为结构化配置对象。"""
from dataclasses import dataclass, field
import os
from typing import Any, Dict, List, Optional, Union

from .yaml_lite import expand_env, load_config_file


def _resolve_path(val: Optional[str], base_dir: str) -> str:
    if not val:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    s = os.path.expanduser(s)
    if os.path.isabs(s):
        return os.path.abspath(s)
    return os.path.abspath(os.path.join(base_dir, s))


def _as_endpoints(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [u.strip() for u in raw.split(",") if u.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(u).strip() for u in raw if str(u).strip()]
    return []


@dataclass
class ApiConfig:
    endpoints: List[str] = field(default_factory=list)
    api_key: str = ""
    model: str = "deepseek-v4.1-flash"
    protocol: str = "auto"           # auto | responses | chat_completions
    timeout: int = 600
    max_output_tokens: int = 32768
    reasoning_effort: str = "none"   # none | low | medium | high | omit
    temperature: Optional[float] = None
    request_attempts: int = 8
    probe_models: bool = False       # 默认不强依赖 GET /models（部分 Responses 代理不暴露 /models）
    proxy: Optional[str] = None      # None=直连绕过系统代理；"env"=使用环境变量代理；或显式代理 URL
    extra_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class ClusterParams:
    concurrency: int = 8
    chunk_size: int = 0              # 0=自动（全量 200，增量 60）
    consensus: int = 1
    retries: int = 3
    path_repair: bool = True
    sessions_all: int = 0
    font_path: str = ""


@dataclass
class AdaptiveConfig:
    enabled: bool = True
    auto_component: bool = True
    auto_leaf: bool = True
    min_leaf_items: int = 1


@dataclass
class L4Config:
    enabled: bool = True
    leaves: str = "auto"             # "auto"=l4_nodes.json 全部已登记父叶；""=关闭；或逗号分隔叶 id
    min_items: int = 1
    chunk_size: int = 60
    force: bool = False


@dataclass
class ClusterConfig:
    input_path: str = ""
    records: Optional[List[Dict[str, Any]]] = None
    tree_dir: str = ""
    output_dir: str = ""
    cases_path: str = ""
    taxonomy_dir: str = ""
    work_dir: str = ""
    channel: str = "auto"
    mode: str = "auto"               # auto | incremental | full | rebuild
    dry_run: bool = False
    api: ApiConfig = field(default_factory=ApiConfig)
    cluster: ClusterParams = field(default_factory=ClusterParams)
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)
    l4: L4Config = field(default_factory=L4Config)

    @classmethod
    def from_source(cls, src: Union[str, Dict[str, Any], "ClusterConfig"], base_dir: Optional[str] = None) -> "ClusterConfig":
        if isinstance(src, ClusterConfig):
            return src
        if isinstance(src, str):
            cfg_path = os.path.abspath(os.path.expanduser(src))
            if not os.path.isfile(cfg_path):
                raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
            bdir = base_dir or os.path.dirname(cfg_path)
            raw = load_config_file(cfg_path)
        elif isinstance(src, dict):
            bdir = base_dir or os.getcwd()
            raw = expand_env(src)
        else:
            raise TypeError(f"不支持的配置类型: {type(src)!r}")

        if not isinstance(raw, dict):
            raise ValueError("配置顶层必须是键值映射（dict）")

        api_raw = raw.get("api") or {}
        endpoints = _as_endpoints(
            api_raw.get("endpoints") or api_raw.get("base_url") or api_raw.get("endpoint")
            or raw.get("base_url") or raw.get("api_endpoint") or os.environ.get("CLUSTER_GATEWAYS", "")
        )
        api_key = str(
            api_raw.get("api_key") or raw.get("api_key")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("EVAL_GATEWAY_API_KEY")
            or "dummy"
        ).strip()
        model = str(api_raw.get("model") or raw.get("model") or os.environ.get("CLUSTER_MODEL") or "deepseek-v4.1-flash").strip()
        proto_raw = str(
            api_raw.get("protocol") or raw.get("protocol") or os.environ.get("CLUSTER_API_PROTOCOL") or "auto"
        ).strip().lower()
        protocol = "chat_completions" if proto_raw == "chat" else proto_raw
        temp_raw = api_raw.get("temperature", raw.get("temperature"))
        api = ApiConfig(
            endpoints=endpoints,
            api_key=api_key or "dummy",
            model=model,
            protocol=protocol,
            timeout=int(api_raw.get("timeout", 600)),
            max_output_tokens=int(api_raw.get("max_output_tokens", 32768)),
            reasoning_effort=str(api_raw.get("reasoning_effort", "none") or "omit").strip(),
            temperature=float(temp_raw) if temp_raw is not None and str(temp_raw) != "" else None,
            request_attempts=int(api_raw.get("request_attempts", 8)),
            probe_models=bool(api_raw.get("probe_models", False)),
            proxy=api_raw.get("proxy"),
            extra_headers=dict(api_raw.get("extra_headers") or {}),
        )

        cl_raw = raw.get("cluster") or {}
        conc = max(1, min(32, int(cl_raw.get("concurrency", raw.get("concurrency", 8)))))
        cluster = ClusterParams(
            concurrency=conc,
            chunk_size=int(cl_raw.get("chunk_size", 0)),
            consensus=max(1, int(cl_raw.get("consensus", 1))),
            retries=max(1, int(cl_raw.get("retries", 3))),
            path_repair=bool(cl_raw.get("path_repair", True)),
            sessions_all=int(cl_raw.get("sessions_all", 0)),
            font_path=_resolve_path(cl_raw.get("font_path") or os.environ.get("CLUSTER_CHART_FONT", ""), bdir),
        )

        ad_raw = raw.get("adaptive")
        if isinstance(ad_raw, bool):
            adaptive = AdaptiveConfig(enabled=ad_raw, auto_component=ad_raw, auto_leaf=ad_raw)
        else:
            ad_dict = ad_raw if isinstance(ad_raw, dict) else {}
            en = bool(ad_dict.get("enabled", True))
            adaptive = AdaptiveConfig(
                enabled=en,
                auto_component=en and bool(ad_dict.get("auto_component", True)),
                auto_leaf=en and bool(ad_dict.get("auto_leaf", True)),
                min_leaf_items=max(1, int(ad_dict.get("min_leaf_items", 1))),
            )

        l4_raw = raw.get("l4")
        if isinstance(l4_raw, bool):
            l4 = L4Config(enabled=l4_raw)
        else:
            l4_dict = l4_raw if isinstance(l4_raw, dict) else {}
            l4 = L4Config(
                enabled=bool(l4_dict.get("enabled", True)),
                leaves=str(l4_dict.get("leaves", "auto") or "").strip(),
                min_items=max(1, int(l4_dict.get("min_items", 1))),
                chunk_size=max(10, int(l4_dict.get("chunk_size", 60))),
                force=bool(l4_dict.get("force", False)),
            )

        tree_dir = _resolve_path(raw.get("tree_dir") or raw.get("cluster_tree_dir"), bdir)
        output_dir = _resolve_path(raw.get("output_dir") or raw.get("out"), bdir) or tree_dir
        input_path = _resolve_path(raw.get("input") or raw.get("input_path") or raw.get("attr_path"), bdir)
        cases_path = _resolve_path(raw.get("cases") or raw.get("eval_input") or raw.get("cases_path"), bdir)
        taxonomy_dir = _resolve_path(raw.get("taxonomy_dir"), bdir)
        work_dir = _resolve_path(raw.get("work_dir"), bdir)
        mode = str(raw.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "incremental", "full", "rebuild"):
            raise ValueError(f"mode 只能是 auto / incremental / full / rebuild，收到: {mode!r}")

        records_raw = raw.get("records") if "records" in raw else raw.get("items")
        if records_raw is not None and not isinstance(records_raw, list):
            raise ValueError("`records` / `items` 必须是字典列表 (list[dict])")

        cfg = cls(
            input_path=input_path,
            records=list(records_raw) if records_raw is not None else None,
            tree_dir=tree_dir,
            output_dir=output_dir,
            cases_path=cases_path,
            taxonomy_dir=taxonomy_dir,
            work_dir=work_dir,
            channel=str(raw.get("channel") or "auto").strip() or "auto",
            mode=mode,
            dry_run=bool(raw.get("dry_run", False)),
            api=api,
            cluster=cluster,
            adaptive=adaptive,
            l4=l4,
        )
        cfg.validate()
        return cfg

    def validate(self):
        if not self.input_path and self.records is None:
            raise ValueError("配置缺少必填项 `input`（待聚类的归因文件/目录路径，或内存列表 `records`）")
        if self.input_path and not os.path.exists(self.input_path):
            raise FileNotFoundError(f"输入路径不存在: {self.input_path}")
        if not self.tree_dir and not self.output_dir:
            raise ValueError("配置缺少必填项 `tree_dir`（聚类树文件目录）")
        if not self.output_dir:
            self.output_dir = self.tree_dir
        if not self.dry_run and not self.api.endpoints:
            raise ValueError("配置缺少必填项 `api.base_url`（OpenAI 协议端点）")
        if self.api.protocol not in ("auto", "responses", "chat_completions"):
            raise ValueError(
                f"api.protocol 只能是 auto / responses / chat_completions，收到: {self.api.protocol!r}"
            )
