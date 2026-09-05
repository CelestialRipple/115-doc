from typing import Literal

from pydantic import BaseModel, Field


class ResourceSearchRequest(BaseModel):
    """RE0 资源搜索请求"""

    media_type: Literal["movie", "tv"] = Field(
        ...,
        description="TMDB 媒体类型",
    )
    tmdb_id: str = Field(..., description="TMDB 媒体标识")


class UnlockRequest(BaseModel):
    """RE0 资源解锁请求"""

    slug: str = Field(..., description="搜索结果中的 RE0 资源标识")
    confirmed_points: int = Field(
        ...,
        ge=0,
        description="用户明确确认的积分消耗",
    )


class LibrarySaveRequest(BaseModel):
    """把已解锁资源交给媒体库插件的请求"""

    slug: str = Field(..., description="已解锁的 RE0 资源标识")
    group_name: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="媒体库直属输出文件夹",
    )
    media_mode: Literal["movie", "tv", "mixed"] = Field(
        ...,
        description="媒体库类型",
    )
