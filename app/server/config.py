"""
DocConverter 配置系统
优先级: 环境变量 > 配置文件 > 默认值
"""
import os, yaml, json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

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
    },
    "converter": {
        "upload_dir": "/data/uploads",
        "output_dir": "/data/output",
        "temp_dir": "/data/tmp",
        "keep_uploaded": False,
    },
    "scanner": {
        "enabled": False,
        "source_dir": "/data/input",
        "recursive": True,
        "include_extensions": [
            ".ofd", ".wps", ".docx", ".xlsx", ".et",
            ".pdf", ".htm", ".html", ".pptx", ".ppt", ".dps"
        ],
        "exclude_patterns": ["~*", ".*", "*.tmp", "*.bak"],
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
        "delete_source_after_push": False,
    },
    "batch": {
        "workers": 2,
        "retry_count": 3,
        "retry_delay": 5,
    },
}

# 环境变量映射表: (env_var, config_path, type)
ENV_MAP = [
    ("CONVERTER_SERVER_PORT", ["server", "port"], int),
    ("CONVERTER_SERVER_HOST", ["server", "host"], str),
    ("CONVERTER_OUTPUT_DIR", ["converter", "output_dir"], str),
    ("CONVERTER_UPLOAD_DIR", ["converter", "upload_dir"], str),
    ("CONVERTER_TEMP_DIR", ["converter", "temp_dir"], str),
    ("CONVERTER_KEEP_UPLOADED", ["converter", "keep_uploaded"], bool),
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
    ("BATCH_WORKERS", ["batch", "workers"], int),
    ("BATCH_RETRY_COUNT", ["batch", "retry_count"], int),
    ("BATCH_RETRY_DELAY", ["batch", "retry_delay"], int),
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


def load_config() -> dict:
    """加载配置: Yaml文件 → 环境变量覆盖"""
    config = dict(DEFAULT_CONFIG)

    # 1. 尝试加载 YAML 配置文件
    for path in CONFIG_PATHS:
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
