# -*- coding: utf-8 -*-
"""抖音带货收藏音乐本地缓存的离线回归测试。"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_core import douyin_favorite_music_cache


class DouyinFavoriteMusicCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "music-cache.sqlite3"
        self.database_patch = patch("app_core.database.DB_PATH", self.database_path)
        self.database_patch.start()

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.temporary_directory.cleanup()

    def test_cache_isolated_by_account_and_keeps_only_safe_music_fields(self) -> None:
        first = douyin_favorite_music_cache.replace_cached_favorite_music(
            3,
            [
                {
                    "musicId": "music-a",
                    "title": "歌曲 A",
                    "creator": "作者 A",
                    "duration": "03:21",
                    "marker": "transient-marker",
                    "cookie": "never-store",
                }
            ],
        )
        second = douyin_favorite_music_cache.replace_cached_favorite_music(
            4,
            [
                {
                    "musicId": "music-b",
                    "title": "歌曲 B",
                    "creator": "作者 B",
                    "duration": "02:18",
                }
            ],
        )

        self.assertEqual(
            [{key: value for key, value in row.items() if key != "syncedAt"} for row in first],
            [
                {
                    "musicId": "music-a",
                    "title": "歌曲 A",
                    "creator": "作者 A",
                    "duration": "03:21",
                }
            ],
        )
        self.assertEqual(douyin_favorite_music_cache.list_cached_favorite_music(3), first)
        self.assertEqual(douyin_favorite_music_cache.list_cached_favorite_music(4), second)
        self.assertNotIn("marker", first[0])
        self.assertNotIn("cookie", first[0])

    def test_replace_converts_unstable_row_id_to_metadata_fingerprint(self) -> None:
        saved = douyin_favorite_music_cache.replace_cached_favorite_music(
            3,
            [
                {
                    "musicId": "favorite-index:1",
                    "title": "临时行",
                    "creator": "作者",
                    "duration": "00:31",
                    "marker": "only-this-session",
                }
            ],
        )

        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["musicId"].startswith("metadata:"))
        self.assertEqual(saved[0]["title"], "临时行")
        self.assertEqual(saved[0]["creator"], "作者")
        self.assertEqual(saved[0]["duration"], "00:31")
        self.assertNotIn("marker", saved[0])
        self.assertEqual(douyin_favorite_music_cache.list_cached_favorite_music(3), saved)

    def test_replace_rejects_duplicate_stable_music_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "重复"):
            douyin_favorite_music_cache.replace_cached_favorite_music(
                3,
                [
                    {
                        "musicId": "music-a",
                        "title": "歌曲 A",
                        "creator": "作者 A",
                        "duration": "03:21",
                    },
                    {
                        "musicId": "music-a",
                        "title": "歌曲 A 副本",
                        "creator": "作者 A",
                        "duration": "03:21",
                    },
                ],
            )


if __name__ == "__main__":
    unittest.main()
