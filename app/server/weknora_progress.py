"""
weknora_progress.py — WeKnora 解析/嵌入/Wiki 进度聚合器
直连 postgres（与 doc-converter 同 Docker 网络）聚合 KB 级进度：
  - parse_status 分布 (pending/processing/finalizing/completed/failed)
  - chunks 计数 (text / parent_text / summary)
  - embeddings 计数 (嵌入目标 = text + summary)
  - wiki_pages 计数 (Wiki 生成进度)
  - 嵌入速率 (最近 60s / 5min 增量)
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

# postgres 连接参数（doc-converter 与 WeKnora 同 Docker 网络，容器名直连）
PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
PG_DB = os.environ.get("POSTGRES_DB", "weknora")
PG_USER = os.environ.get("POSTGRES_USER", "postgres")
PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres123")

# 嵌入目标 chunk 类型（源码实锤：parent_text 不嵌入，只嵌 text + summary）
EMBED_TARGET_TYPES = ("text", "summary")

_conn = None
_conn_ts = 0


def _get_conn():
    """获取 postgres 连接（带 5 分钟自动重连）"""
    global _conn, _conn_ts
    import psycopg2
    now = time.time()
    if _conn is None or (now - _conn_ts > 300):
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASSWORD, connect_timeout=5,
        )
        _conn_ts = now
    return _conn


def _q(cur, sql, params=None):
    """执行查询返回全部行"""
    cur.execute(sql, params or ())
    return cur.fetchall()


def _q1(cur, sql, params=None):
    """执行查询返回单值"""
    rows = _q(cur, sql, params)
    return rows[0][0] if rows else 0


def get_kb_progress(kb_id: str) -> dict:
    """聚合单个知识库的解析/嵌入/Wiki 进度"""
    if not kb_id:
        return {"ok": False, "error": "kb_id 不能为空"}
    try:
        conn = _get_conn()
        cur = conn.cursor()
    except Exception as e:
        logger.error("postgres 连接失败: %s", e)
        return {"ok": False, "error": "postgres 连接失败: {}".format(e)}

    try:
        # 1. 知识库元信息
        kb_row = _q(cur, """
            SELECT id, name, embedding_model_id,
                   indexing_strategy->>'wiki_enabled' AS wiki_enabled
            FROM knowledge_bases WHERE id = %s
        """, (kb_id,))
        if not kb_row:
            return {"ok": False, "error": "知识库不存在: {}".format(kb_id)}
        kb = {
            "id": kb_row[0][0],
            "name": kb_row[0][1],
            "embedding_model_id": kb_row[0][2] or "",
            "wiki_enabled": (kb_row[0][3] or "").lower() == "true",
        }

        # 2. 文档解析状态分布
        parse_rows = _q(cur, """
            SELECT parse_status, count(*) FROM knowledges
            WHERE knowledge_base_id = %s AND deleted_at IS NULL
            GROUP BY parse_status
        """, (kb_id,))
        parse_status = {r[0]: r[1] for r in parse_rows}
        total_docs = sum(parse_status.values())

        # 3. enable_status 分布（disabled 文档 worker 会跳过）
        en_rows = _q(cur, """
            SELECT enable_status, count(*) FROM knowledges
            WHERE knowledge_base_id = %s AND deleted_at IS NULL
            GROUP BY enable_status
        """, (kb_id,))
        enable_status = {r[0]: r[1] for r in en_rows}

        # 4. chunks 计数（按类型）— ⚠️ 必须过滤 c.deleted_at IS NULL（软删旧批次）
        #    reparse 会生成新 chunks 并软删旧批次，不过滤会把 30 万条死数据算进分母 → 百分比失真
        chunk_rows = _q(cur, """
            SELECT c.chunk_type, count(*) FROM chunks c
            JOIN knowledges k ON c.knowledge_id = k.id
            WHERE k.knowledge_base_id = %s AND k.deleted_at IS NULL
              AND c.deleted_at IS NULL
            GROUP BY c.chunk_type
        """, (kb_id,))
        chunks_by_type = {r[0]: r[1] for r in chunk_rows}
        total_chunks = sum(chunks_by_type.values())
        embed_target = sum(chunks_by_type.get(t, 0) for t in EMBED_TARGET_TYPES)

        # 5. embeddings 计数（准确口径：IN 子查询，避免 JOIN 孤儿低估）
        total_embeddings = _q1(cur, """
            SELECT count(*) FROM embeddings
            WHERE knowledge_id IN (
                SELECT id FROM knowledges
                WHERE knowledge_base_id = %s AND deleted_at IS NULL
            )
        """, (kb_id,))

        # 6. 嵌入速率（60s / 300s 增量）
        emb_1m = _q1(cur, """
            SELECT count(*) FROM embeddings
            WHERE knowledge_id IN (
                SELECT id FROM knowledges
                WHERE knowledge_base_id = %s AND deleted_at IS NULL
            ) AND created_at > now() - interval '60 seconds'
        """, (kb_id,))
        emb_5m = _q1(cur, """
            SELECT count(*) FROM embeddings
            WHERE knowledge_id IN (
                SELECT id FROM knowledges
                WHERE knowledge_base_id = %s AND deleted_at IS NULL
            ) AND created_at > now() - interval '300 seconds'
        """, (kb_id,))

        # 7. Wiki 进度（wiki_pages 表）
        wiki_total = 0
        wiki_completed = 0
        try:
            wiki_total = _q1(cur, """
                SELECT count(*) FROM wiki_pages
                WHERE knowledge_base_id = %s
            """, (kb_id,))
            wiki_completed = _q1(cur, """
                SELECT count(*) FROM wiki_pages
                WHERE knowledge_base_id = %s AND status IN ('completed', 'active', 'published')
            """, (kb_id,))
        except Exception as e:
            logger.warning("wiki_pages 查询失败（表可能不存在）: %s", e)

        # 8. 最近文档更新时间（判断流水线是否活着）
        last_update = _q1(cur, """
            SELECT max(updated_at)::text FROM knowledges
            WHERE knowledge_base_id = %s AND deleted_at IS NULL
        """, (kb_id,))

        conn.rollback()
        return {
            "ok": True,
            "kb": kb,
            "documents": {
                "total": total_docs,
                "parse_status": parse_status,
                "enable_status": enable_status,
                "last_update": last_update,
            },
            "chunks": {
                "total": total_chunks,
                "by_type": chunks_by_type,
                "embed_target": embed_target,
            },
            "embeddings": {
                "total": total_embeddings,
                "target": embed_target,
                "pct": round(total_embeddings * 100.0 / embed_target, 1) if embed_target else 0.0,
                "rate_1m": emb_1m,
                "rate_5m": emb_5m,
            },
            "wiki": {
                "total": wiki_total,
                "completed": wiki_completed,
                "pct": round(wiki_completed * 100.0 / wiki_total, 1) if wiki_total else 0.0,
            },
            "ts": time.time(),
        }
    except Exception as e:
        logger.error("聚合进度失败: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": "聚合进度失败: {}".format(str(e))}


def get_all_kbs_progress() -> dict:
    """聚合所有知识库的进度（列表用）"""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        rows = _q(cur, "SELECT id, name FROM knowledge_bases ORDER BY name")
        conn.rollback()
    except Exception as e:
        logger.error("postgres 连接失败: %s", e)
        return {"ok": False, "error": str(e)}
    out = []
    for kb_id, name in rows:
        prog = get_kb_progress(kb_id)
        if prog.get("ok"):
            out.append({
                "id": kb_id,
                "name": name,
                "documents_total": prog["documents"]["total"],
                "completed": prog["documents"]["parse_status"].get("completed", 0),
                "embeddings_pct": prog["embeddings"]["pct"],
                "embedding_total": prog["embeddings"]["total"],
                "embedding_target": prog["embeddings"]["target"],
                "wiki_pct": prog["wiki"]["pct"],
            })
    return {"ok": True, "knowledge_bases": out, "ts": time.time()}
