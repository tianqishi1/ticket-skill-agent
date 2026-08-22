"""agent/rag.py
================
故障知识库 RAG 链路（向量库 = Milvus；其余为轻量本地组件）。

链路
----
1. 加载 sample_data/knowledge_base/*.md，按 Markdown 标题语义分块
   （chunk_size=800, overlap=150），每块携带 source 文件名 + tenant_id 元数据；
2. Milvus 持久化向量库（嵌入 = sentence-transformers all-MiniLM-L6-v2，dim=384），
   支持 --rebuild-vector 重建；
3. 检索 Query 由工单解析实体（service/keywords/phenomenon）组装，而非原始用户输入；
4. 两路召回：rank_bm25 关键词 Top20 + Milvus 向量稠密 Top20；
5. RRF 融合排序；
6. sentence-transformers cross-encoder 精排 → Top5；
7. 相关性分数阈值 0.35，低于阈值标记 rag_low_score=True（知识库无高相关性匹配案例）。

工程边界
--------
- 租户隔离：所有 Milvus 检索/查询都带 tenant_id 过滤（tenant_id in ["shared", <请求租户>]，
  构建时统一写入 tenant_id="shared" 表示跨租户共享知识）；
- 降级：Milvus 不可用 / 向量库未构建时，retrieve() 直接返回 low_score 结果，不抛异常；
- 安全与授权：向量库构建（离线）直接读取知识库文件；运行时 Agent 仅在
  「用户授权 knowledge_base」后才执行检索，未授权直接跳过 RAG 链路；
- 所有返回片段携带 source 元数据，用于报告溯源，抑制大模型幻觉。
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

from .infra import get_milvus, log_call

# ---------------------------------------------------------------------------
# 路径常量
# 本文件位于 <repo_root>/langgraph_demo/agent/rag.py
# ---------------------------------------------------------------------------
LANGGRAPH_DEMO_DIR: Path = Path(__file__).resolve().parent.parent  # langgraph_demo/
REPO_ROOT: Path = LANGGRAPH_DEMO_DIR.parent                        # ticket-skill-agent/
KNOWLEDGE_BASE_DIR: Path = REPO_ROOT / "sample_data" / "knowledge_base"
COLLECTION_NAME = "ticket_kb"
EMBED_DIM = 384  # all-MiniLM-L6-v2 输出维度

# 模型与超参
EMBED_MODEL = "all-MiniLM-L6-v2"
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150
BM25_TOP = 20
VECTOR_TOP = 20
RERANK_TOP = 5
RRF_K = 60
SCORE_THRESHOLD = 0.35
# 构建时给所有共享知识库 chunk 打的租户标签（检索时与请求租户一起纳入过滤）
SHARED_TENANT = "shared"

# 模型单例（懒加载，避免 import 期拉起 torch）
_embedder = None
_cross_encoder = None


# ===========================================================================
# 1. Markdown 语义分块
# ===========================================================================
_HEADER_RE = re.compile(r"^(#{1,6})\s+\S")


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切分为 (section_title, section_text) 列表。"""
    sections: list[tuple[str, str]] = []
    cur_title, cur_lines = "", []
    for line in text.split("\n"):
        if _HEADER_RE.match(line):
            if cur_lines:
                sections.append((cur_title, "\n".join(cur_lines)))
            cur_title = line.strip()
            cur_lines = [line]
        else:
            cur_lines.append(line)
    if cur_lines:
        sections.append((cur_title, "\n".join(cur_lines)))
    return sections


def _mk_chunk(source: str, section: str, idx: int, text: str) -> dict:
    return {
        "id": f"{source}::{idx}",
        "text": text.strip(),
        "source": source,
        "section": section or "",
    }


def chunk_markdown(text: str, source: str) -> list[dict]:
    """按标题分段，段内按 CHUNK_SIZE 滑窗分块，带 CHUNK_OVERLAP 重叠。"""
    chunks: list[dict] = []
    idx = 0
    for section_title, section_text in _split_sections(text):
        if not section_text.strip():
            continue
        if len(section_text) <= CHUNK_SIZE:
            chunks.append(_mk_chunk(source, section_title, idx, section_text))
            idx += 1
            continue
        start = 0
        while start < len(section_text):
            end = min(start + CHUNK_SIZE, len(section_text))
            chunks.append(_mk_chunk(source, section_title, idx, section_text[start:end]))
            idx += 1
            if end >= len(section_text):
                break
            start = end - CHUNK_OVERLAP  # 重叠
    return chunks


# ===========================================================================
# 2. Milvus 向量库（手动计算嵌入，避免对内置 EF 的版本依赖）
# ===========================================================================
def _get_embedder():
    """懒加载 sentence-transformers 嵌入模型（单例）。"""
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    """计算归一化嵌入（cosine 相似度）。"""
    if not texts:
        return []
    vecs = _get_embedder().encode(
        texts, normalize_embeddings=True, convert_to_numpy=True
    )
    return vecs.tolist()


def _chunk_id(source: str, text: str) -> str:
    """由 source + 文本哈希生成稳定 id（两路召回统一，供 RRF 去重）。"""
    return f"{source}::{hashlib.md5((text or "")[:200].encode("utf-8")).hexdigest()[:8]}"


def _ensure_collection(client) -> bool:
    """幂等创建 Milvus collection（含 schema + 向量索引）。成功返回 True。"""
    try:
        if client.has_collection(COLLECTION_NAME):
            return True
        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, max_length=128, is_primary=True)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBED_DIM)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        schema.add_field("source", DataType.VARCHAR, max_length=256)
        schema.add_field("section", DataType.VARCHAR, max_length=256)
        schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="AUTOINDEX", metric_type="COSINE"
        )
        client.create_collection(
            collection_name=COLLECTION_NAME,
            schema=schema,
            index_params=index_params,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log_call("rag:create_collection", error=str(exc))
        return False


def vector_store_ready() -> bool:
    """向量库是否就绪（Milvus 可用 + collection 存在 + 有数据）。"""
    client = get_milvus()
    if client is None:
        return False
    try:
        if not client.has_collection(COLLECTION_NAME):
            return False
        return client.num_entities(COLLECTION_NAME) > 0
    except Exception:  # noqa: BLE001
        return False


def build_vector_store(
    rebuild: bool = False, verbose: bool = True, tenant_id: str = SHARED_TENANT
) -> int:
    """构建（或重建）知识库 Milvus 向量库，返回入库 chunk 数。"""
    client = get_milvus()
    if client is None:
        if verbose:
            print("[RAG] Milvus 不可用，向量库构建跳过（知识库检索将降级）。")
        return 0

    if not KNOWLEDGE_BASE_DIR.exists():
        if verbose:
            print(f"[RAG] 知识库目录不存在：{KNOWLEDGE_BASE_DIR}")
        return 0

    # 强制重建：先删旧 collection
    if rebuild:
        try:
            if client.has_collection(COLLECTION_NAME):
                client.drop_collection(COLLECTION_NAME)
        except Exception as exc:  # noqa: BLE001
            log_call("rag:drop_collection", error=str(exc))

    if not _ensure_collection(client):
        if verbose:
            print("[RAG] Milvus collection 创建失败，跳过构建。")
        return 0

    # 已存在且非强制重建：跳过
    if not rebuild and vector_store_ready():
        n = client.num_entities(COLLECTION_NAME)
        if verbose:
            print(f"[RAG] 向量库已存在（{n} entity），跳过构建。")
        return int(n)

    md_files = sorted(KNOWLEDGE_BASE_DIR.rglob("*.md"))
    if not md_files:
        if verbose:
            print("[RAG] 知识库无 .md 文件，跳过构建。")
        return 0

    if verbose:
        print(f"[RAG] 开始构建向量库：{len(md_files)} 个知识库文件 → 嵌入中（首次会下载模型）...")

    all_chunks: list[dict] = []
    for fp in md_files:
        source = str(fp.relative_to(KNOWLEDGE_BASE_DIR)).replace("\\", "/")
        text = fp.read_text(encoding="utf-8")
        all_chunks.extend(chunk_markdown(text, source))

    if not all_chunks:
        return 0

    # 分批嵌入并 upsert，避免一次性内存峰值
    batch = 64
    inserted = 0
    for i in range(0, len(all_chunks), batch):
        batch_chunks = all_chunks[i:i + batch]
        vecs = _embed([c["text"] for c in batch_chunks])
        data = [
            {
                "id": _chunk_id(c["source"], c["text"]),
                "vector": vecs[j],
                "text": c["text"][:8192],
                "source": c["source"],
                "section": c["section"],
                "tenant_id": tenant_id,
            }
            for j, c in enumerate(batch_chunks)
        ]
        client.upsert(collection_name=COLLECTION_NAME, data=data)
        inserted += len(data)
    if verbose:
        print(f"[RAG] 向量库构建完成：{len(md_files)} 文件 / {inserted} chunk → Milvus[{COLLECTION_NAME}]")
    return inserted


# ===========================================================================
# 3. 召回：BM25 + 向量
# ===========================================================================
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")


def _tokenize(text: str) -> list[str]:
    """简易中英文分词：中文按字、英文按词（BM25 不依赖分词库即可工作）。"""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _tenant_filter(tenant_id: str) -> str:
    """构造 Milvus 过滤表达式：共享知识 + 当前租户。"""
    t = (tenant_id or SHARED_TENANT).replace('"', "").strip() or SHARED_TENANT
    return f'tenant_id in ["{SHARED_TENANT}", "{t}"]'


def _all_chunks_from_store(tenant_id: str) -> list[dict]:
    """从 Milvus 取回全部 chunk（供 BM25 建索引，带租户过滤）。"""
    client = get_milvus()
    if client is None:
        return []
    try:
        rows = client.query(
            collection_name=COLLECTION_NAME,
            filter=_tenant_filter(tenant_id),
            output_fields=["text", "source", "section"],
            limit=10000,
        )
    except Exception as exc:  # noqa: BLE001
        log_call("rag:query_all", tenant_id=tenant_id, error=str(exc))
        return []
    out: list[dict] = []
    for row in rows or []:
        text = row.get("text", "")
        out.append({
            "id": _chunk_id(row.get("source", ""), text),
            "text": text,
            "source": row.get("source", ""),
            "section": row.get("section", ""),
        })
    return out


def _bm25_recall(query: str, tenant_id: str, k: int = BM25_TOP) -> list[dict]:
    """rank_bm25 关键词召回 Top-K。"""
    from rank_bm25 import BM25Okapi
    chunks = _all_chunks_from_store(tenant_id)
    if not chunks:
        return []
    corpus = [_tokenize(c["text"]) for c in chunks]
    bm = BM25Okapi(corpus)
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    scores = bm.get_scores(q_tokens)
    ranked = sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)[:k]
    for i, (c, _s) in enumerate(ranked):
        c["bm25_rank"] = i + 1
    return [c for c, _ in ranked]


def _vector_recall(query: str, tenant_id: str, k: int = VECTOR_TOP) -> list[dict]:
    """Milvus 向量稠密召回 Top-K（手动计算 query 嵌入）。"""
    client = get_milvus()
    if client is None:
        return []
    q_emb = _embed([query])
    try:
        res = client.search(
            collection_name=COLLECTION_NAME,
            data=[q_emb[0]] if q_emb else [[]],
            filter=_tenant_filter(tenant_id),
            limit=k,
            output_fields=["text", "source", "section"],
        )
    except Exception as exc:  # noqa: BLE001
        log_call("rag:vector_search", tenant_id=tenant_id, error=str(exc))
        return []
    out: list[dict] = []
    hits = (res or [[]])[0] if res else []
    for i, hit in enumerate(hits):
        entity = hit.get("entity", hit) if isinstance(hit, dict) else {}
        text = entity.get("text", "")
        out.append({
            "id": _chunk_id(entity.get("source", ""), text),
            "text": text,
            "source": entity.get("source", ""),
            "section": entity.get("section", ""),
            "vector_rank": i + 1,
        })
    return out


# ===========================================================================
# 4. RRF 融合
# ===========================================================================
def _rrf_fuse(lists: list[list[dict]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion：对多路有序结果做 1/(k+rank) 加权融合。"""
    scores: dict[str, float] = {}
    for lst in lists:
        for rank, item in enumerate(lst, start=1):
            scores[item["id"]] = scores.get(item["id"], 0.0) + 1.0 / (k + rank)
    return scores


# ===========================================================================
# 5. Cross-encoder 精排
# ===========================================================================
def _get_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        from sentence_transformers import CrossEncoder
        _cross_encoder = CrossEncoder(CROSS_ENCODER_MODEL)
    return _cross_encoder


def _rerank(query: str, candidates: list[dict], top: int = RERANK_TOP) -> list[dict]:
    """cross-encoder 对候选 (query, doc) 打分，sigmoid 归一化后取 Top。"""
    if not candidates:
        return []
    ce = _get_cross_encoder()
    raw = ce.predict([(query, c["text"]) for c in candidates])
    scored: list[dict] = []
    for c, r in zip(candidates, raw):
        s = 1.0 / (1.0 + math.exp(-float(r)))  # sigmoid → 0~1 相关性
        c2 = dict(c)
        c2["score"] = round(s, 4)
        scored.append(c2)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]


# ===========================================================================
# 6. 对外检索 API
# ===========================================================================
def retrieve(query: str, tenant_id: str = SHARED_TENANT) -> dict[str, Any]:
    """两路召回 + RRF + cross-encoder 精排，返回 Top5 片段与 low_score 标记。

    返回结构：
        {
          "query": "...",
          "chunks": [{"id","text","source","section","score"}, ...],  # Top5
          "low_score": bool,   # best_score < SCORE_THRESHOLD
          "best_score": float,
          "reason": "..."       # 失败/降级原因（可选）
        }
    """
    if not query or not query.strip():
        return {"query": query, "chunks": [], "low_score": True, "best_score": 0.0,
                "reason": "查询为空"}
    if get_milvus() is None:
        return {"query": query, "chunks": [], "low_score": True, "best_score": 0.0,
                "reason": "Milvus 不可用"}
    if not vector_store_ready():
        return {"query": query, "chunks": [], "low_score": True, "best_score": 0.0,
                "reason": "向量库未构建"}

    try:
        bm = _bm25_recall(query, tenant_id, BM25_TOP)
        vec = _vector_recall(query, tenant_id, VECTOR_TOP)
    except Exception as exc:  # noqa: BLE001
        log_call("rag:recall", tenant_id=tenant_id, input_params={"query": query}, error=str(exc))
        return {"query": query, "chunks": [], "low_score": True, "best_score": 0.0,
                "reason": f"召回失败：{exc}"}

    # 候选 = 两路并集（去重，保留 text/source/section）
    rrf = _rrf_fuse([bm, vec])
    cand_map: dict[str, dict] = {}
    for c in bm + vec:
        base = cand_map.setdefault(c["id"], {})
        for kk, vv in c.items():
            if kk not in ("bm25_rank", "vector_rank"):
                base.setdefault(kk, vv)
    candidates = list(cand_map.values())
    # 用 RRF 分数初排序，便于精排
    candidates.sort(key=lambda c: rrf.get(c["id"], 0.0), reverse=True)

    reranked = _rerank(query, candidates, RERANK_TOP)
    best = reranked[0]["score"] if reranked else 0.0
    low = best < SCORE_THRESHOLD
    return {
        "query": query,
        "chunks": reranked,
        "low_score": low,
        "best_score": round(best, 4),
    }


def build_rag_query(parsed_ticket: dict) -> str:
    """由工单解析实体组装检索 query（非原始用户输入）。"""
    if not parsed_ticket:
        return ""
    parts: list[str] = []
    for key in ("service", "keywords", "phenomenon", "reproduce"):
        v = parsed_ticket.get(key)
        if v and isinstance(v, str) and v != "用户未提供":
            parts.append(v)
    return " ".join(parts).strip()
