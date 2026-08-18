"""
WeKnora API 客户端: 登录认证(JWT) / API Key 双模式 + 文档推送

对接的 WeKnora API（Tencent/WeKnora 源码 internal/handler 核对）:
- POST /api/v1/auth/login                      -> { success, token, refresh_token, tenant{...} }
- GET  /api/v1/knowledge-bases                 -> { success, data: [ {id, name, ...} ] }
- POST /api/v1/knowledge-bases/{id}/knowledge/manual -> { title, content, status, tag_ids, channel }
- POST /api/v1/knowledge-bases/{id}/knowledge/file   -> multipart { file, fileName, metadata, channel, ... }
  - fileName 支持相对路径（如 政策制度/2024/xxx.md）→ WeKnora 自动生成文件夹结构
  - 相同 (fileName+fileSize+fileHash) 重复上传返回 409 duplicate_file，视为幂等成功
  - 文件大小上限 MAX_FILE_SIZE_MB（默认 50MB）
  - 需要知识库 Admin/Editor 权限

认证优先级: 配置了 api_key 则使用 X-API-Key 头（官方中间件支持）；
否则用 email/password 登录拿 JWT。api_url 兼容带/不带 /api/v1 后缀。
"""
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import requests

from config import get_config

logger = logging.getLogger("docconverter.weknora")

# 与官方 MAX_FILE_SIZE_MB 默认一致（可通过配置 weknora.max_file_size_mb 覆盖）
DEFAULT_MAX_FILE_SIZE_MB = 50

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_filename(name: str) -> str:
    """对齐官方 secutils.ValidateInput：去除控制字符、修剪空白、保证 UTF-8"""
    if not name:
        return ""
    name = _CONTROL_CHARS.sub("", name)
    name = name.strip()
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        name = name.encode("utf-8", errors="replace").decode("utf-8")
    return name


class WeKnoraClient:
    """WeKnora 知识库 API 客户端"""

    def __init__(self, config: dict = None):
        if config is None:
            config = get_config()
        wk = config.get("weknora", {})
        raw_url = (wk.get("api_url") or "").strip().rstrip("/")
        # 兼容配置里带 /api/v1 后缀的写法
        if raw_url.endswith("/api/v1"):
            raw_url = raw_url[: -len("/api/v1")]
        self.api_url = raw_url
        self.api_key = (wk.get("api_key") or "").strip()
        self.dataset_id = (wk.get("dataset_id") or "").strip()
        self.enabled = wk.get("enabled", False)
        self.auto_push = wk.get("auto_push", True)
        self.push_original_on_missing = wk.get("push_original_on_missing", True)
        self.push_as_file = wk.get("push_as_file", True)
        self.email = (wk.get("email") or "").strip()
        self.password = wk.get("password", "")
        self.retry_count = int(wk.get("retry_count", 3) or 3)
        self.tenant_id = str(wk.get("tenant_id", "") or "").strip()
        self.max_file_size = int(wk.get("max_file_size_mb",
                                        DEFAULT_MAX_FILE_SIZE_MB) or DEFAULT_MAX_FILE_SIZE_MB) * 1024 * 1024
        self._token = None
        self._token_expiry = 0
        self._kb_id = None
        self._kb_name = None
        self._kb_error = ""

    # ── 基础 ──

    def is_configured(self) -> bool:
        return bool(self.enabled and self._url_ok()
                    and (self.api_key or (self.email and self.password)))

    def _url_ok(self) -> bool:
        """校验 api_url：必须是 http(s) 且非空（防 SSRF/拼写错误）"""
        if not self.api_url:
            return False
        if not (self.api_url.startswith("http://") or self.api_url.startswith("https://")):
            return False
        return True

    def _url_error(self) -> str:
        if not self.api_url:
            return "未配置 WeKnora URL"
        if not self._url_ok():
            return ("WeKnora URL 配置无效（需以 http:// 或 https:// 开头）: {}".format(
                self.api_url))
        return ""

    def _auth_headers(self) -> dict:
        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        elif self._token and time.time() < self._token_expiry:
            headers["Authorization"] = "Bearer {}".format(self._token)
        # 平台级(platform) API Key 需要 X-Tenant-ID 指定工作空间（官方 mw_auth.go）
        if self.tenant_id and not headers.get("X-API-Key"):
            headers["X-Tenant-ID"] = self.tenant_id
        return headers

    def _ensure_auth(self):
        """确保有可用的认证（登录或 API Key）"""
        if self.api_key:
            return
        if not self._token or time.time() > self._token_expiry:
            self._login()

    # ── 认证 ──

    def _login(self):
        """登录 WeKnora 获取 JWT token"""
        try:
            resp = requests.post(
                "{}/api/v1/auth/login".format(self.api_url),
                json={"email": self.email, "password": self.password},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") or (data.get("data") or {}).get("token")
                if token:
                    self._token = token
                    self._token_expiry = time.time() + 3300  # 略小于 1 小时
                    # 注意: 官方登录 DTO 无 tenant.api_key 字段（auth_dto.go），
                    # 仅用户的 fork 会注入；此处为尽力拾取，取不到则继续用 JWT
                    tenant = data.get("tenant") or data.get("active_tenant") or {}
                    if tenant.get("api_key") and not self.api_key:
                        self.api_key = tenant["api_key"]
                    logger.info("WeKnora login successful")
                    return
            logger.error("WeKnora login failed: HTTP %s %s",
                         resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("WeKnora login failed: %s", e)
        self._token = None

    # ── API 调用 ──

    def _api_call(self, method: str, path: str, data=None, files=None,
                  params=None, timeout: int = 60, retry: int = 0) -> dict:
        """执行 API 调用，5xx/网络错误自动重试（指数退避）"""
        url = "{}{}".format(self.api_url, path)
        self._ensure_auth()
        headers = self._auth_headers()

        try:
            resp = requests.request(
                method, url, json=data, files=files, params=params,
                headers=headers, timeout=timeout,
            )
        except Exception as e:
            if retry < self.retry_count:
                time.sleep(min(2 ** (retry + 1), 10))
                return self._api_call(method, path, data, files, params, timeout, retry + 1)
            logger.error("WeKnora API error: %s", e)
            return {"ok": False, "error": str(e)}

        if resp.status_code == 401 and not self.api_key and retry < 1:
            # token 过期，重新登录后重试一次
            self._token = None
            return self._api_call(method, path, data, files, params, timeout, retry + 1)

        if resp.status_code >= 500 or resp.status_code == 429:
            if retry < self.retry_count:
                time.sleep(min(2 ** (retry + 1), 10))
                return self._api_call(method, path, data, files, params, timeout, retry + 1)

        if resp.status_code == 409:
            # 官方按 (fileName+fileSize+fileHash) 判重，重复上传返回 409 duplicate_*
            # 视为幂等成功：返回已有知识的 id
            try:
                body = resp.json()
            except Exception:
                body = {}
            code = body.get("code", "")
            if isinstance(code, str) and code.startswith("duplicate_"):
                existing = body.get("data") or {}
                doc_id = existing.get("id", "") if isinstance(existing, dict) else ""
                return {"ok": True, "doc_id": doc_id, "duplicate": True,
                        "status_code": 409, "message": body.get("message", "")}
            err = resp.text[:300]
            logger.error("WeKnora API %s %s: HTTP 409 %s", method, path, err)
            return {"ok": False, "error": "HTTP 409: {}".format(err),
                    "status_code": 409}

        if resp.status_code >= 400:
            err = resp.text[:300]
            hint = ""
            if resp.status_code == 403:
                hint = "（推送账号需为知识库的 Admin/Editor 角色）"
            elif resp.status_code == 404:
                hint = "（知识库不存在，检查 dataset_id）"
            logger.error("WeKnora API %s %s: HTTP %s %s", method, path, resp.status_code, err)
            return {"ok": False, "error": "HTTP {}: {}{}".format(resp.status_code, err, hint),
                    "status_code": resp.status_code}

        try:
            result = resp.json()
        except Exception:
            result = {"data": None}
        result.setdefault("ok", True)
        return result

    def _ensure_kb_id(self) -> bool:
        """解析知识库 ID（dataset_id 支持 ID 或名称），带缓存

        安全策略: dataset_id 未配置时，仅当空间只有一个知识库才自动使用；
        有多个知识库时不猜测，避免推送到错误的知识库。
        """
        if self._kb_id:
            return True
        result = self._api_call("GET", "/api/v1/knowledge-bases")
        if not result.get("ok"):
            return False
        kbs = result.get("data") or []
        if isinstance(kbs, dict):
            kbs = kbs.get("list") or kbs.get("items") or []
        if not kbs:
            return False
        self._kb_list = kbs
        if self.dataset_id:
            for kb in kbs:
                if str(kb.get("id")) == str(self.dataset_id) or kb.get("name") == self.dataset_id:
                    self._kb_id = kb["id"]
                    self._kb_name = kb.get("name")
                    return True
            self._kb_error = "未找到知识库: {}（检查 dataset_id 是否为 ID 或名称）".format(
                self.dataset_id)
            return False
        if len(kbs) == 1:
            self._kb_id = kbs[0].get("id")
            self._kb_name = kbs[0].get("name")
            return True
        self._kb_error = ("存在多个知识库，但未配置 dataset_id（知识库 ID 或名称），"
                          "为避免推送到错误的知识库，请在配置中指定")
        return False

    def _resolve_kb_id(self, kb_id: str = None) -> str:
        """解析目标知识库 ID（支持 ID 或名称；None 用默认 dataset_id 逻辑）"""
        if not kb_id:
            if not self._ensure_kb_id():
                return ""
            return self._kb_id
        kb_id = str(kb_id).strip()
        if not kb_id:
            if not self._ensure_kb_id():
                return ""
            return self._kb_id
        # 尝试按名称/ID 匹配（列表已加载时）
        if self._ensure_kb_list():
            for kb in self._kb_list:
                if kb.get("name") == kb_id or str(kb.get("id")) == kb_id:
                    return str(kb.get("id"))
            self._kb_error = "folder 映射的知识库不存在: {}（检查名称/ID）".format(kb_id)
            return ""
        # 列表不可用但含 ID 特征（UUID/短标识）时直接使用
        if "-" in kb_id or (kb_id.isalnum() and len(kb_id) <= 32):
            return kb_id
        return ""

    def _ensure_kb_list(self) -> bool:
        if getattr(self, "_kb_list", None):
            return True
        result = self._api_call("GET", "/api/v1/knowledge-bases")
        if not result.get("ok"):
            return False
        kbs = result.get("data") or []
        if isinstance(kbs, dict):
            kbs = kbs.get("list") or kbs.get("items") or []
        self._kb_list = kbs or []
        return bool(self._kb_list)

    # ── 对外接口 ──

    def test_connection(self) -> dict:
        """测试 WeKnora 连接"""
        url_err = self._url_error()
        if url_err:
            return {"ok": False, "error": url_err}
        if not self.api_key and (not self.email or not self.password):
            return {"ok": False, "error": "未配置 API Key 或登录凭据"}

        self._ensure_auth()
        if not self.api_key and not self._token:
            return {"ok": False, "error": "登录失败，请检查邮箱/密码"}

        found = self._ensure_kb_id()
        return {
            "ok": True,
            "kb_id": self._kb_id,
            "kb_name": self._kb_name,
            "auth": "api_key" if self.api_key else "jwt",
        } if found else {
            "ok": True,
            "warning": "已连接但未找到知识库（检查 dataset_id）",
        }

    def push_manual(self, title: str, content: str,
                    source_path: str = "", source_format: str = "",
                    metadata: dict = None, kb_id: str = None) -> dict:
        """推送纯 Markdown 文本到 WeKnora 知识库（manual 知识，无文件夹结构）

        注意: manual 接口不支持文件夹路径，若需要保留目录结构请用 push_markdown_file。
        Args:
            kb_id: 目标知识库 ID/名称（folder→KB 映射用；None 用 dataset_id）
        Returns:
            {"ok": True, "doc_id": "..."} or {"ok": False, "error": "..."}
        """
        if not self.is_configured():
            return {"ok": False, "error": "WeKnora 未配置"}
        target_kb = self._resolve_kb_id(kb_id)
        if not target_kb:
            return {"ok": False, "error": self._kb_error or "无法找到知识库"}

        # 生产经验: WeKnora manual 内容上限约 20 万字符，超限截断避免推送失败
        if content and len(content) > 195000:
            logger.warning("内容超长 %d 字符，截断至 195000: %s",
                           len(content), (title or "")[:50])
            content = content[:195000]

        body = {
            "title": sanitize_filename(title) or "未命名文档",
            "content": content,
            # 官方服务端只接受 draft / publish（见 knowledge_create.go 的校验），
            # "published" 会 400；publish 会立即触发解析
            "status": "publish",
            "channel": "docconverter",
        }
        result = self._api_call(
            "POST",
            "/api/v1/knowledge-bases/{}/knowledge/manual".format(target_kb),
            data=body,
            timeout=120,
        )
        out = self._extract_doc_id(result)
        if out.get("ok") and out.get("doc_id"):
            self._post_push_actions(out["doc_id"])
            out["kb_id"] = target_kb
        return out

    def push_markdown_file(self, title: str, content: str, rel_path: str,
                           metadata: dict = None, kb_id: str = None) -> dict:
        """将 Markdown 内容以文件方式推送到 WeKnora（保留目录结构）

        通过 fileName 携带相对路径（如 政策制度/2024/xxx.md），
        WeKnora 端按 / 拆分为文件夹 + 文件名（官方 SplitKnowledgeRelativePath）。
        相同 (fileName+size+hash) 重复推送返回 409，客户端按幂等成功处理。

        Args:
            title: 显示标题（通常为文件名）
            content: Markdown 正文
            rel_path: 相对路径（含 .md 后缀），决定 WeKnora 文件夹结构
            metadata: 附加元数据（如 source_hash）
            kb_id: 目标知识库 ID/名称（folder→KB 映射用；None 用 dataset_id）
        """
        if not self.is_configured():
            return {"ok": False, "error": "WeKnora 未配置"}
        target_kb = self._resolve_kb_id(kb_id)
        if not target_kb:
            return {"ok": False, "error": self._kb_error or "无法找到知识库"}

        rel_path = rel_path.replace("\\", "/").lstrip("/")
        filename = sanitize_filename(rel_path)
        if not filename:
            filename = (sanitize_filename(title) or "未命名.md") + ".md"
        if not filename.lower().endswith(".md"):
            filename += ".md"

        data_bytes = content.encode("utf-8")
        if len(data_bytes) > self.max_file_size:
            return {"ok": False,
                    "error": "内容超过 WeKnora 上传上限 {}MB (MAX_FILE_SIZE_MB / weknora.max_file_size_mb)".format(
                        self.max_file_size // 1024 // 1024)}

        form = {
            "file": (Path(filename).name, data_bytes, "text/markdown; charset=utf-8"),
            "fileName": filename,
            "channel": "docconverter",
        }
        if metadata:
            form["metadata"] = json.dumps(metadata, ensure_ascii=False)

        result = self._api_call(
            "POST",
            "/api/v1/knowledge-bases/{}/knowledge/file".format(target_kb),
            files=form,
            timeout=120,
        )
        out = self._extract_doc_id(result)
        if out.get("ok") and out.get("doc_id"):
            self._post_push_actions(out["doc_id"])
            out["kb_id"] = target_kb
        return out

    def push_file(self, file_path: str, title: str = "",
                  source_format: str = "", metadata: dict = None,
                  rel_path: str = "", kb_id: str = None) -> dict:
        """推送原始文件到 WeKnora 知识库（由 WeKnora 端解析）

        Args:
            file_path: 原始文件路径
            title: 文档标题 (留空用文件名)
            source_format: 源格式名称
            metadata: 附加元数据（如 source_hash）
            rel_path: 相对路径（保留目录结构）；空则用文件名

        Returns:
            {"ok": True, "doc_id": "..."} or {"ok": False, "error": "..."}
        """
        if not self.is_configured():
            return {"ok": False, "error": "WeKnora 未配置"}
        target_kb = self._resolve_kb_id(kb_id)
        if not target_kb:
            return {"ok": False, "error": self._kb_error or "无法找到知识库"}

        path = Path(file_path)
        if not path.exists():
            return {"ok": False, "error": "文件不存在: {}".format(file_path)}
        size = path.stat().st_size
        if size > self.max_file_size:
            return {"ok": False,
                    "error": "文件 {} 超过 WeKnora 上传上限 {}MB (MAX_FILE_SIZE_MB，"
                             "可在配置 weknora.max_file_size_mb 调大)".format(
                                 path.name, self.max_file_size // 1024 // 1024)}

        doc_title = title or path.stem
        if rel_path:
            filename = sanitize_filename(rel_path.replace("\\", "/").lstrip("/")) or path.name
        else:
            filename = sanitize_filename(path.name) or path.name

        form = {
            "file": (Path(filename).name, open(path, "rb"), self._mime_for(path)),
            "fileName": filename,
            "channel": "docconverter",
        }
        if metadata:
            form["metadata"] = json.dumps(metadata, ensure_ascii=False)

        try:
            result = self._api_call(
                "POST",
                "/api/v1/knowledge-bases/{}/knowledge/file".format(target_kb),
                files=form,
                timeout=300,
            )
        finally:
            form["file"][1].close()

        out = self._extract_doc_id(result)
        if out.get("ok") and out.get("doc_id"):
            self._post_push_actions(out["doc_id"])
            out["kb_id"] = target_kb
        return out

    @staticmethod
    def _extract_doc_id(result: dict) -> dict:
        """从 API 响应提取 doc_id（兼容成功/409 幂等重复两种形态）"""
        if not result.get("ok"):
            return result
        doc_id = result.get("doc_id", "")
        if not doc_id:
            data = result.get("data") or {}
            if isinstance(data, dict):
                doc_id = data.get("id", "")
        out = {"ok": True, "doc_id": doc_id}
        if result.get("duplicate"):
            out["duplicate"] = True
            out["message"] = result.get("message", "文档已存在（幂等跳过）")
        return out

    def list_knowledge(self, page: int = 1, page_size: int = 100) -> list:
        """列出知识库下的知识（用于去重/对账）"""
        if not self._ensure_kb_id():
            return []
        result = self._api_call(
            "GET",
            "/api/v1/knowledge-bases/{}/knowledge".format(self._kb_id),
            params={"page": page, "page_size": page_size},
        )
        if not result.get("ok"):
            return []
        data = result.get("data") or {}
        if isinstance(data, dict):
            return data.get("list") or data.get("items") or []
        return data if isinstance(data, list) else []

    def get_knowledge(self, doc_id: str) -> dict:
        """获取单条知识详情（含 parse_status / error_message）"""
        if not doc_id:
            return {"ok": False, "error": "缺少 doc_id"}
        if not self.is_configured():
            return {"ok": False, "error": "WeKnora 未配置"}
        result = self._api_call(
            "GET",
            "/api/v1/knowledge/{}".format(doc_id),
            timeout=30,
        )
        if not result.get("ok"):
            return result
        data = result.get("data") or {}
        if isinstance(data, dict):
            return {"ok": True, "knowledge": data,
                    "parse_status": data.get("parse_status", ""),
                    "error_message": data.get("error_message", "")}
        return {"ok": True, "knowledge": data}

    def verify_knowledge(self, doc_id: str) -> dict:
        """校验推送后的解析状态（HTTP 200 ≠ 解析成功，需轮询 parse_status）"""
        info = self.get_knowledge(doc_id)
        if not info.get("ok"):
            return info
        status = info.get("parse_status", "")
        terminal = status in ("completed", "failed", "cancelled")
        return {
            "ok": True,
            "doc_id": doc_id,
            "parse_status": status,
            "error_message": info.get("error_message", ""),
            "terminal": terminal,
            "success": status == "completed",
        }

    def _post_push_actions(self, doc_id: str):
        """推送成功后的补充动作（配置控制）:
        - auto_reparse: 显式触发解析（官方解析异步，manual/file 可能停在 draft/disabled）
        - db_enable: 直连数据库启用文档（官方无启用 API；仅当 weknora.db_enable=true）
        """
        if not doc_id:
            return
        cfg = get_config().get("weknora", {})
        if cfg.get("auto_reparse", True):
            self.trigger_reparse(doc_id)
        if cfg.get("db_enable", False):
            self.enable_knowledge_doc(doc_id)

    def trigger_reparse(self, doc_id: str) -> dict:
        """显式触发文档解析: POST /api/v1/knowledge/{id}/reparse

        生产经验: 推送返回 200 只代表已入队，且 manual/file 创建后可能
        enable_status=disabled / parse_status 停在 draft，需显式 reparse。
        """
        result = self._api_call(
            "POST",
            "/api/v1/knowledge/{}/reparse".format(doc_id),
            data=None,
            timeout=60,
        )
        if result.get("ok"):
            logger.info("WeKnora reparse 已触发: %s", doc_id)
        else:
            logger.warning("WeKnora reparse 失败 %s: %s", doc_id, result.get("error"))
        return result

    def enable_knowledge_doc(self, doc_id: str) -> dict:
        """直连 Postgres 启用文档（enable_status=disabled → enabled）

        ⚠️ 官方没有公开的启用 API（UpdateKnowledge 不更新 enable_status），
        而解析 worker 会跳过 disabled 文档（生产 v0.7.1 实证）。
        此方法直连数据库，**破坏 WeKnora 独立性**，仅在 weknora.db_enable=true
        且 DocConverter 与 WeKnora 处于同一 Docker 网络时使用。
        """
        try:
            import psycopg2
        except ImportError:
            logger.error("db_enable 需要 psycopg2（pip install psycopg2-binary）")
            return {"ok": False, "error": "缺少 psycopg2"}
        cfg = get_config().get("weknora", {})
        try:
            conn = psycopg2.connect(
                host=cfg.get("db_host", "postgres"),
                port=int(cfg.get("db_port", 5432)),
                dbname=cfg.get("db_name", "weknora"),
                user=cfg.get("db_user", "postgres"),
                password=cfg.get("db_password", ""),
                connect_timeout=5,
            )
            cur = conn.cursor()
            cur.execute(
                "UPDATE knowledges SET enable_status='enabled' WHERE id=%s",
                (doc_id,))
            conn.commit()
            rows = cur.rowcount
            cur.close()
            conn.close()
            if rows:
                logger.info("已启用文档 %s (enable_status=disabled → enabled)", doc_id)
                return {"ok": True, "enabled": True}
            logger.warning("启用文档 %s 未命中（可能已启用或不存在）", doc_id)
            return {"ok": True, "enabled": False}
        except Exception as e:
            logger.error("启用文档 %s 失败: %s", doc_id, e)
            return {"ok": False, "error": str(e)}

    def delete_knowledge(self, doc_id: str) -> dict:
        """删除单条知识（DELETE /api/v1/knowledge/{id}）

        用于"内容变化 → 删除旧文档再推送"的更新语义（file 类型不支持内容更新，
        只能删除重建）。404 视为已删除（幂等）。
        """
        if not doc_id:
            return {"ok": False, "error": "缺少 doc_id"}
        if not self.is_configured():
            return {"ok": False, "error": "WeKnora 未配置"}
        result = self._api_call(
            "DELETE",
            "/api/v1/knowledge/{}".format(doc_id),
            timeout=60,
        )
        if not result.get("ok") and result.get("status_code") == 404:
            return {"ok": True, "deleted": False, "message": "文档不存在（已删除）"}
        return result

    @staticmethod
    def _mime_for(path: Path) -> str:
        ext = path.suffix.lower()
        mimes = {
            ".md": "text/markdown", ".markdown": "text/markdown",
            ".txt": "text/plain", ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".wps": "application/vnd.ms-works",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".et": "application/vnd.ms-excel",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".dps": "application/vnd.ms-powerpoint",
            ".html": "text/html", ".htm": "text/html",
            ".chm": "application/x-chm",
            ".ofd": "application/ofd",
        }
        return mimes.get(ext, "application/octet-stream")
