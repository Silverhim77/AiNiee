"""独立的「订阅管理」页 —— 专门入口。

用户在此一处即可：完成 Claude OAuth 登录、查看订阅登录状态、选择模型并一键创建/应用并启用
Claude 订阅接口、对已建订阅接口做激活/测试/编辑/删除，并看到当前全局激活
的是订阅还是 API 接口。

激活语义：与「接口管理」共享 `api_settings.active`（互斥）——激活订阅接口则
后续翻译走订阅模型，从接口管理激活 API 接口则走 API 模型，以最后激活的为准。
"""

import copy
import json
import os
import random
import time

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    MessageBoxBase,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
)

from ModuleFolders.Base.Base import Base
from ModuleFolders.Config.Config import ConfigMixin
from ModuleFolders.Config.FilePathConfig import platform_preset_path
from UserInterface.Platform.APIEditPage import APIEditPage
from UserInterface.Platform.APIItemCard import APIItemCard
from UserInterface.Platform.ArgsEditPage import ArgsEditPage
from UserInterface.Platform.PlatformPage import APITypeCard
from UserInterface.Widget.Toast import ToastMixin

_SUBSCRIPTION_PRESET_TAG = "claude_subscription"


class OAuthLoginDialog(MessageBoxBase, ConfigMixin):
    def __init__(self, window, auth_url: str):
        super().__init__(window)
        self.auth_url = auth_url
        self.widget.setFixedSize(720, 520)
        self.yesButton.setText(self.tra("完成登录"))
        self.cancelButton.setText(self.tra("取消"))
        self.viewLayout.setContentsMargins(20, 20, 20, 20)
        self.viewLayout.setSpacing(12)

        self.viewLayout.addWidget(StrongBodyLabel(self.tra("登录 Claude 订阅"), self))

        tip = BodyLabel(
            self.tra("将打开浏览器完成 Claude OAuth 授权，授权后把页面显示的 code#state 粘贴到下方。"),
            self,
        )
        tip.setWordWrap(True)
        self.viewLayout.addWidget(tip)

        self.open_btn = PushButton(FluentIcon.LINK, self.tra("打开登录页面"), self)
        self.open_btn.clicked.connect(self.open_auth_url)
        self.viewLayout.addWidget(self.open_btn, 0, Qt.AlignLeft)

        self.url_edit = PlainTextEdit(self)
        self.url_edit.setPlainText(auth_url)
        self.url_edit.setReadOnly(True)
        self.url_edit.setFixedHeight(110)
        self.viewLayout.addWidget(self.url_edit)

        self.viewLayout.addWidget(StrongBodyLabel(self.tra("授权码"), self))
        self.code_edit = PlainTextEdit(self)
        self.code_edit.setPlaceholderText(self.tra("粘贴浏览器页面显示的 code#state 或完整回调地址"))
        self.code_edit.setFixedHeight(120)
        self.viewLayout.addWidget(self.code_edit)

    def open_auth_url(self):
        QDesktopServices.openUrl(QUrl(self.auth_url))

    def code_text(self) -> str:
        return self.code_edit.toPlainText().strip()

    def validate(self):
        return bool(self.code_text())


class SubscriptionPage(QFrame, ConfigMixin, ToastMixin, Base):

    def __init__(self, text: str, window):
        super().__init__(window)
        self.setObjectName(text.replace(" ", "-"))

        self.default = {
            "platforms": {},
            "api_settings": {
                "active": None, "extract": None, "translate": None,
                "polish": None, "proofread": None,
            },
        }

        self.window = window
        self.api_buttons = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 12, 24, 20)
        root.setSpacing(12)
        root.addWidget(SubtitleLabel(self.tra("订阅管理"), self))

        # ---- 登录状态卡 ----
        self.status_card = CardWidget(self)
        sc = QVBoxLayout(self.status_card)
        sc.setContentsMargins(20, 16, 20, 16)
        sc.setSpacing(8)
        sc.addWidget(StrongBodyLabel(self.tra("订阅登录状态"), self.status_card))
        self.status_label = BodyLabel("", self.status_card)
        self.status_label.setWordWrap(True)
        sc.addWidget(self.status_label)
        self.active_label = BodyLabel("", self.status_card)
        self.active_label.setWordWrap(True)
        sc.addWidget(self.active_label)
        status_actions = QHBoxLayout()
        status_actions.setSpacing(8)
        self.login_btn = PrimaryPushButton(FluentIcon.LINK, self.tra("登录 Claude 订阅"), self.status_card)
        self.login_btn.clicked.connect(self._on_login)
        status_actions.addWidget(self.login_btn, 0, Qt.AlignLeft)
        self.logout_btn = PushButton(FluentIcon.DELETE, self.tra("退出登录"), self.status_card)
        self.logout_btn.clicked.connect(self._on_logout)
        status_actions.addWidget(self.logout_btn, 0, Qt.AlignLeft)
        self.recheck_btn = PushButton(FluentIcon.SYNC, self.tra("重新检测"), self.status_card)
        self.recheck_btn.clicked.connect(self.refresh)
        status_actions.addWidget(self.recheck_btn, 0, Qt.AlignLeft)
        status_actions.addStretch(1)
        sc.addLayout(status_actions)
        root.addWidget(self.status_card)

        # ---- 订阅接口分组卡（复用接口管理同款 APITypeCard）----
        self.sub_group = APITypeCard(
            title=self.tra("订阅接口"),
            icon=FluentIcon.PEOPLE,
            description=self.tra("使用 AiNiee 内置 OAuth 登录（无需 API Key）"),
            parent=self,
        )
        root.addWidget(self.sub_group)
        # 空态（已登录但无订阅接口，含登录后取消/卡片被删）入口：
        # 创建走「按预设建默认模型 → 复用接口管理 APIEditPage 选模型 → 激活」
        self.create_btn = PrimaryPushButton(
            FluentIcon.ADD, self.tra("创建订阅接口"), self
        )
        self.create_btn.clicked.connect(self._on_create_subscription)
        root.addWidget(self.create_btn, 0, Qt.AlignLeft)

        # ---- 说明卡 ----
        tip = CardWidget(self)
        tc = QVBoxLayout(tip)
        tc.setContentsMargins(20, 16, 20, 16)
        tc.setSpacing(6)
        tc.addWidget(StrongBodyLabel(self.tra("说明"), tip))
        tl = CaptionLabel(self.tra(
            "在「接口管理」或「订阅管理」激活其一即可：激活订阅接口则后续翻译走订阅模型，"
            "从接口管理激活 API 接口则走 API 模型（二者互斥，以最后激活的为准）。"
            "订阅按 5 小时 / 7 天滚动窗口用量限额，触顶会暂时返回 429，稍后自动恢复。"
            "使用 AiNiee 内置 OAuth 订阅凭据驱动第三方应用可能违反 Anthropic 服务条款，"
            "限流/封号风险由使用者自担。"
        ), tip)
        tl.setWordWrap(True)
        tc.addWidget(tl)
        root.addWidget(tip)
        root.addStretch(1)

        self.subscribe(Base.EVENT.API_TEST_DONE, self._on_test_done)
        self.refresh()

    # ---------- preset ----------
    def _load_preset_platform(self):
        path = platform_preset_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return (data.get("platforms", {}) or {}).get(_SUBSCRIPTION_PRESET_TAG)
        except (OSError, ValueError):
            pass
        return None

    # ---------- 刷新 ----------
    def refresh(self, *_):
        self._refresh_status()        # 设置 self._logged_in
        self._rebuild_cards()         # 设置 self._has_subs，重建卡片
        self._refresh_active_label()
        self._refresh_visibility()    # 据登录/有无接口切换 订阅接口分组卡 与 创建按钮

    def showEvent(self, event):
        # 切回本页时按共享 config 重刷：在「接口管理」改了激活接口能同步反映
        super().showEvent(event)
        self.refresh()

    def _refresh_status(self):
        try:
            from ModuleFolders.Infrastructure.Auth.CredentialManager import CredentialManager
            meta = CredentialManager().get_status_meta("anthropic")
        except Exception as e:  # noqa: BLE001
            self._logged_in = False
            self.status_label.setText(self.tra("无法读取订阅登录状态") + f"：{e}")
            self.status_label.setStyleSheet("color:#c0392b;")
            return
        self._logged_in = bool(meta.get("logged_in"))
        if meta.get("logged_in"):
            exp = meta.get("expires_at") or 0
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "-"
            source = meta.get("source") or "AiNiee OAuth"
            if source == "ainiee_oauth":
                source = "AiNiee OAuth"
            txt = (f"{self.tra('已登录')}　{meta.get('subscription_type', '') or '-'}　"
                   f"{source}　"
                   f"{self.tra('到期时间')}：{when}")
            if meta.get("expiring"):
                txt += "　" + self.tra("（即将过期，使用时将自动刷新）")
            self.status_label.setStyleSheet("color:#2e7d32;")
            self.logout_btn.setEnabled(True)
        else:
            txt = (self.tra("未检测到登录") + "　"
                   + self.tra("请在订阅管理中登录 Claude 订阅账号"))
            self.status_label.setStyleSheet("color:#c0392b;")
            self.logout_btn.setEnabled(False)
        self.status_label.setText(txt)

    def _subscription_tags(self, config) -> list:
        out = [(t, d) for t, d in config.get("platforms", {}).items()
               if d.get("auth_method") == "oauth" or d.get("group") == "subscription"]
        out.sort(key=lambda kv: kv[1].get("name", ""))
        return out

    def _refresh_active_label(self):
        config = self.load_config()
        # 当前全局激活接口（订阅 vs API，互斥）
        active = config.get("api_settings", {}).get("active")
        platforms = config.get("platforms", {})
        if active and active in platforms:
            p = platforms[active]
            is_sub = p.get("auth_method") == "oauth" or p.get("group") == "subscription"
            kind = self.tra("订阅") if is_sub else self.tra("API")
            self.active_label.setText(
                f"{self.tra('当前激活接口')}：{p.get('name', active)}（{kind}）")
            self.active_label.setStyleSheet("color:#2e7d32;" if is_sub else "color:#555;")
        else:
            self.active_label.setText(self.tra("未激活任何接口"))
            self.active_label.setStyleSheet("color:#c0392b;")

    def _rebuild_cards(self):
        # APITypeCard.removeWidget 同步内部计数；清理后逐张重建
        for _c in list(self.api_buttons.values()):
            self.sub_group.removeWidget(_c)
            _c.setParent(None)
            _c.deleteLater()
        self.api_buttons.clear()
        config = self.load_config()
        active = config.get("api_settings", {}).get("active")
        subs = self._subscription_tags(config)
        self._has_subs = bool(subs)
        for tag, data in subs:
            c = APIItemCard(tag, data, self)
            c.testClicked.connect(self._on_test)
            c.activateClicked.connect(self._on_activate)
            c.editClicked.connect(self._on_edit)
            c.editArgsClicked.connect(self._on_args)
            c.deleteClicked.connect(self._on_delete)
            c.set_active(tag == active)
            self.api_buttons[tag] = c
            self.sub_group.addWidget(c)

    def _refresh_visibility(self):
        """订阅接口分组卡显示逻辑与「接口管理」官方接口一致：有接口才显示；
        且未登录则整组不显示。创建按钮仅在「已登录且无订阅接口」时出现。"""
        logged_in = getattr(self, "_logged_in", False)
        has_subs = getattr(self, "_has_subs", False)
        self.sub_group.setVisible(logged_in and has_subs)
        self.create_btn.setVisible(logged_in and not has_subs)

    # ---------- 操作 ----------
    def _on_login(self):
        try:
            from ModuleFolders.Infrastructure.Auth.CredentialManager import CredentialManager
            flow = CredentialManager().start_login("anthropic")
        except Exception as e:  # noqa: BLE001
            self.error_toast("", self.tra("登录失败") + f": {e}")
            return

        auth_url = flow.get("auth_url", "")
        if auth_url:
            QDesktopServices.openUrl(QUrl(auth_url))
        dialog = OAuthLoginDialog(self.window, auth_url)
        if not dialog.exec():
            return
        try:
            from ModuleFolders.Infrastructure.Auth.CredentialManager import CredentialManager
            CredentialManager().complete_login("anthropic", dialog.code_text())
        except Exception as e:  # noqa: BLE001
            self.error_toast("", self.tra("登录失败") + f": {e}")
            return
        self.refresh()
        self.success_toast("", self.tra("登录成功"))
        # 登录成功后：无订阅接口才引导创建（Q1：已有则只刷新，不再弹）
        if not getattr(self, "_has_subs", False):
            self._create_default_subscription_and_edit()

    def _on_logout(self):
        try:
            from ModuleFolders.Infrastructure.Auth.CredentialManager import CredentialManager
            CredentialManager().logout("anthropic")
        except Exception as e:  # noqa: BLE001
            self.error_toast("", self.tra("退出登录失败") + f": {e}")
            return
        self.refresh()
        self.success_toast("", self.tra("已退出登录"))

    def _on_create_subscription(self):
        self._create_default_subscription_and_edit()

    def _create_default_subscription_and_edit(self):
        """按预设以默认模型创建订阅接口并激活，随后复用接口管理 APIEditPage 选模型。

        APIEditPage 需要 config 中已存在的 tag，故先建后编；用户即使在编辑窗
        取消，也已有一个可用的默认模型订阅接口（不会出现“取消即无接口”死角）。
        改模型/启用/测试/删除后续全走订阅接口卡片下拉（编辑接口=同一 APIEditPage）。
        """
        config = self.load_config()
        subs = self._subscription_tags(config)
        if subs:
            # 已有则不重复创建（Q1），直接复用编辑窗调整第一个
            tag = subs[0][0]
        else:
            preset = self._load_preset_platform()
            if not preset:
                self.error_toast("", self.tra("未找到 Claude 订阅预设"))
                return
            new_p = copy.deepcopy(preset)
            tag = f"{_SUBSCRIPTION_PRESET_TAG}_{random.randint(100000, 999999)}"
            new_p["tag"] = tag
            new_p["group"] = preset.get("group", "subscription")
            new_p["name"] = preset.get("name", "Claude 订阅")
            config.setdefault("platforms", {})[tag] = new_p
            config.setdefault("api_settings", {})["active"] = tag  # 创建即启用
            self.save_config(config)
            self.refresh()
            self.success_toast("", self.tra("订阅接口已创建并启用"))
        # 复用接口管理的编辑弹窗选择模型（claude_subscription 预设含 model 字段）
        APIEditPage(self.window, tag).exec()
        self.refresh()

    def _on_activate(self, tag: str):
        config = self.load_config()
        if tag not in config.get("platforms", {}):
            return
        config.setdefault("api_settings", {})["active"] = tag
        self.save_config(config)
        self.refresh()
        name = config["platforms"][tag].get("name", tag)
        self.success_toast("", f"{self.tra('已激活接口')}: {name}")

    def _on_delete(self, tag: str):
        config = self.load_config()
        platforms = config.get("platforms", {})
        if tag not in platforms:
            return
        api_settings = config.setdefault("api_settings", {})
        for role in ("active", "extract", "translate", "polish", "proofread"):
            if api_settings.get(role) == tag:
                api_settings[role] = None
        del platforms[tag]
        self.save_config(config)
        self.refresh()
        self.success_toast("", self.tra("接口已删除"))

    def _on_test(self, tag: str):
        config = self.load_config()
        platform = config.get("platforms", {}).get(tag)
        if platform is None:
            self.warning_toast("", self.tra("接口不存在"))
            return
        if Base.work_status == Base.STATUS.IDLE:
            Base.work_status = Base.STATUS.API_TEST
            self._api_test_pending = True
            self.emit(Base.EVENT.API_TEST_START, copy.deepcopy(platform))

    def _on_test_done(self, event: int, data: dict):
        # API_TEST_DONE 为全局事件，接口管理页也订阅；仅处理本页发起的测试，
        # 否则会对同一次测试重复弹提示、重复复位 work_status。
        if not getattr(self, "_api_test_pending", False):
            return
        self._api_test_pending = False
        Base.work_status = Base.STATUS.IDLE
        if len(data.get("failure", [])) > 0:
            self.error_toast("", self.tra("测试完成：成功")
                             + f" {len(data.get('success', []))} "
                             + self.tra("失败") + f" {len(data.get('failure', []))}")
        else:
            self.success_toast("", self.tra("测试成功"))

    def _on_edit(self, tag: str):
        APIEditPage(self.window, tag).exec()
        self.refresh()

    def _on_args(self, tag: str):
        ArgsEditPage(self.window, tag).exec()
        self.refresh()
