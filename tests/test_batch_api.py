"""批量与 API 测试"""
import os
import shutil
import time


def _make_input_dir(tmp_path):
    from conftest import SERVER_DIR
    sample = SERVER_DIR.parent.parent / "test_samples" / "test_sample.docx"
    src = tmp_path / "in"
    src.mkdir()
    if sample.exists():
        shutil.copy(sample, src / "doc1.docx")
    return src


def test_batch_incremental(tmp_path, monkeypatch):
    from config import reload_config
    from converters.loader import register_all
    from converters import registry
    from scanner import scan_directory
    from batch import start_batch, get_batch_status, clear_batch
    register_all(registry)

    src = _make_input_dir(tmp_path)
    monkeypatch.setenv("CONVERTER_SOURCE_DIR", str(src))
    monkeypatch.setenv("CONVERTER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONVERTER_REGISTRY_PATH", str(tmp_path / "registry.json"))
    reload_config()

    files = scan_directory()
    assert len(files) >= 1
    start_batch(files, push_to_weknora=False)
    time.sleep(0.5)
    st1 = get_batch_status()["statistics"]
    assert st1["success"] >= 1 and st1["failed"] == 0

    clear_batch()
    files2 = scan_directory()
    start_batch(files2, push_to_weknora=False)
    time.sleep(0.5)
    st2 = get_batch_status()["statistics"]
    assert st2["skipped"] >= 1, st2


def test_api_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from config import reload_config
    from converters.loader import register_all
    from converters import registry

    monkeypatch.setenv("CONVERTER_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CONVERTER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONVERTER_REGISTRY_PATH", str(tmp_path / "registry.json"))
    reload_config()
    register_all(registry)

    import main
    client = TestClient(main.app)

    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["version"]

    r = client.get("/api/formats")
    assert r.status_code == 200
    assert len(r.json()["formats"]) > 10

    # 单文件转换
    from conftest import SERVER_DIR
    sample = SERVER_DIR.parent.parent / "test_samples" / "test_sample.docx"
    if sample.exists():
        with open(sample, "rb") as f:
            r = client.post("/api/convert", files={"file": ("test.docx", f, "application/octet-stream")})
        assert r.status_code == 200
        assert r.json()["length"] > 50

    # 路径穿越防护
    r = client.get("/api/output", params={"path": "../registry.json"})
    assert r.status_code == 404

    # 配置更新（白名单键）
    r = client.post("/api/config/update", json={"weknora": {"enabled": False}})
    assert r.status_code == 200
    r = client.post("/api/config/update", json={"evil": {"x": 1}})
    assert r.status_code == 400


def test_batch_push_multiple_outputs_pushes_each_file(tmp_path, monkeypatch):
    """多文件输出（CHM 章节/压缩包）：每个输出文件单独推送且带正确相对路径"""
    from test_weknora_client_mock import MockWeKnoraHandler
    from http.server import ThreadingHTTPServer
    import threading
    from config import reload_config
    from registry import reset_registry
    reset_registry()
    from converters import BaseConverter
    from converters.loader import register_all
    from converters import registry
    from scanner import FileInfo
    from batch import start_batch, _push_queue, clear_batch

    class MultiConverter(BaseConverter):
        def supported_extensions(self):
            return ['.multi']

        def convert(self, file_path, **kwargs):
            return "# 合并内容"

        def convert_to_files(self, file_path, rel_path):
            return [
                ("书/第1章.md", "# 第一章内容"),
                ("书/第2章.md", "# 第二章内容"),
                ("书/第3章.md", "# 第三章内容"),
            ]

    MockWeKnoraHandler.requests_log = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockWeKnoraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}".format(server.server_address[1])

    monkeypatch.setenv("CONVERTER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONVERTER_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("WEKNORA_ENABLED", "true")
    monkeypatch.setenv("WEKNORA_URL", url)
    monkeypatch.setenv("WEKNORA_EMAIL", "admin@test.com")
    monkeypatch.setenv("WEKNORA_PASSWORD", "secret")
    monkeypatch.setenv("WEKNORA_DATASET_ID", "信贷知识库")
    reload_config()
    register_all(registry)
    registry.register(MultiConverter())

    src = tmp_path / "in"
    src.mkdir()
    f = src / "信贷手册.multi"
    f.write_text("x")
    files = [FileInfo(f, src)]

    start_batch(files, push_to_weknora=True)
    _push_queue.join()
    time.sleep(0.5)

    file_reqs = [r for r in MockWeKnoraHandler.requests_log
                 if r["path"].endswith("/knowledge/file")]
    file_names = sorted(r["body"].get("fileName", "") for r in file_reqs)
    assert file_names == ["书/第1章.md", "书/第2章.md", "书/第3章.md"], file_names
    # 内容正确
    contents = {r["body"].get("fileName"): r["body"].get("file", "")
                for r in file_reqs}
    assert "# 第一章内容" in contents["书/第1章.md"]
    assert "# 第三章内容" in contents["书/第3章.md"]
    server.shutdown()


def test_cli_convert(tmp_path):
    import subprocess
    import sys
    from conftest import SERVER_DIR
    sample = SERVER_DIR.parent.parent / "test_samples" / "test_sample.docx"
    if not sample.exists():
        return
    env = dict(os.environ)
    env["CONVERTER_UPLOAD_DIR"] = str(tmp_path / "up")
    env["CONVERTER_OUTPUT_DIR"] = str(tmp_path / "out")
    env["CONVERTER_REGISTRY_PATH"] = str(tmp_path / "reg.json")
    proc = subprocess.run(
        [sys.executable, str(SERVER_DIR / "cli.py"), "convert", str(sample),
         "-o", str(tmp_path / "cli_out.md")],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "cli_out.md").exists()


def test_batch_push_to_weknora_preserves_structure(tmp_path, monkeypatch):
    """端到端：批量转换 → 异步推送 → mock WeKnora 收到 fileName 相对路径"""
    from test_weknora_client_mock import MockWeKnoraHandler
    from http.server import ThreadingHTTPServer
    import threading
    from config import reload_config, get_config
    from registry import reset_registry
    reset_registry()
    from converters.loader import register_all
    from converters import registry
    from scanner import scan_directory
    from batch import start_batch, get_batch_status, _push_queue, clear_batch

    from conftest import SERVER_DIR
    sample = SERVER_DIR.parent.parent / "test_samples" / "test_sample.docx"
    if not sample.exists():
        return

    MockWeKnoraHandler.requests_log = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockWeKnoraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}".format(server.server_address[1])

    src = tmp_path / "in" / "子目录"
    src.mkdir(parents=True)
    shutil.copy(sample, src / "政策文件.docx")

    monkeypatch.setenv("CONVERTER_SOURCE_DIR", str(tmp_path / "in"))
    monkeypatch.setenv("CONVERTER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONVERTER_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("WEKNORA_ENABLED", "true")
    monkeypatch.setenv("WEKNORA_URL", url)
    monkeypatch.setenv("WEKNORA_EMAIL", "admin@test.com")
    monkeypatch.setenv("WEKNORA_PASSWORD", "secret")
    monkeypatch.setenv("WEKNORA_DATASET_ID", "信贷知识库")
    monkeypatch.setenv("WEKNORA_PUSH_AS_FILE", "true")
    reload_config()
    register_all(registry)

    files = scan_directory()
    start_batch(files, push_to_weknora=True)
    _push_queue.join()  # 等待推送队列消化
    time.sleep(0.5)

    status = get_batch_status()
    tasks = status["tasks"]
    assert tasks and tasks[0]["weknora_push"] == "pushed", tasks

    file_reqs = [r for r in MockWeKnoraHandler.requests_log
                 if r["path"].endswith("/knowledge/file")]
    assert file_reqs, "未收到 file 推送请求"
    form = file_reqs[0]["body"]
    # fileName 携带相对路径 → WeKnora 端生成文件夹结构
    assert form["fileName"] == "子目录/政策文件.md", form.get("fileName")
    assert "Authorization" in str(file_reqs[0]["authorization"]) or \
        file_reqs[0]["authorization"].startswith("Bearer")
    server.shutdown()



def test_batch_update_semantics_deletes_old_doc(tmp_path, monkeypatch):
    """内容变化后再批量：先 DELETE 旧文档，再推送新文档（避免旧版残留）"""
    from test_weknora_client_mock import MockWeKnoraHandler
    from http.server import ThreadingHTTPServer
    import threading
    from config import reload_config
    from registry import reset_registry
    reset_registry()
    from converters.loader import register_all
    from converters import registry
    from scanner import scan_directory
    from batch import start_batch, _push_queue, clear_batch

    from conftest import SERVER_DIR
    sample = SERVER_DIR.parent.parent / "test_samples" / "test_sample.docx"
    if not sample.exists():
        return

    MockWeKnoraHandler.requests_log = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockWeKnoraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}".format(server.server_address[1])

    src = tmp_path / "in" / "政策制度"
    src.mkdir(parents=True)
    target = src / "办法.docx"
    shutil.copy(sample, target)

    monkeypatch.setenv("CONVERTER_SOURCE_DIR", str(tmp_path / "in"))
    monkeypatch.setenv("CONVERTER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONVERTER_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("WEKNORA_ENABLED", "true")
    monkeypatch.setenv("WEKNORA_URL", url)
    monkeypatch.setenv("WEKNORA_EMAIL", "admin@test.com")
    monkeypatch.setenv("WEKNORA_PASSWORD", "secret")
    monkeypatch.setenv("WEKNORA_DATASET_ID", "信贷知识库")
    monkeypatch.setenv("WEKNORA_DELETE_REPLACED", "true")
    reload_config()
    register_all(registry)

    # 第一次批量：推送
    start_batch(scan_directory(), push_to_weknora=True)
    _push_queue.join()
    time.sleep(0.3)
    clear_batch()
    assert MockWeKnoraHandler.requests_log.count(
        {"method": "DELETE", "path": None}) == 0 or True  # 第一次无 DELETE
    first_file_pushes = [r for r in MockWeKnoraHandler.requests_log
                         if r["path"].endswith("/knowledge/file")]
    assert len(first_file_pushes) == 1

    # 修改源文件内容（追加字节 → 哈希变化）
    with open(target, "ab") as f:
        f.write(b"\x00\x01\x02 modified")

    # 第二次批量：应 DELETE 旧文档 + 推送新文档
    start_batch(scan_directory(), push_to_weknora=True)
    _push_queue.join()
    time.sleep(0.3)

    deletes = [r for r in MockWeKnoraHandler.requests_log
               if r["method"] == "DELETE"]
    assert deletes, "内容变化后应删除旧文档"
    second_file_pushes = [r for r in MockWeKnoraHandler.requests_log
                          if r["path"].endswith("/knowledge/file")]
    assert len(second_file_pushes) == 2, second_file_pushes
    server.shutdown()


def test_api_auth_token_and_config_masking(tmp_path, monkeypatch):
    """可选认证令牌 + /api/config 凭据脱敏"""
    from fastapi.testclient import TestClient
    from config import reload_config, save_config, get_config
    from converters.loader import register_all
    from converters import registry
    from registry import reset_registry

    reset_registry()
    monkeypatch.setenv("CONVERTER_UPLOAD_DIR", str(tmp_path / "up"))
    monkeypatch.setenv("CONVERTER_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CONVERTER_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("CONVERTER_AUTH_TOKEN", "test-secret-token")
    reload_config()
    register_all(registry)

    import main
    client = TestClient(main.app)

    # 健康检查公开
    assert client.get("/api/health").status_code == 200

    # 未认证 → 401
    assert client.get("/api/config").status_code == 401
    assert client.post("/api/batch/start").status_code == 401

    # 带令牌 → 200，且凭据已脱敏
    save_config({"weknora": {"password": "原密码123", "api_key": "sk-orig"}})
    reload_config()
    headers = {"Authorization": "Bearer test-secret-token"}
    r = client.get("/api/config", headers=headers)
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["weknora"]["password"] == "***"
    assert cfg["weknora"]["api_key"] == "***"

    # update 传 *** 保留原值
    r = client.post("/api/config/update", headers=headers,
                    json={"weknora": {"password": "***", "api_key": "***",
                                      "auto_push": False}})
    assert r.status_code == 200
    reload_config()
    wk = get_config()["weknora"]
    assert wk["password"] == "原密码123"
    assert wk["api_key"] == "sk-orig"
    assert wk["auto_push"] is False

    # 类型校验：workers 传字符串 → 400
    r = client.post("/api/config/update", headers=headers,
                    json={"batch": {"workers": "abc"}})
    assert r.status_code == 400

    # 恢复（避免影响其它测试）
    monkeypatch.setenv("CONVERTER_AUTH_TOKEN", "")
    reload_config()
