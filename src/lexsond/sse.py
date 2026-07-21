from __future__ import annotations

import codecs
from dataclasses import dataclass


class SSEProtocolError(ValueError):
    """Raised when an SSE stream cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str | None = None
    event_id: str | None = None
    retry_ms: int | None = None


class SSEParser:
    """Incremental UTF-8 SSE parser following the event-stream line model.

    The parser accepts arbitrary byte boundaries, including boundaries inside a
    multibyte UTF-8 character. Events are dispatched only on a blank line.
    """

    _MAX_LINES = 16_384
    _MAX_LINE_CHARS = 1_048_576

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buffer = ""
        self._data_lines: list[str] = []
        self._event: str | None = None
        self._event_id: str | None = None
        self._retry_ms: int | None = None
        self._line_count = 0

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        try:
            self._buffer += self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise SSEProtocolError("SSE stream is not valid UTF-8") from exc
        return self._drain_complete_lines()

    def finalize(self) -> list[SSEEvent]:
        try:
            self._buffer += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise SSEProtocolError("SSE stream ended inside a UTF-8 sequence") from exc

        events = self._drain_complete_lines()
        if self._buffer:
            if len(self._buffer) > self._MAX_LINE_CHARS:
                raise SSEProtocolError("SSE line length exceeds the probe limit")
            self._line_count += 1
            if self._line_count > self._MAX_LINES:
                raise SSEProtocolError("SSE line count exceeds the probe limit")
            self._process_line(self._buffer.rstrip("\r"), events)
            self._buffer = ""
        event = self._dispatch()
        if event is not None:
            events.append(event)
        return events

    def _drain_complete_lines(self) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        last_newline = self._buffer.rfind("\n")
        if last_newline < 0:
            if len(self._buffer) > self._MAX_LINE_CHARS:
                raise SSEProtocolError("SSE line length exceeds the probe limit")
            return events

        complete = self._buffer[:last_newline]
        self._buffer = self._buffer[last_newline + 1 :]
        lines = complete.split("\n")
        self._line_count += len(lines)
        if self._line_count > self._MAX_LINES:
            raise SSEProtocolError("SSE line count exceeds the probe limit")
        if len(self._buffer) > self._MAX_LINE_CHARS:
            raise SSEProtocolError("SSE line length exceeds the probe limit")
        for line in lines:
            if len(line) > self._MAX_LINE_CHARS:
                raise SSEProtocolError("SSE line length exceeds the probe limit")
            self._process_line(line.rstrip("\r"), events)
        return events

    def _process_line(self, line: str, events: list[SSEEvent]) -> None:
        if line == "":
            event = self._dispatch()
            if event is not None:
                events.append(event)
            return
        if line.startswith(":"):
            return

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]

        if field == "data":
            self._data_lines.append(value)
        elif field == "event":
            self._event = value
        elif field == "id" and "\x00" not in value:
            self._event_id = value
        elif field == "retry" and value.isdecimal():
            self._retry_ms = int(value)

    def _dispatch(self) -> SSEEvent | None:
        if not self._data_lines:
            self._event = None
            self._retry_ms = None
            return None
        event = SSEEvent(
            data="\n".join(self._data_lines),
            event=self._event,
            event_id=self._event_id,
            retry_ms=self._retry_ms,
        )
        self._data_lines = []
        self._event = None
        self._retry_ms = None
        return event
