import re


class ModelConfigHelper:
    """模型配置辅助类，用于获取不同模型的输出限制等配置"""

    # Claude 模型输出限制映射
    CLAUDE_OUTPUT_LIMITS = {
        # Claude 4.6+ adaptive thinking series
        "claude-opus-4-7": 64000,
        "claude-opus-4-6": 64000,
        "claude-sonnet-4-6": 64000,
        # Claude 4.5 系列
        "claude-sonnet-4-5": 64000,
        "claude-haiku-4-5": 64000,
        "claude-opus-4-5": 64000,
        # Claude 4.x 系列
        "claude-opus-4-1": 32000,
        "claude-sonnet-4": 64000,
        "claude-opus-4": 32000,
        # Claude 3.x 系列
        "claude-3-7-sonnet": 64000,
        "claude-3-5-haiku": 8000,
        "claude-3-haiku": 4000,
        "claude-3-opus": 4000,
        "claude-3-sonnet": 4000,
    }
    CLAUDE_DEFAULT_LIMIT = 4000

    # Google 模型输出限制映射
    GOOGLE_OUTPUT_LIMITS = {
        "gemini-3-pro": 65536,
        "gemini-2.5-flash": 65536,
        "gemini-2.5-flash-lite": 65536,
        "gemini-2.5-pro": 65536,
        "gemini-2.0-flash": 8192,
        "gemini-2.0-flash-lite": 8192,
    }
    GOOGLE_DEFAULT_LIMIT = 8192

    @staticmethod
    def _extract_claude_version_info(model_name: str) -> tuple[float, str]:
        """从 Claude 模型名称中提取版本号和模型类型

        返回: (版本号, 模型类型)
        """
        # 提取模型类型
        model_type = ""
        if "haiku" in model_name.lower():
            model_type = "haiku"
        elif "sonnet" in model_name.lower():
            model_type = "sonnet"
        elif "opus" in model_name.lower():
            model_type = "opus"

        # 提取版本号
        version_match = re.search(r'claude-(?:\w+-)?([\d-]+)', model_name)
        if version_match:
            version_str = version_match.group(1).replace('-', '.')
            version_parts = version_str.split('.')[:2]
            try:
                if len(version_parts) == 1:
                    version = float(version_parts[0])
                else:
                    version = float(f"{version_parts[0]}.{version_parts[1]}")
                return version, model_type
            except ValueError:
                pass

        return 0.0, model_type

    @staticmethod
    def _extract_google_version(model_name: str) -> float:
        """从 Google 模型名称中提取版本号"""
        match = re.search(r'gemini-(\d+(?:\.\d+)?)', model_name)
        if match:
            return float(match.group(1))
        return 0.0

    @classmethod
    def is_gemini_3_or_newer(cls, model_name: str) -> bool:
        """检测是否为 Gemini 3.x 或更新版本"""
        version = cls._extract_google_version(model_name)
        return version >= 3.0

    @classmethod
    def get_thinking_level_options(cls, model_name: str) -> list[str]:
        """获取模型支持的 thinking_level 选项

        Gemini 3 Pro: low, high
        Gemini 3 Flash: minimal, low, medium, high
        """
        if "flash" in model_name.lower():
            return ["minimal", "low", "medium", "high"]
        else:  # Pro 模型
            return ["low", "high"]

    @classmethod
    def get_claude_max_output_tokens(cls, model_name: str) -> int:
        """获取 Claude 模型的最大输出 token 限制"""
        name = model_name or ""

        # 新式 minor 版本（claude-<type>-<major>-<minor>）先走推断，
        # 避免 claude-opus-4-7 被旧的 claude-opus-4 前缀误判成 32K。
        major, minor, m_type = cls._parse_claude_new_style(name.lower())
        if m_type:
            if (major, minor) >= (4, 5):
                return 64000
            if major >= 4:
                return 32000 if m_type == "opus" else 64000

        # 优先检查已知模型
        for known_model, limit in sorted(cls.CLAUDE_OUTPUT_LIMITS.items(),
                                         key=lambda x: len(x[0]),
                                         reverse=True):
            if known_model in name:
                return limit

        # 根据版本号和类型推断
        version, model_type = cls._extract_claude_version_info(name)

        if version > 0 and model_type:
            # Claude 4.5+: 统一 64K
            if version >= 4.5:
                return 64000

            # Claude 4.x
            elif version >= 4.0:
                if model_type == "opus":
                    return 32000
                else:
                    return 64000

            # Claude 3.x
            elif version >= 3.0:
                if version >= 3.5:
                    if model_type == "sonnet":
                        return 64000
                    elif model_type == "haiku":
                        return 8000
                return 4000

        # 使用默认值
        return cls.CLAUDE_DEFAULT_LIMIT

    @classmethod
    def claude_thinking_mode(cls, model_name: str) -> str:
        """返回 Claude 模型的思考配置模式：'adaptive' 或 'budget'。

        - adaptive：Opus 4.6/4.7、Sonnet 4.6、Mythos 及更新 —— 用
          ``thinking:{type:"adaptive"}`` + 顶层 ``output_config.effort``；
          旧式 ``budget_tokens`` 在 Opus 4.7 会返回 400。
        - budget：旧模型（Opus/Sonnet 4.5 等）—— 仍用
          ``thinking:{type:"enabled", budget_tokens}``。

        判定保守：未知/无法识别版本时回退 budget（兼容性最好）。
        不复用 _extract_claude_version_info（其正则对 "claude-3-7-sonnet"
        这类「版本在中间、类型在后」的旧命名会误判为高版本）。
        """
        name = (model_name or "").lower()
        if "mythos" in name:
            return "adaptive"
        major, minor, m_type = cls._parse_claude_new_style(name)
        if m_type in ("opus", "sonnet") and (major, minor) >= (4, 6):
            return "adaptive"
        return "budget"

    @staticmethod
    def _parse_claude_new_style(name: str) -> tuple[int, int, str]:
        """只解析新式命名 ``claude-<type>-<major>-<minor>``（4.x 起，类型在前）。

        旧式 3.x（``claude-3-7-sonnet``）/ 4.0 日期命名
        （``claude-opus-4-20250514``）/ 无法识别 → (0,0,"")，
        天然回退 budget。允许 minor 后再带日期后缀，如
        ``claude-opus-4-7-20991231``。
        """
        m = re.search(r"claude-(opus|sonnet|haiku)-(\d+)-(\d{1,2})(?:-|$)", name)
        if m:
            return int(m.group(2)), int(m.group(3)), m.group(1)
        return 0, 0, ""

    @classmethod
    def claude_effort(cls, think_depth: str, model_name: str) -> str:
        """把 AiNiee 的 think_depth 映射为 Anthropic 的 effort 档位。

        合法 effort：low / medium / high / xhigh(仅 Opus 4.7+) / max。
        """
        depth = (think_depth or "high").lower()
        if depth in ("low", "medium", "high", "max"):
            return depth
        if depth == "xhigh":
            major, minor, m_type = cls._parse_claude_new_style((model_name or "").lower())
            if m_type == "opus" and (major, minor) >= (4, 7):
                return "xhigh"
            return "max"  # 其它 adaptive 模型不支持 xhigh，用 max 近似
        return "high"

    @classmethod
    def get_google_max_output_tokens(cls, model_name: str) -> int:
        """获取 Google 模型的最大输出 token 限制"""
        # 优先检查已知模型
        for known_model, limit in sorted(cls.GOOGLE_OUTPUT_LIMITS.items(),
                                         key=lambda x: len(x[0]),
                                         reverse=True):
            if known_model in model_name:
                return limit

        # 根据版本号推断
        version = cls._extract_google_version(model_name)
        if version > 0:
            if version >= 2.5:
                return 65536
            else:
                return 8192

        # 使用默认值
        return cls.GOOGLE_DEFAULT_LIMIT
