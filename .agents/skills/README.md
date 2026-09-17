# 企业差旅业务指南

SKILL.md 提供业务方法，hommey.yaml 只提供名称、版本与展示信息。
目录不包含 Agent 入口、DAG 步骤或记忆回写钩子。

六类专业角色、工具权限和可读 Skill 统一定义在 agent_runtime/profiles.py。
由主 Agent 按需委派；子 Agent 只报告结果。运行时校验并自动事务提交行程变更，偏好变更由主 Agent 调用 apply_changes 经同一提交门校验。

车次适配器位于 core/integrations/trains.py；知识库初始化位于 scripts/init_knowledge_base.py；
独立质量评测器位于 evaluation/judge.py。评测器不参与业务调度。
