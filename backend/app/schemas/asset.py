"""资产域 Pydantic Schema 模块（文件级说明）。

定义角色 / 场景 / 道具 / 风格四类资产的 CRUD 出入参、资产变体、
定妆照生成任务与一致性校验的模型。
"""

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# 角色资产允许的变体类型（RD-14）
CHARACTER_VARIANT_TYPES = ("outfit", "age", "other")
# 场景资产允许的变体类型（RD-19 光照变体）
SCENE_VARIANT_TYPES = ("lighting",)
# 道具资产允许的变体类型
PROP_VARIANT_TYPES = ("other",)


class CharacterOut(BaseModel):
    """角色资产输出模型。

    Attributes:
        id: 角色 ID。
        project_id: 项目 ID。
        name: 角色名。
        aliases: 别名集合（JSONB）。
        description: 角色描述。
        visual_prompt: 标准化视觉提示词。
        asset_url: 主定妆照 URL。
        face_locked: 面部层锁定。
        generation_status: 生成状态 pending/generating/done/failed。
        created_at / updated_at: 时间戳。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    aliases: Optional[Any] = None
    description: Optional[str] = None
    visual_prompt: str
    asset_url: Optional[str] = None
    face_locked: bool = False
    generation_status: str = "pending"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SceneOut(BaseModel):
    """场景资产输出模型。

    Attributes:
        id: 场景 ID。
        project_id: 项目 ID。
        name: 场景名。
        description: 场景描述。
        visual_prompt: 视觉提示词。
        asset_url: 概念图 URL。
        is_locked: 锁定状态。
        created_at / updated_at: 时间戳。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    visual_prompt: str
    asset_url: Optional[str] = None
    is_locked: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PropOut(BaseModel):
    """道具资产输出模型。

    Attributes:
        id: 道具 ID。
        project_id: 项目 ID。
        name: 道具名。
        category: 分类。
        description: 描述。
        visual_prompt: 视觉提示词。
        asset_url: 标准图 URL。
        is_locked: 锁定状态。
        created_at / updated_at: 时间戳。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    visual_prompt: str
    asset_url: Optional[str] = None
    is_locked: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StyleOut(BaseModel):
    """风格资产输出模型。

    Attributes:
        id: 风格 ID。
        project_id: 项目 ID。
        name: 风格名。
        preset_code: 内置预设编码。
        reference_url: 参考图 URL。
        visual_prompt: 风格提示词。
        is_default: 是否项目默认风格。
        created_at / updated_at: 时间戳。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    preset_code: Optional[str] = None
    reference_url: Optional[str] = None
    visual_prompt: str
    is_default: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AssetsGroupOut(BaseModel):
    """四类资产聚合输出（GET /assets 不指定 type 时）。

    Attributes:
        characters: 角色列表。
        scenes: 场景列表。
        props: 道具列表。
        styles: 风格列表。
    """

    characters: List[CharacterOut] = Field(default_factory=list)
    scenes: List[SceneOut] = Field(default_factory=list)
    props: List[PropOut] = Field(default_factory=list)
    styles: List[StyleOut] = Field(default_factory=list)


class CharacterCreateRequest(BaseModel):
    """角色创建请求。

    Attributes:
        name: 角色名（项目内唯一，重复返回 409）。
        description: 角色描述。
        visual_prompt: 视觉提示词（缺省用中文模板生成）。
        aliases: 别名列表。
    """

    name: str = Field(min_length=1, max_length=50)
    description: Optional[str] = None
    visual_prompt: Optional[str] = None
    aliases: Optional[List[str]] = None


class CharacterUpdateRequest(BaseModel):
    """角色更新请求（全部字段可空，None 表示不修改）。

    Attributes:
        name: 角色名。
        description: 角色描述。
        visual_prompt: 视觉提示词。
        aliases: 别名列表。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    description: Optional[str] = None
    visual_prompt: Optional[str] = None
    aliases: Optional[List[str]] = None


class SceneCreateRequest(BaseModel):
    """场景创建请求。

    Attributes:
        name: 场景名（项目内唯一）。
        description: 场景描述。
        visual_prompt: 视觉提示词（缺省用中文模板生成）。
    """

    name: str = Field(min_length=1, max_length=50)
    description: Optional[str] = None
    visual_prompt: Optional[str] = None


class SceneUpdateRequest(BaseModel):
    """场景更新请求（全部字段可空）。

    Attributes:
        name: 场景名。
        description: 场景描述。
        visual_prompt: 视觉提示词。
        is_locked: 锁定状态。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    description: Optional[str] = None
    visual_prompt: Optional[str] = None
    is_locked: Optional[bool] = None


class PropCreateRequest(BaseModel):
    """道具创建请求。

    Attributes:
        name: 道具名。
        category: 分类。
        description: 描述。
        visual_prompt: 视觉提示词（缺省用中文模板生成）。
    """

    name: str = Field(min_length=1, max_length=50)
    category: Optional[str] = Field(default=None, max_length=30)
    description: Optional[str] = None
    visual_prompt: Optional[str] = None


class PropUpdateRequest(BaseModel):
    """道具更新请求（全部字段可空）。

    Attributes:
        name: 道具名。
        category: 分类。
        description: 描述。
        visual_prompt: 视觉提示词。
        is_locked: 锁定状态。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    category: Optional[str] = Field(default=None, max_length=30)
    description: Optional[str] = None
    visual_prompt: Optional[str] = None
    is_locked: Optional[bool] = None


class StyleCreateRequest(BaseModel):
    """风格创建请求。

    Attributes:
        name: 风格名。
        preset_code: 内置预设编码。
        reference_url: 参考图 URL。
        visual_prompt: 风格提示词。
        is_default: 是否设为项目默认风格（会清掉其他默认）。
    """

    name: str = Field(min_length=1, max_length=50)
    preset_code: Optional[str] = Field(default=None, max_length=30)
    reference_url: Optional[str] = Field(default=None, max_length=255)
    visual_prompt: str = Field(min_length=1)
    is_default: bool = False


class StyleUpdateRequest(BaseModel):
    """风格更新请求（全部字段可空）。

    Attributes:
        name: 风格名。
        preset_code: 内置预设编码。
        reference_url: 参考图 URL。
        visual_prompt: 风格提示词。
        is_default: 是否设为项目默认风格（会清掉其他默认）。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    preset_code: Optional[str] = Field(default=None, max_length=30)
    reference_url: Optional[str] = Field(default=None, max_length=255)
    visual_prompt: Optional[str] = None
    is_default: Optional[bool] = None


class CharacterGenerateRequest(BaseModel):
    """角色定妆照生成请求。

    Attributes:
        prompt: 覆盖用提示词（缺省使用角色 visual_prompt）。
    """

    prompt: Optional[str] = None


class VariantCreateRequest(BaseModel):
    """资产变体创建请求。

    Attributes:
        variant_type: 变体类型（角色 outfit/age/other；场景 lighting；道具 other）。
        name: 变体名。
        inherit_mask: 显式继承/独立声明（RD-14，必填，缺失返回 400）。
        visual_prompt: 变体提示词（可空）。
        is_active: 是否启用。
    """

    variant_type: str = Field(description="变体类型，如 outfit/lighting/age/other")
    name: str = Field(min_length=1, max_length=50)
    inherit_mask: Optional[Any] = Field(
        default=None, description="继承/独立声明（RD-14，必填；缺失时服务层返回 400）")
    visual_prompt: Optional[str] = None
    is_active: bool = True


class VariantOut(BaseModel):
    """资产变体输出模型。

    Attributes:
        id: 变体 ID。
        owner_type / owner_id: 多态归属。
        variant_type: 变体类型。
        name: 变体名。
        inherit_mask: 继承/独立声明。
        asset_url: 变体图 URL。
        visual_prompt: 变体提示词。
        is_active: 是否启用。
        created_at / updated_at: 时间戳。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_type: str
    owner_id: int
    variant_type: str
    name: str
    inherit_mask: Optional[Any] = None
    asset_url: Optional[str] = None
    visual_prompt: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ConsistencyCheckRequest(BaseModel):
    """一致性校验请求（占位实现）。

    Attributes:
        character_id: 角色资产 ID。
        variant_id: 可选的变体 ID（用于校验该变体与主形象的一致性）。
    """

    character_id: int
    variant_id: Optional[int] = None


class ConsistencyCheckOut(BaseModel):
    """一致性校验结果。

    Attributes:
        similarity: 伪相似度（0~1，mock 确定性计算）。
        passed: 是否通过（similarity >= 0.85）。
        report: 校验报告文本。
    """

    similarity: float
    passed: bool
    report: str


class CrossProjectAssetOut(BaseModel):
    """跨项目资产总览行（GET /api/characters，characters + scenes 联合）。

    Attributes:
        id: 资产 ID。
        asset_type: 资产类型 character / scene。
        project_id: 所属项目 ID。
        project_title: 所属项目标题。
        name: 资产名。
        description: 描述。
        visual_prompt: 视觉提示词。
        asset_url: 资产图 URL。
        created_at: 创建时间。
    """

    id: int
    asset_type: str
    project_id: int
    project_title: str
    name: str
    description: Optional[str] = None
    visual_prompt: str
    asset_url: Optional[str] = None
    created_at: Optional[datetime] = None
