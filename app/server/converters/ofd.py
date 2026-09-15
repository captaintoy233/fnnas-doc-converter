"""OFD → Markdown 转换器。纯 Python，零外部依赖。

架构改进（v2 - Document Model）:
- 先解析为统一 Document Model，再通过共享序列化器输出 Markdown
- 字体检测标题映射为 Heading blocks，正文映射为 Paragraph blocks
- 解析失败时抛出 MalformedDocumentError

向后兼容:
- convert() 方法保持原有签名和返回类型
- 新增 convert_to_document() 返回 Document Model
"""
import zipfile, xml.etree.ElementTree as ET, os, re
from typing import List, Dict, Optional, Tuple
from . import BaseConverter

# Document Model 导入
from model.document import Document
from model.block import Heading, Paragraph
from model.inline import Text
from render.markdown import document_to_markdown
from errors import MalformedDocumentError

NS = {"ofd": "http://www.ofdspec.org/2016"}
HEADING_FONTS = {"黑体", "方正黑体_GBK", "方正小标宋_GBK", "方正小标宋简体", "方正小标宋",
                 "方正书宋", "方正大标宋", "宋体", "SimSun"}

# ============================================================================
# Sursen OFD Writer 自定义字体编码映射 (EU-BX / EU-BZ / EU-FZ / EU-B6)
#
# 这些字体名称以 "EU-" 开头，使用私有 Unicode 码位存储标点符号和数字。
# 不同 OFD 文件中 Font ID 可能不同，但字体名称→编码映射是固定的。
# 通过上下文分析和多文件交叉验证确认以下映射表。
# ============================================================================

# EU-BX: 中文标点符号 + 控制符
_EU_BX_MAP = {
    '\u68F3': '，',      # 棳 → 逗号
    '\u668D': '、',      # 暍 → 顿号
    '\u668E': '。',      # 暎 → 句号
    '\u68EC': '（',      # 棬 → 左圆括号
    '\u68ED': '）',      # 棭 → 右圆括号
    '\u669F': '\u201c',  # 暟 → 左双引号 "
    '\u66A0': '\u201d',  # 暠 → 右双引号 "
    '\u66A1': '〔',      # 暡 → 左方括号
    '\u66A2': '〕',      # 暢 → 右方括号
    '\u66A5': '《',      # 暥 → 左书名号
    '\u66A6': '》',      # 暦 → 右书名号
    '\u6907': '：',      # 椇 → 冒号
    '\u6908': '）',      # 椈 → 右圆括号(变体)
    '\u6698': '\n',      # 暘 → 换行符（段落/页面分隔）
    '\u68F7': '；',      # 棷 → 分号
    '\u66A3': '【',      # 暣 → 左实心方括号（上下文：《...》【...】）
    '\u66A4': '】',      # 暤 → 右实心方括号
    '\u669C': '①',      # 暜 → 圈数字1（上下文：序号标记）
    '\u669E': '②',      # 暞 → 圈数字2
}

# EU-BZ: 数字、符号和西文字母
_EU_BZ_MAP = {
    '\u68F8': '0',   # 棸
    '\u68FB': '1',   # 棻
    '\u68FD': '2',   # 棽
    '\u68FE': '3',   # 棾
    '\u68FF': '4',   # 棿
    '\u6900': '5',   # 椀
    '\u6902': '6',   # 椂
    '\u6903': '7',   # 椃
    '\u6904': '8',   # 椄
    '\u6906': '9',   # 椆
    '\u68F6': '.',   # 棶 → 小数点
    '\u68E9': '%',   # 棩 → 百分号
    '\u6586': 'C',   # 斆 → 拉丁字母 C（如 C4 系统）
    '\u65BF': 'e',   # 斿 → 拉丁字母 e（如 农银e学）
    '\u6589': 'E',   # 斉 → 拉丁字母 E（如 ETC）
    '\u65A3': 'T',   # 斣 → 拉丁字母 T（如 ETC）
    # --- v3 扩展映射（基于多文件交叉验证） ---
    '\u68C1': ' ',   # 棁 → 空格/填充符（大量出现在表格空白区域）
    '\u6585': 'A',   # 斅 → 拉丁字母 A（上下文：eeAC20, 线下AC20）
    '\u68F4': '-',   # 棴 → 连字符/破折号（上下文：协议-3ee, 043-4）
    '\u659D': 'R',   # 斝 → 拉丁字母 R（上下文：利率和R, 营销R）
    '\u68F2': '，',  # 棲 → 逗号变体（上下文：服务，、各行）
    '\u6584': 'A',   # 斄 → 拉丁字母 A 变体（上下文：部或网A, eeAC2）
    '\u65D3': 's',   # 旓 → 拉丁字母 s（上下文：56s, 旓gs）
    '\u65D1': 'r',   # 旑 → 拉丁字母 r（上下文：Er, r.）
    '\u65C8': 'i',   # 旈 → 拉丁字母 i（上下文：Ei, ir）
    '\u65C2': 'g',   # 旂 → 拉丁字母 g（上下文：Eig, g.）
    '\u65B8': '.',   # 斸 → 小数点变体（上下文：r., .r）
    '\u7060': '-',   # 灠 → 连字符/减号（上下文：191-450, 2-811）
    '\u658A': 'P',   # 斊 → 拉丁字母 P（上下文：评价PT, 影响PT）
    '\u65BB': 'b',   # 斻 → 拉丁字母 b（上下文：.ab, ab.）
    '\u65BE': 'd',   # 斾 → 拉丁字母 d（上下文：bd, ds）
    '\u65D0': 'm',   # 旐 → 拉丁字母 m（上下文：dsm, m.）
    '\u65C7': 'h',   # 旇 → 拉丁字母 h（上下文：sh, hi）
    '\u6596': 'V',   # 斖 → 拉丁字母 V（上下文：谁CV, 和V）
    '\u66FB': '✓',   # 曻 → 勾选标记（上下文：查询✓✓）
    '\u6595': 'L',   # 斕 → 拉丁字母 L（上下文：利率和L, 表L0）
    '\u65A0': 'R',   # 斠 → 拉丁字母 R 变体（上下文：LR, 积极R）
    '\u65D9': 's',   # 旙 → 拉丁字母 s 变体（上下文：ds, sm）
    '\u65BA': 'a',   # 斺 → 拉丁字母 a（上下文：.aa, ab）
    '\u6588': '，',  # 斈 → 逗号变体（上下文：退出，、关行）
    '\u6592': '《',  # 斒 → 左书名号变体（上下文：017《》, 应《》）
    '\u6598': 'N',   # 斘 → 拉丁字母 N（上下文：EN, NEN）
    '\u659A': 'E',   # 斚 → 拉丁字母 E 变体（上下文：NE, NE）
    '\u65A2': 'S',   # 斢 → 拉丁字母 S（上下文：NES, NES）
    '\u6594': 'K',   # 斔 → 拉丁字母 K（上下文：尽K, KC）
    '\u65AE': 'C',   # 斮 → 拉丁字母 C 变体（上下文：KC, C、、）
    '\u65DB': 't',   # 旛 → 拉丁字母 t（上下文：156ts）
    '\u65D8': 'x',   # 旘 → 拉丁字母 x（上下文：tsx）
    '\u6911': 'p',   # 椑 → 拉丁字母 p（上下文：xp）
    '\u7062': 'l',   # 灢 → 拉丁字母 l（上下文：pl）
    '\u65D4': 'n',   # 旔 → 拉丁字母 n（上下文：in, ng）
    '\u65C9': 'f',   # 旉 → 拉丁字母 f（上下文：lf, fn）
    '\u658D': 'F',   # 斍 → 拉丁字母 F（上下文：企业FT4）
    '\u658F': 'I',   # 斏 → 拉丁字母 I（上下文：和IVP）
    '\u66E4': '-',   # 曤 → 连字符变体（上下文：403-。）
    '\u658E': '5',   # 斎 → 数字5变体（上下文：手机5，）
    '\u7061': '.',   # 灡 → 小数点变体（上下文：4.5%）
    '\u659E': 'Q',   # 斞 → 拉丁字母 Q（上下文：LQ028）
}

# 合并所有 EU 映射（按字体名称查找）
_EU_FONT_MAPS = {
    'EU-BX': _EU_BX_MAP,
    'EU-BZ': _EU_BZ_MAP,
    'EU-FZ': _EU_BX_MAP,   # EU-FZ 与 EU-BX 共享标点映射
    'EU-B6': _EU_BX_MAP,   # EU-B6 混合标点和括号，与 EU-BX 一致
    'EU-F1': _EU_BX_MAP,   # EU-F1 也使用标点映射
}


def _decode_eu_text(text: str, font_name: str) -> str:
    """将 EU 自定义编码文本解码为标准 Unicode"""
    cmap = _EU_FONT_MAPS.get(font_name)
    if not cmap:
        return text
    return ''.join(cmap.get(ch, ch) for ch in text)


def _is_eu_font(font_name: str) -> bool:
    """判断是否为 EU 自定义编码字体"""
    return font_name.startswith('EU-')


class OFDConverter(BaseConverter):
    """OFD → Markdown (纯Python, 标准库)"""

    def supported_extensions(self) -> list:
        return ['.ofd']

    def convert_to_document(self, file_path: str, **kwargs) -> Document:
        """转换为统一文档模型"""
        try:
            with OFDParser(file_path) as p:
                return p.convert_to_document()
        except MalformedDocumentError:
            raise
        except Exception as e:
            raise MalformedDocumentError(
                f"Failed to parse OFD file: {e}",
                file_path=file_path
            ) from e

    def convert(self, file_path: str, **kwargs) -> str:
        """转换为 Markdown 字符串（向后兼容接口）"""
        doc = self.convert_to_document(file_path, **kwargs)
        return document_to_markdown(doc)


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
        # OFD 规范中 PublicRes 文件名可能带编号（如 PublicRes_0.xml）
        # 先从 Document.xml 的 CommonData/PublicRes 获取准确路径
        pub_res_path = None
        try:
            doc_root = self._read_xml(self.doc_root)
            pr = doc_root.find(".//ofd:CommonData/ofd:PublicRes", NS)
            if pr is not None and pr.text:
                pub_res_path = f"{base}/{pr.text.strip()}"
        except Exception:
            pass
        # 回退尝试常见路径
        candidates = [pub_res_path, f"{base}/PublicRes.xml", f"{base}/PublicRes_0.xml"]
        for path in candidates:
            if not path:
                continue
            try:
                root = self._read_xml(path)
                for f in root.findall(".//ofd:Font", NS):
                    self.font_map[f.get("ID", "")] = f.get("FontName", "")
                break  # 成功读取即停止
            except Exception:
                continue

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
                font_name = self.font_map.get(fid, "?")
                b = obj.get("Boundary", "0 0 0 0")
                bx, by, bw, bh = [float(v) for v in b.split()]
                sz = float(obj.get("Size", "0"))
                ce = obj.find("ofd:TextCode", NS)
                if ce is None: continue
                t = (ce.text or "").strip()
                if not t: continue
                
                # 计算文本在页面上的绝对坐标
                # OFD 规范：TextCode X/Y 的含义取决于是否有 CTM
                # - 有 CTM: X/Y 是变换前坐标，需要通过 CTM 映射到页面空间
                # - 无 CTM: X/Y 是相对于 Boundary 左上角的偏移量
                tc_x = ce.get("X")
                tc_y = ce.get("Y")
                ctm_str = obj.get("CTM")
                
                if tc_x is not None and tc_y is not None:
                    try:
                        raw_x = float(tc_x)
                        raw_y = float(tc_y)
                        if ctm_str:
                            # CTM 存在：X/Y 是变换前坐标，应用 CTM
                            parts = ctm_str.split()
                            if len(parts) == 6:
                                a, b_, c, d, e, f_ = [float(v) for v in parts]
                                use_x = a * raw_x + c * raw_y + e
                                use_y = b_ * raw_x + d * raw_y + f_
                            else:
                                use_x, use_y = raw_x, raw_y
                        else:
                            # 无 CTM：X/Y 是相对于 Boundary 的偏移
                            use_x = bx + raw_x
                            use_y = by + raw_y
                    except (ValueError, TypeError):
                        use_x, use_y = bx, by
                else:
                    use_x, use_y = bx, by
                
                # EU 自定义编码字体需要解码
                if _is_eu_font(font_name):
                    t = _decode_eu_text(t, font_name)
                frags.append({"text": t, "font": font_name,
                              "size": sz, "x": use_x, "y": use_y, "w": bw, "h": bh})
            except: continue
        return frags

    def convert_to_document(self) -> Document:
        """解析 OFD 并返回统一文档模型"""
        self._parse_meta()
        self._parse_fonts()
        self._parse_pages()

        # 统一检测坐标系
        all_frags = []
        for pp in self.pages:
            all_frags.extend(self._page_frags(pp))
        self._origin_bl = self._detect_origin(all_frags)

        doc = Document()
        doc.title = self.doc_title or os.path.basename(self.path)
        doc.author = self.doc_author or None

        for pp in self.pages:
            raw = self._page_frags(pp)
            if not raw: continue
            if self._origin_bl:
                raw.sort(key=lambda f: (-f["y"], f["x"]))
            else:
                raw.sort(key=lambda f: (f["y"], f["x"]))

            # 行合并 — 使用字号(size)作为行高基准，比 Boundary h 更可靠
            # （某些 OFD 生成器每个字符一个 TextObject，Boundary 全部相同）
            lines, cur = [], [raw[0]]
            for f in raw[1:]:
                # 优先用 size 估算行高，回退到 h
                prev_line_h = cur[-1].get("size") or cur[-1]["h"]
                cur_line_h = f.get("size") or f["h"]
                y_thresh = min(prev_line_h, cur_line_h) * 0.7
                if abs(f["y"] - cur[-1]["y"]) < y_thresh:
                    cur.append(f)
                else:
                    lines.append(cur); cur = [f]
            if cur: lines.append(cur)

            # 段落分组（v3 优化：结合间距 + 内容特征 + 缩进检测）
            paras = []
            cp = {"lines": [lines[0]]}
            for l in lines[1:]:
                prev_line = cp["lines"][-1]
                yg = abs(l[0]["y"] - prev_line[0]["y"])
                # 使用字号作为高度基准（更可靠）
                prev_sz = prev_line[0].get("size") or prev_line[0]["h"]
                cur_sz = l[0].get("size") or l[0]["h"]
                avg_h = (prev_sz + cur_sz) / 2
                
                # 行内换行：间距 ≈ 字号 × 1.3~1.8
                # 段落间：间距 > 字号 × 2.0（或绝对值 > 4mm）
                line_gap = avg_h * 1.8
                para_gap = max(avg_h * 2.2, 4.0)
                
                # 检查首行缩进（中文段落通常有 2 字符缩进）
                prev_x = min(f["x"] for f in prev_line)
                cur_x = min(f["x"] for f in l)
                indent_diff = cur_x - prev_x
                has_indent = abs(indent_diff) > avg_h * 1.5  # 缩进超过 1.5 个字宽
                
                if yg > para_gap:
                    paras.append(cp); cp = {"lines": [l]}
                elif yg > line_gap or has_indent:
                    prev_text = "".join(f["text"] for f in sorted(prev_line, key=lambda x: x["x"]))
                    curr_text = "".join(f["text"] for f in sorted(l, key=lambda x: x["x"]))
                    is_new_para = (
                        prev_text.rstrip().endswith(('。', '；', '！', '？', '.', ';', '）', ')'))
                        or bool(re.match(r'^[\s]*[（(]?[一二三四五六七八九十百千\d]+[）).、：:\s]', curr_text))
                        or bool(re.match(r'^[\s]*第[一二三四五六七八九十\d]+[章节条部分篇]', curr_text))
                        or has_indent
                    )
                    if is_new_para:
                        paras.append(cp); cp = {"lines": [l]}
                    else:
                        cp["lines"].append(l)
                else:
                    cp["lines"].append(l)
            if cp["lines"]: paras.append(cp)

            for p in paras:
                ft = "".join("".join(f["text"] for f in sorted(l, key=lambda x: x["x"])) for l in p["lines"])
                ft = ft.strip()
                if not ft: continue
                f0 = p["lines"][0][0]
                hl = self._is_heading(f0["font"], f0["size"], ft)
                if hl:
                    doc.add_block(Heading(level=hl, children=[Text(ft)]))
                else:
                    doc.add_block(Paragraph(children=[Text(ft)]))

        return doc

    def convert(self) -> str:
        """旧版接口：直接返回 Markdown 字符串（保留用于内部兼容）"""
        doc = self.convert_to_document()
        return document_to_markdown(doc)

    def _is_heading(self, font, size, text):
        if not text:
            return None
        hn = bool(re.match(r'^[（(]?[一二三四五六七八九十百千]+[）).、：:]', text))
        if font in HEADING_FONTS:
            # 标题字体（黑体/宋体等）即使较短也视为标题
            if size >= 10: return 1
            if size >= 7: return 2
            if size >= 5.5: return 3 if hn else 2
            return None
        if len(text) < 6:
            return None
        if size >= 7: return 2
        if size >= 5.5 and hn: return 3
        return None
