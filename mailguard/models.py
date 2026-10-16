from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from typing import Annotated

Verdict = Literal['risk', 'clear', 'review']

class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str
    quote: str = Field(min_length=1)

class Assessment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    verdict: Verdict
    risk_types: list[str]
    evidence: list[Evidence]
    explanation: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    limitations: list[str]

class SkillCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    skill_id: str
    outcome: Literal['finding', 'no_finding', 'insufficient']
    evidence: list[Evidence]
    note: str = Field(min_length=1)

class SkillAssessment(Assessment):
    skill_checks: list[SkillCheck]

class ReferencedSkillCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    skill_id: str
    outcome: Literal['finding', 'no_finding', 'insufficient']
    evidence_indices: list[Annotated[StrictInt, Field(ge=0)]]
    note: str = Field(min_length=1)

class ReferencedAssessment(Assessment):
    skill_checks: list[ReferencedSkillCheck]

class Mail(BaseModel):
    id: str
    content_hash: str
    sender: str
    reply_to: str
    recipients: list[str]
    subject: str
    text: str
    html_text: str
    hidden_text: str
    links: list[dict[str, str]]
    attachments: list[dict]
    warnings: list[str]

class InspectedMail(Mail):
    html_inspections: list[dict]

def load_mail(value):
    return (InspectedMail if 'html_inspections' in value else Mail).model_validate(value)

class AnalysisError(Exception):
    """A safe message for UI/logs; never includes a provider response or key."""
    def __init__(self, message, *, code='analysis_error', attempts=None):
        super().__init__(message)
        self.code = code
        self.attempts = attempts or []
