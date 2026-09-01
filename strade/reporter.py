"""Progress, warning, and summary reporting for the extractor commands.

The :class:`Reporter` tracks counts, collects non-fatal warnings, reports
progress and a final summary, and drives the process exit code. Both the
``extract`` and ``join`` commands create their own reporter.

All human-readable output (progress lines, warnings, summary) is written to
**stderr** so that stdout stays clean for the street list.
"""

import sys
from typing import TextIO


class Reporter:
    """Tracks counts and non-fatal warnings and drives the exit code.

    Progress and summary output go to ``stream`` (stderr by default) so that
    stdout is reserved for the street list. Non-fatal warnings increment an
    internal error count; a non-zero ``error_count`` yields a non-zero
    ``exit_code``.

    ``verbosity`` gates the optional :meth:`info` (>= 1) and :meth:`debug`
    (>= 2) levels. :meth:`warn`, :meth:`progress`, and :meth:`summary` are
    always emitted regardless of the level.
    """

    def __init__(self, stream: TextIO | None = None, verbosity: int = 0) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stderr
        self._verbosity: int = verbosity
        self._error_count: int = 0
        self._parsed: int | None = None
        self._groups: int | None = None
        self._streets: int | None = None

    def warn(self, message: str) -> None:
        """Record a non-fatal warning and write it to the report stream.

        Increments the internal error count so the run terminates with a
        non-zero exit code.
        """
        self._error_count += 1
        print(f"warning: {message}", file=self._stream)

    def progress(self, message: str) -> None:
        """Write a progress message to the report stream (stderr)."""
        print(message, file=self._stream)

    def info(self, message: str) -> None:
        """Write an informational message when verbosity is >= 1.

        Info messages are additional, non-essential detail about the run's
        progress. They are suppressed at the default verbosity (0) and shown
        once the caller opts in (e.g. via ``-v``).
        """
        if self._verbosity >= 1:
            print(f"info: {message}", file=self._stream)

    def debug(self, message: str) -> None:
        """Write a debug message when verbosity is >= 2.

        Debug messages carry fine-grained diagnostic detail and are only shown
        at the highest verbosity (e.g. via ``-vv``).
        """
        if self._verbosity >= 2:
            print(f"debug: {message}", file=self._stream)

    def set_counts(
        self,
        parsed: int | None = None,
        groups: int | None = None,
        streets: int | None = None,
    ) -> None:
        """Record counts used by :meth:`summary`.

        Each count is optional so both commands can use the reporter: the
        ``extract`` command records ``parsed`` and ``groups`` (there is no
        street count yet), while ``join`` fills in ``streets``. Only the
        arguments that are not ``None`` overwrite the stored values, so callers
        may set counts incrementally across several calls.
        """
        if parsed is not None:
            self._parsed = parsed
        if groups is not None:
            self._groups = groups
        if streets is not None:
            self._streets = streets

    def summary(self) -> None:
        """Report the recorded parsed/group/street counts to stderr.

         Counts that were never set are shown as ``n/a``. When one or more
         non-fatal errors were recorded, the error count is also reported
        .
        """
        parsed = self._format_count(self._parsed)
        groups = self._format_count(self._groups)
        streets = self._format_count(self._streets)
        print(
            f"summary: parsed={parsed} groups={groups} streets={streets}",
            file=self._stream,
        )
        if self._error_count:
            print(
                f"summary: {self._error_count} non-fatal error(s) recorded",
                file=self._stream,
            )

    @staticmethod
    def _format_count(value: int | None) -> str:
        return "n/a" if value is None else str(value)

    @property
    def error_count(self) -> int:
        """Number of recorded non-fatal errors/warnings."""
        return self._error_count

    @property
    def exit_code(self) -> int:
        """0 when no non-fatal errors were recorded, else 1."""
        return 0 if self._error_count == 0 else 1
