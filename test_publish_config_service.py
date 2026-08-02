# -*- coding: utf-8 -*-

import unittest

from app_core.publish_config_service import parse_tags


class PublishConfigServiceTests(unittest.TestCase):
    def test_parses_hash_and_line_separated_tags_without_duplicates(self):
        self.assertEqual(
            parse_tags("#AI工具\n#AI工作流 #AI工具\n独立开发"),
            ["AI工具", "AI工作流", "独立开发"],
        )

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(parse_tags(""), [])


if __name__ == "__main__":
    unittest.main()
