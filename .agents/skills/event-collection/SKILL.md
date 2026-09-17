---
name: event-collection
description: Collect and incrementally update the employee's current company trip, including origin, destination, dates, purpose, work location, work schedule, and missing information. Use when a user starts or supplements a business-trip task.
---

# 整理当前出差事项

由 trip_context 角色整理事实，通过 report 提出变更，不直接写记忆。
只整理 origin、destination、start_date、end_date、duration_days、trip_purpose、work_location、work_schedule。
每个变更放在 data.trip 中，data.field_sources 引用本轮用户原文。未知日期不能默认今天。
新行程用 trip_action=new，取消出差用 cancel，并提供 action_source；修改已有行程用 update。
新行程不继承旧行程字段。缺少的必填项交给主 Agent 统一补问。
运行时校验原文依据并自动提交有效行程变更；committed=true 才表示已保存，主 Agent 不重复 apply_changes。
无法确定用户是否在提供行程信息时先澄清意图；没有可提取字段时返回 needs_input，不创建空行程或宣称保存成功。
