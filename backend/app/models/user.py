"""用户与账务域模型（文件级说明）。

包含表：users（用户）、wallets（用户钱包）、wallet_transactions（积分流水）、
orders（订单）、operation_logs（操作日志）。
严格对应架构设计文档 §5.3.1 ~ §5.3.5。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, Numeric, SmallInteger, String, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class User(Base):
    """用户表（§5.3.1 users）。

    Attributes:
        username: 用户名（唯一）。
        email: 邮箱（唯一，可空，脱敏存储展示）。
        phone: 手机号（唯一，可空）。
        password_hash: bcrypt 密码哈希。
        avatar: 头像 URL。
        role: 角色，取值 'user'（普通用户）/'operator'（运营方）。
            注：架构文档原文写 user/admin，本系统统一用 operator 表示管理员。
        vip_level: 会员等级。
        status: 状态，0 禁用 / 1 正常（账号启停开关，管理端手控）。
        account_status: 账号状态（C-04 分级处罚）：active 正常 / restricted 受限 /
            banned 禁用。注：与整型 status 列语义不同——status 为管理端手控启停，
            account_status 由内容违规处罚自动驱动；命名避开既有 status 列。
        violation_count: 内容违规累计次数（C-04，机审 violate 累加，≥3 次禁用）。
        last_login_at: 最近登录时间。
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'operator')", name="ck_users_role"),
        CheckConstraint("status IN (0, 1)", name="ck_users_status"),
        CheckConstraint("account_status IN ('active', 'restricted', 'banned')",
                        name="ck_users_account_status"),
        Index("uk_users_username", "username", unique=True),
        Index("uk_users_email", "email", unique=True),
        Index("uk_users_phone", "phone", unique=True),
        Index("idx_users_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    username: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户名")
    email: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="邮箱（脱敏存储展示）")
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="手机号")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="密码哈希（bcrypt）")
    avatar: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="头像 URL")
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="user",
                                      comment="角色：user 普通用户 / operator 运营方（文档原文 user/admin，统一用 operator 表示管理员）")
    vip_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0", comment="会员等级")
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1",
                                        comment="状态：0 禁用 / 1 正常")
    account_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active",
        comment="账号状态（C-04 分级处罚）：active/restricted/banned")
    violation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
        comment="内容违规累计次数（C-04：1-2 次受限、≥3 次禁用）")
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="最近登录时间")

    wallet: Mapped[Optional["Wallet"]] = relationship(back_populates="user", uselist=False, viewonly=True)


class Wallet(Base):
    """用户钱包表（§5.3.2 wallets）。

    Attributes:
        user_id: 用户 ID（唯一，外键级联删除）。
        points_balance: 可用积分（CHECK >= 0）。
        frozen_points: 冻结积分（CHECK >= 0）。
        version: 乐观锁版本号。
    """

    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("points_balance >= 0", name="ck_wallets_points_balance"),
        CheckConstraint("frozen_points >= 0", name="ck_wallets_frozen_points"),
        Index("uk_wallets_user", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="用户 ID（级联删除）")
    points_balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0", comment="可用积分")
    frozen_points: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0", comment="冻结积分")
    version: Mapped[int] = mapped_column(nullable=False, server_default="0", comment="乐观锁版本号")

    user: Mapped[User] = relationship(viewonly=True)


class WalletTransaction(Base):
    """积分流水表（§5.3.3 wallet_transactions）。

    Attributes:
        user_id: 用户 ID（外键 RESTRICT）。
        type: 流水类型 freeze/consume/refund/recharge/grant。
        amount: 变动积分（正负）。
        bear: 成本承担方 user/platform（RD-28）。
        ref_task_id: 关联任务 ID（外键 RESTRICT）。
        ref_order_id: 关联订单 ID（外键 RESTRICT）。
        idempotency_key: 幂等键（唯一，RD-55）。
        remark: 备注。
    """

    __tablename__ = "wallet_transactions"
    __table_args__ = (
        CheckConstraint("type IN ('freeze', 'consume', 'refund', 'recharge', 'grant')", name="ck_wtx_type"),
        CheckConstraint("bear IN ('user', 'platform')", name="ck_wtx_bear"),
        Index("uk_wtx_idem", "idempotency_key", unique=True),
        Index("idx_wtx_user_time", "user_id", "created_at"),
        Index("idx_wtx_type", "type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, comment="用户 ID（禁止级联删除）")
    type: Mapped[str] = mapped_column(String(20), nullable=False,
                                      comment="类型：freeze/consume/refund/recharge/grant")
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="变动积分（正负）")
    bear: Mapped[str] = mapped_column(String(20), nullable=False, server_default="user",
                                      comment="成本承担方：user / platform（RD-28）")
    ref_task_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True, comment="关联任务 ID")
    ref_order_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True, comment="关联订单 ID")
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, comment="幂等键（RD-55）")
    remark: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="备注")


class Order(Base):
    """订单表（§5.3.4 orders）。

    Attributes:
        user_id: 用户 ID（外键 RESTRICT）。
        order_no: 订单号（唯一）。
        product_type: 商品类型 points/subscription。
        amount: 金额（元，NUMERIC(12,2)，禁止浮点 RD-55）。
        points: 兑换积分。
        pay_channel: 支付渠道。
        status: 状态 pending/paid/closed/refunded。
        paid_at: 支付时间。
        invoice_status: 发票状态（PL-23）。
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("product_type IN ('points', 'subscription')", name="ck_orders_product_type"),
        CheckConstraint("amount >= 0", name="ck_orders_amount"),
        CheckConstraint("status IN ('pending', 'paid', 'closed', 'refunded')", name="ck_orders_status"),
        Index("uk_orders_no", "order_no", unique=True),
        Index("idx_orders_user", "user_id"),
        Index("idx_orders_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, comment="用户 ID")
    order_no: Mapped[str] = mapped_column(String(50), nullable=False, comment="订单号（唯一）")
    product_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="商品类型：points / subscription")
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, comment="金额（元）")
    points: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0", comment="兑换积分")
    pay_channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="支付渠道")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending",
                                        comment="状态：pending/paid/closed/refunded")
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="支付时间")
    invoice_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="发票状态（PL-23）")


class OperationLog(Base):
    """操作日志表（§5.3.5 operation_logs，V-02 审计）。

    Attributes:
        user_id: 操作人 ID（外键 RESTRICT：用户删除受限制，审计记录不可丢）。
        project_id: 关联项目 ID（外键 SET NULL）。
        action: 操作类型。
        detail: 操作详情（JSONB）。
        ip: 操作 IP。
        risk_level: 风险等级 normal/high（RD-56 高危操作）。
    """

    __tablename__ = "operation_logs"
    __table_args__ = (
        CheckConstraint("risk_level IN ('normal', 'high')", name="ck_oplog_risk_level"),
        Index("idx_oplog_user", "user_id"),
        Index("idx_oplog_project", "project_id"),
        Index("idx_oplog_action", "action"),
        Index("idx_oplog_time", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        comment="操作人 ID（用户删除受限制，保留审计）")
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, comment="关联项目 ID")
    action: Mapped[str] = mapped_column(String(50), nullable=False, comment="操作类型")
    detail: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, comment="操作详情")
    ip: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="操作 IP")
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False, server_default="normal",
                                            comment="风险等级：normal / high（RD-56）")
