#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for clustering experiments (eval set, TF-IDF retrieval, scoring)."""
import collections, glob, json, math, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDX = os.path.join(ROOT, "examples/lost_user_eval_2026-10_9stage/item_index.jsonl")
AUD_IN = os.path.join(ROOT, "audit/in")
AUD_A = os.path.join(ROOT, "audit/a")
AUD_B = os.path.join(ROOT, "audit/b")


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def load_items():
    return {r["seq"]: r for r in load_jsonl(IDX)}


def load_gold():
    """Return seq -> {path, agree, a, b}. Gold = a and b agree."""
    A, B = {}, {}
    for p in glob.glob(os.path.join(AUD_A, "*.out.jsonl")):
        for r in load_jsonl(p):
            A[r["seq"]] = r
    for p in glob.glob(os.path.join(AUD_B, "*.out.jsonl")):
        for r in load_jsonl(p):
            B[r["seq"]] = r
    out = {}
    for s in A:
        out[s] = {"a": A[s]["path"], "b": B[s]["path"],
                  "path": A[s]["path"] if A[s]["path"] == B[s]["path"] else None,
                  "agree": A[s]["path"] == B[s]["path"], "l1": A[s]["path"].split(".")[0]}
    return out


def item_text(r, fields=("label", "definition", "reason")):
    return " ".join(str(r.get(k, "") or "") for k in fields)


def tokens(s):
    s = (s or "").lower()
    out = ["w:" + w for w in re.findall(r"[a-z0-9_]+", s)]
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        for n in (2, 3):
            out += [run[i:i + n] for i in range(len(run) - n + 1)]
    return out


class Tfidf:
    """Small sparse TF-IDF cosine index over a handful of documents (leaves)."""

    def __init__(self, docs, repeat=1):
        self.keys = list(docs)
        self.toks = {k: tokens(docs[k]) for k in self.keys}
        df = collections.Counter()
        for t in self.toks.values():
            df.update(set(t))
        N = len(self.keys)
        self.idf = {t: math.log((N + 1) / (v + 1)) + 1 for t, v in df.items()}
        self.vec = {}
        for k, t in self.toks.items():
            c = collections.Counter(t)
            v = {x: (1 + math.log(n)) * self.idf[x] for x, n in c.items()}
            norm = math.sqrt(sum(w * w for w in v.values())) or 1.0
            self.vec[k] = (v, norm)

    def rank(self, text):
        q = collections.Counter(tokens(text))
        qv = {x: (1 + math.log(n)) * self.idf[x] for x, n in q.items() if x in self.idf}
        qn = math.sqrt(sum(w * w for w in qv.values())) or 1.0
        scores = {}
        for k, (v, norm) in self.vec.items():
            s = 0.0
            for x, w in qv.items():
                s += w * v.get(x, 0.0)
            scores[k] = s / (norm * qn)
        return sorted(scores.items(), key=lambda kv: -kv[1])


def leaf_text(n):
    return " ".join(str(n.get(k, "") or "") for k in ("name", "plain", "definition", "include", "exclude"))


def build_shortlist_index(inv, items, exclude_seqs=(), examples_per_leaf=12):
    """Leaf profile = taxonomy text (weighted) + a few real examples."""
    by_leaf = collections.defaultdict(list)
    for s, r in items.items():
        if s in exclude_seqs:
            continue
        by_leaf[r["path"]].append(r)
    docs = {}
    for n in inv["nodes"]:
        parts = [leaf_text(n)] * 3
        for r in by_leaf.get(n["id"], [])[:examples_per_leaf]:
            parts.append(item_text(r, ("label", "definition")))
        docs[n["id"]] = "\n".join(parts)
    return Tfidf(docs)


def shortlist(index, item, l1, k=8):
    ranked = [(p, s) for p, s in index.rank(item_text(item)) if p.startswith(l1 + ".")]
    return ranked[:k]


def score(pred, gold):
    """pred: seq -> path. gold: seq -> dict. Returns metrics over gold-agree items."""
    g = {s: v["path"] for s, v in gold.items() if v["agree"] and s in pred}
    n = len(g)
    exact = sum(pred[s] == g[s] for s in g) / n if n else 0
    l3 = sum(pred[s].rsplit(".", 1)[0] == g[s].rsplit(".", 1)[0] for s in g) / n if n else 0
    l2 = sum(pred[s].split(".")[:2] == g[s].split(".")[:2] for s in g) / n if n else 0
    per = collections.defaultdict(lambda: [0, 0])
    for s in g:
        per[g[s].split(".")[0]][0] += pred[s] == g[s]
        per[g[s].split(".")[0]][1] += 1
    macro = sum(v[0] / v[1] for v in per.values() if v[1]) / len([v for v in per.values() if v[1]])
    return {"n": n, "exact": exact, "l3": l3, "l2": l2, "macro_l1": macro,
            "per_l1": {k: {"acc": v[0] / v[1], "n": v[1]} for k, v in sorted(per.items())}}


def confusion(pred, gold, top=25):
    g = {s: v["path"] for s, v in gold.items() if v["agree"] and s in pred}
    c = collections.Counter((g[s], pred[s]) for s in g if pred[s] != g[s])
    return c.most_common(top)
