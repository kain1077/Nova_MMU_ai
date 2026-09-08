"""
Tests for the packaging layer -- the installer, launcher and doctor.

All PURE. Nothing here starts a container, touches a real MCP client config, or
needs Docker: every test works on a throwaway tmp_path. That matters more here
than elsewhere in the suite, because the code under test is the code that edits
the user's chat-client configuration and generates the password protecting their
graph. A test that reached the real ones would be doing the exact thing the code
is written to avoid.

    pytest tests/test_packaging.py -v
"""

import json
import os
import sys

import pytest

# The packaging code is not installed as a package; it lives in packaging/.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "packaging"))

from mmu_cli import embedding, envfile, mcpconfig, payload  # noqa: E402


# ── .env generation ──────────────────────────────────

TEMPLATE = """\
# A comment that must survive.
NEO4J_PASS=change-me

# Another explanatory paragraph.
MMU_EMBEDDING_DIM=768
MMU_BIND=127.0.0.1
"""


def test_render_replaces_values_in_place():
    out = envfile.render(TEMPLATE, {"NEO4J_PASS": "hunter2xyz", "MMU_EMBEDDING_DIM": "1024"})
    values = envfile.parse(out)
    assert values["NEO4J_PASS"] == "hunter2xyz"
    assert values["MMU_EMBEDDING_DIM"] == "1024"


def test_render_preserves_comments():
    """The template is the documentation; generating a bare key=value file loses it."""
    out = envfile.render(TEMPLATE, {"NEO4J_PASS": "hunter2xyz"})
    assert "# A comment that must survive." in out
    assert "# Another explanatory paragraph." in out


def test_render_leaves_untouched_keys_alone():
    out = envfile.render(TEMPLATE, {"NEO4J_PASS": "hunter2xyz"})
    assert envfile.parse(out)["MMU_BIND"] == "127.0.0.1"


def test_render_appends_keys_absent_from_the_template():
    out = envfile.render(TEMPLATE, {"MMU_NEW_SETTING": "yes"})
    assert envfile.parse(out)["MMU_NEW_SETTING"] == "yes"


def test_generated_password_has_no_dollar_sign():
    """
    docker compose expands ${...} using values from .env, so a '$' inside the
    password reaches the container mangled -- and presents as a wrong password
    against a .env that looks correct.
    """
    for _ in range(200):
        assert "$" not in envfile.generate_password()


def test_generated_password_clears_neo4j_minimum():
    assert len(envfile.generate_password()) >= 8
    assert envfile.needs_attention({"NEO4J_PASS": envfile.generate_password()}) == []


@pytest.mark.parametrize("password,expected", [
    ("change-me", "placeholder"),
    ("pick-something", "placeholder"),
    ("", "placeholder"),
    ("short", "minimum"),
    ("has$dollar123", "expand"),
])
def test_needs_attention_catches_bad_passwords(password, expected):
    problems = envfile.needs_attention({"NEO4J_PASS": password})
    assert problems, f"{password!r} should have been rejected"
    assert any(expected in p for p in problems)


# ── embedding endpoint ───────────────────────────────

@pytest.mark.parametrize("given,expected", [
    ("http://127.0.0.1:1234/v1", "http://host.docker.internal:1234/v1"),
    ("http://localhost:11434/v1", "http://host.docker.internal:11434/v1"),
    ("http://0.0.0.0:8080/v1", "http://host.docker.internal:8080/v1"),
])
def test_local_bases_are_rewritten_for_the_container(given, expected):
    """
    Inside Docker, 127.0.0.1 is the container. Writing a host-visible URL into
    .env unchanged is the single commonest way the embedding backend ends up
    unreachable while looking correctly configured.
    """
    assert embedding.to_container_base(given) == expected


def test_remote_bases_are_left_alone():
    base = "http://192.168.1.50:1234/v1"
    assert embedding.to_container_base(base) == base


def test_rewrite_is_reversible_for_the_doctor():
    """doctor probes from the host, so it has to undo the rewrite to reach it."""
    container = embedding.to_container_base("http://127.0.0.1:1234/v1")
    assert container.replace(embedding.DOCKER_HOST_ALIAS, "127.0.0.1") == \
        "http://127.0.0.1:1234/v1"


# ── MCP client config ────────────────────────────────

def _config_with(tmp_path, servers):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    return path


def test_install_preserves_other_servers(tmp_path):
    """
    The README warns that clients replace configs rather than merging, and tells
    users to paste every server by hand. Merging is the whole point of this code;
    dropping a neighbouring server would be worse than not helping at all.
    """
    path = _config_with(tmp_path, {"duckduckgo": {"command": "uvx", "args": ["ddg@1"]}})
    entry = mcpconfig.build_entry("/opt/mmu/mmu-mcp")
    action, _, error = mcpconfig.install(path, entry)

    assert (action, error) == ("added", None)
    servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["duckduckgo"] == {"command": "uvx", "args": ["ddg@1"]}
    assert servers[mcpconfig.SERVER_KEY] == entry


def test_install_preserves_unrelated_top_level_keys(tmp_path):
    """Claude Desktop keeps its window preferences in the same file."""
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"mcpServers": {}, "preferences": {"theme": "dark"}}),
                    encoding="utf-8")
    mcpconfig.install(path, mcpconfig.build_entry("/opt/mmu/mmu-mcp"))
    assert json.loads(path.read_text(encoding="utf-8"))["preferences"] == {"theme": "dark"}


def test_install_is_idempotent(tmp_path):
    path = _config_with(tmp_path, {})
    entry = mcpconfig.build_entry("/opt/mmu/mmu-mcp")
    mcpconfig.install(path, entry)
    action, backup, error = mcpconfig.install(path, entry)
    assert (action, backup, error) == ("unchanged", None, None)


def test_install_backs_up_before_changing(tmp_path):
    path = _config_with(tmp_path, {"other": {"command": "x"}})
    original = path.read_text(encoding="utf-8")
    _, backup, _ = mcpconfig.install(path, mcpconfig.build_entry("/opt/mmu/mmu-mcp"))
    assert backup is not None and backup.exists()
    assert backup.read_text(encoding="utf-8") == original


def test_install_refuses_to_touch_unparseable_config(tmp_path):
    """Hand-written config we cannot read is config we must not rewrite."""
    path = tmp_path / "client.json"
    path.write_text("{ this is not json", encoding="utf-8")
    action, _, error = mcpconfig.install(path, mcpconfig.build_entry("/opt/mmu/mmu-mcp"))
    assert action is None and error
    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_install_creates_a_config_that_does_not_exist_yet(tmp_path):
    path = tmp_path / "nested" / "client.json"
    action, _, error = mcpconfig.install(path, mcpconfig.build_entry("/opt/mmu/mmu-mcp"))
    assert (action, error) == ("added", None)
    assert mcpconfig.SERVER_KEY in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_remove_leaves_other_servers_intact(tmp_path):
    path = _config_with(tmp_path, {"duckduckgo": {"command": "uvx"}})
    mcpconfig.install(path, mcpconfig.build_entry("/opt/mmu/mmu-mcp"))
    action, _, error = mcpconfig.remove(path)
    assert (action, error) == ("removed", None)
    assert sorted(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == ["duckduckgo"]


# ── project-scoped removal ───────────────────────────

def test_remove_spares_an_entry_pointing_at_another_install(tmp_path):
    """
    Regression: uninstalling a throwaway MMU unregistered the user's real one,
    because the client config is global while the uninstall was not. Removal is
    now gated on the entry actually launching the project being removed.
    """
    other_install = tmp_path / "real-mmu"
    scratch_install = tmp_path / "scratch-mmu"
    path = _config_with(tmp_path, {
        mcpconfig.SERVER_KEY: mcpconfig.build_entry(
            "python", [str(other_install / "mmu_mcp_server.py")]
        )
    })

    action, backup, error = mcpconfig.remove(path, project_dir=scratch_install)

    assert (action, backup, error) == ("foreign", None, None)
    assert mcpconfig.SERVER_KEY in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_remove_takes_out_an_entry_pointing_at_this_install(tmp_path):
    install_dir = tmp_path / "mmu"
    path = _config_with(tmp_path, {
        mcpconfig.SERVER_KEY: mcpconfig.build_entry(
            "python", [str(install_dir / "mmu_mcp_server.py")]
        )
    })
    action, _, error = mcpconfig.remove(path, project_dir=install_dir)
    assert (action, error) == ("removed", None)


def test_references_matches_a_frozen_bridge_beside_the_project():
    install_dir = os.path.join("C:" if os.name == "nt" else "", os.sep, "opt", "mmu")
    entry = mcpconfig.build_entry(os.path.join(install_dir, "mmu-mcp"))
    assert mcpconfig.references(entry, install_dir)


def test_references_rejects_a_sibling_with_a_shared_prefix():
    """`/opt/mmu-old` must not count as living inside `/opt/mmu`."""
    root = "C:" + os.sep if os.name == "nt" else os.sep
    entry = mcpconfig.build_entry(os.path.join(root, "opt", "mmu-old", "mmu-mcp"))
    assert not mcpconfig.references(entry, os.path.join(root, "opt", "mmu"))


# ── payload ──────────────────────────────────────────

def test_payload_list_covers_everything_the_dockerfile_copies():
    """
    The image build fails at `docker compose up` if a COPYed module is missing
    from the bundle -- late, slow and confusing. Catch it here instead.
    """
    dockerfile = os.path.join(REPO_ROOT, "Dockerfile")
    copied = []
    for line in open(dockerfile, encoding="utf-8"):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "COPY" and parts[1].endswith(".py"):
            copied.append(parts[1])
    assert copied, "no COPY lines found; has the Dockerfile changed shape?"
    missing = [name for name in copied if name not in payload.PAYLOAD_FILES]
    assert not missing, f"Dockerfile COPYs {missing}, which the installer would not ship"


def test_payload_list_matches_the_spec_file():
    """mmu-setup.spec keeps its own copy of the list; they must not drift."""
    spec = open(os.path.join(REPO_ROOT, "packaging", "mmu-setup.spec"), encoding="utf-8").read()
    for name in payload.PAYLOAD_FILES:
        assert f'"{name}"' in spec, f"{name} is missing from mmu-setup.spec"


def test_materialize_writes_a_usable_project(tmp_path):
    written = payload.materialize(tmp_path)
    assert "docker-compose.yml" in written
    assert (tmp_path / "docker-compose.yml").exists()
    assert (tmp_path / ".env.example").exists()
    # /docs is a read-only mount; compose refuses to start if it is missing.
    assert (tmp_path / "documents").is_dir()


def test_materialize_never_overwrites_an_existing_env(tmp_path):
    """.env holds the password protecting the graph. Losing it loses the graph."""
    payload.materialize(tmp_path)
    (tmp_path / ".env").write_text("NEO4J_PASS=irreplaceable\n", encoding="utf-8")
    payload.materialize(tmp_path)
    assert "irreplaceable" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_looks_like_mmu_rejects_an_unrelated_compose_project(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  postgres:\n    image: postgres:16\n", encoding="utf-8"
    )
    assert not payload.looks_like_mmu(tmp_path)


def test_looks_like_mmu_accepts_the_real_repo():
    assert payload.looks_like_mmu(REPO_ROOT)
