# -*- coding: utf-8 -*-
"""UI 编排：侧边栏、两种交互方式、产出展示、数字员工档案、main()。

线上稳定性设计（修复休眠/断连中断、刷新丢产出）：
- 长流程（自动一键运行 / 手动"自动执行后续剩余任务"）放到「后台线程」执行，写入单例 RunStore；
- UI 在后台运行时用「fragment 定时器」每约 1.5s 触发一次完整重跑刷新进度（保留 session_state 不丢配置/Key），从 RunStore 读取进度与产出；
- 即使浏览器断连/电脑休眠，后台线程仍在服务器继续跑；重连后进度与过程日志都还在（除非主动重置）。
"""

import threading

import streamlit as st

from .employees import EMPLOYEES, AGENT_SKILLS, default_skills_block
from .tasks import TASK_MAP, TASK_ORDER, TASK_METHODS
from .llm_service import PROVIDERS, MODELS
from .state import init_state, get_emp_config
from .store import get_store, task_done, is_ready, get_batches
from .engine import (
    run_generic_task,
    run_task8_batch,
    run_task9,
    run_pipeline,
    count_episodes,
    parse_script_to_df,
)
from .visuals import inject_heartbeat, inject_styles, render_office, render_pipeline, emp_state_key


# ==========================================
# 运行参数快照 + 后台线程启动
# ==========================================
def _snapshot_config(store):
    """把当前会话的运行参数拷贝进 store（供前台/后台执行使用，不依赖 session_state）。
    其中包含用户自定义的「数字员工设定」与「任务协作方式」，确保执行时按用户设定生效。"""
    emp_settings = {
        k: {
            "intro": st.session_state.get(f"emp_intro_{k}", EMPLOYEES[k]["intro"]),
            "skills": st.session_state.get(f"emp_skills_{k}", default_skills_block(k)),
        }
        for k in EMPLOYEES
    }
    task_methods = {
        tid: st.session_state.get(f"task_method_{tid}", TASK_METHODS[tid])
        for tid in TASK_ORDER
    }
    store.snapshot_config(
        st.session_state.total_episodes,
        st.session_state.script_mode,
        st.session_state.seed,
        {k: dict(get_emp_config(k)) for k in EMPLOYEES},
        emp_settings,
        task_methods,
    )


def _start_bg(store, from_progress=False):
    """启动后台流水线线程（自动一键运行 / 自动执行后续剩余任务）。"""
    # 已在运行、或上一个线程尚未完全退出，则不重复启动（避免两个线程并发写入）
    if store.is_running or (store.thread is not None and store.thread.is_alive()):
        return
    _snapshot_config(store)
    store.cancel = False
    with store.lock:
        store.is_running = True
        store.failed_task = None
    t = threading.Thread(
        target=run_pipeline, args=(store,), kwargs={"from_progress": from_progress}, daemon=True
    )
    store.thread = t
    t.start()


def _clear_edit_buffers():
    for tid in TASK_ORDER:
        st.session_state.pop(f"edit_{tid}", None)
    for k in list(st.session_state.keys()):
        if k.startswith("e8_") or k.startswith("refresh_"):
            st.session_state.pop(k, None)


def _setting_widget_keys():
    """「数字员工设定 / 任务协作方式」全部编辑控件的 session_state key。"""
    return (
        [f"emp_intro_{k}" for k in EMPLOYEES]
        + [f"emp_skills_{k}" for k in EMPLOYEES]
        + [f"task_method_{t}" for t in TASK_ORDER]
    )


def _persist_setting_keys():
    """修复「改完设定、点运行后设定回退默认」(bug)：
    某些 rerun（点启动/一键运行/恢复默认 等会在脚本中途 st.rerun()）会在「设定控件尚未渲染」时就中断本次脚本，
    Streamlit 会因此回收这些未渲染控件的 session_state，导致下次 init_state 用默认值重新填充 → 看起来被重置。
    解决：每次运行开头把这些控件值「自我赋值」一次，将其提升为用户态值，从而跨「未渲染的 rerun」存活。
    必须在任何设定控件实例化之前调用（放在 main() 顶部）。"""
    for key in _setting_widget_keys():
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]


def _downstream(tid):
    """返回所有（直接 / 间接）依赖 tid 的下游任务（tid 改动后会失效、需要重跑的任务）。"""
    result = set()
    frontier = [tid]
    while frontier:
        cur = frontier.pop()
        for t, v in TASK_MAP.items():
            if cur in v["deps"] and t not in result:
                result.add(t)
                frontier.append(t)
    return sorted(result)


def _invalidate_downstream(store, tid):
    """当任务 tid 被修改 / 重跑 / 清空后，清除其所有下游任务的产出（连同编辑缓冲），
    这样'自动执行后续剩余任务'会从该任务之后重新生成，不会因为下游仍标记'已完成'而跳过。"""
    ds = _downstream(tid)
    for d in ds:
        store.clear_output(d)
        st.session_state.pop(f"edit_{d}", None)
        if d == 8:
            for k in list(st.session_state.keys()):
                if k.startswith("e8_"):
                    st.session_state.pop(k, None)
    return ds


# ==========================================
# 侧边栏：模型配置 + 项目设置
# ==========================================
def model_config_block(prefix, default_cfg):
    provider = st.selectbox(
        "API 服务商",
        PROVIDERS,
        index=PROVIDERS.index(default_cfg["provider"]) if default_cfg["provider"] in PROVIDERS else 0,
        key=f"{prefix}_provider",
    )
    key = ""
    if provider != "Mock (演示)":
        key = st.text_input("API Key", type="password", key=f"{prefix}_key")
    model = st.selectbox("模型", MODELS[provider], key=f"{prefix}_model")
    return {"provider": provider, "key": key, "model": model}


def render_sidebar(store):
    with st.sidebar:
        st.header("⚙️ 模型配置")
        st.caption("  ")

        st.markdown("**全局默认（应用于所有数字员工）**")
        st.session_state.global_cfg = model_config_block("global", st.session_state.global_cfg)

        st.divider()
        st.session_state.per_emp = st.checkbox("🧩 为每位数字员工单独配置模型", value=st.session_state.per_emp)
        if st.session_state.per_emp:
            st.caption("未单独配置的员工沿用全局默认。")
            for k, e in EMPLOYEES.items():
                with st.expander(f"{e['emoji']} {e['name']}"):
                    st.session_state.emp_cfg[k] = model_config_block(f"emp_{k}", st.session_state.global_cfg)

        st.divider()
        st.markdown("**📂 项目设置**")
        st.session_state.total_episodes = st.number_input(
            "剧本总集数", min_value=1, max_value=100, value=st.session_state.total_episodes, step=1
        )
        mode_label = st.radio(
            "分镜脚本模式",
            ["剧本分镜脚本 (标准短剧)", "解说漫分镜脚本 (小说推文/漫改)"],
            index=0 if st.session_state.script_mode == "standard" else 1,
        )
        st.session_state.script_mode = "standard" if mode_label.startswith("剧本") else "comic"

        st.divider()
        st.markdown("**🧑‍💼 数字员工花名册**")
        st.caption("详细角色介绍 / 工作技能（可编辑）见顶部「设定中心」。")
        for k, e in EMPLOYEES.items():
            cfg = get_emp_config(k)
            state = {"working": "🟣 工作中", "done": "🟢 已完成", "idle": "⚪ 待命"}[emp_state_key(store, k)]
            with st.expander(f"{e['emoji']} {e['name']}　{state}", expanded=False):
                st.caption(e["title"])
                st.markdown(f"**当前模型**：`{cfg['provider']} / {cfg['model']}`")
                mem = store.memory.get(k, [])
                st.markdown(f"**记忆**：已累积 {len(mem)} 条" if mem else "**记忆**：暂无（执行后写入）")

        st.divider()
        if st.button("♻️ 重置工作室（清空所有产出）", use_container_width=True):
            store.reset()  # 同时会通知后台线程停止
            _clear_edit_buffers()
            st.rerun()
        st.caption("（重置仅清空产出与过程；不影响你的自定义设定与「交付文档库」历史）")


# ==========================================
# 产出展示
# ==========================================
def render_result(store, tid, key_suffix=""):
    o = store.outputs
    if tid == 8:
        scripts = o.get(8) or {}
        if not scripts:
            return
        mode_name = "解说漫" if store.script_mode == "comic" else "标准短剧"
        st.caption(f"分镜模式：{mode_name}")
        for label, content in scripts.items():
            st.markdown(f"**📑 分镜 · {label}**")
            if isinstance(content, str) and content.startswith("❌"):
                st.error(content)
                continue
            try:
                df = parse_script_to_df(content)
                if df is not None and len(df) > 0:
                    shot_rows = df[~df["镜号"].astype(str).str.contains("🎬")]
                    st.markdown(
                        f'<span class="qa-ok">✅ 解析成功</span>　共 {len(shot_rows)} 个镜头 · '
                        f'{int(df["镜号"].astype(str).str.contains("🎬").sum())} 个分集标记',
                        unsafe_allow_html=True,
                    )
                    st.dataframe(
                        df,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "镜号": st.column_config.TextColumn("镜号", width="small"),
                            "场景": st.column_config.TextColumn("场景", width="medium"),
                            "画面内容 (Visual)": st.column_config.TextColumn("画面内容 (Visual)", width="large"),
                            "台词/解说 (Dialogue/Commentary)": st.column_config.TextColumn(
                                "台词/解说 (Dialogue/Commentary)", width="large"
                            ),
                        },
                    )
                    csv_out = df.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        f"📥 下载 {label} CSV (Excel 专用)",
                        data=csv_out,
                        file_name=f"storyboard_{label}.csv",
                        mime="text/csv",
                        key=f"dl8_{label}{key_suffix}",
                    )
                else:
                    st.warning("⚠️ 未检测到有效分镜内容，展示原始返回：")
                    st.text(content)
            except Exception as e:
                st.error(f"⚠️ 解析异常: {e}")
                st.text(content)
        return

    content = o.get(tid)
    if not content:
        return
    if isinstance(content, str) and content.startswith("❌"):
        st.error(content)
        return

    if tid in (5, 7):
        n = count_episodes(content)
        target = store.total_episodes
        cls = "qa-ok" if n >= target else "qa-warn"
        msg = (
            f"✅ 集数完整性校验通过：检测到 {n} 集 / 目标 {target} 集"
            if n >= target
            else f"⚠️ 集数校验：检测到 {n} 集 / 目标 {target} 集（可点击重新执行补全）"
        )
        st.markdown(f'<span class="{cls}">{msg}</span>', unsafe_allow_html=True)

    st.markdown(content)
    st.download_button(
        "📥 下载该产出 (TXT)",
        data=content.encode("utf-8"),
        file_name=f"task{tid}_{TASK_MAP[tid]['short']}.txt",
        mime="text/plain",
        key=f"dl_{tid}{key_suffix}",
    )

    if tid == 9:
        st.success("✅ 文档助理已完成飞书文档粘贴与校对，确认无错误、无遗漏。")
        st.download_button(
            "📥 下载飞书最终交付文档 (Markdown)",
            data=content.encode("utf-8"),
            file_name="飞书_最终剧本交付文档.md",
            mime="text/markdown",
            key=f"dl9_md{key_suffix}",
        )


def status_badge(store, tid):
    if task_done(store, tid):
        return "🟢 已完成"
    if is_ready(store, tid):
        return "🟡 可执行"
    return "⚪ 等待依赖"


def _failure_reason(store):
    """提取最近失败任务的真实报错原因（便于用户判断是 Key / 网络 / endpoint 问题，而非误以为程序没启动）。"""
    ft = store.failed_task
    if ft is None:
        return ""
    v = store.outputs.get(ft)
    if isinstance(v, str) and v.startswith("❌"):
        return v
    if ft == 8 and isinstance(v, dict):
        for val in v.values():
            if isinstance(val, str) and val.startswith("❌"):
                return val
    for line in reversed(store.log or []):
        if "❌" in line and "API" in line:
            return line
    return ""


def render_run_banner(store):
    """运行中横幅（简短）+ 停止按钮 + 失败提示。放在中栏「运营方式」上方，始终可见（更新2）。"""
    if store.is_running:
        cur = store.running_task
        cur_txt = f"任务{cur}「{TASK_MAP[cur]['title']}」" if cur else "准备中"
        sc = st.columns([3, 1])
        sc[0].info(f"⏳ 后台正在执行 · 当前：{cur_txt}　")
        if sc[1].button("⏹ 停止自动执行", key="stop_run", type="secondary",
                        help="立即停止后台自动执行并解锁手动编辑（已生成的产出会保留）"):
            store.force_stop()
            st.rerun()
    if store.failed_task is not None:
        st.error(
            f"⛔ 任务{store.failed_task} 失败（已重试 3 次）。"
            + (f"\n\n**失败原因**：{_failure_reason(store)}" if _failure_reason(store) else "")
            + "\n\n请依次排查：① 该数字员工的「模型配置」Provider / API Key 是否填写正确；"
            "② 网络是否能访问所选模型服务（内网 / Azure 等私有 endpoint 在公网会连接超时，并非程序本身问题）；"
            "③ 可先切换为「Mock (演示)」验证整条流程是否正常。"
            "修复后再次点击生成，或切到「手动逐步启动」模式逐个任务执行。"
        )


def render_run_log(store):
    """持久化的运行过程日志（刷新/重连都不丢失）。放在右栏。"""
    if store.log:
        with st.expander("🛰️ 运行过程日志（实时 · 刷新/重连不丢失）", expanded=store.is_running):
            st.code("\n".join(store.log[-200:]), language=None)


# ==========================================
# 自动模式
# ==========================================
def render_auto_mode(store):
    st.markdown("### 🤖 自动按序执行")
    st.info(
        "点击下方按钮，5 位数字员工将依次自动完成任务 1 → 9，全程实时可视化，最终自动产出飞书交付文档。"
        "中途如遇问题，可切换至② 手动逐步启动继续任务。"
    )
    auto_cols = st.columns([1, 3])
    start_auto = auto_cols[0].button(
        "🚀 一键运行全流程", type="primary", use_container_width=True, disabled=store.is_running
    )
    auto_cols[1].caption(f"当前总集数：{st.session_state.total_episodes} 集 · 大纲与分镜均自动分批生成。")

    if start_auto and not store.is_running:
        _start_bg(store, from_progress=False)
        st.rerun()

    st.caption("▶ 各任务产出会在右侧「产出速览」面板即时显示；完整剧本飞书文档见顶部「📚 交付文档库」。")


# ==========================================
# 手动模式
# ==========================================
def render_manual_mode(store):
    st.markdown("### 🙋 手动逐步启动")
    st.info(
        "按顺序逐个点击任务按钮启动对应数字员工；每个任务产出后可【修改】再进入下一步，"
        "也可【跳过本任务】直接粘贴本地剧本文本后保存，再继续下一步；"
        "也可随时点击下方按钮，让产品【自动跑完后续剩余任务】（后台执行，断连不中断）。"
    )

    running = store.is_running
    o = store.outputs

    # —— 自动执行后续剩余任务（后台从当前进度起跑完）——
    done_cnt = sum(1 for t in TASK_ORDER if task_done(store, t))
    rc = st.columns([1.4, 2.6])
    run_rest = rc[0].button(
        "🚀 自动执行后续剩余任务", type="primary",
        disabled=running or (done_cnt == len(TASK_ORDER)),
        help="从当前已完成的最后一步往后，自动执行后续任务，直到全部完成；不回头补跑前序未完成的任务",
    )
    rc[1].caption(
        f"已完成 {done_cnt}/{len(TASK_ORDER)} 个任务。"
        "点击后将从“当前进度的下一个任务”开始往后自动执行（后台运行，单个任务卡顿时仍可在下方查看前序已完成产出）。"
    )
    if run_rest and not running:
        _clear_edit_buffers()
        _start_bg(store, from_progress=True)
        st.rerun()

    # 预同步「编辑缓冲区」——必须在创建任何编辑框（widget）之前完成
    for tid in TASK_ORDER:
        if tid == 8:
            continue
        ekey = f"edit_{tid}"
        cur = o.get(tid)
        cur = cur if isinstance(cur, str) else ""
        if running:
            # 运行中：强制同步 store 最新产出，使前序已完成任务的结果实时显示（更新2）
            st.session_state[ekey] = cur
        elif st.session_state.pop(f"refresh_{tid}", False):
            st.session_state[ekey] = cur
        elif ekey not in st.session_state:
            st.session_state[ekey] = cur

    for tid in TASK_ORDER:
        t = TASK_MAP[tid]
        e = EMPLOYEES[t["owner"]]
        with st.container(border=True):
            c1, c2 = st.columns([0.72, 0.28])
            c1.markdown(f"**任务 {tid} · {t['title']}**　{e['emoji']} **{e['name']}**")
            c2.markdown(f"<div style='text-align:right'>{status_badge(store, tid)}</div>", unsafe_allow_html=True)
            st.caption(t["desc"])

            ready = is_ready(store, tid)
            if not ready and not running:
                missing = [f"任务{d}" for d in t["deps"] if not task_done(store, d)]
                # 用低调的 caption（而非醒目的黄色 warning），避免被误以为是报错（更新2）
                st.caption(
                    f"🔒 该任务依赖：{'、'.join(missing)}。可先手动执行前序任务，"
                    "或在下方“修改 / 粘贴”里粘贴本地文本保存以跳过；也可直接点上方“自动执行后续剩余任务”自动跑完。"
                )

            if tid == 8:
                _render_manual_task8(store, ready)
            else:
                btn_label = "▶️ 重新执行" if task_done(store, tid) else "▶️ 启动该任务（调用数字员工生成）"
                if st.button(btn_label, key=f"run_{tid}", disabled=(not ready) or running,
                             type="primary" if ready and not task_done(store, tid) else "secondary"):
                    _snapshot_config(store)
                    store.failed_task = None
                    store.running_task = tid
                    if tid == 9:
                        run_task9(store)
                        res = store.outputs.get(9)
                    else:
                        res = run_generic_task(store, tid)
                    store.running_task = None
                    if isinstance(res, str) and res.startswith("❌"):
                        store.failed_task = tid
                    else:
                        _invalidate_downstream(store, tid)  # 重跑后清除下游，便于后续重新生成
                    st.session_state[f"refresh_{tid}"] = True
                    st.rerun()

                # 查看 / 修改 / 粘贴 本任务产出
                with st.expander("📄 查看 / ✏️ 修改 / 📋 粘贴本任务产出", expanded=task_done(store, tid)):
                    if tid in (5, 7) and task_done(store, tid):
                        n = count_episodes(o.get(tid))
                        target = store.total_episodes
                        cls = "qa-ok" if n >= target else "qa-warn"
                        st.markdown(
                            f'<span class="{cls}">集数校验：检测到 {n} 集 / 目标 {target} 集</span>',
                            unsafe_allow_html=True,
                        )
                    st.text_area(
                        "本任务产出（可直接修改 AI 产出；也可粘贴本地剧本文本后“保存”以跳过本任务）：",
                        key=f"edit_{tid}", height=260,
                    )
                    if _downstream(tid):
                        st.caption(
                            "提示：保存修改 / 清空 / 重新执行 本任务后，会自动清除其后续任务的产出，"
                            "便于点上方“自动执行后续剩余任务”从这里往后重新生成。"
                        )
                    _done = task_done(store, tid) and isinstance(o.get(tid), str)
                    # 「保存 / 清空 / 下载该产出」三个控件同一行、紧凑排列（末列留白吸收多余宽度，
                    # 各列宽度保证标签单行不折行）。任务 9 的「飞书 Markdown」按钮标签较长，单独占一行。
                    if _done:
                        cols = st.columns([2.8, 1.4, 2.6, 1.8], gap="small")
                    else:
                        cols = st.columns([2.8, 1.4, 4.0], gap="small")
                    if cols[0].button("💾 保存为本任务产出", key=f"save_{tid}", disabled=running,
                                      use_container_width=True):
                        store.set_output(tid, st.session_state[f"edit_{tid}"])
                        _invalidate_downstream(store, tid)
                        st.rerun()
                    if cols[1].button("🧹 清空", key=f"clear_{tid}", disabled=running,
                                      use_container_width=True):
                        store.clear_output(tid)
                        _invalidate_downstream(store, tid)
                        st.session_state[f"refresh_{tid}"] = True
                        st.rerun()
                    if _done:
                        cols[2].download_button(
                            "📥 下载该产出 (TXT)", data=o[tid].encode("utf-8"),
                            file_name=f"task{tid}_{t['short']}.txt", mime="text/plain",
                            key=f"dlm_{tid}", use_container_width=True,
                        )
                        if tid == 9:
                            st.download_button(
                                "📥 下载飞书最终交付文档 (Markdown)", data=o[9].encode("utf-8"),
                                file_name="飞书_最终剧本交付文档.md", mime="text/markdown",
                                key="dlm9_md", use_container_width=False,
                            )

                if tid == 9 and task_done(store, tid):
                    with st.expander("📑 飞书文档渲染预览（含分镜表格）", expanded=False):
                        st.markdown(o[9])


def _render_manual_task8(store, ready):
    """手动模式下任务 8（分镜脚本）的控制：分批生成 / 查看表格 / 修改批次 / 粘贴本地分镜跳过。"""
    o = store.outputs
    scripts = o.get(8) or {}
    running = store.is_running

    for label in list(scripts.keys()):
        ek = f"e8_{label}"
        cur = scripts.get(label)
        cur = cur if isinstance(cur, str) else ""
        if running:
            st.session_state[ek] = cur  # 运行中实时同步分镜批次产出（更新2）
        elif st.session_state.pop(f"refresh_e8_{label}", False):
            st.session_state[ek] = cur
        elif ek not in st.session_state:
            st.session_state[ek] = cur

    if ready:
        batches = get_batches(st.session_state.total_episodes)
        mode_name = "解说漫" if st.session_state.script_mode == "comic" else "标准短剧"
        st.caption(f"分镜模式：{mode_name}（可在侧边栏切换）。建议每次 10 集，可逐批生成。")
        bcols = st.columns(min(5, len(batches)) or 1)
        for i, (a, b) in enumerate(batches):
            label = f"{a}-{b}集"
            done_mark = "✅" if label in scripts else ""
            if bcols[i % len(bcols)].button(f"生成 {label} {done_mark}", key=f"b8_{label}", disabled=running):
                _snapshot_config(store)
                store.failed_task = None
                store.running_task = 8
                res = run_task8_batch(store, a, b)
                store.running_task = None
                if isinstance(res, str) and res.startswith("❌"):
                    store.failed_task = 8
                else:
                    _invalidate_downstream(store, 8)  # 分镜变动 → 失效任务9（飞书文档）
                st.session_state[f"refresh_e8_{label}"] = True
                st.rerun()
        if st.button("⚡ 一键生成全部批次分镜", key="b8_all", disabled=running):
            _snapshot_config(store)
            store.failed_task = None
            store.running_task = 8
            for (a, b) in batches:
                res = run_task8_batch(store, a, b)
                st.session_state[f"refresh_e8_{a}-{b}集"] = True
                if isinstance(res, str) and res.startswith("❌"):
                    store.failed_task = 8
                    break
            store.running_task = None
            _invalidate_downstream(store, 8)
            st.rerun()
    else:
        st.caption("（前置任务未完成；可在下方直接粘贴本地分镜以跳过生成）")

    # 清空全部分镜（修复任务8完成后无法清空）
    if scripts and st.button("🧹 清空全部分镜（任务8）", key="b8_clear", disabled=running):
        store.clear_output(8)
        for k in list(st.session_state.keys()):
            if k.startswith("e8_"):
                st.session_state.pop(k, None)
        _invalidate_downstream(store, 8)  # 连同清除任务9
        st.rerun()

    if scripts:
        with st.expander("📄 查看分镜表格", expanded=True):
            render_result(store, 8)

    if scripts:
        with st.expander("✏️ 修改已生成的分镜批次（CSV）", expanded=False):
            for label in list(scripts.keys()):
                st.markdown(f"**批次 {label}**")
                st.text_area(f"编辑 {label} 的 CSV：", key=f"e8_{label}", height=200)
                if st.button(f"💾 保存批次 {label}", key=f"save8_{label}", disabled=running):
                    store.set_batch(label, st.session_state[f"e8_{label}"])
                    _invalidate_downstream(store, 8)
                    st.rerun()

    with st.expander("📋 跳过生成 · 直接粘贴本地分镜（新建批次）", expanded=not scripts):
        st.caption(
            "适用于直接使用本地已有分镜：建议保持 “镜号,场景,画面内容 (Visual),台词 (Dialogue) & 音效 (SFX)” "
            "的 CSV 表头格式，并以“第 X 集”单独成行。"
        )
        st.text_input("批次名称：", value="本地粘贴", key="paste8_label")
        st.text_area(
            "粘贴本地分镜 CSV：", key="paste8_text", height=200,
            placeholder="第 1 集\n镜号,场景,画面内容 (Visual),台词 (Dialogue) & 音效 (SFX)\n1,场景,画面内容,Role: line",
        )
        if st.button("📌 保存为分镜批次", key="paste8_save", disabled=running):
            txt = (st.session_state.get("paste8_text") or "").strip()
            lbl = ((st.session_state.get("paste8_label") or "").strip()) or "本地粘贴"
            if txt:
                store.set_batch(lbl, txt)
                _invalidate_downstream(store, 8)
                st.rerun()
            else:
                st.warning("请先粘贴分镜内容再保存。")


# ==========================================
# 数字员工档案：角色 / Agent 技能 / 记忆
# ==========================================
def render_profiles(store):
    st.divider()
    st.markdown("### 🧑‍💼 数字员工档案（默认设定）")
    emp_cols = st.columns(5)
    for i, (k, e) in enumerate(EMPLOYEES.items()):
        with emp_cols[i]:
            with st.expander(f"{e['emoji']} {e['name']}", expanded=False):
                cfg = get_emp_config(k)
                st.markdown(f"**{e['title']}**")
                # 展示「当前生效」的角色介绍（用户若已自定义则同步显示自定义内容）
                st.caption(st.session_state.get(f"emp_intro_{k}", e["intro"]))
                st.markdown("**🛠️ Agent 技能**")
                for s in AGENT_SKILLS[k]:
                    st.markdown(f"- {s}")
                st.markdown("**🔧 配套外部工具**")
                st.caption(e["tool"])
                st.markdown(f"**🤖 当前模型**：`{cfg['provider']} / {cfg['model']}`")
                st.markdown("**🧠 记忆（本次协作累积）**")
                mem = store.memory.get(k, [])
                if mem:
                    for m in mem:
                        st.markdown(f"- {m}")
                else:
                    st.caption("（暂无记忆，执行任务后自动写入）")


# ==========================================
# 设定中心：数字员工设定（角色介绍 / 工作技能）+ 各任务协作方式（均可查看 / 编辑）
# 默认展示产品内置（PRD）设定；用户可改写或输入自己的设定，执行时实际生效。
# ==========================================
def _render_emp_settings(store):
    running = store.is_running
    if running:
        st.caption("⏳ 运行中暂不可编辑（避免影响进行中的任务）；本次运行结束后即可修改，下次执行生效。")
    for k, e in EMPLOYEES.items():
        st.markdown(f"**{e['emoji']} {e['name']}** · {e['title']}")
        # 「恢复默认」按钮放在编辑框之前：点击后先写回默认值再 rerun，避免“控件实例化后再改值”报错
        if st.button("↩︎ 恢复默认设定", key=f"reset_emp_{k}", disabled=running):
            st.session_state[f"emp_intro_{k}"] = e["intro"]
            st.session_state[f"emp_skills_{k}"] = default_skills_block(k)
            st.rerun()
        st.text_area("角色介绍：", key=f"emp_intro_{k}", height=150, disabled=running)
        st.text_area(
            "工作技能（每行一条，可自由增删 / 改写）：",
            key=f"emp_skills_{k}", height=200, disabled=running,
        )
        st.divider()


def _render_task_methods(store):
    running = store.is_running
    if running:
        st.caption("⏳ 运行中暂不可编辑；本次运行结束后即可修改，下次执行生效。")
    for tid in TASK_ORDER:
        t = TASK_MAP[tid]
        e = EMPLOYEES[t["owner"]]
        st.markdown(f"**任务 {tid} · {t['title']}**　{e['emoji']} {e['name']}")
        st.caption(t["desc"])
        if tid == 9:
            st.caption("提示：任务9为文档拼装（不调用模型），此处协作方式仅作说明展示。")
        if st.button("↩︎ 恢复默认", key=f"reset_method_{tid}", disabled=running):
            st.session_state[f"task_method_{tid}"] = TASK_METHODS[tid]
            st.rerun()
        st.text_area(
            "协作方式 / 工作方法：", key=f"task_method_{tid}", height=180, disabled=running,
        )
        st.divider()


def render_settings(store):
    st.divider()
    st.markdown("### 🛠️ 数字员工 & 协作方式设定（可查看 / 可编辑）")
    st.caption(
        "默认展示产品内置设定；你可以直接修改或输入自己的设定。修改后将作为「数字员工的角色与技能」"
        "以及「每个任务的协作方式」在执行时的实际依据（留空则回退到内置默认）。"
    )
    with st.expander("🧑‍💼 数字员工设定：角色介绍 / 工作技能（可编辑）", expanded=False):
        _render_emp_settings(store)
    with st.expander("📋 各任务协作方式 / 工作方法（可查看 / 可编辑）", expanded=False):
        _render_task_methods(store)


# ==========================================
# 右栏：产出速览（已完成任务结果，即时查看）
# ==========================================
def render_results_panel(store):
    st.markdown('<div class="panel-title">📤 产出速览 · 已完成任务</div>', unsafe_allow_html=True)
    done_cnt = sum(1 for tid in TASK_ORDER if task_done(store, tid))
    st.caption(
        f"已完成 {done_cnt}/{len(TASK_ORDER)} 个任务 · 任务 1 → 9 从上到下排列；"
        "点击任意任务可展开查看产出详情（默认折叠，只显示执行进度）。"
    )
    # 9 个任务栏从上到下（任务1在最上），默认全部折叠；用户自行点开查看详情
    for tid in TASK_ORDER:
        t = TASK_MAP[tid]
        e = EMPLOYEES[t["owner"]]
        if store.running_task == tid:
            badge = "🟣 执行中"
        elif task_done(store, tid):
            badge = "🟢 已完成"
        elif is_ready(store, tid):
            badge = "🟡 可执行"
        else:
            badge = "⚪ 等待依赖"
        with st.expander(f"任务 {tid} · {t['short']}　{e['emoji']} {e['name']}　{badge}", expanded=False):
            if task_done(store, tid):
                render_result(store, tid, key_suffix="_rp")
            elif store.running_task == tid:
                st.info("⏳ 正在执行…完成后这里会显示该任务产出。")
            else:
                st.caption("尚未执行。")


# ==========================================
# 顶部分栏：交付文档库（当前 + 历史 任务9最终飞书剧本文档）
# ==========================================
def render_doc_library(store):
    st.markdown(
        '<div class="panel-title">📚 交付文档库 · 任务9 最终飞书剧本文档（当前 + 历史）</div>',
        unsafe_allow_html=True,
    )
    st.caption("每次「任务9 · 飞书归档」完成后，最终交付文档会自动归档到这里，可随时查看 / 下载当前与过去的成片剧本。")
    hist = list(store.doc_history)
    if not hist:
        st.info("暂无交付文档。完成「任务9 · 飞书归档」后，最终剧本飞书文档会自动出现在这里。")
        return
    top = st.columns([1, 3])
    if top[0].button("🗑 清空文档库", key="clear_doclib", disabled=store.is_running):
        store.clear_doc_history()
        st.rerun()
    top[1].caption(f"共 {len(hist)} 份历史交付文档（最新在最上）。")
    for i, doc in enumerate(reversed(hist)):
        idx = len(hist) - i
        with st.expander(f"📕 {doc['title']}　·　🕒 {doc['time']}", expanded=(i == 0)):
            st.download_button(
                "📥 下载该飞书文档 (Markdown)",
                data=doc["content"].encode("utf-8"),
                file_name=f"飞书剧本_{doc['title']}.md",
                mime="text/markdown",
                key=f"dllib_{idx}",
            )
            st.markdown(doc["content"])


# ==========================================
# 工作台：中栏（办公室 + 运营方式 + 产出速览）｜ 右栏（流水线 + 状态 + 日志，可折叠）
# ==========================================
def _render_wb_center(store):
    # 1) 数字员工办公室 · 实时工作状态（含动图）
    st.markdown('<div class="panel-title">🏢 数字员工办公室</div>', unsafe_allow_html=True)
    render_office(store)
    st.divider()
    # 2) 运行中横幅 + 停止按钮 + 失败提示：紧靠「运营方式」上方（更新2）
    render_run_banner(store)
    # 3) 运营方式（字体放大、醒目卡片）
    with st.container(border=True):
        st.markdown('<div class="mode-title">🕹️ 运营方式 · 选择执行方式</div>', unsafe_allow_html=True)
        run_mode = st.radio(
            "运营方式",
            ["① 自动按序执行（一键运行全流程）", "② 手动逐步启动（逐个任务点击执行）"],
            horizontal=True, key="run_mode_radio", label_visibility="collapsed",
        )
    # 3) 创作方向：紧挨着放在「任务 1」上方（更新3）
    with st.expander("✏️ （可选）为「短剧爆款研究员」提供创作方向 / 赛道（留空则由其自主选择）", expanded=True):
        st.session_state.seed = st.text_area(
            "创作方向 / 赛道参考：",
            value=st.session_state.seed,
            height=80,
            placeholder="例如：赛博朋克 + 底层逆袭；或：女频 狼人 虐恋……（留空则研究员自主决策）",
        )
    # 4) 执行控件（任务区）
    if run_mode.startswith("①"):
        render_auto_mode(store)
    else:
        render_manual_mode(store)
    # 5) 产出速览（任务 1→9 从上到下，默认折叠）
    st.divider()
    render_results_panel(store)


def _render_wb_right(store):
    render_pipeline(store)
    st.divider()
    render_run_log(store)


# ==========================================
# 后台运行进度刷新驱动（修复「运行时整块重复+闪烁」「运行时右栏无法完全折叠」）
# ==========================================
@st.fragment(run_every=1.5)
def _auto_refresh_tick():
    """后台流水线运行时，用 fragment 定时器每 1.5s 触发一次「完整 app 重跑」来刷新进度。

    为什么不再用 time.sleep(1.5)+st.rerun() 结束脚本（原实现）：那样每次运行都以 RERUN 结束，
    Streamlit 据此判断「马上会重跑、无需清理过期元素」。但任务推进 / 折叠右栏会改变页面结构，
    旧帧里位于「已不再使用的位置」上的元素不会被清理 → 整块残留，表现为「数字员工办公室 /
    产出速览」重复堆叠并闪烁，且运行中右栏无法真正折叠。改由 fragment 定时器驱动后，main 每次都能
    正常结束（FINISHED_SUCCESSFULLY），Streamlit 会清理过期元素，从根本上消除重复与闪烁。

    _tick_armed 标志用于跳过 fragment 在「本次 app 重跑」内的首次同步执行（否则会立刻再次触发
    app 重跑形成紧循环）；只有「1.5s 定时器触发」的那次执行才升级为一次完整 app 重跑。"""
    if st.session_state.get("_tick_armed"):
        st.session_state["_tick_armed"] = False
        return
    st.rerun(scope="app")


# ==========================================
# 主入口
# ==========================================
def main():
    # 注意：st.set_page_config 必须由入口脚本在调用 main() 前完成。
    inject_heartbeat()
    inject_styles()
    store = get_store()

    # 自愈：若标记为运行中、但后台线程已结束/不存在，则立即解锁——
    # 防止后台线程异常退出后 is_running 卡在 True，导致手动模式所有按钮被永久禁用、页面不停自刷新。
    if store.is_running and not (store.thread is not None and store.thread.is_alive()):
        with store.lock:
            store.is_running = False
            store.running_task = None

    # 后台运行「刚刚结束」的瞬间：清空手动编辑缓冲，使各任务编辑框用最新产出重新初始化。
    # 修复：自动执行完毕后，任务9（及最后完成的任务）编辑框不自动显示内容、需手动点"重新执行"才出现。
    if st.session_state.get("_prev_running", False) and not store.is_running:
        _clear_edit_buffers()
    st.session_state["_prev_running"] = store.is_running

    init_state()
    # 防止「设定控件」在中途 st.rerun() 的运行里被回收而回退默认（须在设定控件渲染前执行）
    _persist_setting_keys()
    render_sidebar(store)

    st.markdown('<div class="studio-title">🎬 AI Agent 短剧剧本工作室</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="studio-sub">5 位数字员工分工协作，全流程可视化产出 1 部极致优质的海外短剧剧本。'
        "　左栏配置 · 中栏实时执行 · 右栏产出速览 · 顶部切换设定中心与交付文档库。</div>",
        unsafe_allow_html=True,
    )

    # ===== 顶部分栏（Tabs）：工作台 / 设定中心 / 交付文档库 =====
    tab_studio, tab_settings, tab_docs = st.tabs([
        "🎬 工作台",
        "📋 设定中心 · 协作方式 & 员工档案",
        f"📚 交付文档库（{len(store.doc_history)}）",
    ])

    with tab_studio:
        # 中栏（办公室 + 运营方式 + 产出速览）｜ 右栏（流水线 + 日志）
        # 右栏宽度固定 = 左侧边栏（21rem，由 CSS :has(#wb-right-marker) 锁定）；可完全折叠隐藏。
        right_open = st.session_state.setdefault("right_open", True)
        if right_open:
            cC, cR = st.columns([2.1, 1], gap="large")
            with cC:
                st.markdown('<div id="wb-center-marker"></div>', unsafe_allow_html=True)
                _render_wb_center(store)
            with cR:
                st.markdown('<div id="wb-right-marker"></div>', unsafe_allow_html=True)
                hc = st.columns([5, 1])
                hc[0].markdown('<div class="panel-title">🔗 流水线 & 运行日志</div>', unsafe_allow_html=True)
                if hc[1].button("⏴", key="collapse_right", help="收起右栏（完全隐藏）"):
                    st.session_state.right_open = False
                    st.rerun()
                _render_wb_right(store)
        else:
            # 右栏完全折叠隐藏（与左侧边栏「收起」一致）：右栏内容整体不渲染、正文占满整行，
            # 仅在右上角保留一个小巧的「展开」标签用于重新打开（不再占用一整条醒目的按钮栏）。
            tr = st.columns([12, 2])
            if tr[1].button("🔗 展开 ⏵", key="expand_right", use_container_width=True,
                            help="展开右栏（协作流水线 & 运行日志）"):
                st.session_state.right_open = True
                st.rerun()
            _render_wb_center(store)

    with tab_settings:
        render_settings(store)
        render_profiles(store)

    with tab_docs:
        render_doc_library(store)

    st.divider()
    st.caption("© AI Agent 短剧剧本工作室 - zhangzhou.01@bytedance.com ")

    # 后台运行时：用「fragment 定时器」周期性触发一次「完整 app 重跑」刷新进度（详见 _auto_refresh_tick）。
    # 不再用 time.sleep()+st.rerun() 结束脚本，从而让每次 app 重跑都正常结束、清理过期元素，
    # 根除「整块重复+闪烁」与「运行中右栏无法折叠」。
    if store.is_running:
        st.session_state["_tick_armed"] = True
        _auto_refresh_tick()
