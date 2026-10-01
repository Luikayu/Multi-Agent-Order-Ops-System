"""Order submission and lookup HTTP endpoints."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from order_agent_ops.domain.base import DomainModel, NonEmptyString, TraceId
from order_agent_ops.domain.enums import OrderStatus
from order_agent_ops.domain.orders import OrderRecord, OrderRequest
from order_agent_ops.services.order_workflow import (
    OrderWorkflow,
    OrderWorkflowConflictError,
    OrderWorkflowUnavailableError,
)
from order_agent_ops.storage.repositories import OrderRepository


class OrderSubmissionResponse(DomainModel):
    request_id: NonEmptyString
    order_id: NonEmptyString
    trace_id: TraceId
    status: OrderStatus
    idempotent_replay: bool


class ErrorResponse(DomainModel):
    error_code: NonEmptyString
    message: NonEmptyString
    trace_id: TraceId | None = None


def _error_response(
    status_code: int,
    *,
    error_code: str,
    message: str,
    trace_id: str | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error_code=error_code,
        message=message,
        trace_id=trace_id,
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def create_order_router(
    workflow: OrderWorkflow,
    order_repository: OrderRepository,
) -> APIRouter:
    router = APIRouter(prefix="/orders", tags=["orders"])

    @router.post(
        "",
        response_model=OrderSubmissionResponse,
        responses={
            409: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    def submit_order(
        order_request: OrderRequest,
    ) -> OrderSubmissionResponse | JSONResponse:
        try:
            result = workflow.submit(order_request)
        except OrderWorkflowConflictError as error:
            return _error_response(
                409,
                error_code=error.error_code,
                message=str(error),
                trace_id=error.trace_id,
            )
        except OrderWorkflowUnavailableError as error:
            return _error_response(
                503,
                error_code=error.error_code,
                message=str(error),
                trace_id=error.trace_id,
            )

        order = result.order
        return OrderSubmissionResponse(
            request_id=order.request_id,
            order_id=order.order_id,
            trace_id=order.trace_id,
            status=order.status,
            idempotent_replay=result.idempotent_replay,
        )

    @router.get(
        "/{order_id}",
        response_model=OrderRecord,
        responses={404: {"model": ErrorResponse}},
    )
    def get_order(order_id: str) -> OrderRecord | JSONResponse:
        order = order_repository.get(order_id)
        if order is None:
            return _error_response(
                404,
                error_code="ORDER_NOT_FOUND",
                message=f"Order not found: {order_id}",
            )
        return order

    return router
