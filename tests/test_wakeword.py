import unittest

from voice_pet.runtime.wakeword import WakewordDetector


class WakewordDetectorTest(unittest.TestCase):
    def test_detect_boundary_split_repeated_wakeword(self) -> None:
        detector = WakewordDetector(["小爱"])

        result = detector.detect_boundary("小爱小", "爱")

        self.assertTrue(result.matched)
        self.assertEqual(result.alias, "小爱")
        self.assertEqual(result.cleaned_text, "")

    def test_detect_boundary_split_wakeword_with_followup_text(self) -> None:
        detector = WakewordDetector(["小爱"])

        result = detector.detect_boundary("小", "爱查天气")

        self.assertTrue(result.matched)
        self.assertEqual(result.alias, "小爱")
        self.assertEqual(result.cleaned_text, "查天气")
