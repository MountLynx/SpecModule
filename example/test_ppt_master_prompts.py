"""参考包组装：文件存在、包内含关键锚点、HarnessConfig 形状。"""

from __future__ import annotations

from example.ppt_master import prompts_config as pc


def test_prompt_assets_present():
    for name in pc.REQUIRED_ASSETS:
        assert (pc.PROMPTS_DIR / name).is_file(), name


def test_plan_pack_contains_anchors():
    pack = pc.plan_prompt_pack()
    assert "design_spec" in pack and "spec_lock" in pack
    assert "SpecModule 改编声明" in pack


def test_page_pack_contains_vocab():
    pack = pc.page_prompt_pack()
    assert "SVG" in pack
    assert len(pack) > 5000  # 词表 + 契约非占位


def test_plan_prompt_declares_module_validations():
    """PlanValidate 机检契约必须写进 plan prompt：`## <页id>` 页块（含
    Audience move）+ lock 的 palette/typography 锚点。

    参考包教的是上游格式（`#### Slide NN`、`## colors`），不含模块私有
    机检要求——真实 LLM 首跑实测：九个页块全缺 + palette 锚点缺，
    PlanValidate 即 infrastructure 停图。"""
    core = pc.plan_config().prompt_core
    assert "`## <页id>`" in core
    assert "Audience move" in core
    assert "palette" in core and "typography" in core


def test_plan_prompt_pins_lock_theme_contract():
    """lock 主题契约（终门 checker 实测要求）必须钉进 plan prompt。

    真实 LLM 二跑实测：`## colors` 节随机缺失——该缺失是 scope 级
    blocking，repair 只重写页 SVG 修不了锁文件，必然烧完修复轮后
    infrastructure 停图。行格式 = checker 主题加载器逐字解析的形状。"""
    core = pc.plan_config().prompt_core
    assert "`## colors`" in core and "primary" in core
    assert "font_family" in core and "title_family" in core and "body_family" in core


def test_configs_shape():
    expected = {"ppt_plan": "json_object", "ppt_research": "json_object",
                "ppt_page": "text", "ppt_repair": "text", "ppt_notes": "text"}
    for cfg in (pc.plan_config(), pc.page_config(), pc.repair_config(),
                pc.notes_config(), pc.research_config()):
        assert cfg.prompt_core
        assert cfg.mode == "text"
        assert cfg.prompt_modes == {}  # v1 层 2 预留
        assert cfg.output_format.type == expected[cfg.name]
    # 图像配置：image 模式互斥断言 + 必须路由到唯一注册的生图模型
    # （model 缺省会路由到默认文本 provider——无 images API，图像行必炸）
    img = pc.image_config(image_dir="x")
    assert img.mode == "image" and img.output_format is None
    assert img.model == "gpt-image-1-mini"
    assert img.image_size == "1536x1024"
