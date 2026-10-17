"""Classifier fixture: sleeps until the runner's timeout fires."""
import time
import unittest


class T(unittest.TestCase):
    def test_one(self):
        time.sleep(30)
