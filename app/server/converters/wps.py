"""WPS (WPS文字) → Markdown。OLE2 + UTF-16LE，零外部依赖。"""
import olefile, re
from . import BaseConverter


class WPSConverter(BaseConverter):
    """WPS文字 → MD (OLE2 WordDocument 流)"""

    def supported_extensions(self) -> list:
        return ['.wps']

    def convert(self, file_path: str, **kwargs) -> str:
        ole = olefile.OleFileIO(file_path)
        wd = ole.openstream('WordDocument').read()
        ole.close()
        text = wd.decode('utf-16-le', errors='replace')
        start = re.search(r'[\u4e00-\u9fff]{4,}', text)
        text = text[start.start():] if start else text
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()
