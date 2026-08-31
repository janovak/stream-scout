"""Deterministic, validation-only ranking fixtures for feature 006."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import AsyncIterator, Iterator, Literal, Tuple

PAGE_SIZE = 100
SUPPORTED_SCALES = (50, 500, 900)


@dataclass(frozen=True)
class RankingRecord:
    """A Twitch-like ranking row with its measured clipping eligibility."""

    streamer_id: int
    login: str
    allows_clipping: bool
    raw_rank: int
    rank: int | None

    @property
    def user_id(self) -> str:
        return str(self.streamer_id)

    @property
    def user_login(self) -> str:
        return self.login


@dataclass(frozen=True)
class RankingFixture:
    """A complete paginated input and its test-only poll configuration."""

    scale: int
    fixture_id: Literal["A", "B"]
    records: Tuple[RankingRecord, ...]
    eligible_records: Tuple[RankingRecord, ...]
    pages: Tuple[Tuple[RankingRecord, ...], ...]
    disabled_records: int
    disabled_proportion: float
    page_size: int
    page_delay_ms: float
    test_join_threshold: int
    test_leave_threshold: int
    test_fetch_buffer: int

    def iter_pages(self) -> Iterator[Tuple[RankingRecord, ...]]:
        """Iterate pages without delay, for setup and deterministic tests."""

        return iter(self.pages)

    async def delayed_pages(self) -> AsyncIterator[Tuple[RankingRecord, ...]]:
        """Yield every raw page with the calibrated delay before each response."""

        delay = self.page_delay_ms / 1000.0
        for page in self.pages:
            if delay:
                await asyncio.sleep(delay)
            yield page

    async def get_streams(self, *, first: int = PAGE_SIZE) -> AsyncIterator[RankingRecord]:
        """Expose the small Twitch surface consumed by a ranking integration."""

        if first != self.page_size:
            raise ValueError(f"fixture page size is fixed at {self.page_size}")
        async for page in self.delayed_pages():
            for record in page:
                yield record


def _disabled_count(scale: int, proportion: float) -> int:
    if not math.isfinite(proportion) or proportion < 0.0 or proportion >= 1.0:
        raise ValueError("disabled_proportion must be finite and in [0, 1)")
    if proportion == 0.0:
        return 0
    # The disabled share is D / (scale + D). Nearest integer is the least
    # biased deterministic representation of the live calibrated proportion.
    return max(1, int(math.floor(scale * proportion / (1.0 - proportion) + 0.5)))


def build_ranking_fixture(
    scale: int,
    fixture_id: str,
    *,
    disabled_proportion: float,
    page_delay_ms: float,
) -> RankingFixture:
    """Build an exact eligible ranking with deterministic disabled injection.

    A and B use non-overlapping numeric ranges and login prefixes. Disabled
    rows are distributed through the raw ranking rather than collected in a
    synthetic tail, and the eligible rows are ranked after filtering.
    """

    if scale not in SUPPORTED_SCALES:
        raise ValueError(f"scale must be one of {SUPPORTED_SCALES}")
    normalized_id = fixture_id.upper()
    if normalized_id not in {"A", "B"}:
        raise ValueError("fixture_id must be A or B")
    fixture_key: Literal["A", "B"] = "A" if normalized_id == "A" else "B"
    if not math.isfinite(page_delay_ms) or page_delay_ms < 0:
        raise ValueError("page_delay_ms must be a finite non-negative value")

    disabled_count = _disabled_count(scale, disabled_proportion)
    raw_count = scale + disabled_count
    base = 6_000_000_000 if fixture_key == "A" else 7_000_000_000
    prefix = f"feature006-{fixture_key.lower()}"
    records = []
    eligible = []

    for index in range(raw_count):
        disabled = (
            (index + 1) * disabled_count // raw_count
            > index * disabled_count // raw_count
        )
        eligible_rank = None if disabled else len(eligible) + 1
        record = RankingRecord(
            streamer_id=base + index + 1,
            login=f"{prefix}-{index + 1:04d}",
            allows_clipping=not disabled,
            raw_rank=index + 1,
            rank=eligible_rank,
        )
        records.append(record)
        if not disabled:
            eligible.append(record)

    if len(eligible) != scale:
        raise AssertionError("fixture construction did not preserve exact eligibility")
    pages = tuple(
        tuple(records[offset : offset + PAGE_SIZE])
        for offset in range(0, raw_count, PAGE_SIZE)
    )
    actual_proportion = disabled_count / raw_count
    return RankingFixture(
        scale=scale,
        fixture_id=fixture_key,
        records=tuple(records),
        eligible_records=tuple(eligible),
        pages=pages,
        disabled_records=disabled_count,
        disabled_proportion=actual_proportion,
        page_size=PAGE_SIZE,
        page_delay_ms=float(page_delay_ms),
        test_join_threshold=scale,
        test_leave_threshold=scale,
        test_fetch_buffer=disabled_count,
    )
