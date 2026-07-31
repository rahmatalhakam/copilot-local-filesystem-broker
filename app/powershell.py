from __future__ import annotations

import codecs
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import BinaryIO, Literal

from app.config import AppConfig, Workspace
from app.errors import PolicyViolation
from app.security import resolve_workspace_path, validate_extension


MAXIMUM_ARGUMENT_LENGTH = 1000
MAXIMUM_TOTAL_COUNT = 100_000
OUTPUT_READ_SIZE_BYTES = 8192

FORBIDDEN_ARGUMENT_TOKENS = (
    "\0",
    "\r",
    "\n",
    ";",
    "|",
    ">",
    "<",
    "&",
    "`",
    "$(",
    "${",
)
WILDCARD_TOKENS = ("*", "?", "[", "]")
EXECUTABLE_SUFFIXES = frozenset({".exe", ".com", ".bat", ".cmd", ".scr"})
WINDOWS_DEVICE_NAMES = frozenset(
    {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)
_NESTED_SHELL_PATTERN = re.compile(
    r"(?i)(?<![\w.-])(?:pwsh|powershell|cmd)(?:\.exe)?(?![\w.-])"
)
_UNSIGNED_INTEGER_PATTERN = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: Literal["switch", "path", "integer", "enum", "text"]
    allowed_values: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class CommandProfile:
    name: str
    parameters: tuple[ParameterSpec, ...]
    required: frozenset[str] = frozenset()
    conflicts: tuple[frozenset[str], ...] = ()


COMMAND_PROFILES: dict[str, CommandProfile] = {
    "Get-ChildItem": CommandProfile(
        name="Get-ChildItem",
        parameters=(
            ParameterSpec("-LiteralPath", "path"),
            ParameterSpec("-File", "switch"),
            ParameterSpec("-Directory", "switch"),
            ParameterSpec("-Recurse", "switch"),
            ParameterSpec("-Name", "switch"),
        ),
        conflicts=(frozenset({"-file", "-directory"}),),
    ),
    "Get-Content": CommandProfile(
        name="Get-Content",
        parameters=(
            ParameterSpec("-LiteralPath", "path"),
            ParameterSpec("-Raw", "switch"),
            ParameterSpec(
                "-TotalCount",
                "integer",
                minimum=0,
                maximum=MAXIMUM_TOTAL_COUNT,
            ),
        ),
        required=frozenset({"-literalpath"}),
        conflicts=(frozenset({"-raw", "-totalcount"}),),
    ),
    "Test-Path": CommandProfile(
        name="Test-Path",
        parameters=(
            ParameterSpec("-LiteralPath", "path"),
            ParameterSpec(
                "-PathType",
                "enum",
                allowed_values=("Any", "Container", "Leaf"),
            ),
        ),
        required=frozenset({"-literalpath"}),
    ),
    "Get-Item": CommandProfile(
        name="Get-Item",
        parameters=(ParameterSpec("-LiteralPath", "path"),),
        required=frozenset({"-literalpath"}),
    ),
    "Select-String": CommandProfile(
        name="Select-String",
        parameters=(
            ParameterSpec("-LiteralPath", "path"),
            ParameterSpec("-Pattern", "text"),
            ParameterSpec("-SimpleMatch", "switch"),
            ParameterSpec("-CaseSensitive", "switch"),
        ),
        required=frozenset(
            {"-literalpath", "-pattern", "-simplematch"},
        ),
    ),
}

_PROFILES_BY_CASEFOLD = {
    name.casefold(): profile for name, profile in COMMAND_PROFILES.items()
}


def _violation(code: str, message: str, rule: str) -> PolicyViolation:
    return PolicyViolation(code, message, rule)


def _command_profile(
    workspace: Workspace,
    command: str,
) -> CommandProfile:
    if not isinstance(command, str) or command != command.strip():
        raise _violation(
            "COMMAND_DENIED",
            "The command is not allowlisted.",
            "allowlisted-powershell-commands",
        )

    configured = workspace.command_policy.get("allowed_commands", [])
    configured_names = {
        value.casefold()
        for value in configured
        if isinstance(value, str)
    }
    profile = _PROFILES_BY_CASEFOLD.get(command.casefold())
    if profile is None or profile.name.casefold() not in configured_names:
        raise _violation(
            "COMMAND_DENIED",
            f"The command '{command}' is not allowlisted.",
            "allowlisted-powershell-commands",
        )
    return profile


def _reject_forbidden_argument_syntax(
    workspace: Workspace,
    argument: str,
) -> None:
    if len(argument) > MAXIMUM_ARGUMENT_LENGTH:
        raise _violation(
            "ARGUMENT_TOO_LONG",
            "A command argument exceeds the allowed length.",
            "maximum-command-argument-length",
        )

    if any(token in argument for token in FORBIDDEN_ARGUMENT_TOKENS):
        raise _violation(
            "COMMAND_TOKEN_DENIED",
            "A command argument contains a forbidden shell token.",
            "deny-shell-metacharacters",
        )

    if not workspace.command_policy.get(
        "allow_environment_variables",
        False,
    ) and ("$" in argument or "%" in argument):
        raise _violation(
            "ENVIRONMENT_EXPANSION_DENIED",
            "Environment-variable syntax is not permitted.",
            "deny-environment-expansion",
        )

    if not workspace.command_policy.get(
        "allow_wildcards",
        False,
    ) and any(token in argument for token in WILDCARD_TOKENS):
        raise _violation(
            "COMMAND_WILDCARD_DENIED",
            "Wildcard syntax is not permitted.",
            "deny-command-wildcards",
        )

    candidate = argument.strip().strip("\"'")
    suffix = PureWindowsPath(candidate).suffix.casefold()
    if (
        _NESTED_SHELL_PATTERN.search(candidate) is not None
        or suffix in EXECUTABLE_SUFFIXES
    ):
        raise _violation(
            "COMMAND_EXECUTABLE_DENIED",
            "Nested shells and executable arguments are not permitted.",
            "deny-nested-shells-and-executables",
        )


def _validate_relative_path(workspace: Workspace, value: str) -> None:
    if not value:
        raise _violation(
            "COMMAND_PARAMETER_VALUE_INVALID",
            "A command path value cannot be empty.",
            "command-parameter-profile",
        )

    windows_path = PureWindowsPath(value)
    if (
        value.startswith(("\\", "/"))
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
    ):
        raise _violation(
            "COMMAND_ABSOLUTE_PATH_DENIED",
            "Command path arguments must be relative.",
            "deny-command-absolute-path",
        )

    if ".." in windows_path.parts:
        raise _violation(
            "COMMAND_PATH_TRAVERSAL_DENIED",
            "Command path traversal is not permitted.",
            "deny-command-path-traversal",
        )

    for part in windows_path.parts:
        stem = part.split(".", maxsplit=1)[0].casefold()
        if (
            ":" in part
            or part.endswith((" ", "."))
            or stem in WINDOWS_DEVICE_NAMES
        ):
            raise _violation(
                "COMMAND_PATH_DENIED",
                "The command path uses a disallowed Windows path form.",
                "deny-command-special-paths",
            )

    # Apply the same containment and hidden/reparse checks as structured paths.
    # Missing leaves remain valid because Test-Path is allowed to probe them.
    resolve_workspace_path(
        workspace,
        value,
        must_exist=False,
        allow_root=True,
    )


def _validate_parameter_value(
    workspace: Workspace,
    spec: ParameterSpec,
    value: str,
) -> None:
    if spec.kind == "path":
        _validate_relative_path(workspace, value)
        return

    if spec.kind == "integer":
        if _UNSIGNED_INTEGER_PATTERN.fullmatch(value) is None:
            raise _violation(
                "COMMAND_PARAMETER_VALUE_INVALID",
                f"Parameter '{spec.name}' requires an unsigned integer.",
                "command-parameter-profile",
            )
        parsed = int(value)
        if (
            spec.minimum is not None
            and parsed < spec.minimum
            or spec.maximum is not None
            and parsed > spec.maximum
        ):
            raise _violation(
                "COMMAND_PARAMETER_VALUE_INVALID",
                f"Parameter '{spec.name}' is outside its allowed range.",
                "command-parameter-profile",
            )
        return

    if spec.kind == "enum":
        allowed = {item.casefold() for item in spec.allowed_values}
        if value.casefold() not in allowed:
            raise _violation(
                "COMMAND_PARAMETER_VALUE_INVALID",
                f"Parameter '{spec.name}' has an invalid value.",
                "command-parameter-profile",
            )
        return

    if spec.kind == "text" and not value:
        raise _violation(
            "COMMAND_PARAMETER_VALUE_INVALID",
            f"Parameter '{spec.name}' cannot be empty.",
            "command-parameter-profile",
        )


def _parse_arguments(
    workspace: Workspace,
    profile: CommandProfile,
    arguments: list[str],
) -> list[tuple[ParameterSpec, str | None]]:
    specs = {
        parameter.name.casefold(): parameter
        for parameter in profile.parameters
    }
    parsed: list[tuple[ParameterSpec, str | None]] = []
    seen: set[str] = set()
    index = 0

    while index < len(arguments):
        token = arguments[index]
        spec = specs.get(token.casefold())
        if spec is None:
            raise _violation(
                "COMMAND_PARAMETER_DENIED",
                f"Parameter '{token}' is not approved for {profile.name}.",
                "command-parameter-profile",
            )

        key = spec.name.casefold()
        if key in seen:
            raise _violation(
                "COMMAND_PARAMETER_DUPLICATE",
                f"Parameter '{spec.name}' cannot be repeated.",
                "command-parameter-profile",
            )
        seen.add(key)

        if spec.kind == "switch":
            parsed.append((spec, None))
            index += 1
            continue

        value_index = index + 1
        if value_index >= len(arguments):
            raise _violation(
                "COMMAND_PARAMETER_VALUE_INVALID",
                f"Parameter '{spec.name}' requires a value.",
                "command-parameter-profile",
            )

        value = arguments[value_index]
        if value.casefold() in specs:
            raise _violation(
                "COMMAND_PARAMETER_VALUE_INVALID",
                f"Parameter '{spec.name}' requires a value.",
                "command-parameter-profile",
            )

        _validate_parameter_value(workspace, spec, value)
        parsed.append((spec, value))
        index += 2

    missing = profile.required - seen
    if missing:
        names = ", ".join(sorted(missing))
        raise _violation(
            "COMMAND_PARAMETER_REQUIRED",
            f"Required command parameter(s) missing: {names}.",
            "command-parameter-profile",
        )

    for conflict in profile.conflicts:
        if conflict <= seen:
            raise _violation(
                "COMMAND_PARAMETER_CONFLICT",
                "The command contains mutually exclusive parameters.",
                "command-parameter-profile",
            )
    return parsed


def _enforce_existing_content_file_policy(
    workspace: Workspace,
    profile: CommandProfile,
    parsed: list[tuple[ParameterSpec, str | None]],
) -> None:
    if profile.name not in {'Get-Content', 'Select-String'}:
        return

    literal_path = next(
        (
            value
            for spec, value in parsed
            if spec.name.casefold() == '-literalpath'
        ),
        None,
    )
    if literal_path is None:
        return

    target = resolve_workspace_path(
        workspace,
        literal_path,
        must_exist=False,
        allow_root=True,
    )
    if not target.is_file():
        return

    if 'allowed_extensions' in workspace.policy:
        validate_extension(workspace, target)

    maximum_size = workspace.policy.get('maximum_file_size_bytes')
    if not isinstance(maximum_size, int) or isinstance(maximum_size, bool):
        return
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return
    if size > maximum_size:
        raise _violation(
            'FILE_SIZE_LIMIT_EXCEEDED',
            'The file exceeds the configured read size limit.',
            'maximum-file-size',
        )


def validate_command(
    workspace: Workspace,
    command: str,
    arguments: list[str],
) -> None:
    profile = _command_profile(workspace, command)
    if not isinstance(arguments, list) or any(
        not isinstance(argument, str) for argument in arguments
    ):
        raise _violation(
            "COMMAND_ARGUMENTS_INVALID",
            "Command arguments must be an explicit array of strings.",
            "command-argument-array",
        )

    maximum_arguments = int(
        workspace.command_policy.get("maximum_arguments", 0),
    )
    if len(arguments) > maximum_arguments:
        raise _violation(
            "TOO_MANY_ARGUMENTS",
            "The command has too many arguments.",
            "maximum-command-arguments",
        )

    for argument in arguments:
        _reject_forbidden_argument_syntax(workspace, argument)

    parsed = _parse_arguments(workspace, profile, arguments)
    _enforce_existing_content_file_policy(workspace, profile, parsed)


def _quote_powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_wrapper_command(command: str, arguments: list[str]) -> list[str]:
    profile = _PROFILES_BY_CASEFOLD.get(command.casefold())
    if profile is None:
        raise ValueError("No PowerShell command profile exists for the command.")

    specs = {
        parameter.name.casefold(): parameter
        for parameter in profile.parameters
    }
    invocation = ["&", _quote_powershell_literal(profile.name)]
    index = 0
    while index < len(arguments):
        spec = specs.get(arguments[index].casefold())
        if spec is None:
            raise ValueError("The PowerShell arguments were not validated.")
        invocation.append(spec.name)
        index += 1
        if spec.kind != "switch":
            if index >= len(arguments):
                raise ValueError("The PowerShell arguments were not validated.")
            invocation.append(_quote_powershell_literal(arguments[index]))
            index += 1

    return [
        "pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "AllSigned",
        "-Command",
        " ".join(invocation),
    ]


def _minimal_environment(workspace: Workspace) -> dict[str, str]:
    environment = {
        "PATH": r"C:\Program Files\PowerShell\7",
        "TEMP": str(workspace.root),
        "TMP": str(workspace.root),
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    }
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        environment["SYSTEMROOT"] = system_root
    return environment


def _capture_bounded_utf8(
    stream: BinaryIO,
    maximum_characters: int,
) -> tuple[str, bool]:
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    retained: list[str] = []
    retained_characters = 0
    truncated = False

    def retain(decoded: str) -> None:
        nonlocal retained_characters, truncated
        remaining = maximum_characters - retained_characters
        if len(decoded) > remaining:
            truncated = True
        if remaining > 0:
            portion = decoded[:remaining]
            retained.append(portion)
            retained_characters += len(portion)

    while True:
        chunk = stream.read(OUTPUT_READ_SIZE_BYTES)
        if not chunk:
            break
        retain(decoder.decode(chunk))
    retain(decoder.decode(b'', final=True))
    return ''.join(retained), truncated


def execute_restricted_command(
    app_config: AppConfig,
    workspace: Workspace,
    command: str,
    arguments: list[str],
    timeout_seconds: int,
) -> tuple[int, str, str, bool]:
    validate_command(workspace, command, arguments)
    command_line = build_wrapper_command(command, arguments)
    timeout = max(
        1,
        min(int(timeout_seconds), app_config.maximum_timeout_seconds),
    )

    try:
        process = subprocess.Popen(
            command_line,
            cwd=workspace.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=_minimal_environment(workspace),
        )
    except OSError as exc:
        raise RuntimeError(
            'PowerShell process could not be started.',
        ) from exc

    stdout_stream = process.stdout
    stderr_stream = process.stderr
    if stdout_stream is None or stderr_stream is None:
        process.kill()
        process.wait()
        raise RuntimeError('PowerShell output capture could not be initialized.')

    timeout_error: subprocess.TimeoutExpired | None = None
    try:
        with ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix='broker-powershell-output',
        ) as executor:
            stdout_future = executor.submit(
                _capture_bounded_utf8,
                stdout_stream,
                app_config.maximum_stdout_characters,
            )
            stderr_future = executor.submit(
                _capture_bounded_utf8,
                stderr_stream,
                app_config.maximum_stderr_characters,
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                timeout_error = exc
                process.kill()
                process.wait()

            stdout, stdout_truncated = stdout_future.result()
            stderr, stderr_truncated = stderr_future.result()
    finally:
        stdout_stream.close()
        stderr_stream.close()

    if timeout_error is not None:
        raise TimeoutError('PowerShell command timed out.') from timeout_error

    return (
        process.returncode,
        stdout,
        stderr,
        stdout_truncated or stderr_truncated,
    )
