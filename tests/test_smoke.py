"""Repository smoke tests for the standalone plugin package."""

from __future__ import annotations

import pathlib
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    return tomllib.loads((_ROOT / "plugin.toml").read_text(encoding="utf-8"))


def test_plugin_manifest_declares_expected_entrypoint_and_runtime():
    manifest = _manifest()

    assert manifest["plugin"]["id"] == "neko_terraria"
    # 用户安装场景下宿主 normalize 会把 plugin.plugins. 前缀改写为 plugins.；
    # 本插件直接声明 plugins. 前缀（实测 plugin.plugins. 前缀在本宿主下导入失败）
    assert manifest["plugin"]["entry"] == "plugins.neko_terraria:NTerrariaPlugin"
    assert manifest["plugin_runtime"]["enabled"] is True


def test_plugin_manifest_declares_hosted_ui_surface_and_files_exist():
    manifest = _manifest()

    assert manifest["plugin"]["ui"]["enabled"] is True
    for panel in manifest["plugin"]["ui"]["panel"]:
        entry = _ROOT / panel["entry"]
        assert entry.exists(), f"UI entry missing: {panel['entry']}"


def test_plugin_source_modules_compile():
    import ast

    for py in sorted((_ROOT / "bridge").glob("*.py")) + sorted((_ROOT / "autonomous").glob("*.py")):
        ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
