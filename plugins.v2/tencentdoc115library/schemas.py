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


class ManualImportRequest(BaseModel):
    """手动添加115分享资源；空字段会读取插件配置页中已保存的值。"""

    links: Optional[str] = Field(default=None, description="单条或批量115分享链接")
    group_name: Optional[str] = Field(default=None, max_length=120)
    media_mode: Optional[str] = Field(default=None, description="movie、tv 或 mixed")
