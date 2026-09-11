"""Describe the same payload preparation used by outbound adapters."""
from typing import Any

from providers.base import AdapterRequest, ProviderAdapter, ProviderRequestError


def inspect_payload(
    adapter: ProviderAdapter,
    request: AdapterRequest,
    *,
    original_body: dict[str, Any],
) -> dict[str, Any]:
    report = {
        "status": "prepared",
        "provider": adapter.name,
        "model": request.public_model,
        "upstream_model": request.upstream_model,
        "upstream_verified": False,
        "payload": None,
        "changes": [],
        "errors": [],
        "dropped_parameters": [],
    }
    try:
        from providers.parameters import apply_sampling_policy
        _, dropped = apply_sampling_policy(
            request.body, adapter.name, request.upstream_model, request.drop_sampling_params,
        )
        report["dropped_parameters"] = [
            {"field": field, "reason": "Explicit administrator model policy"}
            for field in dropped
        ]
        payload = adapter.prepare(request)
    except ProviderRequestError as error:
        report.update(status="blocked", errors=[{
            "code": "unsupported_request", "message": str(error),
        }])
        return report
    changes = []
    for field in sorted(original_body.keys() | payload.keys()):
        if field not in payload:
            changes.append({"field": field, "action": "removed"})
        elif field not in original_body:
            changes.append({"field": field, "action": "added"})
        elif original_body[field] != payload[field]:
            changes.append({"field": field, "action": "changed"})
    report.update(payload=payload, changes=changes)
    return report
