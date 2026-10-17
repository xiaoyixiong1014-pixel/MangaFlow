"""分镜路由模块（文件级说明）。

实现分镜域全部接口（每个接口先校验项目归属，他人或已删除项目一律 404）：
- POST /api/projects/{project_id}/storyboards/decompose          AI 拆解（全量重建）
- GET  /api/projects/{project_id}/storyboards                    分镜列表（按 sequence）
- PUT  /api/projects/{project_id}/storyboards/{storyboard_id}    更新（character_links 整体替换）
- POST /api/projects/{project_id}/storyboards/reorder            重排 sequence
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/split    按对白行拆分
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/merge    合并两镜
- POST /api/projects/{project_id}/storyboards/warnings           告警检查
- POST /api/projects/{project_id}/storyboards/continuity-check  连续性检查（SB-06）
- POST /api/projects/{project_id}/storyboards/auto-fit-duration 目标时长自动调整（SB-07）
- POST /api/projects/{project_id}/storyboards/{storyboard_id}/regen    重生成计数（RD-25）
- POST /api/projects/{project_id}/storyboards/deliver-to-canvas  投递画布
- GET  /api/projects/{project_id}/storyboards/episodes/{episode_id}/export
                                                                分镜表导出（xlsx/csv/pdf）

注意：路由需携带 {project_id} 路径参数，FastAPI router prefix 不支持路径参数，
因此本路由不设 prefix，路由内部书写完整路径；静态路径段（reorder/warnings 等）
注册在带 {storyboard_id} 的路径之前以避免误匹配。
"""

import csv
import io
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import Episode, User
from app.schemas.storyboard import (
    AutoFitDurationOut,
    AutoFitDurationRequest,
    ContinuityCheckOut,
    ContinuityCheckRequest,
    DecomposeRequest,
    DeliverCanvasOut,
    DeliverCanvasRequest,
    MergeRequest,
    ReorderRequest,
    SplitRequest,
    StoryboardOut,
    StoryboardRegenOut,
    StoryboardUpdateRequest,
    StoryboardWarningsOut,
)
from app.services.script_service import get_owned_project
from app.services.generation_service import render_stale_storyboards
from app.services.storyboard_service import (
    auto_fit_duration,
    continuity_check,
    decompose_storyboards,
    deliver_to_canvas,
    list_storyboards,
    merge_storyboards,
    reorder_storyboards,
    regen_storyboard,
    split_storyboard,
    storyboard_warnings,
    update_storyboard,
)

router = APIRouter(tags=["storyboards"])


@router.post("/api/projects/{project_id}/storyboards/decompose")
async def decompose(
    project_id: int,
    episode_id: Optional[int] = Query(default=None),
    req: Optional[DecomposeRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """AI 拆解：读剧本 content.scenes 逐场景生成 1~3 镜（LLM 失败规则降级 2 镜），
    project.status='storyboard'。

    P0-1 锁定门禁：默认校验主要角色定妆照就绪，未就绪返回 409（body 含
    code=asset_lock_not_ready 与未就绪清单）；前端快速模式勾选后传
    skip_lock_check=true 跳过。空 body 兼容旧调用（等同严格校验）。

    按集拆解（S-06 全局剧集上下文）：episode_id 传值时只读取并重建该剧集的
    分镜（其他集分镜保留，sequence 跨集全局重排）；缺省全量重建（既有行为）。

    Args:
        project_id: 项目 ID。
        episode_id: 可选剧集 ID（按集拆解范围；None=全量重建）。
        req: 拆解请求（可空；skip_lock_check=快速模式跳过门禁）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"total": 新建分镜数, "degraded": 是否发生过规则降级}。

    Raises:
        HTTPException: 400 —— 范围内无剧本或剧本无场景数据；
            409 —— 主要角色定妆照未就绪（body 含未就绪清单）；404 —— 项目不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    return await decompose_storyboards(
        db, project,
        skip_lock_check=bool(req and req.skip_lock_check),
        scope_episode_id=episode_id,
    )


@router.post("/api/projects/{project_id}/storyboards/reorder")
async def reorder(
    project_id: int,
    req: ReorderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """按 ordered_ids 顺序重写 sequence（1..n，须恰好覆盖项目全部分镜）。

    Args:
        project_id: 项目 ID。
        req: 重排请求（ordered_ids）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"total": 重排分镜数}。

    Raises:
        HTTPException: 400 —— ordered_ids 未恰好覆盖全部分镜；404 —— 项目不存在。
    """
    project = await _owned(db, project_id, user)
    total = await reorder_storyboards(db, project, req.ordered_ids)
    return {"total": total}


@router.post("/api/projects/{project_id}/storyboards/warnings", response_model=StoryboardWarningsOut)
async def warnings(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardWarningsOut:
    """分镜告警检查：总时长偏差>±15%、单镜>10s、实体未强绑资产（scene_id 为 NULL）。

    Args:
        project_id: 项目 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardWarningsOut: {"total_duration","target_duration","over_ratio","warnings"}。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    return StoryboardWarningsOut.model_validate(await storyboard_warnings(db, project))


@router.post("/api/projects/{project_id}/storyboards/continuity-check", response_model=ContinuityCheckOut)
async def continuity_check_endpoint(
    project_id: int,
    req: ContinuityCheckRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContinuityCheckOut:
    """连续性检查（SB-06）：窄口径规则（场景跳变/服装变体冲突/提示词缺失），
    RD-23「宁可漏报不误报」，无问题返回空 items。

    Args:
        project_id: 项目 ID。
        req: 检查请求（episode_id 可空，None = 项目全部分镜）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        ContinuityCheckOut: {"items": [{storyboard_id,type,message,confidence}],
            "checked_pairs": 检查的相邻镜头对数量}。

    Raises:
        HTTPException: 404 —— 项目或剧集不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    await _episode_owned_or_404(db, project, req.episode_id)
    return ContinuityCheckOut.model_validate(await continuity_check(db, project, req.episode_id))


@router.post("/api/projects/{project_id}/storyboards/auto-fit-duration", response_model=AutoFitDurationOut)
async def auto_fit_duration_endpoint(
    project_id: int,
    req: AutoFitDurationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AutoFitDurationOut:
    """目标时长自动调整（SB-07）：分镜时长按比例等比缩放至目标总时长并落库。

    Args:
        project_id: 项目 ID。
        req: 调整请求（target_seconds 1~3600；episode_id 可空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        AutoFitDurationOut: {"before_total","after_total","target_seconds",
            "changed","per_shot"}。

    Raises:
        HTTPException: 400 —— target_seconds 越界或无分镜可调整；
            404 —— 项目或剧集不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    await _episode_owned_or_404(db, project, req.episode_id)
    result = await auto_fit_duration(db, project, req.target_seconds, req.episode_id)
    return AutoFitDurationOut.model_validate(result)


@router.post("/api/projects/{project_id}/storyboards/deliver-to-canvas", response_model=DeliverCanvasOut)
async def deliver_to_canvas_endpoint(
    project_id: int,
    req: Optional[DeliverCanvasRequest] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeliverCanvasOut:
    """分镜投递画布：建 studio_linked 画布 + text/asset_ref/generate 三节点 +
    text→generate、asset_ref→generate 两条连线。

    Args:
        project_id: 项目 ID。
        req: 投递请求（storyboard_id 可缺省，缺省取第一镜）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        DeliverCanvasOut: {"canvas_id": 新建画布 ID}。

    Raises:
        HTTPException: 400 —— 项目尚无分镜；404 —— 项目或分镜不存在。
    """
    project = await _owned(db, project_id, user)
    body = req or DeliverCanvasRequest()
    canvas_id = await deliver_to_canvas(db, user, project, body.storyboard_id)
    return DeliverCanvasOut(canvas_id=canvas_id)


@router.get("/api/projects/{project_id}/storyboards", response_model=List[StoryboardOut])
async def list_storyboards_endpoint(
    project_id: int,
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[StoryboardOut]:
    """查询分镜列表（按 sequence 升序，每项含 characters 与 scene_name）。

    Args:
        project_id: 项目 ID。
        episode_id: 剧集过滤（可空）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[StoryboardOut]: 分镜列表。

    Raises:
        HTTPException: 404 —— 项目不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    items = await list_storyboards(db, project, episode_id)
    return [StoryboardOut.model_validate(i) for i in items]


@router.put("/api/projects/{project_id}/storyboards/{storyboard_id}", response_model=StoryboardOut)
async def update_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    req: StoryboardUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardOut:
    """更新分镜字段；character_links 提供时整体替换角色关联。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        req: 更新请求。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardOut: 更新后的分镜信息。

    Raises:
        HTTPException: 400 —— scene_id/角色/变体引用非法；404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    result = await update_storyboard(db, project, storyboard_id, req)
    return StoryboardOut.model_validate(result)


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/split", response_model=List[StoryboardOut])
async def split_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    req: SplitRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[StoryboardOut]:
    """按对白行拆分为两镜（前 at_line 行留本镜，其余进新镜，sequence 重排）。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        req: 拆分请求（at_line）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        List[StoryboardOut]: [拆分后的本镜, 新镜]。

    Raises:
        HTTPException: 400 —— 对白不足两行或行号越界；404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    items = await split_storyboard(db, project, storyboard_id, req.at_line)
    return [StoryboardOut.model_validate(i) for i in items]


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/merge", response_model=StoryboardOut)
async def merge_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    req: MergeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardOut:
    """合并两镜：dialogue 拼接、时长相加、删除 other，sequence 重排。

    Args:
        project_id: 项目 ID。
        storyboard_id: 保留的分镜 ID。
        req: 合并请求（other_id）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardOut: 合并后的分镜信息。

    Raises:
        HTTPException: 400 —— 与自身合并；404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    result = await merge_storyboards(db, project, storyboard_id, req.other_id)
    return StoryboardOut.model_validate(result)


@router.post("/api/projects/{project_id}/storyboards/render-stale")
async def render_stale(
    project_id: int,
    episode_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量重渲染过期镜头（P0-3 版本透传）。

    找出引用资产（角色定妆照/场景概念图）版本已过期的分镜，逐镜入队关键帧
    重生成任务（10 积分/帧，统一冻结）；已在队列中的镜头自动跳过。

    Args:
        project_id: 项目 ID。
        episode_id: 可选剧集过滤（缺省全部）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        dict: {"queued": 入队数, "skipped": 跳过数, "storyboard_ids": [...],
              "total_cost": 冻结积分总额}。

    Raises:
        HTTPException: 409 —— 积分余额不足；404 —— 项目不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    return await render_stale_storyboards(db, project=project, episode_id=episode_id)


@router.post("/api/projects/{project_id}/storyboards/{storyboard_id}/regen", response_model=StoryboardRegenOut)
async def regen_storyboard_endpoint(
    project_id: int,
    storyboard_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StoryboardRegenOut:
    """分镜重生成计数：regen_count += 1；达到 3 次时 suggest_canvas=True（RD-25）。

    Args:
        project_id: 项目 ID。
        storyboard_id: 分镜 ID。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StoryboardRegenOut: {"regen_count", "suggest_canvas"}。

    Raises:
        HTTPException: 404 —— 分镜不存在。
    """
    project = await _owned(db, project_id, user)
    return StoryboardRegenOut.model_validate(await regen_storyboard(db, project, storyboard_id))


async def _owned(db: AsyncSession, project_id: int, user: User):
    """校验项目归属并返回项目对象（本路由共用，统一 404）。

    Args:
        db: 请求级异步数据库会话。
        project_id: 项目 ID。
        user: 当前用户。

    Returns:
        Project: 归属当前用户的项目 ORM 对象。

    Raises:
        HTTPException: 404 —— 项目不存在、已删除或不属于当前用户。
    """
    return await get_owned_project(db, project_id, user.id)


async def _episode_owned_or_404(db: AsyncSession, project, episode_id) -> None:
    """校验剧集归属本项目（连续性检查 / 时长调整共用，统一 404）。

    Args:
        db: 请求级异步数据库会话。
        project: 已校验归属的项目 ORM 对象。
        episode_id: 剧集 ID（None 表示不过滤，直接放行）。

    Raises:
        HTTPException: 404 —— 剧集不存在或不属于该项目。
    """
    if episode_id is None:
        return
    episode = await db.get(Episode, episode_id)
    if episode is None or episode.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧集不存在")


# ---------------------------------------------------------------------------
# 分镜表导出（Task 7.2）
# ---------------------------------------------------------------------------
# 导出表头（与列数据一一对应）
_EXPORT_HEADERS = ["编号", "场景", "景别", "画面描述", "台词", "旁白/口播",
                   "预估时长", "提示词", "状态", "关键帧 URL"]
# xlsx 响应 MIME 类型
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# 单列最大列宽（防止超长提示词把列撑到不可读）
_MAX_COLUMN_WIDTH = 60


def _split_dialogue(dialogue: Optional[str]) -> tuple:
    """把分镜对白文本按"旁白"说话人拆为（台词, 旁白/口播）两列。

    拆解落库的对白为多行 "角色名：台词" 文本；说话人为「旁白」的行归入
    旁白/口播列，其余归入台词列；无说话人前缀的整行视为台词。

    Args:
        dialogue: 分镜 dialogue 字段文本（可空）。

    Returns:
        tuple: (台词多行文本, 旁白多行文本)，均为去掉首尾空白的字符串。

    Raises:
        无：纯字符串运算，不产生异常路径。
    """
    speech: List[str] = []
    narration: List[str] = []
    for line in (dialogue or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("旁白：") or line.startswith("旁白:"):
            content = line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]
            narration.append(content.strip())
        else:
            speech.append(line)
    return "\n".join(speech), "\n".join(narration)


def _build_export_rows(items: List[dict]) -> List[List]:
    """把分镜输出 dict 列表转换为导出表格行（与 _EXPORT_HEADERS 对齐）。

    Args:
        items: list_storyboards 返回的分镜输出列表（含 scene_name）。

    Returns:
        List[List]: 每镜一行，共 10 列的单元格值列表。

    Raises:
        无：纯字典取值与字符串处理。
    """
    rows: List[List] = []
    for it in items:
        speech, narration = _split_dialogue(it.get("dialogue"))
        prompt = (it.get("prompt_editable") or "").strip() or (it.get("prompt_locked") or "")
        rows.append([
            it.get("sequence"),
            it.get("scene_name") or "",
            it.get("shot_type") or "",
            it.get("visual_description") or "",
            speech,
            narration,
            it.get("duration"),
            prompt,
            it.get("status") or "",
            it.get("keyframe_url") or "",
        ])
    return rows


@router.get("/api/projects/{project_id}/storyboards/episodes/{episode_id}/export")
async def export_storyboard_sheet(
    project_id: int,
    episode_id: int,
    format: str = Query(default="xlsx"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """导出指定剧集的分镜表（xlsx / csv / pdf，数据源与列表接口同构）。

    归属校验链：episode → project → 当前用户（任一失败统一 404）。
    episode_id 传真实剧集 ID 时按该剧集导出（S-06 剧集切换器默认行为）；
    特殊值 episode_id=0 保留为「全部分镜」兼容语义：不筛剧集导出项目全部
    镜头（普通创作流拆解的分镜 episode_id 为空，无剧集可绑定；工作区未
    选择具体剧集时前端回退传 0）。pdf 格式由 fpdf2 生成 A4 横向表格
    （SB-08）：中文字体探测失败时自动降级英文兜底版，并在响应头
    X-PDF-Font-Fallback: true 标记。

    Args:
        project_id: 项目 ID。
        episode_id: 剧集 ID（必须属于该项目；0 表示导出全部，兼容语义）。
        format: 导出格式，支持 xlsx / csv / pdf（默认 xlsx）。
        user: 当前用户。
        db: 请求级异步数据库会话。

    Returns:
        StreamingResponse: xlsx（openpyxl，表头加粗、列宽自适应）、
            csv（utf-8-sig BOM，便于 Excel 直接打开）或 pdf（fpdf2）字节流。

    Raises:
        HTTPException: 400 —— format 非法；404 —— 项目或剧集不存在/非本人。
    """
    project = await _owned(db, project_id, user)
    # format 手动校验返回 400（Query pattern 会产生 422，与任务约定 400 不符）
    if format not in ("xlsx", "csv", "pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="format 仅支持 xlsx / csv / pdf"
        )
    if episode_id == 0:
        # 全量导出：不校验剧集，直接取项目全部分镜
        episode = None
    else:
        episode = await db.get(Episode, episode_id)
        if episode is None or episode.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="剧集不存在")

    # 与列表接口同构的数据源（含场景名联查）；0 表示全量（过滤条件传 None）
    items = await list_storyboards(db, project, episode_id if episode_id != 0 else None)
    rows = _build_export_rows(items)

    name_stem = "storyboards_all" if episode_id == 0 else f"storyboards_ep{episode_id}"
    ascii_name = f"{name_stem}.{format}"
    utf8_name = quote(f"{name_stem}.{format}")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

    if format == "csv":
        # utf-8-sig：带 BOM，保证 Excel 打开中文不乱码
        sio = io.StringIO()
        writer = csv.writer(sio)
        writer.writerow(_EXPORT_HEADERS)
        writer.writerows(rows)
        buf = io.BytesIO(sio.getvalue().encode("utf-8-sig"))
        return StreamingResponse(buf, media_type="text/csv",
                                 headers={"Content-Disposition": disposition})

    if format == "pdf":
        # SB-08：PDF 导出。懒导入 pdf_export_service，避免 fpdf2 未安装时
        # 影响路由模块整体加载（xlsx/csv 导出与分镜其他功能不受影响）；
        # 字体缺失降级时在响应头标记 X-PDF-Font-Fallback（不 500）
        from app.services.pdf_export_service import build_storyboard_pdf

        pdf_bytes, font_fallback = build_storyboard_pdf(items)
        pdf_headers = {"Content-Disposition": disposition}
        if font_fallback:
            pdf_headers["X-PDF-Font-Fallback"] = "true"
        return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf",
                                 headers=pdf_headers)

    # xlsx：表头加粗 + 简单列宽自适应（取各列最大字符宽度，钳位上限）
    wb = Workbook()
    ws = wb.active
    ws.title = "分镜表"
    ws.append(_EXPORT_HEADERS)
    for row in rows:
        ws.append(row)
    bold = Font(bold=True)
    for cell in ws[1]:
        cell.font = bold
    for idx, column_cells in enumerate(ws.columns, start=1):
        width = max((len(str(c.value)) if c.value is not None else 0 for c in column_cells),
                    default=0) + 2
        ws.column_dimensions[get_column_letter(idx)].width = min(width, _MAX_COLUMN_WIDTH)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type=_XLSX_MEDIA_TYPE,
                             headers={"Content-Disposition": disposition})
