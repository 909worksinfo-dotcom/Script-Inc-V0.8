# -*- coding: utf-8 -*-
"""任务编排执行、飞书文档编排、分镜 CSV 解析、集数校验。

重要：本模块所有执行函数都只读写 RunStore（store），不访问 st.* / st.session_state，
因此可以在「后台线程」里安全运行（浏览器断连也不会中断），见 run_pipeline。
"""

import re
import csv
import time

import pandas as pd

from .prompts import Prompts
from .tasks import (
    TASK_MAP,
    TASK_ORDER,
    MEM_NOTE,
    get_task_method,
    build_instruction,
)
from .employees import (
    EMPLOYEES,
    persona_from_store,
)
from .store import make_service, task_done, is_ready, get_batches

# 每个任务最多尝试 3 次（1 次 + 重试 3 次实际为 4 次调用上限，这里按"重试 3 次"语义）
TASK_MAX_RETRIES = 3


def count_episodes(text):
    """统计分集大纲的「主线集数」，纠正过去把序章/番外/ACT区间/正文内嵌引用误计的问题。

    做法：
    1) 只匹配「行首的分集标题」，且中文必须形如「第 N 集」（数字后带“集”），英文形如「EPISODE N」，
       从而排除“第41-50集”这类区间、正文里的数字引用等假阳性；
    2) 取「从 1 开始的最长连续编号」作为主线集数 —— 即使模型多写了第0集/番外/跳号，也不会被算进去。
    """
    t = text or ""
    nums = set()
    # 中文集标题：行首（可带 # / * / > / - / 空格 / 制表符）+ 第 + 数字 + 集
    nums |= {int(n) for n in re.findall(r"(?im)^[ \t>#*\-]*第\s*(\d+)\s*集", t)}
    # 英文集标题：行首 EPISODE/Episode + 数字（数字后非数字，避免 500 之类）
    nums |= {int(n) for n in re.findall(r"(?im)^[ \t>#*\-]*Episode\s*(\d+)(?!\d)", t)}
    if not nums:
        # 兜底：宽松匹配（无“集”的「第N」或行首「EPISODE N」）
        nums = {int(n) for n in re.findall(r"(?im)^[ \t>#*\-]*(?:Episode|第)\s*(\d+)", t)}
    if not nums:
        return 0
    # 主线集数 = 从 1 开始的最长连续序列长度（忽略 第0集 / 番外 / 杂项编号）
    k = 0
    while (k + 1) in nums:
        k += 1
    return k if k > 0 else len(nums)


def build_io(store, tid):
    """为任务 1-7 构建 (system, user, mock_key)。任务 8/9 单独处理。

    系统提示（数字员工设定）与工作方法（任务协作方式）均在此「执行时」从 store 动态读取，
    因此用户在产品里自定义的设定会真正作用于生成。
    """
    o = store.outputs
    ep = store.total_episodes
    method = lambda i: get_task_method(i, store.task_methods)
    if tid == 1:
        seed = (store.seed or "").strip()
        seed_block = (
            f"\n【创作方向 / 赛道参考（用户提供）】\n{seed}\n"
            if seed
            else "\n（用户未指定方向，请你自主选择当前最具爆款潜力的赛道）\n"
        )
        return (
            persona_from_store(store, "researcher"),
            build_instruction(1, method(1)) + seed_block,
            "researcher_idea",
        )
    if tid == 2:
        system = persona_from_store(store, "creative") + "\n\n" + Prompts.ACT_GEN_SYSTEM
        user = (
            "【任务2：生成三幕式创意】根据任务1的原始创意写出一个三幕式创意。\n\n【工作方法】\n"
            + method(2)
            + "\n\n"
            + Prompts.ACT_GEN_TASK
            + f"\n[原始创意]\n{o[1]}"
        )
        return system, user, "three_act_v1"
    if tid == 3:
        return (
            persona_from_store(store, "reviewer"),
            build_instruction(3, method(3)) + f"\n\n[待审核 · 三幕式创意]\n{o[2]}",
            "review_3act",
        )
    if tid == 4:
        system = persona_from_store(store, "creative") + "\n\n" + Prompts.ACT_GEN_SYSTEM
        user = build_instruction(4, method(4)) + f"\n\n[原始三幕式创意]\n{o[2]}\n\n[审核员修改建议]\n{o[3]}"
        return system, user, "three_act_final"
    if tid == 5:
        system = persona_from_store(store, "writer") + "\n\n" + Prompts.OUTLINE_SYSTEM
        user = (
            "【任务5：生成分集大纲】根据任务4修改后的三幕式创意，调用分集大纲生成工具生成大纲。\n\n【工作方法】\n"
            + method(5)
            + "\n\n"
            + Prompts.OUTLINE_TASK.format(total_episodes=ep)
            + f"\n[三幕式创意]\n{o[4]}"
        )
        return system, user, f"outline:{ep}"
    if tid == 6:
        user = (
            build_instruction(6, method(6))
            + f"\n\n[原始创意]\n{o[1]}\n\n[三幕式创意 · 最终版]\n{o[4]}\n\n[待审核 · {ep} 集分集大纲]\n{o[5]}"
        )
        return persona_from_store(store, "reviewer"), user, "review_outline"
    if tid == 7:
        user = (
            build_instruction(7, method(7), total_episodes=ep)
            + f"\n\n[三幕式创意]\n{o[4]}\n\n[原始 {ep} 集大纲]\n{o[5]}\n\n[审核员修改建议]\n{o[6]}"
        )
        return persona_from_store(store, "writer"), user, f"outline_final:{ep}"
    raise ValueError(f"build_io 不支持任务 {tid}")


def _is_transient_error(msg):
    """判断报错是否为临时性错误（值得重试）。
    临时性：超时 / 429 限流 / 5xx 服务端错误 / 网络连接问题。
    永久性（鉴权 401/403、参数 400、余额/配额不足、内容拦截等）一律返回 False，不重试。
    """
    t = (msg or "").lower()
    # HTTP 状态码用单词边界匹配，避免把 50000、60000 之类数字误判为 500
    if re.search(r"\b(429|500|502|503|504)\b", t):
        return True
    transient_phrases = (
        "rate limit", "rate_limit", "ratelimit", "too many requests",
        "timeout", "timed out", "read timed out",
        "overload", "overloaded", "temporar", "try again", "again later",
        "connection", "network", "econnreset",
        "service unavailable", "internal server error", "bad gateway", "gateway timeout",
    )
    return any(p in t for p in transient_phrases)


def _generate_with_retry(svc, system_prompt, user_prompt, mock_key=None, max_retries=TASK_MAX_RETRIES):
    """带有限重试的生成。
    - 仅对临时性错误重试，最多 max_retries 次，两次重试之间退避等待 2s→4s→8s…；
    - 永久性错误或重试用尽，返回 ❌ 错误串（不再重试）；
    - Mock 或配置类错误（如未填 Key，这些是普通返回而非异常）不会触发重试。
    """
    attempts = max_retries + 1
    for attempt in range(attempts):
        try:
            return svc.generate(system_prompt, user_prompt, mock_key=mock_key, raise_on_error=True)
        except Exception as e:
            err = str(e)
            if _is_transient_error(err) and attempt < attempts - 1:
                time.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s, ...
                continue
            return f"❌ API 调用异常（已重试 {attempt} 次）: {err}"


def run_generic_task(store, tid, max_retries=TASK_MAX_RETRIES):
    system, user, mkey = build_io(store, tid)
    svc = make_service(store, TASK_MAP[tid]["owner"])
    res = _generate_with_retry(svc, system, user, mock_key=mkey, max_retries=max_retries)
    store.set_output(tid, res)
    if not (isinstance(res, str) and res.startswith("❌")):
        store.add_memory(TASK_MAP[tid]["owner"], MEM_NOTE[tid])
    return res


def run_task8_batch(store, start, end, max_retries=TASK_MAX_RETRIES):
    mode = store.script_mode
    if mode == "comic":
        base, mtag = Prompts.COMIC_SCRIPT_TASK_TEMPLATE, "comic"
    else:
        base, mtag = Prompts.SCRIPT_TASK_TEMPLATE, "standard"
    user = (
        "【任务8：生成分镜脚本表格】根据任务7修改后的分集大纲，逐批生成分镜脚本并严格审核。\n\n【工作方法】\n"
        + get_task_method(8, store.task_methods)
        + "\n\n"
        + base.format(episode_range=f"{start}-{end}")
        + f"\n[大纲]\n{store.outputs[7]}"
    )
    svc = make_service(store, "reviewer")
    res = _generate_with_retry(
        svc, Prompts.SCRIPT_SYSTEM, user,
        mock_key=f"script:{mtag}:{start}-{end}", max_retries=max_retries,
    )
    store.set_batch(f"{start}-{end}集", res)
    if not (isinstance(res, str) and res.startswith("❌")):
        store.add_memory("reviewer", MEM_NOTE[8])
    return res


def _df_to_markdown(df):
    """把分镜 DataFrame 转成飞书可识别的 Markdown 表格（与任务8表格列一致）。"""
    cols = list(df.columns)
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            cell = str(row[c]) if row[c] is not None else ""
            cell = cell.replace("\\", "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()
            cells.append(cell)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def compile_feishu_doc(store):
    o = store.outputs
    ep = store.total_episodes
    parts = [
        "# 📕 短剧剧本工作室 · 最终交付文档（飞书格式）",
        f"> 由 5 位 AI Agent 数字员工协作产出 · 共 {ep} 集 · 文档助理已逐项校对，核对无误。",
        "",
        "## 一、三幕式创意（任务 4 · 最终版）",
        o.get(4) or "（缺失）",
        "",
        f"## 二、{ep} 集分集大纲（任务 7 · 优化版）",
        o.get(7) or "（缺失）",
        "",
        "## 三、分镜脚本表格（任务 8）",
    ]
    scripts = o.get(8) or {}
    if scripts:
        for label, content in scripts.items():
            parts.append(f"\n### 分镜 · {label}\n")
            df = None
            try:
                df = parse_script_to_df(content or "")
            except Exception:
                df = None
            if df is not None and len(df) > 0:
                parts.append(_df_to_markdown(df))
            else:
                parts.append("```")
                parts.append((content or "").strip())
                parts.append("```")
    else:
        parts.append("（缺失）")
    return "\n".join(parts)


def _extract_drama_title(store):
    """从已有产出里提取剧名（用于文档库归档标题）；取不到则用占位名。"""
    for tid in (4, 2, 1):
        s = store.outputs.get(tid)
        if isinstance(s, str):
            m = re.search(r"剧名[:：]\s*([^\n]+)", s)
            if m:
                return m.group(1).strip()[:40]
    return "未命名短剧"


def run_task9(store):
    doc = compile_feishu_doc(store)
    store.set_output(9, doc)
    store.add_memory("assistant", MEM_NOTE[9])
    store.add_doc_history(_extract_drama_title(store), doc)


# ==========================================
# 后台流水线（在线程中运行，只读写 store，不触碰 st.*）
# ==========================================
def _mark_failed(store, tid, res):
    with store.lock:
        store.failed_task = tid
    store.log_line(f"❌ 任务{tid} 失败（已重试 {TASK_MAX_RETRIES} 次）：{res}")
    store.log_line("⛔ 任务失败，再次点击生成或者调整手动模式")


def run_pipeline(store, from_progress=False):
    """后台执行尚未完成的任务，直到全部完成或失败/被取消。仅读写 store。

    from_progress=True（手动“自动执行后续剩余任务”）：仅从「当前已完成的最高任务」的下一个
    任务开始往后执行，不回头补跑前序未完成任务。
    """
    try:
        with store.lock:
            store.is_running = True
            store.cancel = False
            store.failed_task = None

        start = 1
        if from_progress:
            done = [t for t in TASK_ORDER if task_done(store, t)]
            start = (max(done) + 1) if done else 1

        ok = True
        for tid in [1, 2, 3, 4, 5, 6, 7]:
            if store.cancel:
                ok = False
                break
            if tid < start or task_done(store, tid):
                continue
            # 不因依赖未满足而中止：从当前进度往后执行所有未完成任务
            # （正常顺序执行时前置自然会先产出；若用户跳过了某前置，则按空内容处理，绝不卡住）
            e = EMPLOYEES[TASK_MAP[tid]["owner"]]
            with store.lock:
                store.running_task = tid
            store.log_line(f"▶️ 任务{tid}「{TASK_MAP[tid]['title']}」开始 · {e['name']}")
            res = run_generic_task(store, tid)
            if store.cancel:
                ok = False
                break
            if isinstance(res, str) and res.startswith("❌"):
                _mark_failed(store, tid, res)
                ok = False
                break
            store.log_line(f"✅ 任务{tid} 完成 · {e['name']}")

        # 任务 8（分批，跳过已生成批次）
        if ok and not store.cancel and 8 >= start and not task_done(store, 8):
            with store.lock:
                store.running_task = 8
            store.log_line("⚖️ 犀利的短剧剧本审核员开始生成分镜脚本（分批）…")
            for (a, b) in get_batches(store.total_episodes):
                if store.cancel:
                    ok = False
                    break
                if f"{a}-{b}集" in store.outputs[8]:
                    continue
                store.log_line(f"🎬 生成 {a}-{b} 集分镜…")
                res = run_task8_batch(store, a, b)
                if store.cancel:
                    ok = False
                    break
                if isinstance(res, str) and res.startswith("❌"):
                    _mark_failed(store, 8, res)
                    ok = False
                    break
            if ok and not store.cancel:
                store.log_line("✅ 任务8 完成 · 全部分镜脚本已生成")

        # 任务 9（编排归档）
        if ok and not store.cancel and 9 >= start and not task_done(store, 9):
            with store.lock:
                store.running_task = 9
            store.log_line("📋 文档助理整理飞书交付文档…")
            run_task9(store)
            if not store.cancel:
                store.log_line("✅ 任务9 完成 · 飞书文档已生成")

        if ok and not store.cancel:
            store.log_line("🎉 全流程执行完毕！可在下方查看每位数字员工的任务产出。")
    except Exception as ex:
        store.log_line(f"❌ 运行异常：{ex}")
    finally:
        with store.lock:
            store.running_task = None
            store.is_running = False


def parse_script_to_df(content):
    """复用原工具的鲁棒 CSV 解析逻辑，返回 DataFrame（解析失败返回 None）。"""
    match = re.search(r"((第\s*\d+\s*集|Episode|镜号).*$)", content, re.DOTALL)
    if not match:
        return None
    csv_text = match.group(1).strip()
    csv_text = re.sub(r"```\w*\n?", "", csv_text).replace("```", "").strip()

    data_rows = []
    reader = csv.reader(csv_text.splitlines())
    for row in reader:
        if not row:
            continue
        row = [str(x).strip() for x in row]
        row_str = "".join(row)

        # 逻辑 A：识别分集标题行
        if (len(row) == 1 or (len(row) < 3 and len(row_str) < 20)) and (
            "集" in row_str or "Episode" in row_str
        ):
            title = row[0].replace(",", "")
            data_rows.append([f"🎬 {title} 🎬", "", "", ""])
            continue

        # 逻辑 B：处理表头
        if "镜号" in row[0]:
            continue

        # 逻辑 C：数据行格式化（智能分离画面与台词）
        processed_row = []
        if len(row) >= 3:
            if len(row) == 3:
                row.append("")
            rest_text = ",".join(row[2:])
            match_dialogue = re.search(
                r'(?:^|[,。！？”\s])\s*([A-Za-z0-9\s\(\)\-]{2,25}:\s*\S)', rest_text
            )
            if match_dialogue:
                idx = match_dialogue.start(1)
                visual_part = rest_text[:idx].strip(' ,"')
                dialogue_part = rest_text[idx:].strip(' ,"')
                processed_row = [row[0], row[1], visual_part, dialogue_part]
            else:
                if len(row) == 4:
                    processed_row = row
                else:
                    processed_row = [row[0], row[1], ",".join(row[2:-1]), row[-1]]
        elif len(row) < 3:
            row.extend([""] * (4 - len(row)))
            processed_row = row

        # 逻辑 E：清洗景别关键词
        if processed_row and len(processed_row) == 4:
            clean_visual = re.sub(r"【.*?】|\[.*?\]", "", processed_row[2]).strip()
            processed_row[2] = clean_visual

        # 逻辑 D：隐式分集检测
        if processed_row and processed_row[0] == "1" and len(data_rows) > 0:
            if "🎬" not in data_rows[-1][0]:
                data_rows.append(["🎬 下一集 / Next Episode 🎬", "", "", ""])

        if processed_row:
            data_rows.append(processed_row)

    header_list = ["镜号", "场景", "画面内容 (Visual)", "台词/解说 (Dialogue/Commentary)"]
    if len(data_rows) > 0:
        return pd.DataFrame(data_rows, columns=header_list)
    return pd.DataFrame(columns=header_list)
