from __future__ import annotations

import unittest

from lexsond.sse import SSEParser, SSEProtocolError


class SSEParserTests(unittest.TestCase):
    def test_parses_event_across_arbitrary_utf8_boundaries(self) -> None:
        raw = "event: message\nid: 42\ndata: 你\ndata: 好\n\n".encode("utf-8")
        parser = SSEParser()
        events = []
        for byte in raw:
            events.extend(parser.feed(bytes([byte])))
        events.extend(parser.finalize())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "message")
        self.assertEqual(events[0].event_id, "42")
        self.assertEqual(events[0].data, "你\n好")

    def test_ignores_comments_and_unknown_fields(self) -> None:
        parser = SSEParser()
        events = parser.feed(b": keepalive\nunknown: value\ndata: ok\n\n")
        self.assertEqual([event.data for event in events], ["ok"])

    def test_rejects_invalid_utf8(self) -> None:
        parser = SSEParser()
        with self.assertRaises(SSEProtocolError):
            parser.feed(b"data: \xff\n\n")

    def test_rejects_excessive_comment_lines(self) -> None:
        parser = SSEParser()
        with self.assertRaisesRegex(SSEProtocolError, "line count"):
            parser.feed(b": keepalive\n" * 16_385)

    def test_rejects_an_excessively_long_unterminated_line(self) -> None:
        parser = SSEParser()
        with self.assertRaisesRegex(SSEProtocolError, "line length"):
            parser.feed(b"data: " + (b"x" * 1_048_576))


if __name__ == "__main__":
    unittest.main()
