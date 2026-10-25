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
    assert '"${GRAFANA_BIND:-127.0.0.1}:${GRAFANA_PORT_TYLE:-13300}:3000"' in compose
    assert '"${GRAFANA_BIND:-127.0.0.1}:${GRAFANA_PORT_BB:-13301}:3000"' in compose


def test_releases_build_both_architectures_whatever_the_shell_exports() -> None:
    # `?=` would yield to an exported PLATFORMS (even an empty one) and publish host-only.
    assert _make_setting("PLATFORMS") == ["linux/amd64,linux/arm64"]


def test_every_published_grafana_port_binds_to_loopback_by_default() -> None:
    """A bare `port:3000` listens on every interface, so admin/admin is open to the LAN."""
    compose = (REPO / "compose.yml").read_text()
    published = re.findall(r'^\s+- "(.*:3000)"$', compose, re.MULTILINE)
    assert published, "no Grafana port found"
    for mapping in published:
        assert mapping.startswith("${GRAFANA_BIND:-127.0.0.1}:"), mapping


def test_the_provisioned_datasource_cannot_be_repointed_from_the_ui() -> None:
    """It carries the InfluxDB admin token; an editable URL sends that token anywhere."""
    datasource = (
        REPO / "grafana" / "provisioning" / "datasources" / "influxdb.yml"
    ).read_text()
    assert re.search(r"^\s+editable: false$", datasource, re.MULTILINE)


def test_the_demo_runs_in_its_own_compose_project() -> None:
    """Sharing the dev stacks' project puts the demo on the network of their InfluxDBs."""
    demo = _make_setting("DEMO_DC")
    assert "-p" in demo
    assert demo[demo.index("-p") + 1] != "sense-collector"


def test_each_grafana_reaches_its_own_stacks_influxdb() -> None:
    """The provisioned datasource must name a host that exists on the stack's network."""
    datasource = (
        REPO / "grafana" / "provisioning" / "datasources" / "influxdb.yml"
    ).read_text()
    assert "url: http://${INFLUX_HOST}:8086" in datasource

    compose = (REPO / "compose.yml").read_text()
    for stack in ("dev_tyle", "dev_bb", "demo"):
        service = compose.split(f"  sense_{stack}_grafana:")[1].split("\n  sense_")[0]
        assert f"INFLUX_HOST: sense_{stack}_influxdb" in service, stack
        assert f"  sense_{stack}_influxdb:" in compose


def test_grafana_never_holds_the_influxdb_admin_token() -> None:
    """Grafana's datasource proxy forwards any path with the datasource's credentials, so
    every Grafana user (Viewers too) can use them. They must be read-only."""
    datasource = (
        REPO / "grafana" / "provisioning" / "datasources" / "influxdb.yml"
    ).read_text()
    assert "INFLUX_TOKEN" not in datasource
    assert "user: grafana" in datasource
    assert "password: ${GRAFANA_INFLUX_PASSWORD}" in datasource

    compose = (REPO / "compose.yml").read_text()
    grafana_env = compose.split("x-grafana: &grafana")[1].split("\nx-")[0]
    assert "INFLUX_TOKEN" not in grafana_env
    assert (
        "GRAFANA_INFLUX_PASSWORD" in compose.split("x-influx-init:")[1].split("\nx-")[0]
    )

    init = (REPO / "ops" / "influxdb" / "init-dbrp.sh").read_text()
    assert "influx v1 auth create" in init
    assert "--read-bucket" in init
    assert "--write-bucket" not in init


def test_the_fake_sense_has_no_fixed_container_name() -> None:
    """The demo and e2e projects both run it; one fixed name makes them collide."""
    compose = (REPO / "compose.yml").read_text()
    fake = compose.split("  sense_fake:\n")[1].split("\n  # ")[0]
    assert "container_name" not in fake


def test_the_runtime_user_cannot_rewrite_the_app_code() -> None:
    """Only /app/output is the collector's to write: code owned by appuser lets anything
    running as it plant a change that survives restarts of the same container."""
    dockerfile = (REPO / "Dockerfile").read_text()
    production = dockerfile.split(" AS production")[1].split("\nFROM ")[0]
    assert "COPY --chown=appuser:appuser app /app/app" not in production
    assert "COPY app /app/app" in production
    assert not re.search(r"chown -R \S+ /app\b(?!/)", production)
    assert "chown appuser:appuser /app/output" in production


def test_the_app_code_is_readable_whatever_the_checkouts_umask() -> None:
    """COPY keeps the build context's modes: from a umask-077 checkout appuser could not
    import the root-owned code, and the image would not start."""
    dockerfile = (REPO / "Dockerfile").read_text()
    production = dockerfile.split(" AS production")[1].split("\nFROM ")[0]
    copy = production.index("COPY app /app/app")
    assert re.search(r"RUN chmod -R a\+rX,go-w /app/app\b", production[copy:])


def test_throwaway_stacks_tear_down_in_their_own_compose_project() -> None:
    """`down -v` removes the project's volumes. In compose.yml's project, the one the dev
    stacks run in, a stopped dev stack's InfluxDB volume would go with the e2e teardown."""
    project = re.search(r"^name:\s*(\S+)", (REPO / "compose.yml").read_text(), re.M)[1]
    e2e = (REPO / "scripts" / "e2e-test.sh").read_text()
    dc = re.search(r'^DC="([^"]+)"', e2e, re.M)[1]
    own = re.search(r"(?:-p|--project-name)\s+(\S+)", dc)
    assert own and own[1] != project, dc
