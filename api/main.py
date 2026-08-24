# Imports the helper that loads variables from the local .env file.
from dotenv import load_dotenv

# Gives Python access to environment variables such as the provider API key.
import os

# Sends asynchronous outbound HTTP requests from the proxy to Fireworks.
import httpx

# FastAPI creates the app, Request reads client input, and Response returns provider output.
from fastapi import FastAPI, Request, Response

# Loads the variables from .env into the process environment.
load_dotenv()

# Reads the secret Fireworks API key from the environment.
PROVIDER_API_KEY = os.getenv("FIREWORK_API_KEY")

# Reads the Fireworks chat-completions URL from the environment.
PROVIDER_URL = os.getenv("FIREWORK_URL")

# Creates the FastAPI application that Uvicorn runs.
app = FastAPI()

# Registers a GET route at /, currently used as a simple server check.
@app.get("/")
def read_root():
    # Returns JSON confirming that the server is running.
    return {"message": "Hello, World!"}

# Accepts the same POST path used by OpenAI-compatible chat clients.
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Parses the client's JSON body, including its model, messages, and options.
    body = await request.json()

    # Opens an async HTTP client; the timeout limits the wait for Fireworks to 60 seconds.
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Forwards the client's JSON to Fireworks and authenticates with the server-side key.
        response = await client.post(
            PROVIDER_URL,
            headers={
                "Authorization": f"Bearer {PROVIDER_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
        )

        # Sends Fireworks' raw body, HTTP status, and content type back to the client.
        return Response(content=response.content, 
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
        )
