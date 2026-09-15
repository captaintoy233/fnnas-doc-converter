"""纯文本类转换器测试（.md 透传 / .txt 分段 / .csv 表格）"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "server"))

from converters.text_converter import TextConverter  # noqa: E402


def test_md_passthrough_is_byte_identical(tmp_path):
    """已是 Markdown 的文件必须原样透传，二次渲染会损伤格式"""
    content = "# 标题\n\n- 列表项\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```py\nx=1\n```\n"
    p = tmp_path / "doc.md"
    p.write_text(content, encoding="utf-8")
    assert TextConverter().convert(str(p)) == content


def test_txt_kept_as_text(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("第一行\r\n第二行\r\n\r\n第三段\n", encoding="utf-8")
    out = TextConverter().convert(str(p))
    assert "第一行" in out and "第三行" not in out
    assert "\r" not in out, "换行应归一化"


def test_csv_to_markdown_table(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("姓名,金额\n张三,100\n李四,200\n", encoding="utf-8")
    out = TextConverter().convert(str(p))
    lines = out.strip().split("\n")
    assert lines[0] == "| 姓名 | 金额 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 张三 | 100 |"
    assert lines[3] == "| 李四 | 200 |"


def test_csv_escapes_pipe_and_quotes(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text('a,b\n"含|竖线","含,逗号"\n', encoding="utf-8")
    out = TextConverter().convert(str(p))
    assert "含\\|竖线" in out
    assert "含,逗号" in out


def test_csv_ragged_rows_padded(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("a,b,c\n1,2\n", encoding="utf-8")
    out = TextConverter().convert(str(p))
    assert out.strip().split("\n")[2] == "| 1 | 2 |  |"


def test_gbk_text_decoded(tmp_path):
    p = tmp_path / "gbk.txt"
    p.write_bytes("中文内容测试".encode("gbk"))
    out = TextConverter().convert(str(p))
    assert "中文内容测试" in out


def test_empty_file(tmp_path):
    p = tmp_path / "e.txt"
    p.write_bytes(b"")
    assert TextConverter().convert(str(p)) == ""


def test_registered_and_scannable():
    from converters.loader import register_all
    from converters import registry
    register_all(registry)
    for ext in (".md", ".txt", ".csv"):
        assert registry.is_available(ext), ext
        assert registry.get(ext).__class__.__name__ == "TextConverter"


def test_output_paths_do_not_collide(tmp_path):
    """同名的 .md/.txt/.csv 必须产出不同文件，否则互相覆盖"""
    conv = TextConverter()
    pairs = [("报告.md", "报告.md"), ("报告.txt", "报告.txt.md"),
             ("报告.csv", "报告.csv.md")]
    outs = []
    for rel, expect in pairs:
        p = tmp_path / rel
        p.write_text("x", encoding="utf-8")
        got = conv.convert_to_files(str(p), "子目录/" + rel)
        assert got[0][0] == "子目录/" + expect, (rel, got[0][0])
        outs.append(got[0][0])
    assert len(set(outs)) == 3, "三个输出路径必须互不相同"
