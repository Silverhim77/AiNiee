from ModuleFolders.Base.Base import Base
from ModuleFolders.Log.Log import LogMixin
from ModuleFolders.Infrastructure.LLMRequester.LLMClientFactory import LLMClientFactory
from ModuleFolders.Infrastructure.LLMRequester.ModelConfigHelper import ModelConfigHelper
from ModuleFolders.Infrastructure.Auth.CredentialManager import (
    CredentialManager,
    SubscriptionAuthError,
)

# 订阅按 token 用量计 5h/7d 滚动窗口配额；翻译分块输出很短，套模型理论上限
# (如 64000) + high effort 会让单请求开销巨大、极易触顶订阅限流。订阅模式克制此值。
_OAUTH_MAX_TOKENS = 8192


# 接口请求器
class AnthropicRequester(LogMixin, Base):
    def __init__(self) -> None:
        pass

    def _calculate_budget_tokens(self, think_depth: str, max_tokens: int) -> int:
        """
        根据思考深度档位计算 budget_tokens（仅旧模型的 type:"enabled" 路径用）
        参考比例：low ~10%, medium ~40%, high ~70%
        Anthropic 要求 budget_tokens 最小值为 1024
        """
        ratio_map = {
            "low": 0.1,
            "medium": 0.4,
            "high": 0.7,
            "xhigh": 0.9,
        }
        ratio = ratio_map.get(think_depth, 0.5)  # 默认 medium
        budget = int(max_tokens * ratio)
        # Anthropic 的 budget_tokens 最小值是 1024
        return max(1024, budget)

    @staticmethod
    def _status_of(e: Exception):
        """尽量从 anthropic SDK 异常取 HTTP 状态码。"""
        return getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)

    @staticmethod
    def _is_auth_error(e: Exception, status) -> bool:
        if status in (401, 403):
            return True
        s = str(e).lower()
        return any(k in s for k in ("unauthor", "invalid_token", "invalid token", "oauth", "authentication"))

    @staticmethod
    def _move_prompt_to_user(messages: list[dict], system_prompt: str) -> list[dict]:
        adapted_messages = [dict(message) for message in messages]
        if not system_prompt:
            return adapted_messages

        prompt_content = (
            "<AiNieePrompt>\n"
            f"{system_prompt}\n"
            "</AiNieePrompt>\n\n"
        )

        for message in adapted_messages:
            if message.get("role") != "user":
                continue

            content = message.get("content", "")
            if isinstance(content, str):
                message["content"] = prompt_content + content
            elif isinstance(content, list):
                message["content"] = [{"type": "text", "text": prompt_content}] + content
            else:
                message["content"] = prompt_content + str(content)
            return adapted_messages

        return [{"role": "user", "content": prompt_content.rstrip()}] + adapted_messages

    @staticmethod
    def _collect_content(blocks) -> tuple[str, str]:
        """收集所有 thinking / text block 后拼接。响应可能含多个 block，
        只取最后一个会丢失前半段译文。适配所有 Anthropic 路径（含纯 API key）。"""
        think_parts: list[str] = []
        text_parts: list[str] = []
        for block in blocks:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                think_parts.append(getattr(block, "thinking", "") or "")
            elif btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
        return "\n".join(p for p in think_parts if p), "".join(text_parts)

    def _do_create(self, base_params: dict, platform_config: dict):
        client = LLMClientFactory().get_anthropic_client(platform_config)
        return client.messages.create(**base_params)

    def request_anthropic(self, messages, system_prompt, platform_config) -> tuple[bool, str, str, int, int]:
        try:
            model_name = platform_config.get("model_name")
            request_timeout = platform_config.get("request_timeout", 60)
            temperature = platform_config.get("temperature", 1.0)
            think_switch = platform_config.get("think_switch")
            think_depth = platform_config.get("think_depth")

            is_oauth = platform_config.get("auth_method") == "oauth"
            provider_id = platform_config.get("oauth_provider", "anthropic")

            max_tokens = ModelConfigHelper.get_claude_max_output_tokens(model_name)
            # 订阅模式克制 max_tokens（min 防止抬高本就更小的旧模型上限），
            # 大幅降低单请求开销，避免毒化订阅 5h/7d 限流窗口
            if is_oauth:
                max_tokens = min(max_tokens, _OAUTH_MAX_TOKENS)

            # OAuth 订阅模式：Claude Code 身份前导留在 system；
            # AiNiee 翻译规则/术语表/格式要求移入 user，避免污染身份层提示。
            if is_oauth:
                # 每次请求前即时取有效令牌（CredentialManager 30s 缓存 + 过期前
                # 自动刷新）：长任务中令牌临期会被提前换新，避免每个分块各吃一次
                # 401 重试再刷新的浪费；未登录/刷新失败抛 SubscriptionAuthError，
                # 由下方对应 except 妥善处理。
                cm = CredentialManager()
                fresh_token = cm.get_valid_access_token(provider_id)
                platform_config = dict(platform_config)
                platform_config["oauth_access_token"] = fresh_token
                platform_config["api_key"] = fresh_token
                auth_system_prompt = cm.required_system_preamble(provider_id) or ""
                messages = self._move_prompt_to_user(messages, system_prompt)
            else:
                auth_system_prompt = system_prompt

            # 参数基础配置
            base_params = {
                "model": model_name,
                "system": auth_system_prompt,
                "messages": messages,
                "timeout": request_timeout,
                "max_tokens": max_tokens,
            }

            # 按模型版本选择思考配置：新模型 adaptive+effort，旧模型 budget_tokens
            mode = ModelConfigHelper.claude_thinking_mode(model_name)
            if think_switch:
                if mode == "adaptive":
                    # display=summarized：默认展示思考摘要（opus-4-7 默认 omitted 会让思考块为空）
                    base_params["thinking"] = {"type": "adaptive", "display": "summarized"}
                    base_params["output_config"] = {
                        "effort": ModelConfigHelper.claude_effort(think_depth, model_name)
                    }
                    # adaptive 下 temperature 只能为 1（实测传其它值 400），故省略用默认
                else:
                    base_params["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": self._calculate_budget_tokens(think_depth, max_tokens),
                    }
                    base_params["temperature"] = 1.0
            else:
                if mode == "adaptive":
                    # 关思考时 adaptive 模型即便省略 thinking，服务端 effort 仍默认偏高
                    # （延迟/用量意外）。显式下发低 effort，与 think_depth 解耦；
                    # 此修复对纯 API key 的新模型同样生效，非仅订阅。
                    base_params["output_config"] = {"effort": "low"}
                    # adaptive 类模型 temperature 限制不明（订阅接口也不暴露该项），
                    # OAuth 下保守不发，走默认 1
                    if not is_oauth:
                        base_params["temperature"] = temperature
                else:
                    base_params["temperature"] = temperature

            # 发送请求；OAuth 模式遇认证失败先强制刷新令牌再重试一次
            try:
                response = self._do_create(base_params, platform_config)
            except Exception as e:  # noqa: BLE001
                status = self._status_of(e)
                if is_oauth and self._is_auth_error(e, status):
                    self.warning("订阅令牌疑似失效，正在尝试自动刷新后重试 ...")
                    # 传入刚失败的令牌：并发分块同时 401 时，force_refresh 进锁后
                    # 双检，只有第一个真正打网络刷新，其余复用已轮换的新令牌，
                    # 避免刷新风暴反复消耗单次有效的 refresh_token。
                    stale = platform_config.get("oauth_access_token")
                    new_cred = CredentialManager().force_refresh(provider_id, stale)
                    platform_config = dict(platform_config)
                    platform_config["oauth_access_token"] = new_cred.access_token
                    platform_config["api_key"] = new_cred.access_token
                    response = self._do_create(base_params, platform_config)
                else:
                    raise

            # 提取回复文本/思考（多 block 收集拼接，避免只留最后一个丢译文）
            response_think, response_content = self._collect_content(response.content)

        except SubscriptionAuthError as e:
            if Base.work_status == Base.STATUS.STOPING:
                return True, None, None, None, None
            self.error(f"订阅账号不可用：{e}")
            return True, None, None, None, None
        except Exception as e:
            if Base.work_status == Base.STATUS.STOPING:
                return True, None, None, None, None
            status = self._status_of(e)
            if status in (429, 529):
                self.warning(
                    "Claude 订阅额度受限或服务繁忙（HTTP "
                    f"{status}），将在后续轮次自动重试该批次 ..."
                )
            else:
                self.error(f"请求任务错误 ... {e}", e)
            return True, None, None, None, None

        # 获取指令消耗（Anthropic 使用 input_tokens）
        try:
            prompt_tokens = int(response.usage.input_tokens)
        except Exception:
            prompt_tokens = 0

        # 获取回复消耗（Anthropic 使用 output_tokens）
        try:
            completion_tokens = int(response.usage.output_tokens)
        except Exception:
            completion_tokens = 0

        return False, response_think, response_content, prompt_tokens, completion_tokens
