"""Abstract interface implemented by every model provider."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel


ModelMessage = Mapping[str, str]


class ModelTaskType(StrEnum):
    SHOPPING_ANALYZE = "shopping_analyze"
    SHOPPING_CATALOG_SEARCH = "shopping_catalog_search"
    SHOPPING_RANK = "shopping_rank"
    ORDER_COORDINATOR = "order_coordinator"
    INVENTORY_AGENT = "inventory_agent"
    RISK_AGENT = "risk_agent"
    OPS_GUARDIAN = "ops_guardian"


class ModelProvider(ABC):
    """Minimum provider capability used by the Python model gateway."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable provider identifier used in telemetry."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Configured model identifier used in telemetry."""

    @abstractmethod
    def generate(
        self,
        messages: Sequence[ModelMessage],
        *,
        task_type: str,
        response_schema: type[BaseModel] | None = None,
    ) -> str:
        """Return a JSON string; validation remains the gateway's responsibility."""

    def close(self) -> None:
        """Release provider resources; providers without resources need no override."""
