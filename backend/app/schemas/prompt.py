"""提示词库 Pydantic Schema 模块（文件级说明）。

定义提示词创建 / 更新 / 查询 / 批量导入的出入参模型。
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class PromptCreateRequest(BaseModel):
    """提示词创建请求。

    Attributes:
        title: 标题（1~100 字符）。
        content: 内容（1~20000 字符）。
        capability: 适用能力（如 角色定妆/分镜画面/运镜/配音，可空）。
        category: 分类（可空）。
        tags: 标签（JSONB，可为列表或对象，可空）。
    """

    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=20000)
    capability: Optional[str] = Field(default=None, max_length=20)
    category: Optional[str] = Field(default=None, max_length=30)
    tags: Optional[Any] = None


class PromptUpdateRequest(BaseModel):
    """提示词更新请求（全部字段可空，None 表示不修改）。

    Attributes:
        title: 标题。
        content: 内容。
        capability: 适用能力。
        category: 分类。
        tags: 标签。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    content: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    capability: Optional[str] = Field(default=None, max_length=20)
    category: Optional[str] = Field(default=None, max_length=30)
    tags: Optional[Any] = None


class PromptOut(BaseModel):
    """提示词输出模型。

    Attributes:
        id: 提示词 ID。
        user_id: 归属用户 ID（NULL = 公共提示词）。
        scope: 范围（public / private）。
        capability: 适用能力。
        title: 标题。
        content: 内容。
        category: 分类。
        tags: 标签。
        created_at: 创建时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    scope: str
    capability: Optional[str] = None
    title: str
    content: str
    category: Optional[str] = None
    tags: Optional[Any] = None
    created_at: Optional[datetime] = None


class PromptImportRequest(BaseModel):
    """提示词批量导入请求。

    Attributes:
        prompts: 待导入的提示词列表（全部导入为当前用户私有）。
    """

    prompts: List[PromptCreateRequest] = Field(min_length=1, max_length=200)


class PromptImportOut(BaseModel):
    """提示词批量导入结果。

    Attributes:
        imported: 成功导入的条数。
    """

    imported: int
