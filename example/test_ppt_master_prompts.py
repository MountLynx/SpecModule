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


def test_configs_shape():
    for cfg in (pc.plan_config(), pc.page_config(), pc.repair_config(),
                pc.notes_config(), pc.research_config()):
        assert cfg.prompt_core
        assert cfg.mode == "text"
        assert cfg.prompt_modes == {}  # v1 层 2 预留
    # 图像配置：image 模式互斥断言
    img = pc.image_config(image_dir="x")
    assert img.mode == "image" and img.output_format is None
