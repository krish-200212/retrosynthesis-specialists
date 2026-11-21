"""Bounded, auditable provider processes. This module does not validate chemistry.

Codex 0.171.0 exposes ``--ignore-user-config``, ``--strict-config`` and the
``shell_tool`` feature locally. The top-level ``web_search='disabled'`` setting
is documented at https://developers.openai.com/codex/config-reference/ .
Unsupported controls cause CLI failure; no alternate model/provider is selected.
Literature retrieval belongs to the backend, outside this tool-disabled LLM call.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import signal
import tempfile
import time


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    model: str
    timeout_seconds: float = 180
    executable: str = "codex"
    provider: str = "codex"
    external_command: tuple[str, ...] = ()
    max_output_bytes: int = 1_048_576
    max_input_bytes: int = 1_048_576
    reasoning_effort: str | None = None

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip() or self.model != self.model.strip():
            raise ValueError("An explicit nonempty model is required")
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 1800):
            raise ValueError("Provider timeout must be finite and within (0, 1800]")
        if self.provider not in ("codex", "external"):
            raise ValueError("Provider must be codex or external")
        if self.reasoning_effort is not None and self.reasoning_effort not in ("low", "medium", "high"):
            raise ValueError("Reasoning effort must be low, medium, high or unspecified")
        if not isinstance(self.executable, str) or not self.executable or '\0' in self.executable:
            raise ValueError("Invalid provider executable")
        if not isinstance(self.external_command, tuple) or any(
                not isinstance(v, str) or not v or '\0' in v for v in self.external_command):
            raise ValueError("External command must be an immutable administrator-supplied argv")
        if self.provider == "external" and not self.external_command:
            raise ValueError("External provider requires a configured command")
        if self.provider == "codex" and self.external_command:
            raise ValueError("A Codex provider cannot also select an external command")
        for value in (self.max_output_bytes, self.max_input_bytes):
            if type(value) is not int or not 0 < value <= 16_777_216:
                raise ValueError("Provider byte limits must be integers within 1..16777216")


def codex_command(config: ProviderConfig, schema: Path, response: Path) -> list[str]:
    """Only trusted configuration becomes argv; supplied molecular data use stdin."""
    command = [config.executable, "exec", "--ignore-user-config", "--ignore-rules",
               "--strict-config", "--ephemeral", "--sandbox", "read-only",
               "--skip-git-repo-check", "--model", config.model, "--json",
               "--output-schema", str(schema), "--output-last-message", str(response)]
    settings = ['web_search="disabled"', 'mcp_servers={}', 'project_doc_max_bytes=0',
                'approval_policy="never"', 'features.shell_tool=false',
                'features.apps=false', 'features.plugins=false', 'features.hooks=false',
                'features.multi_agent=false', 'features.multi_agent_v2=false',
                'features.browser_use=false', 'features.computer_use=false',
                'features.image_generation=false', 'features.view_image=false',
                'features.memories=false', 'features.skill_search=false',
                'features.skip_host_skill_discovery=true', 'features.goals=false',
                'features.sleep_tool=false', 'features.shell_snapshot=false']
    for setting in settings:
        command += ["-c", setting]
    if config.reasoning_effort is not None:
        command += ["-c", "model_reasoning_effort=" + json.dumps(config.reasoning_effort)]
    return [*command, "-"]


def codex_schema(schema: dict) -> dict:
    """Remove Codex's unsupported uniqueItems keyword only for transport.

    The original schema remains authoritative for backend validation. Property
    names and example/default data are not schema keywords and remain intact.
    """
    schema_maps = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
    schema_nodes = {"items", "additionalProperties", "unevaluatedProperties", "contains",
                    "if", "then", "else", "not", "propertyNames", "unevaluatedItems"}
    schema_arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    def visit(node):
        if not isinstance(node, dict):
            return deepcopy(node)
        result = {}
        for key, value in node.items():
            if key == "uniqueItems":
                continue
            if key in schema_maps and isinstance(value, dict):
                result[key] = {name: visit(child) for name, child in value.items()}
            elif key in schema_nodes:
                result[key] = visit(value)
            elif key in schema_arrays and isinstance(value, list):
                result[key] = [visit(child) for child in value]
            else:
                result[key] = deepcopy(value)
        return result
    return visit(schema)


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _write(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)


class OutputLimit(Exception):
    pass


class CleanupFailure(Exception):
    pass


async def _read(stream, path: Path, limit: int) -> bytes:
    chunks, size = [], 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        while chunk := await stream.read(min(65536, limit + 1)):
            permitted = chunk[:max(0, limit - size)]
            handle.write(permitted)
            handle.flush()
            chunks.append(permitted)
            size += len(chunk)
            if size > limit:
                raise OutputLimit("Provider output exceeds configured limit")
    return b"".join(chunks)


async def _send(process, payload: bytes) -> None:
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # The exit code remains authoritative; broken input is not success.
    finally:
        process.stdin.close()


async def _watch_file(path: Path, limit: int) -> None:
    while True:
        if path.exists() and path.stat().st_size > limit:
            raise OutputLimit("Provider response file exceeds configured limit")
        await asyncio.sleep(.05)


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


async def _reap(process) -> None:
    # Signal the entire session even when the leader exited and a descendant
    # still owns stdout/stderr. No executor shutdown can wait on that child.
    _signal_group(process.pid, signal.SIGTERM)
    await asyncio.sleep(.05)
    _signal_group(process.pid, signal.SIGKILL)
    async def drain(stream):
        while await stream.read(65536):
            pass
    # Readers that stopped at their byte cap must be drained after termination,
    # otherwise an asyncio pipe can remain paused and keep its transport alive.
    await asyncio.wait_for(asyncio.gather(drain(process.stdout), drain(process.stderr)), 2)
    await asyncio.wait_for(process.wait(), 2)


async def _execute(command, payload, cwd, receipt, response, config):
    process, tasks = None, []
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd, start_new_session=True)
        tasks = [asyncio.create_task(_read(process.stdout, receipt / "stdout.txt", config.max_output_bytes)),
                 asyncio.create_task(_read(process.stderr, receipt / "stderr.txt", config.max_output_bytes)),
                 asyncio.create_task(_send(process, payload)), asyncio.create_task(process.wait())]
        watcher = asyncio.create_task(_watch_file(response, config.max_output_bytes))
        tasks.append(watcher)
        joined = asyncio.gather(*tasks[:4])
        tasks.append(joined)
        done, _ = await asyncio.wait((joined, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        stdout, _, _, code = await joined
        return stdout, code
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None:
            try:
                await _reap(process)
            except (TimeoutError, OSError) as error:
                raise CleanupFailure("Provider cleanup could not be confirmed") from error


def _codex_usage(stdout: bytes):
    usage = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            supplied = event.get("usage")
            if isinstance(supplied, dict):
                usage = {k: v for k, v in supplied.items()
                         if isinstance(k, str) and type(v) is int and v >= 0}
    return usage or None


def _read_final(path: Path, limit: int) -> bytes:
    if not path.exists():
        raise ValueError("Provider did not write a final response")
    with path.open("rb") as handle:
        result = handle.read(limit + 1)
    if len(result) > limit:
        raise OutputLimit("Provider response file exceeds configured limit")
    return result


async def generate(request: dict, instructions: str, schema: dict,
                   config: ProviderConfig, receipt_dir: Path) -> dict:
    """Run one fresh call, returning transport provenance and unvalidated text.

    Transport failures return a failure receipt; cancellation persists a receipt
    then propagates CancelledError. JSON/chemical validation belongs downstream.
    The caller supplies a new receipt directory for every attempt and retry.
    """
    if not isinstance(config, ProviderConfig) or not isinstance(request, dict) or not isinstance(schema, dict):
        raise ValueError("ProviderConfig and dictionary request/schema are required")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("Instructions must be nonempty text")
    handoff = {"request": request, "instructions": instructions, "schema": schema, "model": config.model,
               "reasoning_effort": config.reasoning_effort}
    payload = _json(handoff)
    if len(payload) > config.max_input_bytes:
        raise ValueError("Provider input exceeds configured limit")
    receipt = Path(receipt_dir).absolute()
    if receipt.is_symlink():
        raise ValueError("Receipt directory cannot be a symlink")
    receipt.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(receipt / "request.json", _json(request))  # Exclusive claim prevents overwritten evidence.
    _write(receipt / "instructions.txt", instructions.encode())
    _write(receipt / "schema.json", _json(schema))
    effective_schema = codex_schema(schema) if config.provider == "codex" else deepcopy(schema)
    _write(receipt / "effective_provider_schema.json", _json(effective_schema))
    start, started = time.monotonic(), datetime.now(timezone.utc).isoformat()
    status, error, raw, stdout, code, cancelled = "provider_error", None, b"", b"", None, False
    reaped = True
    try:
        with tempfile.TemporaryDirectory(prefix="provider-", dir=receipt) as work:
            final = Path(work) / "final.json"
            command = (codex_command(config, receipt / "effective_provider_schema.json", final)
                       if config.provider == "codex" else list(config.external_command))
            _write(receipt / "command.json", _json(command))
            async with asyncio.timeout(config.timeout_seconds):
                stdout, code = await _execute(command, payload, work, receipt, final, config)
            if code != 0:
                error = "Provider process exited unsuccessfully"
            else:
                raw = _read_final(final, config.max_output_bytes) if config.provider == "codex" else stdout
                if not raw.strip():
                    error = "Provider returned an empty response"
                else:
                    raw.decode("utf-8", errors="strict")
                    status = "completed"
    except asyncio.CancelledError:
        status, error, cancelled = "cancelled", "Caller cancelled provider call", True
    except TimeoutError:
        status, error = "timeout", "Provider exceeded its bounded deadline"
    except OutputLimit:
        status, error = "output_limit", "Provider output exceeds configured limit"
    except CleanupFailure:
        status, error, reaped = "cleanup_failed", "Provider cleanup could not be confirmed", False
    except (OSError, ValueError):
        error = "Provider could not start or return a valid text response"
    result = {"status": status, "provider": config.provider, "model": config.model,
              "reasoning_effort": config.reasoning_effort,
              "model_identity_source": "explicit_command_selection",
              "independently_reported_model": None,
              "raw_response": raw.decode("utf-8", errors="replace"), "error": error,
              "started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
              "duration_seconds": time.monotonic() - start,
              "request_sha256": sha256(payload).hexdigest(), "response_sha256": sha256(raw).hexdigest(),
              "instructions_sha256": sha256(instructions.encode()).hexdigest(),
              "schema_sha256": sha256(_json(schema)).hexdigest(),
              "effective_schema_sha256": sha256(_json(effective_schema)).hexdigest(),
              "schema_adjustments": (["removed_uniqueItems_for_codex_transport"]
                                     if effective_schema != schema else []),
              "prompt_version": request.get("prompt_version"), "returncode": code,
              "usage": _codex_usage(stdout) if config.provider == "codex" else None,
              "cost": None, "process_reaped": reaped}
    _write(receipt / "response.txt", raw)
    _write(receipt / "receipt.json", _json(result))
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass(frozen=True, slots=True)
class Provider:
    config: ProviderConfig

    async def generate(self, request: dict, instructions: str, schema: dict, receipt_dir: Path) -> dict:
        return await generate(request, instructions, schema, self.config, receipt_dir)
