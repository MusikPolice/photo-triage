import datetime as dt

from photo_triage.worker.queue import (
    LAST_ERROR_MAX_CHARS,
    PRIORITY,
    RetryPolicy,
    Stage,
    describe_error,
)


def test_every_stage_has_a_priority() -> None:
    assert PRIORITY.keys() == set(Stage)


def test_stage_priorities_follow_the_plan() -> None:
    # plan §6.11: metadata writes > scan > thumbnails > CLIP > quality > faces >
    # batch jobs > LLM tagging. Lower runs first.
    plan_order = [
        [Stage.METADATA_WRITE],
        [Stage.SCAN],
        [Stage.THUMBNAIL],
        [Stage.CLIP],
        [Stage.QUALITY],
        [Stage.FACES, Stage.RECOGNIZE],
        [Stage.LAYOUT, Stage.DUPLICATES, Stage.ATLASES],
        [Stage.LLM_TAG],
    ]
    for group in plan_order:
        assert len({PRIORITY[s] for s in group}) == 1, group
    group_priorities = [PRIORITY[group[0]] for group in plan_order]
    assert group_priorities == sorted(set(group_priorities))


def test_default_backoff_doubles_from_one_minute() -> None:
    policy = RetryPolicy()
    waits = [policy.backoff(n) for n in range(1, policy.max_attempts)]
    assert waits == [dt.timedelta(minutes=m) for m in (1, 2, 4, 8)]


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(first_backoff=dt.timedelta(minutes=10), max_backoff=dt.timedelta(hours=1))
    assert policy.backoff(4) == dt.timedelta(hours=1)
    assert policy.backoff(40) == dt.timedelta(hours=1)


def _raise(message: str) -> BaseException:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


def test_errors_are_described_with_their_traceback() -> None:
    text = describe_error(_raise("corrupt file"))
    assert text.startswith("Traceback")
    assert text.endswith("ValueError: corrupt file")


def test_long_errors_keep_their_end() -> None:
    text = describe_error(_raise("x" * 10_000 + " the end"))
    assert len(text) == LAST_ERROR_MAX_CHARS
    assert text.startswith("…")
    assert text.endswith(" the end")
