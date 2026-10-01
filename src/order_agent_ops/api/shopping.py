"""Natural-language shopping intent, clarification, and confirmation endpoints."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from order_agent_ops.api.orders import ErrorResponse
from order_agent_ops.domain.base import DomainModel, IntentId, TraceId
from order_agent_ops.domain.orders import OrderRecord
from order_agent_ops.domain.shopping import (
    PurchaseClarificationRequest,
    PurchaseConfirmationRequest,
    PurchaseIntentRecord,
    PurchaseIntentRequest,
)
from order_agent_ops.services.shopping_workflow import (
    InvalidProductCandidateError,
    ProductChangedError,
    PurchaseConfirmationConflictError,
    PurchaseIntentExpiredError,
    PurchaseIntentNotFoundError,
    PurchaseIntentNotReadyError,
    ShoppingModelOutputError,
    ShoppingOrderSubmissionError,
    ShoppingWorkflow,
    ShoppingWorkflowError,
)


class PurchaseConfirmationResponse(DomainModel):
    intent_id: IntentId
    trace_id: TraceId
    order: OrderRecord
    idempotent_replay: bool


def _shopping_error(error: ShoppingWorkflowError) -> JSONResponse:
    if isinstance(error, PurchaseIntentNotFoundError):
        status_code = 404
    elif isinstance(error, (ShoppingModelOutputError, ShoppingOrderSubmissionError)):
        status_code = 502
    elif isinstance(
        error,
        (
            PurchaseIntentNotReadyError,
            PurchaseIntentExpiredError,
            PurchaseConfirmationConflictError,
            InvalidProductCandidateError,
            ProductChangedError,
        ),
    ):
        status_code = 409
    else:  # pragma: no cover - all current public errors are mapped above
        status_code = 400
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error_code=error.error_code,
            message=str(error),
        ).model_dump(mode="json"),
    )


def create_shopping_router(workflow: ShoppingWorkflow) -> APIRouter:
    router = APIRouter(prefix="/shopping/intents", tags=["shopping"])

    @router.post("", response_model=PurchaseIntentRecord)
    def create_intent(
        request: PurchaseIntentRequest,
    ) -> PurchaseIntentRecord | JSONResponse:
        try:
            return workflow.create_intent(request)
        except ShoppingWorkflowError as error:
            return _shopping_error(error)

    @router.get("/{intent_id}", response_model=PurchaseIntentRecord)
    def get_intent(intent_id: str) -> PurchaseIntentRecord | JSONResponse:
        try:
            return workflow.get_intent(intent_id)
        except ShoppingWorkflowError as error:
            return _shopping_error(error)

    @router.post("/{intent_id}/messages", response_model=PurchaseIntentRecord)
    def add_message(
        intent_id: str,
        request: PurchaseClarificationRequest,
    ) -> PurchaseIntentRecord | JSONResponse:
        try:
            return workflow.add_message(intent_id, request.message)
        except ShoppingWorkflowError as error:
            return _shopping_error(error)

    @router.post(
        "/{intent_id}/confirm",
        response_model=PurchaseConfirmationResponse,
    )
    def confirm(
        intent_id: str,
        request: PurchaseConfirmationRequest,
    ) -> PurchaseConfirmationResponse | JSONResponse:
        try:
            result = workflow.confirm(intent_id, request)
        except ShoppingWorkflowError as error:
            return _shopping_error(error)
        return PurchaseConfirmationResponse(
            intent_id=result.intent.intent_id,
            trace_id=result.intent.trace_id,
            order=result.order,
            idempotent_replay=result.idempotent_replay,
        )

    return router
