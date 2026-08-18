# DocConverter — 全能文档格式转换器（Weknora 配套工具）

把各种格式的文档批量转换为 **Markdown**，保留源目录结构，并自动推送到
[WeKnora](https://github.com/Tencent/WeKnora) 知识库，供 RAG 检索问答。

专为个人/单位知识库场景设计，核心诉求：**目录结构最大化保留、增量同步、真实格式识别**。

## 特性

- **格式覆盖广**（纯 Python 零 Office 依赖）：
  OFD / WPS(.wps) / WPSX(.wpsx) / DOCX / XLSX / ET(.et) / ETX(.etx) /
  PDF(文字型，可选项配 OCR) / HTML / PPTX / DPSX(.dpsx) / **CHM** / **EML** / **MSG** / **压缩包**
- **格式嗅探**：按魔数识别真实格式，扩展名伪装（如 `.wps` 实为 OOXML）自动路由
- **目录结构保留**：`AI/政策制度/xxx.docx` → `AI-OUT/政策制度/xxx.md`
- **压缩包自动解压**：`资料包.zip` → `AI-OUT/资料包/<内部结构>/xxx.md`，支持嵌套归档
- **EML/MSG 邮件合并**：邮件头 + 正文 + 附件内容合并为**单个 MD**
- **CHM 支持**：7zz 解压 + `.hhc` 章节分组合并（如 213MB 信贷手册）
- **增量同步**：`registry.json` 记录 SHA-256 指纹，未变化文件跳过（转换 + 推送双重去重）
- **WeKnora 对接**：JWT / **API Key** 双认证、异步推送队列、失败重试、
  相对路径标题（知识库内保留目录结构）、无法转换时**回退推送原始文件**
- **Windows 路径支持**：配置直接写 `C:\Users\Administrator\Documents\AI`，
  容器内通过 `drive_map` 映射到挂载点
- Web UI + REST API + 命令行三端可用

## 架构

```
┌─ Windows 主机 ──────────────┐      ┌─ DocConverter (Docker/Linux) ────────────┐
│ C:\...\Documents\AI (源)     │ SMB  │ 扫描 → 嗅探 → 转换 → MD 输出(保目录结构)  │
│ C:\...\Documents\AI-OUT (出) │◄────►│ registry.json 增量指纹 → 异步推送队列     │
└─────────────────────────────┘      └──────────────┬───────────────────────────┘
                                                     │ HTTP (JWT / X-API-Key)
                                          ┌──────────▼──────────┐
                                          │ WeKnora 知识库平台   │
                                          │ (NAS / GPU 主机)     │
                                          └─────────────────────┘
```

## 快速开始

### 方式一：独立部署（推荐，对接已有 WeKnora）

```bash
# 1. 在宿主机 SMB 挂载 Windows 目录（示例）
#    mount -t cifs //192.168.31.169/AI /mnt/win-ai -o username=Administrator
#    mount -t cifs //192.168.31.169/AI-OUT /mnt/win-ai-out -o username=Administrator

# 2. 配置 .env
cat > .env <<'EOF'
WEKNORA_URL=http://192.168.31.131:8080
WEKNORA_EMAIL=admin@rag.local
WEKNORA_PASSWORD=你的密码          # 或 WEKNORA_API_KEY=sk-...
WEKNORA_DATASET_ID=知识库ID或名称
HOST_INPUT_DIR=/mnt/win-ai         # Windows AI 目录的挂载点
HOST_OUTPUT_DIR=/mnt/win-ai-out    # Windows AI-OUT 目录的挂载点
BATCH_WORKERS=8
EOF

# 3. 启动（需要 weknora-net 外部网络，见 compose 文件注释）
docker compose -f docker-compose.standalone.yml up -d --build
```

> 也可以不配置 `drive_map`：源/输出目录直接用容器内路径（如 `/data/input`），
> 通过 compose 的 volumes 把 Windows 挂载点 bind 进容器即可。

### 方式二：全栈一键（converter + WeKnora 全家桶）

```bash
docker compose up -d --build
# Web UI:  http://localhost:8080
# API 文档: http://localhost:8080/docs
```

### Windows 原生运行（不装 Docker）

```bash
pip install -r app/server/requirements.txt
cd app/server
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

此时路径直接写 Windows 路径即可（无需 drive_map）：

```yaml
converter:
  output_dir: "C:\\Users\\Administrator\\Documents\\AI-OUT"
scanner:
  source_dir: "C:\\Users\\Administrator\\Documents\\AI"
```

## WeKnora 版本兼容性（v0.7.1 vs main）

已对照源码核实（v0.7.1 与 main 分支差异）:

| 能力 | v0.7.1 | main | 说明 |
|------|--------|------|------|
| manual 状态 draft/publish | ✅ | ✅ | 客户端已用 `publish` |
| reparse / delete / batch-delete | ✅ | ✅ | 客户端删除重建与重试均兼容 |
| `tag_ids`（复数） | ✅ | ✅ | 文档写 `tag_id` 是文档滞后 |
| **fileName 路径 → 文件夹树** | ❌ | ✅ | v0.7.1 把 `政策制度/x.md` 当**文件名**存（含斜杠），不生成文件夹；main 才支持 `SplitKnowledgeRelativePath` 文件夹拆分 |

**对 v0.7.1 用户的建议**：当前生产部署（v0.7.1）上，
- 文件夹结构保留用 **folder→KB 映射 + manual 推送**（生产已验证）或
- 升级 WeKnora 到 main 分支以获得 fileName 文件夹树；
- `push_as_file` 在 v0.7.1 上仍可推送（文档正常入库），只是 UI 里文件名会带路径斜杠。

## 多知识库路由（folder → KB 映射）

源目录下不同子目录可推送到不同知识库。映射文件（`folder_kb_map_path`）格式:

```json
{ "mappings": { "工作邮件": "de2e0345-…", "报表数据": "kb-2", "政策制度/近期新制度": "kb-3" } }
```

- 匹配规则：按文件相对路径**最长前缀优先**；未匹配回退 `dataset_id`
- 值支持知识库 **ID 或名称**
- 配置方式：
  ```bash
  python cli.py mappings --set "工作邮件=de2e0345-…,报表数据=kb-2"   # 命令行
  curl -X POST /api/weknora/mappings -d '{"mappings":{"工作邮件":"kb-1"}}'  # API
  ```
- 推送编排：批量转换时自动按目录路由到对应知识库，再触发 reparse

## 解析状态与重试（生产经验）

- **HTTP 200 ≠ 解析成功**：WeKnora 推送只代表入队，解析异步进行
- **Phase 0 实测（v0.7.1 生产环境，2026-08-17）**：
  - manual 推送 `status="publish"`：初始 `enable_status=disabled`，**约 45 秒内自动变为 enabled 并进入解析**（pending → finalizing → completed）
  - 结论：**status=publish + auto_reparse 即可正常解析，不需要 db_enable**（保持 WeKnora 独立性）
  - 生产版直连数据库的启用逻辑，可能是其早期版本未传 status（默认 draft 不解析）的历史遗留
  - `db_enable` 保留为兜底选项（默认关闭），仅当你确认推送后文档停在 disabled 时再开启
- 对账与重试：`python cli.py verify`（查 parse_status）、`python cli.py reparse`（重试 failed 文档）
- 内容上限：manual 单条约 20 万字符，客户端自动截断至 195000

## 命令行

```bash
cd app/server
python cli.py scan                    # 扫描源目录
python cli.py convert 文件.docx -o 输出.md
python cli.py batch --source /data/input   # 批量转换（增量跳过）
python cli.py watch                   # 前台监听源目录
python cli.py push 输出.md --title 标题    # 推送 MD 到 WeKnora
python cli.py push-file 原文件.pdf    # 推送原始文件（WeKnora 端解析）
python cli.py test-weknora            # 测试连接
```

## 配置参考

配置文件 `app/server/converter.yaml`（容器内 `/app/config/converter.yaml`），
所有项均可被环境变量覆盖（优先级：环境变量 > 文件 > 默认值）。

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `scanner.source_dir` | `CONVERTER_SOURCE_DIR` | `C:\Users\Administrator\Documents\AI` | 源目录（支持 Windows 路径） |
| `converter.output_dir` | `CONVERTER_OUTPUT_DIR` | `/data/output` | 输出 MD 目录 |
| `converter.drive_map` | — | `{}` | 盘符→挂载点，如 `{"C:": "/mnt/c"}` |
| `weknora.api_url` | `WEKNORA_URL` | `""` | WeKnora 地址（兼容带/不带 `/api/v1`） |
| `weknora.api_key` | `WEKNORA_API_KEY` | `""` | API Key（优先于账号密码） |
| `weknora.email/password` | `WEKNORA_EMAIL/PASSWORD` | `""` | JWT 登录凭据 |
| `weknora.dataset_id` | `WEKNORA_DATASET_ID` | `""` | 知识库 ID 或名称（空=第一个） |
| `weknora.auto_push` | `WEKNORA_AUTO_PUSH` | `true` | 转换后自动推送 |
| `weknora.push_rel_title` | `WEKNORA_PUSH_REL_TITLE` | `true` | 推送标题用相对路径 |
| `weknora.push_as_file` | `WEKNORA_PUSH_AS_FILE` | `true` | 以文件方式推送（fileName 携带相对路径 → WeKnora 文件夹结构） |
| `weknora.folder_kb_map_path` | `WEKNORA_FOLDER_KB_MAP` | `""` | 文件夹→知识库映射文件（多知识库路由，见下） |
| `weknora.auto_reparse` | `WEKNORA_AUTO_REPARSE` | `true` | 推送后自动触发 reparse |
| `weknora.db_enable` | `WEKNORA_DB_ENABLE` | `false` | 直连数据库启用文档（官方无启用 API；破坏独立性，谨慎） |
| `registry.backend` | `CONVERTER_REGISTRY_BACKEND` | `json` | 增量注册表后端：json 或 sqlite |
| `weknora.push_original_on_missing` | `WEKNORA_PUSH_ORIGINAL` | `true` | 无法转换→推送原始文件 |
| `batch.workers` | `BATCH_WORKERS` | `4` | 并行转换数（E5 多核可调 8） |
| `archive.seven_zip_path` | `ARCHIVE_SEVEN_ZIP_PATH` | `7zz` | 7-Zip 路径（rar/7z） |
| `chm.seven_zip_path` | `CHM_SEVEN_ZIP_PATH` | `7zz` | 7-Zip 路径（CHM） |
| `pdf.ocr_enabled` | `PDF_OCR_ENABLED` | `false` | 图片型 PDF OCR（需 tesseract） |
| `registry.path` | `CONVERTER_REGISTRY_PATH` | `/data/registry.json` | 增量注册表 |

## 转换器支持矩阵

| 格式 | 转换器 | 说明 |
|---|---|---|
| OFD | 纯 Python | 版面分析、字体/字号识别标题 |
| WPS / DOCX / WPSX | python-docx / FIB | 保留文档顺序、列表、1-6 级标题、表格 |
| XLSX / ET / ETX | openpyxl / xlrd | 表格转 MD、数值/日期格式化、`\|` 转义 |
| PDF | PyMuPDF | 文字型直接提取；图片型可选 Tesseract OCR |
| PPTX / DPSX | python-pptx | 每页文本、表格、组合图形、演讲备注 |
| HTML | bs4 + markdownify | 编码自动检测（GBK/UTF-8） |
| CHM | 7zz + bs4 | 解压、`.hhc` 章节分组、按章节输出 MD；超大页面走快速文本提取 |
| EML / MSG | email / extract-msg | 邮件头+正文+附件合并为单 MD |
| zip/rar/7z/tar | zipfile/tarfile/7zz | 解压后逐文件转换，保留内部结构 |
| PPT/DPS/DOC/XLS(旧版) | 占位 | 无法转换时回退推送原始文件到 WeKnora |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/formats` | 支持格式列表 |
| POST | `/api/convert` | 单文件转换（multipart） |
| POST | `/api/scan?source_dir=` | 扫描源目录 |
| POST | `/api/batch/start?push=&source_dir=` | 启动批量转换 |
| GET | `/api/batch/status` | 批量状态（含推送状态） |
| POST | `/api/batch/stop` / `/api/batch/clear` | 停止/清除 |
| GET | `/api/watcher/status` | 监听状态 |
| POST | `/api/watcher/start` / `/stop` | 启停监听 |
| POST | `/api/weknora/test` | 测试 WeKnora 连接 |
| GET | `/api/weknora/status` | WeKnora 状态 |
| GET | `/api/output?path=` | 浏览输出目录 |
| GET | `/api/output/download?path=` | 下载输出文件 |
| GET | `/api/config` | 查看配置 |
| POST | `/api/config/update` | 更新配置（Web UI 表单） |

## 测试

```bash
pip install -r app/server/requirements.txt pytest httpx
python -m pytest tests/ -v
```

## fnOS 应用中心安装

```bash
./build.sh    # 构建镜像并本地启动
```

fnOS 打包结构：`manifest` / `cmd/` / `config/` / `app/ui/`（CGI 反向代理到 8080）。

## 与 WeKnora 的对接细节（已对照 Tencent/WeKnora 源码核实）

- **认证**：配置 `api_key` 时使用 `X-API-Key` 头（官方中间件支持）；否则
  `email/password` 登录拿 JWT。官方登录响应**不含** tenant.api_key，
  若使用 API Key 需在 DocConverter 配置里直接填写（或在你的 fork 里注入）
- **推送**：默认以**文件方式**推送（`POST /api/v1/knowledge-bases/{kb}/knowledge/file`，
  multipart 的 `fileName` 携带相对路径如 `政策制度/2024/xxx.md`，
  WeKnora 端 `SplitKnowledgeRelativePath` 按 `/` 拆分为文件夹+文件名，
  **知识库内自动生成目录树**）；`push_as_file=false` 时退回 manual 推送
- **幂等去重**：WeKnora 按 (fileName+fileSize+fileHash) 判重，重复推送返回
  `409 duplicate_file` 时客户端按成功处理（拿到已有 doc_id，不产生重复文档）
- **权限要求**：推送账号需为知识库 **Admin / Editor** 角色（403 会给出明确提示）
- **大小上限**：单文件 ≤ 50MB（服务端 `MAX_FILE_SIZE_MB`，默认 50），超限会明确报错
- **原文回退**：无法转换的格式 → 推送原始文件（同样带 `fileName` 相对路径与
  `metadata.source_hash`）
- **本地去重**：`registry.json` 记录源文件指纹 + 推送状态；指纹不变且已推送 → 跳过
- **独立性**：DocConverter 只走公开 HTTP API，不读 WeKnora 数据库、不依赖其内部实现，
  WeKnora 升级（git pull / 换镜像）不影响 DocConverter

## 注意事项

- CHM / rar / 7z 依赖外部 `7zz`（[ip7z/7zip](https://github.com/ip7z/7zip) 提供），
  Docker 镜像未内置，需在镜像/宿主机安装后配置 `seven_zip_path`
- 图片型 PDF 的 OCR 默认关闭（`pdf.ocr_enabled=false`），开启需安装 tesseract-ocr + 中文语言包
- `.gd`（WPS 加密公文）等私有加密格式无法解析，走原文回退推送
- 目录不可读/损坏的条目在扫描时自动跳过，不影响整体任务
