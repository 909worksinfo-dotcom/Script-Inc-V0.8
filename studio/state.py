# -*- coding: utf-8 -*-
"""会话级 UI 状态（仅控件/配置）+ 模型配置读取。

注意：产出 / 记忆 / 运行进度 / 过程日志 等「需要跨会话与刷新存活」的数据已迁移到
studio.store.RunStore（单例），不再放在 session_state，详见 store.py 说明。
"""

import streamlit as st

from .employees import EMPLOYEES, default_skills_block
from .tasks import TASK_ORDER, TASK_METHODS


def init_state():
    # 仅初始化「会话级 UI 控件」状态（这些本就应随会话；run 数据在 store 里）
    if "total_episodes" not in st.session_state:
        st.session_state.total_episodes = 50
    if "script_mode" not in st.session_state:
        st.session_state.script_mode = "standard"
    if "seed" not in st.session_state:
        st.session_state.seed = ""
    if "global_cfg" not in st.session_state:
        st.session_state.global_cfg = {"provider": "Mock (演示)", "key": "", "model": "mock-studio-model"}
    if "per_emp" not in st.session_state:
        st.session_state.per_emp = False
    if "emp_cfg" not in st.session_state:
        st.session_state.emp_cfg = {}

    # 「数字员工设定」与「任务协作方式」编辑框默认值
    # （默认展示 PRD 内置设定，用户可改写；执行时从这里快照进 store 实际生效）
    for k in EMPLOYEES:
        st.session_state.setdefault(f"emp_intro_{k}", EMPLOYEES[k]["intro"])
        st.session_state.setdefault(f"emp_skills_{k}", default_skills_block(k))
    for tid in TASK_ORDER:
        st.session_state.setdefault(f"task_method_{tid}", TASK_METHODS[tid])


def get_emp_config(emp_key):
    """读取某数字员工的模型配置（个性化优先，否则用全局默认）。"""
    if st.session_state.per_emp and emp_key in st.session_state.emp_cfg:
        return st.session_state.emp_cfg[emp_key]
    return st.session_state.global_cfg
