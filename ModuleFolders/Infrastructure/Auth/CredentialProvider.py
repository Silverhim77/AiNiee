"""订阅账户凭据 provider 抽象。

设计为 provider 无关，便于后续扩展（ChatGPT 订阅等）。当前唯一实现为
AnthropicOAuthCredentialProvider —— AiNiee 应用内 OAuth 登录。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import time


@dataclass
class SubscriptionCredential:
    """一份订阅令牌。expires_at 统一用「秒级」时间戳（float）。"""
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0          # 秒级 epoch；0 表示未知
    scopes: list = field(default_factory=list)
    subscription_type: str = ""      # 如 max / pro
    provider_id: str = ""
    # 原始凭据文件内容，刷新写回时用于保留其它字段（如 organizationUuid）
    raw: dict = field(default_factory=dict)

    def seconds_left(self) -> float:
        return self.expires_at - time.time() if self.expires_at else float("inf")

    def is_expiring(self, safety: float = 300.0) -> bool:
        """距过期不足 safety 秒（或已过期）视为需要刷新。"""
        if not self.expires_at:
            return False
        return self.seconds_left() < safety


class CredentialProvider(ABC):
    """订阅凭据来源。实现负责加载、刷新，并声明该 provider 的强制 system 前导。"""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """provider 标识，如 "anthropic"。"""

    @abstractmethod
    def load(self) -> SubscriptionCredential | None:
        """加载当前凭据；不存在/未登录返回 None（不抛异常）。"""

    @abstractmethod
    def refresh(self, cred: SubscriptionCredential) -> SubscriptionCredential:
        """用 refresh_token 续期，持久化新令牌并返回。失败抛 RuntimeError。"""

    def required_system_preamble(self) -> str | None:
        """该 provider 用订阅令牌调模型时，必须前置到 system 的身份前导。

        默认 None；Anthropic 订阅实测要求固定前导，否则被服务端拒绝。
        """
        return None

    def supports_login(self) -> bool:
        """是否支持应用内发起 OAuth 登录。"""
        return False

    def start_login(self) -> dict:
        """创建 OAuth 登录会话并返回登录 URL 等元信息。"""
        raise NotImplementedError(f"{self.provider_id} 不支持应用内登录")

    def complete_login(self, code_text: str) -> SubscriptionCredential:
        """用用户粘贴的授权码完成登录并持久化令牌。"""
        raise NotImplementedError(f"{self.provider_id} 不支持应用内登录")

    def logout(self) -> None:
        """清除该 provider 的本地凭据。"""
        raise NotImplementedError(f"{self.provider_id} 不支持应用内退出登录")
