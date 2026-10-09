"""计费 Pydantic Schema 模块（文件级说明）。

定义钱包、积分流水、积分套餐、订单等出入参模型。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class WalletOut(BaseModel):
    """钱包输出模型。

    Attributes:
        points_balance: 可用积分。
        frozen_points: 冻结积分。
        low_balance: 是否低余额（可用积分 < 1000）。
        version: 乐观锁版本号。
    """

    points_balance: int
    frozen_points: int
    low_balance: bool
    version: int


class TransactionOut(BaseModel):
    """积分流水输出模型。

    Attributes:
        id: 流水 ID。
        type: 类型（freeze/consume/refund/recharge/grant）。
        amount: 变动积分（带符号：freeze/consume 为负，refund/grant/recharge 为正）。
        bear: 成本承担方（user/platform）。
        ref_task_id: 关联任务 ID（可空）。
        ref_order_id: 关联订单 ID（可空）。
        idempotency_key: 幂等键。
        remark: 备注。
        created_at: 流水时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    amount: int
    bear: str
    ref_task_id: Optional[int] = None
    ref_order_id: Optional[int] = None
    idempotency_key: str
    remark: Optional[str] = None
    created_at: Optional[datetime] = None


class PackageOut(BaseModel):
    """积分套餐输出模型。

    Attributes:
        id: 套餐标识（p_small/p_mid/p_large/p_sub）。
        name: 套餐名称。
        points: 套餐积分。
        price: 售价（人民币元）。
        subscription: 是否为订阅型套餐。
    """

    id: str
    name: str
    points: int
    price: float
    subscription: bool = False


class OrderCreateRequest(BaseModel):
    """订单创建请求。

    Attributes:
        package_id: 套餐标识（须为有效套餐常量）。
    """

    package_id: str = Field(min_length=1, max_length=30)


class OrderOut(BaseModel):
    """订单输出模型。

    Attributes:
        id: 订单 ID。
        order_no: 订单号。
        product_type: 商品类型（points / subscription）。
        amount: 订单金额（元）。
        points: 兑换积分。
        pay_channel: 支付渠道。
        status: 订单状态（pending/paid/closed/refunded）。
        paid_at: 支付时间（ISO 字符串，可空）。
        created_at: 下单时间（ISO 字符串）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    product_type: str
    amount: float
    points: int
    pay_channel: Optional[str] = None
    status: str
    paid_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class PayResponse(BaseModel):
    """模拟支付响应。

    Attributes:
        order: 支付后的订单信息。
        points_balance: 支付到账后的可用积分余额。
    """

    order: OrderOut
    points_balance: int


class MonthConsumedOut(BaseModel):
    """月度消耗统计项。

    Attributes:
        month: 月份（格式 YYYY-MM）。
        consumed: 该月消耗积分（正值）。
    """

    month: str
    consumed: int
