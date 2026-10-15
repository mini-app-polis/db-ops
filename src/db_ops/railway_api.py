"""The slice of Railway's public GraphQL API that db-drill drives.

Point-in-time restore is one server-side mutation,
``volumeInstancePITRRestore``: Railway creates the fork service, gives it an
empty volume, and the image restores into it on first boot. Everything else
here is finding that fork and taking it down again. The queries are the ones
the Railway CLI sends for the same operations (railwayapp/cli, ``src/gql``),
so they track what Railway itself relies on.

Authentication is one token, ``DB_DRILL_RAILWAY_TOKEN``. A *project* token
(scoped to one environment) is sent as ``Project-Access-Token``; a workspace
or account token as ``Authorization: Bearer``. The narrowest that works is
the one to keep.

Nothing in this module logs a request or a response body: the fork's
variables include the PITR archive's read/write credential.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

TokenKind = Literal["project", "workspace"]

DEFAULT_URL = "https://backboard.railway.com/graphql/v2"


class RailwayApiError(RuntimeError):
    """A Railway API call that failed. The message carries no secrets."""


@dataclass(frozen=True)
class ServiceInstance:
    """A service as it exists in one environment."""

    service_id: str
    name: str


@dataclass(frozen=True)
class VolumeInstance:
    """A volume as it exists in one environment, and what it is mounted on."""

    instance_id: str
    volume_id: str
    service_id: str | None
    name: str = ""


@dataclass(frozen=True)
class Service:
    """A service in the project, with when it was created."""

    service_id: str
    name: str
    created_at: datetime


ENVIRONMENT_INSTANCES = """
query EnvironmentInstances($environmentId: String!, $projectId: String!) {
  environment(id: $environmentId, projectId: $projectId) {
    serviceInstances(first: 100) {
      edges { node { serviceId serviceName } }
    }
    volumeInstances(first: 100) {
      edges { node { id serviceId deletedAt isPendingDeletion volume { id name } } }
    }
  }
}
"""

PROJECT_SERVICES = """
query ProjectServices($projectId: String!) {
  project(id: $projectId) {
    services(first: 100) {
      edges { node { id name createdAt } }
    }
  }
}
"""

PITR_RESTORE = """
mutation VolumeInstancePitrRestore(
  $volumeInstanceId: String!
  $targetTimestamp: DateTime!
  $newServiceName: String
) {
  volumeInstancePITRRestore(
    volumeInstanceId: $volumeInstanceId
    targetTimestamp: $targetTimestamp
    newServiceName: $newServiceName
  ) {
    workflowId
  }
}
"""

WORKFLOW_STATUS = """
query WorkflowStatus($workflowId: String!) {
  workflowStatus(workflowId: $workflowId) { status error }
}
"""

SERVICE_VARIABLES = """
query Variables($projectId: String!, $environmentId: String!, $serviceId: String!) {
  variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
}
"""

VOLUME_DELETE = """
mutation VolumeDelete($volumeId: String!) { volumeDelete(volumeId: $volumeId) }
"""

SERVICE_DELETE = """
mutation ServiceDelete($environmentId: String!, $serviceId: String!) {
  serviceDelete(environmentId: $environmentId, id: $serviceId)
}
"""


class RailwayApi:
    """One project and environment, through one token."""

    def __init__(
        self,
        token: str,
        kind: TokenKind,
        project_id: str,
        environment_id: str,
        *,
        url: str = DEFAULT_URL,
        timeout: float = 30,
        write_timeout: float = 120,
        client: httpx.Client | None = None,
    ) -> None:
        # no-retry: this builds the client and calls nothing. Reads are
        # retried in query(); writes are deliberately not, in mutate().
        header = (
            {"Project-Access-Token": token}
            if kind == "project"
            else {"Authorization": f"Bearer {token}"}
        )
        self.project_id = project_id
        self.environment_id = environment_id
        self._url = url
        self._client = client or httpx.Client(timeout=timeout)
        # Deleting a volume or a service can take Railway well over the
        # read timeout to answer, and still happen.
        self._write_timeout = write_timeout
        self._headers = {**header, "Content-Type": "application/json"}

    def close(self) -> None:
        """Release the HTTP client."""
        self._client.close()

    # -- transport ---------------------------------------------------------

    def _post(
        self, query: str, variables: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                self._url,
                json={"query": query, "variables": variables},
                headers=self._headers,
                timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:
            raise _Retryable(f"Railway API unreachable: {type(exc).__name__}") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise _Retryable(f"Railway API answered {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise RailwayApiError(
                f"Railway API answered {response.status_code} with no JSON"
            ) from None
        # GraphQL reports failures — authorization included — in `errors`,
        # usually with a 200. Only the messages are kept: they name the
        # field, never a value.
        errors = body.get("errors") or []
        if errors:
            messages = "; ".join(str(e.get("message", "?")) for e in errors)[:300]
            raise RailwayApiError(f"Railway API error: {messages}")
        if response.status_code >= 400:
            raise RailwayApiError(f"Railway API answered {response.status_code}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise RailwayApiError("Railway API answered without data")
        return data

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """A read. Retried on a 429 or 5xx, which are safe to repeat."""
        delay = 2.0
        for attempt in range(3):
            try:
                return self._post(query, variables)
            except _Retryable as exc:
                if attempt == 2:
                    raise RailwayApiError(str(exc)) from None
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    def mutate(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """A write. Never retried here: a write that reached Railway ran."""
        try:
            return self._post(query, variables, timeout=self._write_timeout)
        except _Retryable as exc:
            raise RailwayApiError(str(exc)) from None

    # -- reads -------------------------------------------------------------

    def environment_instances(
        self,
    ) -> tuple[list[ServiceInstance], list[VolumeInstance]]:
        """Every service and volume in the environment.

        A volume Railway has accepted for deletion is left out: deleting one
        is asynchronous, and it can stay listed, pending, for many minutes.
        """
        data = self.query(
            ENVIRONMENT_INSTANCES,
            {"environmentId": self.environment_id, "projectId": self.project_id},
        )
        env = data.get("environment") or {}
        services = [
            ServiceInstance(service_id=n["serviceId"], name=n["serviceName"])
            for n in _nodes(env.get("serviceInstances"))
        ]
        volumes = [
            VolumeInstance(
                instance_id=n["id"],
                volume_id=(n.get("volume") or {}).get("id", ""),
                service_id=n.get("serviceId"),
                name=(n.get("volume") or {}).get("name", ""),
            )
            for n in _nodes(env.get("volumeInstances"))
            if not (n.get("deletedAt") or n.get("isPendingDeletion"))
        ]
        return services, volumes

    def find_service(self, name: str) -> ServiceInstance | None:
        """The environment's service with exactly this name, if there is one."""
        services, _ = self.environment_instances()
        return next((s for s in services if s.name == name), None)

    def volumes_of(self, service_id: str) -> list[VolumeInstance]:
        """The volumes mounted on one service in the environment."""
        _, volumes = self.environment_instances()
        return [v for v in volumes if v.service_id == service_id]

    def project_services(self) -> list[Service]:
        """Every service in the project, with its creation time."""
        data = self.query(PROJECT_SERVICES, {"projectId": self.project_id})
        return [
            Service(
                service_id=n["id"],
                name=n["name"],
                created_at=datetime.fromisoformat(
                    n["createdAt"].replace("Z", "+00:00")
                ),
            )
            for n in _nodes((data.get("project") or {}).get("services"))
        ]

    def workflow_status(self, workflow_id: str) -> tuple[str, str | None]:
        """A workflow's status — Running, Complete, Error or NotFound."""
        data = self.query(WORKFLOW_STATUS, {"workflowId": workflow_id})
        result = data.get("workflowStatus") or {}
        return str(result.get("status", "NotFound")), result.get("error")

    def private_domain(self, service_id: str) -> str | None:
        """The service's ``*.railway.internal`` host, once Railway has set it.

        Read from its rendered variables. Only this one name is kept; the
        rest — among them the archive bucket's credential — is dropped here.
        """
        data = self.query(
            SERVICE_VARIABLES,
            {
                "projectId": self.project_id,
                "environmentId": self.environment_id,
                "serviceId": service_id,
            },
        )
        variables = data.get("variables") or {}
        domain = variables.get("RAILWAY_PRIVATE_DOMAIN")
        return str(domain) if domain else None

    # -- writes ------------------------------------------------------------

    def pitr_restore(
        self, volume_instance_id: str, target: datetime, new_service_name: str
    ) -> str | None:
        """Start a point-in-time restore into a new service. Returns its workflow."""
        data = self.mutate(
            PITR_RESTORE,
            {
                "volumeInstanceId": volume_instance_id,
                "targetTimestamp": target.isoformat(),
                "newServiceName": new_service_name,
            },
        )
        result = data.get("volumeInstancePITRRestore") or {}
        return result.get("workflowId")

    def delete_volume(self, volume_id: str) -> None:
        """Delete a volume and its data."""
        self.mutate(VOLUME_DELETE, {"volumeId": volume_id})

    def delete_service(self, service_id: str) -> None:
        """Delete a service from the environment."""
        self.mutate(
            SERVICE_DELETE,
            {"environmentId": self.environment_id, "serviceId": service_id},
        )


class _Retryable(RailwayApiError):
    """A 429 or 5xx: the request may not have run."""


def _nodes(connection: Any) -> list[dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    return [e["node"] for e in connection.get("edges") or [] if e.get("node")]
