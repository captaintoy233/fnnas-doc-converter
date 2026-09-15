"""
Document Model 单元测试

测试统一文档模型和 Markdown 序列化器的正确性。
这些测试不依赖外部文件，纯内存构造。
"""
import pytest
import sys
import os

# 添加 app/server 到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app', 'server'))

from model.document import Document, Note, NoteKind
from model.block import Heading, Paragraph, CodeBlock, BlockQuote, ThematicBreak
from model.inline import Text, Bold, Italic, Strikethrough, InlineCode, Link, Image, NoteRef
from model.table import Table, TableRow, TableCell
from model.list import ListBlock, ListItem, MarkerKind
from render.markdown import document_to_markdown


class TestDocumentModel:
    """Document Model 基础测试"""
    
    def test_empty_document(self):
        doc = Document()
        assert doc.is_empty()
        assert document_to_markdown(doc) == ""
    
    def test_document_with_title(self):
        doc = Document(title="测试文档", author="张三")
        md = document_to_markdown(doc)
        assert "# 测试文档" in md
        assert "张三" in md
    
    def test_heading_levels(self):
        doc = Document()
        for level in range(1, 7):
            doc.add_block(Heading(level=level, children=[Text(f"标题 {level}")]))
        
        md = document_to_markdown(doc)
        assert "# 标题 1" in md
        assert "## 标题 2" in md
        assert "### 标题 3" in md
        assert "#### 标题 4" in md
        assert "##### 标题 5" in md
        assert "###### 标题 6" in md
    
    def test_paragraph(self):
        doc = Document()
        doc.add_block(Paragraph(children=[Text("这是一个段落。")]))
        md = document_to_markdown(doc)
        assert "这是一个段落。" in md
    
    def test_inline_formatting(self):
        doc = Document()
        doc.add_block(Paragraph(children=[
            Text("普通文本 "),
            Bold([Text("加粗")]),
            Text(" "),
            Italic([Text("斜体")]),
            Text(" "),
            Strikethrough([Text("删除线")]),
            Text(" "),
            InlineCode("代码"),
        ]))
        md = document_to_markdown(doc)
        assert "**加粗**" in md
        assert "*斜体*" in md
        assert "~~删除线~~" in md
        assert "`代码`" in md
    
    def test_link(self):
        doc = Document()
        doc.add_block(Paragraph(children=[
            Link(url="https://example.com", children=[Text("链接文本")])
        ]))
        md = document_to_markdown(doc)
        assert "[链接文本](https://example.com)" in md
    
    def test_image(self):
        doc = Document()
        doc.add_block(Paragraph(children=[
            Image(src="image.png", alt="图片描述")
        ]))
        md = document_to_markdown(doc)
        assert "![图片描述](image.png)" in md
    
    def test_code_block(self):
        doc = Document()
        doc.add_block(CodeBlock(language="python", content="print('hello')"))
        md = document_to_markdown(doc)
        assert "```python" in md
        assert "print('hello')" in md
        assert "```" in md
    
    def test_blockquote(self):
        doc = Document()
        doc.add_block(BlockQuote(blocks=[
            Paragraph(children=[Text("引用内容")])
        ]))
        md = document_to_markdown(doc)
        assert "> 引用内容" in md
    
    def test_thematic_break(self):
        doc = Document()
        doc.add_block(ThematicBreak())
        md = document_to_markdown(doc)
        assert "---" in md


class TestTableModel:
    """表格模型测试"""
    
    def test_simple_table(self):
        doc = Document()
        table = Table(rows=[
            TableRow(cells=[
                TableCell(children=[Text("姓名")], is_header=True),
                TableCell(children=[Text("年龄")], is_header=True),
            ], is_header=True),
            TableRow(cells=[
                TableCell(children=[Text("张三")]),
                TableCell(children=[Text("25")]),
            ]),
        ])
        doc.add_block(table)
        
        md = document_to_markdown(doc)
        assert "| 姓名 | 年龄 |" in md
        assert "| --- | --- |" in md
        assert "| 张三 | 25 |" in md
    
    def test_table_cell_escaping(self):
        """测试表格单元格特殊字符转义"""
        doc = Document()
        table = Table(rows=[
            TableRow(cells=[
                TableCell(children=[Text("包含|管道符")], is_header=True),
                TableCell(children=[Text("包含\n换行")], is_header=True),
            ], is_header=True),
        ])
        doc.add_block(table)
        
        md = document_to_markdown(doc)
        assert "\\|" in md  # 管道符被转义
        assert "<br>" in md  # 换行转为 <br>
    
    def test_empty_table_skipped(self):
        doc = Document()
        table = Table(rows=[
            TableRow(cells=[TableCell(children=[])]),
        ])
        doc.add_block(table)
        
        md = document_to_markdown(doc)
        assert "|" not in md  # 空表格不输出


class TestListModel:
    """列表模型测试"""
    
    def test_bullet_list(self):
        doc = Document()
        lst = ListBlock(
            marker=MarkerKind.BULLET,
            items=[
                ListItem(children=[Paragraph(children=[Text("项目一")])]),
                ListItem(children=[Paragraph(children=[Text("项目二")])]),
            ]
        )
        doc.add_block(lst)
        
        md = document_to_markdown(doc)
        assert "- 项目一" in md
        assert "- 项目二" in md
    
    def test_numbered_list(self):
        doc = Document()
        lst = ListBlock(
            marker=MarkerKind.NUMBERED,
            start=1,
            items=[
                ListItem(children=[Paragraph(children=[Text("第一步")])]),
                ListItem(children=[Paragraph(children=[Text("第二步")])]),
            ]
        )
        doc.add_block(lst)
        
        md = document_to_markdown(doc)
        assert "1. 第一步" in md
        assert "2. 第二步" in md
    
    def test_task_list(self):
        doc = Document()
        lst = ListBlock(
            marker=MarkerKind.TASK,
            items=[
                ListItem(children=[Paragraph(children=[Text("已完成")])], checked=True),
                ListItem(children=[Paragraph(children=[Text("未完成")])], checked=False),
            ]
        )
        doc.add_block(lst)
        
        md = document_to_markdown(doc)
        assert "- [x] 已完成" in md
        assert "- [ ] 未完成" in md


class TestNotesModel:
    """脚注/尾注模型测试"""
    
    def test_footnote(self):
        doc = Document()
        note_id = doc.add_note(Note(
            id="note1",
            kind=NoteKind.FOOTNOTE,
            blocks=[Paragraph(children=[Text("这是脚注内容")])]
        ))
        doc.add_block(Paragraph(children=[
            Text("正文内容"),
            NoteRef(id=note_id),
        ]))
        
        md = document_to_markdown(doc)
        assert "[^1]" in md
        assert "[^1]: 这是脚注内容" in md


class TestConsecutiveBlankLines:
    """连续空行合并测试"""
    
    def test_multiple_blank_lines_collapsed(self):
        doc = Document()
        doc.add_block(Paragraph(children=[Text("第一段")]))
        doc.add_block(Paragraph(children=[]))  # 空段落
        doc.add_block(Paragraph(children=[]))  # 空段落
        doc.add_block(Paragraph(children=[]))  # 空段落
        doc.add_block(Paragraph(children=[Text("第二段")]))
        
        md = document_to_markdown(doc)
        # 不应有超过 2 个连续换行
        assert "\n\n\n\n" not in md
