"""Agent activity is admin-only and exposes recorded counters without private payloads."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_current_user
from app.api.endpoints.resources import (
    _agent_activity_payload,
    _agent_activity_record,
    router,
)
from app.db.models import AIAgent, AIAgentRun, Role
from app.db.session import get_db


def recorded_run(usage=None, **overrides):
    values = {
        "id": uuid4(),
        "created_at": datetime(2026, 10, 20, 10, 30, tzinfo=UTC),
        "status": "COMPLETED",
        "model": "recorded-model",
        "duration_ms": 120,
        "input_payload": {"system_prompt": "private prompt", "context": "private parcel"},
        "output_payload": {"usage": usage or {}, "markdown": "private diagnosis"},
        "error_message": "private provider body",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_activity_reads_both_provider_usage_shapes_and_only_allowlisted_metadata():
    openai = recorded_run({"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28})
    edge = recorded_run({"input_tokens": "12", "output_tokens": 5}, model="edge-model")
    result = _agent_activity_payload([(openai, "OPENAI"), (edge, "EDGE")])
    assert result["summary"] == {
        "runs": 2,
        "input_tokens": 32,
        "output_tokens": 13,
        "total_tokens": 45,
        "usage_reported_runs": 2,
        "cost_usd": None,
        "cost_status": "not_configured",
    }
    assert result["runs"][1]["provider"] == "EDGE"
    assert result["runs"][1]["model"] == "edge-model"
    assert result["scope"] == {"kind": "latest_runs", "limit": 100, "summary": "returned_runs_only"}
    assert not any(secret in str(result) for secret in (
        "private prompt", "private parcel", "private diagnosis", "private provider body",
    ))
    assert set(result["runs"][0]) == {
        "id", "created_at", "status", "model", "provider", "input_tokens", "output_tokens",
        "total_tokens", "usage_recorded", "cost_usd", "duration_ms",
    }


def test_activity_costs_must_be_recorded_for_every_run_to_report_a_total():
    first = recorded_run({"input_tokens": 1, "output_tokens": 2, "cost_usd": "0.0015"})
    free = recorded_run({"input_tokens": 3, "cost_usd": 0})
    known = _agent_activity_payload([(first, "OPENAI"), (free, "EDGE")])
    assert known["summary"]["cost_status"] == "known"
    assert known["summary"]["cost_usd"] == pytest.approx(0.0015)
    incomplete = _agent_activity_payload([(first, "OPENAI"), (recorded_run(), None)])
    assert incomplete["runs"][0]["cost_usd"] == 0.0015
    assert incomplete["runs"][1]["cost_usd"] is None
    assert incomplete["summary"]["cost_status"] == "not_configured"
    assert incomplete["summary"]["cost_usd"] is None
    assert incomplete["summary"]["usage_reported_runs"] == 1
    payload_cost = recorded_run(output_payload={"usage": {}, "cost_usd": "0.002"})
    assert _agent_activity_record(payload_cost, None)["cost_usd"] == 0.002


@pytest.mark.parametrize("invalid", [None, True, -1, "not recorded", "NaN", "Infinity", {"value": 10}])
def test_activity_does_not_estimate_or_accept_invalid_counters_and_costs(invalid):
    run = recorded_run({"input_tokens": invalid, "output_tokens": invalid, "cost_usd": invalid})
    result = _agent_activity_record(run, None)
    assert result["input_tokens"] == result["output_tokens"] == result["total_tokens"] == 0
    assert result["cost_usd"] is None


def test_activity_empty_history_and_non_object_usage_are_explicit():
    empty = _agent_activity_payload([])
    assert empty["runs"] == []
    assert empty["summary"]["runs"] == empty["summary"]["usage_reported_runs"] == 0
    assert empty["summary"]["cost_usd"] is None
    for payload in (None, [], {"usage": "not an object"}):
        result = _agent_activity_record(recorded_run(output_payload=payload), None)
        assert result["total_tokens"] == 0
        assert result["usage_recorded"] is False


@pytest.fixture
def activity_app():
    agent_id = uuid4()
    agent = SimpleNamespace(id=agent_id, deleted_at=None)
    role = SimpleNamespace(id=uuid4(), code="administrador")
    user = SimpleNamespace(id=uuid4(), role_id=role.id)
    queries = []
    rows = [(recorded_run({"prompt_tokens": 4, "completion_tokens": 6}), "OPENAI")]

    class FakeSession:
        async def get(self, model, key):
            if model is Role and key == role.id:
                return role
            if model is AIAgent and key == agent_id:
                return agent
            return None

        async def execute(self, statement):
            queries.append(statement)
            return SimpleNamespace(all=lambda: rows)

    app = FastAPI()
    app.include_router(router)

    async def database_override():
        yield FakeSession()

    async def user_override():
        return user

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_current_user] = user_override
    return app, agent, role, queries


async def test_activity_endpoint_filters_agent_caps_history_and_uses_historical_provider(activity_app):
    app, agent, _, queries = activity_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test.local") as client:
        response = await client.get(f"/agents/{agent.id}/activity")
    assert response.status_code == 200
    assert response.json()["summary"]["total_tokens"] == 10
    assert response.json()["runs"][0]["provider"] == "OPENAI"
    assert queries[0]._limit_clause.value == 100
    assert AIAgentRun.agent_id.name in str(queries[0].whereclause)
    assert agent.id in queries[0].compile().params.values()
    assert "LEFT OUTER JOIN ai_agent_versions" in str(queries[0])
    assert "ai_agent_runs.created_at DESC" in str(queries[0])
    assert "private" not in response.text


@pytest.mark.parametrize("role_code", ["productor", "tecnico", "analista"])
async def test_activity_endpoint_requires_an_admin_role(activity_app, role_code):
    app, agent, role, queries = activity_app
    role.code = role_code
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test.local") as client:
        response = await client.get(f"/agents/{agent.id}/activity")
    assert response.status_code == 403
    assert queries == []


async def test_activity_endpoint_rejects_missing_deleted_and_unauthenticated_agents(activity_app):
    app, agent, _, queries = activity_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test.local") as client:
        assert (await client.get(f"/agents/{uuid4()}/activity")).status_code == 404
        agent.deleted_at = datetime.now(UTC)
        assert (await client.get(f"/agents/{agent.id}/activity")).status_code == 404
        del app.dependency_overrides[get_current_user]
        assert (await client.get(f"/agents/{agent.id}/activity")).status_code == 401
    assert queries == []
