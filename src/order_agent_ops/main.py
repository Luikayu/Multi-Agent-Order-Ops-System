"""FastAPI application entry point and dependency composition."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from order_agent_ops.agents import (
    InventoryAgent,
    OpsGuardianAgent,
    OrderCoordinatorAgent,
    RiskAgent,
    ShoppingAssistantAgent,
)
from order_agent_ops.api.orders import ErrorResponse, create_order_router
from order_agent_ops.api.dashboard import create_dashboard_router
from order_agent_ops.api.ops import OpsApiDependencies, create_ops_router
from order_agent_ops.api.shopping import create_shopping_router
from order_agent_ops.business.inventory_adapter import InventoryAdapter
from order_agent_ops.business.order_executor import OrderExecutorService
from order_agent_ops.business.policy_engine import OrderPolicyEngine
from order_agent_ops.business.product_search_adapter import ProductSearchAdapter
from order_agent_ops.business.replay_service import ReplayService
from order_agent_ops.business.risk_profile_adapter import RiskProfileAdapter
from order_agent_ops.config import PROJECT_ROOT, load_settings
from order_agent_ops.faults import (
    FaultController,
    FaultInjectingInventoryAdapter,
    FaultInjectingModelProvider,
    FaultInjectingRiskProfileAdapter,
    FaultTarget,
)
from order_agent_ops.models.gateway import ModelGateway
from order_agent_ops.models.factory import build_model_provider
from order_agent_ops.models.provider import ModelProvider
from order_agent_ops.ops.detector import AnomalyDetector
from order_agent_ops.ops.evidence_store import EvidenceStore
from order_agent_ops.ops.evidence_validator import EvidenceValidator
from order_agent_ops.ops.approval_service import ApprovalService
from order_agent_ops.ops.remediation_executor import RemediationExecutor
from order_agent_ops.ops.verification_service import (
    VerificationProbeResult,
    VerificationService,
)
from order_agent_ops.services.incident_workflow import IncidentWorkflow
from order_agent_ops.services.order_workflow import OrderWorkflow
from order_agent_ops.services.shopping_workflow import ShoppingWorkflow
from order_agent_ops.storage.database import Database
from order_agent_ops.storage.repositories import (
    ApprovalRepository,
    AuditRepository,
    InventoryRepository,
    IncidentRepository,
    OrderRepository,
    PurchaseIntentRepository,
    RemediationActionRepository,
)
from order_agent_ops.telemetry.metrics import MetricRegistry
from order_agent_ops.telemetry.log_store import OperationalLogStore
from order_agent_ops.telemetry.tracing import TraceRecorder
from order_agent_ops.tools.query_observability import (
    LocalObservabilitySource,
    QueryObservabilityTool,
)
from order_agent_ops.tools.query_service_context import (
    LocalServiceContextSource,
    QueryServiceContextTool,
)
from order_agent_ops.tools.execute_remediation import ExecuteRemediationTool
from order_agent_ops.tools.product_catalog import ProductCatalogTool


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "demo.sqlite3"


def _seed_missing_inventory(
    inventory_repository: InventoryRepository,
    inventory_adapter: InventoryAdapter,
) -> None:
    """Insert missing demo SKUs without resetting quantities consumed by orders."""

    for product in inventory_adapter.list_products():
        if inventory_repository.get(product.sku) is None:
            inventory_repository.set_quantity(product.sku, product.available_quantity)


def create_app(
    *,
    database_path: Path | None = None,
    provider: ModelProvider | None = None,
    inventory_adapter: InventoryAdapter | None = None,
    risk_profile_adapter: RiskProfileAdapter | None = None,
    fault_controller: FaultController | None = None,
) -> FastAPI:
    settings = load_settings()
    metrics = MetricRegistry()
    selected_provider = provider
    if selected_provider is None:
        selected_provider = build_model_provider(
            settings.model,
            metrics=metrics,
        )

    database = Database(database_path or DEFAULT_DATABASE_PATH)
    database.initialize()
    orders = OrderRepository(database)
    incidents = IncidentRepository(database)
    approvals = ApprovalRepository(database)
    remediation_actions = RemediationActionRepository(database)
    audit = AuditRepository(database)
    inventory = InventoryRepository(database)
    purchase_intents = PurchaseIntentRepository(database)
    evidence_store = EvidenceStore(database)
    inventory_facts = inventory_adapter or InventoryAdapter()
    risk_facts = risk_profile_adapter or RiskProfileAdapter()
    _seed_missing_inventory(inventory, inventory_facts)
    faults = fault_controller or FaultController.from_yaml()
    fault_aware_provider = FaultInjectingModelProvider(selected_provider, faults)
    fault_aware_inventory = FaultInjectingInventoryAdapter(inventory_facts, faults)
    fault_aware_risk = FaultInjectingRiskProfileAdapter(risk_facts, faults)

    def component_version(component: str) -> str | None:
        if component == InventoryAgent.name:
            scenario = faults.active_scenario(FaultTarget.INVENTORY_AGENT)
            if scenario is None:
                scenario = faults.active_scenario(FaultTarget.INVENTORY_ADAPTER)
            return scenario.component_version if scenario else InventoryAgent.version
        if component == RiskAgent.name:
            return RiskAgent.version
        if component == OrderCoordinatorAgent.name:
            return OrderCoordinatorAgent.version
        if component == ShoppingAssistantAgent.name:
            return ShoppingAssistantAgent.version
        if component == ProductCatalogTool.name:
            return ProductCatalogTool.version
        if component == OpsGuardianAgent.name:
            return OpsGuardianAgent.version
        return settings.app.version

    log_store = OperationalLogStore()
    trace_recorder = TraceRecorder(
        metrics=metrics,
        log_store=log_store,
        component_version_resolver=component_version,
    )
    gateway = ModelGateway(
        fault_aware_provider,
        timeout_seconds=settings.model.timeout_seconds,
        trace_recorder=trace_recorder,
        metrics=metrics,
    )
    inventory_agent = InventoryAgent(gateway, fault_aware_inventory)
    risk_agent = RiskAgent(gateway, fault_aware_risk)
    coordinator = OrderCoordinatorAgent(
        gateway,
        inventory_agent,
        risk_agent,
    )
    executor = OrderExecutorService(database, orders, inventory)
    policy_engine = OrderPolicyEngine()
    workflow = OrderWorkflow(
        database,
        orders,
        coordinator,
        policy_engine,
        executor,
        trace_recorder,
    )
    product_catalog_tool = ProductCatalogTool(inventory_facts)
    shopping_assistant = ShoppingAssistantAgent(gateway, product_catalog_tool)
    shopping_workflow = ShoppingWorkflow(
        shopping_assistant,
        ProductSearchAdapter(inventory_facts),
        purchase_intents,
        orders,
        workflow,
        trace_recorder,
    )
    query_observability = QueryObservabilityTool(
        LocalObservabilitySource(trace_recorder, log_store),
        evidence_store,
    )
    query_service_context = QueryServiceContextTool(
        LocalServiceContextSource(),
        evidence_store,
    )
    ops_guardian = OpsGuardianAgent(gateway)
    evidence_validator = EvidenceValidator(evidence_store)
    incident_workflow = IncidentWorkflow(
        incidents,
        evidence_store,
        ops_guardian,
        evidence_validator,
        query_observability,
        query_service_context,
        trace_recorder,
    )
    approval_service = ApprovalService(approvals, evidence_store, audit)
    remediation_executor = RemediationExecutor(
        remediation_actions,
        incidents,
        approval_service,
        faults,
        audit,
    )
    execute_remediation = ExecuteRemediationTool(remediation_executor)

    def verification_probe(trace_id: str) -> VerificationProbeResult:
        """Run a non-persisting test order and derive fresh component latency."""

        from order_agent_ops.domain.orders import OrderItem, OrderRequest

        request = OrderRequest(
            user_id="USER-001",
            items=[OrderItem(sku="SKU-001", quantity=1, unit_price="399.00")],
            total_amount="399.00",
            idempotency_key=f"verification-{trace_id}",
        )
        started = time.perf_counter()
        coordination = coordinator.run(request, trace_id=trace_id)
        order_latency_ms = max(0.0, (time.perf_counter() - started) * 1000)
        decision = policy_engine.decide(
            coordination.inventory_results,
            coordination.risk_result,
        )
        inventory_spans = [
            span
            for span in trace_recorder.get_trace(trace_id)
            if span.component == InventoryAgent.name
            and span.action == "inventory.check"
        ]
        if not inventory_spans:
            raise RuntimeError("Verification probe did not record inventory latency")
        return VerificationProbeResult(
            trace_id=trace_id,
            success=decision.allow_creation,
            inventory_latency_ms=inventory_spans[-1].duration_ms,
            order_latency_ms=order_latency_ms,
            detail=(
                "Test-order checks passed"
                if decision.allow_creation
                else f"Test-order checks failed: {decision.reason}"
            ),
        )

    verification_service = VerificationService(
        incidents,
        verification_probe,
        trace_recorder,
    )
    replay_service = ReplayService(
        orders,
        coordinator,
        policy_engine,
        executor,
        trace_recorder,
        approval_service,
    )
    anomaly_detector = AnomalyDetector(
        metrics,
        incidents,
        order_provider=orders.list_all,
        agent_run_providers=(
            coordinator.list_run_records,
            inventory_agent.list_run_records,
            risk_agent.list_run_records,
            shopping_assistant.list_run_records,
            ops_guardian.list_run_records,
        ),
        model_call_providers=(gateway.list_calls,),
    )
    ops_api_dependencies = OpsApiDependencies(
        orders=orders,
        incidents=incidents,
        approvals=approvals,
        actions=remediation_actions,
        evidence=evidence_store,
        traces=trace_recorder,
        gateway=gateway,
        faults=faults,
        detector=anomaly_detector,
        incident_workflow=incident_workflow,
        approval_service=approval_service,
        execute_remediation=execute_remediation,
        verification_service=verification_service,
        replay_service=replay_service,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        gateway.close()

    application = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        payload = ErrorResponse(
            error_code="REQUEST_VALIDATION_ERROR",
            message="Request validation failed",
        )
        return JSONResponse(
            status_code=422,
            content=payload.model_dump(mode="json"),
        )

    @application.exception_handler(Exception)
    async def internal_server_error(
        _request: Request, _error: Exception
    ) -> JSONResponse:
        payload = ErrorResponse(
            error_code="INTERNAL_SERVER_ERROR",
            message="The server could not complete the request",
        )
        return JSONResponse(
            status_code=500,
            content=payload.model_dump(mode="json"),
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        """Return the process health and application identity."""

        return {
            "name": settings.app.name,
            "version": settings.app.version,
            "status": "ok",
        }

    application.include_router(create_order_router(workflow, orders))
    application.include_router(create_shopping_router(shopping_workflow))
    application.include_router(create_ops_router(ops_api_dependencies))
    application.include_router(create_dashboard_router())
    application.state.database = database
    application.state.order_repository = orders
    application.state.incident_repository = incidents
    application.state.approval_repository = approvals
    application.state.remediation_action_repository = remediation_actions
    application.state.audit_repository = audit
    application.state.inventory_repository = inventory
    application.state.order_workflow = workflow
    application.state.purchase_intent_repository = purchase_intents
    application.state.evidence_store = evidence_store
    application.state.shopping_assistant = shopping_assistant
    application.state.product_catalog_tool = product_catalog_tool
    application.state.shopping_workflow = shopping_workflow
    application.state.model_gateway = gateway
    application.state.fault_controller = faults
    application.state.trace_recorder = trace_recorder
    application.state.metrics = metrics
    application.state.log_store = log_store
    application.state.anomaly_detector = anomaly_detector
    application.state.ops_guardian = ops_guardian
    application.state.evidence_validator = evidence_validator
    application.state.incident_workflow = incident_workflow
    application.state.approval_service = approval_service
    application.state.remediation_executor = remediation_executor
    application.state.execute_remediation = execute_remediation
    application.state.verification_service = verification_service
    application.state.replay_service = replay_service
    application.state.query_observability = query_observability
    application.state.query_service_context = query_service_context
    application.state.ops_api_dependencies = ops_api_dependencies
    return application


app = create_app()
