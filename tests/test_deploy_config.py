"""The committed deploy configuration names the image every stack runs, and probes it.

Reproductions from the /adversarial pass on adopting the image block: the env template resolved
an invalid image reference (`registry.example.com:5000/...:`) when TAG was left empty, the smoke
probe never imported the collector's real entry points (it looks for `app/*_main.py`), and only
one of the two dev stacks was deployed and smoked. These read the committed files the stacks and
`make dev-deploy` read, so a regression fails here rather than at the next deploy.
"""

import re
import subprocess
from pathlib import Path

import pytest

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


def _service(compose: str, name: str) -> str:
    return compose.split(f"  {name}:\n")[1].split("\n  sense_")[0]


def test_the_dev_stacks_run_on_the_declared_dev_host() -> None:
    """Every target that drives a dev stack must reach the dev host's docker, or it starts,
    stops or reads a stack on whichever machine runs make."""
    assert _make_setting("DEV_HOST") == ["devhost"]
    makefile = (REPO / "Makefile").read_text()
    assert re.search(
        r"^DEV_HOST_DC = DOCKER_HOST=ssh://\$\(DEV_HOST\) ", makefile, re.M
    )
    for wrapper in ("DEV_TYLE_DC", "DEV_BB_DC"):
        assert re.search(rf"^{wrapper}\s+= \$\(DEV_HOST_DC\) ", makefile, re.M), wrapper


def test_every_dev_target_resolves_the_stacks_as_dev_deploy_does() -> None:
    """dev-deploy resolves both stacks from DEV_ENV_FILE, whose TAG= it pins: a wrapper that
    resolves from another file starts another image and another Grafana password."""
    makefile = (REPO / "Makefile").read_text()
    # the image block's own default; the repo sets no DEV_ENV_FILE of its own
    deploy_env = re.findall(r"^DEV_ENV_FILE \?= (\S+)$", makefile, re.M)
    assert deploy_env == [".env.dev"], deploy_env
    for wrapper in ("DEV_TYLE_DC", "DEV_BB_DC"):
        line = re.search(rf"^{wrapper}\s+= (.+)$", makefile, re.M)
        assert line, wrapper
        assert re.findall(r"--env-file (\S+)", line[1]) == deploy_env, line[1]


def test_each_dev_grafana_is_served_by_name_on_the_edge_not_a_port() -> None:
    """The edge routes each dev name to a pinned address on `dev-edge` (it has no Docker DNS):
    dev-sense-collector to 172.30.0.13:3000, dev-sense-collector-bb to 172.30.0.14:3000. A
    Grafana on any other address answers 502 there, and a published port would bypass it."""
    compose = (REPO / "compose.yml").read_text()
    for stack, address in (
        ("sense_dev_tyle_grafana", "172.30.0.13"),
        ("sense_dev_bb_grafana", "172.30.0.14"),
    ):
        service = _service(compose, stack)
        assert "ports:" not in service, stack
        assert re.search(
            rf"^    networks:\n      default: \{{\}}\n      dev-edge:\n"
            rf"        ipv4_address: {re.escape(address)}$",
            service,
            re.M,
        ), stack
        assert f"container_name: {stack}" in service, stack
    assert re.search(r"^networks:\n  dev-edge:\n    external: true$", compose, re.M)


DEV_GRAFANA_ENTRYPOINT = REPO / "ops" / "grafana" / "dev-entrypoint.sh"


def test_each_dev_grafana_starts_through_the_password_guard() -> None:
    compose = (REPO / "compose.yml").read_text()
    for stack in ("sense_dev_tyle_grafana", "sense_dev_bb_grafana"):
        service = _service(compose, stack)
        # sh exits 0, silently, when its script is a directory: the shape docker gives a
        # missing bind source. Grafana must then fail loudly instead of restarting in silence.
        assert (
            'entrypoint: ["/bin/sh", "-c", "test -f /ops/dev-entrypoint.sh' in service
        ), stack
        assert "exit 1; }; exec /bin/sh /ops/dev-entrypoint.sh" in service, stack
        assert (
            "- ./ops/grafana/dev-entrypoint.sh:/ops/dev-entrypoint.sh:ro" in service
        ), stack
        assert "GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:-}" in service, stack


def _guard(tmp_path: Path, password: str, cli_exit: int = 0):
    """Run the dev Grafana entrypoint with a stand-in `grafana` that records its arguments
    (one call per line) and what it read on stdin (in `<tmp>/stdin`)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    cli = bin_dir / "grafana"
    cli.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\ncat > {tmp_path / "stdin"}\n'
        f"exit {cli_exit}\n"
    )
    cli.chmod(0o755)
    run = subprocess.run(
        ["/bin/sh", str(DEV_GRAFANA_ENTRYPOINT)],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "GF_SECURITY_ADMIN_PASSWORD": password,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return run, (calls.read_text().splitlines() if calls.exists() else [])


@pytest.mark.parametrize(
    "password",
    [
        "",
        "admin",
        "Admin",
        "ADMIN",
        "choose-a-password",
        "Choose-A-Password",
        # whitespace alone is no password
        "    ",
        "\t \t",
        # the CLI reads stdin's first line only: `admin\n…` would set `admin`
        "admin\nZq7-unrelated-suffix",
        "Kp9x\nW7-rest-of-it",
        "S3cret-pw\r",
    ],
)
def test_a_dev_grafana_refuses_a_default_or_published_password(tmp_path, password):
    """Served by name, a dev Grafana must never take a password anyone can guess: none,
    Grafana's default, or the placeholder this public repo's env template shows."""
    run, calls = _guard(tmp_path, password)
    assert run.returncode == 1
    assert "REFUSING" in run.stderr
    assert calls == []


@pytest.mark.parametrize(
    "password", ["S3cret pw with spaces*", "-Rk9vT2qLm8w", "--debug", "q\"$x`y'z"]
)
def test_a_dev_grafana_sets_its_admin_password_on_every_start(tmp_path, password):
    """Grafana applies GF_SECURITY_ADMIN_PASSWORD only when it creates its database: a
    volume first started with admin/admin, or before a rotation, keeps the old password
    unless the entrypoint sets it each start. It goes on stdin, never as an argument, so
    a password a generator starts with `-` is not read as a flag and no `ps` shows it."""
    run, calls = _guard(tmp_path, password)
    assert calls == [
        "cli --homepath /usr/share/grafana admin reset-admin-password"
        " --password-from-stdin"
    ]
    assert (tmp_path / "stdin").read_text() == password
    assert "REFUSING" not in run.stderr
    assert password not in run.stdout + run.stderr


def test_a_dev_grafana_does_not_start_when_its_password_cannot_be_set(tmp_path):
    run, calls = _guard(tmp_path, "S3cret-pw", cli_exit=1)
    assert run.returncode == 1
    assert "REFUSING" in run.stderr
    assert len(calls) == 1


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


def test_every_env_template_describes_a_file_a_stack_reads() -> None:
    """A template for an env file nothing reads sends operators to configure nothing."""
    readers = "".join(
        (REPO / name).read_text() for name in ("compose.yml", "Makefile")
    ) + "".join(p.read_text() for p in (REPO / "scripts").glob("*.sh"))
    for template in REPO.glob(".env.*.example"):
        target = template.name.removesuffix(".example")
        assert target in readers, f"{template.name}: nothing reads {target}"


def test_every_setting_in_the_env_template_is_read_by_the_collector() -> None:
    """A knob in .env.example that no code reads changes nothing when an operator tunes it."""
    source = "".join(p.read_text() for p in (REPO / "app").rglob("*.py"))
    template = (REPO / ".env.example").read_text()
    keys = set(re.findall(r"^#?\s*(SENSE_COLLECTOR_[A-Z0-9_]+)=", template, re.M))
    assert keys, "the template sets no SENSE_COLLECTOR_ keys"
    assert not {key for key in keys if key not in source}


def test_each_stack_binds_its_output_folder_through_its_own_variable() -> None:
    """The output folder holds the stack's heartbeat. Two collectors bound through one
    variable share a folder once it is set, and a live collector then keeps a dead one's
    healthcheck green."""
    binds = re.findall(
        r"^\s*-\s*\$\{([A-Z0-9_]+)(?::-([^}]*))?\}:/app/output\s*$",
        (REPO / "compose.yml").read_text(),
        re.MULTILINE,
    )
    assert len(binds) >= 2, binds
    names = [name for name, _ in binds]
    defaults = [default for _, default in binds]
    assert len(set(names)) == len(names), names
    assert len(set(defaults)) == len(defaults), defaults


def test_every_make_target_the_run_stacks_table_names_exists() -> None:
    """CLAUDE.md's run-stacks table is how an operator starts each stack; a target it names
    that the Makefile lacks fails at the moment someone deploys."""
    table = (
        (REPO / "CLAUDE.md").read_text().split("## The run stacks")[1].split("\n## ")[0]
    )
    named = set(re.findall(r"`make ([a-z][a-z0-9-]*)`", table))
    assert named, "the run-stacks table names no make target"
    defined = set(
        re.findall(r"^([a-z][a-z0-9-]*):", (REPO / "Makefile").read_text(), re.M)
    )
    assert named <= defined, sorted(named - defined)


def test_the_prod_bb_targets_drive_the_prod_bb_stack() -> None:
    makefile = (REPO / "Makefile").read_text()
    for target, verb in (
        ("prod-bb-up", "up -d"),
        ("prod-bb-down", "down"),
        ("prod-bb-logs", "logs -f"),
        ("prod-bb-ps", "ps"),
    ):
        recipe = re.search(rf"^{target}:.*\n((?:\t.*\n)+)", makefile, re.M)
        assert recipe, target
        assert f"$(PROD_BB_DC) {verb}" in recipe[1], (target, recipe[1])
