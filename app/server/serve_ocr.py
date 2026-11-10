#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OCR 服务封装：把任意 OCR 引擎暴露为 DocConverter `RemoteOcrEngine` 协议的 HTTP 服务。

协议（与 app/server/ocr.py::RemoteOcrEngine 对应）：
    POST /ocr   body = {"image": "<服务端本地路径>"}        # 同机共享卷
               body = {"image_b64": "...", "name": "x.png"} # 跨机传输
    -> {"ok": true, "text": "...", "engine": "...", "seconds": 1.2}
       {"ok": false, "error": "..."}
    GET /health -> {"ok": true, "engine": "...", "device": "..."}

后端选择（--backend）：
    paddle : PaddleOCR / PaddleOCR-VL（GPU 主机首选；复用常驻 worker 协议，
             需 paddleocr venv；--engine fast|vl 透传）
    rapid  : RapidOCR（纯 CPU ONNX，任何机器可用）
    cmd    : 任意外部命令（--cmd 'your-ocr {image}'，stdout 即文本）

安全：默认只监听 127.0.0.1；对外须配 --token（Bearer），
      且建议只在 Tailscale/内网接口上暴露。

**按需调用（GPU 资源紧张时的推荐用法）**
    不要把本服务做成开机自启/常驻。用 --idle-timeout 让它"用完即走"：

        python serve_ocr.py --backend paddle --idle-timeout 300 ...
        # 300 秒没有新请求就自行退出，释放显存

    批量流程（客户端侧，见 ocr_batch.py 的 --start-cmd/--stop-cmd）：
        拉起服务 → 跑完一批图 → 关闭服务；两批之间 GPU 完全空出。

用法示例（2080Ti GPU 主机）：
    python serve_ocr.py --backend paddle --host 0.0.0.0 --port 8890 \
        --token <secret> --idle-timeout 300 [--engine fast|vl]
客户端（DocConverter 侧）配置：
    ocr: {enabled: true, backend: remote,
          remote: {url: "http://<gpu-host>:8890/ocr", token: "<secret>"}}
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TOKEN = ""
_IDLE_TIMEOUT = 0
_LAST_ACTIVITY = time.time()
_SERVER = None


def _touch():
    global _LAST_ACTIVITY
    _LAST_ACTIVITY = time.time()


def _start_idle_watchdog(timeout: int, server):
    """无请求超过 timeout 秒即关闭服务：GPU 主机不该被一个空闲进程占着显存。"""
    if timeout <= 0:
        return

    def loop():
        while True:
            time.sleep(min(30, max(1, timeout // 4)))
            idle = time.time() - _LAST_ACTIVITY
            if idle >= timeout:
                print("idle %.0fs >= %ds, shutting down" % (idle, timeout),
                      file=sys.stderr, flush=True)
                server.shutdown()
                return

    threading.Thread(target=loop, daemon=True).start()


class OcrBackend:
    name = "base"

    def ready_info(self) -> dict:
        return {"engine": self.name}

    def run(self, image_path: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        """释放引擎资源（GPU 场景必须真正结束子进程，否则显存不归还）"""


class CmdBackend(OcrBackend):
    """外部命令包装：{image} 占位符替换为图片路径，stdout 即识别文本。"""
    name = "cmd"

    def __init__(self, template: str):
        self.template = template

    def run(self, image_path: str) -> str:
        cmd = self.template.replace("{image}", image_path)
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=600, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError("cmd exit {}: {}".format(r.returncode, r.stderr[:300]))
        return (r.stdout or "").strip()


class RapidBackend(OcrBackend):
    name = "rapidocr"

    def __init__(self):
        from rapidocr import RapidOCR  # noqa
        self._eng = RapidOCR()

    def run(self, image_path: str) -> str:
        res = self._eng(image_path)
        txts = getattr(res, "txts", None) or []
        return "\n".join(str(t) for t in txts if str(t).strip())


class PaddleSubprocessBackend(OcrBackend):
    """通过常驻子进程调用 paddleocr venv 里的 worker（JSON-lines 协议）。

    worker 协议兼容 ABC工作台 scripts/paddle_ocr_worker.py：
      -> {"id": n, "image": path}   <- {"id": n, "ok": ..., "text"/"error": ...}
    stderr 一律落日志文件，绝不建管道（防 64KiB 写满死锁）。
    """
    name = "paddle"

    def __init__(self, python: str, worker: str, device: str = "gpu",
                 engine: str = "fast", timeout: int = 600, log: str = ""):
        self.python = python
        self.worker = worker
        self.device = device
        self.engine = engine
        self.timeout = timeout
        self.log_path = log or os.path.expanduser("~/.cache/docconverter/serve_ocr_worker.log")
        self._lock = threading.Lock()
        self._proc = None
        self._q = None
        self._seq = 0
        self.ready_info_dict = {}

    def _start(self):
        import queue as _queue
        if self._proc and self._proc.poll() is None:
            return
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self._proc = subprocess.Popen(
            [self.python, "-u", self.worker, "--device", self.device,
             "--engine", self.engine],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_fh,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._q = _queue.Queue()

        def pump():
            try:
                for line in self._proc.stdout:
                    self._q.put(line)
            except Exception:
                pass
            self._q.put(None)
        threading.Thread(target=pump, daemon=True).start()
        first = self._wait(self.timeout)
        info = json.loads(first)
        if not info.get("ready"):
            raise RuntimeError("worker 未就绪: {}".format(first))
        self.ready_info_dict = info

    def _wait(self, timeout):
        import queue as _queue
        try:
            line = self._q.get(timeout=timeout)
        except _queue.Empty:
            self._kill()
            raise RuntimeError("worker 超时({})，已重启".format(timeout))
        if line is None:
            self._kill()
            raise RuntimeError("worker 已退出")
        return line

    def _kill(self):
        p, self._proc, self._q = self._proc, None, None
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        if p is not None:
            try:
                p.wait(timeout=10)
            except Exception:
                pass

    def close(self) -> None:
        """结束 worker 子进程：GPU 显存随进程退出归还，按需调用必须保证这一步"""
        self._kill()

    def ready_info(self) -> dict:
        return {"engine": "paddle:" + self.engine, "device": self.device,
                **self.ready_info_dict}

    def run(self, image_path: str) -> str:
        with self._lock:
            self._start()
            self._seq += 1
            rid = self._seq
            self._proc.stdin.write(json.dumps({"id": rid, "image": image_path}) + "\n")
            self._proc.stdin.flush()
            line = self._wait(self.timeout)
            out = json.loads(line)
            if out.get("id") != rid:
                raise RuntimeError("响应错位")
            if not out.get("ok"):
                raise RuntimeError(out.get("error", "worker 失败"))
            return out.get("text") or ""


def make_backend(args) -> OcrBackend:
    if args.backend == "cmd":
        return CmdBackend(args.cmd)
    if args.backend == "rapid":
        return RapidBackend()
    if args.backend == "paddle":
        return PaddleSubprocessBackend(args.python, args.worker, args.device,
                                       args.engine, args.worker_timeout)
    raise SystemExit("unknown backend")


class Handler(BaseHTTPRequestHandler):
    backend: OcrBackend = None

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _touch()
        if self.path.rstrip("/") == "/health":
            try:
                self._json(200, {"ok": True, "idle_timeout": _IDLE_TIMEOUT,
                                 **self.backend.ready_info()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        global _TOKEN
        _touch()
        if _TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth != "Bearer " + _TOKEN:
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
        if self.path.rstrip("/") in ("/shutdown", "/exit"):
            # 显式关闭：批量跑完后主动释放 GPU，不必等 idle 超时
            self._json(200, {"ok": True, "message": "shutting down"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path.rstrip("/") != "/ocr":
            self._json(404, {"ok": False, "error": "not found"})
            return
        tmp = None
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n))
            t0 = time.time()
            if req.get("image_b64"):
                fd, tmp = tempfile.mkstemp(suffix=Path(req.get("name", "img.png")).suffix or ".png")
                os.close(fd)
                with open(tmp, "wb") as f:
                    f.write(base64.b64decode(req["image_b64"]))
                path = tmp
            else:
                path = req.get("image", "")
                if not path or not os.path.isfile(path):
                    self._json(400, {"ok": False, "error": "image not found"})
                    return
            text = self.backend.run(path)
            self._json(200, {"ok": True, "text": text,
                             "engine": self.backend.name,
                             "seconds": round(time.time() - t0, 2)})
        except Exception as e:
            self._json(200, {"ok": False, "error": "{}: {}".format(type(e).__name__, e)[:500]})
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="paddle", choices=["paddle", "rapid", "cmd"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--token", default=os.environ.get("OCR_SERVICE_TOKEN", ""))
    ap.add_argument("--python", default="", help="paddle venv python 路径")
    ap.add_argument("--worker", default="", help="paddle_ocr_worker.py 路径")
    ap.add_argument("--device", default="gpu")
    ap.add_argument("--engine", default="fast", choices=["fast", "vl"])
    ap.add_argument("--worker-timeout", type=int, default=600)
    ap.add_argument("--idle-timeout", type=int, default=900,
                    help="无请求超过该秒数自动退出（0=不退）。按需调用建议 300~900")
    ap.add_argument("--cmd", default="", help="backend=cmd 的命令模板，{image} 占位")
    a = ap.parse_args()

    global _TOKEN, _IDLE_TIMEOUT, _SERVER
    _TOKEN = a.token
    _IDLE_TIMEOUT = a.idle_timeout
    Handler.backend = make_backend(a)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    _SERVER = srv
    _touch()
    _start_idle_watchdog(a.idle_timeout, srv)
    print("OCR service on http://{}:{}/ocr (backend={}, idle_timeout={}s)".format(
        a.host, a.port, a.backend, a.idle_timeout), file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        try:
            Handler.backend.close()
        except Exception as e:
            print("backend close failed: %s" % e, file=sys.stderr, flush=True)
        print("OCR service stopped", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
