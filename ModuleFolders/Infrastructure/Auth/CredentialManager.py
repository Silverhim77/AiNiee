"""订阅凭据管理：按 provider 提供有效 access_token，过期前加锁自动刷新。

单例。不依赖 ConfigMixin/LogMixin（低层）；令牌存于各 provider 自己的凭据源。
"""

import threading
import time

from ModuleFolders.Infrastructure.Auth.CredentialProvider import (
    CredentialProvider,
    SubscriptionCredential,
)
from ModuleFolders.Infrastructure.Auth.AnthropicOAuthCredentialProvider import (
    AnthropicOAuthCredentialProvider,
)


class SubscriptionAuthError(RuntimeError):
    """订阅未登录 / 刷新失败等，message 为面向用户的中文指引。"""


class CredentialManager:
    _instance = None
    _instance_lock = threading.Lock()
    # 有效凭据的内存缓存时长（秒）。仅为消除批量并发下对凭据文件的重复磁盘读，
    # 取短值：仍按 is_expiring 兜底刷新，并尽快感知官方 CLI 带外轮换，避免 stale。
    _CACHE_TTL = 30.0

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._providers: dict[str, CredentialProvider] = {}
                obj._refresh_locks: dict[str, threading.Lock] = {}
                obj._locks_guard = threading.Lock()
                obj._cred_cache: dict[str, tuple[SubscriptionCredential, float]] = {}
                obj._cache_lock = threading.Lock()
                # 仅 AiNiee 应用内 OAuth；不读写官方 Claude Code 凭据文件。
                obj.register(AnthropicOAuthCredentialProvider())
                cls._instance = obj
            return cls._instance

    def register(self, provider: CredentialProvider) -> None:
        self._providers[provider.provider_id] = provider

    def get_provider(self, provider_id: str) -> CredentialProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise SubscriptionAuthError(f"未知的订阅类型：{provider_id}")
        return provider

    def _refresh_lock(self, provider_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._refresh_locks.get(provider_id)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[provider_id] = lock
            return lock

    def get_credential(self, provider_id: str) -> SubscriptionCredential:
        """返回有效（必要时已刷新）的凭据；未登录/失败抛 SubscriptionAuthError。

        命中短 TTL 内存缓存则直接返回，消除批量并发下对凭据文件的重复磁盘读；
        缓存项仍校验 is_expiring，临近过期则走加锁刷新路径。
        """
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cred_cache.get(provider_id)
            if hit is not None:
                cred, deadline = hit
                if now < deadline and not cred.is_expiring():
                    return cred
        cred = self._resolve_credential(provider_id)
        with self._cache_lock:
            self._cred_cache[provider_id] = (cred, time.monotonic() + self._CACHE_TTL)
        return cred

    def _resolve_credential(self, provider_id: str) -> SubscriptionCredential:
        """实际从 provider 取/刷新凭据（无缓存）。"""
        provider = self.get_provider(provider_id)
        cred = provider.load()
        if cred is None or not cred.access_token:
            raise SubscriptionAuthError(
                "未检测到订阅登录。请在 AiNiee 的「订阅管理」中登录 Claude 订阅账号后重试。"
            )
        if not cred.is_expiring():
            return cred
        # 需要刷新：加 per-provider 锁，双检，避免并发重复刷新轮换互相失效
        lock = self._refresh_lock(provider_id)
        with lock:
            cred = provider.load() or cred
            if not cred.is_expiring():
                return cred
            try:
                return provider.refresh(cred)
            except Exception as e:  # noqa: BLE001
                raise SubscriptionAuthError(
                    f"订阅令牌已过期且自动续期失败：{e}。"
                    f"请在 AiNiee 的「订阅管理」中重新登录 Claude 订阅账号。"
                ) from e

    def get_valid_access_token(self, provider_id: str) -> str:
        return self.get_credential(provider_id).access_token

    def required_system_preamble(self, provider_id: str) -> str | None:
        return self.get_provider(provider_id).required_system_preamble()

    def force_refresh(
        self, provider_id: str, stale_token: str | None = None
    ) -> SubscriptionCredential:
        """401/invalid_token 时强制刷新一次（不看过期时间）。

        stale_token：触发 401 的那个 access_token。并发分块同时 401 会全部
        调本方法，per-provider 锁串行化后逐一双检——若进锁时盘上令牌已不再是
        stale_token，说明已有线程刷新过，直接复用，不再打网络。否则真正刷新。
        这样 N 个并发 401 只产生 1 次刷新，避免反复轮换单次有效的 refresh_token。
        """
        provider = self.get_provider(provider_id)
        cred = provider.load()
        if cred is None:
            raise SubscriptionAuthError(
                "未检测到订阅登录。请在 AiNiee 的「订阅管理」中登录 Claude 订阅账号。"
            )
        with self._refresh_lock(provider_id):
            current = provider.load() or cred
            if (
                stale_token
                and current.access_token
                and current.access_token != stale_token
            ):
                fresh = current  # 已被其它线程刷新过，复用，不再重复轮换
            else:
                try:
                    fresh = provider.refresh(current)
                except Exception as e:  # noqa: BLE001
                    raise SubscriptionAuthError(
                        f"订阅令牌刷新失败：{e}。"
                        f"请在 AiNiee 的「订阅管理」中重新登录 Claude 订阅账号。"
                    ) from e
        with self._cache_lock:
            self._cred_cache[provider_id] = (fresh, time.monotonic() + self._CACHE_TTL)
        return fresh

    def start_login(self, provider_id: str) -> dict:
        provider = self.get_provider(provider_id)
        if not provider.supports_login():
            raise SubscriptionAuthError(f"{provider_id} 不支持应用内登录。")
        return provider.start_login()

    def complete_login(self, provider_id: str, code_text: str) -> SubscriptionCredential:
        provider = self.get_provider(provider_id)
        if not provider.supports_login():
            raise SubscriptionAuthError(f"{provider_id} 不支持应用内登录。")
        try:
            cred = provider.complete_login(code_text)
        except Exception as e:  # noqa: BLE001
            raise SubscriptionAuthError(f"订阅登录失败：{e}") from e
        with self._cache_lock:
            self._cred_cache[provider_id] = (cred, time.monotonic() + self._CACHE_TTL)
        return cred

    def logout(self, provider_id: str) -> None:
        provider = self.get_provider(provider_id)
        try:
            provider.logout()
        except NotImplementedError:
            pass
        with self._cache_lock:
            self._cred_cache.pop(provider_id, None)

    def get_status_meta(self, provider_id: str) -> dict:
        """供 UI 显示用，绝不含令牌本身。"""
        try:
            provider = self.get_provider(provider_id)
        except SubscriptionAuthError:
            return {"logged_in": False, "reason": "unknown_provider"}
        cred = provider.load()
        if cred is None or not cred.access_token:
            return {"logged_in": False}
        return {
            "logged_in": True,
            "source": cred.raw.get("source", provider_id),
            "subscription_type": cred.subscription_type,
            "scopes": cred.scopes,
            "expires_at": cred.expires_at,
            "seconds_left": cred.seconds_left(),
            "expiring": cred.is_expiring(),
        }
