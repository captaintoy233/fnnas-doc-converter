"""ET (WPS表格) → Markdown 表格。用 xlrd 兼容 BIFF/.xls 格式。

改进点: 单元格转义、数值/日期友好格式化。
"""
import xlrd
from . import BaseConverter


def _fmt(cell):
    """格式化 xlrd 单元格"""
    if cell.ctype == xlrd.XL_CELL_EMPTY or cell.ctype == xlrd.XL_CELL_BLANK:
        return ""
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        v = cell.value
        if v.is_integer():
            return str(int(v))
        return repr(v) if not isinstance(v, float) else ('%g' % v)
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            dt = xlrd.xldate_as_datetime(cell.value, cell.book.datemode)
            return dt.strftime("%Y-%m-%d %H:%M" if dt.hour or dt.minute else "%Y-%m-%d")
        except Exception:
            return str(cell.value)
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    s = str(cell.value).strip()
    s = s.replace('\\', '\\\\').replace('|', '\\|')
    s = s.replace('\r\n', '<br>').replace('\n', '<br>')
    return s


class ETConverter(BaseConverter):
    """ET表格 → MD (xlrd读Workbook流, 兼容BIFF)"""

    def supported_extensions(self) -> list:
        return ['.et']

    def convert(self, file_path: str, **kwargs) -> str:
        wb = xlrd.open_workbook(file_path)
        parts = []
        for sn in wb.sheet_names():
            ws = wb.sheet_by_name(sn)
            parts.append(f'# {sn}\n')
            for r in range(ws.nrows):
                cells = [_fmt(ws.cell(r, c)) for c in range(ws.ncols)]
                if all(c == '' for c in cells):
                    continue
                parts.append('| ' + ' | '.join(cells) + ' |')
                if r == 0:
                    parts.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
            parts.append('')
        return '\n'.join(parts)
