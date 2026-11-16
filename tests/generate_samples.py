# -*- coding: utf-8 -*-
"""生成合成测试样例 test_sample.{docx,ofd,wps}（不含任何真实业务内容）。

公开仓库不附带真实业务文档；本脚本以纯虚构内容重建三种格式的测试夹具，
仍覆盖转换器关键路径：DOCX 标题/段落/无序与有序列表/表格；OFD 字体→标题/正文；
WPS OLE2 WordDocument 流 FIB(fcMin/fcMac) 正文定位。

用法： python tests/generate_samples.py
"""
import os, struct, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "test_samples"


# ---------------- WPS: 最小 OLE2/CFB（WordDocument + Data 流） ----------------
def _build_cfb(streams):
    SECT, MINI, CUTOFF = 512, 64, 4096
    FREESECT, ENDOFCHAIN, FATSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
    NOSTREAM = 0xFFFFFFFF
    names = list(streams.keys())
    mini_data = bytearray(); mini_start = {}; mini_fat = []
    for nm in names:
        data = streams[nm]
        start = len(mini_data) // MINI
        mini_start[nm] = start
        nsec = (len(data) + MINI - 1) // MINI
        for i in range(nsec):
            mini_fat.append(start + i + 1 if i < nsec - 1 else ENDOFCHAIN)
        mini_data += data + b'\x00' * ((nsec * MINI) - len(data))
    mini_sect_count = (len(mini_data) + SECT - 1) // SECT
    mini_data += b'\x00' * ((mini_sect_count * SECT) - len(mini_data))
    fat = [FATSECT, ENDOFCHAIN, ENDOFCHAIN]
    for i in range(mini_sect_count):
        fat.append(3 + i + 1 if i < mini_sect_count - 1 else ENDOFCHAIN)
    fat += [FREESECT] * (128 - len(fat))

    def dir_entry(name, etype, color, left, right, child, start, size):
        nb = name.encode('utf-16-le') + b'\x00\x00'
        e = bytearray(128)
        e[0:len(nb)] = nb
        struct.pack_into('<H', e, 64, len(nb))
        e[66] = etype; e[67] = color
        struct.pack_into('<III', e, 68, left, right, child)
        struct.pack_into('<I', e, 116, start)
        struct.pack_into('<Q', e, 120, size)
        return bytes(e)

    root = dir_entry("Root Entry", 5, 1, NOSTREAM, NOSTREAM, 1, 3, len(mini_data))
    wd = dir_entry("WordDocument", 2, 1, 2, NOSTREAM, NOSTREAM,
                   mini_start["WordDocument"], len(streams["WordDocument"]))
    da = dir_entry("Data", 2, 0, NOSTREAM, NOSTREAM, NOSTREAM,
                   mini_start["Data"], len(streams["Data"]))
    free = dir_entry("", 0, 0, NOSTREAM, NOSTREAM, NOSTREAM, 0, 0)
    directory = root + wd + da + free
    mf = b''.join(struct.pack('<I', x) for x in mini_fat)
    mf += b'\xff' * ((SECT - len(mf) % SECT) % SECT)
    fat_bytes = b''.join(struct.pack('<I', x) for x in fat)
    h = bytearray(SECT)
    h[0:8] = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    struct.pack_into('<H', h, 24, 0x003E)
    struct.pack_into('<H', h, 26, 3)
    struct.pack_into('<H', h, 28, 0xFFFE)
    struct.pack_into('<H', h, 30, 9)
    struct.pack_into('<H', h, 32, 6)
    struct.pack_into('<I', h, 44, 1)
    struct.pack_into('<I', h, 48, 1)
    struct.pack_into('<I', h, 56, CUTOFF)
    struct.pack_into('<I', h, 60, 2)
    struct.pack_into('<I', h, 64, 1)
    struct.pack_into('<I', h, 68, ENDOFCHAIN)
    struct.pack_into('<I', h, 72, 0)
    struct.pack_into('<109I', h, 76, *([0] + [FREESECT] * 108))
    return bytes(h) + fat_bytes + directory + mf + bytes(mini_data)


def make_wps():
    text = ("文档转换测试样例。\n第一章 概述\n"
            "本文件为合成测试样例，不含任何真实业务内容。\n"
            "第二条 列表项一。\n第三条 列表项二。")
    raw = text.encode('utf-16-le')
    fc_min = 0x20; fc_mac = fc_min + len(raw)
    wd = bytearray(fc_mac)
    wd[0x0A] = 0x00
    struct.pack_into('<II', wd, 0x18, fc_min, fc_mac)
    wd[fc_min:fc_mac] = raw
    cfb = _build_cfb({"WordDocument": bytes(wd), "Data": b'\x00' * 64})
    (OUT / "test_sample.wps").write_bytes(cfb)


# ---------------- OFD: 最小 ZIP（OFD.xml + Document.xml + Page） ----------------
def make_ofd():
    NS = 'http://www.ofdspec.org/2016'
    ofd_xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               f'<ofd:OFD xmlns:ofd="{NS}" Version="1.0" DocType="OFD"><ofd:DocBody>'
               '<ofd:DocInfo><ofd:Title>合成测试样例</ofd:Title></ofd:DocInfo>'
               '<ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>')
    document = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<ofd:Document xmlns:ofd="{NS}"><ofd:CommonData><ofd:MaxUnitID>100</ofd:MaxUnitID>'
                '<ofd:PageArea><ofd:PhysicalBox>0 0 210 297</ofd:PhysicalBox></ofd:PageArea>'
                '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes></ofd:CommonData>'
                '<ofd:Pages><ofd:Page ID="1" BaseLoc="Pages/Page_0/Content.xml"/></ofd:Pages></ofd:Document>')
    res = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<ofd:Res xmlns:ofd="{NS}" BaseLoc="Res"><ofd:Fonts>'
           '<ofd:Font ID="10" FontName="仿宋" FamilyName="仿宋"/>'
           '<ofd:Font ID="11" FontName="黑体" FamilyName="黑体"/></ofd:Fonts></ofd:Res>')
    content = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               f'<ofd:Page xmlns:ofd="{NS}"><ofd:Content><ofd:Layer ID="20">'
               '<ofd:TextObject ID="21" Boundary="20 20 170 12" Font="11" Size="16">'
               '<ofd:TextCode X="20" Y="30">文档转换测试样例</ofd:TextCode></ofd:TextObject>'
               '<ofd:TextObject ID="22" Boundary="20 40 170 10" Font="10" Size="10.5">'
               '<ofd:TextCode X="20" Y="48">第一章 概述</ofd:TextCode></ofd:TextObject>'
               '<ofd:TextObject ID="23" Boundary="20 55 170 10" Font="10" Size="10.5">'
               '<ofd:TextCode X="20" Y="63">本文件为合成测试样例，不含任何真实业务内容。</ofd:TextCode>'
               '</ofd:TextObject></ofd:Layer></ofd:Content></ofd:Page>')
    with zipfile.ZipFile(OUT / "test_sample.ofd", 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr("OFD.xml", ofd_xml)
        z.writestr("Doc_0/Document.xml", document)
        z.writestr("Doc_0/PublicRes.xml", res)
        z.writestr("Doc_0/Pages/Page_0/Content.xml", content)


# ---------------- DOCX ----------------
def make_docx():
    from docx import Document
    d = Document()
    d.add_heading("文档转换测试样例", level=1)
    d.add_paragraph("本文件为合成测试样例，不含任何真实业务内容。用于验证 DOCX 转换器的标题、段落、列表与表格渲染。")
    d.add_heading("第一章 概述", level=2)
    d.add_paragraph("第一条 制定目的。为验证文档转换质量，特制定本合成样例。")
    d.add_paragraph("第二条 总体原则。按照结构完整、格式干净的原则组织内容。")
    d.add_heading("列表演示", level=2)
    for t in ["无序项一", "无序项二", "无序项三"]:
        d.add_paragraph(t, style="List Bullet")
    for t in ["有序项一", "有序项二"]:
        d.add_paragraph(t, style="List Number")
    d.add_heading("表格演示", level=2)
    tb = d.add_table(rows=3, cols=3); tb.style = "Table Grid"
    for i, row in enumerate([["指标", "数值", "单位"], ["余额", "1000", "万元"], ["比率", "5.2", "%"]]):
        for j, v in enumerate(row):
            tb.cell(i, j).text = v
    d.save(OUT / "test_sample.docx")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    make_docx(); make_ofd(); make_wps()
    print("generated synthetic samples in", OUT)
    for f in ["test_sample.docx", "test_sample.ofd", "test_sample.wps"]:
        print(" ", f, (OUT / f).stat().st_size, "bytes")
