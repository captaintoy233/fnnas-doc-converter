"""
DocConverter 命令行工具

用法示例:
  python cli.py scan [源目录]                    # 扫描可转换文件
  python cli.py convert <文件>                   # 单文件转 Markdown (输出到 stdout)
  python cli.py convert <文件> -o 输出.md        # 单文件转换写文件
  python cli.py batch [--source DIR] [--no-push] # 批量转换（增量跳过）
  python cli.py watch [--source DIR]             # 前台监听并自动转换
  python cli.py push <md文件> [--title 标题]      # 推送 Markdown 到 WeKnora
  python cli.py push-file <原文件> [--title 标题] # 推送原始文件到 WeKnora
  python cli.py test-weknora                     # 测试 WeKnora 连接
"""
import argparse
import logging
import sys
import time
from pathlib import Path

from config import get_config, reload_config
from converters.loader import register_all
from converters import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("docconverter.cli")


def _register():
    register_all(registry, get_config())


def cmd_scan(args):
    from scanner import scan_directory, get_statistics
    files = scan_directory(source_dir=args.source)
    stats = get_statistics(files)
    print("源目录: {}".format(args.source or get_config()["scanner"].get("source_dir")))
    print("文件数: {} ({})".format(stats["total"], stats["total_size_str"]))
    for f in files:
        print("  {}  [{}]  {}".format(f.rel_path, f.ext, f.size_str))


def cmd_convert(args):
    from sniffer import sniff_format
    path = Path(args.file)
    if not path.exists():
        print("文件不存在: {}".format(path), file=sys.stderr)
        return 1
    ext = path.suffix.lower()
    real_ext, _ = sniff_format(str(path))
    converter = registry.get(real_ext if real_ext != ext else ext)
    if not converter:
        print("不支持的格式: {} (嗅探: {})".format(ext, real_ext), file=sys.stderr)
        return 1
    print("转换器: {} (嗅探: {})".format(converter.display_name, real_ext), file=sys.stderr)
    outputs = converter.convert_to_files(str(path), path.name)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 多文件输出时，args.output 作为目录
        if len(outputs) > 1:
            out_path.mkdir(parents=True, exist_ok=True)
            for rel, content in outputs:
                (out_path / rel).parent.mkdir(parents=True, exist_ok=True)
                (out_path / rel).write_text(content, encoding="utf-8")
                print("写入: {}".format(out_path / rel))
        else:
            out_path.write_text(outputs[0][1], encoding="utf-8")
            print("写入: {}".format(out_path))
    else:
        # 单文件输出到 stdout
        for rel, content in outputs:
            print("# 输出: {}\n".format(rel))
            print(content)
    return 0


def cmd_batch(args):
    from scanner import scan_directory
    from batch import start_batch, get_batch_status, _push_queue
    files = scan_directory(source_dir=args.source)
    if not files:
        print("源目录没有可转换的文件", file=sys.stderr)
        return 1
    print("开始批量转换: {} 个文件 (workers={})".format(
        len(files), get_config()["batch"].get("workers", 4)))
    result = start_batch(files, push_to_weknora=None if not args.no_push else False,
                         full_scan=True)
    if isinstance(result, dict) and result.get("error"):
        print("错误: {}".format(result["error"]), file=sys.stderr)
        return 1
    # 等待推送队列消化（若启用了推送）
    if not args.no_push:
        try:
            _push_queue.join()
        except KeyboardInterrupt:
            pass
    status = get_batch_status()
    print("完成: 成功 {} / 失败 {} / 跳过 {}".format(
        status["statistics"].get("success", 0),
        status["statistics"].get("failed", 0),
        status["statistics"].get("skipped", 0)))
    return 0


def cmd_watch(args):
    from watcher import start_watcher, stop_watcher, get_watcher_status
    from batch import start_batch

    def on_new_files(files):
        logger.info("监控到 %d 个新文件，开始转换", len(files))
        start_batch(files)

    result = start_watcher(on_new_files=on_new_files)
    print("监听已启动 (Ctrl+C 停止)")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        stop_watcher()
        print("\n已停止")


def cmd_push(args):
    from weknora_client import WeKnoraClient
    client = WeKnoraClient()
    if not client.is_configured():
        print("WeKnora 未配置", file=sys.stderr)
        return 1
    title = args.title or Path(args.file).stem
    content = Path(args.file).read_text(encoding="utf-8")
    result = client.push_manual(title=title, content=content, source_path=str(Path(args.file)))
    print(result)
    return 0 if result.get("ok") else 1


def cmd_push_file(args):
    from weknora_client import WeKnoraClient
    client = WeKnoraClient()
    if not client.is_configured():
        print("WeKnora 未配置", file=sys.stderr)
        return 1
    title = args.title or Path(args.file).stem
    result = client.push_file(args.file, title=title)
    print(result)
    return 0 if result.get("ok") else 1


def cmd_verify(args):
    from weknora_client import WeKnoraClient
    from registry import get_registry
    client = WeKnoraClient()
    if not client.is_configured():
        print("WeKnora 未配置", file=sys.stderr)
        return 1
    if args.doc_id:
        print(client.verify_knowledge(args.doc_id))
        return 0
    # 对账全部已推送文档
    reg = get_registry()
    total = completed = failed = pending = 0
    for src, rec in reg.to_dict().items():
        out_docs = rec.get("output_docs") or {}
        ids = [v for v in out_docs.values() if v]
        if not ids and rec.get("weknora_doc_id"):
            ids = str(rec["weknora_doc_id"]).split(",")
        for doc_id in ids:
            if not doc_id:
                continue
            total += 1
            info = client.verify_knowledge(doc_id)
            st = info.get("parse_status", "")
            if st == "completed":
                completed += 1
            elif st == "failed":
                failed += 1
                print("  ✗ {} <- {} ({})".format(doc_id, src,
                                                 info.get("error_message", "")))
            else:
                pending += 1
    print("对账完成: 共 {} 条，completed={} failed={} 其它/进行中={}".format(
        total, completed, failed, pending))
    return 0 if failed == 0 else 1


def cmd_mappings(args):
    import folder_mappings
    if args.set:
        # --set "工作邮件=kb-id,报表数据=kb-2"
        mappings = {}
        for pair in args.set.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k.strip() and v.strip():
                    mappings[k.strip()] = v.strip()
        data = folder_mappings.save_mappings(mappings)
        if data is None:
            print("保存失败", file=sys.stderr)
            return 1
        print("已保存映射:", data["mappings"])
        return 0
    for k, v in folder_mappings.get_mappings().items():
        print("  {} -> {}".format(k, v))
    return 0


def cmd_reparse(args):
    from weknora_client import WeKnoraClient
    client = WeKnoraClient()
    if not client.is_configured():
        print("WeKnora 未配置", file=sys.stderr)
        return 1
    if args.doc_id:
        print(client.trigger_reparse(args.doc_id))
        return 0
    # 对账 failed 文档并全部 reparse
    from registry import get_registry
    reg = get_registry()
    done = failed = 0
    for src, rec in reg.to_dict().items():
        out_docs = rec.get("output_docs") or {}
        ids = [v for v in out_docs.values() if v]
        if not ids and rec.get("weknora_doc_id"):
            ids = str(rec["weknora_doc_id"]).split(",")
        for doc_id in ids:
            if not doc_id:
                continue
            info = client.verify_knowledge(doc_id)
            if info.get("parse_status") == "failed":
                r = client.trigger_reparse(doc_id)
                if r.get("ok"):
                    done += 1
                    print("  已重试: {} <- {}".format(doc_id, src))
                else:
                    failed += 1
    print("重试完成: 成功 {} 失败 {}".format(done, failed))
    return 0 if failed == 0 else 1


def cmd_test(args):
    from weknora_client import WeKnoraClient
    client = WeKnoraClient()
    if not client.is_configured():
        print("WeKnora 未配置 (weknora.enabled=false 或缺少 URL/凭据)", file=sys.stderr)
        return 1
    result = client.test_connection()
    print(result)
    return 0 if result.get("ok") else 1


def main():
    parser = argparse.ArgumentParser(description="DocConverter 命令行工具")
    parser.add_argument("--reload-config", action="store_true", help="先重载配置文件")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("scan", help="扫描可转换文件")
    p.add_argument("source", nargs="?", default=None, help="源目录")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("convert", help="单文件转换")
    p.add_argument("file")
    p.add_argument("-o", "--output", default=None, help="输出文件/目录")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("batch", help="批量转换")
    p.add_argument("--source", default=None, help="源目录")
    p.add_argument("--no-push", action="store_true", help="不推送 WeKnora")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("watch", help="监听源目录并自动转换")
    p.add_argument("--source", default=None, help="源目录")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("push", help="推送 Markdown 到 WeKnora")
    p.add_argument("file")
    p.add_argument("--title", default=None)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("push-file", help="推送原始文件到 WeKnora")
    p.add_argument("file")
    p.add_argument("--title", default=None)
    p.set_defaults(func=cmd_push_file)

    p = sub.add_parser("test-weknora", help="测试 WeKnora 连接")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("verify", help="校验已推送文档的解析状态（HTTP 200 ≠ 解析成功）")
    p.add_argument("doc_id", nargs="?", default=None, help="单个文档 ID（空则对账全部）")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("mappings", help="查看/设置 folder→知识库映射")
    p.add_argument("--set", default=None,
                   help='设置映射，如 "工作邮件=kb-id,报表数据=kb-2"')
    p.set_defaults(func=cmd_mappings)

    p = sub.add_parser("reparse", help="重试解析（对账 failed 文档或指定 doc_id）")
    p.add_argument("doc_id", nargs="?", default=None, help="单个文档 ID（空则重试全部 failed）")
    p.set_defaults(func=cmd_reparse)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 1
    if args.reload_config:
        reload_config()
    _register()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
