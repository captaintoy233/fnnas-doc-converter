# DocConverter × WeKnora 两轮项目检查报告

> 检查方式：两轮独立走查（docconverter 代码 / WeKnora 源码 / 对接契约三个视角并行），
> 依据 Tencent/WeKnora 官方源码（internal/handler、internal/application/service）逐行核对。
> 日期：本版本（v2.1.0）检查结果。

---

## 一、第一轮：docconverter 项目代码审查

### [严重] 已修复
| # | 问题 | 修复 |
|---|------|------|
| S3 | `pdf.ocr_command` 经 `sh -c "command -v ..."` 拼接执行 → 配置被改后 RCE | 改用 `shutil.which` + 拒绝 shell 元字符 |
| S4 | zip/tar 无解压配额（zip 炸弹）、7z 解压路径未校验 | 新增解压配额（默认 2GB/10万条）+ 7z 产物越界校验删除 |
| S2 | `/api/config` 明文泄露 password/api_key | 凭据脱敏（`***`），保存时保留原值 |
| H2 | `/api/convert` async 内同步阻塞大文件 → 事件循环冻结 | 改同步端点（FastAPI 线程池） |
| H4 | 上传无大小限制 | `converter.max_upload_mb`（默认 1024MB） |

### [重要] 已修复
| # | 问题 | 修复 |
|---|------|------|
| H1 | UI 存储型 XSS：file.name 未转义、onclick 用 escHtml 拼路径 | file.name 转义；onclick 统一 `JSON.stringify` |
| H5 | 批次运行中 `clear_batch` 可清空 → 并发写输出/统计错乱 | running/stopping 时拒绝清除 |
| H6 | `push=true` 覆盖参数失效（auto_push=false 时不推送） | `enqueue_push` 接收 use_push |
| H9 | 转换器注册与配置热更新不同步（chm.7z 路径改后不生效） | `registry.rebuild()` + update 后重建 |
| H7-部分 | registry `_save` 静默失败 | 保留建议：见"待落地" |

### 已修复（第二轮追加）
| # | 问题 | 修复 |
|---|------|------|
| S1 | 全 API 无认证 + CORS 全开 | 可选认证令牌 `server.auth_token`：设置后所有 /api/* 需 `Authorization: Bearer <token>`（健康检查/首页除外） |
| S5 | api_url 任意可控（SSRF/凭据外带） | 校验必须 http/https，`is_configured`/`test_connection` 拒绝非法 scheme |
| H3 | /api/output 全量读入内存 + 非 UTF-8 崩溃 | 预览限 5MB；下载改流式 FileResponse |
| H13 | Docker root + 缺 7zz/tesseract | 非 root（nobody）+ 内置 p7zip/tesseract-ocr-chi-sim/noto-cjk 字体 |
| L1 | 弱默认口令 + MinIO 公网暴露 | compose 强制 .env 提供 PG/Redis/MinIO 口令；MinIO 不再暴露宿主端口 |
| L4 | config update 无类型校验 + 批次线程无保护 | 叶子键类型白名单（400 拒绝非法类型）；批次线程异常复位状态 |

### 待落地（按优先级）
1. **[重要] 推送队列**：无界内存队列 + 单消费者 + daemon 线程 → 建议有界队列+背压、多消费者、失败任务持久化、优雅退出 drain
2. **[重要] registry 可靠性**：崩溃半写丢失 → 建议直写前备份、批量聚合落盘、批量前 `prune()` 清理已删源文件
3. **[重要] 推送结果校验**：轮询 `parse_status`、失败自动 reparse、UI 展示解析状态
4. **[重要] refresh_token / X-Tenant-ID**：401 先刷新；平台级 API Key 需 tenant_id 配置
5. **[一般] `available` 运行时探测**（镜像现已内置依赖，此条降级）
6. **[一般] XML 实体扩展 DoS**（ofd/docx 底层解析）→ defusedxml
7. **[一般] 归档转换全量驻留内存** → 边转边写
8. **[一般] watcher 在批次运行期间丢新文件** → 积压队列
9. **[一般] 版本号不统一**（manifest 1.0.1 vs APP_VERSION 2.1.0）

---

## 二、第二轮：WeKnora 源码 + 对接契约审查

### 源码核对关键结论（均已落实到客户端）
| 发现 | 源码依据 | 处理 |
|------|---------|------|
| **manual status 只接受 `draft`/`publish`**（文档写 `published` 是漂移） | `knowledge_create.go` 校验 | ✅ 改为 `publish`（原实现必 400） |
| **file 类型知识不支持内容更新**，同名同内容判重（fileName+size+hash） | `kb_create.go` CheckKnowledgeExists | ✅ 内容变化 → 先 DELETE 旧文档再推送（`delete_replaced`） |
| **判重不含 folder_path**：不同目录同名同内容会被 409 误拒 | `kb_create.go:90-96`（FileName 只取 basename） | ✅ 本地 output_docs 按输出相对路径追踪 doc_id |
| **登录响应无 `tenant.api_key`**（官方 DTO 只有 active_tenant；你 fork 注入的） | `auth_dto.go` | ✅ 优雅降级 JWT；API Key 需显式配置 |
| **X-API-Key 官方支持**，但平台级 key 需 `X-Tenant-ID` 头 | `mw_auth.go` | 待落地：可选 tenant_id 配置 |
| **parse_status 异步**：push 成功 ≠ 解析成功，失败停在 failed | `knowledge.go` | 待落地：verify 命令/UI 状态展示 |
| **reparse API 存在**（单条 + batch ≤200/批） | `knowledge.go` | 待落地：失败自动重试 reparse |
| **50MB 上限**（MAX_FILE_SIZE_MB，nginx 也参与） | `filesize.go` | ✅ `weknora.max_file_size_mb` 可配置 |
| **文件夹结构**：fileName 相对路径 → SplitKnowledgeRelativePath 自动建目录 | `kb_folder.go` | ✅ 文件方式推送默认开启 |

### 对接契约剩余风险（待落地）
1. **[重要] 推送结果校验**：`POST` 返回 `parse_status=processing`，需轮询 `GET /knowledge/{id}` 确认 completed；建议加 `verify` 命令 + UI 展示解析状态 + 失败自动 reparse
2. **[重要] refresh_token**：登录响应有 refresh_token（7 天），客户端应 401 时先刷新再重登，抗密码轮换
3. **[重要] 平台级 X-API-Key**：需要 `X-Tenant-ID` 头，客户端加可选 `weknora.tenant_id`
4. **[重要] 推送失败无手动重推**：新增 `POST /api/batch/retry-push` + UI 重推按钮 + 展示 push_queue_size/失败数
5. **[一般] metadata 值需强转 str**（服务端 `map[string]string`，数字会 400）
6. **[一般] 超时随文件大小缩放**、429 尊重 `Retry-After`、JWT exp 解析
7. **[一般] rel_path 归一化对齐服务端**（去 `..`、段长 128/深度 16/总长 1024）

---

## 三、运维建议（WeKnora 侧）

1. **KB 配置 `parser_engine_rules` 加 `.md → builtin`**，确保推送的 md 走内置解析器（不依赖外部 docreader 网络/模型）
2. **推送账号需为 KB Admin/Editor**（官方 403 语义）
3. **升级 WeKnora 后先跑冒烟脚本**再放量（文档与源码漂移多：tag_ids、status、active_tenant、swagger 滞后）
4. **对账**：`GET /api/weknora/list` 已提供，可对照 parse_status 清理 failed 文档
5. **知识库判重不含文件夹**：同一内容复制到不同目录会只留一条——组织文档时避免跨目录复制

---

## 三·五、对接契约审查结论核对（子代理报告 vs 落地状态）

| 子代理发现 | 状态 |
|-----------|------|
| manual status "published" 必 400 | ✅ 已改 `publish`，mock 严格校验 |
| /api/config 明文凭据 | ✅ 已脱敏 + 可选认证令牌 |
| 内容变化无更新语义（KB 累积旧版） | ✅ delete_replaced + output_docs 删除重建 |
| 判重不含 folder_path（跨目录同名被 409 误拒） | ✅ 本地按输出相对路径追踪 doc_id；文档已说明约束 |
| 每任务新建客户端 → 千文件千次登录 | ✅ 按配置签名缓存客户端 |
| 多输出源增量跳过失效（每批全量重转） | ✅ 按 output_files 存在性判断 |
| 推送队列单消费者/无界/丢任务 | ⏳ 待办（有界+多消费者+持久化） |
| refresh_token 未用 / X-Tenant-ID 缺失 | 🟡 refresh_token 待办；**X-Tenant-ID 已加**（weknora.tenant_id） |
| **HTTP 200 ≠ 推送成功**（asynq 入队失败仍 200，需轮询 parse_status） | ✅ 新增 `verify_knowledge` / CLI `verify` / API `/api/weknora/verify` + `/verify-pushed` 对账 |
| 登录字段实为 active_tenant，api_key 拾取是死代码 | ✅ 已修正注释与拾取逻辑（兼容 fork） |
| 50MB 硬编码 | ✅ weknora.max_file_size_mb 可配置 |
| api_url 归一化边界 | ✅ 增加 http/https 校验与明确报错 |
| metadata 非字符串 400 | ✅ batch 元数据全为字符串（文档注明） |
| 429 未尊重 Retry-After / 超时不随大小缩放 | ⏳ 待办 |

## 三·六、第三轮审查（DeepSeek-V4-Pro 视角）新增修复

| 发现 | 问题 | 修复 |
|------|------|------|
| 更新语义删除顺序 | 先删旧文档再推送，推送失败会丢文档 | 改为**推送成功后（非重复）再删旧文档**（多文件与 push_original 两处） |
| 认证中间件 | OPTIONS 预检被 401 拦截，启用认证后 CORS 失效 | 放行 OPTIONS |
| UI 令牌 | 启用认证后 UI 的 fetch 不带令牌，整页 401 | 全局 fetch 包装：localStorage 存令牌 + 401 提示输入重试 |
| 单文件解压炸弹 | .gz/.bz2/.xz 解压无配额（1MB→10GB） | extract_single 加配额检查 |
| save_config 原子性 | 直接 open("w") 全量写，崩溃半写损坏配置 | 临时文件 + fsync + os.replace（回退直写） |

## 四、本轮已落地的修复清单（30 项测试通过）

- manual status `publish` 修复（原必 400）＋ mock 严格校验
- `/api/config` 凭据脱敏 + 保存保留原值
- 配置热更新后重建转换器（registry.rebuild）
- `push=true` 覆盖参数生效
- UI XSS（file.name 转义 + onclick JSON.stringify）
- 多输出源（CHM/压缩包）增量跳过按 output_files 存在性判断
- 推送客户端按配置签名缓存（千文件批次一次登录）
- 运行中禁止 clear_batch
- `/api/convert` 线程池化 + 上传上限
- 解压配额（zip 炸弹防护）+ 7z 解压路径校验
- OCR 命令注入修复
- 内容变化 → 删除旧文档再推送（delete_replaced + output_docs 追踪）
- 50MB 上限可配置
- mock 新增 DELETE 端点；新增更新语义、删除、多文件推送等测试
