"""Tooling baseline proof tests (roadmap item_02, issue #6).

Each test carries its GWT id via the parametrized case id, so the
pytest nodeid literally renders as ``test_name[ITEM-02.GWT-N]``.
"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve

REPO_ROOT = Path(__file__).resolve().parents[1]
# Keep tool runs inside the pytest-timeout=60 envelope so a wedged
# subprocess fails the test instead of hanging the suite.
TOOL_TIMEOUT_SEC = 55


class AgentHello(BaseModel):
    """Trivial runtime model proving pydantic is wired in."""

    agent: str = Field(min_length=1)
    protocol_version: int = Field(ge=1)


async def _echo_hello(connection: ServerConnection) -> None:
    async for raw in connection:
        hello = AgentHello.model_validate_json(raw)
        await connection.send(hello.model_dump_json())


def _tool_binary(tool: str) -> str:
    """Resolve a dev tool next to the interpreter or on PATH.

    The dev extras (ruff, mypy) are optional installs: in a plain
    unit-test environment the binary is absent and the test must skip
    honestly instead of erroring on FileNotFoundError.
    """
    binary = Path(sys.executable).with_name(tool)
    if binary.exists():
        return str(binary)
    found = shutil.which(tool)
    if found is None:
        pytest.skip(f"{tool} not installed in this environment (dev extras missing)")
    return found


def _run_gate_tool(tool: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_tool_binary(tool), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=TOOL_TIMEOUT_SEC,
        check=False,
    )


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-02.GWT-1")])
def test_ruff_check_exits_zero(_gwt: object) -> None:
    """GWT-1: `ruff check .` exits 0 on the checkout."""
    result = _run_gate_tool("ruff", "check", ".")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-02.GWT-2")])
def test_mypy_strict_exits_zero(_gwt: object) -> None:
    """GWT-2: `mypy src tests` exits 0 under the strict config."""
    result = _run_gate_tool("mypy", "src", "tests")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-02.GWT-3")])
async def test_websocket_roundtrip_validates_with_pydantic(_gwt: object) -> None:
    """GWT-3: real async roundtrip through websockets.asyncio.server.

    A pydantic model is serialized, pushed through a live localhost
    websocket endpoint, echoed back and re-validated - exercising both
    new runtime dependencies for real, not a bare event-loop sleep.
    """
    async with serve(_echo_hello, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as client:
            sent = AgentHello(agent="proof", protocol_version=1)
            await client.send(sent.model_dump_json())
            raw = await asyncio.wait_for(client.recv(), timeout=10)
            received = AgentHello.model_validate_json(raw)

    assert received == sent


def _init_tool_repo(tmp_path: Path) -> tuple[Path, str]:
    """Build a throwaway git repo for the shell tools.

    Both tools resolve the repo root via ``git rev-parse
    --show-toplevel``, so running them with ``cwd`` set to a scratch
    repo exercises them against fixture content instead of the live
    ``.changes/`` checkout.
    """
    git = shutil.which("git")
    bash = shutil.which("bash")
    if git is None or bash is None:
        pytest.skip("git and bash are required to exercise the repo tools")
    (tmp_path / ".changes").mkdir(parents=True)
    subprocess.run(
        [git, "init", "-q"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=TOOL_TIMEOUT_SEC,
    )
    return tmp_path, bash


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-02.GWT-5")])
def test_release_notes_handles_yaml_block_scalars(_gwt: object, tmp_path: Path) -> None:
    """GWT-5: block scalars in fragment frontmatter parse cleanly.

    Regression for SA_carbon_review_15 item 35: ``key: >-`` used to
    leak the literal operator into the generated changelog section.
    """
    repo, bash = _init_tool_repo(tmp_path)
    (repo / ".changes" / "fold.md").write_text(
        "---\n"
        "type: fix\n"
        "scope: tooling\n"
        "user_visible: false\n"
        "breaking: false\n"
        'user_summary: "-"\n'
        "dev_summary: >-\n"
        "  folded summary first line\n"
        "  folded summary second line\n"
        "why: |\n"
        "  literal reason line\n"
        "refs: issue #1\n"
        "---\n\n"
        "## Spec impact\n- none\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [bash, str(REPO_ROOT / "tools" / "release_notes.sh")],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=TOOL_TIMEOUT_SEC,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # '>' folds continuation lines into a single space-joined line;
    # '|' keeps the literal line content; the operator itself never
    # reaches the generated notes.
    assert ">-" not in result.stdout
    assert "folded summary first line folded summary second line" in result.stdout
    assert "Why: literal reason line" in result.stdout


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-02.GWT-6")])
def test_spec_anchors_requirement_prefix(_gwt: object, tmp_path: Path) -> None:
    """GWT-6: requirement anchors carry the ``requirement-`` prefix.

    Regression for SA_carbon_review_15 item 45: GitHub slugs the WHOLE
    ``### Requirement: <name>`` heading, so the anchor is
    ``#requirement-<name>`` - the checker must match that.
    """
    repo, bash = _init_tool_repo(tmp_path)
    spec_dir = repo / "docs" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "line-manager.md").write_text(
        "# Spec\n\n### Requirement: Foo bar\n",
        encoding="utf-8",
    )
    fragment = repo / ".changes" / "frag.md"

    def run_check() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [bash, str(REPO_ROOT / "tools" / "check_spec_anchors.sh")],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SEC,
            check=False,
        )

    fragment.write_text(
        "## Spec impact\n- adds: docs/specs/line-manager.md#requirement-foo-bar\n",
        encoding="utf-8",
    )
    ok = run_check()
    assert ok.returncode == 0, ok.stdout + ok.stderr

    fragment.write_text(
        "## Spec impact\n- adds: docs/specs/line-manager.md#foo-bar\n",
        encoding="utf-8",
    )
    bad = run_check()
    assert bad.returncode == 1
    assert "not found" in bad.stderr


@pytest.mark.e2e
@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-02.GWT-4")])
def test_e2e_gate_placeholder(_gwt: object) -> None:
    """GWT-4: stub for live-hardware tests; proves the e2e marker gates.

    Deselected under ``pytest -m "not e2e"``; skips honestly when the
    e2e selection is run without real Bluetooth/telephony hardware.
    """
    pytest.skip("e2e harness requires real Bluetooth/telephony hardware")
