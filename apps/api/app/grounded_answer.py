"""Validate provider claims before rendering the platform's citation syntax."""
import json
import re

from pydantic import BaseModel, Field, ValidationError

INSUFFICIENT_ANSWER = "หลักฐานที่ได้รับยังไม่เพียงพอสำหรับตอบคำถามครบทุกส่วน"

class GroundedClaim(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    source_ids: list[str] = Field(min_length=1, max_length=12)


class GroundedAnswer(BaseModel):
    claims: list[GroundedClaim] = Field(max_length=40)
    insufficient_evidence: bool


def render_grounded_answer(content: str, allowed_ids: set[str]) -> str:
    value = content.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        # Some compatible servers ignore JSON mode. Cited prose still passes
        # the same ID validation; uncited prose never receives invented IDs.
        cited = set(re.findall(r"\[(S\d+)\]", value))
        structured = value.startswith("{") or (value.startswith("[") and not re.match(r"\[S\d+\]", value))
        if not structured and cited and cited <= allowed_ids:
            return value
        raise RuntimeError("LLM_ANSWER_CITATIONS_INVALID") from None
    try:
        answer = GroundedAnswer.model_validate(parsed)
    except ValidationError as exc:
        raise RuntimeError("LLM_ANSWER_INVALID_RESPONSE") from exc
    lines = []
    for claim in answer.claims:
        ids = set(claim.source_ids)
        embedded = set(re.findall(r"\[(S\d+)\]", claim.text))
        if not ids <= allowed_ids or not embedded <= ids or not claim.text.strip():
            raise RuntimeError("LLM_ANSWER_CITATIONS_INVALID")
        prose = re.sub(r"\[S\d+\]", "", claim.text).strip()
        if not prose:
            raise RuntimeError("LLM_ANSWER_INVALID_RESPONSE")
        citations = "".join(f"[{identifier}]" for identifier in dict.fromkeys(claim.source_ids))
        lines.append(f"{prose} {citations}")
    if not lines and not answer.insufficient_evidence:
        raise RuntimeError("LLM_ANSWER_INVALID_RESPONSE")
    if answer.insufficient_evidence:
        lines.append(INSUFFICIENT_ANSWER)
    return "\n\n".join(lines)
