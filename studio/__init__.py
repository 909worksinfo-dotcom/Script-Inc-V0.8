# -*- coding: utf-8 -*-
"""
studio —— AI Agent 短剧剧本工作室 应用包

模块划分：
- prompts      : 原工具 foragent.py 的 Prompts 模板（逐字还原）
- employees    : 5 位数字员工设定、Agent 技能、角色系统提示
- tasks        : 9 个协作任务定义、记忆备注、任务指令文案
- mock_data    : Mock 演示内容（离线走通全流程）
- llm_service  : 服务商 / 模型清单 + LLMService（复用原工具）
- state        : 会话状态初始化与读写、模型配置、批次切分
- engine       : 任务编排执行、飞书文档编排、分镜 CSV 解析、集数校验
- visuals      : 心跳保活、样式、办公室与流水线可视化
- ui           : 侧边栏、两种交互方式、产出展示、数字员工档案、main()
"""

__all__ = ["ui"]
