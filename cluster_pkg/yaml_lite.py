# -*- coding: utf-8 -*-
"""零外部依赖配置文件解析器：优先使用 PyYAML（若已安装），否则回退纯标准库 YAML/JSON 解析。

支持特性：
  - 嵌套键值映射（按缩进分层）
  - 块级列表（`- item` 与 `- key: val`）及内联列表/字典（`[a, b]`、`{k: v}`）
  - 单双引号字符串、转义字符、整数、浮点数、布尔值（true/false）、空值（null/~）
  - 行尾与整行 `#` 注释（严格忽略引号内部的 `#`）
  - `${ENV_VAR}` 与 `${ENV_VAR:-default}` 环境变量插值
"""
import ast
import json
import os
import re

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(obj):
    """递归展开字符串中的 ${VAR} 与 ${VAR:-default}。"""
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""), obj)
    if isinstance(obj, list):
        return [expand_env(x) for x in obj]
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    return obj


def _strip_comment(line):
    in_sq = in_dq = esc = False
    for i, ch in enumerate(line):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_dq:
            esc = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "#" and not in_sq and not in_dq:
            if i == 0 or line[i - 1].isspace():
                return line[:i].rstrip()
    return line.rstrip()


def _split_top(text, sep=","):
    parts, cur = [], []
    in_sq = in_dq = esc = False
    depth = 0
    for ch in text:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == "\\" and in_dq:
            cur.append(ch)
            esc = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif not in_sq and not in_dq:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth = max(0, depth - 1)
            elif ch == sep and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
                continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _find_colon(s):
    """定位顶层键后的冒号（忽略引号与括号内冒号，要求冒号后为空或空白，避免切坏 http://...）。"""
    in_sq = in_dq = esc = False
    depth = 0
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_dq:
            esc = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif not in_sq and not in_dq:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth = max(0, depth - 1)
            elif ch == ":" and depth == 0:
                if i + 1 == len(s) or s[i + 1].isspace():
                    return i
    return -1


def _parse_scalar(val):
    s = val.strip()
    if not s or s in ("null", "Null", "NULL", "~"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        if s.startswith("'") and len(s) >= 2:
            return s[1:-1].replace("''", "'")
        try:
            return ast.literal_eval(s)
        except Exception:
            return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_parse_scalar(x) for x in _split_top(inner, ",")] if inner else []
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        out = {}
        if inner:
            for item in _split_top(inner, ","):
                idx = _find_colon(item)
                if idx < 0:
                    raise ValueError(f"无法解析内联字典项: {item!r}")
                out[str(_parse_scalar(item[:idx]))] = _parse_scalar(item[idx + 1:])
        return out
    if re.fullmatch(r"[+-]?\d+", s):
        try:
            return int(s)
        except ValueError:
            pass
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?", s):
        try:
            return float(s)
        except ValueError:
            pass
    return s


def parse_yaml_stdlib(text):
    """纯标准库缩进式 YAML 子集解析器。"""
    stripped = text.lstrip("\ufeff").strip()
    if not stripped:
        return {}
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except Exception:
            pass

    lines = []
    for ln_no, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        cleaned = _strip_comment(raw)
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        if "\t" in cleaned[:indent]:
            raise ValueError(f"第 {ln_no} 行缩进包含 Tab，请改用空格")
        lines.append((ln_no, indent, cleaned.strip()))

    if not lines:
        return {}

    def parse_block(idx, min_indent):
        if idx >= len(lines):
            return {}, idx
        _, cur_indent, first_txt = lines[idx]
        if cur_indent < min_indent:
            return None, idx
        is_list = first_txt == "-" or first_txt.startswith("- ")
        if is_list:
            arr = []
            while idx < len(lines):
                ln_no, ind, txt = lines[idx]
                if ind < cur_indent:
                    break
                if ind > cur_indent:
                    raise ValueError(f"第 {ln_no} 行列表缩进异常: {txt!r}")
                if not (txt == "-" or txt.startswith("- ")):
                    break
                rest = txt[1:].strip()
                idx += 1
                if not rest:
                    sub, idx = parse_block(idx, cur_indent + 1)
                    arr.append(sub)
                else:
                    cidx = _find_colon(rest)
                    if cidx >= 0:
                        k = str(_parse_scalar(rest[:cidx]))
                        v_str = rest[cidx + 1:].strip()
                        item_dict = {}
                        if v_str:
                            item_dict[k] = _parse_scalar(v_str)
                        else:
                            sub, idx = parse_block(idx, cur_indent + 1)
                            item_dict[k] = sub
                        if idx < len(lines) and lines[idx][1] > cur_indent:
                            extra, idx = parse_block(idx, cur_indent + 1)
                            if isinstance(extra, dict):
                                item_dict.update(extra)
                        arr.append(item_dict)
                    else:
                        arr.append(_parse_scalar(rest))
            return arr, idx

        mapping = {}
        while idx < len(lines):
            ln_no, ind, txt = lines[idx]
            if ind < cur_indent:
                break
            if ind > cur_indent:
                raise ValueError(f"第 {ln_no} 行映射缩进异常: {txt!r}")
            if txt == "-" or txt.startswith("- "):
                break
            cidx = _find_colon(txt)
            if cidx < 0:
                raise ValueError(f"第 {ln_no} 行缺少键值冒号: {txt!r}")
            key = str(_parse_scalar(txt[:cidx]))
            val_str = txt[cidx + 1:].strip()
            idx += 1
            if val_str in ("|", ">"):
                block_lines = []
                b_indent = None
                while idx < len(lines) and lines[idx][1] > cur_indent:
                    _, bind, btxt = lines[idx]
                    if b_indent is None:
                        b_indent = bind
                    block_lines.append(btxt)
                    idx += 1
                sep = "\n" if val_str == "|" else " "
                mapping[key] = sep.join(block_lines)
            elif val_str:
                mapping[key] = _parse_scalar(val_str)
            else:
                if idx < len(lines) and lines[idx][1] > cur_indent:
                    sub, idx = parse_block(idx, cur_indent + 1)
                    mapping[key] = sub
                else:
                    mapping[key] = None
        return mapping, idx

    result, _ = parse_block(0, 0)
    return result if result is not None else {}


def load_config_file(path, force_stdlib=False):
    """读取 YAML 或 JSON 配置文件并展开 ${ENV_VAR}。"""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.lower().endswith(".json"):
        data = json.loads(text)
        return expand_env(data or {})
    if not force_stdlib:
        try:
            import yaml  # type: ignore
            if yaml is not None and hasattr(yaml, "safe_load"):
                data = yaml.safe_load(text)
                return expand_env(data or {})
        except ImportError:
            pass
    return expand_env(parse_yaml_stdlib(text) or {})
