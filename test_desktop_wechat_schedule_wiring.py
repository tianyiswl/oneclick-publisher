from unittest import TestCase
from unittest.mock import MagicMock, patch

import desktop_native_app


class DesktopWechatScheduleWiringTests(TestCase):
    def test_authorized_schedule_is_visible_on_publish_page_before_task_start(self) -> None:
        """新增页面后，已授权任务仍必须先切到用户可见的发布中心。"""

        window = MagicMock()
        window.publish = MagicMock()
        window.visible_page = None
        window._set_current_page.side_effect = lambda index: setattr(
            window,
            "visible_page",
            {3: "montage", 4: "publish"}.get(index),
        )
        window.set_current_page_by_key.side_effect = lambda key: setattr(
            window,
            "visible_page",
            key,
        )
        window.publish.wechat_group_notification = None
        window.publish.collect_payloads.return_value = [{"platform": 10}]

        def start_publish(_payloads):
            self.assertEqual(
                window.visible_page,
                "publish",
                "authorized WeChat schedule started while its page was hidden",
            )
            return {"id": 42, "taskNo": "T-SCHEDULE", "itemCount": 1}

        bundle = {
            "coverPaths": {},
            "title": "可见任务",
            "body": "正文",
            "aiDisclosure": {},
        }
        account = {
            "id": 7,
            "type": 10,
            "profileName": "唯一公众号",
            "userName": "唯一公众号",
        }
        with (
            patch.object(
                desktop_native_app.content_bundle,
                "load_content_bundle",
                return_value=bundle,
            ),
            patch.object(
                desktop_native_app.account_service,
                "list_accounts",
                return_value=[account],
            ),
            patch.object(
                desktop_native_app.media_service,
                "import_files_with_records",
                return_value=[],
            ),
            patch.object(
                desktop_native_app.publish_service,
                "start_desktop_publish",
                side_effect=start_publish,
            ),
        ):
            desktop_native_app.start_authorized_wechat_schedule(
                window,
                "/tmp/manifest.json",
                "2099-09-03 10:00",
                "唯一公众号",
            )

        self.assertEqual(window.visible_page, "publish")


if __name__ == "__main__":
    import unittest

    unittest.main()
