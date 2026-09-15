"""
质量评估 Pipeline

对转换后的 Markdown 进行规则评分，跟踪转换质量变化。
评分维度（0-100）:
- completeness: 内容完整性（非空、长度合理）
- structure: 结构质量（标题层级、段落分隔）
- formatting: 格式规范（表格对齐、列表标记）
- cleanliness: 清洁度（无乱码、无多余空白）

用法:
    from quality import assess_quality
    result = assess_quality(markdown_text, source_format="docx")
    print(result.score, result.details)
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QualityResult:
    """质量评估结果"""
    score: float                        # 综合得分 (0-100)
    completeness: float                 # 内容完整性
    structure: float                    # 结构质量
    formatting: float                   # 格式规范
    cleanliness: float                  # 清洁度
    details: dict = field(default_factory=dict)  # 各维度详情
    warnings: list = field(default_factory=list)  # 警告信息
    
    def summary(self) -> str:
        return (
            f"Quality: {self.score:.0f}/100 "
            f"(completeness={self.completeness:.0f}, "
            f"structure={self.structure:.0f}, "
            f"formatting={self.formatting:.0f}, "
            f"cleanliness={self.cleanliness:.0f})"
        )


def assess_quality(
    markdown: str,
    source_format: Optional[str] = None,
    source_size: Optional[int] = None,
) -> QualityResult:
    """评估 Markdown 输出质量"""
    if not markdown or not markdown.strip():
        return QualityResult(
            score=0, completeness=0, structure=0,
            formatting=0, cleanliness=0,
            warnings=["Empty output"],
        )
    
    completeness = _assess_completeness(markdown, source_size)
    structure = _assess_structure(markdown)
    formatting = _assess_formatting(markdown)
    cleanliness = _assess_cleanliness(markdown)
    
    # 加权平均
    score = (
        completeness * 0.35 +
        structure * 0.25 +
        formatting * 0.20 +
        cleanliness * 0.20
    )
    
    warnings = []
    if completeness < 50:
        warnings.append("Low completeness: output may be truncated or sparse")
    if structure < 50:
        warnings.append("Poor structure: missing headings or paragraph separation")
    if cleanliness < 50:
        warnings.append("Low cleanliness: garbled text or excessive whitespace detected")
    
    return QualityResult(
        score=round(score, 1),
        completeness=round(completeness, 1),
        structure=round(structure, 1),
        formatting=round(formatting, 1),
        cleanliness=round(cleanliness, 1),
        details={
            "char_count": len(markdown),
            "line_count": markdown.count("\n") + 1,
            "heading_count": len(re.findall(r'^#{1,6}\s', markdown, re.MULTILINE)),
            "table_count": len(re.findall(r'^\|.+\|$', markdown, re.MULTILINE)),
            "list_item_count": len(re.findall(r'^[\-\*\d]+[.)]\s', markdown, re.MULTILINE)),
        },
        warnings=warnings,
    )


def _assess_completeness(md: str, source_size: Optional[int]) -> float:
    """内容完整性评分"""
    score = 100.0
    char_count = len(md.strip())
    
    # 空或极短输出
    if char_count == 0:
        return 0.0
    if char_count < 10:
        return 10.0
    if char_count < 50:
        score -= 30
    
    # 与源文件大小比较（如果提供）
    if source_size and source_size > 0:
        ratio = char_count / source_size
        if ratio < 0.01:
            score -= 40  # 输出不到源文件 1%
        elif ratio < 0.05:
            score -= 20
        elif ratio > 10:
            score -= 10  # 输出异常膨胀
    
    # 检查是否有实质内容（非纯标记）
    text_only = re.sub(r'[#*|\-\[\]>`~\n\r\t ]', '', md)
    if len(text_only) < char_count * 0.3:
        score -= 20  # 大部分是标记符号
    
    return max(0, min(100, score))


def _assess_structure(md: str) -> float:
    """结构质量评分"""
    score = 100.0
    lines = md.split('\n')
    
    # 有标题加分
    headings = re.findall(r'^(#{1,6})\s+(.+)$', md, re.MULTILINE)
    if not headings:
        score -= 30  # 无标题
    else:
        # 检查标题层级连续性
        levels = [len(h[0]) for h in headings]
        for i in range(1, len(levels)):
            if levels[i] > levels[i-1] + 1:
                score -= 5  # 跳级
        
        # 检查标题不为空
        empty_headings = [h for h in headings if not h[1].strip()]
        if empty_headings:
            score -= 10 * len(empty_headings)
    
    # 段落分隔
    para_breaks = len(re.findall(r'\n\n', md))
    if para_breaks == 0 and len(lines) > 5:
        score -= 20  # 长文档无段落分隔
    
    # 表格结构
    table_lines = re.findall(r'^\|.+\|$', md, re.MULTILINE)
    if table_lines:
        # 检查是否有分隔行
        sep_lines = re.findall(r'^\|[\s\-:|]+\|$', md, re.MULTILINE)
        if not sep_lines:
            score -= 15  # 表格缺分隔行
    
    return max(0, min(100, score))


def _assess_formatting(md: str) -> float:
    """格式规范评分"""
    score = 100.0
    
    # 表格列数一致性
    table_rows = re.findall(r'^(\|.+\|)$', md, re.MULTILINE)
    if table_rows:
        col_counts = [row.count('|') - 1 for row in table_rows]
        if col_counts:
            most_common = max(set(col_counts), key=col_counts.count)
            inconsistent = sum(1 for c in col_counts if c != most_common)
            if inconsistent > 0:
                score -= min(30, inconsistent * 5)
    
    # 未闭合的代码块
    code_fences = len(re.findall(r'^```', md, re.MULTILINE))
    if code_fences % 2 != 0:
        score -= 20  # 奇数个代码围栏
    
    # 链接格式
    broken_links = re.findall(r'\[[^\]]*\]\([^)]*$', md, re.MULTILINE)
    if broken_links:
        score -= 10 * len(broken_links)
    
    # 列表格式
    bad_list_items = re.findall(r'^[\-\*]\S', md, re.MULTILINE)
    if bad_list_items:
        score -= min(15, len(bad_list_items) * 3)
    
    return max(0, min(100, score))


def _assess_cleanliness(md: str) -> float:
    """清洁度评分"""
    score = 100.0
    
    # 乱码检测（Unicode replacement character）
    replacement_chars = md.count('\ufffd')
    if replacement_chars > 0:
        score -= min(40, replacement_chars * 5)
    
    # 控制字符（除换行/制表符外）
    control_chars = len(re.findall(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', md))
    if control_chars > 0:
        score -= min(30, control_chars * 3)
    
    # 过多连续空行
    excessive_blanks = re.findall(r'\n{5,}', md)
    if excessive_blanks:
        score -= min(20, len(excessive_blanks) * 5)
    
    # 尾部空白
    trailing_ws = len(re.findall(r'[ \t]+$', md, re.MULTILINE))
    total_lines = md.count('\n') + 1
    if total_lines > 0 and trailing_ws / total_lines > 0.3:
        score -= 15
    
    # HTML 残留标签
    html_tags = re.findall(r'<(?!br\s*/?>)(?!/br)[a-zA-Z][^>]*>', md)
    if html_tags:
        score -= min(20, len(html_tags) * 2)
    
    return max(0, min(100, score))


def batch_assess(results: list[tuple[str, str, QualityResult]]) -> dict:
    """批量评估汇总统计
    
    Args:
        results: [(file_path, format, QualityResult), ...]
    
    Returns:
        汇总统计字典
    """
    if not results:
        return {"count": 0}
    
    scores = [r.score for _, _, r in results]
    by_format: dict[str, list[float]] = {}
    for _, fmt, r in results:
        by_format.setdefault(fmt, []).append(r.score)
    
    return {
        "count": len(results),
        "avg_score": round(sum(scores) / len(scores), 1),
        "min_score": round(min(scores), 1),
        "max_score": round(max(scores), 1),
        "median_score": round(sorted(scores)[len(scores) // 2], 1),
        "by_format": {
            fmt: {
                "count": len(s),
                "avg": round(sum(s) / len(s), 1),
            }
            for fmt, s in sorted(by_format.items())
        },
        "low_quality_files": [
            (path, fmt, r.score)
            for path, fmt, r in results
            if r.score < 50
        ],
    }
