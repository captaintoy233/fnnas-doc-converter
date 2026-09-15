"""
DocConverter 配置系统
优先级: 环境变量 > 配置文件 > 默认值
"""
import os, yaml, json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

APP_VERSION = "2.2.0"
APP_NAME = "DocConverter"

CONFIG_PATHS = [
    Path("/app/config/converter.yaml"),
    Path("converter.yaml"),
    Path.home() / ".docconverter.yaml",
    Path("/data/config/converter.yaml"),
]

DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        # 可选 API 认证令牌：为空则不鉴权（内网模式）；
        # 设置后所有 /api/* 需携带 Authorization: Bearer <token> 或 X-Auth-Token 头
        "auth_token": "",
    },
    "converter": {
        "upload_dir": "/data/uploads",
        "output_dir": "/data/output",
        "temp_dir": "/data/tmp",
        "keep_uploaded": False,
        # 输出镜像目录：转换产物额外同步一份到此目录（空 = 不镜像）
        "output_mirror": "",
        "max_upload_mb": 1024,        # 单文件 API 上传上限 (MB)
        # Windows 盘符/共享 → 本地挂载点映射（容器内运行时配置）
        # 例: {"C:": "/mnt/c", "\\\\nas": "/mnt/nas"}
        "drive_map": {},
    },
    "scanner": {
        "enabled": False,
        "source_dir": "/data/input",
        "recursive": True,
        "include_extensions": [
            ".ofd", ".wps", ".wpsx", ".dpsx", ".docx", ".xlsx", ".et", ".etx",
            ".pdf", ".htm", ".html", ".pptx", ".ppt", ".dps",
            ".doc", ".xls", ".gd", ".rtf", ".chm",
            ".eml", ".msg",
            ".md", ".txt", ".csv",
            ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz",
        ],
        # '_*' 用于排除导出产物的内部文件/目录（_manifest.json、_index.md、
        # _废止清单.md、_未编目/ 等），避免被当作知识文档推送
        "exclude_patterns": ["~*", ".*", "_*", "*.tmp", "*.bak"],
        "poll_interval": 60,
    },
    "watcher": {
        "enabled": False,
        "source_dir": "/data/input",
        "recursive": True,
        "debounce": 5,
    },
    "weknora": {
        "enabled": False,
        "api_url": "",
        "api_key": "",
        "email": "",
        "password": "",
        "dataset_id": "",
        "auto_push": True,
        "push_original_on_missing": True,
        "push_rel_title": True,        # 推送时标题用相对路径（保留目录结构）
        "push_as_file": True,          # 以文件方式推送（fileName 携带相对路径 → WeKnora 文件夹）
        "delete_replaced": True,       # 内容变化时先删除旧文档再推送（file 类型不支持更新）
        "folder_kb_map_path": "",      # 文件夹→知识库映射文件路径（空=不启用多知识库路由）
        "auto_reparse": True,          # 推送后自动触发 reparse（官方解析异步，需显式触发）
        "db_enable": False,            # 推送后直连数据库启用文档（官方无启用 API；需在 WeKnora 网络内，破坏独立性，谨慎开启）
        "db_host": "postgres",         # db_enable 时的 Postgres 地址
        "db_port": 5432,
        "db_name": "weknora",
        "db_user": "postgres",
        "db_password": "",
        "max_file_size_mb": 50,        # 与服务端 MAX_FILE_SIZE_MB 对齐（默认 50）
        "tenant_id": "",               # 平台级 API Key 时的 X-Tenant-ID（可选）
        "delete_source_after_push": False,
        "retry_count": 3,
        "push_queue_size": 200,        # 有界推送队列容量（满则背压，等待消费）
        "push_workers": 2,             # 推送消费者线程数
        "push_failed_path": "/data/push_failed.jsonl",  # 推送失败持久化（审计/重推）
    },
    "batch": {
        "workers": 4,
        "retry_count": 3,
        "retry_delay": 5,
    },
    "archive": {
        "seven_zip_path": "7zz",
        # 源文件消失时移入的归档目录（空 = 不归档，仅从知识库删除）
        "dir": "",
        "max_depth": 3,               # 嵌套归档最大深度
        "keep_extracted": False,       # 是否在输出目录保留解压出的原始文件
        "max_extract_mb": 0,          # 解压配额 (MB, 0=默认 2GB)
        "max_extract_entries": 0,     # 解压条目数配额 (0=默认 10 万)
    },
    "chm": {
        "seven_zip_path": "7zz",
        "extract_workers": 8,
        # 输出布局: tree = 按 .hhc 索引层级建文件夹、每文档一个 MD（推荐，利于知识库）
        #           chapter = 每个顶层章节合并为一个 MD（旧行为）
        "output_layout": "tree",
        # 是否在 MD 头部写入 YAML 元数据（title/status/toc_path/content_hash 等）
        "front_matter": True,
        # 批处理共用输出根目录时，以 CHM 名建一层命名空间目录，避免多本手册混树
        "namespace_output": True,
        # 跳过 Word/Excel 导出 HTML 的空壳辅助页（header/tabstrip/tabscript 等）
        "skip_scaffold": True,
        # 渲染并行方式：文档数 >= mp_threshold 时用多进程（绕开 GIL，
        # 实测 4471 篇 127s vs 多线程 25 分钟），否则用线程
        "render_processes": True,
        "mp_threshold": 50,
    },
    "pdf": {
        "ocr_enabled": False,
        "ocr_command": "tesseract",
        "ocr_lang": "chi_sim+eng",
        "ocr_dpi": 200,
    },
    "libreoffice": {
        "soffice_path": "",             # 空=自动检测；指定路径如 /usr/bin/soffice
    },
    "ocr": {
        "enabled": False,               # 是否对图片做 OCR 并把文字注入 MD
        "backend": "rapidocr",          # 纯 CPU / ONNX Runtime
        "cache_dir": "~/.cache/docconverter/ocr",
        "intra_op_num_threads": 2,      # 进程数 × 该值 ≈ 物理核数
        "tile_threshold": 960,          # 最长边超过则分块（避免缩放漏检小字）
        "tile_size": 960,
        "tile_overlap": 96,
        "min_score": 0.5,
        "min_image_side": 32,           # 任一边小于该值的碎片图跳过
        "max_side_len": 2000,
        "det_limit_side_len": 736,
        "label": "图片文字",
        "placeholder_missing": True,    # 图片缺失时写占位文本而非死引用
        # 常驻服务（容器批处理）无独立 OCR 预处理阶段，需开启内联回退
        "inline_fallback": False,
    },
    "htm": {
        # HTML 图片处理：留空则跟随 ocr.enabled（启用 OCR 时用 link）
        "image_mode": "",               # none | link | base64
    },
    "registry": {
        "backend": "json",            # json | sqlite
        "path": "/data/registry.json",
        "sqlite_path": "/data/registry.sqlite",
        "prune_on_batch": False,      # 批次前清理已删源文件记录（仅在批次恒为全量源目录扫描时开启）
        # 源文件消失时：从知识库删除对应文档（+ 可选归档源文件）。
        # 仅在全量扫描批次中生效，避免把"部分批次的缺席"误判为"已删除"。
        "handle_missing": False,
    },
}

# 环境变量映射表: (env_var, config_path, type)
ENV_MAP = [
    ("CONVERTER_SERVER_PORT", ["server", "port"], int),
    ("CONVERTER_AUTH_TOKEN", ["server", "auth_token"], str),
    ("CONVERTER_SERVER_HOST", ["server", "host"], str),
    ("CONVERTER_OUTPUT_DIR", ["converter", "output_dir"], str),
    ("CONVERTER_UPLOAD_DIR", ["converter", "upload_dir"], str),
    ("CONVERTER_TEMP_DIR", ["converter", "temp_dir"], str),
    ("CONVERTER_KEEP_UPLOADED", ["converter", "keep_uploaded"], bool),
    ("CONVERTER_OUTPUT_MIRROR", ["converter", "output_mirror"], str),
    ("CONVERTER_MAX_UPLOAD_MB", ["converter", "max_upload_mb"], int),
    ("CONVERTER_SOURCE_DIR", ["scanner", "source_dir"], str),
    ("CONVERTER_RECURSIVE", ["scanner", "recursive"], bool),
    ("CONVERTER_POLL_INTERVAL", ["scanner", "poll_interval"], int),
    ("CONVERTER_WATCH_ENABLED", ["watcher", "enabled"], bool),
    ("CONVERTER_WATCH_DIR", ["watcher", "source_dir"], str),
    ("CONVERTER_WATCH_DEBOUNCE", ["watcher", "debounce"], int),
    ("WEKNORA_URL", ["weknora", "api_url"], str),
    ("WEKNORA_API_KEY", ["weknora", "api_key"], str),
    ("WEKNORA_DATASET_ID", ["weknora", "dataset_id"], str),
    ("WEKNORA_ENABLED", ["weknora", "enabled"], bool),
    ("WEKNORA_AUTO_PUSH", ["weknora", "auto_push"], bool),
    ("WEKNORA_EMAIL", ["weknora", "email"], str),
    ("WEKNORA_PASSWORD", ["weknora", "password"], str),
    ("WEKNORA_PUSH_ORIGINAL", ["weknora", "push_original_on_missing"], bool),
    ("WEKNORA_PUSH_REL_TITLE", ["weknora", "push_rel_title"], bool),
    ("WEKNORA_PUSH_AS_FILE", ["weknora", "push_as_file"], bool),
    ("WEKNORA_MAX_FILE_SIZE_MB", ["weknora", "max_file_size_mb"], int),
    ("WEKNORA_TENANT_ID", ["weknora", "tenant_id"], str),
    ("WEKNORA_FOLDER_KB_MAP", ["weknora", "folder_kb_map_path"], str),
    ("WEKNORA_AUTO_REPARSE", ["weknora", "auto_reparse"], bool),
    ("WEKNORA_DB_ENABLE", ["weknora", "db_enable"], bool),
    ("WEKNORA_DB_HOST", ["weknora", "db_host"], str),
    ("WEKNORA_DB_PORT", ["weknora", "db_port"], int),
    ("WEKNORA_DB_NAME", ["weknora", "db_name"], str),
    ("WEKNORA_DB_USER", ["weknora", "db_user"], str),
    ("WEKNORA_DB_PASSWORD", ["weknora", "db_password"], str),
    ("CONVERTER_REGISTRY_BACKEND", ["registry", "backend"], str),
    ("CONVERTER_REGISTRY_SQLITE", ["registry", "sqlite_path"], str),
    ("WEKNORA_DELETE_REPLACED", ["weknora", "delete_replaced"], bool),
    ("WEKNORA_PUSH_QUEUE_SIZE", ["weknora", "push_queue_size"], int),
    ("WEKNORA_PUSH_WORKERS", ["weknora", "push_workers"], int),
    ("WEKNORA_PUSH_FAILED_PATH", ["weknora", "push_failed_path"], str),
    ("BATCH_WORKERS", ["batch", "workers"], int),
    ("BATCH_RETRY_COUNT", ["batch", "retry_count"], int),
    ("BATCH_RETRY_DELAY", ["batch", "retry_delay"], int),
    ("ARCHIVE_SEVEN_ZIP_PATH", ["archive", "seven_zip_path"], str),
    ("ARCHIVE_MAX_DEPTH", ["archive", "max_depth"], int),
    ("ARCHIVE_KEEP_EXTRACTED", ["archive", "keep_extracted"], bool),
    ("CHM_SEVEN_ZIP_PATH", ["chm", "seven_zip_path"], str),
    ("CHM_EXTRACT_WORKERS", ["chm", "extract_workers"], int),
    ("CHM_OUTPUT_LAYOUT", ["chm", "output_layout"], str),
    ("CHM_FRONT_MATTER", ["chm", "front_matter"], bool),
    ("CHM_NAMESPACE_OUTPUT", ["chm", "namespace_output"], bool),
    ("PDF_OCR_ENABLED", ["pdf", "ocr_enabled"], bool),
    ("PDF_OCR_COMMAND", ["pdf", "ocr_command"], str),
    ("PDF_OCR_LANG", ["pdf", "ocr_lang"], str),
    ("LIBREOFFICE_SOFFICE_PATH", ["libreoffice", "soffice_path"], str),
    ("OCR_ENABLED", ["ocr", "enabled"], bool),
    ("OCR_BACKEND", ["ocr", "backend"], str),
    ("OCR_CACHE_DIR", ["ocr", "cache_dir"], str),
    ("OCR_THREADS", ["ocr", "intra_op_num_threads"], int),
    ("OCR_TILE_THRESHOLD", ["ocr", "tile_threshold"], int),
    ("OCR_MIN_IMAGE_SIDE", ["ocr", "min_image_side"], int),
    ("OCR_INLINE_FALLBACK", ["ocr", "inline_fallback"], bool),
    ("CONVERTER_REGISTRY_PATH", ["registry", "path"], str),
    ("REGISTRY_HANDLE_MISSING", ["registry", "handle_missing"], bool),
    ("ARCHIVE_DIR", ["archive", "dir"], str),
    ("CHM_SKIP_SCAFFOLD", ["chm", "skip_scaffold"], bool),
    ("CHM_RENDER_PROCESSES", ["chm", "render_processes"], bool),
    ("CHM_MP_THRESHOLD", ["chm", "mp_threshold"], int),
]


def deep_set(d, keys, value):
    """Set nested dict value by key path."""
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def deep_get(d, keys):
    """Get nested dict value by key path."""
    for k in keys:
        d = d.get(k) if isinstance(d, dict) else None
        if d is None:
            return None
    return d


def _config_paths() -> list:
    """配置文件路径列表（可用 CONVERTER_CONFIG_FILE 覆盖，测试/多实例用）"""
    override = os.environ.get("CONVERTER_CONFIG_FILE")
    if override:
        return [Path(override)]
    return CONFIG_PATHS


def load_config() -> dict:
    """加载配置: Yaml文件 → 环境变量覆盖"""
    config = dict(DEFAULT_CONFIG)

    # 1. 尝试加载 YAML 配置文件
    for path in _config_paths():
        if path.exists():
            with open(path) as f:
                try:
                    yaml_config = yaml.safe_load(f)
                    if isinstance(yaml_config, dict):
                        _deep_merge(config, yaml_config)
                except Exception:
                    pass
            break

    # 2. 环境变量覆盖
    for env_var, keys, typ in ENV_MAP:
        val = os.environ.get(env_var)
        if val is not None:
            try:
                if typ == bool:
                    parsed = val.lower() in ("1", "true", "yes", "on")
                else:
                    parsed = typ(val)
                deep_set(config, keys, parsed)
            except (ValueError, TypeError):
                pass

    # 3. 创建所需目录
    for dir_key in ["upload_dir", "output_dir", "temp_dir"]:
        dir_path = deep_get(config, ["converter", dir_key])
        if dir_path:
            try:
                Path(dir_path).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

    return config


def _deep_merge(base, override):
    """递归合并字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def save_config(updates: dict) -> bool:
    """将部分配置更新写入配置文件（第一个可写的 CONFIG_PATHS），并热重载。

    优先级: 已有配置文件 > 当前目录 converter.yaml > 家目录。

    Args:
        updates: 与 config 结构一致的嵌套 dict（部分更新即可）

    Returns:
        True 保存成功并已重载；False 无任何可写位置
    """
    current = get_config()
    merged = json.loads(json.dumps(current))
    _deep_merge(merged, updates)

    target = None
    for p in _config_paths():
        if p.exists():
            target = p
            break
    if target is None:
        target = _config_paths()[0]

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, target)   # 原子替换
        except OSError:
            # 单文件 bind mount 等不支持 rename 的场景：回退直写
            with open(target, "w", encoding="utf-8") as f:
                yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
            try:
                tmp.unlink()
            except OSError:
                pass
    except Exception:
        return False

    reload_config()
    return True


# 全局配置实例
_config = None


def get_config() -> dict:
    """获取配置（懒加载+缓存）"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> dict:
    """重新加载配置"""
    global _config
    _config = load_config()
    return _config


def config_to_json(config: dict = None) -> str:
    """配置转 JSON（用于 API 返回）"""
    if config is None:
        config = get_config()
    return json.dumps(config, indent=2, ensure_ascii=False)
