import unittest

from app_core.branding import APP_VERSION


class BrandingVersionTests(unittest.TestCase):
    def test_current_integration_release_is_0_5_28(self) -> None:
        self.assertEqual(APP_VERSION, "0.5.28")


if __name__ == "__main__":
    unittest.main()
