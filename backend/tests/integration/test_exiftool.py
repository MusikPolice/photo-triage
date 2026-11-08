"""The persistent exiftool process the worker reads metadata through (plan §5)."""

import logging
import os
import signal
from pathlib import Path

import pytest

from photo_triage.files import ExifTool, MetadataError

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
KEYWORDS = SYNTHETIC / "metadata/keywords.jpg"


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_reads_tags_named_by_group_with_both_group_families(exiftool: ExifTool) -> None:
    tags = exiftool.read(KEYWORDS, ["IPTC:Keywords", "XMP-dc:Subject", "DateTimeOriginal"])

    assert tags == {
        "IPTC:Keywords": ["beach", "sunset", "café"],
        "XMP:XMP-dc:Subject": ["beach", "sunset", "café"],
        "EXIF:ExifIFD:DateTimeOriginal": "2024:09:01 12:00:00",
    }


def test_one_process_serves_every_read(exiftool: ExifTool) -> None:
    assert exiftool.pid is None  # started by the first read
    exiftool.read(KEYWORDS, ["IPTC:Keywords"])
    pid = exiftool.pid

    for path in sorted(SYNTHETIC.glob("scenes/*.jpg")):
        assert exiftool.read(path, ["DateTimeOriginal"])

    assert pid is not None and exiftool.pid == pid


def test_a_file_with_none_of_the_tags_gives_nothing(exiftool: ExifTool) -> None:
    assert exiftool.read(SYNTHETIC / "dates/no-dates.jpg", ["DateTimeOriginal"]) == {}


@pytest.mark.parametrize("name", ["été 2024.jpg", "-execute.jpg", "-q"])
def test_a_name_that_looks_like_an_option_is_still_a_file(
    exiftool: ExifTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    (tmp_path / name).write_bytes(KEYWORDS.read_bytes())
    monkeypatch.chdir(tmp_path)

    assert exiftool.read(Path(name), ["IPTC:Keywords"]) == {
        "IPTC:Keywords": ["beach", "sunset", "café"]
    }


def test_a_name_with_a_line_break_is_refused(exiftool: ExifTool, tmp_path: Path) -> None:
    odd = tmp_path / "two\n-execute\nlines.jpg"
    odd.write_bytes(KEYWORDS.read_bytes())

    with pytest.raises(MetadataError, match="line break"):
        exiftool.read(odd, ["IPTC:Keywords"])


def test_a_missing_file_raises_and_the_process_carries_on(exiftool: ExifTool) -> None:
    exiftool.read(KEYWORDS, ["IPTC:Keywords"])
    pid = exiftool.pid

    with pytest.raises(MetadataError, match="File not found"):
        exiftool.read(SYNTHETIC / "nothing-here.jpg", ["IPTC:Keywords"])

    assert exiftool.read(KEYWORDS, ["IPTC:Keywords"])
    assert exiftool.pid == pid


def test_a_process_that_died_is_started_again(
    exiftool: ExifTool, caplog: pytest.LogCaptureFixture
) -> None:
    exiftool.read(KEYWORDS, ["IPTC:Keywords"])
    pid = exiftool.pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)  # so it's seen to have exited, not just signalled

    assert exiftool.read(KEYWORDS, ["IPTC:Keywords"]) == {
        "IPTC:Keywords": ["beach", "sunset", "café"]
    }
    assert exiftool.pid not in (None, pid)
    assert "exited" in caplog.text and "starting it again" in caplog.text


def test_a_process_that_dies_during_a_read_fails_that_read_only(exiftool: ExifTool) -> None:
    exiftool.read(KEYWORDS, ["IPTC:Keywords"])
    pid = exiftool.pid
    assert pid is not None
    os.kill(pid, signal.SIGSTOP)  # so it can't answer, then dies
    try:
        exiftool._timeout_s = 0.5  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(MetadataError, match=r"no answer in 0\.5 s"):
            exiftool.read(KEYWORDS, ["IPTC:Keywords"])
    finally:
        if not _gone(pid):
            os.kill(pid, signal.SIGCONT)
    assert _gone(pid)  # killed and reaped
    exiftool._timeout_s = 30  # pyright: ignore[reportPrivateUsage]

    assert exiftool.read(KEYWORDS, ["IPTC:Keywords"])
    assert exiftool.pid not in (None, pid)


def test_close_stops_the_process(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="photo_triage.files.exiftool")
    with ExifTool() as exiftool:
        exiftool.read(KEYWORDS, ["IPTC:Keywords"])
        pid = exiftool.pid
        assert pid is not None

    assert exiftool.pid is None
    assert _gone(pid)
    assert f"Stopped exiftool (pid {pid})" in caplog.text
    exiftool.close()  # again, harmlessly
