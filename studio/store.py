# -*- coding: utf-8 -*-
"""会话无关的全局运行存储（单例）+ 基于它的纯逻辑工具。

为什么需要它（修复线上稳定性）：
- Streamlit 的脚本运行与浏览器会话/WebSocket 绑定。电脑休眠/长时间不操作时连接会断开，
  Streamlit 会中断当前脚本运行 → "一键运行全流程"被打断、跑不到最后。
- st.session_state 是「按会话隔离」的，浏览器重连会新建会话 → 之前的产出/过程输出丢失。

解决方案：
- 用 @st.cache_resource 提供一个「跨会话、跨重连、跨刷新」都存在的单例 RunStore，
  把产出 / 进度 / 过程日志都存在这里（而不是 session_state）。
- 长流程在「后台线程」里跑（见 engine.run_pipeline），只读写本 RunStore，不触碰 st.*，
  因此浏览器断连不会中断它；重连后 UI 从 RunStore 读取，进度与过程输出都不丢失
  （除非用户主动点「重置工作室」）。
"""

import time
import threading

import streamlit as st

from .tasks import TASK_MAP, TASK_ORDER
from .employees import EMPLOYEES
from .llm_service import LLMService


class RunStore:
    """单例运行存储。所有写操作均加锁，可被后台线程与前台脚本安全并发访问。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.outputs = {i: None for i in TASK_ORDER}
        self.outputs[8] = {}
        self.memory = {k: [] for k in EMPLOYEES}
        self.running_task = None       # 当前正在执行的任务号（用于办公室/流水线高亮）
        self.is_running = False        # 是否有后台流水线正在跑
        self.cancel = False            # 取消标志（点重置时置位，后台线程尽快停止并丢弃在途写入）
        self.failed_task = None        # 最近失败的任务号（用于"任务失败"提示）
        self.log = []                  # 过程输出日志（逐行，持久化、不随刷新消失）
        self.thread = None
        # 运行参数快照（启动一次运行时从 session_state 拷入；后台线程只读它，不访问 session_state）
        self.total_episodes = 50
        self.script_mode = "standard"
        self.seed = ""
        self.emp_configs = {}          # {emp_key: {"provider","key","model"}}
        # 用户自定义的「数字员工设定」与「任务协作方式」（执行时实际生效；留空则用 PRD 默认）
        self.emp_settings = {}         # {emp_key: {"intro": str, "skills": str}}
        self.task_methods = {}         # {tid: str}
        # 任务9最终飞书交付文档的历史归档（当前 + 过去），跨刷新/重连存活
        self.doc_history = []          # [{"time","title","content"}]

    # ---------- 数据操作（线程安全） ----------
    def reset(self):
        with self.lock:
            self.cancel = True         # 通知后台线程停止并丢弃在途写入
            self.is_running = False    # 强制解锁（即使后台线程异常未复位，也立刻可手动操作）
            self.outputs = {i: None for i in TASK_ORDER}
            self.outputs[8] = {}
            self.memory = {k: [] for k in EMPLOYEES}
            self.running_task = None
            self.failed_task = None
            self.log = []

    def force_stop(self):
        """停止当前后台运行并立即解锁手动操作（不清空已产出的结果）。"""
        with self.lock:
            self.cancel = True
            self.is_running = False
            self.running_task = None

    def log_line(self, msg):
        with self.lock:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(self.log) > 500:
                self.log = self.log[-500:]

    def set_output(self, tid, value):
        with self.lock:
            self.outputs[tid] = value

    def set_batch(self, label, value):
        with self.lock:
            self.outputs[8][label] = value

    def clear_output(self, tid):
        with self.lock:
            if tid == 8:
                self.outputs[8] = {}
            else:
                self.outputs[tid] = None

    def add_memory(self, emp_key, note):
        with self.lock:
            mem = self.memory.setdefault(emp_key, [])
            if note not in mem:
                mem.append(note)

    def add_doc_history(self, title, content):
        """把一份任务9最终飞书文档归档进历史（与上一条内容相同则跳过，避免重复）。"""
        with self.lock:
            if not content:
                return
            if self.doc_history and self.doc_history[-1]["content"] == content:
                return
            self.doc_history.append({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "title": title or "未命名短剧",
                "content": content,
            })
            if len(self.doc_history) > 50:
                self.doc_history = self.doc_history[-50:]

    def clear_doc_history(self):
        with self.lock:
            self.doc_history = []

    def snapshot_config(self, total_episodes, script_mode, seed, emp_configs,
                        emp_settings=None, task_methods=None):
        with self.lock:
            self.total_episodes = total_episodes
            self.script_mode = script_mode
            self.seed = seed
            self.emp_configs = emp_configs
            if emp_settings is not None:
                self.emp_settings = emp_settings
            if task_methods is not None:
                self.task_methods = task_methods


@st.cache_resource
def get_store():
    """跨会话 / 重连 / 刷新都返回同一个 RunStore 单例。"""
    return RunStore()


# ---------- 基于 store 的纯逻辑工具 ----------
def task_done(store, tid):
    v = store.outputs.get(tid)
    if tid == 8:
        return isinstance(v, dict) and len(v) > 0
    return isinstance(v, str) and v.strip() != "" and not v.startswith("❌")


def is_ready(store, tid):
    return all(task_done(store, d) for d in TASK_MAP[tid]["deps"])


def get_batches(total, size=10):
    out = []
    for i in range(1, total + 1, size):
        out.append((i, min(i + size - 1, total)))
    return out


def make_service(store, emp_key):
    """按运行参数快照里的配置创建 LLMService（后台线程可用，不依赖 session_state）。"""
    cfg = store.emp_configs.get(emp_key) or {
        "provider": "Mock (演示)", "key": "", "model": "mock-studio-model",
    }
    svc = LLMService()
    svc.set_config(cfg["provider"], cfg["key"], cfg["model"])
    return svc
