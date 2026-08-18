"""
真实格式嗅探器: 通过文件魔数(magic bytes)识别真实格式，
解决扩展名与内容不符的问题（如 .wps 实为 OOXML、.doc 实为 RTF 等）。
"""
from pathlib import Path


def _sniff_zip(f) -> tuple:
    """ZIP 容器: 用内部结构区分 docx/xlsx/pptx/ofd"""
    try:
        import zipfile
        zf = zipfile.ZipFile(f)
        names = zf.namelist()
        zf.close()
    except Exception:
        return '.zip', 'ZIP'

    if 'OFD.xml' in names or any(n.startswith('Doc_') and n.endswith('Document.xml') for n in names):
        return '.ofd', 'OFD'
    if any(n == '[Content_Types].xml' for n in names):
        if any(n.startswith('word/') for n in names):
            return '.docx', 'DOCX'
        if any(n.startswith('xl/') for n in names):
            return '.xlsx', 'XLSX'
        if any(n.startswith('ppt/') for n in names):
            return '.pptx', 'PPTX'
        return '.docx', 'DOCX'
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

            # RTF
            if head[:5] == b'{\\rtf':
                return '.doc', 'DOC(RTF)'

            # HTML
            if head[:4].lower() in (b'<htm', b'<!DO'):
                return '.html', 'HTML'

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
