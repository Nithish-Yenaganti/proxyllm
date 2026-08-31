"""Generate, recognize, and hash virtual API keys."""

# Provides the SHA-256 hash function used before a key reaches SQLite.
import hashlib

# Provides cryptographically secure randomness for authentication secrets.
import secrets


# Marks keys as credentials issued by this proxy.
KEY_PREFIX = "nk_"

# Preserves the suffix selected for this project's virtual-key format.
KEY_SUFFIX = "_y"

# Requests 32 random bytes, which provides 256 bits of secret entropy.
KEY_RANDOM_BYTES = 32


# Creates one unpredictable virtual key for an application.
def generate_virtual_key() -> str:
    # Encodes secure random bytes using characters safe in HTTP headers.
    random_part = secrets.token_urlsafe(KEY_RANDOM_BYTES)

    # Wraps the secret with recognizable project-specific markers.  
    return f"{KEY_PREFIX}{random_part}{KEY_SUFFIX}"


# Converts a plaintext virtual key into the repeatable value stored in SQLite.
def hash_virtual_key(virtual_key: str) -> str:
    # Converts Python text into deterministic UTF-8 bytes for SHA-256.
    encoded_key = virtual_key.encode("utf-8")

    # Returns a 64-character hexadecimal digest instead of the plaintext key.
    return hashlib.sha256(encoded_key).hexdigest()


# Checks the inexpensive public shape before performing a database lookup.
def has_valid_key_format(virtual_key: str) -> bool:
    # Requires the prefix, suffix, and enough room for a non-empty random section.
    return (
        virtual_key.startswith(KEY_PREFIX)
        and virtual_key.endswith(KEY_SUFFIX)
        and len(virtual_key) > len(KEY_PREFIX) + len(KEY_SUFFIX)
    )


# Produces a short non-secret identifier suitable for CLI listings.
def get_key_prefix(virtual_key: str) -> str:
    # Keeps only the first twelve characters so the full secret is never stored.
    return virtual_key[:12]


# Runs this demonstration only when the module is executed directly.
if __name__ == "__main__":
    # Generates one key so the displayed key and hash refer to the same secret.
    generated_key = generate_virtual_key()

    # Prints the key for this explicit local demonstration.
    print(f"Virtual key: {generated_key}")

    # Prints its matching hash to demonstrate deterministic hashing.
    print(f"SHA-256: {hash_virtual_key(generated_key)}")
