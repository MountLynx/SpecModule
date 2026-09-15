# ppt-master 模块化设计（替代 M2）

日期：2026-09-15
状态：已评审（三节设计经用户逐节确认）
上位文档：`docs/dev/progress/module-roadmap.md` § M2 论文 → PPT

## 0. 背景与决策链

将 ppt-master skill（v6.3.2，MIT，`https://github.com/hugohe3/ppt-master`）的
Generate PPTX 主流水线拆解并 specmodule 化，作为 roadmap M2 实践线的新本体，
替代现有 ppt_writer 基础版。已确认决策：

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 渲染范式 | 工程上完全复刻 ppt-master：LLM 逐页创作 SVG → 质量门 → svg_to_pptx 确定性编译 |
| D2 | 阻塞确认门映射 | **spec 即确认**：Stage-1/Stage-2 确认内容由 module spec 承载，运行时零交互；规划产物落盘供审计 |
| D3 | 复刻范围 | Generate PPTX 主线（七步 + 条件段：源处理、图像获取、讲者备注）；Quick/Beautify/Image-to-PPTX、Create Template、Edit Native 不做 |
| D4 | 脚本资产 | vendor 最小闭包（SVG→PPTX 管线所需脚本 + 内部包 + 模板语法文件），保留 MIT LICENSE + NOTICE |
| D5 | 模块组织 | 新模块 `ppt_master` 替代 ppt_writer（归档，git 历史保留）；`template_review` 不迁移（服务 python-pptx 占位符体系，与 SVG 管线无关） |
| D6 | 图架构 | 方案 A 单模板动态图：翻译器按 spec 展开完整流图，一次 run 全程可审计 |
| D7 | 后续扩展 | grilling 式 spec 产出 tasklist（交互方式参考 TreeChat grilling），本期只记录 |

### 复刻的两处有意偏差（其余纪律忠实保留）

1. **逐页串行 → 并行 fan-out**。ppt-master 的串行节奏（五页锁重读、单上下文）
   是对抗单一聊天上下文漂移的补丁。SpecModule 每个页节点是无状态独立 LLM 调用，
   一致性由每次调用注入相同锚点输入（spec_lock + 校准表 + 风格参考）保证，
   串行约束失去必要性；并行化命中 M2 框架验证点（AND/OR join、并行页生成与合并）。
2. **阻塞确认门 → spec 契约 + 机械化校验**（D2）。规划 harness 在 spec 约束内
   裁量，来源逐字段记入 design_spec §I（`spec-confirmed` / `planner-decided`），
   即 ppt-master "explicit delegation" 分支的语义。

## 1. ppt-master Generate 主线拆解（复刻对象）

七步主流水线（`workflows/generate-pptx.md`）：

| 步骤 | 内容 | 本设计落点（节点） |
|---|---|---|
| 1 源内容处理 | source_to_md.py 转换；topic-only 走 topic-research；充分性判研 | Ingest / Research |
| 2 项目初始化 | project_manager.py 目录契约 + workflow.log | Init |
| 3 模板候选准备 | 一期非目标（见 §6） | — |
| 4 Strategist 规划 | 两阶段确认 → design_spec.md（§I–X）+ spec_lock.md，Gate 1/2 保真审查 | Plan / PlanValidate |
| 5 图像获取（条件） | ai 生成 / web 搜索 / 切片 / 派生处理，§VIII 行终态化 | IconSync / ImageAcquire |
| 6 Executor 逐页创作 | 校准表 → 逐页 SVG；早期门（P05 后）+ 终门（全页后）；修复分层；备注 | Calibrate / P01…PN / 早终门 / Repair / NotesGen |
| 7 后处理导出 | 备注切分 → finalize_svg → svg_to_pptx + readiness 门 | SplitNotes / Finalize / Export / ImageReadiness |

保留的纪律语义：门点节奏（检查器只在门点或合并修复批后跑）、修复修在
拥有该错的最浅层、schema 校验不等于保真（PlanValidate 机器化 Gate 1/2 的
可校验子集）、silent 不是确认。

## 2. 模块形态

- 位置：`example/modules/ppt_master.py`（ModuleEntry）+ `example/ppt_master/` 包
- 模板：`generate`（唯一），translator 型翻译器（script，确定性）
- registry：全部 harness / script / command / guard 一次注册
- ppt_writer 归档：`example/modules/ppt_writer.py`、`example/ppt_writer/` 及其
  测试移除或标记 legacy（实施计划决定去留方式）；store 发布闭环验收改用
  本模块 Mock 全链零 LLM 路径（§7 测试层 2）

## 3. 页册先行原则

tickflow tasklist 在 run 前一次翻译成静态图，Plan 之后不能改图；而 ppt-master
页数在规划后确定。spec 即确认下的自洽解：

> **spec 必须携带显式页册**（每页 id/标题/角色/要点）。翻译器据此静态展开
> N 个页节点；规划 harness（Plan）在页册约束内细化每页到 design_spec §IX
> 深度（内容、关系、构图参考、资源行），**无权增删页、改序、改标题**。

"页册从大纲/题目自动生成"归入 D7 后续扩展（grilling 式 spec 产出 tasklist），
与本模块解耦——它产出对本模块合规的 spec。

## 4. Module spec schema

全部字段除 `roster`、`source` 外可选、有缺省；spec 写了的字段是已确认的
Literal/Semantic 要求（Plan 无权改），空缺字段是 Plan 裁量域。

```jsonc
{
  "project": "my_paper_ppt",
  "source": { "kind": "files|topic", "paths": ["paper.md"], "topic": "..." },
  "roster": [                       // ★必备·显式页册
    { "id": "p01", "title": "研究背景", "role": "cover|content|divider|closing",
      "points": ["..."], "section": "intro" }
  ],
  "contract": {                     // Stage-1 沟通契约（空 = 规划者裁量）
    "language": "zh", "audience": "...", "intent": "...",
    "audience_outcome": "...", "core_message": "...", "content_divergence": "..."
  },
  "canvas": { "format": "16:9" },
  "reading_mode": "balanced",       // text | balanced | presentation
  "style":   { "mode": "...", "visual_style": "...", "palette": {...},
               "typography": {...}, "icons": {"library": "tabler-outline",
               "stroke_width": 2} },
  "images":  { "sources": ["ai|web|user|none"], "notes": "策略说明" },
  "production": { "speaker_notes": true },
  "template": { "roots": [] },      // 一期不实现（见 §10），字段保留
  "output":  { "dir": "projects/my_paper_ppt" }
}
```

语义规则（复刻 ppt-master confirmed-value 语义）：确认保留值与所属字段的
语义类型——Literal 要求逐字保留；Semantic 要求保留事实/关系/意图；空缺即
无显式约束（下游从源与请求判断），规划者决定后记录 provenance。

## 5. generate 流图与节点清单

### 流图（翻译器动态展开）

```
[Ingest] ──> [Init] ──> [Plan] ──> PlanValidate
PlanValidate ──|ready|──> (IconSync) ──> (ImageAcquire) ──> [Calibrate]
Calibrate ──> P01..P05 (并行) ──> AND1 ──> [EarlyGate]
EarlyGate ──|clean|──> P06..PN (并行) ─────────────┐
EarlyGate ──|issues|──> [EarlyRepair]（批次1终态）──┤
                                              AND2 ──> [FinalGate]
FinalGate ──|errors|──> [Repair] ──(守卫环)──> FinalGate
FinalGate ──|clean|──> (ImageReadiness) ──> (NotesGen) ──> (SplitNotes)
          ──> [Finalize] ──> [Export] ──> [Report]
```

（修订 2026-09-15：NotesGen 移到终门之后——备注校验基于最终 SVG，与
ppt-master 逻辑构造阶段同序；初稿误置于终门前。）

- 页册 ≤6 页：翻译器省略 EarlyGate 段（复刻 ppt-master 同规则）
- 条件节点（括号）：翻译器按 spec 声明生成或省略
- 守卫环（FinalGate→Repair→FinalGate）满足 tickflow 环上至少一条守卫边的约束

### 节点清单

| 节点 | 类型 | 职责 |
|---|---|---|
| Ingest | command（vendor `source_to_md.py`）/ script（md/CSV 直读） | 源 → 标准 markdown + conversion profile |
| Research | harness（条件：topic-only 或事实缺口） | 研究对落盘（facts + research.md） |
| Init | script | 项目目录契约 + workflow.log |
| Plan | harness（Strategist 角色，3 层 prompt） | spec 契约 + 源事实 → design_spec.md + spec_lock.md（方向构造、§IX 细化、§VIII 资源行、锁锚点；无权改页册） |
| PlanValidate | script | 页册保真（数量/顺序/标题不变）、每页 Audience move 存在、§VIII 行与 spec 图像声明一致、锁锚点齐全；违反即 fail-fast |
| IconSync | command（`icon_sync.py`，条件） | 图标池物化到 `icons/` |
| ImageAcquire | harness 图像模式 + command（`image_gen` / `image_search` / `slice_images` / `analyze_images`，条件） | §VIII 行全部终态化 |
| Calibrate | command（`text_measure.py calibrate`） | 角色字宽校准表 → 全体页节点共享输入 |
| P01…PN | harness ×N（Executor 角色），并行 | 逐页 SVG 写入 `svg_output/`；输入 = spec 页块 + spec_lock + 校准表 + 触发的风格/模式参考 |
| EarlyGate / FinalGate | command（`svg_quality_checker --stage early/final`） | 报告落 `validation/`；stdout 摘要进事件流 |
| EarlyRepair / Repair | harness（按报告定向修复，合并修复批纪律） | 修在拥有层：页内错修页、方法级偏差修校准表 |
| NotesGen | harness（executor-notes，条件） | 基于最终 SVG 写 `notes/total.md` |
| ImageReadiness | script（条件：存在 Needs-Manual 行） | 缺文件 → fail-fast 并列精确文件名 |
| SplitNotes / Finalize / Export | command ×3（`total_md_split.py` / `finalize_svg.py` / `svg_to_pptx.py`） | 严格串行链 |
| Report | script | 聚合产物路径 + postflight 摘要 |

### Prompt 三层映射（AGENTS.md 约定）

- `prompt_core`：角色纪律模板（Strategist / Executor 核心条款，含门与纪律），固定
- `prompt_modes`：按 spec 动态选择参考文件清单（mode/style 词表、图像、备注）——
  缺 key 即 KeyError，不静默
- `prompt_extra`：本次调用输入（spec 页块、源事实摘录、校准表、路径契约）

ppt-master `references/*.md`（角色定义、187 形状词表、shared-standards 等）
**原样保留、改编为 prompt 素材**，放 `example/ppt_master/prompts/`，由确定性
script（参考包组装器）拼装清单；不进 vendor 目录（它们是 prompt 资产，不是
可执行资产）。

## 6. 工件契约与 vendor 布局

### 目录契约（Init 建立；文件为真 + 信封）

沿用 ppt_writer 确立的"信封 + 文件"模式：harness 产出结构化收据（写了哪些
文件 + 自检项，经 output_format JSON 校验），文件本体由下游 command/script
直接读。RunState 仍是唯一运行时状态源，文件是产物不是状态。

```
<output_dir>/
  design_spec.md  spec_lock.md     ← Plan 产出（vendor 模板语法）
  sources/  analysis/  images/  icons/
  svg_output/  svg_final/  notes/
  validation/   ← workflow.log + 校准 json + 早/终门报告
  exports/      ← *.pptx + postflight
```

### vendor 布局（D4 最小闭包）

```
example/ppt_master/
  __init__.py  module.py  spec_schema.py  translator.py  nodes/…
  prompts/            ← 参考素材（见 §5 prompt 映射）
  vendor/ppt_master/
    scripts/          ← svg_quality_checker, finalize_svg, svg_to_pptx,
                        text_measure, icon_sync, image_gen, image_search,
                        slice_images, analyze_images, source_to_md,
                        total_md_split + 内部包（pptx_shapes/ pptx_ooxml/
                        svg_finalize/ image_backends/ …）+ scripts/docs
    templates/        ← design_spec_reference.md, spec_lock_reference.md,
                        schemas/, icons/ 词表, charts/ tables/ 词表
    LICENSE + NOTICE  ← 出处、版本（6.3.2）、改动清单
```

闭包以"导入闭包可运行"为准：以 svg_quality_checker / finalize_svg /
svg_to_pptx 三入口的传递依赖为准圈定，实施时用导入图验证无缺件。
`references/`、`workflows/` 不 vendor；confirm_ui / svg_editor / 音视频脚本
不 vendor（一期非目标或由宿主形态承担）。

## 7. 失败与修复语义

| 失败 | 语义 | 复刻自 |
|---|---|---|
| PlanValidate 违反 | `Failure(type="infrastructure")` 停图（硬合规 fail-fast） | 硬合规纪律 |
| 页节点 LLM 失败/输出校验不过 | **降级产出失败收据** `{status: failed}` 而非中断——AND join 不饿死，FinalGate 判缺失页 blocking → Repair 重画 | Act 纪律（修在页层） |
| 早/终门 blocking issues | Repair 按报告定向修复 → 守卫环回门；**环上限 2 轮**（script 计数），超限 infrastructure Failure 停图报告 | consolidated-pass + 非交互补强 |
| 图像行无法终态 | 标 `Needs-Manual` 继续；ImageReadiness 门缺文件即 fail-fast 列名 | Step 7 readiness gate |
| command 非零退出 | 修在拥有该错的源工件，从失败子步恢复，**从不重启规划** | failure-recovery 纪律 |

## 8. 测试策略（四层）

1. **单测**：spec schema 校验、翻译器展开（N 页 → N 页节点、条件段生成/省略、
   ≤6 页无早门、守卫环构造）、PlanValidate 规则表、信封契约
2. **Mock 全链冒烟**：MockLLM 返回预置合格 SVG fixture（取自 ppt-master 示例页，
   随 vendor 引入）→ 真实 checker/exporter 跑通 → 断言图推进 + 产物落盘；
   零 LLM、CI 可跑，兼作 store 发布闭环验收 fixture
3. **vendor 闭包完整性**：command 节点对 fixture 项目真实执行（checker early/final、
   finalize、export）——"工程上完全复刻"的验收底线
4. **真实 LLM 冒烟**：1–2 页小册子端到端（smoke 标记，本地付费可跳过）

## 9. M2 框架验证点对照

| M2 验证点（roadmap） | 本设计落点 |
|---|---|
| 完整 spec 驱动 | spec schema 即确认契约 + 页册先行 |
| 复杂流图（AND/OR join、并行页生成与合并） | N 页并行 fan-out + 批次 AND + 守卫修复环 |
| command/script 节点 | vendor 闭包 10+ command 节点、翻译器/门 script |
| submodule 打包发布 + store 闭环 | publish→install→run 首个真实验收（Mock 全链零 LLM 路径作 fixture） |
| 反哺框架 | 页节点"失败收据 + AND join 不饿死"模式；参考包组装器出现第二消费方再提炼（架构规则 6） |

## 10. 后续扩展（本期只记录不实现）

- **grilling 式 spec 产出 tasklist**（D7）：多轮前沿提问 + 术语沉淀 + 设计树
  更新（交互方式参考 `TreeChat` grilling 模式）→ 产出本模块合规 spec
  （页册自动生成），补上游
- Quick profile（轻量 spec 短路直出）
- 模板工作区模式（structured 页创作：workspace 安装 + Master/Layout slot；
  spec schema 预留 `template.roots` 字段不实现）
- 动画 / 音频 / 视觉回看 stage；Beautify / Edit Native 路线
