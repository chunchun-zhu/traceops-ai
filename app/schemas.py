from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class ModelExplanation(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


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
