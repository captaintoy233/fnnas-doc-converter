"""HTM/HTML → Markdown。编码自动检测 + BeautifulSoup + markdownify。"""
import chardet
from bs4 import BeautifulSoup
from markdownify import markdownify as md_func
import re
from . import BaseConverter


class HTMConverter(BaseConverter):
    """HTML → MD (自动检测GBK/UTF-8编码, bs4提取正文)"""

    def supported_extensions(self) -> list:
        return ['.htm', '.html']

    def convert(self, file_path: str, **kwargs) -> str:
        with open(file_path, 'rb') as f:
            raw = f.read()
        enc = chardet.detect(raw).get('encoding', 'utf-8') or 'utf-8'
        html = raw.decode(enc, errors='replace')
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'link']):
            tag.decompose()
        body = soup.find('body') or soup
        md = md_func(str(body), heading_style='ATX', strip=['script', 'style'])
        md = re.sub(r'\n{4,}', '\n\n', md).strip()
        return md
