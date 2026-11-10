# -*- coding: utf-8 -*-
"""RemoteOcrEngine（HTTP 远程 OCR 通用接口）与 serve_ocr 协议联调测试。

覆盖：text/blocks 两种返回、Bearer 鉴权、缓存复用、失败传播、换服务失效。
"""
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import ocr


class _MockOcr(BaseHTTPRequestHandler):
    RECV = []

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        _MockOcr.RECV.append((body, self.headers.get("Authorization", "")))
        img = body.get("image", "")
        if img.endswith("bad.png"):
            resp = {"ok": False, "error": "boom"}
        elif img.endswith("blk.png"):
            resp = {"ok": True,
                    "blocks": [{"text": "B块", "score": 0.9,
                                "x": 5, "y": 5, "w": 50, "h": 12}],
                    "engine": "X"}
        else:
            resp = {"ok": True, "text": "第一行\n第二行", "engine": "PP-OCRv6-gpu"}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(autouse=True)
def _reset_recv():
    _MockOcr.RECV.clear()
    yield


@pytest.fixture()
def mock_server():
    srv = HTTPServer(("127.0.0.1", 0), _MockOcr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:{}/ocr".format(srv.server_address[1])
    srv.shutdown()


@pytest.fixture()
def tmp_img(tmp_path):
    def make(name, content=b"PNGFAKE"):
        p = tmp_path / name
        p.write_bytes(content + name.encode())
        return str(p)
    return make


def _cfg(url, cache_dir, token="t0k"):
    cfg = ocr.default_config()
    cfg.update({"enabled": True, "backend": "remote",
                "cache_dir": cache_dir})
    cfg["remote"] = {"url": url, "token": token, "timeout": 10}
    return cfg


def test_remote_text_mode(mock_server, tmp_img, tmp_path):
    ocr._ENGINE = None
    cfg = _cfg(mock_server, str(tmp_path / "c1"))
    d = ocr.ocr_image(tmp_img("ok.png"), cfg)
    assert d["text"] == "第一行\n第二行"
    assert d["engine"] == "remote" and d["remote_engine"] == "PP-OCRv6-gpu"
    assert _MockOcr.RECV[-1][1] == "Bearer t0k"


def test_remote_blocks_mode(mock_server, tmp_img, tmp_path):
    ocr._ENGINE = None
    cfg = _cfg(mock_server, str(tmp_path / "c2"))
    d = ocr.ocr_image(tmp_img("blk.png"), cfg)       # blocks 模式
    assert d["blocks"][0]["text"] == "B块"


def test_remote_cache_and_failure(mock_server, tmp_img, tmp_path):
    ocr._ENGINE = None
    cfg = _cfg(mock_server, str(tmp_path / "c3"))
    good = tmp_img("g.png")
    d = ocr.ocr_image(good, cfg)
    assert ocr.ocr_image(good, cfg)["cached"] is True
    with pytest.raises(RuntimeError, match="boom"):
        ocr.ocr_image(tmp_img("bad.png"), cfg)


def test_remote_sig_invalidation(mock_server, tmp_path):
    cfg = _cfg(mock_server, str(tmp_path / "c4"))
    cfg2 = {**cfg, "remote": {**cfg["remote"], "url": cfg["remote"]["url"] + "/x"}}
    assert ocr._config_signature(cfg) != ocr._config_signature(cfg2)
