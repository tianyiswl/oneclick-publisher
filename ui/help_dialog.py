# -*- coding: utf-8 -*-
"""使用说明弹窗。"""

from __future__ import annotations

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QTextEdit, QVBoxLayout

from .common import button


class HelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用说明")
        self.resize(720, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(12)
        heading = QLabel("使用说明")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            "使用流程：\n"
            "1. 在账号管理中维护各平台账号。\n"
            "2. 在素材管理中导入视频和封面图片，可拖拽导入。\n"
            "3. 在发布中心选择账号、素材、填写文案和话题。\n"
            "4. 点击“保存填写内容”只保存本机工作区，重新登录或重启软件后可完整恢复。\n"
            "5. 点击“保存平台草稿”会上传视频和填写数据，不公开发布；国内五个平台目前仅视频号和B站支持，小红书、抖音和快手请改用前台预发布检查。\n"
            "6. 先用预发布检查确认内容已经上传到平台页面，再选择继续一键发布或手动发布。\n"
            "7. 常用文案、话题和 B站设置可以保存为发布模板。\n"
            "8. 账号状态超过可信时限会显示“待检测”，账号页和发布页会后台复检。\n"
            "9. 任务记录支持按关键词、状态和平台筛选，双击可查看执行明细。\n\n"
            "菜单说明：\n"
            "- 授权：查看试用状态和机器码，或输入购买后获得的激活码。\n"
            "- 工具：运行环境体检、打包前检查、刷新首页概览。\n"
            "- 数据目录：打开素材、账号登录文件、头像、日志和数据库目录。\n"
            "- 帮助：查看使用说明和版本信息。\n\n"
            "说明：首次打开自动开始 7 天全功能试用。试用到期后，"
            "请联系淘宝店铺“逆浪风”，购买与当前电脑机器码绑定的激活码。"
        )
        layout.addWidget(text)
        actions = QHBoxLayout()
        actions.addStretch()
        close_button = button("关闭", variant="secondary")
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        layout.addLayout(actions)
