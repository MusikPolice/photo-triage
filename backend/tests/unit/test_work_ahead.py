"""A stage's ETA counts the work queued ahead of it (`work_ahead`, plan §7, §6.11)."""

import pytest

from photo_triage.api.activity import StageActivity, work_ahead
from photo_triage.worker.queue import BATCH_STAGES, Stage


def _row(stage: Stage, pending: int = 0, *, paused: bool = False) -> StageActivity:
    return StageActivity(
        stage=stage,
        kind="batch" if stage in BATCH_STAGES else "item",
        paused=paused,
        done=0,
        total=pending,
        pending=pending,
        running=0,
        errored=0,
        parked=0,
    )


def test_a_later_stage_adds_the_work_of_the_stages_before_it() -> None:
    rows = [_row(Stage.SCAN, 120), _row(Stage.THUMBNAIL, 60), _row(Stage.CLIP, 30)]
    rates = {Stage.SCAN: 60.0, Stage.THUMBNAIL: 30.0, Stage.CLIP: 10.0}

    ahead = work_ahead(rows, rates)

    assert ahead == {
        Stage.SCAN: (2.0, False),
        Stage.THUMBNAIL: (pytest.approx(4.0), False),
        Stage.CLIP: (pytest.approx(7.0), False),
    }


def test_stages_in_one_tier_are_done_together() -> None:
    # Scan and metadata reads share a priority, so the worker interleaves them.
    rows = [_row(Stage.SCAN, 60), _row(Stage.METADATA_READ, 120)]
    rates = {Stage.SCAN: 60.0, Stage.METADATA_READ: 120.0}

    ahead = work_ahead(rows, rates)

    assert ahead[Stage.SCAN] == ahead[Stage.METADATA_READ] == (2.0, False)


def test_a_stage_ahead_without_a_throughput_makes_it_a_minimum() -> None:
    rows = [_row(Stage.SCAN, 100), _row(Stage.CLIP, 30)]

    ahead = work_ahead(rows, {Stage.CLIP: 10.0})

    assert ahead[Stage.CLIP] == (3.0, True)


def test_a_paused_stage_ahead_still_counts() -> None:
    rows = [_row(Stage.SCAN, 60, paused=True), _row(Stage.CLIP, 10)]

    ahead = work_ahead(rows, {Stage.SCAN: 60.0, Stage.CLIP: 10.0})

    assert ahead[Stage.CLIP] == (2.0, False)


def test_stages_without_work_get_nothing_and_batch_stages_are_left_out() -> None:
    rows = [_row(Stage.WALK, 1), _row(Stage.SCAN, 0), _row(Stage.CLIP, 10)]

    ahead = work_ahead(rows, {Stage.SCAN: 60.0, Stage.CLIP: 10.0})

    assert ahead == {Stage.CLIP: (1.0, False)}
