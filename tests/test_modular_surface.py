import importlib
import unittest


class ModularSurfaceTests(unittest.TestCase):
    def test_package_modules_import(self):
        for name in (
            "watcher.config",
            "watcher.errors",
            "watcher.reliability",
            "watcher.snapshot",
            "watcher.browser",
            "watcher.runtime",
            "watcher.utils",
        ):
            self.assertIsNotNone(importlib.import_module(name))

    def test_legacy_entrypoint_reexports_core_primitives(self):
        legacy = importlib.import_module("biu_grade_watcher")
        reliability = importlib.import_module("watcher.reliability")
        utils = importlib.import_module("watcher.utils")

        self.assertIs(legacy.ProtectionController, reliability.ProtectionController)
        self.assertIs(legacy.RequestGate, reliability.RequestGate)
        self.assertIs(legacy.normalize_text, utils.normalize_text)
        self.assertIs(legacy.parse_retry_after, utils.parse_retry_after)


if __name__ == "__main__":
    unittest.main()
