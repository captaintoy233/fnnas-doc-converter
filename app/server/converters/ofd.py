"""OFD → Markdown 转换器。纯 Python，零外部依赖。"""
import zipfile, xml.etree.ElementTree as ET, os, re
from typing import List, Dict, Optional, Tuple
from . import BaseConverter

NS = {"ofd": "http://www.ofdspec.org/2016"}
HEADING_FONTS = {"黑体", "方正小标宋_GBK", "方正小标宋简体", "方正小标宋",
                 "方正书宋", "方正大标宋", "宋体", "SimSun"}


class OFDConverter(BaseConverter):
    """OFD → Markdown (纯Python, 标准库)"""

    def supported_extensions(self) -> list:
        return ['.ofd']

    def convert(self, file_path: str, **kwargs) -> str:
        with OFDParser(file_path) as p:
            return p.convert()


class OFDParser:
    def __init__(self, path: str):
        self.path = path
        self.zf = None
        self.font_map = {}
        self.doc_title = ""
        self.doc_author = ""
        self.doc_root = "Doc_0/Document.xml"
        self.pages: list = []
        self.page_height = 297.0
        self._origin_bl = None

    def __enter__(self):
        self.zf = zipfile.ZipFile(self.path, 'r')
        return self

    def __exit__(self, *args):
        if self.zf: self.zf.close()

    def _read_xml(self, p: str):
        return ET.fromstring(self.zf.read(p))

    def _detect_origin(self, frags):
        if not frags:
            return True
        mf = max(frags, key=lambda f: f["size"])
        return mf["y"] >= self.page_height / 2

    def _parse_meta(self):
        root = self._read_xml("OFD.xml")
        di = root.find(".//ofd:DocInfo", NS)
        if di is not None:
            t = di.find("ofd:Title", NS)
            if t is not None and t.text: self.doc_title = t.text.strip()
            a = di.find("ofd:Author", NS)
            if a is not None and a.text: self.doc_author = a.text.strip()
        dr = root.find(".//ofd:DocRoot", NS)
        if dr is not None and dr.text: self.doc_root = dr.text.strip()

    def _parse_fonts(self):
        base = os.path.dirname(self.doc_root) or "."
        try:
            root = self._read_xml(f"{base}/PublicRes.xml")
            for f in root.findall(".//ofd:Font", NS):
                self.font_map[f.get("ID", "")] = f.get("FontName", "")
        except:
            pass

    def _parse_pages(self):
        root = self._read_xml(self.doc_root)
        base = os.path.dirname(self.doc_root)
        pa = root.find(".//ofd:PageArea/ofd:PhysicalBox", NS)
        if pa is not None and pa.text:
            try: _, _, _, self.page_height = [float(x) for x in pa.text.strip().split()]
            except: pass
        for pe in root.findall(".//ofd:Page", NS):
            bl = pe.get("BaseLoc", "")
            self.pages.append(f"{base}/{bl}" if not bl.startswith("/") else bl)

    def _page_frags(self, page_path: str) -> list:
        root = self._read_xml(page_path)
        frags = []
        for obj in root.findall(".//ofd:TextObject", NS):
            try:
                fid = obj.get("Font", "")
                b = obj.get("Boundary", "0 0 0 0")
                bx, by, bw, bh = [float(v) for v in b.split()]
                sz = float(obj.get("Size", "0"))
                ce = obj.find("ofd:TextCode", NS)
                if ce is None: continue
                t = (ce.text or "").strip()
                if not t: continue
                frags.append({"text": t, "font": self.font_map.get(fid, "?"),
                              "size": sz, "x": bx, "y": by, "w": bw, "h": bh})
            except: continue
        return frags

    def convert(self) -> str:
        self._parse_meta()
        self._parse_fonts()
        self._parse_pages()

        # 统一检测坐标系
        all_frags = []
        for pp in self.pages:
            all_frags.extend(self._page_frags(pp))
        self._origin_bl = self._detect_origin(all_frags)

        parts = [f"# {self.doc_title or os.path.basename(self.path)}"]
        if self.doc_author:
            parts.append(f"\n> **来源**: {self.doc_author}")
        parts.append("\n---\n")

        for pp in self.pages:
            raw = self._page_frags(pp)
            if not raw: continue
            if self._origin_bl:
                raw.sort(key=lambda f: (-f["y"], f["x"]))
            else:
                raw.sort(key=lambda f: (f["y"], f["x"]))

            # 行合并
            lines, cur = [], [raw[0]]
            for f in raw[1:]:
                if abs(f["y"] - cur[-1]["y"]) < min(f["h"], cur[-1]["h"]) * 0.7:
                    cur.append(f)
                else:
                    lines.append(cur); cur = [f]
            if cur: lines.append(cur)

            # 段落分组
            paras = []
            cp = {"lines": [lines[0]]}
            for l in lines[1:]:
                yg = abs(l[0]["y"] - cp["lines"][-1][0]["y"])
                ah = (cp["lines"][-1][0]["h"] + l[0]["h"]) / 2
                if yg > ah * 1.2:
                    paras.append(cp); cp = {"lines": [l]}
                else: cp["lines"].append(l)
            if cp["lines"]: paras.append(cp)

            for p in paras:
                ft = "".join("".join(f["text"] for f in sorted(l, key=lambda x: x["x"])) for l in p["lines"])
                ft = ft.strip()
                if not ft: continue
                f0 = p["lines"][0][0]
                hl = self._is_heading(f0["font"], f0["size"], ft)
                parts.append(f"\n{'#' * hl} {ft}\n" if hl else ft)

        return "\n".join(parts)

    def _is_heading(self, font, size, text):
        if not text or len(text) < 6: return None
        hn = bool(re.match(r'^[（(]?[一二三四五六七八九十百千]+[）).、：:]', text))
        if font in HEADING_FONTS:
            if size >= 10: return 1
            if size >= 7: return 2
            if size >= 5.5: return 3 if hn else 2
            return None
        if size >= 7: return 2
        if size >= 5.5 and hn: return 3
        return None
