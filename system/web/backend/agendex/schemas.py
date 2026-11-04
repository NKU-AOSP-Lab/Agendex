"""网页输入契约。规则是数据，不接受用户代码或任意网络地址。"""

from typing import Any, Literal, Optional
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_filter_tag_ids(value: list[str]) -> list[str]:
    """The same bounded selection is accepted by URL filters and frozen tasks."""
    if not isinstance(value, list) or len(value) > 40:
        raise ValueError("标签筛选最多选择40个标签")
    if any(not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier) for identifier in value):
        raise ValueError("标签筛选包含无效标签ID")
    if len(set(value)) != len(value):
        raise ValueError("标签筛选不能重复")
    return value


class TagInput(InputModel):
    id: Optional[str] = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=8000)
    threshold: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @field_validator("label", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("标签名称和判断规则不能为空")
        return value.strip()


class PolicyInput(InputModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=12000)
    mode: Literal["multi_label", "single_label"] = "multi_label"
    threshold: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)
    tags: list[TagInput] = Field(min_length=1, max_length=40)
    evidence_kinds: list[Literal["metadata", "readme", "source_code"]] = Field(default_factory=lambda: ["metadata"], min_length=1, max_length=3)

    @field_validator("name", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def check_unique(self):
        if not self.name:
            raise ValueError("策略名称不能为空")
        labels = [tag.label for tag in self.tags]
        ids = [tag.id for tag in self.tags if tag.id]
        if len(set(labels)) != len(labels) or len(set(ids)) != len(ids):
            raise ValueError("标签名称或 ID 重复")
        if len(set(self.evidence_kinds)) != len(self.evidence_kinds):
            raise ValueError("证据来源重复")
        return self


class RepositoryFilters(InputModel):
    q: str = Field(default="", max_length=300)
    language: Optional[str] = Field(default=None, max_length=100)
    retention_source: Optional[Literal["", "metadata_prescreen", "awesome_readme_reference", "legacy_topic"]] = None
    min_stars: int = Field(default=0, ge=0)
    sort: Literal["stars", "updated", "name"] = "stars"
    policy_id: Optional[str] = Field(default=None, max_length=80)
    tag_id: Optional[str] = Field(default=None, max_length=80)
    tag_ids: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("tag_ids", mode="before")
    @classmethod
    def valid_tag_selection(cls, value):
        return validate_filter_tag_ids(value)


class JobScope(InputModel):
    repository_ids: Optional[list[int]] = Field(default=None, max_length=50000)
    filters: Optional[RepositoryFilters] = None
    limit: Optional[int] = Field(default=None, ge=1, le=50000)

    @model_validator(mode="after")
    def check_selection(self):
        if self.repository_ids is not None:
            if not self.repository_ids or any(identifier <= 0 for identifier in self.repository_ids):
                raise ValueError("请选择有效仓库")
            if len(self.repository_ids) != len(set(self.repository_ids)):
                raise ValueError("仓库选择重复")
            if self.filters is not None:
                raise ValueError("仓库选择与筛选范围不能同时提供")
        return self


class JobInput(InputModel):
    policy_id: str = Field(min_length=1, max_length=80)
    model: Optional[Literal["typesafe/jev-1.14", "glm-5.3"]] = None
    # Old API clients keep the frozen policy's source setting when omitted.
    # Full-repository input is not offered until that task source is implemented.
    evidence_kinds: Optional[list[Literal["metadata", "readme"]]] = Field(default=None, min_length=1, max_length=2)
    scope: JobScope = Field(default_factory=JobScope)
    skip_completed: bool = True

    @field_validator("evidence_kinds")
    @classmethod
    def unique_evidence_kinds(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("数据来源重复")
        return value


class ThresholdInput(InputModel):
    base_revision: int = Field(ge=1)
    thresholds: dict[str, float] = Field(min_length=1)

    @field_validator("thresholds", mode="before")
    @classmethod
    def valid_thresholds(cls, value):
        import math
        if not isinstance(value, dict) or any(type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= 1 for number in value.values()):
            raise ValueError("每个标签阈值必须在 [0,1] 内")
        return value


class EvidenceInput(InputModel):
    kind: Literal["metadata", "readme", "source_code"]
    version: str = Field(min_length=1, max_length=200)
    source_key: str = Field(default="", max_length=2048)
    source_url: Optional[str] = Field(default=None, max_length=2000)
    content: Any
    provenance: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_url")
    @classmethod
    def source_link(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or any(ord(char) < 32 for char in value):
                raise ValueError("证据链接必须是 HTTP 或 HTTPS 地址")
        return value

    @model_validator(mode="after")
    def check_content(self):
        if not self.version.strip():
            raise ValueError("证据版本不能为空")
        if self.kind == "source_code" and not self.source_key:
            # 兼容已有采集器传 provenance.path 的形式，存储身份仍统一到列。
            path = self.provenance.get("path")
            if isinstance(path, str):
                self.source_key = path
        if any(ord(char) < 32 or ord(char) == 127 for char in self.source_key):
            raise ValueError("证据文件标识不能包含控制字符")
        if self.kind == "source_code" and not self.source_key.strip():
            raise ValueError("源码证据必须提供 source_key 文件标识")
        if self.kind == "metadata" and not isinstance(self.content, dict):
            raise ValueError("元数据证据必须是 JSON 对象")
        if self.kind != "metadata" and not isinstance(self.content, str):
            raise ValueError("README 或源码证据必须是文本")
        if self.kind == "readme" and not self.content.strip():
            raise ValueError("README 证据必须是非空文本")
        return self
