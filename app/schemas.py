from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_timestamp(value: str) -> str:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("时间须包含时区")
    return stamp.astimezone(timezone.utc).isoformat()


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    model: str | None = None
    version: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class DocumentInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    model: str = Field(default="AIR-SYSTEM", min_length=1, max_length=100)
    version: str = Field(default="1.0", min_length=1, max_length=80)
    kind: Literal["manual", "work_order"] = "manual"
    text: str = Field(min_length=1, max_length=500000)
    preliminary_cause: str | None = Field(default=None, max_length=1000)
    confirmed_cause: str | None = Field(default=None, max_length=1000)


class StartInput(BaseModel):
    device_ids: list[str] = Field(default_factory=list, max_length=20)
    symptom: str = Field(min_length=1, max_length=2000)
    alarm_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_devices(self):
        self.device_ids = list(dict.fromkeys(self.device_ids))
        return self


class ClarifyInput(BaseModel):
    device_ids: list[str] = Field(min_length=1, max_length=20)
    expected_revision: int = Field(ge=0)


class FeedbackInput(BaseModel):
    check_id: str = Field(min_length=1, max_length=100)
    result: Literal["normal", "abnormal", "unknown"]
    note: str = Field(default="", max_length=2000)
    expected_revision: int = Field(ge=0)


class ConfirmInput(BaseModel):
    decision: Literal["confirmed", "handoff"]
    operator: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=0)


class MeasurementInput(BaseModel):
    node_id: str
    value: float = Field(ge=0, le=100)
    observed_at: str
    source: str = Field(min_length=1, max_length=200)


class ModelCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=8, max_length=2000)
    start: int = Field(ge=0, strict=True)
    end: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def valid_span(self):
        if len(self.quote.strip()) < 8 or self.end-self.start != len(self.quote):
            raise ValueError("引文长度与位置不一致")
        return self


class ModelClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=800)
    citations: list[ModelCitation] = Field(min_length=1, max_length=5)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("结论不能为空")
        return value


class ModelExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[ModelClaim] = Field(min_length=1, max_length=8)


class TopologyNode(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["source", "branch", "device"]
    model: str = Field(min_length=1, max_length=100)
    source: str = Field(default="manual_import", min_length=1, max_length=200)


class TopologyEdge(BaseModel):
    source: str
    target: str


class TopologyInput(BaseModel):
    nodes: list[TopologyNode] = Field(min_length=1, max_length=500)
    edges: list[TopologyEdge] = Field(default_factory=list, max_length=1000)


class RepairInput(BaseModel):
    operator: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=2000)
    completed_at: str
    expected_revision: int = Field(ge=0)

    _utc = field_validator("completed_at")(utc_timestamp)


class RetestReading(BaseModel):
    device_id: str
    value: float = Field(ge=0, le=100, allow_inf_nan=False)
    minimum: float = Field(ge=0, le=100, allow_inf_nan=False)
    maximum: float = Field(ge=0, le=100, allow_inf_nan=False)
    observed_at: str
    reference_document_id: str
    standard_id: str | None = None

    _utc = field_validator("observed_at")(utc_timestamp)

    @model_validator(mode="after")
    def valid_range(self):
        if self.minimum > self.maximum:
            raise ValueError("压力下限不能超过上限")
        return self


class VerifyInput(BaseModel):
    result: Literal["passed", "failed", "unknown"]
    operator: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)
    alarms_cleared: bool
    readings: list[RetestReading] = Field(default_factory=list, max_length=20)
    expected_revision: int = Field(ge=0)


class DocumentStatusInput(BaseModel):
    active: bool


class AlarmInput(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    device_id: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=100)
    status: Literal["raised", "cleared"]
    occurred_at: str
    source: str = Field(min_length=1, max_length=100)
    message: str = Field(default="", max_length=2000)
    _utc = field_validator("occurred_at")(utc_timestamp)


class StandardInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=100)
    minimum: float = Field(ge=0, le=100, allow_inf_nan=False)
    maximum: float = Field(ge=0, le=100, allow_inf_nan=False)
    reference_document_id: str
    page: int = Field(ge=1, le=500)
    quote: str = Field(min_length=1, max_length=4000)
    source_start: int | None = Field(default=None, ge=0)
    approved_by: str = Field(min_length=1, max_length=100)
    @model_validator(mode="after")
    def valid_bounds(self):
        if self.minimum > self.maximum:
            raise ValueError("标准下限不能超过上限")
        return self
