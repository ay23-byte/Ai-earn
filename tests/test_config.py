import unittest

from ai_earn.config import ConfigurationError, Settings, load_settings


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(load_settings({}), Settings("development", "INFO"))

    def test_all_supported_environments(self):
        for environment in ("development", "test", "production"):
            with self.subTest(environment=environment):
                self.assertEqual(load_settings({"AI_EARN_ENV": environment}).environment, environment)

    def test_all_supported_log_levels(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            with self.subTest(level=level):
                self.assertEqual(load_settings({"AI_EARN_LOG_LEVEL": level}).log_level, level)

    def test_rejects_empty_invalid_and_case_mismatched_values_without_echo(self):
        for name in ("AI_EARN_ENV", "AI_EARN_LOG_LEVEL"):
            for value in ("", "not-allowed-SECRET", "info"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ConfigurationError) as caught:
                        load_settings({name: value})
                    self.assertIn(name, str(caught.exception))
                    if value:
                        self.assertNotIn(value, str(caught.exception))

    def test_settings_are_immutable(self):
        settings = load_settings({})
        with self.assertRaises(AttributeError):
            settings.environment = "test"
