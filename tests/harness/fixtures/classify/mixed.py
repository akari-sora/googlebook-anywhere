"""Classifier fixture: one assertion failure and one test error."""
import unittest


class T(unittest.TestCase):
    def test_error(self):
        raise RuntimeError("mixed error")

    def test_fail(self):
        self.fail("mixed failure")
