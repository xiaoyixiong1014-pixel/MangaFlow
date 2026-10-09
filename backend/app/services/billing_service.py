"""积分账务服务模块（文件级说明）。

实现钱包核心账务操作：冻结（freeze）/ 冻结扣费（consume）/ 冻结退还（refund）/
积分发放（grant/recharge）。所有操作统一保证：

- 行锁并发安全：先以 `select ... with_for_update()` 锁定钱包行，再修改数值；
- 数值非负约束：任何操作导致 points_balance / frozen_points 出现负数时抛出
  HTTPException（409 冲突 / 400 参数错），绝不落库负值；
- 幂等（RD-55）：以 wallet_transactions.idempotency_key 唯一索引兜底，重复请求
  直接返回已存在的流水，不重复变更余额；
- 流水完整：每次变动都写入一条 wallet_transactions 记录。

amount 正负号约定（写入 wallet_transactions.amount 字段时即为带符号值）：
    freeze   -> -amount（可用余额减少，冻结增加，资产总额不变）
    consume  -> -amount（冻结减少，资产总额减少）
    refund   -> +amount（冻结减少，可用余额增加，资产总额不变）
    grant / recharge -> +amount（可用余额增加）
"""

from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Wallet, WalletTransaction

# 允许的成本承担方取值（RD-28）
_BEAR_VALUES = ("user", "platform")


async def _get_wallet_for_update(db: AsyncSession, user_id: int) -> Wallet:
    """以行锁获取用户钱包（内部函数）。

    Args:
        db: 异步数据库会话。
        user_id: 用户 ID。

    Returns:
        Wallet: 已加行锁（FOR UPDATE）的钱包 ORM 对象。

    Raises:
        HTTPException: 404 —— 用户钱包不存在。
    """
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
    ).scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户钱包不存在")
    return wallet


async def _get_existing_tx(db: AsyncSession, idempotency_key: str) -> Optional[WalletTransaction]:
    """按幂等键查询已存在的流水（内部函数）。

    Args:
        db: 异步数据库会话。
        idempotency_key: 幂等键。

    Returns:
        Optional[WalletTransaction]: 已存在的流水；不存在时为 None。
    """
    return (
        await db.execute(
            select(WalletTransaction).where(WalletTransaction.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()


def _validate_amount(amount: int) -> None:
    """校验积分数量的合法性（内部函数）。

    Args:
        amount: 积分数量。

    Raises:
        HTTPException: 400 —— 数量不是正整数。
    """
    if not isinstance(amount, int) or amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="积分数量必须为正整数")


def _validate_bear(bear: str) -> None:
    """校验成本承担方取值（内部函数）。

    Args:
        bear: 成本承担方（user / platform）。

    Raises:
        HTTPException: 400 —— 取值不在允许范围内。
    """
    if bear not in _BEAR_VALUES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bear 取值非法")


async def freeze_points(
    db: AsyncSession,
    user_id: int,
    amount: int,
    ref_task_id: Optional[int] = None,
    idempotency_key: str = "",
) -> WalletTransaction:
    """冻结积分：可用余额减少、冻结积分增加（生成任务前预扣，RD-28）。

    Args:
        db: 异步数据库会话（函数内部提交事务）。
        user_id: 用户 ID。
        amount: 冻结积分数量（正整数）。
        ref_task_id: 关联任务 ID（无任务场景传 None）。
        idempotency_key: 幂等键（唯一；重复请求返回已有流水）。

    Returns:
        WalletTransaction: 本次冻结流水（type='freeze'，amount=-amount）。

    Raises:
        HTTPException: 400 —— 积分数量非法；409 —— 可用积分余额不足；
            404 —— 钱包不存在。
    """
    _validate_amount(amount)
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少幂等键")

    # 幂等短路：同键流水已存在则直接返回，不重复扣减
    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    wallet = await _get_wallet_for_update(db, user_id)
    # 加锁后二次幂等检查（并发场景下前一个事务已提交同键流水）
    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    if wallet.points_balance < amount:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="积分余额不足，无法冻结")
    wallet.points_balance -= amount
    wallet.frozen_points += amount
    wallet.version += 1  # 乐观锁版本号随行锁变更递增

    tx = WalletTransaction(
        user_id=user_id,
        type="freeze",
        amount=-amount,  # 符号约定：freeze 为负
        bear="user",
        ref_task_id=ref_task_id,
        ref_order_id=None,
        idempotency_key=idempotency_key,
        remark="冻结积分",
    )
    db.add(tx)
    await db.commit()
    return tx


async def consume_frozen(
    db: AsyncSession,
    user_id: int,
    amount: int,
    ref_task_id: Optional[int] = None,
    idempotency_key: str = "",
    remark: str = "",
    bear: str = "user",
) -> WalletTransaction:
    """冻结扣费：任务成功后从冻结积分中实际扣除。

    Args:
        db: 异步数据库会话（函数内部提交事务）。
        user_id: 用户 ID。
        amount: 扣费积分数量（正整数，须不超过当前冻结额度）。
        ref_task_id: 关联任务 ID（无任务场景传 None）。
        idempotency_key: 幂等键（唯一；重复请求返回已有流水）。
        remark: 备注说明。
        bear: 成本承担方 user / platform（RD-28）。

    Returns:
        WalletTransaction: 本次扣费流水（type='consume'，amount=-amount）。

    Raises:
        HTTPException: 400 —— 积分数量或 bear 非法；409 —— 冻结积分不足；
            404 —— 钱包不存在。
    """
    _validate_amount(amount)
    _validate_bear(bear)
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少幂等键")

    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    wallet = await _get_wallet_for_update(db, user_id)
    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    if wallet.frozen_points < amount:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="冻结积分不足，无法扣费")
    wallet.frozen_points -= amount
    wallet.version += 1

    tx = WalletTransaction(
        user_id=user_id,
        type="consume",
        amount=-amount,  # 符号约定：consume 为负
        bear=bear,
        ref_task_id=ref_task_id,
        ref_order_id=None,
        idempotency_key=idempotency_key,
        remark=remark or "消耗冻结积分",
    )
    db.add(tx)
    await db.commit()
    return tx


async def refund_frozen(
    db: AsyncSession,
    user_id: int,
    amount: int,
    ref_task_id: Optional[int] = None,
    idempotency_key: str = "",
    bear: str = "platform",
    remark: str = "",
) -> WalletTransaction:
    """冻结退还：任务失败等场景将冻结积分退回可用余额（不产生实际损耗）。

    Args:
        db: 异步数据库会话（函数内部提交事务）。
        user_id: 用户 ID。
        amount: 退还积分数量（正整数，须不超过当前冻结额度）。
        ref_task_id: 关联任务 ID（无任务场景传 None）。
        idempotency_key: 幂等键（唯一；重复请求返回已有流水）。
        bear: 成本承担方 user / platform（平台故障退款时为 platform）。
        remark: 备注说明。

    Returns:
        WalletTransaction: 本次退款流水（type='refund'，amount=+amount）。

    Raises:
        HTTPException: 400 —— 积分数量或 bear 非法；409 —— 冻结积分不足；
            404 —— 钱包不存在。
    """
    _validate_amount(amount)
    _validate_bear(bear)
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少幂等键")

    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    wallet = await _get_wallet_for_update(db, user_id)
    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    if wallet.frozen_points < amount:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="冻结积分不足，无法退款")
    wallet.frozen_points -= amount
    wallet.points_balance += amount
    wallet.version += 1

    tx = WalletTransaction(
        user_id=user_id,
        type="refund",
        amount=amount,  # 符号约定：refund 为正
        bear=bear,
        ref_task_id=ref_task_id,
        ref_order_id=None,
        idempotency_key=idempotency_key,
        remark=remark or "退还冻结积分",
    )
    db.add(tx)
    await db.commit()
    return tx


async def grant_points(
    db: AsyncSession,
    user_id: int,
    amount: int,
    ref_order_id: Optional[int] = None,
    idempotency_key: str = "",
    remark: str = "",
    tx_type: str = "grant",
    bear: str = "platform",
) -> WalletTransaction:
    """发放积分：注册赠送 / 订单充值等到账（可用余额直接增加）。

    Args:
        db: 异步数据库会话（函数内部提交事务）。
        user_id: 用户 ID。
        amount: 发放积分数量（正整数）。
        ref_order_id: 关联订单 ID（无订单场景传 None）。
        idempotency_key: 幂等键（唯一；重复请求返回已有流水）。
        remark: 备注说明。
        tx_type: 流水类型，'grant'（平台赠送）或 'recharge'（付费充值到账）。
        bear: 成本承担方 user / platform。

    Returns:
        WalletTransaction: 本次发放流水（amount=+amount）。

    Raises:
        HTTPException: 400 —— 积分数量 / 流水类型 / bear 非法；404 —— 钱包不存在。
    """
    _validate_amount(amount)
    if tx_type not in ("grant", "recharge"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="流水类型非法")
    _validate_bear(bear)
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少幂等键")

    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    wallet = await _get_wallet_for_update(db, user_id)
    existing = await _get_existing_tx(db, idempotency_key)
    if existing is not None:
        return existing

    wallet.points_balance += amount
    wallet.version += 1

    tx = WalletTransaction(
        user_id=user_id,
        type=tx_type,
        amount=amount,  # 符号约定：grant/recharge 为正
        bear=bear,
        ref_task_id=None,
        ref_order_id=ref_order_id,
        idempotency_key=idempotency_key,
        remark=remark or "发放积分",
    )
    db.add(tx)
    await db.commit()
    return tx
