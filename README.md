# 🎬 AI Agent 短剧剧本工作室 (Script Studio)

一个由 **5 位 AI Agent 数字员工**组成的可视化短剧剧本工作室。它们分工协作、依次接力，调用《三幕式创意和剧本生成》工具的 Prompt 与 LLM 服务，从一句原始创意出发，**全流程可视化**地产出一部高质量的海外短剧剧本（三幕式创意 → 50 集分集大纲 → 分镜脚本表格 → 飞书交付文档）。

> 默认使用「Mock (演示)」模式，**无需任何 API Key 即可离线走通全流程与可视化**；需要真实生成时，在侧边栏选择真实服务商并填入 API Key。

---

## ✨ 核心特性

- **5 位数字员工**：短剧爆款研究员🛰️、短剧创意天才💡、经验丰富的短剧编剧🎬、犀利的短剧剧本审核员⚖️、文档助理📋 —— 角色介绍与工作技能均严格采用产品需求文档原文，并配备 Agent 技能与记忆。
- **9 大协作任务**：原始创意 → 三幕式创意 → 创意审核 → 创意精修 → 分集大纲 → 大纲审核 → 大纲精修 → 分镜脚本 → 飞书归档，依赖关系严格编排，审核员意见会作为精修任务的前置输入。
- **两种运营方式**：
  - **① 自动按序执行**：一键运行全流程，5 位员工依次自动完成 9 个任务。
  - **② 手动逐步启动**：逐个任务点击执行；每步产出可**修改**后再进入下一步，也可**跳过本任务直接粘贴本地剧本文本**；还可随时从当前进度**一键自动跑完后续剩余任务**（不回头补跑前序）。
- **完全可视化**：像素风办公室全景 + 5 位数字员工卡通形象坐在各自工位，正在工作的员工会**跳动并高亮**；下方是 9 大任务的实时协作流水线。
- **逐员工模型配置**：每位员工可独立配置 API 服务商 / API Key / 模型（选项与原工具完全一致）；也支持全局默认。
- **质量校验**：分集大纲集数完整性与每集 500-600 字校验、分镜镜头数校验、飞书文档自动转 Markdown 表格。

---

## 📂 工程结构

```
.
├── script_studio.py          # 入口脚本（仅做页面配置并启动 studio.ui.main）
├── requirements.txt          # 依赖
├── studio_office.png         # 办公室全景图（必须与入口同在根目录）
└── studio/                   # 应用包
    ├── __init__.py
    ├── prompts.py            # Prompt 模板（还原原工具 foragent.py）
    ├── employees.py          # 5 位数字员工设定、Agent 技能、角色系统提示
    ├── tasks.py              # 9 个任务定义、记忆备注、任务指令文案
    ├── mock_data.py          # Mock 演示内容（离线走通全流程）
    ├── llm_service.py        # 服务商/模型清单 + LLMService（max_tokens 等）
    ├── state.py              # 会话状态、模型配置、批次切分
    ├── engine.py             # 任务编排执行、飞书文档拼装、分镜解析、集数校验
    ├── visuals.py            # 心跳保活、样式、办公室与流水线可视化
    └── ui.py                 # 侧边栏、两种交互方式、产出展示、数字员工档案、main()
```

---

## 🚀 本地运行

要求 Python 3.9–3.13。

```bash
# 1) 安装依赖
pip install -r requirements.txt

# 2) 启动
streamlit run script_studio.py
# 若提示 streamlit: command not found，用模块方式：
python3 -m streamlit run script_studio.py
```

启动后浏览器访问终端打印的本地地址（默认 http://localhost:8501）。

> 提示：修改了 `studio/` 里的任何文件后，需要**重启服务**才会生效（Streamlit 会缓存已导入的包模块）。

---

## 🤖 使用说明

1. **选服务商**（侧边栏「模型配置」）：默认 `Mock (演示)` 可离线演示；真实生成请选 Azure OpenAI (ByteDance) / OpenRouter / Google Gemini / OpenAI (GPT) / Anthropic (Claude) 并填入 API Key。
2. **项目设置**：设置剧本总集数（默认 50）、分镜模式（标准短剧 / 解说漫）。
3. **选运营方式**：
   - 自动：点「🚀 一键运行全流程」。
   - 手动：逐个任务点「启动该任务」；可在每个任务的「查看 / 修改 / 粘贴」面板里改产出或粘贴本地文本；也可点「🚀 自动执行后续剩余任务」从当前进度往后自动跑完。
4. **下载交付物**：各任务产出可下载 TXT；分镜可下载 CSV（Excel 专用）；最终飞书文档可下载 Markdown。

---

## ☁️ 部署到 Streamlit Community Cloud

1. 将本目录推送到 GitHub 仓库（确保 `script_studio.py`、`studio/`、`requirements.txt`、`studio_office.png` 都在仓库根目录）。
2. 打开 [share.streamlit.io](https://share.streamlit.io) → New app → 选择仓库、分支，**Main file path 填 `script_studio.py`** → Deploy。
3. 部署后默认即为 Mock 演示，访问者可在侧边栏自行填入各自的 API Key 进行真实生成。

**注意事项**
- 仓库中**不含任何密钥**（Key 由侧边栏手动输入），可放心公开。
- `requirements.txt` 当前未锁定版本，云端会安装最新依赖；如需更稳定的构建，建议固定版本（其中 `google-generativeai` 已被官方标记弃用，若不使用 Gemini 可移除——代码为容错导入，移除后不影响其它服务商）。
- `max_tokens` 设为 60000（见 `studio/llm_service.py` 的 `MAX_TOKENS`），需所选模型支持大输出；旧版 `claude-3-x`（最大 4096–8192）会被 API 拒绝并返回友好报错，换用支持大输出的模型即可。

---

## ⚙️ 关键配置位置

| 想改什么 | 改哪里 |
|---|---|
| 模型最大输出 token | `studio/llm_service.py` → `MAX_TOKENS = 60000` |
| 服务商 / 可用模型清单 | `studio/llm_service.py` → `PROVIDERS` / `MODELS` |
| 每集字数要求 | `studio/prompts.py`（任务5 模板）+ `studio/tasks.py`（任务7 指令） |
| 数字员工人设 / 技能 | `studio/employees.py` |
| 任务描述 / 工作方法 / 依赖 | `studio/tasks.py` |
| 办公室卡通形象位置 / 配色 | `studio/visuals.py`（`EMP_POS` / `EMP_SPRITE`） |

---

## 📌 说明

- 本产品严格按《AI Agent剧本工作室 产品需求文档》构建；生成引擎复用《三幕式创意和剧本生成0522带解说漫foragent.py》的 `Prompts` 与 `LLMService`。
- Mock 模式仅用于离线演示与可视化，真实剧本质量取决于所选真实模型。
