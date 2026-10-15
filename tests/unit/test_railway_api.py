"""The Railway API client: what it sends, what it reads back, what it raises.

Railway is replaced at the HTTP layer with respx, so these need no token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from db_ops import railway_api
from db_ops.railway_api import DEFAULT_URL, RailwayApi, RailwayApiError


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(railway_api.time, "sleep", lambda s: None)


def _api(kind: railway_api.TokenKind = "project") -> RailwayApi:
    return RailwayApi("tok", kind, "proj", "env")


def _sent(route: respx.Route, call: int = -1) -> dict[str, object]:
    return json.loads(route.calls[call].request.content)


ENV = {
    "data": {
        "environment": {
            "serviceInstances": {
                "edges": [
                    {"node": {"serviceId": "s-pg", "serviceName": "Postgres"}},
                    {"node": {"serviceId": "s-fork", "serviceName": "drill-x"}},
                ]
            },
            "volumeInstances": {
                "edges": [
                    {
                        "node": {
                            "id": "vi-pg",
                            "serviceId": "s-pg",
                            "volume": {"id": "v-pg"},
                        }
                    },
                    {
                        "node": {
                            "id": "vi-f",
                            "serviceId": "s-fork",
                            "volume": {"id": "v-f"},
                        }
                    },
                ]
            },
        }
    }
}


@respx.mock
def test_a_project_token_goes_in_its_own_header() -> None:
    route = respx.post(DEFAULT_URL).mock(return_value=httpx.Response(200, json=ENV))

    _api("project").environment_instances()

    headers = route.calls[0].request.headers
    assert headers["Project-Access-Token"] == "tok"
    assert "Authorization" not in headers


@respx.mock
def test_a_workspace_token_is_a_bearer_token() -> None:
    route = respx.post(DEFAULT_URL).mock(return_value=httpx.Response(200, json=ENV))

    _api("workspace").environment_instances()

    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_services_and_volumes_are_read_from_the_environment() -> None:
    respx.post(DEFAULT_URL).mock(return_value=httpx.Response(200, json=ENV))
    api = _api()

    found = api.find_service("Postgres")
    assert found is not None and found.service_id == "s-pg"
    assert api.find_service("nope") is None
    volumes = api.volumes_of("s-pg")
    assert [(v.instance_id, v.volume_id) for v in volumes] == [("vi-pg", "v-pg")]


@respx.mock
def test_project_services_carry_their_creation_time() -> None:
    respx.post(DEFAULT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "project": {
                        "services": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "s1",
                                        "name": "drill-x",
                                        "createdAt": "2026-10-15T19:00:00.000Z",
                                    }
                                }
                            ]
                        }
                    }
                }
            },
        )
    )

    [service] = _api().project_services()

    assert service.name == "drill-x"
    assert service.created_at == datetime(2026, 10, 15, 19, tzinfo=UTC)


@respx.mock
def test_only_the_private_domain_is_kept_from_a_services_variables() -> None:
    respx.post(DEFAULT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "variables": {
                        "RAILWAY_PRIVATE_DOMAIN": "drill-x.railway.internal",
                        "WAL_RECOVER_FROM_S3_SECRET": "do-not-keep",
                    }
                }
            },
        )
    )

    assert _api().private_domain("s-fork") == "drill-x.railway.internal"


@respx.mock
def test_a_service_without_a_private_domain_yet_has_none() -> None:
    respx.post(DEFAULT_URL).mock(
        return_value=httpx.Response(200, json={"data": {"variables": {}}})
    )

    assert _api().private_domain("s-fork") is None


@respx.mock
def test_a_restore_names_its_volume_target_and_fork() -> None:
    route = respx.post(DEFAULT_URL).mock(
        return_value=httpx.Response(
            200, json={"data": {"volumeInstancePITRRestore": {"workflowId": "wf-1"}}}
        )
    )
    target = datetime(2026, 10, 15, 20, 0, 1, tzinfo=UTC)

    assert _api().pitr_restore("vi-pg", target, "drill-x") == "wf-1"

    assert _sent(route)["variables"] == {
        "volumeInstanceId": "vi-pg",
        "targetTimestamp": "2026-10-15T20:00:01+00:00",
        "newServiceName": "drill-x",
    }


@respx.mock
def test_workflow_status_and_deletes() -> None:
    route = respx.post(DEFAULT_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"data": {"workflowStatus": {"status": "Error", "error": "boom"}}},
            ),
            httpx.Response(200, json={"data": {"volumeDelete": True}}),
            httpx.Response(200, json={"data": {"serviceDelete": True}}),
        ]
    )
    api = _api()

    assert api.workflow_status("wf-1") == ("Error", "boom")
    api.delete_volume("v-f")
    api.delete_service("s-fork")

    assert _sent(route, 1)["variables"] == {"volumeId": "v-f"}
    assert _sent(route, 2)["variables"] == {
        "environmentId": "env",
        "serviceId": "s-fork",
    }


@respx.mock
def test_graphql_errors_raise_with_the_message_only() -> None:
    respx.post(DEFAULT_URL).mock(
        return_value=httpx.Response(
            200, json={"errors": [{"message": "Not Authorized"}], "data": None}
        )
    )

    with pytest.raises(RailwayApiError, match="Not Authorized"):
        _api().environment_instances()


@respx.mock
def test_a_read_is_retried_through_a_rate_limit() -> None:
    route = respx.post(DEFAULT_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=ENV)]
    )

    _api().environment_instances()

    assert route.call_count == 2


@respx.mock
def test_a_read_gives_up_after_three_tries() -> None:
    route = respx.post(DEFAULT_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(RailwayApiError, match="503"):
        _api().environment_instances()
    assert route.call_count == 3


@respx.mock
def test_a_write_is_never_retried() -> None:
    route = respx.post(DEFAULT_URL).mock(return_value=httpx.Response(502))

    with pytest.raises(RailwayApiError, match="502"):
        _api().delete_service("s-fork")
    assert route.call_count == 1


@respx.mock
def test_transport_failures_and_odd_answers_raise() -> None:
    respx.post(DEFAULT_URL).mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(200, text="<html>"),
            httpx.Response(401, json={"data": {}}),
            httpx.Response(200, json={"data": None}),
        ]
    )
    api = _api()

    with pytest.raises(RailwayApiError, match="unreachable"):
        api.delete_volume("v")
    with pytest.raises(RailwayApiError, match="no JSON"):
        api.delete_volume("v")
    with pytest.raises(RailwayApiError, match="401"):
        api.delete_volume("v")
    with pytest.raises(RailwayApiError, match="without data"):
        api.delete_volume("v")
    api.close()


@respx.mock
def test_a_write_waits_longer_than_a_read() -> None:
    route = respx.post(DEFAULT_URL).mock(
        return_value=httpx.Response(200, json={"data": {"serviceDelete": True}})
    )

    RailwayApi("tok", "workspace", "proj", "env", write_timeout=95).delete_service(
        "s-fork"
    )

    assert route.calls[0].request.extensions["timeout"]["read"] == 95
