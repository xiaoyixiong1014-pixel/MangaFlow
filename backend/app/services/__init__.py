"""业务服务层包（文件级说明）。

承载跨路由复用的核心业务逻辑与通用工具：
- billing_service：钱包冻结 / 扣费 / 退款 / 发放（行锁 + 幂等）
- media_service：上传文件落盘与媒体类型推断
- normalize_pagination：列表接口统一的分页参数规整工具

路由层（app/api/*）只做参数校验与编排，复杂事务逻辑统一收敛在本包。
"""

from typing import Tuple

# 每页条数上限（防止一次拉取过量数据）
MAX_PAGE_SIZE = 100


def normalize_pagination(page: int, page_size: int) -> Tuple[int, int]:
    """规整分页参数：页码最小为 1，每页条数限制在 [1, 100]。

    Args:
        page: 请求页码。
        page_size: 请求每页条数。

    Returns:
        Tuple[int, int]: 规整后的 (page, page_size)。

    Raises:
        无：非法值一律钳位到边界，不抛异常。
    """
    safe_page = max(1, int(page or 1))
    safe_size = min(max(1, int(page_size or 10)), MAX_PAGE_SIZE)
    return safe_page, safe_size
