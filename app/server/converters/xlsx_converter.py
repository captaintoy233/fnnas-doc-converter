"""XLSX → Markdown 表格。用 openpyxl。"""
import openpyxl
from . import BaseConverter


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
                cells = [str(c).strip() if c is not None else '' for c in row]
                if all(c == '' for c in cells):
                    continue
                parts.append('| ' + ' | '.join(cells) + ' |')
                if ri == 0:
                    parts.append('| ' + ' | '.join(['---'] * len(cells)) + ' |')
            parts.append('')
        return '\n'.join(parts)
