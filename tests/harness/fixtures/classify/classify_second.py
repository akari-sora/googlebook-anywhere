"""Classifier fixture: assertion failure, then an error on the behaviour-only re-run."""
import os
import unittest


class T(unittest.TestCase):
    def test_one(self):
        if os.environ.get("HARNESS_BEHAVIOUR_ONLY"):
            raise RuntimeError("second pass")
        self.fail("first pass")
