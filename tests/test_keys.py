"""Verify secure virtual-key generation and deterministic hashing."""

# Supplies Python's built-in test framework.
import unittest

# Supplies the key helpers exercised by this test case.
from auth.keys import (
    generate_virtual_key,
    get_key_prefix,
    has_valid_key_format,
    hash_virtual_key,
)


# Groups all framework-independent key utility tests.
class KeyUtilityTests(unittest.TestCase):
    # Confirms newly issued keys are distinct and recognizable.
    def test_generated_keys_are_unique_and_well_formed(self) -> None:
        # Generates the first cryptographically secure sample.
        first_key = generate_virtual_key()

        # Generates a second independent sample.
        second_key = generate_virtual_key()

        # Verifies that two issuance operations do not reuse a secret.
        self.assertNotEqual(first_key, second_key)

        # Verifies that both generated values satisfy the public key format.
        self.assertTrue(has_valid_key_format(first_key))
        self.assertTrue(has_valid_key_format(second_key))

    # Confirms SHA-256 is repeatable for one key and distinct across keys.
    def test_hashing_is_deterministic(self) -> None:
        # Uses a non-secret fixture that is safe to keep in source control.
        first_key = "nk_test_key_one_y"

        # Uses a different non-secret fixture for the collision check.
        second_key = "nk_test_key_two_y"

        # Hashes the first fixture once.
        first_hash = hash_virtual_key(first_key)

        # Hashes the same fixture a second time.
        repeated_hash = hash_virtual_key(first_key)

        # Hashes the different fixture.
        second_hash = hash_virtual_key(second_key)

        # Proves the same presented key can find the same stored database value.
        self.assertEqual(first_hash, repeated_hash)

        # Proves different fixture keys do not authenticate as one another.
        self.assertNotEqual(first_hash, second_hash)

        # Confirms SHA-256's hexadecimal representation has the expected length.
        self.assertEqual(len(first_hash), 64)

    # Confirms display prefixes cannot reveal the complete secret.
    def test_display_prefix_is_shorter_than_the_key(self) -> None:
        # Generates a realistic secret for the prefix operation.
        virtual_key = generate_virtual_key()

        # Derives the value that SQLite and the list command may safely expose.
        display_prefix = get_key_prefix(virtual_key)

        # Confirms the stored identifier is only twelve characters long.
        self.assertEqual(len(display_prefix), 12)

        # Confirms the identifier is not equal to the authentication secret.
        self.assertNotEqual(display_prefix, virtual_key)


# Runs this test module directly when requested from the terminal.
if __name__ == "__main__":
    # Starts unittest's test discovery and result reporting.
    unittest.main()
