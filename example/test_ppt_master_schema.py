"""spec 校验：必备字段、页册形状、枚举值、缺省回填（含字段路径报错）。"""

from __future__ import annotations

import pytest

from example.ppt_master.spec_schema import validate_ppt_spec


def _base_spec() -> dict:
    return {
        "project": "demo",
        "source": {"kind": "files", "paths": ["paper.md"]},
        "roster": [
            {"id": "p01", "title": "封面", "role": "cover"},
            {"id": "p02", "title": "背景", "points": ["a"]},
        ],
    }


def test_minimal_spec_passes_with_defaults():
    spec = _base_spec()
    validate_ppt_spec(spec)  # 不抛
    assert spec["reading_mode"] == "balanced"
    assert spec["output"]["dir"] == "projects/demo"
    assert spec["production"]["speaker_notes"] is True
    assert spec["roster"][1]["role"] == "content"


def test_missing_roster_raises_with_field_path():
    spec = _base_spec(); del spec["roster"]
    with pytest.raises(ValueError, match="roster"):
        validate_ppt_spec(spec)


def test_duplicate_page_id_rejected():
    spec = _base_spec()
    spec["roster"].append({"id": "p01", "title": "重复"})
    with pytest.raises(ValueError, match="p01"):
        validate_ppt_spec(spec)


def test_bad_role_rejected():
    spec = _base_spec()
    spec["roster"][0]["role"] = "hero"
    with pytest.raises(ValueError, match="role"):
        validate_ppt_spec(spec)


def test_source_kind_topic_requires_topic():
    spec = _base_spec()
    spec["source"] = {"kind": "topic"}
    with pytest.raises(ValueError, match="source.topic"):
        validate_ppt_spec(spec)


def test_bad_reading_mode_rejected():
    spec = _base_spec()
    spec["reading_mode"] = "dense"
    with pytest.raises(ValueError, match="reading_mode"):
        validate_ppt_spec(spec)


def test_bad_image_source_rejected():
    spec = _base_spec()
    spec["images"] = {"sources": ["ai", "magic"]}
    with pytest.raises(ValueError, match="images.sources"):
        validate_ppt_spec(spec)


def test_images_none_conflict_rejected():
    spec = _base_spec()
    spec["images"] = {"sources": ["none", "ai"]}
    with pytest.raises(ValueError, match="none"):
        validate_ppt_spec(spec)
