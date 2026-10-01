"""Natural-language shopping, catalog grounding, and explicit confirmation."""

from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4

from order_agent_ops.agents.shopping_assistant import (
    InvalidCandidateReferenceError,
    ShoppingAssistantAgent,
)
from order_agent_ops.business.product_search_adapter import ProductSearchAdapter
from order_agent_ops.domain.base import DomainModel
from order_agent_ops.domain.enums import PurchaseIntentStatus
from order_agent_ops.domain.orders import OrderItem, OrderRecord, OrderRequest
from order_agent_ops.domain.shopping import (
    ProductCandidate,
    PurchaseConfirmationRequest,
    PurchaseIntentRecord,
    PurchaseIntentRequest,
)
from order_agent_ops.models.errors import ModelGatewayError
from order_agent_ops.services.order_workflow import (
    OrderWorkflow,
    OrderWorkflowConflictError,
    OrderWorkflowUnavailableError,
)
from order_agent_ops.storage.repositories import (
    OrderRepository,
    PurchaseIntentRepository,
)
from order_agent_ops.telemetry.tracing import TraceRecorder


class ShoppingWorkflowError(RuntimeError):
    error_code = "SHOPPING_WORKFLOW_ERROR"


class PurchaseIntentNotFoundError(ShoppingWorkflowError):
    error_code = "PURCHASE_INTENT_NOT_FOUND"


class PurchaseIntentNotReadyError(ShoppingWorkflowError):
    error_code = "PURCHASE_INTENT_NOT_READY"


class PurchaseIntentExpiredError(ShoppingWorkflowError):
    error_code = "PURCHASE_INTENT_EXPIRED"


class PurchaseConfirmationConflictError(ShoppingWorkflowError):
    error_code = "PURCHASE_CONFIRMATION_CONFLICT"


class InvalidProductCandidateError(ShoppingWorkflowError):
    error_code = "INVALID_PRODUCT_CANDIDATE"


class ProductChangedError(ShoppingWorkflowError):
    error_code = "PRODUCT_CHANGED"


class ShoppingModelOutputError(ShoppingWorkflowError):
    error_code = "SHOPPING_MODEL_OUTPUT_INVALID"


class ShoppingOrderSubmissionError(ShoppingWorkflowError):
    error_code = "SHOPPING_ORDER_SUBMISSION_FAILED"


class PurchaseConfirmationResult(DomainModel):
    intent: PurchaseIntentRecord
    order: OrderRecord
    idempotent_replay: bool


class ShoppingWorkflow:
    """Let the Agent search trusted catalog facts before explicit confirmation."""

    def __init__(
        self,
        assistant: ShoppingAssistantAgent,
        product_search: ProductSearchAdapter,
        intent_repository: PurchaseIntentRepository,
        order_repository: OrderRepository,
        order_workflow: OrderWorkflow,
        trace_recorder: TraceRecorder,
        *,
        intent_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        if intent_ttl.total_seconds() <= 0:
            raise ValueError("Purchase intent TTL must be positive")
        self._assistant = assistant
        self._product_search = product_search
        self._intents = intent_repository
        self._orders = order_repository
        self._order_workflow = order_workflow
        self.trace_recorder = trace_recorder
        self._intent_ttl = intent_ttl
        self._confirmation_lock = RLock()

    def create_intent(self, request: PurchaseIntentRequest) -> PurchaseIntentRecord:
        now = datetime.now(timezone.utc)
        trace_id = self.trace_recorder.new_trace_id()
        with self.trace_recorder.span(
            trace_id, "shopping-workflow", "shopping.intent"
        ):
            parsed, candidates, recommendation, status, message = self._analyze_and_search(
                [request.message], trace_id=trace_id
            )
        record = PurchaseIntentRecord(
            intent_id=f"INTENT-{uuid4().hex}",
            user_id=request.user_id,
            messages=[request.message],
            parsed_intent=parsed,
            candidates=candidates,
            recommendation=recommendation,
            status=status,
            trace_id=trace_id,
            model_provider=self._assistant.model_gateway.provider.provider_name,
            model_name=self._assistant.model_gateway.provider.model_name,
            analyze_prompt_version=self._assistant.analyze_prompt_version,
            catalog_search_prompt_version=(
                self._assistant.catalog_search_prompt_version
            ),
            # Keep the persisted/API field name for backward compatibility. It now
            # records the combined Agent catalog-search and ranking prompt.
            rank_prompt_version=self._assistant.catalog_search_prompt_version,
            workflow_message=message,
            created_at=now,
            updated_at=now,
            expires_at=now + self._intent_ttl,
        )
        return self._intents.create(record)

    def get_intent(self, intent_id: str) -> PurchaseIntentRecord:
        record = self._require_intent(intent_id)
        return self._expire_if_needed(record)

    def add_message(self, intent_id: str, message: str) -> PurchaseIntentRecord:
        record = self._expire_if_needed(self._require_intent(intent_id))
        if record.status is PurchaseIntentStatus.EXPIRED:
            raise PurchaseIntentExpiredError(f"Purchase intent expired: {intent_id}")
        if record.status is PurchaseIntentStatus.CONFIRMED:
            raise PurchaseConfirmationConflictError(
                "Confirmed purchase intent cannot accept more clarification"
            )

        messages = [*record.messages, message]
        with self.trace_recorder.span(
            record.trace_id, "shopping-workflow", "shopping.clarify"
        ):
            parsed, candidates, recommendation, status, workflow_message = (
                self._analyze_and_search(messages, trace_id=record.trace_id)
            )
        updated = record.model_copy(
            update={
                "messages": messages,
                "parsed_intent": parsed,
                "candidates": candidates,
                "recommendation": recommendation,
                "status": status,
                "workflow_message": workflow_message,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self._intents.update(updated)

    def confirm(
        self,
        intent_id: str,
        request: PurchaseConfirmationRequest,
    ) -> PurchaseConfirmationResult:
        with self._confirmation_lock:
            return self._confirm_locked(intent_id, request)

    def _confirm_locked(
        self,
        intent_id: str,
        request: PurchaseConfirmationRequest,
    ) -> PurchaseConfirmationResult:
        record = self._expire_if_needed(self._require_intent(intent_id))
        if record.status is PurchaseIntentStatus.EXPIRED:
            raise PurchaseIntentExpiredError(f"Purchase intent expired: {intent_id}")
        if record.status is PurchaseIntentStatus.CONFIRMED:
            return self._replay_confirmation(record, request)
        if record.status is not PurchaseIntentStatus.READY_FOR_CONFIRMATION:
            raise PurchaseIntentNotReadyError(
                "Purchase intent still requires clarification or has no candidates"
            )

        candidate = next(
            (item for item in record.candidates if item.sku == request.sku),
            None,
        )
        if candidate is None:
            raise InvalidProductCandidateError(
                f"SKU is not part of the saved candidate set: {request.sku}"
            )
        current = self._product_search.get_product(request.sku)
        if current is None or not current.active:
            raise ProductChangedError("Selected product is missing or inactive")
        if current.unit_price != candidate.unit_price:
            raise ProductChangedError("Selected product price changed; refresh candidates")
        if current.available_quantity < request.quantity:
            raise ProductChangedError("Selected product catalog inventory changed")

        order_request = OrderRequest(
            user_id=record.user_id,
            items=[
                OrderItem(
                    sku=current.sku,
                    quantity=request.quantity,
                    unit_price=current.unit_price,
                )
            ],
            total_amount=current.unit_price * request.quantity,
            idempotency_key=request.idempotency_key,
        )
        with self.trace_recorder.span(
            record.trace_id, "shopping-workflow", "shopping.confirm"
        ):
            try:
                order_result = self._order_workflow.submit(
                    order_request,
                    trace_id=record.trace_id,
                )
            except OrderWorkflowConflictError as error:
                raise PurchaseConfirmationConflictError(str(error)) from error
            except OrderWorkflowUnavailableError as error:
                raise ShoppingOrderSubmissionError(str(error)) from error

        updated = record.model_copy(
            update={
                "status": PurchaseIntentStatus.CONFIRMED,
                "workflow_message": "Candidate confirmed and submitted to order workflow",
                "confirmed_sku": current.sku,
                "confirmed_quantity": request.quantity,
                "confirmed_unit_price": current.unit_price,
                "confirmation_idempotency_key": request.idempotency_key,
                "order_id": order_result.order.order_id,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        updated = PurchaseIntentRecord.model_validate(updated.model_dump())
        self._intents.update(updated)
        return PurchaseConfirmationResult(
            intent=updated,
            order=order_result.order,
            idempotent_replay=order_result.idempotent_replay,
        )

    def _analyze_and_search(self, messages: list[str], *, trace_id: str):
        try:
            parsed = self._assistant.analyze(messages, trace_id=trace_id)
            if not parsed.ready_to_search:
                return (
                    parsed,
                    [],
                    None,
                    PurchaseIntentStatus.NEEDS_CLARIFICATION,
                    parsed.clarifying_question or "Purchase intent needs clarification",
                )

            catalog_result = self._assistant.search_catalog(
                messages,
                parsed,
                trace_id=trace_id,
            )
            if not catalog_result.candidates:
                return (
                    parsed,
                    [],
                    None,
                    PurchaseIntentStatus.NEEDS_CLARIFICATION,
                    catalog_result.no_match_reason
                    or "No catalog product satisfies all hard constraints; revise one condition",
                )
            candidates = catalog_result.candidates
            recommendation = catalog_result.recommendation
        except (InvalidCandidateReferenceError, ModelGatewayError) as error:
            raise ShoppingModelOutputError(str(error)) from error

        return (
            parsed,
            candidates,
            recommendation,
            PurchaseIntentStatus.READY_FOR_CONFIRMATION,
            "Candidates are ready for explicit user confirmation",
        )

    def _require_intent(self, intent_id: str) -> PurchaseIntentRecord:
        record = self._intents.get(intent_id)
        if record is None:
            raise PurchaseIntentNotFoundError(f"Purchase intent not found: {intent_id}")
        return record

    def _expire_if_needed(self, record: PurchaseIntentRecord) -> PurchaseIntentRecord:
        now = datetime.now(timezone.utc)
        if (
            record.status
            not in {PurchaseIntentStatus.CONFIRMED, PurchaseIntentStatus.EXPIRED}
            and record.is_expired(now)
        ):
            record = record.model_copy(
                update={
                    "status": PurchaseIntentStatus.EXPIRED,
                    "workflow_message": "Purchase intent expired before confirmation",
                    "updated_at": now,
                }
            )
            self._intents.update(record)
        return record

    def _replay_confirmation(
        self,
        record: PurchaseIntentRecord,
        request: PurchaseConfirmationRequest,
    ) -> PurchaseConfirmationResult:
        if (
            record.confirmed_sku != request.sku
            or record.confirmed_quantity != request.quantity
            or record.confirmation_idempotency_key != request.idempotency_key
        ):
            raise PurchaseConfirmationConflictError(
                "Confirmed intent cannot be changed or reused with another idempotency key"
            )
        order = self._orders.get(record.order_id or "")
        if order is None:
            raise PurchaseConfirmationConflictError(
                "Confirmed intent references a missing order"
            )
        return PurchaseConfirmationResult(
            intent=record,
            order=order,
            idempotent_replay=True,
        )
