"""pytest 全局配置：测试用临时目录环境变量"""
import os
import tempfile

# 在导入任何应用模块前设置测试环境
_TMP = tempfile.mkdtemp(prefix="dc_test_")
os.environ["CONVERTER_UPLOAD_DIR"] = os.path.join(_TMP, "uploads")
os.environ["CONVERTER_OUTPUT_DIR"] = os.path.join(_TMP, "output")
os.environ["CONVERTER_SOURCE_DIR"] = os.path.join(_TMP, "input")
os.environ["CONVERTER_TEMP_DIR"] = os.path.join(_TMP, "tmp")
os.environ["CONVERTER_REGISTRY_PATH"] = os.path.join(_TMP, "registry.json")
os.environ["CONVERTER_CONFIG_FILE"] = os.path.join(_TMP, "converter.yaml")
os.environ["WEKNORA_ENABLED"] = "false"

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "app" / "server"
sys.path.insert(0, str(SERVER_DIR))

import pytest


def pytest_addoption(parser):
    """注册自定义命令行参数"""
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="Update snapshot baselines instead of comparing against them",
    )


@pytest.fixture()
def snapshot_update(request):
    """是否处于快照更新模式"""
    return request.config.getoption("--snapshot-update")


@pytest.fixture()
def server_dir():
    return SERVER_DIR


@pytest.fixture()
def sample_docx(server_dir):
    p = server_dir.parent.parent / "test_samples" / "test_sample.docx"
    return p if p.exists() else None


@pytest.fixture()
def sample_ofd(server_dir):
    p = server_dir.parent.parent / "test_samples" / "test_sample.ofd"
    return p if p.exists() else None


@pytest.fixture()
def sample_wps(server_dir):
    p = server_dir.parent.parent / "test_samples" / "test_sample.wps"
    return p if p.exists() else None
