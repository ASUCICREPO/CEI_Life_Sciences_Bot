from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from shared.schemas.cei_ui import (
    ActionDescriptor,
    CEIUISurface,
    ChoicePickerComponent,
    ChoiceOption,
    ColumnComponent,
    Plan,
    PlanStep,
    ReviewGateComponent,
    TextComponent,
    TextFieldComponent,
)


class AskQuestionInput(BaseModel):
    question: str
    options: list[str] = Field(min_length=2)

    @field_validator("options")
    @classmethod
    def validate_unique_options(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("options must be unique")
        return value


class AlternativeInput(BaseModel):
    title: str
    description: str


class PresentAlternativesInput(BaseModel):
    title: str
    alternatives: list[AlternativeInput] = Field(min_length=2)


class PlanStepInput(BaseModel):
    title: str
    description: str | None = None


class RequestPlanApprovalInput(BaseModel):
    plan_summary: str
    steps: list[PlanStepInput] = Field(min_length=1)


class GetUserInputPrompt(BaseModel):
    prompt: str
    placeholder: str = ""


def build_scope_confirmation_surface(
    *,
    surface_id: str,
    revision: int,
    analysis_type: str,
    competitors: list[str],
    market: str | None,
    jurisdiction: str | None,
    focal_entity: str | None,
) -> dict[str, object]:
    """A ReviewGate + free-text edit field for the Analysis scope-confirmation HITL point (B94/F14)."""
    scope_lines: list[str] = [f"Analysis type: {analysis_type}"]
    if focal_entity:
        scope_lines.append(f"Focal entity: {focal_entity}")
    if competitors:
        scope_lines.append(f"Competitors: {', '.join(competitors)}")
    if market:
        scope_lines.append(f"Market: {market}")
    if jurisdiction:
        scope_lines.append(f"Jurisdiction: {jurisdiction}")

    plan_steps = [
        PlanStep(step_id=f"scope-{index}", title=line)
        for index, line in enumerate(scope_lines, start=1)
    ]

    surface = CEIUISurface(
        surface_id=surface_id,
        revision=revision,
        root_id="scope-root",
        components=[
            ColumnComponent(id="scope-root", children=["scope-intro", "scope-review", "scope-edit"]),
            TextComponent(
                id="scope-intro",
                content="Confirm the analysis scope before gathering evidence, or edit it below.",
            ),
            ReviewGateComponent(
                id="scope-review",
                interaction_id="analysis-scope-review",
                plan=Plan(summary=f"Proposed {analysis_type} analysis scope", steps=plan_steps),
                actions=[
                    ActionDescriptor(action_id="approve-scope", label="Approve", intent="approve"),
                    ActionDescriptor(action_id="reject-scope", label="Reject", intent="reject"),
                ],
            ),
            TextFieldComponent(
                id="scope-edit",
                interaction_id="analysis-scope-edit",
                label="Edit the scope (free text)",
                placeholder="For example: add Pfizer, focus on EU market",
                multiline=True,
                action=ActionDescriptor(action_id="edit-scope", label="Submit edit", intent="submit"),
            ),
        ],
    )
    return surface.model_dump(mode="json")


class InlineFunctionRegistry:
    def _ask_user_question(self, question: str, options: list[str]) -> dict[str, object]:
        surface = CEIUISurface(
            surface_id="hitl-ask-user-question",
            root_id="root",
            components=[
                ColumnComponent(id="root", children=["question", "answer"]),
                TextComponent(id="question", content=question),
                ChoicePickerComponent(
                    id="answer",
                    interaction_id="ask-user-question",
                    label=question,
                    options=[ChoiceOption(label=option, value=option) for option in options],
                    action=ActionDescriptor(action_id="submit-answer", label="Submit"),
                ),
            ],
        )
        return surface.model_dump(mode="json")

    def _present_alternatives(
        self, title: str, alternatives: list[AlternativeInput]
    ) -> dict[str, object]:
        surface = CEIUISurface(
            surface_id="hitl-present-alternatives",
            root_id="root",
            components=[
                ColumnComponent(id="root", children=["title", "alternatives"]),
                TextComponent(id="title", content=title, variant="h2"),
                ChoicePickerComponent(
                    id="alternatives",
                    interaction_id="present-alternatives",
                    label=title,
                    options=[
                        ChoiceOption(
                            label=alternative.title,
                            value=f"alternative-{index}",
                            description=alternative.description,
                        )
                        for index, alternative in enumerate(alternatives, start=1)
                    ],
                    action=ActionDescriptor(action_id="select-alternative", label="Select"),
                ),
            ],
        )
        return surface.model_dump(mode="json")

    def _request_plan_approval(
        self, plan_summary: str, steps: list[PlanStepInput]
    ) -> dict[str, object]:
        surface = CEIUISurface(
            surface_id="hitl-plan-approval",
            root_id="plan-review",
            components=[
                ReviewGateComponent(
                    id="plan-review",
                    interaction_id="plan-approval",
                    plan=Plan(
                        summary=plan_summary,
                        steps=[
                            PlanStep(
                                step_id=f"step-{index}",
                                title=step.title,
                                description=step.description,
                            )
                            for index, step in enumerate(steps, start=1)
                        ],
                    ),
                    actions=[
                        ActionDescriptor(action_id="approve-plan", label="Approve", intent="approve"),
                        ActionDescriptor(action_id="reject-plan", label="Reject", intent="reject"),
                    ],
                )
            ],
        )
        return surface.model_dump(mode="json")

    def _get_user_input(self, prompt: str, placeholder: str = "") -> dict[str, object]:
        surface = CEIUISurface(
            surface_id="hitl-get-user-input",
            root_id="user-input",
            components=[
                TextFieldComponent(
                    id="user-input",
                    interaction_id="get-user-input",
                    label=prompt,
                    placeholder=placeholder,
                    action=ActionDescriptor(action_id="submit-input", label="Submit"),
                )
            ],
        )
        return surface.model_dump(mode="json")

    def as_langchain_tools(self) -> list[StructuredTool]:
        return [
            StructuredTool.from_function(
                func=self._ask_user_question,
                name="ask_user_question",
                description="Present a question with selectable options to the user.",
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
