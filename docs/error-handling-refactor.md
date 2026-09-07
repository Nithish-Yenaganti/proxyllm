# Shared request rejection helper

The chat-completions handler previously repeated two operations in ten error
branches: record zero-token usage, then create an OpenAI-style error response.
`reject_request` in `api/main.py` now performs those operations together.

## What stays the same

The existing `try`/`except` and `if` checks still decide when to reject a request.
Each branch supplies its original message, HTTP status, error code, usage status,
key identity, routing context, and cache status using named arguments.
The helper awaits `record_request_usage` and returns `gateway_error`'s response.
The endpoint returns that response immediately.

The ten branches cover malformed JSON, a non-object body, an invalid model name,
an unknown model, denied provider permission, three server configuration errors,
adapter request rejection, and provider connection failure.

## Boundaries

This is internal code reuse, not a new endpoint or microservice. It changes no
authentication, rate-limit, caching, or streaming policy. Successful responses,
upstream HTTP responses, and stream-end accounting retain their existing paths.
Usage persistence failures remain best-effort: the existing recorder catches them
so the intended client error can still be returned. The helper is specifically
for failures with no known token usage, not every possible provider response.

## Why

Keeping the two operations together reduces repetition and gives these rejection
paths one place to maintain their accounting behavior. It does not imply that
the previous code was broken, and adding new error cases still requires tests.
