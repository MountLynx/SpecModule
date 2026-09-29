# ppt_master generate spec 制作经验

来源：2026-09-15 MBGS-SNDPR 论文 → 9 页 deck 的真实 LLM 全链调优（5 次运行：
2 次夭折于规划段、2 次终门修复轮耗尽、1 次全链通过）。读者：后续为
ppt_master（或同构实践线模块）撰写 spec 的人/agent。活样例：
[`example/spec.mbgs_v6.json`](../../example/spec.mbgs_v6.json)。

## spec 的角色

ppt_master 是"spec 即确认"：没有交互确认环节，spec 落地后规划者（LLM）
按它一次产出 design_spec/spec_lock，机检链（PlanValidate → 门 checker）
对产物逐条硬校验。因此 spec 不只是"素材声明"——**凡规划产物中存在机检
词表/存在性约束的地方，spec 契约必须提前钉死**，否则 planner 的自由裁量
就是下一次停图的原因。

## 五条核心原则

1. **页册先行，页 id 是语法约束。** roster 页 id 必须恰为 `p01..pNN`
   （translator 展开期差集校验，schema 不知道 N）；title 写判断句、points
   写源数据锚点。页数即图形状：`n > 6` 有早门（批 1 = 前 5 页先行），
   `n ≤ 6` 单终门——选页数时想清楚要测/要用的链路。
2. **修复环只修页 SVG。** verdict issues 里带 `page` 键的问题（溢出、
   渐变、viewBox）repair 按页重生成自愈；scope 级问题（design_spec §VIII、
   spec_lock、图标池等**一次性产物**的违规）repair 修不了，只能烧完修复
   轮（上限 2）后 infrastructure loud abort。所以：**一次性产物的机检约束
   必须在 plan 之前进 spec 契约**。
3. **词表分三层，各写各的，禁止混用。** 图像域三层词表见下表——本轮
   最大的坑就是 receipt 层与 §VIII 层词表混写（`ready` 不是 §VIII 合法
   Status），以及 planner 把直接放置的图写成 `Type: Source`。

| 层 | 消费方 | 词表 | 写错的后果 |
|---|---|---|---|
| `spec.images.sources` | translator 构图 | `ai/web/user/placeholder/none`（none 独占） | 展开期 ValueError |
| plan 收据 `image_rows[].status` | 模块 acquire/就绪门 | 模块自用（本轮约定 pending/ready；仅 `Needs-Manual` 触发就绪门硬校验） | 语义漂移，一般无碍 |
| design_spec §VIII 行 + lock images 行 | 终门 checker | `Type: Placed/Source/Illustration Sheet`；`Status`: ai→`Generated`、user→`Existing`、slice→`Generated`、web→`Sourced`、formula→`Rendered`（终态备选 `Needs-Manual`；`Pending` 非终态、`Ready` 非法）；`Acquire Via`: `ai/web/user/placeholder/slice/formula`；`Crop Policy`: `adaptive/no-crop` | scope 级 blocking → 修复轮耗尽 → 停图 |

   另注意 `Type: Source` 的上游语义是"未放置的派生母本"：进 lock、被 SVG
   引用、没有 `Derived from` 子行，三者任一都是 blocking。直接上版面的图
   一律 `Type: Placed`。
4. **非交互环境没有"重挑"环节。** 图标池必须逐个验证存在且用完整
   `库/名字` 形式（icon_sync 对库外名/裸名都硬失败 → Failure(llm) → 图
   停滞）；用户图路径必须逐字给定。planner 对库存在性是盲选——spec 不钉，
   就是抽奖。这个盲选问题的系统性修复（词表内建 prompt / 左移校验 /
   软降级）单列分析：[planner-resource-blindspot.md](planner-resource-blindspot.md)。
5. **数值忠实是契约不是期望。** contract 明写"数值逐字取自源、禁止改写"，
   points 里给足数字锚点，planner/页作者才没有编造空间。

## contract 写法模式

`contract` 是 `{主题键: 规则文本}`，整包注入 plan prompt。按四类组织条目：

- **资源清单类**：用户图清单（逐字路径 + 语义描述）、AI 图行（页 id、
  规范路径、英文 prompt 主题：具体视觉描述 + no text/no labels/watermark）、
  图标池（验证过的完整 id 数组，"不得增删替换"）。
- **机检词表类**：§VIII 行的 Type/Status/Acquire Via/Crop、lock images 行
  格式（`- <页id>: images/<file> | source=<via> | crop=...`）、模块机检锚点
  已由模块 prompt 内建（`## <页id>` 页块、palette/typography），spec 侧
  只需补资源相关词表。
- **风格裁量类**：语言（中文 + 术语保留英文缩写）、版面（数据图占宽 ≥55%、
  不复刻坐标轴）、备注深度。
- **全局纪律类**：数值忠实、页册神圣的提醒（prompt 已有，不要重复堆叠——
  contract 条目贵精不贵多，每条都应是 planner 不钉就会错的规则）。

## 工作流

1. **读源定页册**：从源材料提炼 N 页的 title（判断句）+ points（数字锚点）；
   盘点资源（用户图文件清单、AI 图需求、候选图标）。
2. **预置素材**：用户图（user 来源的语义 = "规划前已就位"）按契约清单的
   文件名复制进 `<output.dir>/images/`（`init_workspace` 的 `exist_ok`
   语义不清场）；图标 id 到 vendor 图标库逐个 `[ -f ]` 验证。
3. **零成本预检**：`validate_ppt_spec(spec)` + `build_generate_tasklist(spec)`
   展开检查（roster 差集、条件段、早门有无）——展开期错误不花一分钱 LLM 费。
4. **跑**：编程 API `run_generate(spec, persist=True)`（`llm_client` 缺省
   走 env 配置）——诊断轮必须开持久化，失败分析走审计 timeline + feed，
   不许"重跑到死"（见 AGENTS.md「Run & Debug Discipline」）。
5. **失败分析**：读 firings（或 `validation/` 报告），verdict issues 二分：
   带 `page` 键 → 页级，修复环应自愈（不自愈 = 查修复收据 failed 原因）；
   scope 级 → 回到 spec 契约补词表/资源约束，重跑。

## 运行实录（简要）

| 轮 | 死因 | 修的哪层 |
|---|---|---|
| 1 | PlanValidate：`## pNN` 页块全缺 + palette 锚点缺 | 模块 prompt（内建锚点契约） |
| 2 | IconSync：库外图标名硬失败 | spec 契约（钉图标池） |
| 3 | icon id 裸名不合法 | spec 契约（完整 `库/名字`） |
| 4 | 终门：图行 `Type: Source` ×15 + 文本溢出 | spec 契约（Type: Placed） |
| 5 | 终门：Status 词表混用（Pending/Ready） | spec 契约（§VIII 终态词表） |
| 6 | **通过**（早门修 2 页、终门兜住真缺页 p04 并自愈） | — |

第 6 轮同时实测了图接线缺口修复（早门 issues 分支批 2 照常展开）与页册
完整性安全网（planner 侧 p04 页节点瞬断失败，终门差集检出 → 修复环补生成
→ 复检通过才导出）。
