from typing import List, Optional

from pydantic import BaseModel, Field


class SyncActionRequest(BaseModel):
    """
    手动同步操作请求

    Attributes:
        reset: 是否放弃当前检查点并从头扫描
        max_pages: 本次最多处理的表格分页数
    """

    reset: bool = Field(default=False, description="是否从头扫描")
    max_pages: Optional[int] = Field(
        default=None,
        ge=1,
        le=100,
        description="本次最多处理的分页数",
    )


class BuildActionRequest(BaseModel):
    """
    手动生成媒体库操作请求

    Attributes:
        limit: 本次最多处理的资源数
        retry_failed: 是否同时重试失败资源
    """

    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=500,
        description="本次最多处理的资源数",
    )
    retry_failed: bool = Field(default=False, description="是否重试失败资源")


class ResourceRetryRequest(BaseModel):
    """
    单条资源重试请求

    Attributes:
        resource_ids: 要重试的资源 ID 列表
    """

    resource_ids: List[str] = Field(
        min_length=1,
        max_length=100,
        description="要重试的资源 ID 列表",
    )
