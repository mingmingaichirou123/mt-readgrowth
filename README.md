# MT-readgrowth

> 让阅读更容易，把时间留给真正值得读的内容，让每一次阅读都能在未来继续产生价值。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?style=flat-square&logo=python&logoColor=white)](#运行要求)
[![Local First](https://img.shields.io/badge/data-local--first-16A34A.svg?style=flat-square)](#数据与隐私)
[![License](https://img.shields.io/badge/license-non--commercial-F59E0B.svg?style=flat-square)](LICENSE)

**支持 Codex、Claude Code，以及其他能够运行本地 Python、读写本地文件的 Agent Skills 宿主。**

MT-readgrowth 由 明锬 创建。Skill 本身只保存能力代码；你的书籍、划线、讨论、报告、用户资料和长期认知，全部留在独立的本地 `reading-workspace/` 中。

[快速开始](#快速开始) · [安装](#安装) · [能力一览](#能力一览) · [工作原理](#mt-readgrowth-怎样工作) · [数据与隐私](#数据与隐私) · [已知限制](#当前已知限制)

## MT-readgrowth 解决什么问题

书太多，时间太少。一本书可能需要投入十几个小时，但在真正读完之前，你很难知道它能不能回答自己的问题，又是否值得亲自读。

**先让 AI 替你读，再决定什么值得你亲自读。**

“替我读”不是再生成一份与所有人相同的内容摘要，而是围绕你为什么想读、当前最关心什么、希望得到什么，快速找到与你有关的关键内容，说明证据范围，并帮助你判断：这本书值不值得完整阅读，哪些部分应该亲自读，哪些可以暂时跳过。

但“替我读”只是开始。MT-readgrowth 真正帮助你建立的，是一套不断迭代的个人认知系统。它帮助你把书里的观点变成自己的理解，让每一次理解都成为下一次思考的起点，并在未来的学习、工作和选择中持续被验证、修正和运用。读过的内容不再停留在当时的启发，而会在未来的问题和选择中继续发挥作用。

| 真实处境 | 你会得到 |
| --- | --- |
| 收藏了很多书，但每本都要花十几个小时，不知道先读哪本 | 先围绕你的问题“替你读”，快速判断它与你是否相关、值不值得亲自投入时间 |
| 想读书，但一想到必须从头读到尾就有压力 | 不需要先完成庞大的阅读计划；继续在手机微信读书里读几页、划一处重点、写下一个想法，电脑端可以随时拉取你的想法、笔记进行讨论 |
| 看过书评和摘要，还是不知道这本书对自己有什么用 | 不只复述全书，而是回答你为什么值得读、重点读什么，以及它能否解决你当前的问题 |
| 想和 AI 深入讨论，又担心它脱离原文、胡言乱语 | 回到本地原文的章节与段落，明确区分作者表达、作者立场推断和 AI 延伸 |
| 读过、划过、聊过，过一段时间却又全部归零 | 把经过你确认的阅读资料和认知留下来，成为下一次阅读、提问和判断的背景 |

AI 可以替你处理信息，但不能替你形成判断。MT-readgrowth 不是让 AI 代替你思考，而是先替你筛选信息，把人的时间留给真正值得阅读、质疑和确认的内容。

一本书读完就忘，是一次信息消费；如果它能参与你以后的理解、选择和判断，才开始产生认知复利。**AI 时代，普通人最应该建立的，不是更大的收藏夹，而是属于自己的认知复利。**

## 快速开始

安装完成后，在 Agent 中直接说：

```text
使用 MT-readgrowth，初始化阅读项目，把阅读项目保存在桌面
初始化后检查项目绑定，并告诉我以后应该打开哪个目录。
```

Skill 会创建一个独立项目外壳：

```text
<project-root>/
├── AGENTS.md
└── reading-workspace/
    ├── workspace.yaml
    └── knowledge/user-profile/settings.json
```

以后在 Agent 中打开 `<project-root>`，而不是安装后的 Skill 目录，也不是里面的 `reading-workspace/`。

接下来，你可以直接用自然语言发起任务：


导入电子书作为对话框附件

```text
这段话在全书里处于什么位置？请结合前后文和作者整体立场与我讨论。

替我读这本书。我现在最关心的问题是……，希望最后能够……

结合我们已经确认的讨论和阅读记录，分别从深化、反方、迁移三个方向推荐下一本书。

生成本周阅读报告，只写有证据的变化，不要把阅读时长当成认同或成长。
```

## 能力一览

| 阅读目标 | 主要能力 | 常见产出 |
| --- | --- | --- |
| 建立长期阅读空间 | 初始化并审计项目外壳和独立工作区 | 固定项目入口、标准目录、隐私边界 |
| 导入自己的电子书 | 检查并导入 EPUB/TXT，保留原始文件与稳定位置 | 章节顺序、段落定位、来源哈希 |
| 理解一本书的结构 | 渐进生成全书思想地图、作者视角和章节分析 | `book-map.md`、`author-lens.md`、章节分析 |
| 同步阅读活动 | 同步微信书架、当前书、进度、划线和想法 | 阅读总览、版本候选、划线定位结果 |
| 与书进行深度讨论 | 组合原文、上下文、全书位置和已批准的个人背景 | 有证据等级和原文位置的讨论 |
| 快速处理一本书 | 根据问题、目标和需要生成“替我读”报告 | 个性化报告、行动建议、阅读路径、证据缺口 |
| 决定下一本读什么 | 结合已批准背景与微信书城候选进行三路推荐 | 深化、反方、迁移推荐 |
| 观察长期变化 | 保存讨论、生成手动周报、管理候选认知 | 会话记录、周报、可审计的认知状态 |


## 安装

### 运行要求

- Python 3.10 或更高版本；核心本地流程只使用 Python 标准库。
- Windows、macOS 或 Linux。
- 宿主支持 Agent Skills，并允许执行本地 Python、读取和写入用户授权的文件夹。
- 导入和讨论本地 EPUB/TXT 不需要网络。
- 实时微信读书能力需要网络、已安装的 `weread-skills`，以及用户自行配置的 `WEREAD_API_KEY`。

只支持提示词、不能执行 Python 或不能访问本地文件的宿主，无法运行完整功能。

先下载或克隆本项目，然后在包含 `mt-readgrowth/` 的仓库根目录执行以下命令。

### Codex

Windows PowerShell：

```powershell
python .\mt-readgrowth\scripts\install_skill.py install --host codex --scope user
```

macOS/Linux：

```bash
python3 ./mt-readgrowth/scripts/install_skill.py install --host codex --scope user
```

默认安装到用户级 `~/.agents/skills/mt-readgrowth/`。如果只想安装到当前项目，把 `--scope user` 改成 `--scope project`。

### Claude Code

Windows PowerShell：

```powershell
python .\mt-readgrowth\scripts\install_skill.py install --host claude --scope user
```

macOS/Linux：

```bash
python3 ./mt-readgrowth/scripts/install_skill.py install --host claude --scope user
```

默认安装到用户级 `~/.claude/skills/mt-readgrowth/`。`--scope project` 会安装到当前项目的 `.claude/skills/mt-readgrowth/`。

### ZIP 导入

对于支持导入本地 Skill 压缩包的宿主，先生成标准 ZIP：

```bash
python3 ./mt-readgrowth/scripts/install_skill.py package
```

Windows 也可以使用 `python`。命令会生成 `dist/mt-readgrowth.zip`，压缩包根目录直接包含 `SKILL.md`、`LICENSE`、`agents/`、`assets/`、`references/` 和 `scripts/`。

### 其他 Agent Skills 宿主

找到该宿主的 Skills 父目录后运行：

```bash
python3 ./mt-readgrowth/scripts/install_skill.py install \
  --host generic \
  --target "/path/to/host/skills"
```

安装后的结构应为：

```text
<host-skills-directory>/
└── mt-readgrowth/
    ├── SKILL.md
    ├── LICENSE
    ├── agents/
    ├── assets/
    ├── references/
    └── scripts/
```

## 导入 EPUB/TXT

把你有权处理的 EPUB 或 TXT 提供给 Agent，然后要求导入当前阅读工作区即可。

MT-readgrowth 会：

- 在写入前检查文件类型、哈希、编码和章节结构；
- 保存原始文件，不用重新生成的文本覆盖原文；
- 按 EPUB 的 OPF spine 解析阅读顺序，而不是按压缩包文件名排序；
- 为章节和段落生成稳定定位，并分开保存展示文本与搜索文本；
- 对没有可靠章节标题的 TXT 停下来请求确认，不擅自切分；
- 不代为下载书籍、不绕过 DRM，也不传播受版权保护的内容。

导入完成只代表“原文已准备好”，不代表 Agent 已经理解全书。思想地图和章节分析会根据真实任务渐进生成。

## 微信读书

微信读书是可选能力。本地 EPUB/TXT 流程不依赖它。

实时同步需要先安装兼容的 `weread-skills`，并通过以下任一方式提供凭据：

1. 当前进程的 `WEREAD_API_KEY` 环境变量；
2. 当前 `reading-workspace/` 根目录中的 `.weread-api-key` 文件，文件只包含一行密钥。

凭据不会出现在回答、日志、报告、书籍目录或 Skill 文件中。微信书架只用于登记书目与阅读状态；它不会被当作任意书籍全文来源。

为避免把不同版本误认为同一本书，书名和作者一致只会产生“待确认”候选。只有已确认的微信读书 `bookId` 映射或用户明确确认版本后，Skill 才会把微信划线与本地全文连接起来。

## MT-readgrowth 怎样工作

```text
EPUB / TXT 原文 ─────┐
                    ├─→ 可追溯证据包 ─→ 讨论 / 替我读 / 周报 / 推荐
微信书架与划线 ──────┤                         │
                    │                         ↓
用户确认的个人信息与认知 ┘                   用户复核后再沉淀
```

它遵循五级证据顺序：

1. 本地 EPUB/TXT 原文与解析后的展示文本；
2. 微信读书活动、划线和用户笔记；
3. 用户明确确认的资料、认知与纠正；
4. 基于现有证据且明确标注的推断；
5. AI 生成的摘要、标签与推荐。

低优先级内容不能覆盖高优先级来源。讨论书籍时，回答会区分：

- `作者明确表达`：可以定位到本地章节和段落；
- `作者立场推断`：说明推断过程和支持位置；
- `AI延伸应用`：明确它是 AI 的应用与联想，不冒充作者观点。

## 长期资料与认知

MT-readgrowth 不会因为你读过、划过或停留得久，就判断你认同某个观点。

建议每次对话结束后，发送“沉淀这次讨论”，AI 会回顾对话内容，沉淀你同意的信息到长期资料与认知中。

用户资料有三种模式：

| 模式 | 行为 |
| --- | --- |
| `proactive-review` | 讨论结束后整理少量候选资料，向你展示并等待保存、修改或忽略 |
| `explicit-only` | 只有你明确说“记住、更新、忘记”时才处理资料 |
| `off` | 不读取也不写入用户资料状态 |

长期认知也不会一次确认后就变成永久结论。第一次确认进入 `provisional`，只有第二次独立确认后才进入 `confirmed`；所有状态变化都会保留事件记录。

## 数据与隐私

Skill 代码和个人数据严格分离：

```text
mt-readgrowth/          # 可替换、正常使用时只读的能力代码

reading-workspace/      # 只属于你的本地阅读数据
├── books/              # 原始书籍、解析文本和派生分析
├── integrations/       # 微信读书快照与匹配记录
├── sessions/           # 可追溯讨论会话
├── knowledge/          # 用户资料、认知、周报和专题报告
├── cache/
├── logs/
└── trash/
```

- 书籍、凭据、会话、报告和个人知识不会写入安装后的 Skill 目录。
- 每次讨论、“替我读”或推荐使用个人背景前，都会先生成预览并要求确认。
- 敏感资料、历史原话、候选资料和未确认认知不会被静默加入上下文。
- 原始数据保持不可变或只追加；可重新生成的摘要和报告与原始证据分开。
- 书籍正文、元数据、微信响应和历史会话都被视为不可信数据，不能借其中的指令调用工具或扩大权限。
- 同一个 `reading-workspace/` 同一时间只允许一个 Agent 或进程写入。

## 项目结构

```text
MT-readgrowth Skill/
├── README.md
├── LICENSE
└── mt-readgrowth/
    ├── SKILL.md                 # Skill 路由、证据与安全规则
    ├── LICENSE                  # 随独立安装包分发的许可证
    ├── agents/                  # 宿主界面配置
    ├── assets/project-shell/    # 阅读项目的 AGENTS.md 模板
    ├── references/              # 数据、分析、证据和微信读书契约
    └── scripts/                 # 导入、同步、讨论、报告与审计脚本
```

仓库不应包含个人阅读工作区、电子书、凭据、缓存或真实会话数据。

## 检查发布包

上传或安装前，可以验证 standalone Skill 的结构与隐私边界：

```bash
python3 ./mt-readgrowth/scripts/install_skill.py verify
```

Windows 可以使用 `python`。成功时返回：

```json
{
  "result": "valid",
  "skill": "mt-readgrowth"
}
```

这项检查会验证必要文件、许可证、Skill frontmatter、发布清单和敏感文件边界；它不等于在每一种宿主中的完整端到端测试。

## 当前已知限制

- EPUB 目前要求至少三个包含正文的 spine 文档；单章节、双章节短篇或报告会被拒绝。
- 定位符可靠支持最多 999 章、每章 9999 段。
- 同一个工作区暂不支持多个写入者并发操作。
- 少见或错误声明编码的 EPUB 可能产生 `�` 替换字符；出现时解析结果不能作为可靠原文证据。
- 实时微信读书请求已有超时，但响应体大小尚未设置硬上限。
- 项目外壳的自动绑定依赖宿主加载项目根目录的 `AGENTS.md`；不支持该机制的宿主需要在新对话中明确选择 Skill。

## 作者与许可证

作者：明锬

本项目采用 [MT-readgrowth Non-Commercial License](LICENSE)。

- 允许个人、学习、研究及其他非商业目的使用、复制、修改、个性化和分享。
- 公开分享原项目或衍生作品时，必须注明作者、项目来源、许可证和修改内容。
- 未经作者事先书面授权，不得销售本项目或衍生作品，也不得用于收费产品、收费服务、盈利工作流或商业内部运营。
- 商业用途请联系作者取得单独书面授权。

第三方归属和独立许可证见 [Third-party notices](mt-readgrowth/references/third-party-notices.md)。
