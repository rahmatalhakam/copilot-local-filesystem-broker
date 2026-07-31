from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

from app.config import AppConfig, Workspace
from app.errors import PolicyViolation
from app.powershell import (
    build_wrapper_command,
    execute_restricted_command,
    validate_command,
)


ALLOWED_COMMANDS = [
    "Get-ChildItem",
    "Get-Content",
    "Test-Path",
    "Get-Item",
    "Select-String",
]


class _FakeProcess:
    def __init__(
        self,
        stdout: object,
        stderr: object,
        *,
        returncode: int = 0,
        timeout_once: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout_once = timeout_once
        self.killed = False
        self.wait_timeouts: list[int | None] = []

    def wait(self, timeout: int | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.timeout_once and not self.killed:
            self.timeout_once = False
            raise subprocess.TimeoutExpired(cmd='pwsh.exe', timeout=timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _RepeatingStream:
    def __init__(self, chunk: bytes, repeats: int) -> None:
        self.chunk = chunk
        self.remaining = repeats
        self.bytes_read = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self.remaining == 0:
            return b''
        self.remaining -= 1
        self.bytes_read += len(self.chunk)
        return self.chunk

    def close(self) -> None:
        self.closed = True


class _ChunkStream:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = iter(chunks)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return next(self.chunks, b'')

    def close(self) -> None:
        self.closed = True


def make_workspace(
    tmp_path: Path,
    *,
    allowed_commands: list[str] | None = None,
    maximum_arguments: int = 20,
    allow_environment_variables: bool = False,
    allow_wildcards: bool = False,
) -> Workspace:
    root = tmp_path / "workspace"
    recycle_root = tmp_path / "recycle"
    root.mkdir(exist_ok=True)
    recycle_root.mkdir(exist_ok=True)
    return Workspace(
        alias="test",
        root=root,
        recycle_root=recycle_root,
        permissions={"execute_command": True},
        policy={},
        command_policy={
            "allowed_commands": allowed_commands or ALLOWED_COMMANDS,
            "maximum_arguments": maximum_arguments,
            "allow_environment_variables": allow_environment_variables,
            "allow_wildcards": allow_wildcards,
        },
    )


def make_config(tmp_path: Path, workspace: Workspace) -> AppConfig:
    return AppConfig(
        host="127.0.0.1",
        port=8000,
        log_directory=tmp_path / "logs",
        default_timeout_seconds=20,
        maximum_timeout_seconds=30,
        maximum_stdout_characters=5,
        maximum_stderr_characters=4,
        workspaces={workspace.alias: workspace},
    )


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        (
            "Get-ChildItem",
            ["-LiteralPath", r"notes", "-File", "-Recurse", "-Name"],
        ),
        ("Get-Content", ["-LiteralPath", r"notes\readme.txt", "-Raw"]),
        (
            "Get-Content",
            ["-LiteralPath", r"notes\readme.txt", "-TotalCount", "25"],
        ),
        ("Test-Path", ["-LiteralPath", r"notes", "-PathType", "Container"]),
        ("Get-Item", ["-LiteralPath", r"notes\readme.txt"]),
        (
            "Select-String",
            [
                "-LiteralPath",
                r"notes\readme.txt",
                "-Pattern",
                "needle",
                "-SimpleMatch",
                "-CaseSensitive",
            ],
        ),
    ],
)
def test_validate_command_accepts_each_approved_parameter_shape(
    tmp_path: Path,
    command: str,
    arguments: list[str],
) -> None:
    workspace = make_workspace(tmp_path)

    validate_command(workspace, command, arguments)


def test_validate_command_is_case_insensitive_for_commands_and_parameters(
    tmp_path: Path,
) -> None:
    workspace = make_workspace(tmp_path)

    validate_command(
        workspace,
        "get-childitem",
        ["-literalpath", "notes", "-file"],
    )


@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item",
        "Invoke-Expression",
        "pwsh.exe",
        "powershell.exe",
        "cmd.exe",
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        "Get-ChildItem ",
    ],
)
def test_validate_command_rejects_non_allowlisted_or_executable_commands(
    tmp_path: Path,
    command: str,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(workspace, command, [])

    assert raised.value.code == "COMMAND_DENIED"


def test_config_allowlist_can_further_remove_a_supported_command(
    tmp_path: Path,
) -> None:
    workspace = make_workspace(tmp_path, allowed_commands=["Get-Item"])

    with pytest.raises(PolicyViolation) as raised:
        validate_command(workspace, "Get-ChildItem", [])

    assert raised.value.code == "COMMAND_DENIED"


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("Get-ChildItem", ["-Force"]),
        ("Get-ChildItem", ["-Filter", "file.txt"]),
        ("Get-Content", ["readme.txt"]),
        ("Get-Content", ["-Path", "readme.txt"]),
        ("Test-Path", ["-LiteralPath"]),
        ("Test-Path", ["-LiteralPath", "readme.txt", "-PathType", "Bogus"]),
        ("Get-Item", ["-LiteralPath", "one", "-LiteralPath", "two"]),
        (
            "Select-String",
            ["-LiteralPath", "readme.txt", "-Pattern", "needle"],
        ),
        (
            "Select-String",
            ["-LiteralPath", "readme.txt", "-SimpleMatch"],
        ),
    ],
)
def test_validate_command_rejects_bad_parameter_and_value_shapes(
    tmp_path: Path,
    command: str,
    arguments: list[str],
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(workspace, command, arguments)

    assert raised.value.code in {
        "COMMAND_PARAMETER_DENIED",
        "COMMAND_PARAMETER_VALUE_INVALID",
        "COMMAND_PARAMETER_REQUIRED",
        "COMMAND_PARAMETER_DUPLICATE",
    }


def test_get_child_item_rejects_conflicting_file_and_directory_switches(
    tmp_path: Path,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(workspace, "Get-ChildItem", ["-File", "-Directory"])

    assert raised.value.code == "COMMAND_PARAMETER_CONFLICT"


def test_get_content_rejects_conflicting_raw_and_total_count(
    tmp_path: Path,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Get-Content",
            ["-LiteralPath", "readme.txt", "-Raw", "-TotalCount", "1"],
        )

    assert raised.value.code == "COMMAND_PARAMETER_CONFLICT"


@pytest.mark.parametrize("value", ["-1", "1.5", "100001", "many"])
def test_get_content_total_count_is_a_bounded_integer(
    tmp_path: Path,
    value: str,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Get-Content",
            ["-LiteralPath", "readme.txt", "-TotalCount", value],
        )

    assert raised.value.code == "COMMAND_PARAMETER_VALUE_INVALID"


def test_validate_command_enforces_argument_count(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, maximum_arguments=1)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Get-Item",
            ["-LiteralPath", "readme.txt"],
        )

    assert raised.value.code == "TOO_MANY_ARGUMENTS"


def test_validate_command_enforces_argument_length(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Select-String",
            [
                "-LiteralPath",
                "readme.txt",
                "-Pattern",
                "x" * 1001,
                "-SimpleMatch",
            ],
        )

    assert raised.value.code == "ARGUMENT_TOO_LONG"


@pytest.mark.parametrize(
    "argument",
    [
        "hello\rworld",
        "hello\nworld",
        "hello;world",
        "hello|world",
        "hello>world",
        "hello<world",
        "hello&world",
        "hello`world",
        "$(Get-Item .)",
        "${HOME}",
    ],
)
def test_validate_command_rejects_shell_metacharacters(
    tmp_path: Path,
    argument: str,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Select-String",
            [
                "-LiteralPath",
                "readme.txt",
                "-Pattern",
                argument,
                "-SimpleMatch",
            ],
        )

    assert raised.value.code == "COMMAND_TOKEN_DENIED"


@pytest.mark.parametrize(
    "argument",
    ["$env:TEMP", "$HOME", "%TEMP%", "${env:TEMP}"],
)
def test_validate_command_rejects_environment_syntax(
    tmp_path: Path,
    argument: str,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Select-String",
            [
                "-LiteralPath",
                "readme.txt",
                "-Pattern",
                argument,
                "-SimpleMatch",
            ],
        )

    assert raised.value.code in {
        "COMMAND_TOKEN_DENIED",
        "ENVIRONMENT_EXPANSION_DENIED",
    }


@pytest.mark.parametrize("argument", ["*.txt", "file?.txt", "[ab].txt"])
def test_validate_command_rejects_wildcards_by_default(
    tmp_path: Path,
    argument: str,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Get-Item",
            ["-LiteralPath", argument],
        )

    assert raised.value.code == "COMMAND_WILDCARD_DENIED"


def test_validate_command_honors_explicit_wildcard_policy(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path, allow_wildcards=True)

    validate_command(
        workspace,
        "Select-String",
        [
            "-LiteralPath",
            "readme.txt",
            "-Pattern",
            "needle*",
            "-SimpleMatch",
        ],
    )


@pytest.mark.parametrize(
    ('command', 'arguments'),
    [
        ('Get-Content', ['-LiteralPath', 'data.json', '-Raw']),
        (
            'Select-String',
            [
                '-LiteralPath',
                'data.json',
                '-Pattern',
                'needle',
                '-SimpleMatch',
            ],
        ),
    ],
)
def test_content_commands_enforce_extension_policy_for_existing_files(
    tmp_path: Path,
    command: str,
    arguments: list[str],
) -> None:
    workspace = replace(
        make_workspace(tmp_path),
        policy={
            'allowed_extensions': ['.txt'],
            'maximum_file_size_bytes': 100,
        },
    )
    (workspace.root / 'data.json').write_text('{}', encoding='utf-8')

    with pytest.raises(PolicyViolation) as raised:
        validate_command(workspace, command, arguments)

    assert raised.value.code == 'EXTENSION_DENIED'


@pytest.mark.parametrize(
    ('command', 'arguments'),
    [
        ('Get-Content', ['-LiteralPath', 'large.txt', '-Raw']),
        (
            'Select-String',
            [
                '-LiteralPath',
                'large.txt',
                '-Pattern',
                'needle',
                '-SimpleMatch',
            ],
        ),
    ],
)
def test_content_commands_enforce_size_policy_for_existing_files(
    tmp_path: Path,
    command: str,
    arguments: list[str],
) -> None:
    workspace = replace(
        make_workspace(tmp_path),
        policy={
            'allowed_extensions': ['.txt'],
            'maximum_file_size_bytes': 4,
        },
    )
    (workspace.root / 'large.txt').write_text('12345', encoding='utf-8')

    with pytest.raises(PolicyViolation) as raised:
        validate_command(workspace, command, arguments)

    assert raised.value.code == 'FILE_SIZE_LIMIT_EXCEEDED'


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        (r"C:\Windows\win.ini", "COMMAND_ABSOLUTE_PATH_DENIED"),
        (r"C:Windows\win.ini", "COMMAND_ABSOLUTE_PATH_DENIED"),
        (r"\\server\share\file.txt", "COMMAND_ABSOLUTE_PATH_DENIED"),
        (r"\Windows\win.ini", "COMMAND_ABSOLUTE_PATH_DENIED"),
        ("/etc/passwd", "COMMAND_ABSOLUTE_PATH_DENIED"),
        (r"..\outside.txt", "COMMAND_PATH_TRAVERSAL_DENIED"),
        (r"notes\..\outside.txt", "COMMAND_PATH_TRAVERSAL_DENIED"),
        ("notes/../outside.txt", "COMMAND_PATH_TRAVERSAL_DENIED"),
        (r"notes\file.txt:secret", "COMMAND_PATH_DENIED"),
    ],
)
def test_validate_command_rejects_unsafe_paths(
    tmp_path: Path,
    path: str,
    expected_code: str,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(workspace, "Get-Item", ["-LiteralPath", path])

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "argument",
    [
        "pwsh",
        "pwsh.exe",
        "powershell.exe",
        "cmd",
        "cmd.exe",
        r"tools\helper.exe",
        r"tools\helper.com",
        r"tools\helper.bat",
        r"tools\helper.cmd",
    ],
)
def test_validate_command_rejects_nested_shells_and_executable_arguments(
    tmp_path: Path,
    argument: str,
) -> None:
    workspace = make_workspace(tmp_path)

    with pytest.raises(PolicyViolation) as raised:
        validate_command(
            workspace,
            "Select-String",
            [
                "-LiteralPath",
                "readme.txt",
                "-Pattern",
                argument,
                "-SimpleMatch",
            ],
        )

    assert raised.value.code == "COMMAND_EXECUTABLE_DENIED"


def test_build_wrapper_command_emits_only_validated_parameters_as_syntax() -> None:
    command_line = build_wrapper_command(
        "get-childitem",
        ["-literalpath", "O'Brien.txt", "-file"],
    )

    assert command_line[:7] == [
        "pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "AllSigned",
        "-Command",
    ]
    assert command_line[7] == (
        "& 'Get-ChildItem' -LiteralPath 'O''Brien.txt' -File"
    )


def test_execute_uses_workspace_timeout_minimal_env_and_output_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = make_workspace(tmp_path)
    config = make_config(tmp_path, workspace)
    observed: dict[str, object] = {}
    process = _FakeProcess(
        BytesIO(b'123456789'),
        BytesIO(b'abcdef'),
        returncode=7,
    )

    def fake_popen(command_line: list[str], **kwargs: object) -> _FakeProcess:
        observed['command_line'] = command_line
        observed.update(kwargs)
        return process

    monkeypatch.setattr(subprocess, 'Popen', fake_popen)

    result = execute_restricted_command(
        config,
        workspace,
        "Get-ChildItem",
        ["-LiteralPath", "."],
        timeout_seconds=99,
    )

    assert result == (7, '12345', 'abcd', True)
    assert observed['cwd'] == workspace.root
    assert observed['stdin'] is subprocess.DEVNULL
    assert observed['stdout'] is subprocess.PIPE
    assert observed['stderr'] is subprocess.PIPE
    assert observed['creationflags'] == getattr(
        subprocess,
        'CREATE_NO_WINDOW',
        0,
    )
    assert process.wait_timeouts[0] == 30
    environment = observed['env']
    assert isinstance(environment, dict)
    assert set(environment) <= {
        'PATH',
        'TEMP',
        'TMP',
        'SYSTEMROOT',
        'POWERSHELL_TELEMETRY_OPTOUT',
        'DOTNET_CLI_TELEMETRY_OPTOUT',
    }
    assert environment['TEMP'] == str(workspace.root)
    assert environment['TMP'] == str(workspace.root)
    assert 'USERPROFILE' not in environment
    assert 'command_line' in observed


def test_execute_reports_output_as_not_truncated_at_exact_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = make_workspace(tmp_path)
    config = make_config(tmp_path, workspace)
    process = _FakeProcess(BytesIO(b'12345'), BytesIO(b'abcd'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)

    result = execute_restricted_command(
        config,
        workspace,
        'Get-ChildItem',
        [],
        timeout_seconds=1,
    )

    assert result == (0, '12345', 'abcd', False)


def test_execute_drains_large_output_but_retains_only_character_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = make_workspace(tmp_path)
    config = make_config(tmp_path, workspace)
    stdout = _RepeatingStream(b'x' * 8192, repeats=2048)
    stderr = _RepeatingStream(b'y' * 8192, repeats=1024)
    process = _FakeProcess(stdout, stderr)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)

    result = execute_restricted_command(
        config,
        workspace,
        'Get-ChildItem',
        [],
        timeout_seconds=1,
    )

    assert result == (0, 'xxxxx', 'yyyy', True)
    assert stdout.bytes_read == 16 * 1024 * 1024
    assert stderr.bytes_read == 8 * 1024 * 1024


def test_execute_decodes_utf8_split_across_stream_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = make_workspace(tmp_path)
    config = replace(
        make_config(tmp_path, workspace),
        maximum_stdout_characters=2,
    )
    stdout = _ChunkStream(
        (b'\xe2', b'\x82', b'\xac\xe2\x82', b'\xac\xe2\x82\xac'),
    )
    process = _FakeProcess(stdout, BytesIO())
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)

    result = execute_restricted_command(
        config,
        workspace,
        'Get-ChildItem',
        [],
        timeout_seconds=1,
    )

    assert result == (0, '€€', '', True)


def test_execute_translates_subprocess_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = make_workspace(tmp_path)
    config = make_config(tmp_path, workspace)
    process = _FakeProcess(BytesIO(), BytesIO(), timeout_once=True)
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: process)

    with pytest.raises(TimeoutError, match='PowerShell command timed out'):
        execute_restricted_command(
            config,
            workspace,
            'Get-ChildItem',
            [],
            timeout_seconds=1,
        )

    assert process.killed is True
    assert process.wait_timeouts == [1, None]


def test_execute_translates_subprocess_startup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = make_workspace(tmp_path)
    config = make_config(tmp_path, workspace)

    def fail_to_start(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError('pwsh.exe is unavailable')

    monkeypatch.setattr(subprocess, 'Popen', fail_to_start)

    with pytest.raises(
        RuntimeError,
        match=r'^PowerShell process could not be started\.$',
    ):
        execute_restricted_command(
            config,
            workspace,
            'Get-ChildItem',
            [],
            timeout_seconds=1,
        )


@pytest.mark.skipif(
    shutil.which("pwsh.exe") is None,
    reason="PowerShell 7 is not installed",
)
def test_live_powershell_smoke(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    config = replace(
        make_config(tmp_path, workspace),
        maximum_stdout_characters=100,
        maximum_stderr_characters=100,
    )

    exit_code, stdout, stderr, output_truncated = execute_restricted_command(
        config,
        workspace,
        "Test-Path",
        ["-LiteralPath", ".", "-PathType", "Container"],
        timeout_seconds=10,
    )

    assert exit_code == 0
    assert stdout.strip().casefold() == "true"
    assert stderr == ""
    assert output_truncated is False
