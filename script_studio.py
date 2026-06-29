# -*- coding: utf-8 -*-
"""
🎬 AI Agent 短剧剧本工作室 (Script Studio) —— 入口脚本
================================================================
按《AI Agent剧本工作室 产品需求文档》构建：
- 由 5 位 AI Agent 数字员工组成的剧本工作室
- 完全复用《三幕式创意和剧本生成0522带解说漫foragent.py》的 Prompts 与 LLMService 作为生成引擎
- 9 个协作任务，工作过程完全可视化
- 两种交互方式：①自动按序执行  ②手动逐步启动
- 每位数字员工可独立配置 API 服务商 / API Key / 模型（选项与原工具完全一致）
- 为每位数字员工配备 Agent skills 与 memory

运行方式：
    pip install -r requirements.txt
    streamlit run script_studio.py

工程结构：本入口仅负责页面配置并启动应用，全部业务逻辑拆分在 studio/ 包内：
    studio/prompts.py · employees.py · tasks.py · mock_data.py · llm_service.py
    studio/state.py   · engine.py    · visuals.py · ui.py

说明：默认使用「Mock (演示)」服务商，可在完全离线状态下走通全流程与可视化；
      若需真实生成，请在侧边栏选择真实服务商并填入 API Key。
"""

import os
import sys

import streamlit as st

# 确保 studio 包可被导入：把本入口所在目录加入 sys.path（兼容任意启动方式 / 工作目录）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# st.set_page_config 必须是第一个被调用的 Streamlit 命令，因此放在入口、且在导入 main 之前完成。
st.set_page_config(
    page_title="AI Agent 短剧剧本工作室",
    page_icon="🎬",
    layout="wide",
)

from studio.ui import main

main()
