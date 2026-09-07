"""Pointer replacement must coexist with the new app's metadata reader."""

import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import update_protocol


class PointerSharingTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows file sharing contract")
    def test_persistent_windows_lock_stops_retrying_and_preserves_pointer(self):
        with TemporaryDirectory() as directory:
            pointer = Path(directory) / "current.json"
            update_protocol.write_pointer(pointer, "1.9.1")
            error = PermissionError("sharing conflict")
            error.winerror = 5
            with (
                patch.object(update_protocol.os, "replace", side_effect=error) as replace,
                patch.object(update_protocol.time, "monotonic", side_effect=[0, 0.5, 2.1]),
                patch.object(update_protocol.time, "sleep"),
            ):
                with self.assertRaises(PermissionError):
                    update_protocol.write_pointer(pointer, "1.9.3")
            self.assertEqual(replace.call_count, 2)
            self.assertEqual(update_protocol.read_version_pointer(pointer), "1.9.1")

    @unittest.skipUnless(os.name == "nt", "Windows file sharing contract")
    def test_transient_reader_lock_does_not_abort_pointer_commit(self):
        with TemporaryDirectory() as directory:
            pointer = Path(directory) / "current.json"
            update_protocol.write_pointer(pointer, "1.9.1")
            handle = pointer.open("rb")
            timer = threading.Timer(0.15, handle.close)
            timer.start()
            try:
                update_protocol.write_pointer(pointer, "1.9.3")
            finally:
                handle.close()
                timer.join()
            self.assertEqual(update_protocol.read_version_pointer(pointer), "1.9.3")

    def test_permanent_replace_error_keeps_old_pointer(self):
        with TemporaryDirectory() as directory:
            pointer = Path(directory) / "current.json"
            update_protocol.write_pointer(pointer, "1.9.1")
            with patch.object(update_protocol.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    update_protocol.write_pointer(pointer, "1.9.3")
            self.assertEqual(update_protocol.read_version_pointer(pointer), "1.9.1")
