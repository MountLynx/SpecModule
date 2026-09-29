# planner 资源盲选问题（§VIII 词表 / 图标库存在性）

状态：问题分析 + 候选修复方向（未定稿；定稿后按 superpowers 流程出
design + plan）。背景与实测过程见
[ppt-master-spec-authoring.md](ppt-master-spec-authoring.md)。

## 问题定义

plan 节点的规划者（LLM）在产出两类资源决策时，**没有任何工具化手段感知
机检标准**，而它的输出会被下游硬校验：

1. **design_spec §VIII / spec_lock images 行的机检词表**——`Type`
   （Placed / Source / Illustration Sheet 的语义边界）、`Status`
   （按 Acquire Via 分派的终态词表：ai→Generated、user→Existing……）、
   `Crop Policy`（adaptive/no-crop）。校验方是终门 vendor checker。
2. **本地图标库的存在性**——icon_pool 里的 id 必须在 vendor 图标库中
   真实存在，且用完整 `库/名字` 形式。校验方是 icon_sync（rc≠0 即
   Failure(llm)，下游停滞）。

标准存在于**运行时校验器**（vendor checker / icon_sync），却不在**规划
输入**（plan prompt + contract）里——planner 只能凭训练先验猜，猜错即停。

## 实测证据（2026-09-15 调优轮，6 轮真实运行）

- 词表违反**每轮错法都不同**（非确定性）：轮 1 lock 缺 `## colors` 节
  （上一轮明明写对了）；轮 4 六张图全部 `Type: Source`（应为 Placed）；
  轮 5 Status 写成 `Pending`/`Ready`（应为 Generated/Existing）。
- 图标池**两次运行两份不同池、两次都踩雷**：一次库外名（bacteria 不在
  5138 个的本地子集里）、一次裸名格式不合法。planner 根本不知道本地有
  哪些图标。
- 最终靠 spec 契约逐条钉死（词表、终态、图标白名单）才通过。

## 代价模型

| 违反点 | 暴露时机 | 代价 |
|---|---|---|
| 图标池 | IconSync（规划后 immediately） | Failure(llm) → 图停滞，损失 ~2 分钟 |
| §VIII/lock 词表 | 终门首轮（全部页生成后） | scope 级 blocking，修复环修不了 → 烧尽 2 轮（每轮 9 页 checker + repair LLM 调用）→ 停图。**整条页生成链费用白花** |

当前兜底 = 每份 spec 用 contract 钉死。缺陷：每份 spec 重复劳动、靠作者
记得全部词表、库更新后白名单静默失效——这是把校验器的职责转嫁给了 spec
作者，不可持续。

## 根因

上游 ppt-master 是**交互式**产品：词表语义由 Stage 2 人工确认兜底，图标
库外名走"re-pick before continuing"的人工重挑。SpecModule 复刻是**非交互
管线**，人不在环上，人工纠错环节消失，而校验标准没有跟着前移到规划输入。
（修复环补上了页级问题的自愈，但 scope 级问题本质不可修复，只能预防。）

## 候选修复

| 方案 | 内容 | 代价/风险 | 评估 |
|---|---|---|---|
| **A. 词表内建 prompt** | §VIII/lock 词表（Type 语义、Status 终态分派表、Crop）像 palette/typography 锚点一样钉进模块 plan prompt（prompts_config._PLAN_CORE） | 词表小且静态，一次内建全 spec 受益；与参考包（上游格式）并存需写清优先级 | **必做**。低风险高收益 |
| **B. PlanValidate 左移资源校验** | 规划后立即校验 icon_pool 存在性/格式 + §VIII/lock 词表，违规 infrastructure 停图 | 不解决盲选，但把 §VIII 类失败的代价从"整条页生成链"降到"规划后一秒"。注意双源漂移：词表规则与 vendor checker 重复定义，需调研 checker 是否有可复用的单跑入口（或接受漂移风险 + 测试钉住） | **必做**（与 A 互补：A 降发生概率，B 降暴露代价） |
| **C. icon_sync 软降级** | 同步前预过滤池：库外名剔除、收据记 skipped（可见不静默，calibrate 有先例）；页面少一个图标 ≠ 废整册 | 改变"硬失败 loud"语义——但对象是"缺失图标"这种低危资源，与整册作废不成比例；非交互场景合理 | **推荐**（改动小） |
| **D. 库索引喂给 planner** | tabler-outline 5138 个 id ≈ 2 万 token/次 plan 调用；或按页册主题检索子集（如取 N 百个）喂给 | 全量贵；检索子集引入"检索器质量"新变量 | 备选（C 落地后优先级降低） |

**建议组合：A + B 必做，C 跟进，D 暂缓。** A 把已知词表变成规划常识；
B 保证任何漏网（含未来 checker 新增规则前的窗口期）在一秒内 loud；
C 消掉图标存在性这个最大的非确定性源。

## 验收（修复完成的判定）

一份**裸 spec**——不钉图标池、不写 §VIII 词表、只声明
`images.sources: ["ai", "user"]` 与资源清单——能在非交互全链通过
（含故意放一个库外图标名的对抗用例：要么被 C 剔除继续跑、要么被 B
一秒内点名报错，绝不允许烧完页生成链才死）。
