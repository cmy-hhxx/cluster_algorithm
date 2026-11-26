#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cluster_pkg 端到端测试套件：
1. 纯标准库 YAML 解析与 ${ENV_VAR} 插值
2. 本地真实 HTTP Server 模拟 OpenAI `/v1/responses` 协议（含 400 reasoning 自动降级、429 重试）
3. 冷启动全量聚类 + L4 下钻 + 重复执行 noop + 新增批次增量复用
4. 兼容加载既有历史聚类树目录（含无 taxonomy 子目录的历史树与自定义叶子吸收）
5. 未知 component 自动路由 + 新失效模式自适应建叶（含 matrix_na 联动更新与 taxonomy_lint 零错误门禁）
6. 复制 cluster_pkg 到全新外部目录独立运行 `python run_cluster.py -c config.yaml` 与 `import cluster_pkg`
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(THIS_DIR)
PARENT_DIR = os.path.dirname(PKG_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from cluster_pkg import ClusterConfig, TaxonomyStore, prune_tree, run_cluster  # noqa: E402
from cluster_pkg.yaml_lite import load_config_file, parse_yaml_stdlib  # noqa: E402


class _MockResponsesServer:
    """本地线程级 HTTP 服务器，完整实现 OpenAI `/v1/responses` 与 `/v1/chat/completions` 协议契约。"""

    def __init__(
        self,
        reject_reasoning_once=False,
        throttle_once=False,
        chat_only=False,
        require_max_completion_tokens=False,
        fail_large_l4_slice=False,
    ):
        self.reject_reasoning_once = reject_reasoning_once
        self.throttle_once = throttle_once
        self.chat_only = chat_only
        self.require_max_completion_tokens = require_max_completion_tokens
        self.fail_large_l4_slice = fail_large_l4_slice
        self.requests = []
        self._lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._send(200, {"data": [{"id": "mock-model"}]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                is_resp = self.path.endswith("/responses")
                is_chat = self.path.endswith("/chat/completions")
                if not is_resp and not is_chat:
                    self._send(404, {"error": f"bad path {self.path}"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                auth = self.headers.get("Authorization", "")
                with parent._lock:
                    parent.requests.append({"path": self.path, "auth": auth, "body": body})
                    if is_resp and parent.chat_only:
                        self._send(404, {"error": {"message": "Not found: /responses is not supported"}})
                        return
                    if is_chat and parent.require_max_completion_tokens and "max_tokens" in body:
                        self._send(400, {
                            "error": {
                                "message": "Unsupported parameter: 'max_tokens' is not supported. Use 'max_completion_tokens' instead."
                            }
                        })
                        return
                    if is_resp and parent.reject_reasoning_once and "reasoning" in body:
                        parent.reject_reasoning_once = False
                        self._send(400, {"error": {"message": "Unsupported parameter: reasoning"}})
                        return
                    if parent.throttle_once:
                        parent.throttle_once = False
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Retry-After", "0.05")
                        self.end_headers()
                        self.wfile.write(b'{"error":"rate_limited"}')
                        return

                if is_resp:
                    prompt = body.get("input", "")
                    out_text = parent._dispatch(prompt)
                    self._send(200, {
                        "id": "resp_mock",
                        "object": "response",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": out_text}],
                        }],
                    })
                else:
                    msgs = body.get("messages") or []
                    prompt = "\n".join(str(m.get("content", "")) for m in msgs if isinstance(m, dict))
                    out_text = parent._dispatch(prompt)
                    self._send(200, {
                        "id": "chatcmpl_mock",
                        "object": "chat.completion",
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": out_text},
                        }],
                    })

            def _send(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _extract_input_rows(prompt: str):
        rows = []
        for ln in prompt.splitlines():
            s = ln.strip()
            if s.startswith('{"seq":'):
                try:
                    obj = json.loads(s)
                    if "component" in obj:
                        rows.append(obj)
                except Exception:
                    pass
        return rows

    def _dispatch(self, prompt: str) -> str:
        # 1) 未知组件路由提示词
        if "待路由的新组件与样例" in prompt:
            out = []
            for ln in prompt.splitlines():
                s = ln.strip()
                if s.startswith('{"raw":'):
                    d = json.loads(s)
                    out.append(json.dumps({
                        "raw": d["raw"],
                        "tool": d["raw"],
                        "label": "量化因子引擎",
                        "role": "tool",
                        "stage": "S3",
                    }, ensure_ascii=False))
            return "\n".join(out)

        # 2) 自适应新叶归纳提示词
        if "自适应新叶归纳" in prompt:
            rows = self._extract_input_rows(prompt)
            return json.dumps({
                "new_leaves": [{
                    "key": "NEW_1",
                    "l2": "M6",
                    "name": "抓取摘要反向断言",
                    "plain": "网页摘要与原网页相反。",
                    "definition": "网页检索抓取层返回的摘要结论与目标页面正文直接相反。（本叶仅收S5段产生的缺陷）",
                    "include": "抓取回执附带的摘要把公告利空写成利好或数值方向写反。",
                    "exclude": "终稿阶段编造 → `S9.M6.4`；其他情况归 `S5.M4.1`。",  # 故意含“其他”，验证清洗器确保 lint=0
                    "tags": ["编造"],
                    "sig": "抓取回执 summary 与网页原文语义相反",
                    "near": "S5.M4.1",
                    "split": "正文不相关 → M4.1；摘要与原文相反 → 本叶",
                }],
                "assignments": [{"seq": r["seq"], "target": "NEW_1", "edge": False} for r in rows],
            }, ensure_ascii=False)

        # 3) L4 子类归类提示词
        if "本次子类清单（只能从这里选" in prompt:
            m = re.search(r"只能从这里选 (S\d+\.M[1-7]\.\d+)\.N", prompt)
            leaf = m.group(1) if m else "S3.M3.2"
            rows = self._extract_input_rows(prompt)
            if self.fail_large_l4_slice and len(rows) >= 20:
                return json.dumps({"seq": rows[0]["seq"], "path": f"{leaf}.4", "edge": False})
            return "\n".join(
                json.dumps({"seq": r["seq"], "path": f"{leaf}.4", "edge": False})
                for r in rows
            )

        # 4) 带合法清单重问（PATH_REPAIR）
        if "正在复核一批上一轮判定被退回的条目" in prompt:
            rows = self._extract_input_rows(prompt)
            out = []
            for r in rows:
                if "反向断言" in (r.get("label") or ""):
                    out.append(json.dumps({"seq": r["seq"], "path": "NONE", "uncertain": True}))
                else:
                    valid_ids = re.findall(r"\| `(S\d+\.M[1-7]\.\d+)` \|", prompt)
                    out.append(json.dumps({"seq": r["seq"], "path": valid_ids[0], "uncertain": True}))
            return "\n".join(out)

        # 5) 常规 L3 归类
        valid_ids = re.findall(r"\| `(S\d+\.M[1-7]\.\d+)` \|", prompt)
        default_id = "S3.M3.2" if "S3.M3.2" in valid_ids else (valid_ids[0] if valid_ids else "S3.M3.1")
        rows = self._extract_input_rows(prompt)
        out = []
        for r in rows:
            if "反向断言" in (r.get("label") or ""):
                # 如果分类法已经自适应建过 S5.M6.1，直接命中；否则给越界 S5.M6.99 触发重问 -> NONE -> 建叶
                chosen = "S5.M6.1" if "S5.M6.1" in valid_ids else "S5.M6.99"
                out.append(json.dumps({"seq": r["seq"], "path": chosen, "uncertain": False}))
            else:
                out.append(json.dumps({"seq": r["seq"], "path": default_id, "uncertain": False}))
        return "\n".join(out)


class YamlLiteTest(unittest.TestCase):
    def test_stdlib_yaml_parser_and_env_expansion(self):
        os.environ["TEST_CLUSTER_KEY"] = "sk-secret#hash"
        raw = """
        # 顶层注释
        api:
          base_url: "http://127.0.0.1:8080/v1"   # 带冒号和注释
          endpoints:
            - http://10.0.0.1:5001/v1
            - "http://10.0.0.2:5001/v1/responses"
          api_key: "${TEST_CLUSTER_KEY}"
          fallback_env: "${NON_EXISTENT_VAR:-default_val}"
        cluster:
          concurrency: 6
          path_repair: true
        tags: [a, "b,c", 3]
        """
        parsed = parse_yaml_stdlib(raw)
        self.assertEqual(parsed["api"]["base_url"], "http://127.0.0.1:8080/v1")
        self.assertEqual(len(parsed["api"]["endpoints"]), 2)
        self.assertEqual(parsed["cluster"]["concurrency"], 6)
        self.assertIs(parsed["cluster"]["path_repair"], True)
        self.assertEqual(parsed["tags"], ["a", "b,c", 3])

        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        try:
            tmp.write(raw)
            tmp.close()
            cfg_dict = load_config_file(tmp.name, force_stdlib=True)
            self.assertEqual(cfg_dict["api"]["api_key"], "sk-secret#hash")
            self.assertEqual(cfg_dict["api"]["fallback_env"], "default_val")
        finally:
            os.unlink(tmp.name)


class ClusterPkgEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_cluster_pkg_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cold_start_incremental_adaptive_and_legacy_tree(self):
        with _MockResponsesServer(reject_reasoning_once=True, throttle_once=True) as srv:
            data_dir = os.path.join(self.tmp, "data")
            tree_dir = os.path.join(self.tmp, "my_tree")
            os.makedirs(data_dir)

            # 第 1 轮：冷启动全量（含 L4 下钻 S3.M3.2 -> S3.M3.2.4）
            f1 = os.path.join(data_dir, "batch1_attribution.jsonl")
            with open(f1, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "session_meta": {"session_id": "s1", "question": "查一下贵州茅台毛利率", "request_time": "2026-11-24 09:00:00"},
                    "turns": [{"meta": {"trace_id": "t1"},
                               "problem": [{"problem_id": "p1", "component": "finquery", "severity": "P0",
                                            "label": "无权限报错", "definition": "VIP表鉴权失败", "reason": "返回403"}]}],
                }, ensure_ascii=False) + "\n")

            res1 = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock-v1", "reasoning_effort": "low"},
                "tree_dir": tree_dir,
                "input": data_dir,
            })
            self.assertEqual(res1["status"], "ok")
            self.assertEqual(res1["mode"], "full")
            self.assertEqual(res1["total_items"], 1)
            self.assertEqual(res1["l4_updated"], 1)
            self.assertEqual(res1["verify"]["errors"], 0)
            self.assertTrue(os.path.isfile(os.path.join(tree_dir, "cluster_matrix.svg")))
            self.assertTrue(os.path.isfile(os.path.join(tree_dir, "cluster_brief.md")))

            # 第 2 轮：无新数据 -> noop（0 次 API 调用）
            calls_before = len(srv.requests)
            res2 = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock-v1"},
                "tree_dir": tree_dir,
                "input": data_dir,
            })
            self.assertEqual(res2["status"], "noop")
            self.assertEqual(len(srv.requests), calls_before)

            # 第 3 轮：增量加入未知组件 + 新失效模式（扁平格式输入），触发自适应组件路由 + 自适应建叶
            f2 = os.path.join(data_dir, "batch2_attribution.jsonl")
            with open(f2, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "trace_id": "t2", "session_id": "s2", "component": "quantum_factor_engine",
                    "component_role": "tool", "severity": "P1",
                    "label": "因子引擎连接超时", "definition": "调用量子因子库超时", "reason": "timeout",
                }, ensure_ascii=False) + "\n")
                fh.write(json.dumps({
                    "trace_id": "t3", "session_id": "s3", "component": "web_fetch",
                    "severity": "P1", "label": "网页摘要反向断言",
                    "definition": "抓取摘要与原网页正文相反", "reason": "原文亏损被摘要成盈利",
                }, ensure_ascii=False) + "\n")

            res3 = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock-v1"},
                "tree_dir": tree_dir,
                "input": data_dir,
            })
            self.assertEqual(res3["status"], "ok")
            self.assertEqual(res3["mode"], "incremental")
            self.assertEqual(res3["reused_items"], 1)
            self.assertEqual(res3["new_items"], 2)
            self.assertEqual(len(res3["created_components"]), 1)
            self.assertEqual([x["id"] for x in res3["created_leaves"]], ["S5.M6.1"])
            self.assertEqual(res3["verify"]["errors"], 0)

            # 验证演进后的分类法严格满足 taxonomy_lint errors=0（包括自动从 matrix_na 移除 S5.M6）
            evolved_tax = TaxonomyStore(os.path.join(tree_dir, "taxonomy"))
            lint_errs, _ = evolved_tax.lint()
            self.assertEqual(lint_errs, [])
            self.assertTrue(os.path.isfile(os.path.join(tree_dir, "taxonomy_evolution.jsonl")))

            # 第 4 轮：再进一条同类 S5.M6.1 新数据，直接命中已持久化的新叶，不再重复建叶！
            f3 = os.path.join(data_dir, "batch3_attribution.jsonl")
            with open(f3, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "trace_id": "t4", "session_id": "s4", "component": "web_fetch",
                    "severity": "P2", "label": "另一条反向断言",
                    "definition": "抓取摘要再次反向", "reason": "相反",
                }, ensure_ascii=False) + "\n")
            res4 = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock-v1"},
                "tree_dir": tree_dir,
                "input": data_dir,
            })
            self.assertEqual(res4["created_leaves"], [])
            self.assertEqual(res4["total_items"], 4)

    def test_legacy_tree_dir_reconcile_and_separate_output(self):
        """测试传入旧流水线产出的聚类树目录（无 taxonomy/ 子目录、含历史自定义叶子），另存增量输出。"""
        legacy_dir = os.path.join(self.tmp, "legacy_tree")
        os.makedirs(legacy_dir)
        with open(os.path.join(legacy_dir, "cluster_tree.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "meta": {"title": "归因问题聚类树"},
                "tree": {
                    "id": "root", "children": [{
                        "id": "S5", "name": "网页检索", "children": [{
                            "id": "S5.M3", "mechanism": "M3", "children": [{
                                "id": "S5.M3.9", "name": "检索网关自定义熔断",
                                "plain": "网关熔断", "definition": "检索服务触发自定义熔断返回空。",
                                "include": "返回 circuit_open。", "exclude": "普通报错 → `S5.M3.1`。",
                                "components": ["web_search"], "tags": ["失败"],
                            }],
                        }],
                    }],
                },
            }, fh, ensure_ascii=False)
        with open(os.path.join(legacy_dir, "item_index.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "seq": 1, "trace_id": "old_t1", "session_id": "old_s1", "problem_index": 0,
                "path": "S5.M3.9", "edge": False, "definition": "历史熔断条目", "reason": "circuit_open",
            }, ensure_ascii=False) + "\n")

        inp = os.path.join(self.tmp, "new_attr.jsonl")
        with open(inp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "trace_id": "new_t2", "session_id": "new_s2", "component": "finquery",
                "severity": "P1", "label": "SQL报错", "definition": "表名写错", "reason": "unknown table",
            }, ensure_ascii=False) + "\n")

        new_out = os.path.join(self.tmp, "evolved_out")
        with _MockResponsesServer() as srv:
            res = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock-v1"},
                "tree_dir": legacy_dir,
                "output_dir": new_out,
                "input": inp,
            })
        self.assertEqual(res["reused_items"], 1)
        self.assertEqual(res["new_items"], 1)
        self.assertEqual(res["total_items"], 2)
        self.assertFalse(os.path.exists(os.path.join(legacy_dir, "taxonomy")), "只读源树目录不应被写入")
        tax = TaxonomyStore(os.path.join(new_out, "taxonomy"))
        self.assertIn("S5.M3.9", tax.node_map())
        self.assertEqual(tax.lint()[0], [])

    def test_standalone_copy_and_cli_execution(self):
        """将 cluster_pkg 复制到仓库外的独立目录，验证零依赖便携性与 `python run_cluster.py -c config.yaml`。"""
        ext_root = os.path.join(self.tmp, "external_project")
        ext_pkg = os.path.join(ext_root, "cluster_pkg")
        shutil.copytree(PKG_DIR, ext_pkg, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        with _MockResponsesServer() as srv:
            inp = os.path.join(ext_root, "attr.jsonl")
            with open(inp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "trace_id": "ext_t1", "session_id": "ext_s1", "component": "finquery",
                    "severity": "P1", "label": "取数报错", "definition": "SQL 语法报错", "reason": "syntax error",
                }, ensure_ascii=False) + "\n")

            cfg_file = os.path.join(ext_root, "config.yaml")
            with open(cfg_file, "w", encoding="utf-8") as fh:
                fh.write(
                    f'api:\n  base_url: "{srv.base_url}/responses"\n  api_key: "sk-ext"\n  model: "mock"\n'
                    f'tree_dir: "./out_tree"\ninput: "./attr.jsonl"\n'
                )

            # 方式 A：进入 cluster_pkg 目录直接执行 `python run_cluster.py -c ../config.yaml`
            r1 = subprocess.run(
                [sys.executable, "run_cluster.py", "-c", "../config.yaml"],
                cwd=ext_pkg, capture_output=True, text=True,
            )
            self.assertEqual(r1.returncode, 0, f"CLI 失败:\n{r1.stdout}\n{r1.stderr}")
            self.assertTrue(os.path.isfile(os.path.join(ext_root, "out_tree", "cluster_tree.json")))

            # 方式 B：在外部业务根目录执行 `python -m cluster_pkg -c config.yaml --recall S3.M3.2`
            r2 = subprocess.run(
                [sys.executable, "-m", "cluster_pkg", "-c", "config.yaml", "--recall", "S3.M3.2"],
                cwd=ext_root, capture_output=True, text=True,
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("matched=1", r2.stdout)

            # 方式 C：在外部业务代码中直接内存传入 records 调用（无需落盘输入 jsonl）
            r3 = subprocess.run(
                [
                    sys.executable, "-c",
                    f"import json; from cluster_pkg import run_cluster; "
                    f"res = run_cluster({{'api': {{'base_url': '{srv.base_url}', 'api_key': 'sk-ext', 'model': 'mock'}}, "
                    f"'tree_dir': './out_tree', 'records': [{{'trace_id': 'ext_t2', 'session_id': 'ext_s2', "
                    f"'component': 'finquery', 'label': '内存调用', 'definition': '测试内存列表输入', 'reason': 'ok'}}]}}); "
                    f"assert res['total_items'] == 2 and res['reused_items'] == 1, res; print('MEM_API_OK')"
                ],
                cwd=ext_root, capture_output=True, text=True,
            )
            self.assertEqual(r3.returncode, 0, f"{r3.stdout}\n{r3.stderr}")
            self.assertIn("MEM_API_OK", r3.stdout)

    def test_offline_pip_install_in_clean_venv(self):
        """验证纯标准库 PEP 517 后端支持在无网络、无 setuptools 的裸 venv 中直接 pip install 并跨目录调用。"""
        venv_dir = os.path.join(self.tmp, "bare_venv")
        caller_dir = os.path.join(self.tmp, "third_party_app")
        os.makedirs(caller_dir)
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
        vpy = os.path.join(venv_dir, "bin", "python")
        vpip = os.path.join(venv_dir, "bin", "pip")
        vcli = os.path.join(venv_dir, "bin", "cluster-run")

        r_inst = subprocess.run(
            [vpip, "install", "--no-index", "--no-build-isolation", PKG_DIR],
            cwd=caller_dir, capture_output=True, text=True,
        )
        self.assertEqual(r_inst.returncode, 0, f"离线 pip install 失败:\n{r_inst.stdout}\n{r_inst.stderr}")

        with _MockResponsesServer() as srv:
            r_call = subprocess.run(
                [
                    vpy, "-c",
                    f"import os, cluster_pkg; "
                    f"assert 'site-packages' in cluster_pkg.__file__; "
                    f"res = cluster_pkg.run_cluster({{'api': {{'base_url': '{srv.base_url}', 'model': 'm'}}, "
                    f"'tree_dir': './app_tree', 'records': [{{'trace_id': 'v1', 'component': 'finquery', "
                    f"'label': '测试', 'definition': '定义', 'reason': '原因'}}]}}); "
                    f"assert res['verify']['errors'] == 0; print('VENV_CALL_OK')"
                ],
                cwd=caller_dir, capture_output=True, text=True,
            )
            self.assertEqual(r_call.returncode, 0, f"{r_call.stdout}\n{r_call.stderr}")
            self.assertIn("VENV_CALL_OK", r_call.stdout)

        r_cli = subprocess.run([vcli, "--help"], cwd=caller_dir, capture_output=True, text=True)
        self.assertEqual(r_cli.returncode, 0, r_cli.stderr)

    def test_chat_completions_auto_fallback_and_explicit_modes(self):
        """验证 protocol=auto 自动从 /responses 降级 /chat/completions，以及显式 chat_completions 模式。"""
        with _MockResponsesServer(chat_only=True, require_max_completion_tokens=True) as srv:
            tree_dir = os.path.join(self.tmp, "chat_tree")
            res1 = run_cluster({
                "api": {
                    "base_url": srv.base_url,
                    "protocol": "auto",
                    "api_key": "sk-chat",
                    "model": "gpt-4.1-mini",
                    "reasoning_effort": "low",
                },
                "l4": {"enabled": False},
                "tree_dir": tree_dir,
                "records": [{
                    "trace_id": "tc1", "session_id": "sc1", "component": "finquery",
                    "label": "报错", "definition": "SQL 报错", "reason": "syntax error",
                }],
            })
            self.assertEqual(res1["status"], "ok")
            self.assertEqual(res1["verify"]["errors"], 0)
            paths = [req["path"] for req in srv.requests]
            self.assertEqual(paths[0], "/v1/responses")
            self.assertIn("/v1/chat/completions", paths)
            # 后续请求不再发送 max_tokens，直接用 max_completion_tokens
            self.assertIn("max_completion_tokens", srv.requests[-1]["body"])
            self.assertNotIn("reasoning", srv.requests[-1]["body"])

            # 显式 protocol=chat_completions 与显式 /chat/completions URL 均直连 chat/completions
            before_n = len(srv.requests)
            res2 = run_cluster({
                "api": {
                    "base_url": f"{srv.base_url}/chat/completions",
                    "protocol": "auto",
                    "api_key": "sk-chat",
                    "model": "gpt-4.1-mini",
                },
                "l4": {"enabled": False},
                "tree_dir": tree_dir,
                "records": [
                    {"trace_id": "tc1", "session_id": "sc1", "component": "finquery", "label": "报错", "definition": "SQL 报错"},
                    {"trace_id": "tc2", "session_id": "sc2", "component": "finquery", "label": "超时", "definition": "查询超时"},
                ],
            })
            self.assertEqual(res2["total_items"], 2)
            new_paths = [req["path"] for req in srv.requests[before_n:]]
            self.assertTrue(all(p == "/v1/chat/completions" for p in new_paths))

    def test_prune_tree_api_and_cli(self):
        """验证 prune_tree 与 CLI --prune 保留 L4 4 段路径、0 API 调重聚合、备份与留痕。"""
        with _MockResponsesServer() as srv:
            tree_dir = os.path.join(self.tmp, "prune_tree")
            res = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock"},
                "tree_dir": tree_dir,
                "records": [
                    {"trace_id": "t_keep", "case_id": "c_keep", "session_id": "s_keep", "component": "finquery",
                     "severity": "P0", "label": "保留条目", "definition": "VIP鉴权失败", "reason": "403"},
                    {"trace_id": "t_self", "case_id": "c_self", "session_id": "t_self", "component": "finquery",
                     "severity": "P1", "label": "自证会话", "definition": "session_id 缺失兜底", "reason": "none"},
                    {"trace_id": "t_drop_case", "case_id": "c_drop_case", "session_id": "s_dc", "component": "finquery",
                     "severity": "P1", "label": "按case删", "definition": "脏数据1", "reason": "bad1"},
                    {"trace_id": "t_drop_trace", "case_id": "c_keep2", "session_id": "s_dt", "component": "finquery",
                     "severity": "P2", "label": "按trace删", "definition": "脏数据2", "reason": "bad2"},
                ],
            })
            self.assertEqual(res["total_items"], 4)
            self.assertEqual(res["l4_updated"], 4)
            calls_before = len(srv.requests)

            # 1) dry_run 不改动文件
            dry = prune_tree(tree_dir, drop_ids=["c_drop_case"], self_session=True, dry_run=True)
            self.assertEqual(dry["status"], "dry_run")
            self.assertEqual(dry["dropped_items"], 2)
            self.assertEqual(dry["left_items"], 2)

            # 2) Python API 清理 self_session + case_id
            p1 = prune_tree(tree_dir, drop_ids=["c_drop_case"], self_session=True, reason="api_prune")
            self.assertEqual(p1["status"], "ok")
            self.assertEqual(p1["dropped_items"], 2)
            self.assertEqual(p1["left_items"], 2)
            self.assertEqual(p1["verify"]["errors"], 0)
            self.assertTrue(os.path.isdir(p1["backup"]))

            # 3) CLI --prune 按 trace_id 再删一条，验证无 config 也可独立运行
            drop_file = os.path.join(self.tmp, "drop_traces.txt")
            with open(drop_file, "w", encoding="utf-8") as fh:
                fh.write("t_drop_trace\n")
            cli = subprocess.run(
                [sys.executable, os.path.join(PKG_DIR, "run_cluster.py"),
                 "--prune", "--tree-dir", tree_dir, "--drop-ids", drop_file, "--reason", "cli_prune"],
                capture_output=True, text=True,
            )
            self.assertEqual(cli.returncode, 0, f"{cli.stdout}\n{cli.stderr}")
            self.assertEqual(len(srv.requests), calls_before, "prune 不得发起任何 LLM API 请求")

            with open(os.path.join(tree_dir, "item_index.jsonl"), encoding="utf-8") as fh:
                left = [json.loads(ln) for ln in fh if ln.strip()]
            self.assertEqual(len(left), 1)
            self.assertEqual(left[0]["trace_id"], "t_keep")
            self.assertEqual(left[0]["path"], "S3.M3.2.4", "prune 必须完整保留已有 L4 4 段路径")

            with open(os.path.join(tree_dir, "prune.jsonl"), encoding="utf-8") as fh:
                audits = [json.loads(ln) for ln in fh if ln.strip()]
            self.assertEqual(len(audits), 2)

            # 4) 尝试删空全部条目必须中止且保护原树
            with self.assertRaises(RuntimeError):
                prune_tree(tree_dir, drop_ids=["t_keep"], reason="drop_all_should_fail")
            with open(os.path.join(tree_dir, "item_index.jsonl"), encoding="utf-8") as fh:
                self.assertEqual(len([ln for ln in fh if ln.strip()]), 1)

    def test_l4_force_on_no_new_items_and_chunk_halving(self):
        """验证无新增条目时 l4.force=True 不走 noop，且 >=20 条的失败 L4 分片会自动对半拆片成功。"""
        records = [
            {"trace_id": f"tl4_{i}", "session_id": f"sl4_{i}", "component": "finquery",
             "severity": "P1", "label": "取数报错", "definition": f"SQL 错误 #{i}", "reason": "err"}
            for i in range(22)
        ]
        with _MockResponsesServer(fail_large_l4_slice=True) as srv:
            tree_dir = os.path.join(self.tmp, "l4_halve_tree")
            # 第 1 轮先关闭 L4，建立 22 条 3 段父叶基线
            res1 = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock"},
                "l4": {"enabled": False},
                "tree_dir": tree_dir,
                "records": records,
            })
            self.assertEqual(res1["l4_updated"], 0)

            # 第 2 轮输入完全不变，但开启 l4.force=True 且 chunk_size=30：
            # 必须不走 noop，而是触发 L4 下钻并在 22 条分片失败后对半拆成 11+11 全部升级为 S3.M3.2.4
            res2 = run_cluster({
                "api": {"base_url": srv.base_url, "api_key": "sk-test", "model": "mock"},
                "l4": {"enabled": True, "force": True, "chunk_size": 30},
                "tree_dir": tree_dir,
                "records": records,
            })
            self.assertEqual(res2["status"], "ok")
            self.assertEqual(res2["l4_updated"], 22)
            with open(os.path.join(tree_dir, "item_index.jsonl"), encoding="utf-8") as fh:
                paths = {json.loads(ln)["path"] for ln in fh if ln.strip()}
            self.assertEqual(paths, {"S3.M3.2.4"})


if __name__ == "__main__":
    unittest.main()
