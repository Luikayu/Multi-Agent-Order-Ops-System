from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from order_agent_ops.domain.enums import PurchaseIntentStatus
from order_agent_ops.domain.shopping import PurchaseIntentRecord
from order_agent_ops.main import create_app
from order_agent_ops.models import MockModelProvider, MockTaskType


def make_app(tmp_path: Path, provider: MockModelProvider | None = None) -> FastAPI:
    return create_app(
        database_path=tmp_path / "shopping.sqlite3",
        provider=provider or MockModelProvider(),
    )


def semantic_request() -> dict[str, str]:
    return {
        "user_id": "USER-001",
        "message": "我想买一个500元以内、晚上在宿舍写代码、别影响室友的机械键盘",
    }


def test_semantic_candidates_require_confirmation_then_reuse_order_workflow(
    tmp_path: Path,
) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        created = client.post("/shopping/intents", json=semantic_request())
        assert created.status_code == 200
        intent = created.json()
        assert intent["status"] == "READY_FOR_CONFIRMATION"
        assert intent["candidates"]
        assert application.state.order_repository.list_all() == []

        recommended_sku = intent["recommendation"]["recommended_sku"]
        candidate = next(
            item for item in intent["candidates"] if item["sku"] == recommended_sku
        )
        before = application.state.inventory_repository.get(recommended_sku).quantity
        confirmed = client.post(
            f"/shopping/intents/{intent['intent_id']}/confirm",
            json={
                "sku": recommended_sku,
                "quantity": 1,
                "idempotency_key": "shopping-confirm-001",
            },
        )
        replay = client.post(
            f"/shopping/intents/{intent['intent_id']}/confirm",
            json={
                "sku": recommended_sku,
                "quantity": 1,
                "idempotency_key": "shopping-confirm-001",
            },
        )

        assert confirmed.status_code == replay.status_code == 200
        result = confirmed.json()
        assert result["order"]["status"] == "COMPLETED"
        assert result["order"]["trace_id"] == intent["trace_id"]
        assert result["order"]["items"][0]["unit_price"] == candidate["unit_price"]
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["order"]["order_id"] == result["order"]["order_id"]
        assert len(application.state.order_repository.list_all()) == 1
        assert application.state.inventory_repository.get(recommended_sku).quantity == before - 1

        spans = application.state.trace_recorder.get_trace(intent["trace_id"])
        actions = {(span.component, span.action) for span in spans}
        assert {
            ("shopping-assistant-agent", "shopping.analyze"),
            ("product-catalog-tool", "catalog.read"),
            ("shopping-assistant-agent", "shopping.catalog_search"),
            ("shopping-workflow", "shopping.confirm"),
            ("order-workflow", "order.request"),
        }.issubset(actions)


def test_vague_intent_can_be_clarified_without_losing_previous_message(
    tmp_path: Path,
) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        created = client.post(
            "/shopping/intents",
            json={"user_id": "USER-001", "message": "我想买一个好一点的键盘"},
        )
        intent = created.json()
        assert intent["status"] == "NEEDS_CLARIFICATION"
        assert intent["parsed_intent"]["missing_information"]
        assert application.state.order_repository.list_all() == []

        clarified = client.post(
            f"/shopping/intents/{intent['intent_id']}/messages",
            json={"message": "预算500元，主要用于办公"},
        )

        assert clarified.status_code == 200
        result = clarified.json()
        assert result["status"] == "READY_FOR_CONFIRMATION"
        assert len(result["messages"]) == 2
        assert result["candidates"]
        assert application.state.order_repository.list_all() == []


def test_invalid_candidate_and_client_price_cannot_create_order(tmp_path: Path) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        intent = client.post("/shopping/intents", json=semantic_request()).json()
        invalid_sku = client.post(
            f"/shopping/intents/{intent['intent_id']}/confirm",
            json={
                "sku": "SKU-INVENTED",
                "quantity": 1,
                "idempotency_key": "invalid-sku",
            },
        )
        injected_price = client.post(
            f"/shopping/intents/{intent['intent_id']}/confirm",
            json={
                "sku": intent["candidates"][0]["sku"],
                "quantity": 1,
                "idempotency_key": "injected-price",
                "unit_price": "0.01",
            },
        )

    assert invalid_sku.status_code == 409
    assert invalid_sku.json()["error_code"] == "INVALID_PRODUCT_CANDIDATE"
    assert injected_price.status_code == 422
    assert injected_price.json()["error_code"] == "REQUEST_VALIDATION_ERROR"
    assert application.state.order_repository.list_all() == []


def test_agent_can_return_explained_approximate_feature_matches(
    tmp_path: Path,
) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        response = client.post(
            "/shopping/intents",
            json={
                "user_id": "USER-001",
                "message": "我想买一个预算200元以内的全铝机械键盘",
            },
        )

    assert response.status_code == 200
    intent = response.json()
    assert intent["status"] == "READY_FOR_CONFIRMATION"
    assert intent["candidates"]
    assert all(float(item["unit_price"]) <= 200 for item in intent["candidates"])
    assert intent["candidates"][0]["sku"] == "SKU-037"
    assert intent["recommendation"]["reason"]
    assert application.state.order_repository.list_all() == []


def test_agent_does_not_relax_budget_when_no_catalog_product_is_available(
    tmp_path: Path,
) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        response = client.post(
            "/shopping/intents",
            json={
                "user_id": "USER-001",
                "message": "我想买一个预算10元以内、办公用的键盘",
            },
        )

    assert response.status_code == 200
    intent = response.json()
    assert intent["status"] == "NEEDS_CLARIFICATION"
    assert intent["candidates"] == []
    assert "category, budget" in intent["workflow_message"]
    assert application.state.order_repository.list_all() == []


def test_model_candidate_hallucination_is_discarded_before_candidate_persistence(
    tmp_path: Path,
) -> None:
    provider = MockModelProvider(
        responses={
            MockTaskType.SHOPPING_CATALOG_SEARCH: {
                "selected_skus": ["SKU-INVENTED"],
                "reason": "Invented recommendation",
            }
        }
    )
    application = make_app(tmp_path, provider)
    with TestClient(application) as client:
        response = client.post("/shopping/intents", json=semantic_request())

    assert response.status_code == 200
    assert response.json()["status"] == "NEEDS_CLARIFICATION"
    assert response.json()["candidates"] == []
    assert len(application.state.purchase_intent_repository.list_all()) == 1
    assert application.state.order_repository.list_all() == []


def test_expired_intent_cannot_be_confirmed(tmp_path: Path) -> None:
    application = make_app(tmp_path)
    with TestClient(application) as client:
        intent = client.post("/shopping/intents", json=semantic_request()).json()
        repository = application.state.purchase_intent_repository
        record = repository.get(intent["intent_id"])
        now = datetime.now(timezone.utc)
        expired = PurchaseIntentRecord.model_validate(
            record.model_copy(
                update={
                    "created_at": now - timedelta(hours=1),
                    "expires_at": now - timedelta(seconds=1),
                }
            ).model_dump()
        )
        repository.update(expired)

        response = client.post(
            f"/shopping/intents/{intent['intent_id']}/confirm",
            json={
                "sku": intent["candidates"][0]["sku"],
                "quantity": 1,
                "idempotency_key": "expired-confirm",
            },
        )

    assert response.status_code == 409
    assert response.json()["error_code"] == "PURCHASE_INTENT_EXPIRED"
    stored = application.state.purchase_intent_repository.get(intent["intent_id"])
    assert stored.status is PurchaseIntentStatus.EXPIRED
    assert application.state.order_repository.list_all() == []
