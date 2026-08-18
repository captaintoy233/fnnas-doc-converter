"""PDF → Markdown。文字型PDF用PyMuPDF提取；图片型PDF可选用 Tesseract OCR。"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import fitz

from config import get_config
from . import BaseConverter

logger = logging.getLogger("docconverter.pdf")


class PDFConverter(BaseConverter):
    """PDF → MD (文字型: PyMuPDF; 图片型: 可选 Tesseract OCR)"""

    def supported_extensions(self) -> list:
        return ['.pdf']

    def _ocr_enabled(self) -> bool:
        cfg = get_config().get("pdf", {})
        return bool(cfg.get("ocr_enabled", False))

    def _ocr(self, doc) -> str:
        """渲染每页为图片，调用 tesseract OCR"""
        cfg = get_config().get("pdf", {})
        cmd = cfg.get("ocr_command", "tesseract")
        lang = cfg.get("ocr_lang", "chi_sim+eng")
        dpi = int(cfg.get("ocr_dpi", 200))

        # 安全: 只允许已知的可执行文件路径（shutil.which 解析），
        # 拒绝含 shell 元字符的命令，防止配置被改写后命令注入
        if any(ch in cmd for ch in ";&|`$()<>\\\n"):
            raise RuntimeError("非法的 ocr_command 配置（含 shell 元字符），已拒绝执行")
        executable = shutil.which(cmd) if "/" not in cmd else (
            cmd if os.path.exists(cmd) else None)
        if not executable:
            raise RuntimeError(
                "图片型 PDF 需要 OCR，但未找到 tesseract。\n"
                "请安装 tesseract-ocr 及中文语言包，或在配置中开启 pdf.ocr_enabled。")

        parts = []
        with tempfile.TemporaryDirectory(prefix="pdf_ocr_") as tmp:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=dpi)
                img_path = os.path.join(tmp, f"page_{i:04d}.png")
                pix.save(img_path)
                proc = subprocess.run(
                    [cmd, img_path, "stdout", "-l", lang],
                    capture_output=True, text=True, timeout=120,
                )
                text = proc.stdout.strip() if proc.returncode == 0 else ""
                if text:
                    parts.append(f"\n---\n\n第 {i+1} 页\n\n{text}")
        return "\n".join(parts).strip() or "（OCR 未识别出文字）"

    def convert(self, file_path: str, **kwargs) -> str:
        doc = fitz.open(file_path)
        try:
            pages_text = []
            total_text = 0
            for page in doc:
                t = page.get_text().strip()
                pages_text.append(t)
                total_text += len(t)

            if total_text == 0:
                if self._ocr_enabled():
                    logger.info("PDF 无内嵌文字，尝试 OCR: %s", file_path)
                    return self._ocr(doc)
                return (u"**\u26a0\ufe0f \u65e0\u6cd5\u63d0\u53d6\u6587\u5b57**\n\n"
                        u"\u8be5PDF\u6587\u4ef6\u662f\u626b\u63cf\u4ef6/\u56fe\u7247\u578bPDF\uff0c\u65e0\u5d4c\u5165\u5f0f\u6587\u5b57\u3002\n"
                        u"\u53ef\u4ee5\uff1a\n"
                        u"1. \u5728\u914d\u7f6e\u4e2d\u5f00\u542f pdf.ocr_enabled \u5e76\u5b89\u88c5 tesseract\uff1b\n"
                        u"2. \u6216\u5c06\u539f\u59cb PDF \u63a8\u9001\u5230 WeKnora \u7531\u670d\u52a1\u7aef\u89e3\u6790\u3002")
            return '\n\n'.join(pages_text).strip()
        finally:
            doc.close()
