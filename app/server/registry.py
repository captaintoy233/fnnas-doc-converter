"""
增量同步注册表 (registry.json)

记录每个源文件的 SHA-256 指纹、转换产物与推送状态，
实现增量转换/推送：文件未变化则跳过，变化则重新转换并推送。
写入策略：优先临时文件+原子替换；Docker 单文件 bind mount
不支持 os.replace 时回退为直写（与线上 docconverter.json 行为一致）。
"""
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path


class SyncRegistry:
    """基于 JSON 文件的增量注册表（线程安全）"""

    def __init__(self, path: str = "/data/registry.json"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        # 主文件损坏时回退 .bak，避免一次写坏导致全量重转
        for candidate in (self.path, self.path.with_name(self.path.name + ".bak")):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                continue
        return {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(tmp, self.path)
            except OSError:
                # 单文件 bind mount 场景：直写前先备份旧文件，直写中途崩溃可回退
                bak = self.path.with_name(self.path.name + ".bak")
                if self.path.exists():
                    try:
                        shutil.copyfile(self.path, bak)
                    except OSError:
                        pass
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    tmp.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    @staticmethod
    def sha256_of_file(file_path: str) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def sha256_of_text(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def get(self, file_path: str) -> dict:
        with self._lock:
            return dict(self._data.get(str(file_path), {}))

    def needs_convert(self, file_path: str, source_hash: str = None) -> bool:
        """是否需要（重新）转换：无记录或源文件指纹变化"""
        rec = self.get(file_path)
        if not rec:
            return True
        if source_hash is None:
            source_hash = self.sha256_of_file(file_path)
        return rec.get("source_hash") != source_hash

    def record(self, file_path: str, source_hash: str, output_rel: str = "",
               converted_hash: str = "", status: str = "converted",
               pushed: bool = False, weknora_doc_id: str = "",
               error: str = "", output_files: list = None):
        """记录一次转换结果"""
        out_files = output_files if output_files else ([output_rel] if output_rel else [])
        rec = {
            "source_hash": source_hash,
            "output_rel": output_rel,
            "output_files": out_files,
            "converted_hash": converted_hash,
            "status": status,
            "pushed": pushed,
            "weknora_doc_id": weknora_doc_id,
            "output_docs": {},          # {输出相对路径: WeKnora doc_id}（更新语义用）
            "error": error,
            "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # 保留旧 output_docs（增量更新不丢失已推送文档的 doc_id）
        old = self._data.get(str(file_path), {})
        if isinstance(old.get("output_docs"), dict):
            rec["output_docs"] = old["output_docs"]
        with self._lock:
            self._data[str(file_path)] = rec
            self._save()

    def mark_pushed(self, file_path: str, weknora_doc_id: str = ""):
        with self._lock:
            rec = self._data.get(str(file_path))
            if rec:
                rec["pushed"] = True
                rec["weknora_doc_id"] = weknora_doc_id
                rec["pushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._save()

    def record_output_doc(self, file_path: str, output_rel: str, doc_id: str):
        """记录某个输出文件对应的 WeKnora doc_id（用于更新时删除旧文档）"""
        with self._lock:
            rec = self._data.get(str(file_path))
            if rec:
                rec.setdefault("output_docs", {})[output_rel] = doc_id
                rec["pushed"] = True
                rec["pushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._save()

    def delete(self, file_path: str):
        with self._lock:
            self._data.pop(str(file_path), None)
            self._save()

    def all_files(self) -> set:
        with self._lock:
            return set(self._data.keys())

    def prune(self, current_files: set) -> list:
        """删除注册表中已不存在的源文件记录，返回被清理的路径"""
        removed = []
        with self._lock:
            for key in list(self._data.keys()):
                if key not in current_files:
                    removed.append(key)
                    self._data.pop(key, None)
            if removed:
                self._save()
        return removed

    def to_dict(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))


# ── SQLite 后端（可选，registry.backend=sqlite）──
# 相对路径键：源目录挂载点变化不失效；事务写入崩溃安全。
# 表结构与生产版 registry.sqlite 对齐并扩展 output_files/output_docs。


class SqliteSyncRegistry:
    """基于 SQLite 的增量注册表（与 SyncRegistry 同接口）"""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS registry (
        file_path       TEXT PRIMARY KEY,
        source_hash     TEXT,
        output_rel      TEXT,
        output_files    TEXT,
        converted_hash  TEXT,
        status          TEXT,
        pushed          INTEGER DEFAULT 0,
        weknora_doc_id  TEXT,
        output_docs     TEXT,
        error           TEXT,
        converted_at    TEXT,
        pushed_at       TEXT
    )"""

    def __init__(self, db_path: str = "/data/registry.sqlite", source_dir: str = ""):
        self.path = Path(db_path)
        self._lock = threading.Lock()
        self._src = Path(source_dir).resolve() if source_dir else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            conn.execute(self._SCHEMA)
            # 旧版（生产 v2.4.1）表只有 5 列：file_path/source_hash/converted_hash/
            # last_sync_time/weknora_doc_id → 自动迁移补列，可直接读旧库
            existing = {r[1] for r in conn.execute("PRAGMA table_info(registry)").fetchall()}
            col_types = {
                "output_rel": "TEXT", "output_files": "TEXT",
                "converted_hash": "TEXT", "status": "TEXT",
                "pushed": "INTEGER DEFAULT 0", "weknora_doc_id": "TEXT",
                "output_docs": "TEXT", "error": "TEXT",
                "converted_at": "TEXT", "pushed_at": "TEXT",
            }
            for col, ctype in col_types.items():
                if col not in existing:
                    conn.execute("ALTER TABLE registry ADD COLUMN {} {}".format(col, ctype))
            conn.commit()
        finally:
            conn.close()

    def _key(self, file_path: str) -> str:
        """绝对路径 → 源目录相对路径（不在源目录下则保留绝对路径）"""
        try:
            p = Path(file_path)
            if self._src:
                rel = p.resolve().relative_to(self._src)
                return str(rel).replace("\\", "/")
        except ValueError:
            pass
        return str(file_path).replace("\\", "/")

    def _conn(self):
        return sqlite3.connect(str(self.path), timeout=30)

    _COLS = ("file_path", "source_hash", "output_rel", "output_files",
             "converted_hash", "status", "pushed", "weknora_doc_id",
             "output_docs", "error", "converted_at", "pushed_at")
    _COL_SQL = ", ".join(_COLS)

    def _row_to_dict(self, row) -> dict:
        if not row:
            return {}
        d = dict(zip(self._COLS, row))
        try:
            d["output_files"] = json.loads(d.get("output_files") or "[]")
        except Exception:
            d["output_files"] = []
        try:
            d["output_docs"] = json.loads(d.get("output_docs") or "{}")
        except Exception:
            d["output_docs"] = {}
        d["pushed"] = bool(d.get("pushed"))
        return d

    def get(self, file_path: str) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT {} FROM registry WHERE file_path = ?".format(self._COL_SQL),
                    (self._key(file_path),)).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)

    def needs_convert(self, file_path: str, source_hash: str = None) -> bool:
        rec = self.get(file_path)
        if not rec:
            return True
        if source_hash is None:
            source_hash = self.sha256_of_file(file_path)
        return rec.get("source_hash") != source_hash

    @staticmethod
    def sha256_of_file(file_path: str) -> str:
        return SyncRegistry.sha256_of_file(file_path)

    @staticmethod
    def sha256_of_text(content: str) -> str:
        return SyncRegistry.sha256_of_text(content)

    def record(self, file_path: str, source_hash: str, output_rel: str = "",
               converted_hash: str = "", status: str = "converted",
               pushed: bool = False, weknora_doc_id: str = "",
               error: str = "", output_files: list = None):
        out_files = output_files if output_files else ([output_rel] if output_rel else [])
        # 保留旧 output_docs（增量更新不丢失已推送文档的 doc_id）
        old = self.get(file_path)
        old_docs = old.get("output_docs") or {}
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO registry (file_path, source_hash, output_rel, output_files,
                       converted_hash, status, pushed, weknora_doc_id, output_docs,
                       error, converted_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(file_path) DO UPDATE SET
                         source_hash=excluded.source_hash,
                         output_rel=excluded.output_rel,
                         output_files=excluded.output_files,
                         converted_hash=excluded.converted_hash,
                         status=excluded.status,
                         pushed=excluded.pushed,
                         weknora_doc_id=excluded.weknora_doc_id,
                         output_docs=excluded.output_docs,
                         error=excluded.error,
                         converted_at=excluded.converted_at""",
                    (self._key(file_path), source_hash, output_rel,
                     json.dumps(out_files, ensure_ascii=False),
                     converted_hash, status, 1 if pushed else 0,
                     weknora_doc_id,
                     json.dumps(old_docs, ensure_ascii=False),
                     error, time.strftime("%Y-%m-%dT%H:%M:%S")))
                conn.commit()
            finally:
                conn.close()

    def mark_pushed(self, file_path: str, weknora_doc_id: str = ""):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE registry SET pushed=1, weknora_doc_id=?, pushed_at=? "
                    "WHERE file_path=?",
                    (weknora_doc_id, time.strftime("%Y-%m-%dT%H:%M:%S"),
                     self._key(file_path)))
                conn.commit()
            finally:
                conn.close()

    def record_output_doc(self, file_path: str, output_rel: str, doc_id: str):
        rec = self.get(file_path)
        docs = rec.get("output_docs") or {}
        docs[output_rel] = doc_id
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE registry SET output_docs=?, pushed=1, pushed_at=? "
                    "WHERE file_path=?",
                    (json.dumps(docs, ensure_ascii=False),
                     time.strftime("%Y-%m-%dT%H:%M:%S"), self._key(file_path)))
                conn.commit()
            finally:
                conn.close()

    def delete(self, file_path: str):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM registry WHERE file_path = ?",
                             (self._key(file_path),))
                conn.commit()
            finally:
                conn.close()

    def all_files(self) -> set:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT file_path FROM registry").fetchall()
            finally:
                conn.close()
        return {r[0] for r in rows}

    def prune(self, current_files: set) -> list:
        removed = []
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT file_path FROM registry").fetchall()
                for (key,) in rows:
                    if key not in current_files:
                        removed.append(key)
                        conn.execute("DELETE FROM registry WHERE file_path = ?", (key,))
                if removed:
                    conn.commit()
            finally:
                conn.close()
        return removed

    def to_dict(self) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT {} FROM registry".format(self._COL_SQL)).fetchall()
            finally:
                conn.close()
        return {r[0]: self._row_to_dict(r) for r in rows}


# 全局注册表实例（懒加载）
_registry = None
_registry_lock = threading.Lock()


def get_registry():
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                from config import get_config
                cfg = get_config()
                rcfg = cfg.get("registry", {}) or {}
                if (rcfg.get("backend") or "json") == "sqlite":
                    _registry = SqliteSyncRegistry(
                        rcfg.get("sqlite_path", "/data/registry.sqlite"),
                        source_dir=(cfg.get("scanner", {}) or {}).get("source_dir", ""))
                else:
                    _registry = SyncRegistry(rcfg.get("path", "/data/registry.json"))
    return _registry


def reset_registry():
    """测试用：重置全局实例"""
    global _registry
    _registry = None
