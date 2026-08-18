"""v2.1.1 加固项测试：rel_path 归一化 / registry 崩溃恢复 / 推送失败持久化与重推 / 队列有界配置"""
import os
import json
import queue
import types


# ── rel_path 归一化 ──

def test_normalize_rel_path_basic():
    from weknora_client import normalize_rel_path as norm
    assert norm("a\\b\\c.md") == "a/b/c.md"
    assert norm("a//b/./c.md") == "a/b/c.md"
    assert norm("") == ""
    assert norm("/a/b/c.md") == "a/b/c.md"


def test_normalize_rel_path_traversal_safe():
    """`..` 段被移除，任何输入都不能产生逃逸相对路径"""
    from weknora_client import normalize_rel_path as norm
    for evil in ("../../etc/passwd", "a/../../x", "..", "/../.."):
        out = norm(evil)
        assert ".." not in out.split("/")
        assert not out.startswith("/")


def test_normalize_rel_path_limits():
    from weknora_client import normalize_rel_path as norm
    # 单段 ≤128（保留扩展名）
    out = norm("a" * 300 + ".md")
    assert len(out) <= 128
    assert out.endswith(".md")
    # 深度 ≤16（保留最深层文件名）
    deep = norm("/".join("d%d" % i for i in range(40)) + "/f.md")
    assert deep.count("/") < 16
    assert deep.endswith("f.md")
    # 总长 ≤1024
    assert len(norm("x" * 5000)) <= 1024


# ── registry JSON 崩溃恢复（.bak 回退） ──

def test_registry_bak_recovery(tmp_path):
    from registry import SyncRegistry
    p = tmp_path / "reg.json"
    reg = SyncRegistry(str(p))
    reg.record("/data/in/a.docx", "hash1", "a.md", status="converted")
    reg._save()
    # 制造正常备份
    bak = p.with_name(p.name + ".bak")
    bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    # 主文件被半写损坏
    p.write_text("{ 这不是合法 json", encoding="utf-8")
    reg2 = SyncRegistry(str(p))
    rec = reg2.get("/data/in/a.docx")
    assert rec is not None
    assert rec["source_hash"] == "hash1"
    assert rec["output_rel"] == "a.md"


def test_registry_save_backup_on_fallback(tmp_path, monkeypatch):
    """os.replace 失败时直写前生成 .bak（单文件 bind mount 场景）"""
    import os as _os
    from registry import SyncRegistry
    p = tmp_path / "reg.json"
    reg = SyncRegistry(str(p))
    reg.record("/data/in/b.docx", "hash2", "b.md")
    # 首次直接写（无旧文件）
    p.write_text('{"seed": 1}', encoding="utf-8")
    orig_replace = _os.replace

    def broken_replace(*a, **k):
        raise OSError("simulate bind-mount")

    monkeypatch.setattr(_os, "replace", broken_replace)
    reg.record("/data/in/c.docx", "hash3", "c.md")
    # 主文件已更新，且生成了旧文件备份
    assert p.read_text(encoding="utf-8").find("c.docx") != -1
    bak = p.with_name(p.name + ".bak")
    assert bak.exists()
    assert "seed" in bak.read_text(encoding="utf-8")


# ── 推送失败持久化 + 重推 ──

def test_push_failed_persist_and_retry(monkeypatch, tmp_path):
    from config import reload_config
    import batch
    failed_path = tmp_path / "push_failed.jsonl"
    monkeypatch.setenv("WEKNORA_ENABLED", "false")
    monkeypatch.setenv("WEKNORA_PUSH_FAILED_PATH", str(failed_path))
    reload_config()

    # 清空全局失败列表 + 用本地队列/无 worker，隔离真实消费者
    batch._push_failed[:] = []
    local_q = queue.Queue()
    monkeypatch.setattr(batch, "_get_push_queue", lambda: local_q)
    monkeypatch.setattr(batch, "_ensure_push_workers", lambda: None)

    fi = types.SimpleNamespace(path="/data/in/政策.docx", rel_path="政策.docx")
    task = types.SimpleNamespace(
        file_info=fi, push_original=False, output_files=["政策.md"],
        weknora_push=batch.PUSH_FAILED, weknora_error="网络超时",
    )

    batch._record_push_failed(task, "网络超时")
    assert batch.failed_push_count() == 1
    assert failed_path.exists()
    line = json.loads(failed_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["source"] == "/data/in/政策.docx"
    assert "网络超时" in line["error"]

    out = batch.retry_failed_pushes()
    assert out["requeued"] == 1
    assert batch.failed_push_count() == 0
    assert local_q.qsize() == 1
    qtask = local_q.get_nowait()
    assert qtask is task
    assert qtask.weknora_push == batch.PUSH_PENDING
    assert qtask.weknora_error == ""


def test_retry_push_empty(monkeypatch):
    import batch
    batch._push_failed[:] = []
    local_q = queue.Queue()
    monkeypatch.setattr(batch, "_get_push_queue", lambda: local_q)
    monkeypatch.setattr(batch, "_ensure_push_workers", lambda: None)
    out = batch.retry_failed_pushes()
    assert out["requeued"] == 0


# ── 推送队列有界配置 ──

def test_push_queue_size_config(monkeypatch):
    from config import reload_config
    import batch
    monkeypatch.setenv("WEKNORA_PUSH_QUEUE_SIZE", "5")
    reload_config()
    assert batch._queue_maxsize() == 5
    monkeypatch.setenv("WEKNORA_PUSH_QUEUE_SIZE", "0")
    reload_config()
    assert batch._queue_maxsize() == 1  # 下限 1


def test_retry_push_endpoint(monkeypatch, tmp_path):
    """POST /api/batch/retry-push 端点存在且返回 requeued"""
    # 隔离：其他测试可能把 auth_token 写进持久化配置文件
    monkeypatch.setenv("CONVERTER_AUTH_TOKEN", "")
    from config import reload_config
    reload_config()
    from fastapi.testclient import TestClient
    import main
    import batch
    batch._push_failed[:] = []
    local_q = queue.Queue()
    client = TestClient(main.app)
    r = client.post("/api/batch/retry-push")
    assert r.status_code == 200
    assert r.json().get("requeued") == 0
    # 状态里含 push_failed_count / push_workers
    s = client.get("/api/batch/status").json()
    assert "push_failed_count" in s
    assert "push_workers" in s
