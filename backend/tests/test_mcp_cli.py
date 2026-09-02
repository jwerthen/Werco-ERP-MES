"""``python -m app.mcp``: the stdio bridge CLI, for real.

Three layers, cheapest first:

1. The helpers in ``app/mcp/__main__.py`` IN-PROCESS -- argument parsing, the
   REMOTE-mode placeholder environment (applied only in remote mode, only for missing
   variables, never over a set one, and acceptable to the app's own ``Settings``
   validators), executor / verifier / transport selection with the two ``_serve_*``
   entry points stubbed so no socket opens and no loop blocks, the release-on-exit
   contract of ``_serve_stdio``, and the rule that a credential description names
   the KIND of credential and never its value.
2. ``--print-catalog`` in a SUBPROCESS with an environment built from scratch -- no
   ``DATABASE_URL`` / ``SECRET_KEY`` / ``REFRESH_TOKEN_SECRET_KEY`` -- because the
   placeholder logic only proves anything when those are genuinely absent, and the
   pytest process (conftest) has them set. stdout must be nothing but the JSON: the
   app logs to ``sys.stdout`` on import, and one log line on the wire would corrupt
   every MCP session an agent opens.
3. A full stdio ROUND TRIP over the SDK client against a bridge in REMOTE mode whose
   ERP does not answer: initialize, the whole catalog, a tool call that comes back as
   a bounded ``is_error`` result (status 0 transport error with a token, 401 with no
   credentials) instead of a crash or a hang, and a session that still answers
   afterwards.

Layers 2 and 3 are real ``python -m app.mcp`` invocations from ``backend/``, the way
Cursor / Claude Code / Grok Bot spawn the bridge. Coverage of ``main()`` itself comes
from layer 1, which drives it in-process; the subprocess layers prove the process
boundary (the wire, the environment, the exit code), which no in-process test can.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import Server

from app.core.config import Settings, settings
from app.mcp import __main__ as cli
from app.mcp.auth import ENV_EMAIL, ENV_PASSWORD, ENV_REFRESH_TOKEN, ENV_TOKEN, ErpTokenVerifier, TokenSource
from app.mcp.executor import InProcessExecutor, RemoteExecutor
from app.mcp.server import SERVER_NAME

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# The variables the REMOTE bridge must not need. conftest sets all three in the
# pytest process, which is why the subprocess layers build their environment from
# scratch instead of copying ``os.environ``.
APP_BOOT_VARS = ("DATABASE_URL", "SECRET_KEY", "REFRESH_TOKEN_SECRET_KEY")

# OS plumbing the child needs to start at all. Nothing here configures the app.
_OS_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONPATH", "VIRTUAL_ENV", "SYSTEMROOT")

# Loopback port 9 (discard) has no listener on any developer machine or CI runner, and
# a refused connection on loopback is immediate -- unlike a DNS lookup of a reserved
# name, which some sandboxes retry for seconds before failing.
DEAD_ERP_URL = "http://127.0.0.1:9"
ROUND_TRIP_TIMEOUT_SECONDS = 60.0
# Sentinels, not credentials: each test asserts its value shows up NOWHERE the bridge writes.
SECRET_TOKEN = "bridge-test-access-token-must-never-be-logged"
SECRET_REFRESH = "bridge-test-refresh-token-must-never-be-logged"
SECRET_PASSWORD = "bridge-test-password-must-never-be-logged"
AGENT_EMAIL = "agent@example.test"

# The keys ``docs/MCP.md`` §3.2 documents for ``--print-catalog``.
CATALOG_KEYS = {"server", "version", "convenience_tools", "generated_tools", "shadowed_operations", "counts"}


def clean_env(**overrides: str) -> Dict[str, str]:
    """An environment built from scratch: OS plumbing plus ``overrides``, none of the harness's app variables."""
    env = {key: os.environ[key] for key in _OS_PASSTHROUGH if key in os.environ}
    env.update(overrides)
    leaked = [key for key in APP_BOOT_VARS if key in env]
    assert not leaked, f"clean_env must not carry {leaked}; the point is that the bridge boots without them"
    return env


# --------------------------------------------------------------------------- layer 1: in-process helpers


class TestArgumentParsing:
    pytestmark = pytest.mark.unit

    def test_defaults_leave_every_choice_to_the_environment(self) -> None:
        args = cli._parse_args([])
        assert args.transport is None
        assert args.host is None
        assert args.port is None
        assert args.print_catalog is False

    def test_explicit_flags_are_read(self) -> None:
        args = cli._parse_args(["--transport", "http", "--host", "0.0.0.0", "--port", "9001", "--print-catalog"])
        assert args.transport == "http"
        assert args.host == "0.0.0.0"
        assert args.port == 9001
        assert args.print_catalog is True

    @pytest.mark.parametrize("argv", [["--transport", "tcp"], ["--port", "not-a-port"], ["--bogus"]])
    def test_invalid_arguments_exit_with_argparse_usage_error(self, argv: List[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli._parse_args(argv)
        assert exc_info.value.code == 2


class TestPlaceholderEnvironment:
    pytestmark = pytest.mark.unit

    def test_fills_every_boot_variable_when_none_is_set(self) -> None:
        env: Dict[str, str] = {}
        cli.install_placeholder_env(env)
        assert env == cli._PLACEHOLDER_ENV
        assert set(APP_BOOT_VARS) <= set(env), "the three variables the remote bridge must not need are all defaulted"

    def test_never_overrides_a_variable_that_is_already_set(self) -> None:
        env = {"DATABASE_URL": "postgresql://real:db@db.example.supabase.co/postgres", "ENVIRONMENT": "staging"}
        cli.install_placeholder_env(env)
        assert env["DATABASE_URL"] == "postgresql://real:db@db.example.supabase.co/postgres"
        assert env["ENVIRONMENT"] == "staging"
        assert env["SECRET_KEY"] == cli._PLACEHOLDER_ENV["SECRET_KEY"]
        assert env["REFRESH_TOKEN_SECRET_KEY"] == cli._PLACEHOLDER_ENV["REFRESH_TOKEN_SECRET_KEY"]
        assert env["RATE_LIMIT_ENABLED"] == "false"

    def test_is_idempotent(self) -> None:
        env = {"SECRET_KEY": "x" * 40}
        cli.install_placeholder_env(env)
        once = dict(env)
        cli.install_placeholder_env(env)
        assert env == once

    def test_placeholders_satisfy_the_apps_own_settings_validators(self) -> None:
        # The whole point of the placeholders is that ``app.main`` imports without a
        # real environment. ``Settings`` rejects short or well-known secrets and a
        # non-Supabase database in production; the placeholders must clear all of that.
        booted = Settings(_env_file=None, **cli._PLACEHOLDER_ENV)
        assert booted.ENVIRONMENT != "production"
        assert (
            booted.is_sqlite_database
        ), "the placeholder DATABASE_URL must be an in-memory sqlite URL, never a network one"
        assert booted.RATE_LIMIT_ENABLED is False
        assert len(booted.SECRET_KEY) >= 32 and len(booted.REFRESH_TOKEN_SECRET_KEY) >= 32

    def test_placeholder_secrets_are_distinct_from_each_other(self) -> None:
        # Two keys that happened to be equal would let an access token verify as a refresh token.
        assert cli._PLACEHOLDER_ENV["SECRET_KEY"] != cli._PLACEHOLDER_ENV["REFRESH_TOKEN_SECRET_KEY"]


class TestCredentialDescription:
    """``TokenSource.describe`` is what the bridge logs at startup; it must never carry a value."""

    pytestmark = pytest.mark.unit

    @staticmethod
    def _source(environ: Dict[str, str]) -> Optional[TokenSource]:
        # ``describe`` never exchanges anything, so any object will do as the exchange.
        return TokenSource.from_env(exchange=object(), environ=environ)

    def test_each_credential_kind_is_named_without_its_value(self) -> None:
        environ = {
            ENV_TOKEN: SECRET_TOKEN,
            ENV_REFRESH_TOKEN: SECRET_REFRESH,
            ENV_EMAIL: AGENT_EMAIL,
            ENV_PASSWORD: SECRET_PASSWORD,
        }
        source = self._source(environ)
        assert source is not None
        description = source.describe()
        assert description == "access-token, refresh-token, password-login"
        for value in (SECRET_TOKEN, SECRET_REFRESH, SECRET_PASSWORD, AGENT_EMAIL):
            assert value not in description

    def test_a_single_static_token_describes_as_access_token(self) -> None:
        source = self._source({ENV_TOKEN: SECRET_TOKEN})
        assert source is not None and source.describe() == "access-token"

    def test_email_without_password_is_not_a_credential(self) -> None:
        assert self._source({ENV_EMAIL: AGENT_EMAIL}) is None
        assert self._source({ENV_PASSWORD: SECRET_PASSWORD}) is None

    def test_nothing_configured_is_none(self) -> None:
        assert self._source({}) is None
        assert self._source({ENV_TOKEN: "   "}) is None, "whitespace is not a token"


class TestRemoteBearerPassthrough:
    pytestmark = pytest.mark.unit

    @pytest.mark.parametrize("token", ["", "   "])
    async def test_a_missing_bearer_is_refused_at_the_door(self, token: str) -> None:
        assert await cli._RemoteBearerPassthrough().verify_token(token) is None

    async def test_any_present_bearer_is_passed_through_for_the_remote_to_verify(self) -> None:
        accepted = await cli._RemoteBearerPassthrough().verify_token("opaque-remote-token")
        assert accepted is not None
        assert accepted.token == "opaque-remote-token"
        assert accepted.client_id == "remote"
        assert accepted.expires_at is None, "this process cannot read a remote deployment's expiry"


# --------------------------------------------------------------------------- layer 1: main() wiring


@dataclass
class ServeCall:
    server: Any
    executor: Any
    kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ServeRecorder:
    stdio: List[ServeCall] = field(default_factory=list)
    http: List[ServeCall] = field(default_factory=list)


@dataclass
class FakeExecutor:
    """Only the slice of an executor the ``_serve_*`` entry points touch: release on exit."""

    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def restored_environ() -> Iterator[None]:
    """``main()`` writes placeholders into ``os.environ``; put the worker's environment back afterwards."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@dataclass
class MainRun:
    rc: int
    wire: io.StringIO  # the stand-in process stdout ``main()`` captured as the MCP wire
    stdout_repointed_to_stderr: bool  # what ``sys.stdout`` was when ``main()`` returned


def run_main(argv: List[str], stdout: Optional[io.StringIO] = None) -> MainRun:
    """Run ``main()`` behind a stand-in process stdout, and put the real one back afterwards.

    Done around the call rather than in a fixture on purpose: pytest's capture manager
    re-assigns ``sys.stdout`` at the start of every call phase, so a swap made during
    fixture setup is silently undone before ``main()`` ever reads it.
    """
    buffer = stdout if stdout is not None else io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = buffer
    try:
        rc = cli.main(argv)
        repointed = sys.stdout is sys.stderr
    finally:
        sys.stdout = saved_stdout
    return MainRun(rc=rc, wire=buffer, stdout_repointed_to_stderr=repointed)


@pytest.fixture
def serve_recorder(monkeypatch: pytest.MonkeyPatch) -> ServeRecorder:
    """Replace both ``_serve_*`` entry points: record what ``main()`` hands them, open nothing."""
    recorder = ServeRecorder()

    async def fake_stdio(server: Any, wire: Any, executor: Any) -> None:
        recorder.stdio.append(ServeCall(server, executor, {"wire": wire}))
        await executor.aclose()

    def fake_http(server: Any, *, host: str, port: int, verifier: Any, max_body: int, executor: Any) -> None:
        recorder.http.append(
            ServeCall(server, executor, {"host": host, "port": port, "verifier": verifier, "max_body": max_body})
        )
        asyncio.run(executor.aclose())

    monkeypatch.setattr(cli, "_serve_stdio", fake_stdio)
    monkeypatch.setattr(cli, "_serve_http", fake_http)
    # basicConfig is a no-op while pytest's log capture owns the root logger; make that
    # explicit so a run without the logging plugin cannot leave a stray stderr handler.
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
    return recorder


@pytest.fixture
def build_server_spy(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Record the keyword arguments ``main()`` passes to ``build_server`` (then build for real)."""
    import app.mcp.server as server_module

    real = server_module.build_server
    seen: List[Dict[str, Any]] = []

    def spy(executor: Any, **kwargs: Any) -> Server:
        seen.append({"executor": executor, **kwargs})
        return real(executor, **kwargs)

    monkeypatch.setattr(server_module, "build_server", spy)
    return seen


BRIDGE_ENV_VARS = (
    cli.ENV_URL,
    cli.ENV_TRANSPORT,
    cli.ENV_HOST,
    cli.ENV_PORT,
    ENV_TOKEN,
    ENV_REFRESH_TOKEN,
    ENV_EMAIL,
    ENV_PASSWORD,
)


def _clear_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every ``main()`` case from no bridge configuration at all."""
    for key in BRIDGE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.usefixtures("restored_environ")
class TestMainWiring:
    pytestmark = pytest.mark.unit

    def test_remote_url_selects_the_remote_executor_and_leaves_verification_to_the_remote(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_recorder: ServeRecorder,
        build_server_spy: List[Dict[str, Any]],
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "https://erp.example.test/")  # trailing slash on purpose
        monkeypatch.setenv(ENV_TOKEN, SECRET_TOKEN)

        run = run_main([])
        assert run.rc == 0

        assert serve_recorder.http == []
        (call,) = serve_recorder.stdio
        assert isinstance(call.executor, RemoteExecutor)
        assert call.executor.base_url == "https://erp.example.test"
        assert isinstance(call.server, Server) and call.server.name == SERVER_NAME
        assert (
            call.kwargs["wire"] is run.wire
        ), "the SDK must be handed the ORIGINAL stdout, captured before the app logged"
        assert run.stdout_repointed_to_stderr, "after capture, everything the process prints must land on stderr"

        (built,) = build_server_spy
        assert built["verify_bearer"] is False, "a remote deployment's tokens cannot be verified here"
        assert built["token_source"] is not None and built["token_source"].describe() == "access-token"

    def test_no_url_selects_the_in_process_executor_and_verifies_bearers_itself(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_recorder: ServeRecorder,
        build_server_spy: List[Dict[str, Any]],
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)

        run = run_main([])
        assert run.rc == 0

        (call,) = serve_recorder.stdio
        assert isinstance(call.executor, InProcessExecutor)
        (built,) = build_server_spy
        assert built["verify_bearer"] is True
        assert (
            "RATE_LIMIT_ENABLED" not in os.environ
        ), "placeholders are a REMOTE-mode device; in-process mode must not touch the environment"

    def test_a_blank_url_means_in_process(self, monkeypatch: pytest.MonkeyPatch, serve_recorder: ServeRecorder) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "   ")
        run = run_main([])
        assert run.rc == 0
        (call,) = serve_recorder.stdio
        assert isinstance(call.executor, InProcessExecutor)

    def test_remote_mode_fills_only_the_missing_boot_variables(
        self, monkeypatch: pytest.MonkeyPatch, serve_recorder: ServeRecorder
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "https://erp.example.test")
        monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
        database_url_before = os.environ["DATABASE_URL"]  # conftest's test database

        run = run_main([])
        assert run.rc == 0

        assert os.environ["RATE_LIMIT_ENABLED"] == "false", "a missing variable is defaulted"
        assert os.environ["DATABASE_URL"] == database_url_before, "a set variable is never overridden"
        assert os.environ["DATABASE_URL"] != cli._PLACEHOLDER_ENV["DATABASE_URL"]

    def test_credential_kinds_are_logged_but_their_values_never_are(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_recorder: ServeRecorder,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "https://erp.example.test")
        monkeypatch.setenv(ENV_REFRESH_TOKEN, SECRET_REFRESH)
        monkeypatch.setenv(ENV_EMAIL, AGENT_EMAIL)
        monkeypatch.setenv(ENV_PASSWORD, SECRET_PASSWORD)
        caplog.set_level(logging.INFO, logger="app.mcp")

        run = run_main([])
        assert run.rc == 0

        assert "Credentials configured: refresh-token, password-login" in caplog.text
        assert "REMOTE mode -> https://erp.example.test" in caplog.text
        for secret in (SECRET_REFRESH, SECRET_PASSWORD, AGENT_EMAIL):
            assert secret not in caplog.text
            assert secret not in run.wire.getvalue()

    def test_no_credentials_warns_once_and_still_serves(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_recorder: ServeRecorder,
        build_server_spy: List[Dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "https://erp.example.test")
        caplog.set_level(logging.INFO, logger="app.mcp")

        run = run_main([])
        assert run.rc == 0

        warnings = [r for r in caplog.records if r.name == "app.mcp" and r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "No ERP credentials configured" in warnings[0].getMessage()
        (built,) = build_server_spy
        assert built["token_source"] is None
        assert len(serve_recorder.stdio) == 1, "an unconfigured bridge still serves (every call answers 401)"

    @pytest.mark.parametrize(
        ("argv", "environ", "expected_host", "expected_port"),
        [
            # flags beat the environment
            (
                ["--transport", "http", "--host", "0.0.0.0", "--port", "9001"],
                {"H": "10.0.0.1", "P": "7000"},
                "0.0.0.0",
                9001,
            ),
            # the environment beats the defaults; WERCO_MCP_TRANSPORT is case/whitespace-insensitive
            ([], {"T": " HTTP ", "H": "10.0.0.1", "P": "7000"}, "10.0.0.1", 7000),
            # nothing set: the documented defaults
            (["--transport", "http"], {}, "127.0.0.1", 8765),
        ],
    )
    def test_http_transport_resolves_host_and_port_flags_over_env_over_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
        serve_recorder: ServeRecorder,
        argv: List[str],
        environ: Dict[str, str],
        expected_host: str,
        expected_port: int,
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "https://erp.example.test")
        names = {"T": cli.ENV_TRANSPORT, "H": cli.ENV_HOST, "P": cli.ENV_PORT}
        for short, value in environ.items():
            monkeypatch.setenv(names[short], value)

        assert run_main(argv).rc == 0

        assert serve_recorder.stdio == []
        (call,) = serve_recorder.http
        assert call.kwargs["host"] == expected_host
        assert call.kwargs["port"] == expected_port
        assert call.kwargs["max_body"] == settings.WERCO_MCP_MAX_UPLOAD_BYTES

    def test_transport_flag_beats_the_transport_variable(
        self, monkeypatch: pytest.MonkeyPatch, serve_recorder: ServeRecorder
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "https://erp.example.test")
        monkeypatch.setenv(cli.ENV_TRANSPORT, "http")
        assert run_main(["--transport", "stdio"]).rc == 0
        assert serve_recorder.http == [] and len(serve_recorder.stdio) == 1

    def test_http_door_verifier_is_a_passthrough_for_a_remote_erp_and_the_real_one_in_process(
        self, monkeypatch: pytest.MonkeyPatch, serve_recorder: ServeRecorder
    ) -> None:
        _clear_bridge_env(monkeypatch)
        monkeypatch.setenv(cli.ENV_URL, "https://erp.example.test")
        assert run_main(["--transport", "http"]).rc == 0
        _clear_bridge_env(monkeypatch)
        assert run_main(["--transport", "http"]).rc == 0

        remote, in_process = serve_recorder.http
        assert isinstance(remote.kwargs["verifier"], cli._RemoteBearerPassthrough)
        assert isinstance(remote.executor, RemoteExecutor)
        assert isinstance(in_process.kwargs["verifier"], ErpTokenVerifier)
        assert isinstance(in_process.executor, InProcessExecutor)


@pytest.mark.usefixtures("restored_environ")
class TestPrintCatalogInProcess:
    pytestmark = pytest.mark.unit

    def test_writes_the_catalog_json_to_the_captured_wire_and_serves_nothing(
        self, monkeypatch: pytest.MonkeyPatch, serve_recorder: ServeRecorder
    ) -> None:
        _clear_bridge_env(monkeypatch)
        run = run_main(["--print-catalog"])
        assert run.rc == 0

        assert serve_recorder.stdio == [] and serve_recorder.http == []
        text = run.wire.getvalue()
        assert text.endswith("\n")
        payload = json.loads(text)
        assert set(payload) == CATALOG_KEYS
        assert payload["server"] == SERVER_NAME
        assert payload["counts"]["convenience"] == 15
        assert payload["counts"]["generated"] > 600
        assert payload["counts"]["shadowed"] == len(payload["shadowed_operations"])
        assert payload["counts"]["generated"] == len(payload["generated_tools"])
        assert {tool["name"] for tool in payload["convenience_tools"]} >= {"create_work_order", "list_parts", "search"}

    def test_a_closed_pipe_is_the_readers_choice_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, serve_recorder: ServeRecorder
    ) -> None:
        # ``python -m app.mcp --print-catalog | head`` closes the pipe early.
        class ClosedPipe(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError

        _clear_bridge_env(monkeypatch)
        assert run_main(["--print-catalog"], stdout=ClosedPipe()).rc == 0

    def test_the_module_entry_point_exits_with_mains_return_code(
        self, monkeypatch: pytest.MonkeyPatch, serve_recorder: ServeRecorder
    ) -> None:
        # ``python -m app.mcp`` runs ``app/mcp/__main__.py`` as ``__main__``, whose only
        # top-level act is ``sys.exit(main())``. runpy executes that same file in-process
        # (a fresh namespace, so it must be a case that returns before serving). run_path
        # rather than run_module: this test file already imported the module as ``cli``,
        # which run_module would flag.
        import runpy

        _clear_bridge_env(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["app.mcp", "--print-catalog"])
        buffer = io.StringIO()
        saved_stdout = sys.stdout
        sys.stdout = buffer
        try:
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_path(str(cli.__file__), run_name="__main__")
        finally:
            sys.stdout = saved_stdout

        assert exc_info.value.code == 0
        assert json.loads(buffer.getvalue())["counts"]["convenience"] == 15


class TestServeStdio:
    """``_serve_stdio`` hands the SDK the captured wire and ALWAYS releases the executor."""

    pytestmark = pytest.mark.unit

    @dataclass
    class FakeServer:
        fail: Optional[BaseException] = None
        ran: bool = False

        def create_initialization_options(self) -> Dict[str, Any]:
            return {"fake": True}

        async def run(self, read_stream: Any, write_stream: Any, options: Any) -> None:
            self.ran = True
            if self.fail is not None:
                raise self.fail

    @pytest.fixture
    def fake_stdio_server(self, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
        import mcp.server.stdio as stdio_module

        seen: Dict[str, Any] = {}

        @asynccontextmanager
        async def fake(stdin: Any = None, stdout: Any = None) -> AsyncIterator[Any]:
            seen["stdin"] = stdin
            seen["stdout"] = stdout
            yield object(), object()

        monkeypatch.setattr(stdio_module, "stdio_server", fake)
        return seen

    def test_serves_over_the_captured_wire_and_closes_the_executor_on_exit(self, fake_stdio_server) -> None:
        server = self.FakeServer()
        executor = FakeExecutor()
        wire = io.StringIO()

        asyncio.run(cli._serve_stdio(server, wire, executor))

        assert server.ran
        assert executor.closed
        assert fake_stdio_server["stdout"].wrapped is wire, "the SDK writes to the wire captured before the app logged"
        assert fake_stdio_server["stdin"] is None, "stdin is the process's own; only stdout is redirected"

    def test_closes_the_executor_even_when_the_session_fails(self, fake_stdio_server) -> None:
        server = self.FakeServer(fail=RuntimeError("session torn down"))
        executor = FakeExecutor()

        with pytest.raises(RuntimeError, match="session torn down"):
            asyncio.run(cli._serve_stdio(server, io.StringIO(), executor))

        assert executor.closed, "the pooled httpx client must be released on the failure path too"


class TestServeHttp:
    """``_serve_http`` is DEV-ONLY, but it is what a developer's agent talks to: pin its wiring."""

    pytestmark = pytest.mark.unit

    def test_mounts_the_door_at_the_configured_path_and_releases_the_executor_at_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uvicorn

        from app.mcp.http import McpDoor

        seen: Dict[str, Any] = {}

        def fake_uvicorn_run(starlette_app: Any, *, host: str, port: int, log_level: str) -> None:
            seen.update(app=starlette_app, host=host, port=port, log_level=log_level)

        monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
        server = Server(SERVER_NAME)
        verifier = cli._RemoteBearerPassthrough()
        executor = FakeExecutor()

        cli._serve_http(server, host="127.0.0.1", port=8765, verifier=verifier, max_body=1234, executor=executor)

        assert (seen["host"], seen["port"]) == ("127.0.0.1", 8765)
        starlette_app = seen["app"]
        (route,) = starlette_app.routes
        assert route.path == settings.WERCO_MCP_HTTP_PATH, "the dev server and the mounted door must share one path"
        door = route.endpoint
        assert isinstance(door, McpDoor)
        assert door.server is server
        assert door.verifier is verifier
        assert door.max_request_body_size == 1234, "the upload cap reaches the door unchanged"

        # uvicorn would run this lifespan; drive it directly. The door is armed only
        # inside it, and the executor is released on the way out -- also when uvicorn
        # never got to run.
        async def drive_lifespan() -> bool:
            async with starlette_app.router.lifespan_context(starlette_app):
                armed = door.running
                assert not executor.closed
            return armed

        assert asyncio.run(drive_lifespan()) is True
        assert not door.running
        assert executor.closed, "the pooled httpx client must be released when the dev server stops"


# --------------------------------------------------------------------------- layer 2: --print-catalog subprocess


@pytest.fixture(scope="module", params=["with-remote-url", "without-remote-url"])
def print_catalog_run(request: pytest.FixtureRequest) -> subprocess.CompletedProcess:
    """One real ``python -m app.mcp --print-catalog`` per environment shape, from ``backend/``.

    Module-scoped: the run costs a few seconds (it imports ``app.main`` cold) and
    nothing in it varies between the assertions below. Parametrized over the two
    shapes ``docs/MCP.md`` promises -- with ``WERCO_ERP_URL`` (the bridge's normal
    shape) and without it ("no token, no database, no WERCO_ERP_URL needed").
    """
    overrides = {"WERCO_ERP_URL": "https://example.invalid"} if request.param == "with-remote-url" else {}
    return subprocess.run(
        [sys.executable, "-m", "app.mcp", "--print-catalog"],
        cwd=BACKEND_ROOT,
        env=clean_env(**overrides),
        capture_output=True,
        text=True,
        timeout=180,
    )


class TestPrintCatalogSubprocess:
    pytestmark = pytest.mark.integration

    def test_exits_zero_without_a_database_or_signing_keys(
        self, print_catalog_run: subprocess.CompletedProcess
    ) -> None:
        assert print_catalog_run.returncode == 0, (
            f"python -m app.mcp --print-catalog failed in a clean environment:\n"
            f"--- stdout ---\n{print_catalog_run.stdout[:2000]}\n--- stderr ---\n{print_catalog_run.stderr[-4000:]}"
        )

    def test_stdout_is_exactly_one_json_catalog(self, print_catalog_run: subprocess.CompletedProcess) -> None:
        stdout = print_catalog_run.stdout
        assert stdout.startswith("{"), f"stdout must begin with the JSON document, got: {stdout[:200]!r}"
        # A single json.loads over the WHOLE stream is the proof: one stray log line
        # anywhere in it -- before, inside or after the document -- fails the parse.
        payload = json.loads(stdout)
        assert set(payload) == CATALOG_KEYS
        assert payload["server"] == SERVER_NAME
        assert payload["counts"]["convenience"] == 15
        assert payload["counts"]["generated"] > 600
        assert payload["counts"]["generated"] == len(payload["generated_tools"])
        assert payload["counts"]["shadowed"] == len(payload["shadowed_operations"])
        assert stdout.count('"server"') == 1, "exactly one document on the wire"

    def test_the_apps_log_lines_land_on_stderr(self, print_catalog_run: subprocess.CompletedProcess) -> None:
        # The app logs at import (``app.main`` announces its host-header posture); that
        # line is the evidence the redirect happened -- it must show up on stderr and,
        # by the parse above, nowhere on stdout.
        assert "app.main" in print_catalog_run.stderr, (
            "expected the app's own startup log line on stderr; if it never logged, the clean-stdout "
            f"assertion is vacuous. stderr was:\n{print_catalog_run.stderr[-2000:]}"
        )
        assert "Traceback" not in print_catalog_run.stderr
        assert " - INFO - " not in print_catalog_run.stdout


# --------------------------------------------------------------------------- layer 3: stdio round trip


@dataclass
class BridgeRun:
    server_name: str
    tool_names: List[str]
    result: Any
    tool_count_after: int
    stderr: str


async def round_trip(env: Dict[str, str], errlog_path: Path, tool: str, arguments: Dict[str, Any]) -> BridgeRun:
    """Spawn the bridge the way an agent does, drive one full session over stdio, collect what it said."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp"], env=env, cwd=str(BACKEND_ROOT))
    # ``errlog`` becomes the child's stderr fd, so it must be a real file: pytest's
    # captured ``sys.stderr`` has no fileno.
    with open(errlog_path, "w", encoding="utf-8") as errlog, anyio.fail_after(ROUND_TRIP_TIMEOUT_SECONDS):
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                listing = await session.list_tools()
                result = await session.call_tool(tool, arguments)
                listing_after = await session.list_tools()
    return BridgeRun(
        server_name=init.server_info.name,
        tool_names=[tool.name for tool in listing.tools],
        result=result,
        tool_count_after=len(listing_after.tools),
        stderr=errlog_path.read_text(encoding="utf-8"),
    )


class TestStdioRoundTrip:
    pytestmark = pytest.mark.integration

    async def test_remote_bridge_with_a_token_serves_the_catalog_and_reports_a_dead_erp_as_a_transport_error(
        self, tmp_path: Path
    ) -> None:
        env = clean_env(WERCO_ERP_URL=DEAD_ERP_URL, WERCO_ERP_TOKEN=SECRET_TOKEN)

        run = await round_trip(env, tmp_path / "bridge.err", "list_parts", {"limit": 1})

        assert run.server_name == SERVER_NAME
        assert len(run.tool_names) > 600
        assert "create_work_order" in run.tool_names
        assert "list_parts" in run.tool_names

        assert run.result.is_error is True
        assert run.result.structured_content["status"] == 0, "no HTTP status: the ERP never answered"
        assert "ConnectError" in run.result.structured_content["detail"]
        assert run.tool_count_after == len(run.tool_names), "the session survives a failed call"

        assert f"REMOTE mode -> {DEAD_ERP_URL}" in run.stderr
        assert "Credentials configured: access-token" in run.stderr
        assert SECRET_TOKEN not in run.stderr, "the bridge must never log a credential value"

    async def test_remote_bridge_without_credentials_answers_401_and_keeps_serving(self, tmp_path: Path) -> None:
        env = clean_env(WERCO_ERP_URL=DEAD_ERP_URL)

        run = await round_trip(env, tmp_path / "bridge.err", "list_parts", {"limit": 1})

        assert len(run.tool_names) > 600 and "create_work_order" in run.tool_names
        assert run.result.is_error is True
        assert run.result.structured_content["status"] == 401
        assert "No ERP credentials configured" in run.result.structured_content["detail"]
        assert run.tool_count_after == len(run.tool_names)

        assert "No ERP credentials configured" in run.stderr, "the startup warning belongs on stderr"
        assert "Traceback" not in run.stderr, "a missing credential is an answer, not an exception"
