"""Verify HTTP authentication for missing, valid, and revoked keys."""

# Supplies temporary storage that cannot affect production key records.
import tempfile

# Supplies Python's asynchronous unittest support.
import unittest

# Builds the isolated SQLite path used by each test.
from pathlib import Path

# Provides an in-process FastAPI application for middleware verification.
from fastapi import FastAPI, Request

# Sends requests directly to the test app without opening a network port.
from httpx import ASGITransport, AsyncClient

# Supplies the authentication layer under test.
from api.middleware import VirtualKeyAuthMiddleware

# Supplies database creation, insertion, and revocation operations.
from auth.database import (
    create_virtual_key_record,
    initialize_database,
    revoke_virtual_key_record,
)

# Supplies secure key fixtures and their safe database representations.
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key


# Exercises middleware behavior through real HTTP-shaped test requests.
class VirtualKeyMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    # Creates an isolated app, database, and active key for each test method.
    async def asyncSetUp(self) -> None:
        # Owns a temporary directory until asyncTearDown runs.
        self.temporary_directory = tempfile.TemporaryDirectory()

        # Keeps middleware test data separate from the real gateway database.
        self.database_path = Path(self.temporary_directory.name) / "test.db"

        # Creates the isolated virtual_keys table.
        await initialize_database(self.database_path)

        # Generates the valid plaintext credential used only by this test.
        self.virtual_key = generate_virtual_key()

        # Persists only the key's hash and safe metadata.
        self.record_id = await create_virtual_key_record(
            "test-app",
            get_key_prefix(self.virtual_key),
            hash_virtual_key(self.virtual_key),
            "fireworks",
            "default",
            self.database_path,
        )

        # Creates a small application dedicated to middleware behavior.
        self.app = FastAPI()

        # Configures middleware to use the temporary database.
        self.app.add_middleware(
            VirtualKeyAuthMiddleware,
            database_path=self.database_path,
        )

        # Defines a public health route for bypass verification.
        @self.app.get("/")
        async def public_route():
            # Returns a marker proving middleware allowed the public request.
            return {"public": True}

        # Defines one protected route that exposes only safe authentication metadata.
        @self.app.get("/v1/protected")
        async def protected_route(request: Request):
            # Returns the authenticated application name attached by middleware.
            return {"app_name": request.state.virtual_key["app_name"]}

        # Connects httpx directly to the ASGI app in this Python process.
        transport = ASGITransport(app=self.app)

        # Creates the asynchronous HTTP client used by each assertion.
        self.client = AsyncClient(transport=transport, base_url="http://test")

    # Closes network-test resources and removes temporary storage.
    async def asyncTearDown(self) -> None:
        # Closes httpx cleanly after every test method.
        await self.client.aclose()

        # Deletes the isolated SQLite database and its directory.
        self.temporary_directory.cleanup()

    # Confirms the health route stays public.
    async def test_public_route_requires_no_key(self) -> None:
        # Calls the route without an Authorization header.
        response = await self.client.get("/")

        # Confirms middleware deliberately bypassed the public path.
        self.assertEqual(response.status_code, 200)

    # Confirms every invalid authentication shape receives 401.
    async def test_missing_and_invalid_keys_are_rejected(self) -> None:
        # Sends no Authorization header.
        missing_response = await self.client.get("/v1/protected")

        # Sends the wrong HTTP authentication scheme.
        wrong_scheme_response = await self.client.get(
            "/v1/protected",
            headers={"Authorization": f"Basic {self.virtual_key}"},
        )

        # Sends a correctly shaped but unknown virtual key.
        unknown_response = await self.client.get(
            "/v1/protected",
            headers={"Authorization": "Bearer nk_unknown_y"},
        )

        # Confirms the missing credential was rejected.
        self.assertEqual(missing_response.status_code, 401)

        # Confirms the unsupported scheme was rejected.
        self.assertEqual(wrong_scheme_response.status_code, 401)

        # Confirms the unknown credential was rejected.
        self.assertEqual(unknown_response.status_code, 401)

    # Confirms a valid active key reaches the protected route.
    async def test_active_key_is_accepted(self) -> None:
        # Sends the issued key using the standard Bearer scheme.
        response = await self.client.get(
            "/v1/protected",
            headers={"Authorization": f"Bearer {self.virtual_key}"},
        )

        # Confirms authentication allowed the route to run.
        self.assertEqual(response.status_code, 200)

        # Confirms the route received the correct safe application identity.
        self.assertEqual(response.json()["app_name"], "test-app")

    # Confirms revocation takes effect on the next request.
    async def test_revoked_key_is_rejected(self) -> None:
        # Changes the active database record to revoked.
        await revoke_virtual_key_record(self.record_id, self.database_path)

        # Attempts authentication with the previously valid plaintext key.
        response = await self.client.get(
            "/v1/protected",
            headers={"Authorization": f"Bearer {self.virtual_key}"},
        )

        # Confirms immediate database lookup enforces revocation.
        self.assertEqual(response.status_code, 401)

    # Confirms the middleware stops an authenticated key before route execution.
    async def test_25th_authenticated_request_is_rate_limited(self) -> None:
        # Public and failed-authentication requests do not consume this key's capacity.
        await self.client.get("/")
        await self.client.get(
            "/v1/protected",
            headers={"Authorization": "Bearer nk_unknown_y"},
        )

        # A valid key consumes capacity even when route handling later returns 404.
        authenticated_missing_route = await self.client.get(
            "/v1/not-configured",
            headers={"Authorization": f"Bearer {self.virtual_key}"},
        )

        allowed_responses = [
            await self.client.get(
                "/v1/protected",
                headers={"Authorization": f"Bearer {self.virtual_key}"},
            )
            for _ in range(23)
        ]

        rejected_response = await self.client.get(
            "/v1/protected",
            headers={"Authorization": f"Bearer {self.virtual_key}"},
        )

        self.assertEqual(authenticated_missing_route.status_code, 404)
        self.assertTrue(
            all(response.status_code == 200 for response in allowed_responses)
        )
        self.assertEqual(rejected_response.status_code, 429)
        self.assertEqual(
            rejected_response.json()["error"]["code"],
            "rate_limit_exceeded",
        )
        self.assertGreaterEqual(int(rejected_response.headers["Retry-After"]), 1)
        self.assertLessEqual(int(rejected_response.headers["Retry-After"]), 60)


# Runs this test module directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest's test discovery and result reporting.
    unittest.main()
