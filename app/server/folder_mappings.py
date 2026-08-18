"""
文件夹 → WeKnora 知识库映射管理（合并自生产环境 v2.4.1）

将源目录下的不同子目录映射到不同的 WeKnora 知识库，
实现"目录结构 → 知识库路由"。映射关系存储于 JSON 文件:
{
  "mappings": {
    "工作邮件": "de2e0345-6b1e-43e3-ad42-73cb35a9c068",
    "报表数据": "xxx",
    "政策制度/近期新制度": "kb-0002"
  },
  "updated_at": "2026-08-03T17:00:00"
}

路径可通过配置 weknora.folder_kb_map_path 指定（默认 /app/config/folder_mappings.json）。
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("docconverter.mappings")

_lock = threading.Lock()

DEFAULT_PATH = Path("/app/config/folder_mappings.json")


def _resolve_path(db_path=None) -> Path:
    if db_path:
        return Path(db_path)
    try:
        from config import get_config
        cfg_path = get_config().get("weknora", {}).get("folder_kb_map_path", "")
        if cfg_path:
            return Path(cfg_path)
    except Exception:
        pass
    return DEFAULT_PATH


def _load(db_path=None) -> dict:
    path = _resolve_path(db_path)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("mappings"), dict):
                return data
    except Exception as e:
        logger.error("映射文件加载失败: %s", e)
    return {"mappings": {}, "updated_at": ""}


def get_mappings(db_path=None) -> dict:
    """返回 {folder: kb_id} 映射"""
    return _load(db_path).get("mappings", {})


def save_mappings(mappings: dict, db_path=None) -> dict:
    """保存映射 (整体覆盖)"""
    path = _resolve_path(db_path)
    data = {
        "mappings": mappings or {},
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            try:
                tmp.replace(path)
            except OSError:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                try:
                    tmp.unlink()
                except OSError:
                    pass
        logger.info("映射已保存: %d 条", len(data["mappings"]))
        return data
    except Exception as e:
        logger.error("映射保存失败: %s", e)
        return None


def resolve_kb_id(rel_path: str, mappings: dict, default_kb: str = "") -> str:
    """根据文件相对路径解析目标知识库 ID

    匹配规则（按最长前缀优先）:
      - 精确匹配: mappings["工作邮件/李明磊邮件"]
      - 一级目录: mappings["工作邮件"]
      - 默认: default_kb（通常是 dataset_id）
    """
    if not mappings:
        return default_kb

    # 收集路径的所有前缀（含文件所在目录链）
    parts = Path(rel_path).parts
    candidates = []
    for i in range(1, len(parts) + 1):
        candidates.append("/".join(parts[:i]))

    # 最长前缀优先
    for c in sorted(candidates, key=len, reverse=True):
        if c in mappings and mappings[c]:
            return mappings[c]
    return default_kb
