"""LangSmith 追踪接线测试模块（文件级说明）。

覆盖 app/core/tracing.py 的 setup_tracing()：
- 未配置（langsmith_tracing=False 默认）：不注入任何 LangSmith 追踪环境变量
  （先清理基线再断言），零开销零副作用；
- 配置后（monkeypatch settings 开启）：LANGSMITH_TRACING / LANGSMITH_API_KEY /
  LANGSMITH_PROJECT 及旧版兼容名 LANGCHAIN_TRACING_V2 均正确注入；
- 重复调用幂等：多次调用结果一致且不抛异常。

说明：settings 为模块级单例，用 monkeypatch.setattr 临时改写字段，用例结束
自动还原；setup_tracing() 新写入的进程环境变量由 clean_tracing_env 夹具在
teardown 时清除，防止泄漏到其他用例。
"""

import os

import pytest

from app.core.config import settings
from app.core.tracing import setup_tracing

# setup_tracing 可能写入的全部环境变量名（清理基线与 teardown 共用）
_TRACING_ENV_NAMES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_API_KEY",
)


@pytest.fixture
def clean_tracing_env(monkeypatch):
    """清理 LangSmith 相关环境变量的夹具（测试隔离）。

    setup 前删除全部追踪环境变量保证断言基线干净；teardown 时再次清除
    setup_tracing() 写入的新值，防止污染同进程其他用例。

    Args:
        monkeypatch: pytest 内建夹具（delenv 记录原始状态，teardown 自动还原）。

    Yields:
        None。

    Raises:
        无。
    """
    for name in _TRACING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
    for name in _TRACING_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_setup_tracing_weipeizhi_buzhuru(clean_tracing_env, monkeypatch):
    """未启用追踪：setup_tracing() 不注入任何追踪环境变量。

    Args:
        clean_tracing_env: 环境变量基线清理夹具。
        monkeypatch: 用于临时关闭 settings.langsmith_tracing。

    Returns:
        None。

    Raises:
        AssertionError: 环境变量被意外注入时中断。
    """
    monkeypatch.setattr(settings, "langsmith_tracing", False)
    setup_tracing()
    assert "LANGSMITH_TRACING" not in os.environ
    assert "LANGCHAIN_TRACING_V2" not in os.environ
    assert "LANGSMITH_API_KEY" not in os.environ
    assert "LANGSMITH_PROJECT" not in os.environ


def test_setup_tracing_qiyong_zhuru(clean_tracing_env, monkeypatch):
    """启用追踪：注入新旧两套环境变量，且重复调用幂等。

    Args:
        clean_tracing_env: 环境变量基线清理夹具。
        monkeypatch: 用于临时开启 settings.langsmith_tracing 并写入测试 Key。

    Returns:
        None。

    Raises:
        AssertionError: 注入值或幂等性不符合预期时中断。
    """
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "test-key")
    monkeypatch.setattr(settings, "langsmith_project", "mangaflow-test")

    setup_tracing()
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "test-key"
    assert os.environ["LANGSMITH_PROJECT"] == "mangaflow-test"
    # 旧版兼容命名同步注入
    assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
    assert os.environ["LANGCHAIN_API_KEY"] == "test-key"

    # 幂等：重复调用不抛异常且结果一致
    setup_tracing()
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "test-key"
    assert os.environ["LANGSMITH_PROJECT"] == "mangaflow-test"
