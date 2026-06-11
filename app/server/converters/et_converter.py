"""ET (WPS表格) → Markdown 表格。用 xlrd 兼容 BIFF/.xls 格式。"""
import xlrd
from . import BaseConverter


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
                cells = [str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)]
                if all(c == '' for c in cells):
                    continue
                parts.append('| ' + ' | '.join(cells) + ' |')
                if r == 0:
                    parts.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
            parts.append('')
        return '\n'.join(parts)
