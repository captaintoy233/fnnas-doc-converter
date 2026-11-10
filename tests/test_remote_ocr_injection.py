# -*- coding: utf-8 -*-
"""远程 OCR 注入端到端：HTMConverter + backend=remote → MD 中带 OCR 文本。

证明"换 OCR 引擎只改配置、转换器零改动"这一设计目标成立。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import ocr
from converters.htm_converter import HTMConverter

OCR_TEXT = "图片里的文字：第一条 第二条"


class _Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers["Content-Length"])
        self.rfile.read(n)
        data = json.dumps({"ok": True, "text": OCR_TEXT,
                           "engine": "PP-OCRv6-gpu"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture()
def mock_url():
    srv = HTTPServer(("127.0.0.1", 0), _Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:{}/ocr".format(srv.server_address[1])
    srv.shutdown()


def _png(path: Path, w=120, h=40):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    cv2.imwrite(str(path), np.full((h, w, 3), 200, dtype=np.uint8))


def test_remote_ocr_injected_into_markdown(tmp_path, mock_url):
    ocr._ENGINE = None
    asset = tmp_path / "doc.files"
    asset.mkdir()
    _png(asset / "pic.png")
    (tmp_path / "doc.htm").write_text(
        "<html><head><title>T</title></head><body><p>正文</p>"
        '<img src="doc.files/pic.png"></body></html>', encoding="utf-8")

    out = tmp_path / "out"
    out.mkdir()
    cfg = ocr.default_config()
    cfg.update({"enabled": True, "backend": "remote",
                "cache_dir": str(tmp_path / "cache"),
                "inline_fallback": True})
    cfg["remote"] = {"url": mock_url, "token": "", "timeout": 30}

    conv = HTMConverter(image_mode="link", assets_root=str(out), ocr_cfg=cfg)
    rel, content = conv.convert_to_files(str(tmp_path / "doc.htm"), "文书/doc.htm")[0]

    assert OCR_TEXT in content, content[:400]
    assert "ocr:begin" in content
    # 远程调用结果应写入缓存（缓存按 key 前两位分子目录），二次转换不再打服务
    cached = list((tmp_path / "cache").rglob("*.json"))
    assert cached, "OCR 结果应落缓存"
