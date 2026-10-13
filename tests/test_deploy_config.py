"""The committed deploy configuration names the image every stack runs, and probes it.

Reproductions from the /adversarial pass on adopting the image block: the env template resolved
an invalid image reference (`registry.example.com:5000/...:`) when TAG was left empty, the smoke
probe never imported the collector's real entry points (it looks for `app/*_main.py`), and only
one of the two dev stacks was deployed and smoked. These read the committed files the stacks and
`make dev-deploy` read, so a regression fails here rather than at the next deploy.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _env_lines(name: str) -> list[str]:
    return [
        ln
        for ln in (REPO / name).read_text().splitlines()
        if ln and not ln.startswith("#")
    ]


def _make_setting(name: str) -> list[str]:
    match = re.search(
        rf"^{name}\s*:=\s*(.+)$", (REPO / "Makefile").read_text(), re.MULTILINE
    )
    assert match, f"{name} is not set with := in the Makefile"
    return match.group(1).split()


def test_template_image_refuses_an_empty_tag() -> None:
    lines = _env_lines(".env.example")
    image = next(ln for ln in lines if ln.startswith("SENSE_IMAGE="))
    assert "${TAG:?" in image, (
        "an empty TAG= must stop compose, not resolve an invalid reference"
    )


def test_template_tag_is_set_before_the_image_reads_it() -> None:
    keys = [ln.split("=", 1)[0] for ln in _env_lines(".env.example")]
    assert keys.index("TAG") < keys.index("SENSE_IMAGE"), (
        "compose reads an env file top to bottom"
    )


def test_template_pulls_from_the_public_registry() -> None:
    assert "REGISTRY=ghcr.io" in _env_lines(".env.example")


def test_smoke_imports_the_real_entry_points() -> None:
    modules = _make_setting("SMOKE_ENTRY_MODULES")
    assert {"app.main", "app.health.check"} <= set(modules)
    for module in modules:
        assert (REPO / (module.replace(".", "/") + ".py")).is_file(), module


def test_dev_deploy_deploys_and_smokes_both_monitors() -> None:
    assert _make_setting("DEV_PROFILES") == ["dev-tyle", "dev-bb"]
    assert _make_setting("SMOKE_CONTAINERS") == [
        "sense_dev_tyle_collector",
        "sense_dev_bb_collector",
    ]


def test_dev_grafanas_take_separate_port_variables() -> None:
    compose = (REPO / "compose.yml").read_text()
    assert '"${GRAFANA_PORT_TYLE:-13300}:3000"' in compose
    assert '"${GRAFANA_PORT_BB:-13301}:3000"' in compose
