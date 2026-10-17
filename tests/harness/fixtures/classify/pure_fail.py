"""Classifier fixture: one assertion failure, no error."""
import unittest


class T(unittest.TestCase):
    def test_one(self):
        self.fail("pure failure")
