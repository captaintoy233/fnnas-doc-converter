"""配置一致性：转换器注册表 vs 扫描白名单

背景（真实踩坑）：v2.2.0 新增了 TextConverter(.md/.txt/.csv)、RTFConverter、
.gd 支持，也更新了 config.py 的代码默认值，但 **converter.yaml 模板里的
`include_extensions` 白名单一直没同步**。配置优先级是
「环境变量 > 配置文件 > 代码默认值」，于是所有带 converter.yaml 的部署
（即全部）都静默排除了这些新格式——转换器注册好了，文件却永远扫不到。

这类"两边都要改"的地方最容易漏，用测试锁死。
"""
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "server"))

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "server")


def _yaml_cfg():
    with open(os.path.join(SERVER_DIR, "converter.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _code_default():
    import config
    # DEFAULT_CONFIG 在不同版本里名字可能不同，兜底取模块内首个 dict 常量
    d = getattr(config, "DEFAULT_CONFIG", None)
    if d is None:
        d = getattr(config, "_DEFAULT_CONFIG", None)
    if d is None:
        for name in dir(config):
            v = getattr(config, name)
            if isinstance(v, dict) and "scanner" in v and "converter" in v:
                d = v
                break
    assert d, "找不到 config 里的默认配置字典"
    return d


def test_template_covers_code_default_extensions():
    """converter.yaml 模板必须覆盖代码默认的全部扩展名"""
    tmpl = set(_yaml_cfg()["scanner"]["include_extensions"])
    default = set(_code_default()["scanner"]["include_extensions"])
    assert default <= tmpl, "模板缺少代码默认里的扩展名: %s" % sorted(default - tmpl)


def test_all_registered_extensions_are_scannable():
    """凡是注册了真实转换器的扩展名，都必须能被扫描到

    否则就是"转换器写好了但永远不生效"——.md/.txt/.csv 就这样漏过一次。
    """
    from converters import registry
    from converters.loader import register_all
    try:
        register_all(registry)
    except Exception as e:  # 缺依赖时跳过，避免环境问题导致误报
        pytest.skip("register_all 失败: %s" % e)
    registered = set(registry.list_supported_extensions())
    scanned = set(_code_default()["scanner"]["include_extensions"])
    missing = sorted(registered - scanned)
    assert not missing, "已注册但扫不到的扩展名: %s" % missing


def test_template_scans_new_formats():
    """显式锁定新增格式，避免以后又被回退掉"""
    tmpl = set(_yaml_cfg()["scanner"]["include_extensions"])
    for ext in (".md", ".txt", ".csv", ".rtf", ".gd"):
        assert ext in tmpl, "%s 不在 converter.yaml 的 include_extensions 里" % ext


def test_underscore_excluded_from_push():
    """'_*' 必须排除，否则 _manifest.json / _未编目/ 会被当知识文档推送"""
    pats = set(_yaml_cfg()["scanner"]["exclude_patterns"])
    assert "_*" in pats, "converter.yaml 的 exclude_patterns 缺少 _*"
    assert "_*" in set(_code_default()["scanner"]["exclude_patterns"])
