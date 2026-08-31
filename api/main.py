"""Run the authenticated OpenAI-compatible ProxyLLM HTTP API."""

# Provides an asynchronous startup and shutdown context for FastAPI.
from contextlib import asynccontextmanager

# Reads provider credentials and URLs from the process environment.
import os

# Sends non-blocking outbound HTTP requests to the selected provider.
import httpx

# Loads local development variables from the ignored .env file.
from dotenv import load_dotenv

# Provides the web application, incoming request, and outgoing response types.
from fastapi import FastAPI, Request, Response

# Provides structured JSON errors for authorization and configuration failures.
from fastapi.responses import JSONResponse

# Protects /v1/* routes with active virtual API keys.
from api.middleware import VirtualKeyAuthMiddleware

# Ensures the authentication database exists when the server starts.
from auth.database import initialize_database


# Loads .env values without displaying or logging their contents.
load_dotenv()


# Maps a permitted database reference to the real server-side Fireworks settings.
PROVIDER_CREDENTIALS = {
    # Allows records assigned to fireworks/default to use this environment credential.
    ("fireworks", "default"): {
        # Reads the secret that will authenticate the outbound provider request.
        "api_key": os.getenv("FIREWORK_API_KEY"),
        # Reads the provider's chat-completions endpoint.
        "url": os.getenv("FIREWORK_URL"),
    }
}


# Performs one-time asynchronous application startup work.
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Creates the SQLite file and virtual_keys table when they are missing.
    await initialize_database()

    # Hands control to FastAPI for the lifetime of the running server.
    yield


# Creates the Uvicorn-served FastAPI application with database startup enabled.
app = FastAPI(lifespan=lifespan)

# Applies virtual-key authentication before requests reach protected routes.
app.add_middleware(VirtualKeyAuthMiddleware)


# Registers a public route that confirms the process is reachable.
@app.get("/")
def read_root():
    # Returns a small JSON health response without requiring authentication.
    return {"message": "Hello, World!"}


# Accepts the OpenAI-compatible chat-completions path used by client applications.
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Reads safe metadata attached by the successful authentication middleware.
    virtual_key_record = request.state.virtual_key

    # Builds the exact provider permission reference stored for this virtual key.
    provider_reference = (
        str(virtual_key_record["provider"]),
        str(virtual_key_record["provider_credential"]),
    )

    # Resolves the permitted server-side credential without trusting client input.
    provider_config = PROVIDER_CREDENTIALS.get(provider_reference)

    # Rejects a valid key that is not authorized for a configured provider credential.
    if provider_config is None:
        # Uses 403 because authentication succeeded but authorization did not.
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "This virtual key cannot use the requested provider.",
                    "type": "authorization_error",
                    "code": "provider_not_allowed",
                }
            },
        )

    # Reads the resolved provider URL from trusted server configuration.
    provider_url = provider_config["url"]

    # Reads the resolved real API key from trusted server configuration.
    provider_api_key = provider_config["api_key"]

    # Detects incomplete server configuration before attempting an outbound call.
    if provider_url is None or provider_api_key is None:
        # Returns a sanitized error without exposing environment details.
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "The authorized provider is not configured.",
                    "type": "server_configuration_error",
                    "code": "provider_not_configured",
                }
            },
        )

    # Parses the client's JSON model, messages, and generation options.
    body = await request.json()

    # Opens an asynchronous client with a bounded provider wait time.
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Converts provider network failures into a stable gateway response.
        try:
            # Forwards the unchanged JSON while replacing the virtual key with the real key.
            provider_response = await client.post(
                provider_url,
                headers={
                    "Authorization": f"Bearer {provider_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )

        # Handles DNS, connection, TLS, and timeout failures from httpx.
        except httpx.RequestError:
            # Returns 502 because the proxy could not obtain a provider response.
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": "The upstream provider could not be reached.",
                        "type": "provider_connection_error",
                        "code": "provider_unavailable",
                    }
                },
            )

    # Reads the provider content type so compatible clients parse its body correctly.
    provider_content_type = provider_response.headers.get(
        "content-type",
        "application/json",
    )

    # Returns the provider's raw body and status without exposing internal credentials.
    return Response(
        content=provider_response.content,
        status_code=provider_response.status_code,
        headers={"Content-Type": provider_content_type},
    )
