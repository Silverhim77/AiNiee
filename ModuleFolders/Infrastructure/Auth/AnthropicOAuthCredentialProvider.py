"""AiNiee 内置 Anthropic OAuth 凭据 provider。

登录流程为：
1. AiNiee 生成 PKCE code_verifier / code_challenge，并打开 Claude 授权页。
2. 用户在浏览器完成授权后，将页面显示的 ``code#state`` 或回调 URL 粘贴回 AiNiee。
3. AiNiee 用授权码换取 access_token / refresh_token，并保存到自己的可写数据目录。

Anthropic 目前公开文档确认 Claude Code/ant CLI 都使用浏览器 OAuth 登录，但未公开
消费订阅 OAuth 的完整应用接入常量。这里沿用 Claude Code 2.1.141 的公共
client_id、授权/回调端点与订阅 scopes；若上游调整授权页或回调域名，登录流程
可能需要更新常量。
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ModuleFolders.Config.FilePathConfig import user_data_root
from ModuleFolders.Infrastructure.Auth.CredentialProvider import (
    CredentialProvider,
    SubscriptionCredential,
)

_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_SCOPES = (
    "org:create_api_key",
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)
_CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


def _auth_dir() -> Path:
    return user_data_root() / "Auth"


def _token_path() -> Path:
    return _auth_dir() / "anthropic_oauth.json"


def _pending_path() -> Path:
    return _auth_dir() / "anthropic_oauth_pending.json"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class AnthropicOAuthCredentialProvider(CredentialProvider):
    _io_lock = threading.Lock()

    @property
    def provider_id(self) -> str:
        return "anthropic"

    def required_system_preamble(self) -> str | None:
        return _CLAUDE_CODE_SYSTEM

    def supports_login(self) -> bool:
        return True

    def load(self) -> SubscriptionCredential | None:
        path = _token_path()
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

        access = raw.get("access_token") or raw.get("accessToken")
        if not access:
            return None

        exp = raw.get("expires_at", 0) or raw.get("expiresAt", 0) or 0
        exp_s = exp / 1000 if isinstance(exp, (int, float)) and exp > 1e12 else float(exp or 0)
        scopes = raw.get("scopes", [])
        if isinstance(scopes, str):
            scopes = scopes.split()

        return SubscriptionCredential(
            access_token=access,
            refresh_token=raw.get("refresh_token") or raw.get("refreshToken", ""),
            expires_at=exp_s,
            scopes=list(scopes),
            subscription_type=raw.get("subscription_type") or raw.get("subscriptionType", ""),
            provider_id=self.provider_id,
            raw=raw,
        )

    def start_login(self) -> dict:
        verifier = _b64url(secrets.token_bytes(32))
        state = _b64url(secrets.token_bytes(32))
        pending = {
            "provider_id": self.provider_id,
            "created_at": time.time(),
            "code_verifier": verifier,
            "state": state,
            "redirect_uri": _REDIRECT_URI,
            "scopes": list(_SCOPES),
        }
        params = {
            "code": "true",
            "client_id": _CLIENT_ID,
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "scope": " ".join(_SCOPES),
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
        auth_url = f"{_AUTHORIZE_URL}?{urlencode(params)}"
        with type(self)._io_lock:
            _atomic_write_json(_pending_path(), pending)
        return {
            "auth_url": auth_url,
            "expires_at": pending["created_at"] + 600,
            "redirect_uri": _REDIRECT_URI,
        }

    def complete_login(self, code_text: str) -> SubscriptionCredential:
        pending = self._load_pending()
        code, returned_state = self._extract_code_and_state(code_text)
        if not code:
            raise RuntimeError("授权码为空。")

        expected_state = pending.get("state", "")
        if expected_state and not returned_state:
            raise RuntimeError("请粘贴完整的授权码，或粘贴完整回调 URL。")
        if expected_state and returned_state != expected_state:
            raise RuntimeError("授权状态不匹配，请重新登录。")

        # 必须用 start_login 时存下的 PKCE code_verifier；不能拿 state 顶替
        # （state≠verifier，顶替必然导致 token 交换失败且错误难解读）
        verifier = pending.get("code_verifier")
        if not verifier:
            raise RuntimeError("登录会话已失效，请重新打开登录页面。")

        token = self._exchange_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": _CLIENT_ID,
                "redirect_uri": pending.get("redirect_uri") or _REDIRECT_URI,
                "code_verifier": verifier,
                "state": expected_state,
            }
        )
        cred = self._persist_token(token, fallback_scopes=pending.get("scopes") or list(_SCOPES))
        self._remove_file(_pending_path())
        return cred

    def refresh(self, cred: SubscriptionCredential) -> SubscriptionCredential:
        if not cred.refresh_token:
            raise RuntimeError("订阅凭据缺少 refresh_token，无法自动续期，请重新登录。")
        token = self._exchange_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": cred.refresh_token,
                "client_id": _CLIENT_ID,
                "scope": " ".join(cred.scopes or list(_SCOPES)),
            }
        )
        return self._persist_token(token, old_cred=cred)

    def logout(self) -> None:
        with type(self)._io_lock:
            self._remove_file(_token_path())
            self._remove_file(_pending_path())

    def _load_pending(self) -> dict:
        path = _pending_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError("没有有效的登录会话，请重新打开登录页面。") from e
        if time.time() - float(pending.get("created_at", 0) or 0) > 900:
            raise RuntimeError("登录会话已过期，请重新打开登录页面。")
        return pending

    @staticmethod
    def _extract_code_and_state(code_text: str) -> tuple[str, str]:
        text = (code_text or "").strip().strip("`")
        if not text:
            return "", ""

        parsed = urlparse(text)
        if parsed.scheme and parsed.netloc:
            params = parse_qs(parsed.query)
            fragment_params = parse_qs(parsed.fragment)
            code = (params.get("code") or fragment_params.get("code") or [""])[0]
            state = (params.get("state") or fragment_params.get("state") or [""])[0]
            return code.strip(), state.strip()

        compact = "".join(text.split())
        if "#" in compact:
            code, state = compact.split("#", 1)
            return code.strip(), state.strip()
        return compact, ""

    def _exchange_token(self, payload: dict) -> dict:
        last_err = ""
        with httpx.Client(timeout=60, http2=False) as client:
            try:
                resp = client.post(
                    _TOKEN_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            except Exception as e:  # noqa: BLE001
                last_err = f"{_TOKEN_URL} 异常 {e}"
            else:
                if resp.status_code // 100 == 2:
                    try:
                        return resp.json()
                    except ValueError as e:
                        last_err = f"{_TOKEN_URL} 返回非 JSON：{resp.text[:200]}"
                else:
                    last_err = f"{_TOKEN_URL} HTTP {resp.status_code} {self._oauth_error_text(resp)}"
        raise RuntimeError(f"OAuth token 交换失败：{last_err}")

    @staticmethod
    def _oauth_error_text(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.text[:200]

        error = str(data.get("error", "") or data.get("type", ""))
        description = str(data.get("error_description", "") or data.get("message", ""))
        if error == "invalid_grant":
            return "授权码已失效或不属于当前登录链接，请重新打开登录页面后粘贴新授权码。"
        detail = description or error or resp.text
        return detail[:200]

    def _persist_token(
        self,
        token: dict,
        old_cred: SubscriptionCredential | None = None,
        fallback_scopes: list | None = None,
    ) -> SubscriptionCredential:
        access = token.get("access_token") or token.get("accessToken")
        if not access:
            raise RuntimeError("OAuth 响应缺少 access_token，已拒绝写入本地凭据。")

        refresh = token.get("refresh_token") or token.get("refreshToken")
        if not refresh and old_cred is not None:
            refresh = old_cred.refresh_token

        expires_in = token.get("expires_in") or token.get("expiresIn")
        expires_at = token.get("expires_at") or token.get("expiresAt")
        if expires_in is not None:
            exp_s = time.time() + float(expires_in)
        elif expires_at:
            exp_s = float(expires_at) / 1000 if float(expires_at) > 1e12 else float(expires_at)
        elif old_cred is not None:
            exp_s = old_cred.expires_at
        else:
            exp_s = 0.0

        scopes = token.get("scopes") or token.get("scope") or fallback_scopes or []
        if isinstance(scopes, str):
            scopes = scopes.split()

        subscription_type = (
            token.get("subscription_type")
            or token.get("subscriptionType")
            or (old_cred.subscription_type if old_cred is not None else "")
        )

        raw = {
            "provider_id": self.provider_id,
            "source": "ainiee_oauth",
            "access_token": access,
            "refresh_token": refresh or "",
            "expires_at": exp_s,
            "scopes": list(scopes),
            "subscription_type": subscription_type,
            "updated_at": time.time(),
            "token_type": token.get("token_type") or token.get("tokenType", ""),
        }

        with type(self)._io_lock:
            _atomic_write_json(_token_path(), raw)

        return SubscriptionCredential(
            access_token=access,
            refresh_token=refresh or "",
            expires_at=exp_s,
            scopes=list(scopes),
            subscription_type=subscription_type,
            provider_id=self.provider_id,
            raw=raw,
        )

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
