"""Classifier fixture: stdout says FAILED (failures=1); stderr is the real error."""
import sys
import unittest


class T(unittest.TestCase):
    def test_one(self):
        sys.stdout.write("FAILED (failures=1)\n")
        raise RuntimeError("stderr has the error")
