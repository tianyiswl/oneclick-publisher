from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import desktop_native_app


class DesktopWechatDraftBridgeWiringTests(TestCase):
    def test_normal_window_creation_configures_wechat_draft_bridge(self):
        window = Mock()

        with patch.object(desktop_native_app, "MainWindow", return_value=window):
            created = desktop_native_app.create_main_window()

        self.assertIs(created, window)
        window.publish.configure_wechat_draft_queue.assert_called_once_with(
            Path(desktop_native_app.WECHAT_DRAFT_BRIDGE_DIR)
        )
