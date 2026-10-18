"""Shared entry point for running one write request end to end.

Both HTTP adapters call ``run_request_service`` once per write request. It owns the
outermost transaction, the bounded retry policy, and the idempotency-race resolution:
losing a race means the winning transaction already committed, so the correct recovery is
to run the *same* service again in a fresh transaction, where the normal replay path
re-applies current read authorisation to the historical receipt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

from domain.errors import IdempotencyRaceLost
from domain.transactions import run_write_service

logger = logging.getLogger("domain.service_runner")

T = TypeVar("T")

MAX_RACE_ATTEMPTS = 3


def run_request_service(
    callback: Callable[[], T],
    *,
    global_claim_lock: bool = False,
    max_attempts: int | None = None,
    require_write_lock: bool = True,
) -> T:
    last_race: IdempotencyRaceLost | None = None
    for attempt in range(MAX_RACE_ATTEMPTS):
        try:
            return run_write_service(
                callback,
                global_claim_lock=global_claim_lock,
                max_attempts=max_attempts,
                require_write_lock=require_write_lock,
            )
        except IdempotencyRaceLost as race:
            last_race = race
            logger.info(
                "同一幂等键发生并发竞争，重开事务读取获胜回执（第 %s 次）", attempt + 1
            )
    assert last_race is not None
    raise last_race
