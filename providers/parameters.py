"""Small model-aware sampling policy; never silently drop essential features."""
import json
import math
from copy import deepcopy

SAMPLING = frozenset({"temperature", "top_p", "top_k"})
FIREWORKS_MODEL = "accounts/fireworks/models/deepseek-v4-flash-0731"


def load_drop_policy(raw, routes):
    """Resolve provider defaults and model overrides from trusted configuration."""
    configured = json.loads(raw)
    if not isinstance(configured, dict):
        raise ValueError("PROXY_DROP_SAMPLING_PARAMS must be a JSON object")
    result = {}
    providers = {route.provider for route in routes.values()}
    for name, fields in configured.items():
        if (name not in routes and name not in providers) or not isinstance(fields, list) or not all(
            isinstance(field, str) and field in SAMPLING for field in fields
        ):
            raise ValueError("Drop policy requires configured providers or models and sampling field lists")
        if name in providers:
            identity = (name, None)
        else:
            route = routes[name]
            identity = (route.provider, route.upstream_model)
        selected = frozenset(fields)
        if identity in result and result[identity] != selected:
            raise ValueError("Conflicting drop policies for aliases of the same model")
        result[identity] = selected
    return result


def resolve_drop_fields(policy, provider, model):
    # An explicit empty model override restores strict behavior for that model.
    return policy.get((provider, model), policy.get((provider, None), frozenset()))


def apply_sampling_policy(body, provider, model, drop_fields=frozenset()):
    from providers.base import ProviderRequestError
    if not set(drop_fields) <= SAMPLING:
        raise ValueError("Only sampling fields may be dropped")
    effective = deepcopy(body)
    removed = []
    for field in sorted(SAMPLING & body.keys()):
        if field in drop_fields:
            effective.pop(field)
            removed.append(field)
            continue
        if provider != "fireworks" or model != FIREWORKS_MODEL:
            raise ProviderRequestError(
                f"This gateway's model policy does not accept {field} for {model}; "
                "omit it or ask the administrator to explicitly permit removal."
            )
        value = body[field]
        if value is None:
            continue  # Fireworks documents these fields as nullable.
        valid = not isinstance(value, bool) and isinstance(value, (int, float))
        valid = valid and (not isinstance(value, float) or math.isfinite(value))
        if field == "temperature":
            valid = valid and 0 <= value <= 2
        elif field == "top_p":
            valid = valid and 0 <= value <= 1
        else:
            valid = valid and isinstance(value, int) and 0 <= value <= 100
        if not valid:
            raise ProviderRequestError(f"Invalid {field} for the configured model.")
    return effective, removed
