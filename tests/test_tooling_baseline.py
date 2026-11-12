"""Tooling baseline proof tests (roadmap item_02, issue #6).

Each test carries its GWT id via the parametrized case id, so the
pytest nodeid literally renders as ``test_name[ITEM-02.GWT-N]``.
"""

import asyncio
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


def _run_gate_tool(tool: str, *args: str) -> subprocess.CompletedProcess[str]:
    binary = Path(sys.executable).with_name(tool)
    return subprocess.run(
        [str(binary), *args],
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


@pytest.mark.e2e
@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-02.GWT-4")])
def test_e2e_gate_placeholder(_gwt: object) -> None:
    """GWT-4: stub for live-hardware tests; proves the e2e marker gates.

    Deselected under ``pytest -m "not e2e"``; skips honestly when the
    e2e selection is run without real Bluetooth/telephony hardware.
    """
    pytest.skip("e2e harness requires real Bluetooth/telephony hardware")
