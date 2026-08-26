# Generates cryptographically secure random values for virtual keys.
import secrets


# Creates one virtual key and returns it as a string.
def generate_virtual_key() -> str:
    # Produces 32 secure random bytes encoded with URL-safe characters.
    random_bytes = secrets.token_urlsafe(32)

    # Adds the project-specific prefix and suffix to identify this key type.
    return f"nk_{random_bytes}_y"


# Runs only when this file is executed directly, not when another module imports it.
if __name__ == "__main__":
    # Prints the newly generated key once so it can be copied into an application.
    print(generate_virtual_key())
