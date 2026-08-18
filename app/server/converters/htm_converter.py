"""HTM/HTML → Markdown。编码自动检测 + BeautifulSoup + markdownify。"""
import chardet
from bs4 import BeautifulSoup
from markdownify import markdownify as md_func
import re
from . import BaseConverter


def html_to_markdown(html: str, base_title: str = "") -> str:
    """将 HTML 字符串转换为 Markdown（供 EML 等复用）"""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'link']):
        tag.decompose()
    title = ""
    title_tag = soup.find('title')
    if title_tag:
        title = title_tag.get_text(strip=True)
    body = soup.find('body') or soup
    md = md_func(str(body), heading_style='ATX', strip=['script', 'style'])
    md = re.sub(r'\n{4,}', '\n\n', md).strip()
    if (title or base_title) and not md.startswith('# '):
        md = '# {}\n\n{}'.format(title or base_title, md)
    return md


class HTMConverter(BaseConverter):
    """HTML → MD (自动检测GBK/UTF-8编码, bs4提取正文+标题)"""

    def supported_extensions(self) -> list:
        return ['.htm', '.html']

    def convert(self, file_path: str, **kwargs) -> str:
        with open(file_path, 'rb') as f:
            raw = f.read()
        enc = chardet.detect(raw).get('encoding', 'utf-8') or 'utf-8'
        html = raw.decode(enc, errors='replace')
        return html_to_markdown(html)
