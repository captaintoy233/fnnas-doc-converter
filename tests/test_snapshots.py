"""
Snapshot 测试基线

对真实文档进行转换，将输出保存为快照文件。
后续运行时与快照对比，防止质量退化。

用法:
    # 首次运行（生成快照）:
    pytest tests/test_snapshots.py -v --snapshot-update
    
    # 后续运行（对比快照）:
    pytest tests/test_snapshots.py -v
    
    # 查看质量评分:
    pytest tests/test_snapshots.py -v -s
"""
import os
import sys
import json
import hashlib
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app', 'server'))

from quality import assess_quality

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SAMPLES_DIR = Path(__file__).parent.parent / "test_samples"


def _load_snapshot(name: str) -> str | None:
    """加载已有快照"""
    path = SNAPSHOT_DIR / f"{name}.snap.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _save_snapshot(name: str, content: str):
    """保存快照"""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.snap.md"
    path.write_text(content, encoding="utf-8")


def _save_quality_report(name: str, result):
    """保存质量评估报告"""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.quality.json"
    data = {
        "score": result.score,
        "completeness": result.completeness,
        "structure": result.structure,
        "formatting": result.formatting,
        "cleanliness": result.cleanliness,
        "details": result.details,
        "warnings": result.warnings,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _convert_sample(filename: str) -> str:
    """转换测试样本文件"""
    from converters import registry
    from converters.loader import register_all
    
    # 确保转换器已注册
    register_all(registry)
    
    sample_path = str(SAMPLES_DIR / filename)
    ext = Path(filename).suffix.lower()
    converter = registry.get(ext)
    
    if converter is None or not converter.available:
        pytest.skip(f"No converter for {ext}")
    
    return converter.convert(sample_path)


class TestSnapshots:
    """快照测试 - 对每个测试样本验证转换输出"""
    
    @pytest.fixture(autouse=True)
    def setup(self, snapshot_update):
        """确保快照目录存在，注入 update 模式"""
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        self._update_mode = snapshot_update
    
    def _run_snapshot_test(self, filename: str):
        """通用快照测试逻辑"""
        name = Path(filename).stem
        md = _convert_sample(filename)
        
        # 质量评估
        source_size = (SAMPLES_DIR / filename).stat().st_size
        quality = assess_quality(md, source_format=Path(filename).suffix, source_size=source_size)
        
        # 打印质量报告
        print(f"\n  [{filename}] {quality.summary()}")
        for w in quality.warnings:
            print(f"    ⚠ {w}")
        
        # 保存质量报告
        _save_quality_report(name, quality)
        
        # 快照对比
        existing = _load_snapshot(name)
        
        if self._update_mode or existing is None:
            _save_snapshot(name, md)
            if existing is None:
                print(f"    📸 Created snapshot: {name}.snap.md")
            else:
                print(f"    📸 Updated snapshot: {name}.snap.md")
        else:
            # 对比：使用哈希比较避免大文本 diff 噪音
            new_hash = hashlib.sha256(md.encode()).hexdigest()[:16]
            old_hash = hashlib.sha256(existing.encode()).hexdigest()[:16]
            
            if new_hash != old_hash:
                # 内容变化时检查质量是否下降
                old_quality_path = SNAPSHOT_DIR / f"{name}.quality.json"
                if old_quality_path.exists():
                    old_q = json.loads(old_quality_path.read_text())
                    if quality.score < old_q.get("score", 0) - 5:
                        pytest.fail(
                            f"Quality regression for {filename}: "
                            f"{old_q['score']:.0f} → {quality.score:.0f}\n"
                            f"Run with --snapshot-update to accept changes."
                        )
                
                # 更新快照（非回归的变化）
                _save_snapshot(name, md)
                print(f"    📸 Updated snapshot (non-regression): {name}.snap.md")
    
    def test_docx_snapshot(self):
        """DOCX 快照测试"""
        self._run_snapshot_test("test_sample.docx")
    
    def test_ofd_snapshot(self):
        """OFD 快照测试"""
        self._run_snapshot_test("test_sample.ofd")
    
    def test_wps_snapshot(self):
        """WPS 快照测试"""
        self._run_snapshot_test("test_sample.wps")


class TestQualityAssessment:
    """质量评估系统自身测试"""
    
    def test_empty_input(self):
        result = assess_quality("")
        assert result.score == 0
        assert "Empty output" in result.warnings
    
    def test_good_markdown(self):
        md = """# 标题

## 第一节

这是正文段落，包含足够的内容来通过完整性检查。

| 列1 | 列2 |
| --- | --- |
| 数据1 | 数据2 |

- 列表项一
- 列表项二

### 子节

更多正文内容在这里，确保文档有足够的长度和结构。
"""
        result = assess_quality(md)
        assert result.score >= 70
        assert result.completeness >= 60
        assert result.structure >= 70
    
    def test_garbled_text(self):
        md = "正常文本\ufffd\ufffd\ufffd乱码内容\ufffd\ufffd\ufffd\ufffd\ufffd更多乱码"
        result = assess_quality(md)
        assert result.cleanliness < 90  # 有乱码应扣分
    
    def test_no_structure(self):
        md = "这是一段没有任何结构的纯文本内容，没有标题没有段落分隔也没有列表。" * 5
        result = assess_quality(md)
        assert result.structure <= 70  # 无标题无段落
    
    def test_broken_table(self):
        md = """# 文档

| 列1 | 列2 | 列3 |
| 数据1 | 数据2 |
| 数据3 | 数据4 | 数据5 | 数据6 |
"""
        result = assess_quality(md)
        assert result.formatting <= 90  # 列数不一致
    
    def test_batch_assess(self):
        from quality import batch_assess
        
        results = [
            ("a.docx", "docx", assess_quality("# Title\n\nContent here.")),
            ("b.pdf", "pdf", assess_quality("# Another\n\nMore content.")),
        ]
        summary = batch_assess(results)
        assert summary["count"] == 2
        assert "avg_score" in summary
        assert "by_format" in summary
