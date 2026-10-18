"""ORM 模型汇总导出模块（文件级说明）。

集中导入全部 31 张业务表的模型类，保证 `Base.metadata.create_all()`
能发现并创建所有表；业务代码统一从本包导入模型。
"""

from app.models.user import OperationLog, Order, User, Wallet, WalletTransaction
from app.models.project import Episode, Project, VersionSnapshot
from app.models.script import Script
from app.models.asset import AssetVariant, Character, Prop, Scene, Style
from app.models.storyboard import Storyboard, StoryboardCharacter, StoryboardProp
from app.models.canvas import Canvas, CanvasEdge, CanvasNode, CanvasPlugin
from app.models.task import Task
from app.models.media import FilmOutput, MediaAsset
from app.models.model_config import LogicalModel, ModelChannel
from app.models.prompt import Announcement, Prompt
from app.models.review import ReviewRecord
from app.models.agent import AgentThread, AssetEmbedding, LanggraphStore
from app.models.notification import Notification

__all__ = [
    # 用户与账务域
    "User",
    "Wallet",
    "WalletTransaction",
    "Order",
    "OperationLog",
    # 项目域
    "Project",
    "Episode",
    "VersionSnapshot",
    # 剧本域
    "Script",
    # 资产域
    "Character",
    "Scene",
    "Prop",
    "Style",
    "AssetVariant",
    # 分镜域
    "Storyboard",
    "StoryboardCharacter",
    "StoryboardProp",
    # 画布域
    "Canvas",
    "CanvasNode",
    "CanvasEdge",
    "CanvasPlugin",
    # 生成任务域
    "Task",
    # 媒体域
    "MediaAsset",
    "FilmOutput",
    # 模型配置域
    "ModelChannel",
    "LogicalModel",
    # 内容运营域
    "Prompt",
    "Announcement",
    # 审核域
    "ReviewRecord",
    # 智能体域
    "AgentThread",
    "LanggraphStore",
    "AssetEmbedding",
    # 通知中心域
    "Notification",
]
