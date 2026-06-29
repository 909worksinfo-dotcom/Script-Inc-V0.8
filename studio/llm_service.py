# -*- coding: utf-8 -*-
"""服务商 / 模型清单 + LLMService（复用原工具，新增 mock_key 路由）。"""

import time

from .mock_data import (
    MOCK_IDEA,
    MOCK_3ACT_V1,
    MOCK_REVIEW_3ACT,
    MOCK_3ACT_FINAL,
    MOCK_REVIEW_OUTLINE,
    _mock_outline,
    _mock_storyboard,
)

# --- 引入大模型 SDK (容错导入，与原工具一致) ---
try:
    import google.generativeai as genai
except ImportError:
    genai = None
try:
    import openai
except ImportError:
    openai = None
try:
    import anthropic
except ImportError:
    anthropic = None


# 服务商与可用模型（选项与原工具 foragent.py 完全一致；额外提供 Mock 用于离线演示）
PROVIDERS = [
    "Mock (演示)",
    "Azure OpenAI (ByteDance)",
    "OpenRouter",
    "Google Gemini",
    "OpenAI (GPT)",
    "Anthropic (Claude)",
]
MODELS = {
    "Mock (演示)": ["mock-studio-model"],
    "Azure OpenAI (ByteDance)": ["gpt-5.5-2026-04-24", "gemini-3.1-p", "gemini-3.1-p-priority", "gemini-3.5-flash", "test"],
    "OpenRouter": [
        "anthropic/claude-opus-4.8",
        "anthropic/claude-opus-4.7",
        "anthropic/claude-fable-5",
        "anthropic/claude-fable-5",
        "~anthropic/claude-sonnet-latest",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.5-flash",
        "openai/gpt-5.5-pro",
        "openai/gpt-5.5",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
    ],
    "Google Gemini": ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3-pro-preview", "gemini-3.1-pro-preview"],
    "OpenAI (GPT)": ["gpt-4o", "gpt-4-turbo", "gpt-4o-mini", "gpt-3.5-turbo"],
    "Anthropic (Claude)": ["claude-3-5-sonnet-20240620", "claude-3-opus-20240229", "claude-3-haiku-20240307"],
}

# 模型单次调用的最大输出 token 数。
# 调高至 6 万，使单次调用即可一次性生成全部 50 集分集大纲、且每集写满 500-600 字而不被截断。
MAX_TOKENS = 60000


class LLMService:
    def __init__(self):
        self.provider = "Mock (演示)"
        self.api_key = ""
        self.model_name = ""

    def set_config(self, provider, api_key, model_name):
        self.provider = provider
        self.api_key = api_key
        self.model_name = model_name

    def generate(self, system_prompt: str, user_prompt: str, mock_key: str = None,
                 raise_on_error: bool = False) -> str:
        # 1. Mock 模式
        if self.provider == "Mock (演示)":
            return self._mock_response(user_prompt, mock_key)

        # 2. 真实 API 检查
        if not self.api_key:
            return "❌ 错误：请在侧边栏为该数字员工填写 API Key（或切换为「Mock (演示)」走通流程）"

        try:
            if self.provider == "Google Gemini":
                return self._call_gemini(system_prompt, user_prompt)
            elif self.provider == "OpenAI (GPT)":
                return self._call_openai(system_prompt, user_prompt)
            elif self.provider == "Anthropic (Claude)":
                return self._call_claude(system_prompt, user_prompt)
            elif self.provider == "OpenRouter":
                return self._call_openrouter(system_prompt, user_prompt)
            elif self.provider == "Azure OpenAI (ByteDance)":
                return self._call_azure_openai(system_prompt, user_prompt)
            else:
                return "❌ 未知模型提供商"
        except Exception as e:
            # raise_on_error=True 时把异常上抛，便于调用方区分错误类型并决定是否重试
            if raise_on_error:
                raise
            return f"❌ API 调用异常: {str(e)}"

    # --- 真实模型调用接口 (与原工具一致) ---
    def _call_gemini(self, system_prompt, user_prompt):
        if not genai:
            return "❌ 请安装库: pip install google-generativeai"
        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(model_name=self.model_name, system_instruction=system_prompt)
        response = model.generate_content(
            user_prompt, generation_config={"max_output_tokens": MAX_TOKENS}
        )
        return response.text

    def _call_openai(self, system_prompt, user_prompt):
        if not openai:
            return "❌ 请安装库: pip install openai"
        client = openai.OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            max_tokens=MAX_TOKENS,
        )
        return response.choices[0].message.content

    def _call_claude(self, system_prompt, user_prompt):
        if not anthropic:
            return "❌ 请安装库: pip install anthropic"
        client = anthropic.Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model_name,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text

    def _call_openrouter(self, system_prompt, user_prompt):
        if not openai:
            return "❌ 请安装库: pip install openai"
        client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=MAX_TOKENS,
            extra_body={"reasoning": {"enabled": True}},
        )
        return response.choices[0].message.content

    def _call_azure_openai(self, system_prompt, user_prompt):
        if not openai:
            return "❌ 请安装库: pip install openai"
        log_id = f"tt_script_gen_{int(time.time())}"
        client = openai.AzureOpenAI(
            api_key=self.api_key,
            api_version="2024-03-01",
            azure_endpoint="https://aidp.bytedance.net/api/modelhub/online/v2/crawl",
            default_headers={"X-TT-LOGID": log_id},
        )
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [{"type": "text", "text": user_prompt}]},
            ],
            max_tokens=MAX_TOKENS,
            stream=False,
        )
        return response.choices[0].message.content

    # --- Mock 数据 (按任务 mock_key 路由，离线走通全流程) ---
    def _mock_response(self, prompt_content: str, mock_key: str = None) -> str:
        time.sleep(0.6)
        if mock_key:
            if mock_key == "researcher_idea":
                return MOCK_IDEA
            if mock_key == "three_act_v1":
                return MOCK_3ACT_V1
            if mock_key == "review_3act":
                return MOCK_REVIEW_3ACT
            if mock_key == "three_act_final":
                return MOCK_3ACT_FINAL
            if mock_key == "review_outline":
                return MOCK_REVIEW_OUTLINE
            if mock_key.startswith("outline_final:") or mock_key.startswith("outline:"):
                final = mock_key.startswith("outline_final:")
                parts = mock_key.split(":")
                total = int(parts[1])
                if len(parts) >= 3 and "-" in parts[2]:
                    a, b = parts[2].split("-")
                    return _mock_outline(total, int(a), int(b), final=final)
                return _mock_outline(total, final=final)
            if mock_key.startswith("script:"):
                _, mode, rng = mock_key.split(":")
                a, b = rng.split("-")
                return _mock_storyboard(int(a), int(b), mode)
        return "（Mock 演示）未识别的任务类型。"
