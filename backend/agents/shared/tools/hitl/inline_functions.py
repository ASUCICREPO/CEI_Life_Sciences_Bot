from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from shared.schemas.a2ui import (
    CardComponent,
    ChoicePickerComponent,
    ReviewGateComponent,
    TextFieldComponent,
)


class AskQuestionInput(BaseModel):
    question: str
    options: list[str] = Field(min_length=2)


class PresentAlternativesInput(BaseModel):
    title: str
    alternatives: list[dict[str, str]]


class RequestPlanApprovalInput(BaseModel):
    plan_summary: str
    steps: list[dict[str, Any]]


class GetUserInputPrompt(BaseModel):
    prompt: str
    placeholder: str = ""


class InlineFunctionRegistry:
    def _ask_user_question(self, question: str, options: list[str]) -> dict[str, Any]:
        component = ChoicePickerComponent(
            label=question,
            options=[{"label": o, "value": o} for o in options],
        )
        return {"type": "hitl_request", "component": component.to_dict()}

    def _present_alternatives(self, title: str, alternatives: list[dict[str, str]]) -> dict[str, Any]:
        cards = [CardComponent(title=a["title"], body=a["description"]) for a in alternatives]
        return {"type": "hitl_request", "components": [c.to_dict() for c in cards]}

    def _request_plan_approval(self, plan_summary: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        gate = ReviewGateComponent(plan={"summary": plan_summary, "steps": steps})
        return {"type": "hitl_request", "component": gate.to_dict()}

    def _get_user_input(self, prompt: str, placeholder: str = "") -> dict[str, Any]:
        field = TextFieldComponent(label=prompt, placeholder=placeholder)
        return {"type": "hitl_request", "component": field.to_dict()}

    def as_langchain_tools(self) -> list[StructuredTool]:
        return [
            StructuredTool.from_function(
                func=self._ask_user_question,
                name="ask_user_question",
                description="Present a question with selectable options to the user. Pauses execution until answered.",
                args_schema=AskQuestionInput,
            ),
            StructuredTool.from_function(
                func=self._present_alternatives,
                name="present_alternatives",
                description="Show multiple alternatives with descriptions for user selection.",
                args_schema=PresentAlternativesInput,
            ),
            StructuredTool.from_function(
                func=self._request_plan_approval,
                name="request_plan_approval",
                description="Present a pipeline execution plan for user review and approval.",
                args_schema=RequestPlanApprovalInput,
            ),
            StructuredTool.from_function(
                func=self._get_user_input,
                name="get_user_input",
                description="Request free-text input from the user.",
                args_schema=GetUserInputPrompt,
            ),
        ]
