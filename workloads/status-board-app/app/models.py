"""Response models.

Every route returns one of these rather than a bare dict, so the shape of the
API is declared in one place and shows up in the generated OpenAPI docs.
"""

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Reply from the load balancer health check."""

    status: str = Field(examples=["ok"])


class Service(BaseModel):
    """One service on the board."""

    service_id: str = Field(examples=["checkout-api"])
    name: str = Field(examples=["Checkout API"])
    status: str = Field(examples=["operational"])
    updated_at: str = Field(examples=["2026-09-17T09:00:00+00:00"])


class Incident(BaseModel):
    """One incident update recorded against a service."""

    service_id: str = Field(examples=["checkout-api"])
    incident_id: str
    status: str = Field(examples=["degraded"])
    message: str = Field(examples=["Elevated latency on checkout."])
    created_at: str = Field(examples=["2026-09-17T09:05:00+00:00"])


class ServiceDetail(BaseModel):
    """A service together with its recent incident history."""

    service: Service
    incidents: list[Incident]
