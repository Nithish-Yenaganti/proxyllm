"""Enforce a persistent exact sliding-window request limit for virtual keys."""

import math
import time
from dataclasses import dataclass
from pathlib import Path

from auth.database import DATABASE_PATH, configure_database_connection, open_database


# Applies the agreed rolling policy independently to each authenticated virtual key.
REQUESTS_PER_WINDOW = 24
WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RateLimitDecision:
    """Describe whether one authenticated request may enter the gateway."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


async def consume_rate_limit(
    virtual_key_id: int,
    database_path: Path = DATABASE_PATH,
    *,
    current_time_unix: float | None = None,
    limit: int = REQUESTS_PER_WINDOW,
    window_seconds: int = WINDOW_SECONDS,
) -> RateLimitDecision:
    """Atomically consume one request from a key's preceding sliding window."""

    if limit < 1 or window_seconds < 1:
        raise ValueError("rate-limit settings must be positive")

    observed_time = time.time() if current_time_unix is None else current_time_unix
    cutoff_time = observed_time - window_seconds

    async with open_database(database_path) as database:
        await configure_database_connection(database)

        # Acquires the SQLite writer lock before reading. Every worker sharing this
        # file therefore makes the prune/count/insert decision in serial order.
        await database.execute("BEGIN IMMEDIATE")

        prune_cursor = await database.execute(
            """
            DELETE FROM rate_limit_events
            WHERE virtual_key_id = ? AND accepted_at_unix <= ?
            """,
            (virtual_key_id, cutoff_time),
        )
        await prune_cursor.close()

        count_cursor = await database.execute(
            """
            SELECT COUNT(*), MIN(accepted_at_unix)
            FROM rate_limit_events
            WHERE virtual_key_id = ?
            """,
            (virtual_key_id,),
        )
        count, oldest_accepted_at = await count_cursor.fetchone()
        await count_cursor.close()

        if int(count) < limit:
            insert_cursor = await database.execute(
                """
                INSERT INTO rate_limit_events (virtual_key_id, accepted_at_unix)
                VALUES (?, ?)
                """,
                (virtual_key_id, observed_time),
            )
            await insert_cursor.close()
            request_count = int(count) + 1
            allowed = True
            retry_after = 0
        else:
            # The oldest surviving request is the first one that can free capacity.
            next_capacity_at = float(oldest_accepted_at) + window_seconds
            retry_after = max(math.ceil(next_capacity_at - observed_time), 1)
            request_count = int(count)
            allowed = False

        # A rejected request commits pruning only; it never inserts its own timestamp.
        await database.commit()

    remaining = max(limit - request_count, 0)

    return RateLimitDecision(
        allowed=allowed,
        limit=limit,
        remaining=remaining,
        retry_after_seconds=retry_after,
    )
