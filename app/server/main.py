"""DocConverter FastAPI 主服务"""
import os, tempfile, uuid, json
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn

from converters import registry
from converters.ofd import OFDConverter
from converters.wps import WPSConverter
from converters.docx_converter import DOCXConverter
from converters.xlsx_converter import XLSXConverter
from converters.et_converter import ETConverter
from converters.pdf_converter import PDFConverter
from converters.htm_converter import HTMConverter
from converters.pptx_converter import PPTXConverter
from converters.placeholder_converter import PlaceholderConverter

# ── 注册所有转换器 ──
registry.register(OFDConverter())
registry.register(WPSConverter())
registry.register(DOCXConverter())
registry.register(XLSXConverter())
registry.register(ETConverter())
registry.register(PDFConverter())
registry.register(HTMConverter())
registry.register(PPTXConverter())

# 占位转换器 (需要 LibreOffice/Tesseract)
registry.register(PlaceholderConverter(
    "PPT(旧版)", ['.ppt'],
    u"**\u26a0\ufe0f \u6682\u4e0d\u652f\u6301\u6b64\u683c\u5f0f**\n\n"
    u"\u65e7\u7248\u4e8c\u8fdb\u5236 PPT \u683c\u5f0f\u9700\u8981 LibreOffice \u8f6c\u6362\u3002\n"
    u"\u8bf7\u5c06\u6587\u4ef6\u4fdd\u5b58\u4e3a PPTX \u683c\u5f0f\u540e\u4e0a\u4f20\u3002"
))
registry.register(PlaceholderConverter(
    "DPS(WPS\u6f14\u793a)", ['.dps'],
    u"**\u26a0\ufe0f \u6682\u4e0d\u652f\u6301\u6b64\u683c\u5f0f**\n\n"
    u"WPS \u6f14\u793a\u683c\u5f0f\u9700\u8981 LibreOffice \u8f6c\u6362\u3002"
))
registry.register(PlaceholderConverter(
    "PDF(\u56fe\u7247\u578b)", ['.pdf'],  # 重复注册会覆盖，但图片PDF检测在PDFConverter内
    u""
))

app = FastAPI(title="DocConverter", version="1.0.0")
UPLOAD_DIR = Path("/data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/formats")
def list_formats():
    """返回所有支持的格式列表"""
    return JSONResponse({"formats": registry.get_all_formats()})


@app.post("/api/convert")
async def convert_file(file: UploadFile = File(...)):
    """上传并转换文件，返回 Markdown 结果"""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if not ext:
        raise HTTPException(400, "无法识别文件扩展名")

    converter = registry.get(ext)
    if not converter:
        raise HTTPException(400, f"不支持的格式: {ext}")

    # 保存上传文件
    file_id = str(uuid.uuid4())
    temp_path = UPLOAD_DIR / f"{file_id}{ext}"
    content = await file.read()
    with open(temp_path, "wb") as f:
        f.write(content)

    try:
        result = converter.convert(str(temp_path))
        return JSONResponse({
            "filename": file.filename,
            "format": ext,
            "converter": converter.display_name,
            "length": len(result),
            "markdown": result,
        })
    except Exception as e:
        raise HTTPException(500, f"转换失败: {str(e)}")
    finally:
        if temp_path.exists():
            temp_path.unlink()


@app.get("/api/health")
def health():
    return {"status": "ok", "formats": len(registry.list_supported_extensions())}


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>DocConverter</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:#f5f7fa; color:#333; min-height:100vh; }
.container { max-width:900px; margin:0 auto; padding:40px 20px; }
h1 { font-size:28px; color:#1a1a2e; margin-bottom:8px; }
.subtitle { color:#666; margin-bottom:32px; }
.upload-box { background:white; border:2px dashed #d0d5dd; border-radius:12px;
              padding:60px 40px; text-align:center; transition:all .2s; cursor:pointer; }
.upload-box:hover { border-color:#2563eb; background:#f8faff; }
.upload-box.dragover { border-color:#2563eb; background:#eef4ff; }
.upload-icon { font-size:48px; margin-bottom:16px; }
.upload-text { font-size:16px; color:#555; }
.upload-hint { font-size:13px; color:#999; margin-top:8px; }
#fileInput { display:none; }
.formats { display:flex; flex-wrap:wrap; gap:6px; margin-top:32px; justify-content:center; }
.tag { background:white; border:1px solid #e2e8f0; border-radius:20px;
       padding:4px 14px; font-size:13px; color:#555; }
.tag.ready { background:#e8f5e9; border-color:#a5d6a7; color:#2e7d32; }
.tag.pending { background:#fff3e0; border-color:#ffcc80; color:#e65100; }
#result { display:none; margin-top:24px; background:white; border-radius:12px;
          border:1px solid #e2e8f0; overflow:hidden; }
#result .header { padding:16px 20px; background:#f8fafc; border-bottom:1px solid #e2e8f0;
                  display:flex; justify-content:space-between; align-items:center; }
#result .meta { font-size:14px; color:#666; }
#result pre { padding:20px; overflow-x:auto; font-size:13px; line-height:1.6;
              max-height:600px; overflow-y:auto; white-space:pre-wrap; }
.copy-btn { background:#2563eb; color:white; border:none; border-radius:6px;
            padding:6px 16px; cursor:pointer; font-size:13px; }
.copy-btn:hover { background:#1d4ed8; }
#spinner { display:none; text-align:center; padding:40px; }
.spinner-icon { animation:spin 1s linear infinite; font-size:36px; }
@keyframes spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
.error { color:#dc2626; padding:16px; background:#fef2f2; border-radius:8px; margin-top:16px; }
</style>
</head>
<body>
<div class="container">
  <h1>📄 文档格式转换器</h1>
  <p class="subtitle">上传文档自动转换为 Markdown，兼容 WeKnora 知识库</p>
  
  <div class="upload-box" id="uploadBox" onclick="document.getElementById('fileInput').click()">
    <div class="upload-icon">📤</div>
    <div class="upload-text">点击上传文件或拖拽到此处</div>
    <div class="upload-hint">支持 OFD / WPS / DOCX / XLSX / ET / PDF / HTML / PPTX</div>
  </div>
  <input type="file" id="fileInput" onchange="uploadFile(this)">
  
  <div id="spinner"><div class="spinner-icon">⏳</div><p style="color:#666;margin-top:12px">转换中...</p></div>
  <div id="result">
    <div class="header">
      <div class="meta" id="fileMeta"></div>
      <button class="copy-btn" onclick="copyResult()">📋 复制</button>
    </div>
    <pre id="markdownOutput"></pre>
  </div>
  <div id="error" class="error" style="display:none"></div>
  
  <div class="formats" id="formatTags"></div>
</div>
<script>
async function loadFormats() {
  const r = await fetch('/api/formats');
  const data = await r.json();
  const tags = document.getElementById('formatTags');
  for (const fmt of data.formats) {
    const span = document.createElement('span');
    span.className = fmt.status === 'available' ? 'tag ready' : 'tag pending';
    span.textContent = `${fmt.name} (${fmt.extensions.join(', ')})`;
    tags.appendChild(span);
  }
}
loadFormats();

const uploadBox = document.getElementById('uploadBox');
uploadBox.addEventListener('dragover', e => { e.preventDefault(); uploadBox.classList.add('dragover'); });
uploadBox.addEventListener('dragleave', () => uploadBox.classList.remove('dragover'));
uploadBox.addEventListener('drop', e => {
  e.preventDefault(); uploadBox.classList.remove('dragover');
  if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
});

async function uploadFile(input) {
  if (!input.files.length) return;
  handleFile(input.files[0]);
}

async function handleFile(file) {
  const form = new FormData();
  form.append('file', file);
  document.getElementById('spinner').style.display = 'block';
  document.getElementById('result').style.display = 'none';
  document.getElementById('error').style.display = 'none';
  try {
    const r = await fetch('/api/convert', { method:'POST', body:form });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail || '转换失败'); }
    const data = await r.json();
    document.getElementById('spinner').style.display = 'none';
    document.getElementById('fileMeta').textContent = `${file.name} (${data.length} 字符)`;
    document.getElementById('markdownOutput').textContent = data.markdown;
    document.getElementById('result').style.display = 'block';
  } catch(e) {
    document.getElementById('spinner').style.display = 'none';
    document.getElementById('error').textContent = '❌ ' + e.message;
    document.getElementById('error').style.display = 'block';
  }
}

async function copyResult() {
  const text = document.getElementById('markdownOutput').textContent;
  await navigator.clipboard.writeText(text);
  const btn = document.querySelector('.copy-btn');
  btn.textContent = '✅ 已复制';
  setTimeout(() => btn.textContent = '📋 复制', 2000);
}
</script>
</body>
</html>"""
