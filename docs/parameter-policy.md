# Model parameter policy

Provider policies apply to every authorized client and configured model under that
provider, including models added later. Model-specific overrides remain available.
It is not a Jam-specific mode. No default temperature is inserted.

## Current behavior

- The configured Fireworks DeepSeek route accepts temperature from 0 to 2,
  top_p from 0 to 1, and an integer top_k from 0 to 100; null is also accepted,
  and invalid values fail locally.
- The Sonnet 5 route requires sampling settings to be omitted unless an
  administrator explicitly allows their removal. This conservative gateway policy
  is stricter than accepting the provider's unspecified default-valued settings.
- Other Anthropic features still follow the existing adapter validation: tools
  and unknown fields are rejected, never removed by this policy.
- Other Fireworks fields retain their existing pass-through behavior, so this is
  sampling validation, not complete model-capability validation.

## Administrator configuration

Strict rejection is the default. To allow removal for all Anthropic models, put this in the
server environment and restart the proxy:

```dotenv
PROXY_DROP_SAMPLING_PARAMS={"anthropic":["temperature","top_p","top_k"]}
```

This example is not automatically enabled. It applies to all configured Anthropic
models and authorized client apps, not Fireworks. It does not grant access or add
model routes. Clients cannot enable removal themselves.
Only these three sampling names are allowed; unknown providers/models, invalid lists, or
conflicting alias policies fail at configuration loading. Set the value back to
`{}` and restart to return to strict rejection.

A model-specific list overrides the provider list rather than merging with it,
regardless of configuration order. An empty model list restores strict handling
for that model; all its aliases share the override. Existing per-model configuration
still works. Provider-wide removal also removes supported sampling settings on
future models: use a model override if you want those controls preserved.

## Inspection and caching

Inspection and adapter preparation use the same sampling function. Inspection
lists `dropped_parameters` with an administrator-policy reason; it does not imply
that omitted controls were honored or that upstream acceptance was tested.
Normal chat clients do not receive this inspection report automatically.

Cache eligibility uses the effective settings after removal: a dropped
`temperature: 0` cannot turn caching on. Explicit cache opt-in remains available
for non-streaming requests. Cache keys use a new version and the effective body
plus provider/upstream-model identity, so older cached results cannot mask this
behavior change. Older rows remain on disk; cleanup is separate.

## Evidence and limits

Checked on 2026-09-10:

- [Anthropic Sonnet 5 migration guide](https://platform.claude.com/docs/en/models/sonnet-5/migration-guide)
  says non-default sampling values are rejected and recommends removing them.
- [Fireworks chat API reference](https://docs.fireworks.ai/api-reference/post-chatcompletions)
  documents the sampling parameters; model-specific upstream behavior remains
  unverified by mock tests.

Tests cover strict rejection, selected removal, invalid values/configuration,
alias consistency, unchanged messages, preview/wire equality for both stream
modes, and cache safety. Real-provider calls are a separate paid verification step.
