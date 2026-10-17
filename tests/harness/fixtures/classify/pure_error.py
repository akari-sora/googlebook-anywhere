"""Classifier fixture: one test error, no assertion failure."""
import unittest


class T(unittest.TestCase):
    def test_one(self):
        raise RuntimeError("pure error")
