"""ET (WPS表格) → Markdown 表格。用 xlrd 兼容 BIFF/.xls 格式。

架构改进（v2 - Document Model）:
- 先解析为统一 Document Model，再通过共享序列化器输出 Markdown
- Sheet 名称映射为 Heading(1)，行数据映射为 Table with TableCell/TableRow
- 单元格转义由共享 Markdown 序列化器处理
- 工作簿打开失败时抛出 MalformedDocumentError

向后兼容:
- convert() 方法保持原有签名和返回类型
- 新增 convert_to_document() 返回 Document Model
"""
import xlrd
from . import BaseConverter

# Document Model 导入
from model.document import Document
from model.block import Heading, Table as MdTable
from model.inline import Text
from model.table import TableRow, TableCell
from render.markdown import document_to_markdown
from errors import MalformedDocumentError


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
    # Note: pipe/backslash escaping is now handled by the shared Markdown renderer
    s = s.replace('\r\n', '\n').replace('\r', '\n')
    return s


class ETConverter(BaseConverter):
    """ET表格 → MD (xlrd读Workbook流, 兼容BIFF)"""

    def supported_extensions(self) -> list:
        return ['.et']

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        try:
            wb = xlrd.open_workbook(file_path)
        except Exception as e:
            raise MalformedDocumentError(
                f"Failed to open ET workbook: {e}",
                file_path=file_path
            ) from e

        result = Document()

        for sn in wb.sheet_names():
            ws = wb.sheet_by_name(sn)

            # Sheet name as Heading(1)
            result.add_block(Heading(level=1, children=[Text(sn)]))

            # Build table rows
            rows = []
            for r in range(ws.nrows):
                cells = []
                for c in range(ws.ncols):
                    text = _fmt(ws.cell(r, c))
                    cells.append(TableCell(
                        children=[Text(text)] if text else [],
                        is_header=(r == 0)
                    ))

                # Skip fully empty rows
                if all(not cell.text.strip() for cell in cells):
                    continue

                rows.append(TableRow(cells=cells, is_header=(r == 0)))

            if rows:
                result.add_block(MdTable(rows=rows))

        return result

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串（向后兼容接口）"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)
