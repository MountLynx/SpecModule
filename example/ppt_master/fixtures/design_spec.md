<!-- ppt-master-schema: design-spec/v1 -->
# SpecModule vendor 验证 - Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Project Name | SpecModule vendor 验证 |
| Canvas Format | PPT 16:9 (1280×720) |
| Page Count | 1 |
| Primary Language | zh-CN |
| Target Audience | SpecModule 开发者 |
| Communication Intent | 验证 vendor 闭包四件套真实可用，先看通过证据，再看验证范围 |
| Desired Audience Outcome | 开发者确认 checker/finalize/export 管线在 vendor 目录真实跑通 |
| Core Message / Ask / Action | vendor 闭包可通过真实工具链完整验证 |
| Delivery Context | 开发者本地仓库验证，非对外演示 |
| Artifact Afterlife | 作为 Task 9 mock 页面输出的基线 fixture 长期保留 |
| Reading Mode | presentation |
| Content Strategy | 常规平衡 |
| Design Style | 极简验证页，无装饰 |
| AI Image Acquisition Path | not applicable |
| Generation Mode | continuous |
| Spec Refinement | disabled |
| Speaker Notes | disabled — fixture 明确禁用备注，export 使用 --no-notes |
| Custom Animations | disabled — workflow default（fixture 无动画需求） |
| Narration Audio | disabled — workflow default（fixture 无旁白需求） |
| Created Date | 2026-09-15 |

## II. Canvas Specification

| Property | Value |
| --- | --- |
| Format | PPT 16:9 |
| Dimensions | 1280 × 720 |
| viewBox | `0 0 1280 720` |
| Margins | 四边 80px 安全边距 |
| Content Area | 80,80 – 1200,640 |

## III. Visual Theme

### Theme Style

- **Mode**: custom
- **Visual style**: minimal-validation
- **Theme**: 中性工程验证风格，白底深字
- **Tone**: 克制、技术化

### Color Scheme

| Role | HEX | Purpose |
| --- | --- | --- |
| Background | #FFFFFF | 页面背景 |
| Secondary background | #F1F5F9 | 预留浅底 |
| Primary | #2563EB | 强调色（本页未用） |
| Accent | #F97316 | 次强调色（本页未用） |
| Secondary accent | #0EA5E9 | 预留 |
| Body text | #0F172A | 标题与正文主色 |
| Secondary text | #475569 | 要点次级文本 |
| Divider | #E2E8F0 | 预留分隔线 |

## IV. Typography System

### Font Plan

| Role | Character (Reference) | Primary | English if non-English | Fallback tail |
| --- | --- | --- | --- | --- |
| Title | 无衬线/几何 | Arial | Arial | sans-serif |
| Body | 无衬线/中性 | Arial | Arial | sans-serif |

- **Title stack**: Arial, sans-serif
- **Body stack**: Arial, sans-serif

### Font Size Hierarchy

| Purpose | Anchor Size (px) |
| --- | ---: |
| Body | 24 |
| Title | 44 |
| Subtitle | 32 |
| Annotation | 18 |

## V. Layout Principles

### Deck-wide Direction

- **Hierarchy direction**: 标题先行，要点自上而下依次阅读
- **Composition tendency**: 单栏左对齐，留白充分
- **Cross-page continuity**: 单页项目，无跨页复用
- **Spacing posture**: open
- **Spacing anchors**: 页边距 80px，块间距 40px，列间距 40px，圆角 0px，正文行距 50px

## VI. Icon Usage Specification

- **Primary bundled library**: none

| Icon Path | Suitable Scenarios |
| --- | --- |

## VII. Visualization Reference List

| Page | Family | Template | Usage |
| --- | --- | --- | --- |

## VIII. Image Resource List

| Filename | Dimensions | Ratio | Purpose | Type | Image pattern | Crop Policy | Acquire Via | Status | Reference | text_policy | page_role |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## IX. Content Outline

### Part 1: 验证

#### Slide 01 - vendor 闭包验证

- **Audience move**: 不确定 vendor 闭包是否完整可用 → 确认闭包检查与四件套验证均已真实通过
- **Relationships**: 标题与三条要点为总分关系，要点按验证顺序依次排列
- **Composition**: 标题居上，三条要点纵向排布于下方，单栏左对齐
- **Title**: SpecModule vendor 闭包验证
- **Core message**: vendor 闭包可通过真实工具链完整验证
- **Content**: 闭包检查脚本通过，入口脚本可加载 · checker、finalize、export 四件套真实跑通 · MIT 授权与 NOTICE 归属如实记录

## X. Speaker Notes Requirements

- **Generation**: disabled
