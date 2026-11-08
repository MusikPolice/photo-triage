"""One persistent exiftool process (`-stay_open`), which the worker reads metadata
through (plan §5).

It only reads for now, but Phase 4's writes will go through the same process, and
the file-mutation check bans naming the executable outside this package
(dev-environment §7). Other packages see it as a `MetadataReader`.

Starting exiftool costs a Perl interpreter and its modules, about a tenth of a
second, so one process serves every read. It starts on the first read, is started
again on the next read if it dies, and stops on `close`.
"""

import json
import logging
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Protocol, Self

logger = logging.getLogger(__name__)

CONFIG = Path(__file__).with_name("exiftool.config")
"""The XMP-phototriage namespace (plan §6.10), so its fields can be read too."""

READ_TIMEOUT_S = 30.0
"""How long one read may take before the process is killed and the read fails."""

_READY = b"{ready}\n"
_STDERR_DONE = "{stderr done}"
"""Written to stderr after each request (`-echo4`), so its messages are known to be in."""
_STDERR_MAX_CHARS = 2000


class MetadataError(Exception):
    """exiftool couldn't read the file, or the process failed during the read."""


class MetadataReader(Protocol):
    def read(self, path: Path, tags: Sequence[str]) -> dict[str, Any]:
        """The tags of `path` that exiftool finds among `tags` (e.g. `IPTC:Keywords`),
        keyed `Family0:Family1:Name` as exiftool's `-G0:1` names them, or
        `Family0:Name` when the two are the same, with duplicates kept (`-a`) and
        numeric values (`-n`). Raises `MetadataError` if it can't be read."""
        ...


class ExifTool:
    """A `MetadataReader` backed by one `exiftool -stay_open` process. Safe to share
    between threads: reads take turns."""

    def __init__(self, timeout_s: float = READ_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: list[str] = []
        self._stderr_done = threading.Condition()
        self._drainer: threading.Thread | None = None
        self._lock = threading.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def pid(self) -> int | None:
        """The running process's id, or None if there isn't one."""
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def read(self, path: Path, tags: Sequence[str]) -> dict[str, Any]:
        args = ["-json", "-G0:1", "-a", "-n", "-charset", "filename=utf8"]
        args += [f"-{tag}" for tag in tags]
        # The -@ arguments are one per line, and one starting with "-" is an option.
        name = os.path.abspath(path)
        if "\n" in name or "\r" in name:
            raise MetadataError(f"can't pass a file name with a line break to exiftool: {name!r}")
        args.append(name)
        with self._lock:
            output, messages = self._execute(args)
        results: list[dict[str, Any]] = []
        try:
            if output.strip():
                results = json.loads(output)
        except json.JSONDecodeError as e:
            raise MetadataError(f"exiftool gave unreadable output for {path.name}: {e}") from e
        if len(results) != 1:
            raise MetadataError(f"exiftool can't read {path.name}: {messages}")
        result: dict[str, Any] = results[0]
        result.pop("SourceFile", None)
        return result

    def close(self) -> None:
        """Stop the process, waiting a few seconds for it to exit cleanly."""
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            if process.poll() is None:
                try:
                    assert process.stdin is not None
                    process.stdin.write(b"-stay_open\nFalse\n")
                    process.stdin.flush()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait()
            self._close_pipes(process)
            logger.debug("Stopped exiftool (pid %d)", process.pid)

    def _execute(self, args: list[str]) -> tuple[str, str]:
        """exiftool's output for one request, and what it wrote to stderr."""
        process = self._running()
        assert process.stdin is not None and process.stdout is not None
        deadline = time.monotonic() + self._timeout_s
        self._take_stderr()
        try:
            request = [*args, "-echo4", _STDERR_DONE, "-execute"]
            process.stdin.write(("\n".join(request) + "\n").encode())
            process.stdin.flush()
            output = self._read_until_ready(process.stdout, deadline)
            self._wait_for_stderr(deadline)
        except (OSError, MetadataError) as e:
            self._kill(process)
            raise MetadataError(f"exiftool failed: {e}. {self._take_stderr()}".strip()) from e
        return output.decode(), self._take_stderr()

    def _running(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        if process is not None:
            logger.warning(
                "exiftool (pid %d) exited with %s, starting it again",
                process.pid,
                process.returncode,
            )
            self._close_pipes(process)
        process = subprocess.Popen(
            # -config must come first, on the command line rather than in the -@ args.
            ["exiftool", "-config", str(CONFIG), "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stderr is not None
        # Drained all the time, so a full stderr pipe can't block exiftool.
        self._drainer = threading.Thread(
            target=self._drain, args=(process.stderr,), name="exiftool-stderr", daemon=True
        )
        self._drainer.start()
        logger.debug("Started exiftool (pid %d)", process.pid)
        self._process = process
        return process

    def _read_until_ready(self, stdout: IO[bytes], deadline: float) -> bytes:
        """Everything exiftool writes for one request, up to its `{ready}` line."""
        fd = stdout.fileno()
        output = b""
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while not output.endswith(_READY):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise MetadataError(f"no answer in {self._timeout_s:g} s")
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise MetadataError("the process exited")
                output += chunk
        return output.removesuffix(_READY)

    def _kill(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.kill()
            process.wait()
        self._close_pipes(process)
        self._process = None

    def _wait_for_stderr(self, deadline: float) -> None:
        with self._stderr_done:
            while _STDERR_DONE not in self._stderr:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._stderr_done.wait(remaining):
                    raise MetadataError(f"no answer in {self._timeout_s:g} s")

    def _drain(self, stderr: IO[bytes]) -> None:
        for line in stderr:
            with self._stderr_done:
                self._stderr.append(line.decode(errors="replace").rstrip())
                self._stderr_done.notify_all()

    def _take_stderr(self) -> str:
        """The messages since the last request, and clear them."""
        with self._stderr_done:
            lines = [line for line in self._stderr if line != _STDERR_DONE]
            self._stderr.clear()
        return " ".join(lines)[-_STDERR_MAX_CHARS:]

    def _close_pipes(self, process: subprocess.Popen[bytes]) -> None:
        """Once the process has exited: its stderr ends, so the drainer finishes."""
        if self._drainer is not None:
            self._drainer.join(timeout=5)
            self._drainer = None
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
