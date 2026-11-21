"""Who a worker command from the shell is recorded as (`--actor`'s default)."""

import getpass
import os

import pytest

from photo_triage.worker.__main__ import default_actor


def test_the_actor_is_the_user_s_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(getpass, "getuser", lambda: "jo")

    assert default_actor() == "jo"


def test_a_uid_with_no_name_is_recorded_by_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """As in the container when APP_UID is a UID the image has no user for."""

    def nameless() -> str:
        raise OSError("No username set in the environment")

    monkeypatch.setattr(getpass, "getuser", nameless)

    assert default_actor() == f"uid-{os.getuid()}"
