<!-- mt-readgrowth-project-binding:v1:start -->
# MT-readgrowth 阅读项目

## 强制绑定

- 对本项目中的每个新任务，先使用宿主用户级安装的 `mt-readgrowth` Skill 判断和执行阅读系统流程；不要要求用户在每条消息中重复 Skill 名称。
- 当前项目唯一允许的阅读数据工作区是 `./reading-workspace`。不得搜索、猜测、连接或写入其他阅读工作区。
- 从已安装 Skill 的 `SKILL.md` 所在目录解析其 `scripts/`、`references/` 和 `assets/`；不得把 Skill 代码复制进 `reading-workspace`。
- 第一次读取或写入工作区前，验证 `./reading-workspace/workspace.yaml` 的 `skill_name` 为 `mt-readgrowth`。找不到 Skill、配置冲突、路径越界或出现多个候选时，停止写入并向用户说明。
- 第一次执行阅读系统操作前，简短报告 `MT-readgrowth active` 和解析后的工作区路径，让用户能够确认本次绑定。

## 数据与记忆边界

- 打开本项目或加载 Skill 不等于授权读取全部个人内容。只读取当前请求所需的书籍、会话、资料或报告，不主动枚举工作区。
- 用户资料继续服从工作区已配置的 `explicit-only`、`proactive-review` 或 `off` 模式；自动关联 Skill 不得变成静默保存用户信息的授权。
- 所有用户资料、长期认知、会话和报告写入都必须经过 `mt-readgrowth` 的脚本、确认门和审计，不得口头声称“已记住”却不写入合法事件。
- 同一个 `reading-workspace` 只允许一个 Agent 或进程写入；不得并发执行导入、同步、会话记录或认知决定。
- 与阅读系统无关的请求可以正常回答，但不得因此读取或修改 `reading-workspace`。
<!-- mt-readgrowth-project-binding:v1:end -->
