"""通用 Pydantic Schema 模块（文件级说明）。

定义跨模块复用的通用响应模型：分页结果 PageResult[T]、统一消息 Message。
"""

from typing import Generic, List, TypeVar

from pydantic import BaseModel

# 分页项类型变量
T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    """通用分页结果。

    Attributes:
        total: 总记录数。
        page: 当前页码（从 1 开始）。
        page_size: 每页条数。
        items: 当前页数据列表。
    """

    total: int
    page: int
    page_size: int
    items: List[T]


class Message(BaseModel):
    """统一消息响应。

    Attributes:
        message: 提示信息文本。
    """

    message: str
