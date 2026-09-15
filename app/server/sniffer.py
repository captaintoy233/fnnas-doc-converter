"""
真实格式嗅探器: 通过文件魔数(magic bytes)识别真实格式，
解决扩展名与内容不符的问题（如 .wps 实为 OOXML、.doc 实为 RTF 等）。

借鉴 AnyDoc 的 content-based detection 设计：
- PDF header (%PDF-)
- RTF open group ({\\rtf)
- OLE stream names (DOC/XLS/PPT/WPS)
- ZIP package mimetype/content types (DOCX/PPTX/XLSX/ODT/EPUB)
- EPUB mimetype entry
"""
from pathlib import Path


def _sniff_zip(f) -> tuple:
    """ZIP 容器: 用内部结构区分 docx/xlsx/pptx/ofd/odt/ods/odp/epub"""
    try:
        import zipfile
        zf = zipfile.ZipFile(f)
        names = zf.namelist()
        
        # 尝试读取 mimetype（EPUB 规范要求第一个条目是无压缩的 "application/epub+zip"）
        epub_mimetype = None
        try:
            info = zf.getinfo("mimetype")
            if info.compress_type == 0:  # 无压缩
                epub_mimetype = zf.read("mimetype").decode("utf-8", errors="ignore").strip()
        except (KeyError, UnicodeDecodeError):
            pass
        
        zf.close()
    except Exception:
        return '.zip', 'ZIP'

    # OFD
    if 'OFD.xml' in names or any(n.startswith('Doc_') and n.endswith('Document.xml') for n in names):
        return '.ofd', 'OFD'
    
    # EPUB（优先检查 mimetype）
    if epub_mimetype == "application/epub+zip":
        return '.epub', 'EPUB'
    
    # OOXML / OpenDocument
    if any(n == '[Content_Types].xml' for n in names):
        if any(n.startswith('word/') for n in names):
            return '.docx', 'DOCX'
        if any(n.startswith('xl/') for n in names):
            return '.xlsx', 'XLSX'
        if any(n.startswith('ppt/') for n in names):
            return '.pptx', 'PPTX'
        return '.docx', 'DOCX'
    
    # OpenDocument (ODT/ODS/ODP) - 使用 META-INF/manifest.xml
    if 'META-INF/manifest.xml' in names:
        try:
            manifest = zf.read("META-INF/manifest.xml").decode("utf-8", errors="ignore")
            if 'application/vnd.oasis.opendocument.text' in manifest:
                return '.odt', 'ODT'
            if 'application/vnd.oasis.opendocument.spreadsheet' in manifest:
                return '.ods', 'ODS'
            if 'application/vnd.oasis.opendocument.presentation' in manifest:
                return '.odp', 'ODP'
        except Exception:
            pass
    
    return '.zip', 'ZIP'


def _sniff_cfb(f) -> tuple:
    """OLE2 复合文档: 读取目录流区分 doc/xls/ppt/wps/et/dps"""
    try:
        import olefile
        ole = olefile.OleFileIO(f)
        streams = ole.listdir()
        ole.close()
    except Exception:
        return '.ole', 'OLE2'

    flat = ['/'.join(s) for s in streams]
    if 'Workbook' in flat or 'Book' in flat:
        return '.xls', 'XLS'
    if 'WordDocument' in flat:
        if 'Data' in flat:
            return '.wps', 'WPS'
        return '.doc', 'DOC'
    if 'PowerPoint Document' in flat:
        return '.ppt', 'PPT'
    return '.ole', 'OLE2'


def sniff_format(path) -> tuple:
    """嗅探文件真实格式

    Returns:
        (ext, name): 如 ('.docx', 'DOCX')；无法识别时回退扩展名
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(4096)
            if not head:
                return (Path(path).suffix.lower() or '.bin', 'UNKNOWN')

            # CHM (ITS 格式)
            if head[:4] == b'ITSF':
                return '.chm', 'CHM'

            # PDF
            if head[:5] == b'%PDF-':
                return '.pdf', 'PDF'

            # ZIP 系 (OOXML / OFD)
            if head[:4] in (b'PK\x03\x04', b'PK\x05\x06'):
                return _sniff_zip(f)

            # OLE2 复合文档 (doc/xls/ppt/wps/et/dps)
            if head[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
                return _sniff_cfb(f)

            # RTF (完整签名检查)
            if head[:5] == b'{\\rtf':
                return '.rtf', 'RTF'

            # HTML (多种变体)
            head_lower = head[:16].lower()
            if head_lower.startswith(b'<!doctype html') or \
               head_lower.startswith(b'<html') or \
               head_lower.startswith(b'<?xml') and b'<html' in head_lower:
                return '.html', 'HTML'
            
            # EML (邮件格式，以常见头字段开头)
            if head[:5].lower() in (b'from ', b'retur', b'deliv', b'recei', b'mime-'):
                return '.eml', 'EML'
            
            # MSG (OLE2 格式的 Outlook 邮件，已在 _sniff_cfb 中处理)
            
            # CSV (无魔数，但可检测纯文本+逗号分隔特征)
            # 注意：CSV 没有可靠签名，这里仅做启发式检测
            if b'\x00' not in head[:256]:  # 非二进制
                try:
                    text = head[:256].decode('utf-8', errors='ignore')
                    lines = text.split('\n')
                    if len(lines) >= 2:
                        # 检查是否多行都有逗号分隔
                        comma_counts = [line.count(',') for line in lines[:3] if line.strip()]
                        if comma_counts and min(comma_counts) > 0 and max(comma_counts) == min(comma_counts):
                            return '.csv', 'CSV'
                except Exception:
                    pass

            # 二进制未知
            if b'\x00' in head[:64]:
                return (Path(path).suffix.lower() or '.bin', 'BINARY')

            # 纯文本
            return '.txt', 'TXT'
    except Exception:
        return (Path(path).suffix.lower() or '.bin', 'UNKNOWN')


def sniff_extension(path) -> str:
    """返回嗅探出的扩展名（含点），用于路由到正确的转换器"""
    ext, _ = sniff_format(path)
    return ext


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        e, n = sniff_format(p)
        print(f"{p}: {n} ({e})")
