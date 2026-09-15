"""
质量评估 CLI

用法:
    python quality_cli.py <markdown_file_or_dir> [--format docx] [--json]

示例:
    # 评估单个文件
    python quality_cli.py output/report.md
    
    # 评估整个输出目录
    python quality_cli.py /data/output/ --json
    
    # 指定源格式（影响评分权重）
    python quality_cli.py output/ --format pdf
"""
import argparse
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from quality import assess_quality, batch_assess


def main():
    parser = argparse.ArgumentParser(description="Markdown 转换质量评估")
    parser.add_argument("path", help="Markdown 文件或目录路径")
    parser.add_argument("--format", "-f", default=None, help="源文件格式（如 docx, pdf）")
    parser.add_argument("--json", "-j", action="store_true", help="JSON 格式输出")
    parser.add_argument("--threshold", "-t", type=float, default=50.0,
                        help="低质量阈值（默认 50），低于此分数会标记警告")
    args = parser.parse_args()
    
    target = Path(args.path)
    
    if target.is_file():
        # 单文件评估
        md = target.read_text(encoding="utf-8", errors="replace")
        source_size = target.stat().st_size
        result = assess_quality(md, source_format=args.format, source_size=source_size)
        
        if args.json:
            print(json.dumps({
                "file": str(target),
                **result.__dict__,
            }, indent=2, ensure_ascii=False))
        else:
            print(f"📄 {target}")
            print(f"   {result.summary()}")
            for w in result.warnings:
                print(f"   ⚠ {w}")
            if result.score < args.threshold:
                print(f"   ❌ Below threshold ({args.threshold})")
            else:
                print(f"   ✅ Above threshold ({args.threshold})")
    
    elif target.is_dir():
        # 批量评估
        md_files = sorted(target.rglob("*.md"))
        if not md_files:
            print(f"No .md files found in {target}")
            sys.exit(1)
        
        results = []
        for f in md_files:
            md = f.read_text(encoding="utf-8", errors="replace")
            source_size = f.stat().st_size
            fmt = args.format or f.suffix.lstrip(".")
            r = assess_quality(md, source_format=fmt, source_size=source_size)
            results.append((str(f.relative_to(target)), fmt, r))
        
        summary = batch_assess(results)
        
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(f"📊 Quality Report for {target}")
            print(f"   Files: {summary['count']}")
            print(f"   Average: {summary['avg_score']}/100")
            print(f"   Range: {summary['min_score']} - {summary['max_score']}")
            print(f"   Median: {summary['median_score']}")
            print()
            
            if summary.get("by_format"):
                print("   By format:")
                for fmt, stats in summary["by_format"].items():
                    print(f"     {fmt}: avg={stats['avg']}, count={stats['count']}")
                print()
            
            low = summary.get("low_quality_files", [])
            if low:
                print(f"   ❌ Low quality (< {args.threshold}):")
                for path, fmt, score in low:
                    print(f"     {path} ({fmt}): {score}")
            else:
                print(f"   ✅ All files above threshold ({args.threshold})")
    else:
        print(f"Path not found: {target}")
        sys.exit(1)


if __name__ == "__main__":
    main()
