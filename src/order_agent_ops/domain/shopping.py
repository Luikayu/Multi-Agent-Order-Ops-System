"""Natural-language shopping intent and product candidate contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from order_agent_ops.domain.base import (
    DomainModel,
    IntentId,
    NonEmptyString,
    OrderId,
    TraceId,
)
from order_agent_ops.domain.enums import PurchaseIntentStatus


class AttributeRequirement(DomainModel):
    attribute: NonEmptyString
    acceptable_values: list[NonEmptyString] = Field(min_length=1)
    reason: NonEmptyString


class InferredRequirement(DomainModel):
    attribute: NonEmptyString
    value: NonEmptyString
    reason: NonEmptyString


class PurchaseIntentRequest(DomainModel):
    user_id: NonEmptyString
    message: NonEmptyString


class PurchaseClarificationRequest(DomainModel):
    message: NonEmptyString


class AgentPurchaseIntentAnalysis(DomainModel):
    """Small-model envelope used only to decide whether catalog search can start."""

    category: str | None
    quantity: int | None = Field(gt=0)
    max_price: Decimal | None = Field(ge=0)
    use_case: str | None
    constraint_conflicts: list[NonEmptyString]
    clarifying_question: str | None


class ParsedPurchaseIntent(DomainModel):
    # Every field is required in JSON Schema, even when its value is null or [].
    # This prevents small structured-output models from legally returning {}.
    category: str | None
    quantity: int | None = Field(gt=0)
    max_price: Decimal | None = Field(ge=0)
    use_case: str | None
    hard_constraints: list[AttributeRequirement]
    soft_preferences: list[AttributeRequirement]
    inferred_requirements: list[InferredRequirement]
    missing_information: list[NonEmptyString]
    constraint_conflicts: list[NonEmptyString]
    clarifying_question: str | None
    ready_to_search: bool

    @model_validator(mode="after")
    def readiness_matches_content(self) -> "ParsedPurchaseIntent":
        if self.ready_to_search:
            if self.category is None or self.quantity is None:
                raise ValueError("Ready intent requires category and quantity")
            if self.missing_information or self.constraint_conflicts:
                raise ValueError("Ready intent cannot contain missing data or conflicts")
            if self.clarifying_question is not None:
                raise ValueError("Ready intent cannot contain a clarifying question")
        elif not (
            self.missing_information
            or self.constraint_conflicts
            or self.clarifying_question
        ):
            raise ValueError("Non-ready intent must explain why clarification is needed")
        return self


class ProductCandidate(DomainModel):
    sku: NonEmptyString
    name: NonEmptyString
    category: NonEmptyString
    unit_price: Decimal = Field(ge=0)
    available_quantity: int = Field(ge=0)
    attributes: dict[str, Any]
    matched_preferences: list[NonEmptyString] = Field(default_factory=list)
    unmet_preferences: list[NonEmptyString] = Field(default_factory=list)
    score: int = Field(ge=0)


class ProductRecommendation(DomainModel):
    recommended_sku: NonEmptyString
    alternative_skus: list[NonEmptyString] = Field(default_factory=list)
    reason: NonEmptyString
    tradeoffs: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def recommendation_is_not_duplicated(self) -> "ProductRecommendation":
        if self.recommended_sku in self.alternative_skus:
            raise ValueError("Recommended SKU cannot also be an alternative")
        if len(set(self.alternative_skus)) != len(self.alternative_skus):
            raise ValueError("Alternative SKUs must be unique")
        return self


class AgentCatalogSelection(DomainModel):
    """Minimal model decision after the Agent reads the trusted catalog."""

    selected_skus: list[NonEmptyString] = Field(max_length=5)
    reason: NonEmptyString

    @model_validator(mode="after")
    def selection_is_internally_consistent(self) -> "AgentCatalogSelection":
        if len(set(self.selected_skus)) != len(self.selected_skus):
            raise ValueError("Selected catalog SKUs must be unique")
        return self


class GroundedCatalogSearchResult(DomainModel):
    """Agent selection hydrated with immutable facts from the catalog tool."""

    candidates: list[ProductCandidate] = Field(default_factory=list)
    recommendation: ProductRecommendation | None = None
    no_match_reason: str | None = None

    @model_validator(mode="after")
    def result_matches_candidate_presence(self) -> "GroundedCatalogSearchResult":
        if self.candidates:
            if self.recommendation is None:
                raise ValueError("Catalog candidates require a recommendation")
            if self.no_match_reason is not None:
                raise ValueError("Catalog candidates cannot have a no-match reason")
        elif self.recommendation is not None or not self.no_match_reason:
            raise ValueError("Empty catalog result requires a no-match reason only")
        return self


class PurchaseIntentRecord(DomainModel):
    intent_id: IntentId
    user_id: NonEmptyString
    messages: list[NonEmptyString] = Field(min_length=1)
    parsed_intent: ParsedPurchaseIntent
    candidates: list[ProductCandidate] = Field(default_factory=list)
    recommendation: ProductRecommendation | None = None
    status: PurchaseIntentStatus
    trace_id: TraceId
    model_provider: NonEmptyString
    model_name: NonEmptyString
    analyze_prompt_version: NonEmptyString
    catalog_search_prompt_version: NonEmptyString | None = None
    # Legacy field kept so previously persisted intent records remain readable.
    rank_prompt_version: NonEmptyString
    workflow_message: NonEmptyString
    created_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime
    confirmed_sku: str | None = None
    confirmed_quantity: int | None = Field(default=None, gt=0)
    confirmed_unit_price: Decimal | None = Field(default=None, ge=0)
    confirmation_idempotency_key: str | None = None
    order_id: OrderId | None = None

    @model_validator(mode="after")
    def confirmation_fields_match_status(self) -> "PurchaseIntentRecord":
        confirmation_fields = (
            self.confirmed_sku,
            self.confirmed_quantity,
            self.confirmed_unit_price,
            self.confirmation_idempotency_key,
            self.order_id,
        )
        if self.status is PurchaseIntentStatus.CONFIRMED:
            if any(value is None for value in confirmation_fields):
                raise ValueError("Confirmed intent requires complete confirmation fields")
        elif any(value is not None for value in confirmation_fields):
            raise ValueError("Unconfirmed intent cannot contain confirmation fields")
        if self.expires_at <= self.created_at:
            raise ValueError("Intent expiry must be after creation")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class PurchaseConfirmationRequest(DomainModel):
    sku: NonEmptyString
    quantity: int = Field(gt=0)
    idempotency_key: NonEmptyString
