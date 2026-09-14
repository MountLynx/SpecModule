import pytest

from module_harness.cli.command import CommandConfig
from module_harness.core.config import HarnessConfig
from module_harness.core.outputfmt import OutputFormat


class TestHarnessConfig:
    def test_minimal_config(self):
        cfg = HarnessConfig(prompt_core="你是翻译助手。")
        assert cfg.prompt_core == "你是翻译助手。"
        assert cfg.prompt_modes == {}
        assert cfg.output_format is None
        assert cfg.notdo == []
        assert cfg.model is None
        assert cfg.temperature is None
        assert cfg.think is None

    def test_full_config(self):
        fmt = OutputFormat(type="json_object")
        cfg = HarnessConfig(
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式风格", "casual": "随意风格"},
            output_format=fmt,
            notdo=["不要直译", "不要添加解释"],
            model="claude-sonnet-4-6",
            temperature=0.3,
            think=True,
        )
        assert cfg.prompt_modes["formal"] == "正式风格"
        assert cfg.output_format == fmt
        assert "不要直译" in cfg.notdo
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.temperature == 0.3
        assert cfg.think is True

    def test_default_factories_are_independent(self):
        a = HarnessConfig(prompt_core="A")
        b = HarnessConfig(prompt_core="B")
        a.notdo.append("不要做X")
        assert b.notdo == []  # 不共享

    def test_from_task_definition_basic(self):
        task = {
            "prompt_core": "你是助手",
            "prompt_modes": {"short": "简短回答"},
            "notdo": ["不要啰嗦"],
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.prompt_core == "你是助手"
        assert cfg.prompt_modes == {"short": "简短回答"}
        assert cfg.notdo == ["不要啰嗦"]

    def test_from_task_definition_with_output_format(self):
        task = {
            "prompt_core": "分析文本",
            "outputformat": {"type": "json_object"},
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.output_format is not None
        assert cfg.output_format.type == "json_object"

    def test_from_task_definition_with_schema(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        task = {
            "prompt_core": "提取信息",
            "outputformat": {"type": "json_schema", "schema": schema},
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.output_format.type == "json_schema"
        assert cfg.output_format.schema == schema

    def test_from_task_definition_model_override(self):
        task = {
            "prompt_core": "x",
            "model": "gpt-4o",
            "temperature": 0.1,
        }
        cfg = HarnessConfig.from_task_definition(task)
        assert cfg.model == "gpt-4o"
        assert cfg.temperature == 0.1


class TestSerialization:
    def test_harness_config_roundtrip(self):
        cfg = HarnessConfig(
            name="translate",
            prompt_core="翻译：{text}",
            prompt_modes={"formal": "正式", "casual": "随意"},
            output_format=OutputFormat(type="json_object"),
            notdo=["不要加解释"],
            model="deepseek-v4-flash",
            temperature=0.3,
            think=True,
            api_params={"extra": {"k": "v"}},
        )
        restored = HarnessConfig.from_dict(cfg.to_dict())
        assert restored == cfg

    def test_harness_config_roundtrip_no_output_format(self):
        cfg = HarnessConfig(prompt_core="x")
        assert HarnessConfig.from_dict(cfg.to_dict()) == cfg

    def test_command_config_roundtrip(self):
        cfg = CommandConfig(
            name="ls", command="ls -la", timeout=30, cwd="/tmp",
            env={"A": "1"}, capture_output=False, shell=False,
        )
        assert CommandConfig.from_dict(cfg.to_dict()) == cfg


class TestImageModeConfig:
    """mode="image" 配置：字段默认、非法值与 output_format 互斥。"""

    def test_defaults(self):
        cfg = HarnessConfig(prompt_core="x")
        assert cfg.mode == "text"
        assert cfg.image_size is None
        assert cfg.image_dir == "images"

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="mode"):
            HarnessConfig(prompt_core="x", mode="video")

    def test_image_mode_rejects_output_format(self):
        with pytest.raises(ValueError, match="互斥"):
            HarnessConfig(
                prompt_core="x",
                mode="image",
                output_format=OutputFormat(type="json_object"),
            )

    def test_image_mode_without_output_format_ok(self):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image")
        assert cfg.mode == "image"

    def test_from_task_definition_reads_image_fields(self):
        cfg = HarnessConfig.from_task_definition({
            "prompt_core": "画:{title}",
            "mode": "image",
            "image_size": "1024x1024",
            "image_dir": "out/imgs",
        })
        assert cfg.mode == "image"
        assert cfg.image_size == "1024x1024"
        assert cfg.image_dir == "out/imgs"

    def test_from_task_definition_image_rejects_outputformat(self):
        with pytest.raises(ValueError, match="互斥"):
            HarnessConfig.from_task_definition({
                "prompt_core": "画:{title}",
                "mode": "image",
                "outputformat": {"type": "json_object"},
            })

    def test_to_dict_from_dict_roundtrip(self):
        cfg = HarnessConfig(prompt_core="画:{title}", mode="image",
                            image_size="1024x1024", image_dir="d")
        data = cfg.to_dict()
        assert data["mode"] == "image"
        restored = HarnessConfig.from_dict(data)
        assert restored == cfg

    def test_image_mode_rejects_null_image_dir(self):
        with pytest.raises(ValueError, match="image_dir"):
            HarnessConfig(prompt_core="x", mode="image", image_dir=None)
