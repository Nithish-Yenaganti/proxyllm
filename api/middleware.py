"""Authenticate OpenAI-compatible API requests using virtual keys."""

# Provides a configurable filesystem path for production and isolated tests.
from pathlib import Path

# Provides FastAPI's representation of the incoming HTTP request.
from fastapi import Request

# Provides the middleware base class and downstream handler type.
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

# Provides JSON error responses and the general response type.
from starlette.responses import JSONResponse, Response

# Supplies the default database location and active-key lookup operation.
from auth.database import DATABASE_PATH, get_active_virtual_key_by_hash

# Supplies cheap format checking and deterministic hashing for presented keys.
from auth.keys import has_valid_key_format, hash_virtual_key
from auth.rate_limit import REQUESTS_PER_WINDOW, consume_rate_limit


# Creates the same generic authentication error for every rejected credential.
def unauthorized_response() -> JSONResponse:
    # Uses an OpenAI-style error envelope for compatible client applications.
    error_body = {
        "error": {
            "message": "A valid active virtual API key is required.",
            "type": "authentication_error",
            "code": "invalid_api_key",
        }
    }

    # Returns 401 and advertises the expected HTTP authentication scheme.
    return JSONResponse(
        status_code=401,
        content=error_body,
        headers={"WWW-Authenticate": "Bearer"},
    )


# Returns a stable throttling response without revealing key metadata.
def rate_limited_response(retry_after_seconds: int) -> JSONResponse:
    error_body = {
        "error": {
            "message": (
                f"This virtual API key is limited to {REQUESTS_PER_WINDOW} "
                "requests in any 60-second period."
            ),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }
    }

    return JSONResponse(
        status_code=429,
        content=error_body,
        headers={"Retry-After": str(retry_after_seconds)},
    )


# Protects versioned API routes while leaving the health route public.
class VirtualKeyAuthMiddleware(BaseHTTPMiddleware):
    # Stores the database path used for each authentication lookup.
    def __init__(
        self,
        app: object,
        database_path: Path = DATABASE_PATH,
    ) -> None:
        # Initializes Starlette's middleware machinery with the downstream app.
        super().__init__(app)

        # Saves a configurable path so tests never touch the real database.
        self.database_path = database_path

    # Authenticates one request before allowing its route handler to run.
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # Leaves non-versioned routes such as GET / available for health checks.
        if not request.url.path.startswith("/v1/"):
            # Passes the public request directly to the next application layer.
            return await call_next(request)

        # Reads the caller's Authorization header without logging its secret.
        authorization = request.headers.get("Authorization")

        # Rejects requests that do not present any credential.
        if authorization is None:
            # Returns the generic error without revealing internal details.
            return unauthorized_response()

        # Splits once so the scheme and token can be validated independently.
        scheme, separator, virtual_key = authorization.partition(" ")

        # Requires the standard Bearer scheme and one non-empty token.
        if separator == "" or scheme.lower() != "bearer" or virtual_key == "":
            # Rejects malformed credentials before performing a database query.
            return unauthorized_response()

        # Rejects extra whitespace and credentials that are not gateway keys.
        if " " in virtual_key or not has_valid_key_format(virtual_key):
            # Uses the same response so callers cannot probe key details.
            return unauthorized_response()

        # Recreates the deterministic hash stored when the key was issued.
        key_hash = hash_virtual_key(virtual_key)

        # Queries only active records, making revocation effective immediately.
        key_record = await get_active_virtual_key_by_hash(
            key_hash,
            self.database_path,
        )

        # Rejects unknown and revoked credentials identically.
        if key_record is None:
            # Avoids revealing whether a particular key ever existed.
            return unauthorized_response()

        # Makes safe authorization metadata available to the proxy route.
        request.state.virtual_key = key_record

        # Counts every authenticated protected request, including invalid requests and
        # cache hits, before route parsing or provider work can consume more resources.
        rate_limit = await consume_rate_limit(
            int(key_record["id"]),
            self.database_path,
        )

        # An exhausted request does not add an accepted event or reach the route.
        if not rate_limit.allowed:
            return rate_limited_response(rate_limit.retry_after_seconds)

        # Continues to the requested endpoint after successful authentication.
        return await call_next(request)
