from pydantic import BaseModel, ConfigDict, Field


class RepoArchitectOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_summary: str = Field(min_length=1, max_length=3000)
    root_cause_hypotheses: list[str] = Field(min_length=1, max_length=8)
    supporting_evidence: list[str] = Field(max_length=20)
    relevant_files: list[str] = Field(max_length=30)
    relevant_symbols: list[str] = Field(max_length=40)
    recommended_patch_scope: list[str] = Field(min_length=1, max_length=20)
    tests_to_run: list[str] = Field(max_length=30)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_assessment: str = Field(min_length=1, max_length=2000)


class CoderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause: str = Field(min_length=1, max_length=3000)
    explanation: str = Field(min_length=1, max_length=3000)
    unified_diff: str = Field(min_length=1, max_length=1_000_000)
    regression_test_plan: list[str] = Field(max_length=30)
    tests_to_run: list[str] = Field(max_length=30)
    risk_notes: list[str] = Field(max_length=20)
