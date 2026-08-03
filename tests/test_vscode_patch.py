from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codex_shim import cli


def _picker_bundle(
    additional: str = "e",
    auth: str = "t",
    available: str = "n",
    model: str = "r",
    hidden: str = "i",
) -> str:
    return (
        "prefix "
        f"function CVe({{additionalAvailableModels:{additional},authMethod:{auth},"
        f"availableModels:{available},model:{model},useHiddenModels:{hidden}}}){{"
        f"return {additional}?.has({model}.model)===!0||"
        "("
        f"{hidden}&&{auth}!==`amazonBedrock`?{available}.has({model}.model):!{model}.hidden"
        ")}"
        " suffix"
    )


def _extension(root: Path, version: str, *, bundle: str | None = None) -> Path:
    path = root / f"openai.chatgpt-{version}-linux-x64"
    assets = path / "webview" / "assets"
    assets.mkdir(parents=True)
    (path / "package.json").write_text(
        json.dumps({"publisher": "openai", "name": "chatgpt", "version": version})
    )
    (assets / "app-initial-test.js").write_text(bundle if bundle is not None else _picker_bundle())
    return path


@pytest.fixture
def vscode_environment(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    local = tmp_path / ".vscode" / "extensions"
    server = tmp_path / ".vscode-server" / "extensions"
    insiders = tmp_path / ".vscode-insiders" / "extensions"
    cursor = tmp_path / ".cursor" / "extensions"
    monkeypatch.setattr(cli, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(
        cli,
        "_vscode_extension_roots",
        lambda: [
            ("VS Code", local),
            ("VS Code Server", server),
            ("VS Code Insiders", insiders),
            ("Cursor", cursor),
        ],
    )
    return runtime, local, server, insiders, cursor


def test_discovery_sorts_newest_and_distinguishes_locations(vscode_environment):
    _, local, server, insiders, cursor = vscode_environment
    _extension(local, "26.700.1")
    _extension(server, "26.727.40816")
    _extension(insiders, "26.710.1")
    _extension(cursor, "26.720.1")

    found = cli.discover_vscode_extensions()

    assert [item.version for item in found] == ["26.727.40816", "26.720.1", "26.710.1", "26.700.1"]
    assert {item.location for item in found} == {"VS Code", "VS Code Server", "VS Code Insiders", "Cursor"}
    assert all(item.status == "unpatched" for item in found)


def test_patch_creates_manifest_and_is_idempotent(vscode_environment):
    runtime, _, server, _, _ = vscode_environment
    extension = _extension(server, "26.727.40816")
    bundle = next(extension.glob("webview/assets/app-initial-*.js"))
    original = bundle.read_bytes()

    assert cli.patch_vscode_extensions([extension]) == 0
    patched = bundle.read_bytes()
    assert patched != original
    assert cli.VSCODE_PICKER_MARKER.encode() in patched

    manifests = list((runtime / cli.VSCODE_BACKUP_DIR_NAME).glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["extension_path"] == str(extension.resolve())
    assert manifest["extension_version"] == "26.727.40816"
    assert manifest["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["patched_sha256"] == hashlib.sha256(patched).hexdigest()

    assert cli.patch_vscode_extensions([extension]) == 0
    assert bundle.read_bytes() == patched
    assert len(list((runtime / cli.VSCODE_BACKUP_DIR_NAME).glob("*/manifest.json"))) == 1


def test_patch_handles_renamed_minifier_variables(vscode_environment):
    _, local, _, _, _ = vscode_environment
    extension = _extension(
        local,
        "26.1",
        bundle=_picker_bundle("extra", "kind", "allowed", "model", "useHidden"),
    )

    assert cli.patch_vscode_extensions([extension]) == 0

    text = next(extension.glob("webview/assets/app-initial-*.js")).read_text()
    assert "||(/*codex-shim:vscode-model-picker*/!model.hidden)}" in text


def test_unsupported_or_ambiguous_bundle_is_not_modified(vscode_environment):
    _, local, _, _, _ = vscode_environment
    unknown = _extension(local, "26.1", bundle="different build")
    ambiguous_text = _picker_bundle() + _picker_bundle("a", "b", "c", "d", "f")
    ambiguous = _extension(local, "26.2", bundle=ambiguous_text)

    assert cli.patch_vscode_extensions([unknown]) == 1
    assert cli.patch_vscode_extensions([ambiguous]) == 1
    assert next(unknown.glob("webview/assets/app-initial-*.js")).read_text() == "different build"
    assert next(ambiguous.glob("webview/assets/app-initial-*.js")).read_text() == ambiguous_text


def test_restore_requires_exact_patched_hash(vscode_environment):
    _, _, server, _, _ = vscode_environment
    extension = _extension(server, "26.727.40816")
    bundle = next(extension.glob("webview/assets/app-initial-*.js"))
    original = bundle.read_bytes()
    assert cli.patch_vscode_extensions([extension]) == 0

    assert cli.restore_vscode_extensions([extension]) == 0
    assert bundle.read_bytes() == original

    assert cli.patch_vscode_extensions([extension]) == 0
    bundle.write_text(bundle.read_text() + " changed")
    changed = bundle.read_bytes()
    assert cli.restore_vscode_extensions([extension]) == 1
    assert bundle.read_bytes() == changed


def test_new_version_is_unpatched_while_old_version_stays_patched(vscode_environment):
    _, _, server, _, _ = vscode_environment
    old = _extension(server, "26.727.40816")
    assert cli.patch_vscode_extensions([old]) == 0
    new = _extension(server, "26.801.10000")

    found = cli.discover_vscode_extensions()

    assert [(item.version, item.status) for item in found] == [
        ("26.801.10000", "unpatched"),
        ("26.727.40816", "patched"),
    ]


def test_legacy_ineffective_patch_is_detected_and_can_be_restored(vscode_environment):
    _, _, server, _, _ = vscode_environment
    extension = _extension(server, "26.727.40816")
    bundle = next(extension.glob("webview/assets/app-initial-*.js"))
    original = bundle.read_bytes()
    legacy = b"return n.filter(e=>/*codex-shim:model-picker*/!e.hidden)" + original
    original_hash = hashlib.sha256(original).hexdigest()
    legacy_hash = hashlib.sha256(legacy).hexdigest()
    inspected = cli._inspect_vscode_extension(extension)
    cli._write_vscode_backup(inspected, original, original_hash, legacy_hash)
    bundle.write_bytes(legacy)

    assert cli._inspect_vscode_extension(extension).status == "legacy-patched"
    assert cli.patch_vscode_extensions([extension]) == 1
    assert cli.restore_vscode_extensions([extension]) == 0
    assert bundle.read_bytes() == original


def test_interactive_defaults_to_newest_unpatched(vscode_environment, monkeypatch):
    _, _, server, _, _ = vscode_environment
    old = _extension(server, "26.727.40816")
    assert cli.patch_vscode_extensions([old]) == 0
    new = _extension(server, "26.801.10000")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert cli.patch_vscode_extensions() == 0

    assert cli._inspect_vscode_extension(new).status == "patched"


@pytest.mark.parametrize(
    ("selection", "expected"),
    [("1,3", [1, 3]), ("1-3", [1, 2, 3]), ("2,4-5", [2, 4, 5])],
)
def test_number_selection(selection, expected):
    assert cli._parse_number_selection(selection, 5) == expected


def test_number_selection_rejects_invalid_values():
    with pytest.raises(ValueError):
        cli._parse_number_selection("0", 3)
    with pytest.raises(ValueError):
        cli._parse_number_selection("3-1", 3)


def test_cli_all_flag_dispatches(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "patch_vscode_extensions", lambda paths, all_extensions: calls.append((paths, all_extensions)) or 0)

    assert cli.main(["patch-vscode", "--all"]) == 0
    assert calls == [(None, True)]
