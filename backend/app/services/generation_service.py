"""生成任务服务模块（文件级说明）。

承载生成类任务（关键帧 image / 视频 video / 配音 tts / 合成 compose）的
创建编排逻辑：归属校验、幂等防重、积分冻结（预扣）与任务落库。
路由层（app/api/generation.py、app/api/publish.py）只做参数编排，
事务细节统一收敛在本模块。

计费约定（RD-28 冻结-扣费-退款闭环）：
- 关键帧 10 积分/张；视频 单价×时长（logical_models.price_per_unit.per_second，
  缺省 20/秒）；配音 ceil(字数/100)×5 积分；成片合成 200 积分/次。
"""

import math
import re
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    AssetVariant,
    Character,
    LogicalModel,
    Project,
    Prop,
    Scene,
    Storyboard,
    StoryboardCharacter,
    StoryboardProp,
    Task,
    User,
    Wallet,
)
from app.services.billing_service import freeze_points
from app.services.voice_router import resolve_speech_params, resolve_voice

# 关键帧单价（积分/张）
IMAGE_COST_PER_FRAME = 10
# 成片合成单价（积分/次）
COMPOSE_COST = 200
# 视频单价缺省值（积分/秒，logical_models 未配置时兜底）
DEFAULT_VIDEO_PER_SECOND = 20
# 配音计费常量：每 100 字 5 积分
TTS_COST_PER_100_CHARS = 5

# 受限用户（account_status=restricted）每日生成任务数上限（C-04 分级处罚限流）
RESTRICTED_DAILY_TASK_LIMIT = 10

# 允许的任务创建状态集合（幂等防重检查用）
_ACTIVE_STATUS = ("queued", "running")


async def ensure_generation_allowed(db: AsyncSession, user_id: int) -> None:
    """生成接口统一准入校验（C-04 分级处罚）。

    - banned（禁用）：一律 403 拒绝；
    - restricted（受限）：当日（自然日）已创建任务数达到上限时 429 限流；
    - active：直接放行。

    开关说明：全局配置 punishment_enabled（默认 False）关闭时直接放行——
    产品上线前取消生成限制；违规处罚的累计与通知（apply_violation_penalty）
    不受此开关影响，照常生效，上线后置 True 即恢复拦截。

    Args:
        db: 异步数据库会话。
        user_id: 提交用户 ID（用户不存在时静默放行，交由归属校验兜底）。

    Returns:
        None

    Raises:
        HTTPException: 403 —— 账号已被禁用；429 —— 受限用户当日生成次数已达上限。
    """
    # 上线前取消生成限制：处罚开关关闭时跳过准入校验（violation 累计照常）
    if not settings.punishment_enabled:
        return
    user = await db.get(User, user_id)
    if user is None:
        return
    if user.account_status == "banned":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用，如有疑问请联系运营方申诉")
    if user.account_status == "restricted":
        cnt = await db.scalar(
            select(func.count())
            .select_from(Task)
            .join(Project, Project.id == Task.project_id)
            .where(
                Project.user_id == user_id,
                # 当日起点（数据库侧自然日截断，避免应用/数据库时区偏差）
                Task.created_at >= func.date_trunc("day", func.now()),
            )
        )
        if int(cnt or 0) >= RESTRICTED_DAILY_TASK_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"今日生成次数已达上限（{RESTRICTED_DAILY_TASK_LIMIT} 次），请明日再试或联系运营方",
            )


async def get_owned_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    """校验项目归属并返回项目对象。

    Args:
        db: 异步数据库会话。
        user_id: 当前用户 ID。
        project_id: 项目 ID。

    Returns:
        Project: 归属当前用户的项目 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目不存在；403 —— 项目不属于当前用户。
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    if project.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该项目")
    return project


async def get_storyboard_of_project(
    db: AsyncSession, project_id: int, storyboard_id: int
) -> Storyboard:
    """校验分镜存在且属于指定项目。

    Args:
        db: 异步数据库会话。
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。

    Returns:
        Storyboard: 分镜 ORM 对象。

    Raises:
        HTTPException: 404 —— 分镜不存在或不属于该项目。
    """
    sb = await db.get(Storyboard, storyboard_id)
    if sb is None or sb.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分镜不存在")
    return sb


# 单镜关键帧参考图上限（角色 + 场景 + 道具合计）。qwen-image-3.0 图生图
# 模式硬限制 0~3 张图片输入（0=T2I，1~3=I2I，2026-10-18 实测 400 报错），
# 故上限取 3：排序即重要性（角色 → 场景 → 道具），超出截断
_KEYFRAME_REF_LIMIT = 3

# 关键帧一致性生成指令（动态追加进正向提示词，不写入 prompt_locked 快照，
# 存量分镜无需重新拆解即生效；置于锁定层/可编辑层/风格锚点之后）
CONSISTENCY_DIRECTIVE = (
    "生成要求：严格按照上述画面描述出图；提供的参考图为该镜绑定角色的定妆照/造型变体图、"
    "场景概念图与道具标准图，角色外貌与服装、场景环境、道具样式必须与参考图严格保持一致，"
    "禁止改动参考图中的形象设定；不得新增画面描述与参考图之外的角色、场景元素与物体"
)

# 视频生成专项指令（2026-10-20 用户反馈视频不按分镜内容生成：
# 旧视频提示词仅拼锁定层快照+可编辑层+风格，未直接携带分镜画面描述；
# 新构造以 visual_description 为核心并追加本指令约束内容一致与对白说话表现；
# 同日追加音频约束——wan 系模型会按对白自行合成角色语音与平台 TTS 配音冲突，
# 精细控制为「禁人声、允许环境音」（用户确认：保留人声外的声音接入配音）；
# 2026-10-21 用户反馈「视频里只有嘴动、角色像关键帧一样静止」：旧指令全是
# 「与首帧严格一致/不得新增元素」类压制性措辞，i2v 模型被压得只输出微动画面，
# 重写为「动作驱动型」——一致性仅限定在角色身份/服装/场景布局/画风（防跑偏），
# 明确要求角色肢体动作、环境动态与镜头运动，首帧是动作起点而非静止画框
VIDEO_DIRECTIVE = (
    "生成要求：以上述首帧参考图为动作起点，从该画面自然延续演出，"
    "画面必须是动态的：角色按照分镜画面描述做出相应的肢体动作与表情变化"
    "（如走动、转身、抬头、抬手、俯身、推门等具体动作，动作幅度自然连贯），"
    "有对白时说话角色口部自然开合、表情与台词情绪呼应，"
    "禁止生成角色静止站立只有嘴部微动的画面；"
    "场景环境同步呈现自然动态（风吹动发丝衣物树叶、光影流动、"
    "雨雪烟雾水面波动等按画面场景合理呈现）；"
    "镜头按机位角度做平稳自然的推拉摇移运动，避免完全固定机位；"
    "一致性约束仅限：角色身份与外貌服装、场景布局与道具样式、画风必须与"
    "首帧保持一致，不得新增画面描述与首帧之外的角色与物体，但人物动作、"
    "环境动态与镜头运动不受首帧限制；"
    "音频要求：严禁生成任何人物说话的人声、语音与对白，"
    "仅可保留与画面环境匹配的自然环境音效（如雨声、风声、脚步声）"
)


async def _resolve_keyframe_references(
    db: AsyncSession, storyboard_ids: List[int]
) -> Dict[int, List[str]]:
    """解析分镜关键帧的多张参考图（角色 + 场景 + 道具，A-04 扩展）。

    关键帧一致性锚点：从资产管理中收集该镜绑定资产的已生成图片，按
    「角色（绑定顺序，变体图优先）→ 场景概念图 → 道具标准图」排序透传
    生图渠道（排序即重要性，越靠前权重越高）。无绑定资产的分镜返回
    空列表（回退纯文本提示词生成，与存量行为一致）。

    Args:
        db: 异步数据库会话。
        storyboard_ids: 分镜 ID 列表（批量查询防 N+1）。

    Returns:
        Dict[int, List[str]]: 分镜 ID → 参考图 URL 列表（无可用图时空列表）。

    Raises:
        无：数据库查询异常向上抛出（由调用方事务回滚）。
    """
    if not storyboard_ids:
        return {}
    # 一次联查：分镜 → 场景图 + 角色（定妆照/变体图），按绑定顺序展开
    rows = (
        await db.execute(
            select(
                Storyboard.id.label("sb_id"),
                Scene.asset_url.label("scene_url"),
                StoryboardCharacter.id.label("link_id"),
                StoryboardCharacter.variant_id,
                Character.asset_url.label("char_url"),
                AssetVariant.asset_url.label("variant_url"),
            )
            .select_from(Storyboard)
            .outerjoin(Scene, Scene.id == Storyboard.scene_id)
            .outerjoin(StoryboardCharacter, StoryboardCharacter.storyboard_id == Storyboard.id)
            .outerjoin(Character, Character.id == StoryboardCharacter.character_id)
            .outerjoin(AssetVariant, AssetVariant.id == StoryboardCharacter.variant_id)
            .where(Storyboard.id.in_(storyboard_ids))
            .order_by(Storyboard.id.asc(), StoryboardCharacter.id.asc())
        )
    ).all()
    # 道具标准图单独联查（storyboard_props 与角色关联不同表）
    prop_rows = (
        await db.execute(
            select(StoryboardProp.storyboard_id, Prop.asset_url)
            .join(Prop, Prop.id == StoryboardProp.prop_id)
            .where(StoryboardProp.storyboard_id.in_(storyboard_ids))
            .order_by(StoryboardProp.storyboard_id.asc(), StoryboardProp.id.asc())
        )
    ).all()

    refs: Dict[int, List[str]] = {}
    scene_map: Dict[int, str] = {}
    for sb_id, scene_url, _link_id, variant_id, char_url, variant_url in rows:
        bucket = refs.setdefault(sb_id, [])
        if scene_url:
            scene_map.setdefault(sb_id, scene_url)
        # 角色图：绑定记录指向变体时优先变体图，回退主定妆照
        char_ref = (variant_url if variant_id is not None else None) or char_url
        if char_ref and char_ref not in bucket:
            bucket.append(char_ref)
    # 场景概念图插在角色之后（权重顺序：角色 → 场景 → 道具）
    for sb_id, scene_url in scene_map.items():
        bucket = refs.setdefault(sb_id, [])
        if scene_url not in bucket:
            bucket.append(scene_url)
    # 道具标准图追加在末尾
    for sb_id, prop_url in prop_rows:
        if prop_url and prop_url not in refs.setdefault(sb_id, []):
            refs[sb_id].append(prop_url)
    # 截断上限：排序即重要性（角色 → 场景 → 道具），超出部分丢弃
    return {sb_id: bucket[:_KEYFRAME_REF_LIMIT] for sb_id, bucket in refs.items()}


def build_prompt(*parts: Optional[str]) -> str:
    """拼接提示词各层（RD-04 分层：锁定层 + 可编辑层 + 风格锚点）。

    Args:
        *parts: 提示词片段（忽略空值），顺序为 prompt_locked → prompt_editable → 项目 style。

    Returns:
        str: 以单空格连接的非空片段；全空时返回空串。

    Raises:
        无：纯字符串处理，不产生异常路径。
    """
    return " ".join(p.strip() for p in parts if p and p.strip())


def split_prompt_negative(prompt_locked: Optional[str]) -> Tuple[str, Optional[str]]:
    """从锁定层提示词中剥离「负面排除：」段（方案 B 负面提示词分层）。

    负面提示词不能拼进正向生图提示词（模型会画出被排除的元素），
    生成任务创建时须单独剥离至 meta.negative_prompt，由执行器按渠道
    能力决定透传或忽略。锁定层中「负面排除：」段总在末尾
    （_build_prompt_locked 把扩展要素追加在最后），按前缀截断即可。

    Args:
        prompt_locked: 锁定层提示词（可能含「负面排除：」段，旧数据无）。

    Returns:
        Tuple[str, Optional[str]]: (剥离后的正向锁定层, 负面提示词或 None)。

    Raises:
        无：纯字符串处理，不产生异常路径。
    """
    text = (prompt_locked or "").strip()
    marker = "负面排除："
    idx = text.find(marker)
    if idx < 0:
        return text, None
    positive = text[:idx].strip().rstrip("；;")
    negative = text[idx + len(marker):].strip()
    return positive, (negative or None)


async def get_video_price(db: AsyncSession) -> int:
    """读取视频逻辑模型的每秒单价（元数据来自 logical_models.price_per_unit）。

    Args:
        db: 异步数据库会话。

    Returns:
        int: per_second 单价（整数积分/秒）；未配置模型或字段缺失时取缺省 20。

    Raises:
        无：查不到模型时静默降级为缺省值。
    """
    lm = (
        await db.execute(
            select(LogicalModel).where(
                LogicalModel.capability == "video", LogicalModel.is_default.is_(True)
            )
        )
    ).scalars().first()
    if lm is None:
        lm = (
            await db.execute(select(LogicalModel).where(LogicalModel.capability == "video"))
        ).scalars().first()
    per_second = (lm.price_per_unit or {}).get("per_second", DEFAULT_VIDEO_PER_SECOND) if lm else DEFAULT_VIDEO_PER_SECOND
    try:
        return max(1, int(per_second))
    except (TypeError, ValueError):
        return DEFAULT_VIDEO_PER_SECOND


async def _precheck_balance(db: AsyncSession, user_id: int, total_cost: int) -> None:
    """积分余额预检（避免逐任务冻结中途余额不足留下半提交状态）。

    Args:
        db: 异步数据库会话。
        user_id: 用户 ID。
        total_cost: 本次要冻结的积分总额。

    Raises:
        HTTPException: 409 —— 可用积分余额不足。
    """
    balance = await db.scalar(select(Wallet.points_balance).where(Wallet.user_id == user_id))
    if balance is None or balance < total_cost:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="积分余额不足，无法冻结")


async def freeze_and_create_task(
    db: AsyncSession,
    *,
    project: Project,
    task_type: str,
    phase: str,
    cost_points: int,
    storyboard_id: Optional[int] = None,
    meta: Optional[dict] = None,
    idempotency_key: str,
) -> Task:
    """冻结积分并创建生成任务（同一事务提交，RD-28 预扣）。

    任务创建统一准入口：先执行 C-04 分级处罚准入校验（banned 拒绝 /
    restricted 日限流），再冻结积分落任务。关键帧/视频/配音/合成/重试等
    全部任务创建路径均经由此函数，校验逻辑单点收敛。

    Args:
        db: 异步数据库会话（freeze_points 内部提交事务，任务随同落库）。
        project: 归属项目（取 user_id 与 project_id）。
        task_type: 任务类型 image/video/tts/compose。
        phase: 阶段 trial/full/incremental。
        cost_points: 冻结积分数量。
        storyboard_id: 关联分镜 ID（可空）。
        meta: 任务附加信息（prompt/时长/目标资产等，JSONB）。
        idempotency_key: 冻结流水幂等键。

    Returns:
        Task: 已落库（含自增 ID）的新任务对象。

    Raises:
        HTTPException: 403 —— 账号已被禁用；429 —— 受限用户当日次数达上限；
            409 —— 积分余额不足；400 —— 参数非法（透传自 billing_service）。
    """
    # C-04 分级处罚准入校验（banned 403 / restricted 日限流 429）
    await ensure_generation_allowed(db, project.user_id)
    task = Task(
        project_id=project.id,
        storyboard_id=storyboard_id,
        type=task_type,
        phase=phase,
        status="queued",
        cost_points=cost_points,
        idempotency_key=idempotency_key,
        meta=meta or {},
    )
    db.add(task)
    await db.flush()  # 先取自增主键，供冻结流水 ref_task_id 引用
    await freeze_points(
        db,
        user_id=project.user_id,
        amount=cost_points,
        ref_task_id=task.id,
        idempotency_key=idempotency_key,
    )
    return task


async def _ensure_no_active_image_task(
    db: AsyncSession, storyboard_ids: List[int], phase: str
) -> None:
    """幂等防重：任一涉及分镜已存在排队/进行中的同阶段关键帧任务则拒绝。

    Args:
        db: 异步数据库会话。
        storyboard_ids: 涉及的分镜 ID 列表。
        phase: 阶段 trial/full。

    Raises:
        HTTPException: 409 —— 存在进行中的同阶段任务（防重复提交）。
    """
    if not storyboard_ids:
        return
    cnt = await db.scalar(
        select(func.count()).select_from(Task).where(
            Task.type == "image",
            Task.phase == phase,
            Task.status.in_(_ACTIVE_STATUS),
            Task.storyboard_id.in_(storyboard_ids),
        )
    )
    if cnt:
        # 防重复提交：同分镜同阶段已有排队/进行中的任务
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该分镜已有进行中的同阶段生成任务，请等待完成或先取消",
        )


async def create_keyframe_tasks(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
    phase: str,
    prompt_editable: Optional[str],
    scope_episode_id: Optional[int] = None,
) -> Tuple[List[Task], int]:
    """创建关键帧生成任务（打样 trial 单分镜 / 全量 full 全部分镜）。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 请求指定的分镜（trial 的目标；full 时用于定位用户编辑层）。
        phase: "trial" 或 "full"。
        prompt_editable: 用户本次提交的可编辑提示词（仅 trial 生效）。
        scope_episode_id: 按集工作流（S-06）：full 全量时限定该剧集的分镜
            （None=项目全部分镜；trial 单分镜不受影响）。

    Returns:
        Tuple[List[Task], int]: （新建任务列表，冻结积分总额）。

    Raises:
        HTTPException: 400 —— 项目未确认分镜（状态非 producing，生成阶段门禁）/
            范围无分镜；409 —— 防重复提交 / 积分余额不足。
    """
    # 生成阶段门禁（与前端「确认分镜，进入生成」对应）：未确认不允许发起关键帧生成
    if project.status != "producing":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先在分镜设计页点击「确认分镜，进入生成」后再发起关键帧生成",
        )
    if phase == "trial":
        targets: List[Storyboard] = [storyboard]
    else:
        full_conds = [Storyboard.project_id == project.id]
        if scope_episode_id is not None:
            full_conds.append(Storyboard.episode_id == scope_episode_id)
        rows = await db.execute(
            select(Storyboard)
            .where(*full_conds)
            .order_by(Storyboard.sequence)
        )
        targets = list(rows.scalars().all())
        if not targets:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该范围暂无分镜，无法全量生成")

    await _ensure_no_active_image_task(db, [sb.id for sb in targets], phase)

    # A-04 扩展：批量解析各镜绑定角色参考图（定妆照/变体图）供生图渠道锚定
    ref_map = await _resolve_keyframe_references(db, [sb.id for sb in targets])

    total_cost = len(targets) * IMAGE_COST_PER_FRAME
    await _precheck_balance(db, project.user_id, total_cost)

    tasks: List[Task] = []
    for sb in targets:
        # 提示词分层拼接：锁定层 + 可编辑层（trial 用请求值，full 用分镜自身字段）+ 风格锚点
        # + 一致性生成指令（动态追加，存量分镜即时生效）；
        # 方案 B：负面排除段从锁定层剥离，单独入 meta 供执行器透传
        editable = prompt_editable if phase == "trial" else sb.prompt_editable
        locked_positive, locked_negative = split_prompt_negative(sb.prompt_locked)
        prompt = build_prompt(locked_positive, editable, project.style, CONSISTENCY_DIRECTIVE)
        idem = f"keyframe_{sb.id}_{phase}_{uuid4().hex[:8]}"
        task = await freeze_and_create_task(
            db,
            project=project,
            task_type="image",
            phase=phase,
            cost_points=IMAGE_COST_PER_FRAME,
            storyboard_id=sb.id,
            meta={
                "prompt": prompt,
                "negative_prompt": locked_negative,
                # A-04 扩展：绑定资产参考图组（角色[变体优先]→场景→道具，无图为空列表）
                "reference_image_urls": ref_map.get(sb.id) or [],
                # 输出画幅跟随项目配置（9:16/16:9/1:1，渠道映射输出尺寸）
                "aspect_ratio": project.aspect_ratio,
                "target_type": "storyboard_keyframe",
                "target_id": sb.id,
            },
            idempotency_key=idem,
        )
        tasks.append(task)
    return tasks, total_cost


async def get_storyboard_audio_duration(
    db: AsyncSession, storyboard_id: int
) -> Optional[float]:
    """查询分镜已完成配音的实际时长（音频先行：视频时长跟随配音）。

    取该镜最近一条已完成 TTS 任务的 meta.duration_sec（TTS 执行器回写，
    RD-31 口径）；无配音或字段缺失返回 None（调用方回退分镜估算时长）。

    Args:
        db: 异步数据库会话。
        storyboard_id: 分镜 ID。

    Returns:
        Optional[float]: 配音实际时长（秒）；无配音时 None。

    Raises:
        无：数据库查询异常向上抛出（由调用方事务回滚）。
    """
    row = (
        await db.execute(
            select(Task.meta)
            .where(
                Task.storyboard_id == storyboard_id,
                Task.type == "tts",
                Task.status == "done",
            )
            .order_by(Task.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        dur = float((row or {}).get("duration_sec") or 0)
    except (TypeError, ValueError):
        return None
    return dur if dur > 0 else None


async def create_video_task(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
    use_end_frame: bool,
    resolution: str = "720p",
    fps: int = 24,
    motion_strength: str = "medium",
) -> Task:
    """创建视频片段生成任务（成本 = 每秒单价 × 分镜时长，P-12 视频参数）。

    音频先行（2026-10-19 用户确认流程）：分镜已有配音时视频时长取配音实际
    时长向上取整（wan 端仍钳 2~15s，配音超出部分由合成阶段末帧定格补齐），
    保证口型/节奏与对白对齐；无配音回退分镜估算时长。

    use_end_frame=True 时校验分镜尾帧已生成（storyboards.end_frame_url 非空，
    为空 400 提示先生成尾帧）；三参数写入 meta.video_params，首尾帧地址写入
    meta.first_frame / meta.end_frame 供执行器透传给视频渠道。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 目标分镜。
        use_end_frame: 是否使用尾帧控制（须尾帧已生成）。
        resolution: 分辨率档位 480p/720p/1080p（缺省 720p）。
        fps: 帧率 24/30（缺省 24）。
        motion_strength: 运动幅度 low/medium/high（缺省 medium）。

    Returns:
        Task: 已落库的新视频任务。

    Raises:
        HTTPException: 400 —— 使用尾帧但该分镜尾帧尚未生成；409 —— 积分余额不足。
    """
    if use_end_frame and not storyboard.end_frame_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该分镜尚未生成尾帧，请先生成尾帧再开启尾帧控制",
        )
    # 音频先行：配音时长优先，回退分镜估算时长（计费与生成时长同口径）
    audio_duration = await get_storyboard_audio_duration(db, storyboard.id)
    duration = max(1, math.ceil(audio_duration)) if audio_duration else (storyboard.duration or 5)
    per_second = await get_video_price(db)
    cost = max(1, int(per_second * duration))
    # 方案 B：负面排除段从锁定层剥离（视频正向提示词同样不混入负面）
    locked_positive, locked_negative = split_prompt_negative(storyboard.prompt_locked)
    # 剥离拆解快照中的「固定」运镜残留（2026-10-21 用户反馈视频画面静止）：
    # 拆解八要素写入「景别运镜：全景/固定」等字样，i2v 模型读到「固定」会
    # 倾向静止机位/微动画面，与 VIDEO_DIRECTIVE 动作驱动指令直接矛盾——
    # 仅视频路径替换（关键帧提示词不受影响）
    locked_positive = locked_positive.replace("/固定", "").replace("运镜：固定", "运镜")
    # 视频专项提示词（2026-10-20 强化）：以分镜画面描述为核心（用户在分镜页
    # 编辑后即时生效，不依赖拆解快照），叠加机位角度/环境光影/情绪要素与
    # 对白说话提示（视频 agent 据此表现角色说话时刻）；锁定层八要素快照与
    # 可编辑层、风格锚点作为补充，末尾追加视频一致性指令
    sb_audio = storyboard.audio or {}
    video_parts: List[str] = []
    if (storyboard.visual_description or "").strip():
        video_parts.append(f"分镜画面描述：{storyboard.visual_description.strip()}")
    if storyboard.camera_angle:
        video_parts.append(f"机位角度：{storyboard.camera_angle}")
    if (storyboard.lighting or "").strip():
        video_parts.append(f"环境光影：{storyboard.lighting}")
    emotion = str(sb_audio.get("emotion") or "").strip()
    if emotion:
        video_parts.append(f"镜头情绪：{emotion}")
    if (storyboard.dialogue or "").strip():
        # 说话提示不只口型（2026-10-21）：要求口型+表情+肢体反应联动，
        # 配合 VIDEO_DIRECTIVE 动作驱动，避免「只有嘴动」的静止演出
        video_parts.append(
            f"角色对白（说话角色口部自然开合，并配合台词情绪做出表情与肢体反应）：{storyboard.dialogue.strip()}"
        )
    prompt = build_prompt(
        *video_parts, locked_positive, storyboard.prompt_editable, project.style, VIDEO_DIRECTIVE,
    )
    await _precheck_balance(db, project.user_id, cost)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="video",
        phase="full",
        cost_points=cost,
        storyboard_id=storyboard.id,
        meta={
            "prompt": prompt,
            "negative_prompt": locked_negative,
            "duration": duration,
            # 音频先行溯源：时长来源（audio=配音对齐 / storyboard=估算回退）
            "duration_source": "audio" if audio_duration else "storyboard",
            "storyboard_id": storyboard.id,
            "use_end_frame": use_end_frame,
            # P-12 视频参数（执行器透传给 provider 请求体）
            "video_params": {
                "resolution": resolution,
                "fps": fps,
                "motion_strength": motion_strength,
            },
            # P-03/P-08 首尾帧地址（use_end_frame=False 时 end_frame 记录 None）
            "first_frame": storyboard.keyframe_url,
            "end_frame": storyboard.end_frame_url if use_end_frame else None,
        },
        idempotency_key=f"video_{storyboard.id}_{uuid4().hex[:8]}",
    )


async def create_end_frame_task(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
) -> Task:
    """创建分镜尾帧图像生成任务（P-03/P-08：成本 10 积分/张）。

    复用关键帧图像任务创建路径（type=image），meta 标记 end_frame=True 与
    target_type=storyboard_end_frame，执行器成功后回写 storyboards.end_frame_url。
    幂等防重：同分镜已存在排队/进行中的尾帧任务时 409。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 目标分镜（须已有关键帧，由路由层校验）。

    Returns:
        Task: 已落库的新尾帧图像任务。

    Raises:
        HTTPException: 409 —— 同分镜已有进行中的尾帧任务 / 积分余额不足。
    """
    # 尾帧任务独立防重：仅检查 meta 带 end_frame 标记的图像任务（与关键帧互不阻塞）
    cnt = await db.scalar(
        select(func.count()).select_from(Task).where(
            Task.type == "image",
            Task.status.in_(_ACTIVE_STATUS),
            Task.storyboard_id == storyboard.id,
            Task.meta["end_frame"].as_boolean().is_(True),
        )
    )
    if cnt:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该分镜已有进行中的尾帧生成任务，请等待完成或先取消",
        )
    total_cost = IMAGE_COST_PER_FRAME
    await _precheck_balance(db, project.user_id, total_cost)
    # 方案 B：负面排除段从锁定层剥离，单独入 meta
    locked_positive, locked_negative = split_prompt_negative(storyboard.prompt_locked)
    prompt = build_prompt(locked_positive, storyboard.prompt_editable, project.style, CONSISTENCY_DIRECTIVE)
    # A-04 扩展：尾帧与关键帧共用同镜绑定资产参考图（形象一致）
    ref_map = await _resolve_keyframe_references(db, [storyboard.id])
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="image",
        phase="full",
        cost_points=IMAGE_COST_PER_FRAME,
        storyboard_id=storyboard.id,
        meta={
            "prompt": prompt,
            "negative_prompt": locked_negative,
            # A-04 扩展：绑定资产参考图组（角色[变体优先]→场景→道具）
            "reference_image_urls": ref_map.get(storyboard.id) or [],
            # 输出画幅跟随项目配置
            "aspect_ratio": project.aspect_ratio,
            "target_type": "storyboard_end_frame",
            "target_id": storyboard.id,
            "storyboard_id": storyboard.id,
            "end_frame": True,
        },
        idempotency_key=f"endframe_{storyboard.id}_{uuid4().hex[:8]}",
    )


def _strip_dialogue_speakers(dialogue: Optional[str]) -> str:
    """剥离对白说话人前缀，拼接纯台词文本（内部函数，配音合成文本）。

    「XXX：台词」逐行取冒号后台词，多行以空格连接——角色名不进语音
    （TTS 不把说话人名念出来）；无法解析前缀的行原样保留。

    Args:
        dialogue: 分镜对白文本（多行「说话人：台词」格式，可空）。

    Returns:
        str: 纯台词文本（可能为空，调用方回退画面描述）。

    Raises:
        无：纯文本解析，不产生异常路径。
    """
    lines: List[str] = []
    for line in str(dialogue or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(?:[^：:]{1,20})[：:](.+)$", line)
        lines.append((m.group(1) if m else line).strip())
    return " ".join(x for x in lines if x)


# 情绪标签 → 语音表现乘数（语速乘数, 音调乘数）：叠加在 voice_note 基调上
_EMOTION_MODIFIERS: Dict[str, Tuple[float, float]] = {
    "压抑": (0.92, 0.92), "低落": (0.9, 0.9), "悲伤": (0.85, 0.9), "沮丧": (0.9, 0.9),
    "紧张": (1.1, 1.02), "急促": (1.15, 1.05),
    "爽感": (1.1, 1.05), "激动": (1.12, 1.08), "爆发": (1.15, 1.1), "愤怒": (1.15, 1.1),
    "温馨": (0.95, 1.02), "释然": (0.95, 1.0),
    "恐惧": (1.05, 0.95), "惊讶": (1.1, 1.1),
}


def _apply_emotion(speed: float, pitch: float, emotion: str) -> Tuple[float, float]:
    """按分镜情绪标签微调语速/音调（内部函数，情绪表现力叠加）。

    在 voice_note 推导基调上乘以情绪乘数（乘法叠加），倍率钳制 0.5~2.0；
    情绪词未命中任何修饰项时原样返回。

    Args:
        speed: 基础语速倍率。
        pitch: 基础音调倍率。
        emotion: 分镜情绪标签（如「压抑」「爽感」）。

    Returns:
        Tuple[float, float]: (speed, pitch)（各钳制 0.5~2.0）。

    Raises:
        无：纯数值计算，不产生异常路径。
    """
    for kw, (sm, pm) in _EMOTION_MODIFIERS.items():
        if kw in emotion:
            return (
                min(2.0, max(0.5, speed * sm)),
                min(2.0, max(0.5, pitch * pm)),
            )
    return (speed, pitch)


async def create_tts_task(
    db: AsyncSession,
    *,
    project: Project,
    storyboard: Storyboard,
    voice: Optional[str],
    speed: float,
    pitch: float,
    segment_index: Optional[int] = None,
) -> Task:
    """创建配音（TTS）生成任务（成本 = ceil(字数/100)×5 积分）。

    多说话人分段配音（2026-10-19）：对白逐行解析为「说话人 + 台词」段，
    每段用该说话人角色的锁定音色（voice_selected/自动路由）与自身音色设定
    推导的语速/音调独立合成，执行器拼接为单条音轨回写——一个角色一段对话
    一行，角色名不进语音也不串音。

    段级配音（2026-10-20）：segment_index 指定时仅合成该下标对白段并独立
    落产物（storyboard.audio.dialogue[idx]），全部段完成后执行器自动拼接
    整镜音轨——一句一行独立生成，便于视频按角色对齐配音。计费按该段字数。

    音色确认门禁：对白说话人对应的绑定角色必须已完成音色锁定
    （voice_locked），未锁定 400 拦截（段级模式仅校验目标段说话人）。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        storyboard: 目标分镜（台词取 dialogue，无对白 400 拦截——画面描述不作台词）。
        voice: 音色标识（无匹配说话人段的旁白回退音色；None=自动路由）。
        speed: 语速倍率（旁白段基调）。
        pitch: 音调倍率（旁白段基调）。
        segment_index: 段级配音下标（None=整镜全段合成拼接）。

    Returns:
        Task: 已落库的新配音任务（meta.segments 携带分段参数）。

    Raises:
        HTTPException: 400 —— 分镜无可配音文本 / 说话人角色音色未锁定 /
            段下标越界；409 —— 积分不足 / 同镜同段配音任务进行中。
    """
    # 音频先行对白处理：剥离「说话人：」前缀只合成台词（角色名不进语音）。
    # 无对白分镜直接 400 拦截（2026-10-20 用户确认：画面描述不得被念成配音——
    # 旧逻辑对白为空时回退 visual_description 作台词，导致空镜出现描述旁白）
    text = _strip_dialogue_speakers(storyboard.dialogue)
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该分镜无对白，不生成配音（画面描述不作为台词）")
    # 绑定角色一次查询（音色门禁与段级音色共用）
    bound_chars: List[Character] = (
        await db.execute(
            select(Character)
            .join(StoryboardCharacter, StoryboardCharacter.character_id == Character.id)
            .where(StoryboardCharacter.storyboard_id == storyboard.id)
            .order_by(StoryboardCharacter.id.asc())
        )
    ).scalars().all()
    bound_by_name = {ch.name: ch for ch in bound_chars}
    # 旁白基调（无匹配说话人的段回退）：显式 voice > 第一个绑定角色锁定/路由
    resolved_voice = (voice or "").strip()
    voice_note_used: Optional[str] = None
    if not resolved_voice and bound_chars:
        first_char = bound_chars[0]
        voice_note_used = first_char.voice_note
        resolved_voice = first_char.voice_selected or resolve_voice(
            first_char.voice_note, first_char.gender)
    if not resolved_voice:
        resolved_voice = "female_01"
    # 旁白段基调参数：请求显式调整（≠1.0）优先，否则按第一个绑定角色推导
    base_speed, base_pitch = speed, pitch
    if base_speed == 1.0 or base_pitch == 1.0:
        derived_speed, derived_pitch = resolve_speech_params(voice_note_used, base_speed, base_pitch)
        if base_speed == 1.0:
            base_speed = derived_speed
        if base_pitch == 1.0:
            base_pitch = derived_pitch
    # 情绪表现力：分镜 audio.emotion 情绪标签（段级同样叠加）
    emotion = str((storyboard.audio or {}).get("emotion") or "").strip()
    if emotion:
        base_speed, base_pitch = _apply_emotion(base_speed, base_pitch, emotion)
    # 段级构建：每个对白行独立音色/语速/音调（说话人角色锁定音色 +
    # 自身 voice_note 推导 + 情绪叠加）；无匹配说话人行用旁白基调
    segments: List[Dict] = []
    for line in str(storyboard.dialogue or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([^：:]{1,20})[：:](.+)$", line)
        speaker = m.group(1).strip() if m else None
        seg_text = (m.group(2) if m else line).strip()
        if not seg_text:
            continue
        ch = bound_by_name.get(speaker or "") if speaker else None
        if ch is not None:
            seg_voice = ch.voice_selected or resolve_voice(ch.voice_note, ch.gender)
            seg_speed, seg_pitch = resolve_speech_params(ch.voice_note)
        else:
            seg_voice = resolved_voice
            seg_speed, seg_pitch = base_speed, base_pitch
        if emotion:
            seg_speed, seg_pitch = _apply_emotion(seg_speed, seg_pitch, emotion)
        segments.append({
            "speaker": speaker or "",
            "text": seg_text,
            "voice": seg_voice,
            "speed": seg_speed,
            "pitch": seg_pitch,
        })
    if not segments:
        segments = [{"speaker": "", "text": text, "voice": resolved_voice,
                     "speed": base_speed, "pitch": base_pitch}]
    # 段级配音校验：下标越界拦截 + 同镜同段进行中任务防重（409，
    # meta.segment_index JSONB 文本比对；避免并发写同段产物）
    if segment_index is not None:
        if segment_index >= len(segments):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"段下标 {segment_index} 超出该镜对白段数（{len(segments)}），请刷新后重试",
            )
        dup = (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.storyboard_id == storyboard.id,
                    Task.type == "tts",
                    Task.status.in_(_ACTIVE_STATUS),
                    Task.meta["segment_index"].as_string() == str(segment_index),
                )
            )
        ).scalar_one()
        if dup:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该镜此段配音正在生成中，请等待完成后再试",
            )
    # 音色确认门禁：全镜模式校验全部对白说话人；段级模式仅校验目标段说话人
    if segment_index is not None:
        check_speakers = [segments[segment_index]["speaker"]] if segments[segment_index]["speaker"] else []
    else:
        check_speakers = [s["speaker"] for s in segments if s["speaker"]]
    unlocked = [n for n in check_speakers if n in bound_by_name and not bound_by_name[n].voice_locked]
    if unlocked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"角色「{'、'.join(unlocked)}」音色未确认，请先在配音页完成试音并锁定音色后再生成分镜配音",
        )
    # 计费：段级模式按该段字数，整镜模式按全文（ceil(字数/100)×5）
    if segment_index is not None:
        cost = max(1, math.ceil(len(str(segments[segment_index]["text"])) / 100) * TTS_COST_PER_100_CHARS)
    else:
        cost = max(1, math.ceil(len(text) / 100) * TTS_COST_PER_100_CHARS)
    await _precheck_balance(db, project.user_id, cost)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="tts",
        phase="full",
        cost_points=cost,
        storyboard_id=storyboard.id,
        meta={
            # text：全文（内容审核与溯源）；segments：分段合成参数（执行器
            # 逐段合成拼接；segment_index 指定时仅合成该段并独立落产物）
            "text": text,
            "segments": segments,
            "voice": segments[0]["voice"],
            "speed": segments[0]["speed"],
            "pitch": segments[0]["pitch"],
            "storyboard_id": storyboard.id,
            **({"segment_index": segment_index} if segment_index is not None else {}),
            **({"emotion": emotion} if emotion else {}),
            **({"voice_note": voice_note_used} if voice_note_used else {}),
        },
        idempotency_key=f"tts_{storyboard.id}_{uuid4().hex[:8]}",
    )


async def create_voice_preview_task(
    db: AsyncSession,
    *,
    project: Project,
    character: Character,
    voice: Optional[str],
) -> Task:
    """创建角色音色试音任务（「我是XX，这是我的配音。」一句试音）。

    音色：voice 显式传入（前端映射表下拉当前选择）优先，否则按角色
    voice_note + gender 经 voice_router 自动路由。产物由执行器回写
    characters.voice_sample_url（meta.target_type=character_voice 分流），
    不回写分镜（storyboard_id=None）。成本按 TTS 最低档 5 积分。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        character: 试音角色（须归属该项目）。
        voice: 音色标识（None/空=按角色音色设定自动路由）。

    Returns:
        Task: 已落库的新试音任务。

    Raises:
        HTTPException: 409 —— 积分余额不足；403/429 —— 分级处罚准入拦截。
    """
    text = f"我是{character.name}，这是我的配音。"
    resolved_voice = (voice or "").strip() or resolve_voice(
        character.voice_note, character.gender)
    # 语速/音调表现力：从 voice_note 说话习惯/质地推导（试音即可闻差异）
    speed, pitch = resolve_speech_params(character.voice_note)
    cost = max(1, math.ceil(len(text) / 100) * TTS_COST_PER_100_CHARS)
    await _precheck_balance(db, project.user_id, cost)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="tts",
        phase="trial",
        cost_points=cost,
        storyboard_id=None,
        meta={
            "text": text,
            "voice": resolved_voice,
            "speed": speed,
            "pitch": pitch,
            # 执行器按 target 分流回写 characters.voice_sample_url
            "target_type": "character_voice",
            "target_id": character.id,
            "character_id": character.id,
        },
        idempotency_key=f"voice_preview_{character.id}_{uuid4().hex[:8]}",
    )


async def create_compose_task(
    db: AsyncSession,
    *,
    project: Project,
    resolution: str,
    aspect_ratio: str,
    mode: str,
    storyboard_ids: Optional[List[int]] = None,
    episode_id: Optional[int] = None,
    subtitles: bool = False,
    subtitle_style: Optional[dict] = None,
    bgm: Optional[dict] = None,
    transition: Optional[dict] = None,
    intro_card: bool = False,
    outro_card: bool = False,
) -> Task:
    """创建成片合成任务（成本 200 积分/次）。

    完整性门禁（2026-10-21 用户确认参考即梦/小云雀类平台）：合成前校验
    范围内分镜视频产物齐备，缺镜返回 400（detail.code=videos_incomplete，
    携带缺镜 ID/序号清单）——保证一集成片不静默缺镜头。

    Args:
        db: 异步数据库会话。
        project: 归属项目。
        resolution: 分辨率 720P/1080P/2K。
        aspect_ratio: 画幅比例 9:16/16:9/1:1（缺省沿用项目配置）。
        mode: 合成模式 incremental/full（映射为任务 phase）。
        storyboard_ids: 增量合成指定分镜 ID 列表（Task 5.3；仅 incremental 且
            非空时生效，空/None 回退项目全部分镜）。
        episode_id: 可选剧集 ID（S-06；传入时全量模式仅取该剧集分镜并写入
            meta，合成产物 film_outputs.episode_id 绑定该剧集）。
        subtitles: 是否烧录字幕（Task 5.1）。
        subtitle_style: 字幕样式 {font_size, color, position}（可空用默认）。
        bgm: 背景音乐 {url, volume}（Task 5.2，可空表示不加）。
        transition: 转场 {type, duration}（Task 5.3，可空表示不加）。
        intro_card: 是否插入片头标题卡（剧名+集数，2.5s 黑底白字）。
        outro_card: 是否插入剧终片尾卡（2s 黑底白字）。

    Returns:
        Task: 已落库的新合成任务（meta 携带合成增强参数与涉及分镜清单）。

    Raises:
        HTTPException: 400 —— 项目暂无分镜/增量分镜清单为空/范围内存在未
            生成视频的分镜（videos_incomplete，detail 携带缺镜清单）；
            409 —— 积分余额不足。
    """
    conds = [Storyboard.project_id == project.id]
    if episode_id is not None:
        # S-06：按剧集圈定合成范围（episode_id 归属已由调用方校验）
        conds.append(Storyboard.episode_id == episode_id)
    if mode == "incremental" and storyboard_ids:
        # 增量合成（Task 5.3）：仅拼接指定分镜（调用方已完成归属与顺序校验）
        targets: List[int] = list(storyboard_ids)
    else:
        rows = await db.execute(
            select(Storyboard).where(*conds).order_by(Storyboard.sequence)
        )
        targets = [sb.id for sb in rows.scalars().all()]
    if not targets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目暂无分镜，无法合成")
    # 完整性门禁：范围内分镜视频产物必须齐备（缺镜 400 + 清单，防成片静默缺镜头）
    sb_rows = (
        await db.execute(
            select(Storyboard.id, Storyboard.sequence, Storyboard.video_url)
            .where(Storyboard.id.in_(targets))
            .order_by(Storyboard.sequence)
        )
    ).all()
    missing = [
        {"id": sid, "sequence": seq}
        for sid, seq, video_url in sb_rows if not video_url
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "videos_incomplete",
                "message": f"还有 {len(missing)} 个镜头未生成视频，请补齐后再合成",
                "missing": missing,
                "total": len(sb_rows),
            },
        )
    await _precheck_balance(db, project.user_id, COMPOSE_COST)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type="compose",
        phase=mode,  # incremental/full 直接映射任务 phase（约束允许）
        cost_points=COMPOSE_COST,
        storyboard_id=None,
        meta={
            "resolution": resolution,
            "aspect_ratio": aspect_ratio or project.aspect_ratio,
            "mode": mode,
            "storyboard_ids": targets,
            # S-06：剧集范围（None=全部，compose_service 据此过滤并回写 film_outputs）
            "episode_id": episode_id,
            # Task 5 合成增强参数（None 项由 compose_service 按缺省处理）
            "subtitles": bool(subtitles),
            "subtitle_style": subtitle_style,
            "bgm": bgm,
            "transition": transition,
            # 片头片尾卡（2026-10-21 成片整合：剧名+集数标题卡 / 剧终卡）
            "intro_card": bool(intro_card),
            "outro_card": bool(outro_card),
        },
        idempotency_key=f"compose_{project.id}_{uuid4().hex[:8]}",
    )


async def create_retry_task(db: AsyncSession, task: Task) -> Task:
    """为失败任务创建重试任务（新幂等键，重新冻结积分）。

    Args:
        db: 异步数据库会话。
        task: 原失败任务（须已通过 status=='failed' 校验）。

    Returns:
        Task: 已落库的新重试任务（继承原任务的类型/阶段/meta/积分）。

    Raises:
        HTTPException: 409 —— 积分余额不足。
    """
    project = await db.get(Project, task.project_id)
    await _precheck_balance(db, project.user_id, task.cost_points)
    return await freeze_and_create_task(
        db,
        project=project,
        task_type=task.type,
        phase=task.phase,
        cost_points=task.cost_points,
        storyboard_id=task.storyboard_id,
        meta=task.meta,
        idempotency_key=f"retry_{task.id}_{uuid4().hex[:8]}",
    )


async def render_stale_storyboards(
    db: AsyncSession,
    *,
    project: Project,
    episode_id: Optional[int] = None,
) -> dict:
    """批量重渲染过期镜头（P0-3 版本透传：「仅重渲染受影响镜头」入口）。

    流程：取项目分镜列表 → 过滤 stale=True（引用资产版本快照与当前版本不一致）
    → 跳过已有排队/进行中关键帧任务的镜头 → 逐镜复用关键帧任务构建逻辑
    （freeze_and_create_task，10 积分/帧）入队重渲染。
    复用 /regen 计数端点会造成语义混淆，故此处独立实现且不递增 regen_count。

    Args:
        db: 异步数据库会话。
        project: 已校验归属的项目。
        episode_id: 可选剧集过滤（与分镜列表口径一致；None=全部）。

    Returns:
        dict: {"queued": 成功入队数, "skipped": 跳过数（已在队列）,
              "storyboard_ids": 入队镜头 ID 列表, "total_cost": 冻结积分总额}。

    Raises:
        HTTPException: 409 —— 积分余额不足；403/429 —— 分级处罚准入拦截。
    """
    from app.services.storyboard_service import list_storyboards  # 延迟导入避免循环依赖

    outs = await list_storyboards(db, project, episode_id)
    stale_ids = [o["id"] for o in outs if o.get("stale")]
    if not stale_ids:
        return {"queued": 0, "skipped": 0, "storyboard_ids": [], "total_cost": 0}

    # 幂等防重：已有排队/进行中关键帧任务（trial/full 任一阶段）的镜头跳过
    busy_ids = set(
        (await db.execute(
            select(Task.storyboard_id).where(
                Task.type == "image",
                Task.phase.in_(["trial", "full"]),
                Task.status.in_(_ACTIVE_STATUS),
                Task.storyboard_id.in_(stale_ids),
            )
        )
    ).scalars().all())
    target_ids = [sid for sid in stale_ids if sid not in busy_ids]
    skipped = len(stale_ids) - len(target_ids)
    if not target_ids:
        return {"queued": 0, "skipped": skipped, "storyboard_ids": [], "total_cost": 0}

    # 余额预检（总成本 = 目标镜数 × 单帧价，一次冻结循环前统一校验）
    total_cost = len(target_ids) * IMAGE_COST_PER_FRAME
    await _precheck_balance(db, project.user_id, total_cost)

    # A-04 扩展：批量解析重渲染镜头的绑定角色参考图（定妆照/变体图）
    ref_map = await _resolve_keyframe_references(db, target_ids)

    queued_ids: List[int] = []
    target_set = set(target_ids)
    for o in outs:
        if o["id"] not in target_set:
            continue
        sb = await db.get(Storyboard, o["id"])
        if sb is None:
            continue
        # 方案 B：负面排除段从锁定层剥离，单独入 meta
        locked_positive, locked_negative = split_prompt_negative(sb.prompt_locked)
        prompt = build_prompt(locked_positive, sb.prompt_editable, project.style, CONSISTENCY_DIRECTIVE)
        await freeze_and_create_task(
            db,
            project=project,
            task_type="image",
            phase="full",
            cost_points=IMAGE_COST_PER_FRAME,
            storyboard_id=sb.id,
            meta={
                "prompt": prompt,
                "negative_prompt": locked_negative,
                # A-04 扩展：绑定资产参考图组（角色[变体优先]→场景→道具）
                "reference_image_urls": ref_map.get(sb.id) or [],
                # 输出画幅跟随项目配置
                "aspect_ratio": project.aspect_ratio,
                "target_type": "storyboard_keyframe",
                "target_id": sb.id,
                # P0-3 溯源：标记本次入队为过期重渲染
                "render_reason": "stale",
            },
            idempotency_key=f"keyframe_{sb.id}_full_{uuid4().hex[:8]}",
        )
        queued_ids.append(sb.id)
    return {
        "queued": len(queued_ids),
        "skipped": skipped,
        "storyboard_ids": queued_ids,
        "total_cost": total_cost,
    }
