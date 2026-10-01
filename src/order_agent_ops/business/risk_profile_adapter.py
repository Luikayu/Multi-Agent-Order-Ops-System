"""Read-only access to deterministic user behavior facts."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from order_agent_ops.config import PROJECT_ROOT
from order_agent_ops.domain.base import DomainModel, NonEmptyString


DEFAULT_USERS_PATH = PROJECT_ROOT / "data" / "users.json"


class UserRiskProfile(DomainModel):
    """Observed user facts; this model intentionally contains no risk decision."""

    user_id: NonEmptyString
    account_age_days: int = Field(ge=0)
    historical_order_count: int = Field(ge=0)
    chargeback_count: int = Field(ge=0)
    failed_payment_count_30d: int = Field(ge=0)
    orders_last_hour: int = Field(ge=0)
    average_order_amount: Decimal = Field(ge=0)
    identity_verified: bool
    account_status: NonEmptyString


class RiskProfileLookupResult(DomainModel):
    found: bool
    profile: UserRiskProfile | None
    reason: NonEmptyString

    @model_validator(mode="after")
    def found_matches_profile(self) -> "RiskProfileLookupResult":
        if self.found != (self.profile is not None):
            raise ValueError("Risk profile found flag must match profile presence")
        return self


class RiskProfileAdapter:
    """Return facts for RiskAgent without assigning a risk level."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_USERS_PATH
        payload = self._read_payload(self.path)
        users = payload.get("users")
        if not isinstance(users, list):
            raise ValueError("Users data must contain a 'users' list")

        self.data_version = self._required_string(payload, "data_version")
        self._profiles: dict[str, UserRiskProfile] = {}
        for raw_profile in users:
            profile = UserRiskProfile.model_validate(raw_profile)
            if profile.user_id in self._profiles:
                raise ValueError(f"Duplicate user in profile data: {profile.user_id}")
            self._profiles[profile.user_id] = profile

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as data_file:
            payload = json.load(data_file)
        if not isinstance(payload, dict):
            raise ValueError(f"Users data must contain a JSON object: {path}")
        return payload

    @staticmethod
    def _required_string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Users data requires non-empty '{field}'")
        return value.strip()

    def query(self, user_id: str) -> RiskProfileLookupResult:
        normalized_user_id = user_id.strip()
        if not normalized_user_id:
            raise ValueError("User ID must not be empty")
        profile = self._profiles.get(normalized_user_id)
        if profile is None:
            return RiskProfileLookupResult(
                found=False,
                profile=None,
                reason=f"User profile not found: {normalized_user_id}",
            )
        return RiskProfileLookupResult(
            found=True,
            profile=profile.model_copy(deep=True),
            reason="User fact profile found",
        )

    def list_profiles(self) -> list[UserRiskProfile]:
        return [profile.model_copy(deep=True) for profile in self._profiles.values()]
