"""
WeKnora API 客户端: 登录认证 + 文档/文件推送
"""
import json, logging, time
from pathlib import Path
from typing import Optional
from urllib import request, error as urlerror

from config import get_config

logger = logging.getLogger("docconverter.weknora")


class WeKnoraClient:
    """WeKnora 知识库 API 客户端"""

    def __init__(self, config: dict = None):
        if config is None:
            config = get_config()
        wk = config.get("weknora", {})
        self.api_url = wk.get("api_url", "").rstrip("/")
        self.api_key = wk.get("api_key", "")
        self.dataset_id = wk.get("dataset_id", "")
        self.enabled = wk.get("enabled", False)
        self.auto_push = wk.get("auto_push", True)
        self.email = wk.get("email", "")
        self.password = wk.get("password", "")
        self._token = None
        self._token_expiry = 0
        self._kb_id = None

    def is_configured(self) -> bool:
        return bool(self.enabled and self.api_url and self.email and self.password)

    def _api_call(self, method: str, path: str, data=None, files=None,
                  content_type: Optional[str] = None, retry: int = 0) -> dict:
        """执行 API 调用（自动处理认证和重试）"""
        url = "{}{}".format(self.api_url, path)

        if not self._token or time.time() > self._token_expiry:
            self._login()

        headers = {
            "Authorization": "Bearer {}".format(self._token),
        }
        if content_type:
            headers["Content-Type"] = content_type

        if files:
            # Multipart upload - use the requests library
            import http.client
            # Build multipart body manually for stdlib
            boundary = "----WebKitFormBoundary{}".format(int(time.time()))
            body_parts = []
            for field_name, (filename, file_content, file_mime) in files.items():
                body_parts.append("--{}".format(boundary))
                body_parts.append(
                    'Content-Disposition: form-data; name="{}"; filename="{}"'.format(
                        field_name, filename))
                body_parts.append("Content-Type: {}".format(file_mime))
                body_parts.append("")
                body_parts.append(file_content if isinstance(file_content, str) else
                                  file_content.decode("utf-8", errors="replace"))
            body_parts.append("--{}--".format(boundary))
            body = "\r\n".join(body_parts)
            headers["Content-Type"] = "multipart/form-data; boundary={}".format(boundary)
            body = body.encode("utf-8") if isinstance(body, str) else body
        else:
            body = json.dumps(data).encode("utf-8") if data else None
            if data and not content_type:
                headers["Content-Type"] = "application/json"

        try:
            req = request.Request(url, data=body, headers=headers, method=method)
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urlerror.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 401 and retry < 1:
                # Token expired, re-login and retry
                self._token = None
                return self._api_call(method, path, data, files, content_type, retry + 1)
            logger.error("WeKnora API {} {}: {}".format(method, path, err))
            return {"ok": False, "error": "HTTP {}: {}".format(e.code, err)}
        except Exception as e:
            logger.error("WeKnora API error: {}".format(e))
            return {"ok": False, "error": str(e)}

    def _login(self):
        """登录 WeKnora 获取 JWT token"""
        try:
            body = json.dumps({
                "email": self.email,
                "password": self.password,
            }).encode("utf-8")
            req = request.Request(
                "{}/api/v1/auth/login".format(self.api_url),
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                self._token = data["token"]
                self._token_expiry = time.time() + 3600  # 1 hour
                logger.info("WeKnora login successful")
        except Exception as e:
            self._token = None
            logger.error("WeKnora login failed: {}".format(e))

    def _ensure_kb_id(self):
        """获取知识库 ID（缓存）"""
        if self._kb_id:
            return True
        try:
            result = self._api_call("GET", "/api/v1/knowledge-bases")
            if isinstance(result, dict) and result.get("data"):
                kbs = result["data"]
                if self.dataset_id:
                    for kb in kbs:
                        if kb.get("id") == self.dataset_id or kb.get("name") == self.dataset_id:
                            self._kb_id = kb["id"]
                            return True
                self._kb_id = kbs[0]["id"]
                return True
            return False
        except Exception as e:
            logger.error("Failed to get KB list: {}".format(e))
            return False

    def test_connection(self) -> dict:
        """测试 WeKnora 连接"""
        if not self.api_url:
            return {"ok": False, "error": "未配置 WeKnora URL"}
        if not self.email or not self.password:
            return {"ok": False, "error": "未配置 WeKnora 登录凭据"}

        try:
            self._login()
            if not self._token:
                return {"ok": False, "error": "登录失败，请检查邮箱/密码"}

            found = self._ensure_kb_id()
            if found:
                return {"ok": True, "kb_id": self._kb_id}
            else:
                return {"ok": True, "warning": "已登录但未找到知识库"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def push_file(self, file_path: str, title: str = "",
                  source_format: str = "") -> dict:
        """推送文件到 WeKnora 知识库

        Args:
            file_path: Markdown 文件路径
            title: 文档标题 (留空用文件名)
            source_format: 源格式名称

        Returns:
            {"ok": True, "doc_id": "..."} or {"ok": False, "error": "..."}
        """
        if not self.is_configured():
            return {"ok": False, "error": "WeKnora 未配置"}

        if not self._ensure_kb_id():
            return {"ok": False, "error": "无法找到知识库"}

        try:
            path = Path(file_path)
            content = path.read_text(encoding="utf-8") if path.exists() else file_path
            doc_title = title or path.stem

            # Upload as file
            result = self._api_call(
                "POST",
                "/api/v1/knowledge-bases/{}/knowledge/file".format(self._kb_id),
                files={
                    "file": (
                        "{}.md".format(doc_title),
                        content,
                        "text/markdown",
                    )
                },
            )

            doc_id = ""
            if isinstance(result, dict) and result.get("data"):
                doc_id = result["data"].get("id", "")

            return {"ok": True, "doc_id": doc_id, "response": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def push_manual(self, title: str, content: str,
                    source_path: str = "", source_format: str = "") -> dict:
        """推送纯文本文档到 WeKnora"""
        if not self.is_configured():
            return {"ok": False, "error": "WeKnora 未配置"}

        if not self._ensure_kb_id():
            return {"ok": False, "error": "无法找到知识库"}

        try:
            result = self._api_call(
                "POST",
                "/api/v1/knowledge-bases/{}/knowledge/manual".format(self._kb_id),
                data={
                    "title": title,
                    "content": content,
                },
            )

            doc_id = ""
            if isinstance(result, dict) and result.get("data"):
                doc_id = result["data"].get("id", "")

            return {"ok": True, "doc_id": doc_id}
        except Exception as e:
            return {"ok": False, "error": str(e)}
