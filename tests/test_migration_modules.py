"""Contract tests for the versioned migration module split."""
from __future__ import annotations

import importlib
import unittest

from ai_earn import state_storage


class MigrationModuleTests(unittest.TestCase):
    def test_every_supported_upgrade_has_one_versioned_module(self):
        for version in range(2, state_storage.LATEST_SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                module = importlib.import_module(f"ai_earn.migrations.v{version:02d}")
                self.assertTrue(callable(module.apply))
                self.assertIs(
                    getattr(state_storage, f"_apply_v{version}"),
                    module.apply,
                )

    def test_schema_definitions_remain_available_for_compatibility_validation(self):
        for version in range(2, state_storage.LATEST_SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                module = importlib.import_module(f"ai_earn.migrations.v{version:02d}")
                schemas = [
                    value
                    for name, value in vars(module).items()
                    if name.startswith(f"_V{version}_") and name.endswith("_SCHEMA")
                ]
                # Additive ALTER statements may supplement, but do not replace,
                # each migration's immutable CREATE TABLE schema definitions.
                self.assertTrue(schemas)


if __name__ == "__main__":
    unittest.main()
