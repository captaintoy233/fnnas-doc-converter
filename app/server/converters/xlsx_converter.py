"""XLSX → Markdown 表格。用 openpyxl。

改进点: 单元格转义 `|`、数值格式化（去掉无意义 .0）、日期友好输出。
"""
import openpyxl
from datetime import datetime, date
from . import BaseConverter


def _fmt(cell_value):
    """格式化单元格值为可读文本"""
    if cell_value is None:
        return ""
    if isinstance(cell_value, float) and cell_value.is_integer():
        return str(int(cell_value))
    if isinstance(cell_value, (datetime, date)):
        try:
            return cell_value.strftime("%Y-%m-%d %H:%M" if isinstance(cell_value, datetime) else "%Y-%m-%d")
        except Exception:
            return str(cell_value)
    s = str(cell_value).strip()
    s = s.replace('\\', '\\\\').replace('|', '\\|')
    s = s.replace('\r\n', '<br>').replace('\n', '<br>')
    return s


class XLSXConverter(BaseConverter):
    """XLSX → Markdown表格 (openpyxl)"""

    def supported_extensions(self) -> list:
        return ['.xlsx']

    def convert(self, file_path: str, **kwargs) -> str:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        parts = []
        for sn in wb.sheetnames:
            ws = wb[sn]
            parts.append(f'# {sn}\n')
            for ri, row in enumerate(ws.iter_rows(values_only=True)):
                cells = [_fmt(c) for c in row]
                if all(c == '' for c in cells):
                    continue
                parts.append('| ' + ' | '.join(cells) + ' |')
                if ri == 0:
                    parts.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
            parts.append('')
        return '\n'.join(parts)
