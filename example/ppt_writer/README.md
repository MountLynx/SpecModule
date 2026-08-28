# example/ppt_writer — spec → 可机器校验的 .pptx（M2 实践线）

把一个描述**每页内容与布局**的结构化 spec 生成一份可用的 .pptx 文件，并提供
基于 pptx 模板资产的**制作工作流**（审查 → 入库）。渲染路径零 LLM
（`--mock` 冒烟可用），LLM 只出现在制作工作流的意见节点。

## 双模板

| 模板 | 用途 | 流程 |
|---|---|---|
| `ppt_render`（默认） | 完整内容 spec → .pptx | 翻译器（spec → 归一化 pages_final → 信封）→ command 渲染节点（python-pptx 子进程）→ Report |
| `template_review` | 草稿 .pptx → 硬合规 → LLM 意见 → 合规入库 | Dump（结构化描述）→ Verify（硬合规，与渲染器同判据）→ Opinion（LLM）→ guard：不合规 → 问题清单 + 零写入；合规 → 复制入库 reference/ + manifest 注册 |

## spec 契约（ppt_render）

```json
{
  "title": "演示",
  "output": "deck.pptx",
  "theme": {"font": "微软雅黑"},
  "sections": [
    {"id": "intro", "title": "引言",
     "defaults": {"layout": "content", "content": {"points": ["section 默认要点"]}}}
  ],
  "pages": [
    {"index": 1, "section": "intro", "title": "标题页", "layout": "title"},
    {"index": 2, "section": "intro", "title": "要点页",
     "content": {"points": ["要点一", {"text": "子要点", "level": 1}],
                 "image": "fig.png", "caption": "图片语义注释"}}
  ]
}
```

- `layout`：`title` / `section` / `content` / `picture` / `thanks`（内置版式）或
  `template:<逻辑模板名>`（manifest 注册的自定义模板）
- 归一化规则（纯函数，冲突消解不报错）：page 显式 > section 默认（`defaults`）>
  模板兜底；page 未写 title 取 section 标题，未写 layout 取 section 默认
  （再兜底 `content`）；内容字段逐项覆盖，未给的内容**不填充**（版式占位符
  默认文本原样呈现）
- 非法 spec（缺 index / 未知 section / 未知 layout / 类型错误）在翻译器入口抛
  **含字段路径**的明确错误
- `output` 缺省 `ppt_writer_output.pptx`（相对运行目录）

## 模板资产：reference/ + manifest.json

`example/ppt_writer/reference/` 是模板池，`manifest.json` 显式映射：

```json
{
  "content": {"file": "base.pptx", "layout": 1, "kind": "content"},
  "cover":   {"file": "base.pptx", "layout": 8, "kind": "picture"}
}
```

- 条目 = 逻辑模板名 → {file（相对 reference/，可省略 = 内置默认模板）,
  layout（版式索引）, kind（title/section/content/picture/thanks，缺省按名推断）,
  master?（母版索引，缺省 0 = 第一个母版）}
- **多母版模板**：PowerPoint 模板可含多套母版（如一套主题的两个变体），
  每个母版自带一套版式（可能同名，如两个"节标题"）。寻址用
  `master` + `layout` 两段索引——dump 输出**全部母版**的版式，审查入库时
  指定 `master`（review spec 的 `master` 字段），manifest 记录后渲染即按
  母版索引取版式。同一文件的多个母版可同 deck 混用（`add_slide` 接受任意
  母版的版式）
- **默认组织约定（Open Question 定案）**：审查入库的默认形态 = **一个逻辑模板
  一个文件**（每个入库草稿独立 .pptx）；完整主题模板（多版式成套）用
  **单文件多版式**——同一文件被多个逻辑名引用（如本模块 `base.pptx` 同时承载
  title/section/content/picture/thanks 五个条目，一份文件即可支撑整份 deck）
- 内置默认模板池（无 manifest 条目时的兜底）：python-pptx 内置 11 版式——
  title→Title Slide(0)、section→Section Header(2)、content→Title and Content(1)、
  picture→Picture with Caption(8)、thanks→Title Only(5)
- **一个 deck 只能用一个模板文件**：任一页落到文件模板后，其余页要么指向同一
  文件，要么在 manifest 里映射到该文件（渲染前 fail-fast 报错，不清不楚的
  内置/文件混合被拒）
- **模板文件自带示例页会被清空**：只复用版式/主题（Slide Layouts / Theme），
  输出 .pptx 不含模板自身的示例 slide——模板作者可保留示例内容当参考

## 占位符命名约定

渲染器**按名**找占位符（模板作者在 PowerPoint 里重命名），未命名时按类型兜底：

| 角色 | 命名 | 类型兜底 | 填充方式 |
|---|---|---|---|
| 标题 | `title` | TITLE / CENTER_TITLE | 单段文本 |
| 要点 | `points` | BODY / OBJECT | 多段（clear + add_paragraph + level） |
| 图片 | `image` | PICTURE | `insert_picture`（按占位符尺寸放入） |
| 图片注释 | `caption` | TEXT / SUBTITLE / 其余文本占位符 | 单段文本 |

名称匹配但类型不符 → 硬合规问题（渲染前 fail-fast）；未匹配的占位符**不填充**
（版式默认文本兜底）。

## 模板制作工作流（两段式）

1. 作者在 PowerPoint 里做好 .pptx 草稿（版式占位符按上表命名）；
2. 跑 `template_review`（spec：`{draft_pptx, template_name, layout, kind, master?}`）：
   - **形态诊断**（advisory，随 Verify/Reject/Register 输出）：检测**页面层
     手工设计**（自带 slide 无占位符、全为手工形状/图片/文本框）并逐页列出
     对象构成与版式归属——这类页面的样式不在版式层，渲染不会复现；同时给
     出**未注册页面清单**（自带页及其版式）与**简略修正方法**（把设计迁到
     「视图 → 幻灯片母版」的版式层后重新审查入库）；
   - 硬合规失败 → 输出**逐项问题清单**并中止（reference/ 与 manifest 零变更）；
     作者按清单修改草稿后重跑（幂等）；
   - 合规通过 → LLM 意见（可打磨建议）→ **复制**（不改源草稿）入库
     `reference/<template_name>.pptx` + manifest 注册；
3. spec 里用 `layout: "template:<template_name>"`（或把内置 kind 名映射到该文件）
   即可在渲染时使用。文件模板渲染结果的 `warnings` 同样携带形态诊断。

多母版模板的第二个同名版式（如第二个"节标题"）用 `master: 1` 指定：
同一文件、不同母版索引，注册为独立逻辑名（如 `section_alt`），spec 页用
`layout: "template:section_alt"` 引用。

## 边界（v1 明确不做）

- **不填充** SmartArt / 图表 / 表格——版式含这些对象会被硬合规**拒绝**
  （保留为装饰的例外不存在，制作工作流保证池内模板无此类对象）
- **不处理**网络图片、自由描述性布局（未知 layout 直接报错）、材料→内容生成
  （stage 2）
- **图片适配粗糙**：`insert_picture` 按图片占位符尺寸放入，宽高比不匹配时
  拉伸/留白由 PowerPoint 呈现，精细适配不在 v1
- **中文字体**：渲染器对已填充文本显式设置中文字体（latin + eastAsia
  双 typeface，默认"微软雅黑"，`theme.font` 可覆盖）——模板未声明中文字体时
  避免打开时替换

## 运行

```bash
# 免 key 冒烟（默认 3 页 spec，产出 ppt_writer_output.pptx）
python -m module_harness.cli run --module ppt_writer --mock \
  --modules-dir example/modules

# 指定 spec
python -m module_harness.cli run --module ppt_writer --mock \
  --modules-dir example/modules --spec-file my_deck.json

# 制作工作流（--mock 下意见节点为占位文本，不阻断合规入库）
python -m module_harness.cli run --module ppt_writer --template template_review \
  --mock --modules-dir example/modules --spec-file review.json
```

## 测试

```bash
python -m pytest example/test_ppt_normalize.py example/test_ppt_render.py \
  example/test_ppt_cli.py example/test_ppt_workflow.py example/test_ppt_entry.py -q
```
