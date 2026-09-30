"""Argument models shared by the product tools of ``vkm-ansys`` (Mechanical, Workbench, optiSLang)."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Ref = Annotated[str, Field(min_length=1, max_length=1000,
                           description="absolute path of an existing file, or job:<job_id>/<path> inside an earlier "
                                       "job, e.g. job:MECH-20260928T120000Z-1a2b3c4d/out/model.mechdb")]
RefList = Annotated[list[Ref] | None, Field(max_length=50, description="extra input files or folders, copied to "
                                                                        "in/inputs/<name> (SHA-256 in the receipt)")]
ScriptText = Annotated[str | None, Field(max_length=1_000_000, description="inline script text (UTF-8)")]
WaitS = Annotated[int, Field(ge=0, le=600, description="wait this long for the job before returning (0 = return the "
                                                       "job id at once; follow with job_wait)")]
Label = Annotated[str | None, Field(max_length=200, description="free-text label shown in job_list and the receipt")]
DryRun = Annotated[bool, Field(description="return what would be staged and run, create no job")]
LicenseProbe = Annotated[bool, Field(description="record the licence features in use during the run (read-only "
                                                 "lmstat; adds a few seconds)")]


class CheckIn(BaseModel):
    """Expected-result check evaluated by the job runner after the product exits (plan §2.3)."""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,80}$")]
    kind: Literal["file_exists", "json_value", "number_close", "text_contains", "text_absent", "exit_code"]
    path: Annotated[str | None, Field(max_length=240, description="relative to the job directory, e.g. "
                                                                  "out/result.json")] = None
    pointer: Annotated[str | None, Field(max_length=200, description="JSON Pointer, e.g. /uz_top_m")] = None
    expected: Any = None
    rtol: Annotated[float | None, Field(ge=0)] = None
    atol: Annotated[float | None, Field(ge=0)] = None


class ParamIn(BaseModel):
    """A numeric parameter recorded in the receipt: value with an epistemic status and a source reference."""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
    value: float | None = None
    unit: Annotated[str | None, Field(max_length=40)] = None
    status: Literal["FACT", "DERIVATION", "INTERPOLATION", "MODEL_CHOICE", "ENGINEERING_ASSUMPTION", "ANALOGUE",
                    "UNKNOWN"]
    source_ref: Annotated[str | None, Field(max_length=300, description="source locator or the rationale of the "
                                                                        "assumption; 'TOY' for tool tests")] = None
    scope: Annotated[str | None, Field(max_length=60)] = None


Checks = Annotated[list[CheckIn] | None, Field(max_length=50)]
Params = Annotated[list[ParamIn] | None, Field(max_length=200)]
ModelChoices = Annotated[list[Annotated[str, Field(max_length=300)]] | None, Field(max_length=100)]


def dump(items: list[BaseModel] | None) -> list[dict[str, Any]]:
    return [i.model_dump(exclude_none=True) if isinstance(i, BaseModel) else dict(i) for i in items or []]
