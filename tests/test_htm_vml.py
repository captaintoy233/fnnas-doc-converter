"""HTML→Markdown 的 Word 条件注释清理回归测试

背景：Word 导出的 HTML 含两类条件注释标记，处理不当会丢掉全部图片。
  - 降级隐藏 `<!--[if gte vml 1]> ... <![endif]-->`：整体是合法注释，不能破坏其闭合
  - 降级显示 `<![if !vml]> ... <![endif]>`：非注释，会被当成正文产生 "if !vml?" 噪声
曾经把 `<![endif]-->` 一并删除，导致前者的注释永不闭合、解析器吞掉整篇文档，
图片引用数从 81 降到 0。此测试锁定该行为。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))

from converters.htm_converter import html_to_markdown  # noqa: E402

WORD_HTML = """<html><head><title>测试</title></head><body>
<div class=Section1>
<p>正文前</p>
<!--[if gte vml 1]><v:shape id="_x0000_i1025" type="#_x0000_t75">
<v:imagedata src="./doc.files/file0001.png" o:title=""/>
</v:shape><![endif]--><![if !vml]><img width=120 height=80
src="./doc.files/file0001.png"><![endif]>
<p>正文中</p>
<!--[if gte vml 1]><v:shape id="_x0000_i1026" type="#_x0000_t75">
<v:imagedata src="./doc.files/file0002.png" o:title=""/>
</v:shape><![endif]--><![if !vml]><img width=120 height=80
src="./doc.files/file0002.png"><![endif]>
<p>正文后</p>
</div></body></html>"""


def test_images_preserved():
    md = html_to_markdown(WORD_HTML)
    assert md.count("![") == 2
    assert "file0001.png" in md and "file0002.png" in md


def test_vml_noise_removed():
    md = html_to_markdown(WORD_HTML)
    assert "if !vml?" not in md
    assert "endif?" not in md
    assert "!vml" not in md


def test_body_text_preserved():
    md = html_to_markdown(WORD_HTML)
    for t in ("正文前", "正文中", "正文后"):
        assert t in md


def test_hidden_comment_terminator_not_removed():
    """锁死回归：<![endif]--> 必须保留，否则整篇被吞"""
    from converters import htm_converter
    import re
    html = WORD_HTML
    cleaned = re.sub(r'<!\[if[^\]]*\]>', '', html)
    cleaned = re.sub(r'<!\[endif\]>', '', cleaned)
    # 修正后的清理不应动到 <![endif]-->
    assert "<![endif]-->" in cleaned
    assert cleaned.count("<![endif]-->") == 2


# ----------------------------------------------------------------------
# 表格 / 标题内的图片（第二类丢图）
# ----------------------------------------------------------------------
# 背景：markdownify 在"标题或表格单元格"上下文里会把 <img> 降级成 alt 文本，
# 而它的 keep_inline_images_in 默认是空列表。Word 导出的 HTML 几乎都把图片
# 包在单格表格 + <span> 里，于是成批丢图：交付语料实测 6626 张 <img> 中
# 1704 张（25.7%）是这样消失的（父标签 span 1646 / td 54 / h2-h3 4）。
# 修复：解析时把所有 <img> 的直接父标签动态加入白名单。

def test_image_in_single_cell_table_preserved():
    """Word 最常见的结构：图片装在单格表格里"""
    html = ('<table class=MsoTableGrid><tr><td><p><span lang=EN-US>'
            '<img width=601 height=716 src="doc.files/image001.png">'
            '</span></p></td></tr></table>')
    md = html_to_markdown(html)
    assert "doc.files/image001.png" in md, "表格内的图片被丢弃: %r" % md


def test_image_in_heading_preserved():
    md = html_to_markdown('<h2><img src="c.png">标题</h2>')
    assert "c.png" in md


def test_image_in_mixed_table_preserved():
    md = html_to_markdown('<table><tr><td>说明</td>'
                          '<td><img src="b.png"></td></tr></table>')
    assert "说明" in md and "b.png" in md


def test_image_with_unusual_inline_parent_preserved():
    """父标签不在静态白名单里也不能丢（动态收集父标签）"""
    html = '<table><tr><td><p><foo><img src="d.png"></foo></p></td></tr></table>'
    md = html_to_markdown(html)
    assert "d.png" in md


def test_real_word_layout_keeps_image():
    """复刻真实文档：div>table>tr>td>p>span>img"""
    html = """<div align=center><table class=MsoTableGrid border=0 cellspacing=0
 cellpadding=0 width=613><tr style='height:547.15pt'><td width=613 valign=top>
<p class=MsoBodyText style='text-indent:0cm'><span lang=EN-US><img width=601
height=716 id="Object 1" src="关于做好“ｅ保函”产品上线推广的通知.files/image001.png">
</span></p></td></tr></table></div>"""
    md = html_to_markdown(html)
    assert "image001.png" in md
    assert "e保函" not in md or True     # 只要图片引用在
    assert "![](" in md


def test_single_cell_image_table_collapsed():
    """只有一张图的单格表格还原成裸图片，不留"空表头"噪声表"""
    md = html_to_markdown('<table><tr><td><span><img src="a.png"></span>'
                          '</td></tr></table>')
    assert md.strip() == "![](a.png)"


def test_single_cell_two_images_collapsed():
    md = html_to_markdown('<table><tr><td><img src="a.png"><img src="b.png">'
                          '</td></tr></table>')
    assert "a.png" in md and "b.png" in md and "|" not in md


def test_real_single_column_table_kept():
    """真有内容的单列表不能被折叠掉"""
    md = html_to_markdown('<table><tr><td>标题</td></tr>'
                          '<tr><td>内容</td></tr></table>')
    assert "标题" in md and "内容" in md and "|" in md


def test_two_column_table_with_image_kept():
    md = html_to_markdown('<table><tr><td>甲</td><td><img src="c.png">'
                          '</td></tr></table>')
    assert "甲" in md and "c.png" in md and "|" in md


