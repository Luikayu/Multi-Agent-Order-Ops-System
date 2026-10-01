"""HTTP routers for the order-agent-ops application."""

from order_agent_ops.api.dashboard import create_dashboard_router
from order_agent_ops.api.ops import OpsApiDependencies, create_ops_router
from order_agent_ops.api.orders import create_order_router
from order_agent_ops.api.shopping import create_shopping_router

__all__ = [
    "OpsApiDependencies",
    "create_dashboard_router",
    "create_ops_router",
    "create_order_router",
    "create_shopping_router",
]
