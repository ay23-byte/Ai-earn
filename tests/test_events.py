import io
import json
import unittest

from ai_earn.events import Event, write_event


class EventTests(unittest.TestCase):
    def test_structured_event_has_only_allowlisted_fields(self):
        stream = io.StringIO()
        write_event(Event.STATUS_REQUESTED, stream)
        record = json.loads(stream.getvalue())
        self.assertEqual(set(record), {"timestamp", "level", "event"})
        self.assertEqual(record["event"], "status_requested")
        self.assertEqual(record["level"], "INFO")
        self.assertTrue(record["timestamp"].endswith("+00:00"))

    def test_arbitrary_message_rejected_before_logging(self):
        stream = io.StringIO()
        with self.assertRaises(TypeError):
            write_event("SECRET-token", stream)
        self.assertEqual(stream.getvalue(), "")

    def test_level_filtering_and_invalid_level(self):
        stream = io.StringIO()
        write_event(Event.STATUS_REQUESTED, stream, "WARNING")
        self.assertEqual(stream.getvalue(), "")
        write_event(Event.INVALID_CONFIGURATION, stream, "WARNING")
        self.assertEqual(json.loads(stream.getvalue())["level"], "ERROR")
        with self.assertRaises(ValueError):
            write_event(Event.INTERNAL_ERROR, stream, "SECRET")
        self.assertNotIn("SECRET", stream.getvalue())
