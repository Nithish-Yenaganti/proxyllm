"""Manage virtual API keys from a trusted local terminal."""

# Parses create, list, and revoke commands without adding another dependency.
import argparse

# Runs asynchronous SQLite functions from this synchronous command-line entry point.
import asyncio

# Supplies the database operations used by each administrator command.
from auth.database import (
    create_virtual_key_record,
    list_virtual_key_records,
    revoke_virtual_key_record,
)

# Supplies secure generation, safe identification, and one-way hashing helpers.
from auth.keys import generate_virtual_key, get_key_prefix, hash_virtual_key


# Builds the accepted CLI commands and options.
def build_parser() -> argparse.ArgumentParser:
    # Creates the top-level parser and its help text.
    parser = argparse.ArgumentParser(
        description="Create, list, and revoke ProxyLLM virtual keys."
    )

    # Requires the caller to choose one management command.
    commands = parser.add_subparsers(dest="command", required=True)

    # Defines the command that issues a new application key.
    create_parser = commands.add_parser("create", help="Issue a new virtual key.")

    # Requires a human-readable owner label such as jan or second-app.
    create_parser.add_argument("--app", required=True, help="Application name.")

    # Defaults this phase to the only currently supported provider.
    create_parser.add_argument(
        "--provider",
        default="fireworks",
        help="Allowed provider name.",
    )

    # Identifies which server-side provider credential may be resolved.
    create_parser.add_argument(
        "--credential",
        default="default",
        help="Allowed provider credential name.",
    )

    # Defines the command that displays non-secret key metadata.
    commands.add_parser("list", help="List issued virtual-key metadata.")

    # Defines the command that disables one key by its stable numeric ID.
    revoke_parser = commands.add_parser("revoke", help="Revoke one virtual key.")

    # Requires the unambiguous database ID shown by the list command.
    revoke_parser.add_argument("--id", required=True, type=int, help="Key record ID.")

    # Returns the completed parser to the CLI entry point.
    return parser


# Generates one key, saves only its hash, and displays the secret once.
async def create_key(app_name: str, provider: str, credential: str) -> None:
    # Generates a high-entropy plaintext value for the application.
    virtual_key = generate_virtual_key()

    # Derives the only secret representation that SQLite will retain.
    key_hash = hash_virtual_key(virtual_key)

    # Derives a short non-secret identifier for administrator listings.
    key_prefix = get_key_prefix(virtual_key)

    # Inserts safe metadata and receives the new record's revocation ID.
    record_id = await create_virtual_key_record(
        app_name=app_name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        provider=provider,
        provider_credential=credential,
    )

    # Confirms which application owns the new record.
    print(f"Created virtual key {record_id} for {app_name}.")

    # Displays the plaintext exactly once so the administrator can copy it.
    print(f"Key: {virtual_key}")

    # Warns that the database cannot recover the plaintext later.
    print("Save this key now; it will not be shown again.")


# Displays issued key metadata without printing secrets or hashes.
async def list_keys() -> None:
    # Loads safe fields for every active and revoked record.
    records = await list_virtual_key_records()

    # Handles a fresh database without printing an empty table.
    if not records:
        # Gives the administrator a clear empty-state message.
        print("No virtual keys have been issued.")

        # Stops this command after handling the empty state.
        return

    # Prints one compact header for the metadata table.
    print("ID | APP | PREFIX | PROVIDER | CREDENTIAL | ACTIVE")

    # Visits every safe record returned by SQLite.
    for record in records:
        # Converts SQLite's integer flag into an understandable word.
        active = "yes" if record["is_active"] == 1 else "no"

        # Displays only fields that cannot authenticate a request by themselves.
        print(
            f'{record["id"]} | {record["app_name"]} | '
            f'{record["key_prefix"]}... | {record["provider"]} | '
            f'{record["provider_credential"]} | {active}'
        )


# Revokes one record and reports whether a change occurred.
async def revoke_key(record_id: int) -> None:
    # Attempts an atomic active-to-revoked database update.
    was_revoked = await revoke_virtual_key_record(record_id)

    # Reports the successful security state change.
    if was_revoked:
        # Identifies the record that can no longer authenticate.
        print(f"Revoked virtual key {record_id}.")

        # Stops after the successful branch.
        return

    # Explains that no active record matched the supplied ID.
    raise SystemExit(f"No active virtual key found with ID {record_id}.")


# Routes parsed arguments to the matching asynchronous operation.
async def run_command(arguments: argparse.Namespace) -> None:
    # Handles key issuance.
    if arguments.command == "create":
        # Passes administrator-supplied ownership and permission metadata.
        await create_key(arguments.app, arguments.provider, arguments.credential)

        # Stops dispatch after the selected command finishes.
        return

    # Handles safe metadata listing.
    if arguments.command == "list":
        # Prints all records without revealing credential material.
        await list_keys()

        # Stops dispatch after the selected command finishes.
        return

    # Handles independent key revocation.
    if arguments.command == "revoke":
        # Uses the numeric ID selected from the list command.
        await revoke_key(arguments.id)


# Runs the CLI only when invoked as `python -m auth.cli`.
if __name__ == "__main__":
    # Builds the parser and converts terminal text into structured arguments.
    parsed_arguments = build_parser().parse_args()

    # Starts the event loop and waits for the chosen database operation.
    asyncio.run(run_command(parsed_arguments))
