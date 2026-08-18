"""
Mock WeKnora 服务测试: 验证客户端请求形状与官方源码一致

模拟 Tencent/WeKnora 的真实端点行为（internal/handler 核对）:
- POST /api/v1/auth/login                    -> {success, token, refresh_token, tenant}
- GET  /api/v1/knowledge-bases               -> {success, data:[{id,name}]}
- POST /api/v1/knowledge-bases/{id}/knowledge/manual -> {success, data:{id}}
- POST /api/v1/knowledge-bases/{id}/knowledge/file   -> multipart {file, fileName, metadata, channel}
- 重复文件 (fileName 相同) -> 409 {code:"duplicate_file", data:{id}}
"""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class MockWeKnoraHandler(BaseHTTPRequestHandler):
    """记录请求并在内存中回放官方响应"""

    requests_log = []      # class-level: (method, path, headers, body, parsed)
    auth_header_seen = None

    def log_message(self, *args):
        pass

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        parsed = None
        if "multipart/form-data" in ctype:
            parsed = self._parse_multipart(ctype, raw)
        elif "application/json" in ctype:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = raw.decode("utf-8", errors="replace")
        self.__class__.requests_log.append({
            "method": "POST", "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "api_key": self.headers.get("X-API-Key", ""),
            "body": parsed,
        })

        if self.path == "/api/v1/auth/login":
            self._send_json(200, {
                "success": True, "message": "Login successful",
                "user": {"id": "usr-1", "email": "admin@test.com"},
                "tenant": {"id": 1, "name": "测试空间"},
                "memberships": [{"tenant_id": 1, "role": "owner"}],
                "token": "jwt-test-token",
                "refresh_token": "refresh-test",
            })
            return

        if self.path.startswith("/api/v1/knowledge/") and len(self.path) > len("/api/v1/knowledge/"):
            doc_id = self.path[len("/api/v1/knowledge/"):]
            self._send_json(200, {
                "success": True,
                "data": {"id": doc_id, "title": "测试文档",
                         "parse_status": "completed", "error_message": ""},
            })
            return

        if self.path == "/api/v1/knowledge-bases":
            self._send_json(200, {
                "success": True,
                "data": [
                    {"id": "kb-00000001", "name": "信贷知识库", "type": "document"},
                    {"id": "kb-00000002", "name": "测试知识库", "type": "document"},
                ],
            })
            return

        if self.path.endswith("/knowledge/manual"):
            payload = parsed or {}
            assert isinstance(payload, dict)
            assert payload.get("title"), "manual 请求缺少 title"
            assert payload.get("content"), "manual 请求缺少 content"
            # 对齐官方 knowledge_create.go：status 仅接受 draft/publish
            status = (payload.get("status") or "").strip().lower()
            assert status in ("draft", "publish"), \
                "manual status 非法: %r（官方仅接受 draft/publish）" % payload.get("status")
            self._send_json(200, {
                "success": True,
                "data": {"id": "man-00000001", "title": payload["title"],
                         "type": "manual", "parse_status": "processing"},
            })
            return

        if self.path.endswith("/knowledge/file"):
            form = parsed or {}
            filename = form.get("fileName", "")
            if filename == "已存在.md":
                # 模拟官方重复检测 409 duplicate_file
                self._send_json(409, {
                    "success": False,
                    "message": "文件已存在",
                    "code": "duplicate_file",
                    "data": {"id": "existing-0001", "title": filename},
                })
                return
            assert filename, "file 请求缺少 fileName"
            assert "file" in form, "file 请求缺少 file 字段"
            self._send_json(200, {
                "success": True,
                "data": {"id": "file-00000001", "title": filename,
                         "type": "file", "parse_status": "processing"},
            })
            return

        if self.path.startswith("/api/v1/knowledge/") and self.path.endswith("/reparse"):
            doc_id = self.path.split("/")[-2]
            self._send_json(200, {"success": True,
                                  "data": {"id": doc_id, "parse_status": "pending"}})
            return

        self._send_json(404, {"success": False, "message": "not found"})

    def do_DELETE(self):
        self.__class__.requests_log.append({
            "method": "DELETE", "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "api_key": self.headers.get("X-API-Key", ""), "body": None,
        })
        if self.path.startswith("/api/v1/knowledge/"):
            self._send_json(200, {"success": True, "message": "Deleted"})
            return
        self._send_json(404, {"success": False, "message": "not found"})

    def do_GET(self):
        self.__class__.requests_log.append({
            "method": "GET", "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "api_key": self.headers.get("X-API-Key", ""), "body": None,
        })
        if self.path.startswith("/api/v1/knowledge/") and len(self.path) > len("/api/v1/knowledge/"):
            doc_id = self.path[len("/api/v1/knowledge/"):]
            self._send_json(200, {
                "success": True,
                "data": {"id": doc_id, "title": "测试文档",
                         "parse_status": "completed", "error_message": ""},
            })
            return

        if self.path == "/api/v1/knowledge-bases":
            self._send_json(200, {
                "success": True,
                "data": [
                    {"id": "kb-00000001", "name": "信贷知识库"},
                    {"id": "kb-00000002", "name": "测试知识库"},
                ],
            })
            return
        self._send_json(404, {"success": False, "message": "not found"})

    def _parse_multipart(self, content_type, raw):
        """解析 multipart/form-data（email 标准库）"""
        from email.parser import BytesParser
        from email import policy
        import cgi
        try:
            fields = cgi.FieldStorage(
                fp=io.BytesIO(raw), headers={"content-type": content_type},
                environ={"REQUEST_METHOD": "POST"}, keep_blank_values=True)
            result = {}
            for key in fields.keys():
                field = fields[key]
                if isinstance(field, list):
                    result[key] = [f.value if not f.filename else f.value for f in field]
                else:
                    val = field.value
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="replace")
                    result[key] = val
            return result
        except Exception:
            return {"_raw": raw}


@pytest.fixture()
def mock_weknora():
    MockWeKnoraHandler.requests_log = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockWeKnoraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:{}".format(server.server_address[1])
    server.shutdown()


def _client(url, **overrides):
    from weknora_client import WeKnoraClient
    cfg = {
        "weknora": {
            "enabled": True,
            "api_url": url,
            "email": "admin@test.com",
            "password": "secret",
            "dataset_id": "信贷知识库",
            "auto_push": True,
            **overrides,
        }
    }
    return WeKnoraClient(cfg)


def test_login_and_kb_resolution(mock_weknora):
    client = _client(mock_weknora)
    assert client.is_configured()
    result = client.test_connection()
    assert result["ok"] is True
    assert result["kb_id"] == "kb-00000001"  # 按名称解析 dataset_id
    # 验证登录请求形状
    login = [r for r in MockWeKnoraHandler.requests_log
             if r["path"] == "/api/v1/auth/login"][0]
    assert login["body"] == {"email": "admin@test.com", "password": "secret"}


def test_push_manual_shape(mock_weknora):
    client = _client(mock_weknora)
    result = client.push_manual("测试标题", "# 正文内容\n\n你好")
    assert result["ok"] is True and result["doc_id"] == "man-00000001"
    req = [r for r in MockWeKnoraHandler.requests_log
           if r["path"].endswith("/knowledge/manual")][0]
    assert req["authorization"] == "Bearer jwt-test-token"
    assert req["body"]["title"] == "测试标题"
    assert req["body"]["channel"] == "docconverter"
    assert req["body"]["status"] == "publish"


def test_push_markdown_file_preserves_structure(mock_weknora):
    client = _client(mock_weknora)
    result = client.push_markdown_file(
        title="农银桂办发〔2024〕1号",
        content="# 政策内容\n\n正文…",
        rel_path="政策制度/2024/农银桂办发〔2024〕1号.md",
        metadata={"source_hash": "abc123", "source_path": "/data/input/x.docx"},
    )
    assert result["ok"] is True and result["doc_id"] == "file-00000001"
    req = [r for r in MockWeKnoraHandler.requests_log
           if r["path"].endswith("/knowledge/file")][0]
    form = req["body"]
    # fileName 携带完整相对路径（WeKnora 端按 / 拆分文件夹）
    assert form["fileName"] == "政策制度/2024/农银桂办发〔2024〕1号.md"
    assert "# 政策内容" in form["file"]
    meta = json.loads(form["metadata"])
    assert meta["source_hash"] == "abc123"


def test_push_file_with_rel_path(mock_weknora, tmp_path):
    from weknora_client import WeKnoraClient
    src = tmp_path / "原文.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    client = _client(mock_weknora)
    result = client.push_file(str(src), rel_path="报表数据/2024/月报.pdf")
    assert result["ok"] is True
    req = [r for r in MockWeKnoraHandler.requests_log
           if r["path"].endswith("/knowledge/file")][0]
    assert req["body"]["fileName"] == "报表数据/2024/月报.pdf"


def test_duplicate_409_is_idempotent(mock_weknora):
    client = _client(mock_weknora)
    result = client.push_markdown_file(
        title="已存在", content="# x", rel_path="已存在.md")
    assert result["ok"] is True
    assert result["duplicate"] is True
    assert result["doc_id"] == "existing-0001"


def test_multiple_kbs_require_dataset_id(mock_weknora):
    """多知识库且未配置 dataset_id 时拒绝自动猜测，避免推错库"""
    from weknora_client import WeKnoraClient
    client = WeKnoraClient({
        "weknora": {
            "enabled": True, "api_url": mock_weknora,
            "email": "admin@test.com", "password": "secret",
            "dataset_id": "", "auto_push": True,
        }
    })
    result = client.push_manual("标题", "内容")
    assert result["ok"] is False
    assert "dataset_id" in result["error"]


def test_api_key_auth_header(mock_weknora):
    client = _client(mock_weknora, api_key="sk-test-key")
    result = client.push_manual("标题", "内容")
    assert result["ok"] is True
    req = [r for r in MockWeKnoraHandler.requests_log
           if r["path"].endswith("/knowledge/manual")][0]
    assert req["api_key"] == "sk-test-key"
    assert req["authorization"] == ""  # API Key 模式不发送 Bearer


def test_oversize_file_rejected(mock_weknora, tmp_path):
    from weknora_client import WeKnoraClient, DEFAULT_MAX_FILE_SIZE_MB
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * (DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024 + 1))
    client = _client(mock_weknora)
    result = client.push_file(str(big))
    assert result["ok"] is False
    assert "50MB" in result["error"]


def test_delete_knowledge(mock_weknora):
    """DELETE /knowledge/{id} 幂等（404 视为已删除）"""
    client = _client(mock_weknora)
    r = client.delete_knowledge("doc-999")
    assert r["ok"] is True
    dels = [x for x in MockWeKnoraHandler.requests_log
            if x["method"] == "DELETE"]
    assert len(dels) == 1
    assert dels[0]["path"] == "/api/v1/knowledge/doc-999"


def test_verify_knowledge_parse_status(mock_weknora):
    """校验 parse_status：HTTP 200 后轮询解析终态"""
    client = _client(mock_weknora)
    info = client.verify_knowledge("file-00000001")
    assert info["ok"] is True
    assert info["parse_status"] == "completed"
    assert info["terminal"] is True
    assert info["success"] is True


def test_push_with_target_kb(mock_weknora):
    """folder→KB 映射：push_markdown_file 携带 kb_id 时推送到指定知识库"""
    client = _client(mock_weknora)
    result = client.push_markdown_file(
        title="报告", content="# 内容", rel_path="工作邮件/报告.md",
        kb_id="kb-00000002")
    assert result["ok"] is True
    reqs = [r for r in MockWeKnoraHandler.requests_log
            if r["path"].endswith("/knowledge/file")]
    assert any("/kb-00000002/" in r["path"] for r in reqs), reqs


def test_trigger_reparse(mock_weknora):
    client = _client(mock_weknora)
    r = client.trigger_reparse("doc-abc")
    assert r["ok"] is True
    reparses = [x for x in MockWeKnoraHandler.requests_log
                if x["path"].endswith("/reparse")]
    assert len(reparses) == 1
    assert reparses[0]["path"] == "/api/v1/knowledge/doc-abc/reparse"


def test_push_manual_truncates_long_content(mock_weknora):
    """manual 内容超过 20 万字符时截断（生产经验：服务端上限）"""
    client = _client(mock_weknora)
    long_content = "x" * 250000
    result = client.push_manual("长文档", long_content)
    assert result["ok"] is True
    req = [r for r in MockWeKnoraHandler.requests_log
           if r["path"].endswith("/knowledge/manual")][0]
    assert len(req["body"]["content"]) <= 195000
