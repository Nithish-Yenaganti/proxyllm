"""Map public model names to provider adapters and trusted upstream models."""

# Supplies immutable model-route records.
from dataclasses import dataclass

# Keeps per-token prices exact while calculating very small request costs.
from decimal import Decimal

# Supplies the two concrete provider adapters supported in Phase 4.
from providers.anthropic import AnthropicAdapter
from providers.base import ProviderAdapter
from providers.fireworks import FireworksAdapter


# Describes one explicit public-model routing decision.
@dataclass(frozen=True)
class ModelRoute:
    # Selects the provider adapter that must handle this model.
    provider: str

    # Supplies the exact model identifier sent to that provider.
    upstream_model: str

    # Stores the standard provider price for one million uncached input tokens.
    input_cost_per_million: Decimal

    # Stores the standard provider price for one million generated output tokens.
    output_cost_per_million: Decimal

    # Stores the discounted price for cached input when the provider reports it.
    cached_input_cost_per_million: Decimal


# Creates the adapter instances reused by model-routing lookups.
PROVIDER_ADAPTERS: dict[str, ProviderAdapter] = {
    # Handles providers such as Fireworks that already use OpenAI chat format.
    "fireworks": FireworksAdapter(),
    # Handles request, response, and SSE conversion for Anthropic Messages.
    "anthropic": AnthropicAdapter(),
}


# Defines every client-visible model name accepted by this gateway deployment.
MODEL_ROUTES = {
    # Preserves the full Fireworks model ID already used by Phase 1 through Phase 3.
    "accounts/fireworks/models/deepseek-v4-flash-0731": ModelRoute(
        provider="fireworks",
        upstream_model="accounts/fireworks/models/deepseek-v4-flash-0731",
        input_cost_per_million=Decimal("0.22"),
        output_cost_per_million=Decimal("0.66"),
        cached_input_cost_per_million=Decimal("0.007"),
    ),
    # Adds a shorter stable alias for the same configured Fireworks model.
    "fireworks/deepseek-v4-flash": ModelRoute(
        provider="fireworks",
        upstream_model="accounts/fireworks/models/deepseek-v4-flash-0731",
        input_cost_per_million=Decimal("0.22"),
        output_cost_per_million=Decimal("0.66"),
        cached_input_cost_per_million=Decimal("0.007"),
    ),
    # Adds the provider-qualified public name recommended for Anthropic routing.
    "anthropic/claude-sonnet-5": ModelRoute(
        provider="anthropic",
        upstream_model="claude-sonnet-5",
        input_cost_per_million=Decimal("2.00"),
        output_cost_per_million=Decimal("10.00"),
        cached_input_cost_per_million=Decimal("2.00"),
    ),
    # Accepts Anthropic's direct model name for clients that already use it.
    "claude-sonnet-5": ModelRoute(
        provider="anthropic",
        upstream_model="claude-sonnet-5",
        input_cost_per_million=Decimal("2.00"),
        output_cost_per_million=Decimal("10.00"),
        cached_input_cost_per_million=Decimal("2.00"),
    ),
}


# Resolves one public model name without guessing from prefixes or substrings.
def get_model_route(model: str) -> ModelRoute | None:
    # Returns the exact configured route or None for an unsupported model.
    return MODEL_ROUTES.get(model)


# Resolves the concrete adapter selected by a trusted model route.
def get_provider_adapter(provider: str) -> ProviderAdapter | None:
    # Returns the registered implementation or None for a configuration error.
    return PROVIDER_ADAPTERS.get(provider)
