"""Durable public MCP jobs; no scientific resubmission after uncertain outcomes.

Transport design adapted from the earlier retrosynthesis-team MCP adapter in
this workspace (2026-11-20). This standalone implementation imports none of that
prototype. It uses only the public SDK and three advertised Boltzmann tools.
Raw private responses are persisted before interpretation. Local cancellation
never asserts remote cancellation. Model output and public artifacts are intact.
"""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import stat
from uuid import uuid4

TOOLS = frozenset(("boltzmann_submit", "boltzmann_status", "boltzmann_download"))
MAX_BYTES = 20 * 1024 * 1024


class ScienceError(ValueError):
    """Safe local diagnostic without credentials, URLs or remote exception text."""


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _directory(path):
    path = Path(path).absolute()
    if path == Path("/") or ".." in path.parts:
        raise ScienceError("A dedicated private directory is required")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if os.fstat(descriptor).st_uid != os.getuid():
            raise ScienceError("Private directory must be owned")
        os.fchmod(descriptor, 0o700)
    except OSError as exc:
        raise ScienceError("Unsafe or unavailable private directory") from exc
    finally:
        os.close(descriptor)
    return path


def _write(path, value, *, exclusive=False):
    content = _encode(value)
    if len(content) > MAX_BYTES:
        raise ScienceError("Private response exceeds size limit")
    temporary = path if exclusive else path.parent / (".write-" + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(temporary, path)
        folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        if not exclusive and temporary.exists():
            temporary.unlink()


def _read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_size > MAX_BYTES):
            raise ScienceError("Unsafe private artifact")
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ScienceError("Artifact exceeds size limit")
    return content


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ScienceError("Duplicate JSON key")
        result = {**result, key: value}
    return result


def _invalid(_):
    raise ScienceError("Nonfinite JSON number")


def decode_result(value):
    """Decode one SDK result with strict agreement between its representations."""
    if not isinstance(value, dict) or value.get("isError", False):
        raise ScienceError("MCP error result")
    content = value.get("content", [])
    if not isinstance(content, list) or len(content) > 1:
        raise ScienceError("Ambiguous MCP content")
    parsed = None
    if content:
        block = content[0]
        if not isinstance(block, dict) or block.get("type") != "text":
            raise ScienceError("Unexpected MCP content")
        try:
            parsed = json.loads(block["text"], object_pairs_hook=_pairs, parse_constant=_invalid)
        except (ValueError, TypeError, KeyError) as exc:
            raise ScienceError("Invalid MCP JSON") from exc
    structured = value.get("structuredContent")
    if structured is not None and parsed is not None and _encode(structured) != _encode(parsed):
        raise ScienceError("Conflicting MCP content")
    result = structured if structured is not None else parsed
    if not isinstance(result, dict):
        raise ScienceError("MCP result must be an object")
    _encode(result)
    return result


class MCPTransport:
    """Launch the public server in the explicitly configured private Sci profile.

    Credentials are read only by the existing server. No credential values,
    arbitrary commands, server modules or environment overrides are accepted.
    The managed interpreter running the CLI must already contain the MCP SDK.
    """
    def __init__(self, config, receipt_dir, *, timeout_seconds=1800):
        names = ("server_python", "source_path", "sci_home", "runtime_dir")
        self.config = {name: Path(config[name]).absolute() for name in names}
        if not self.config["server_python"].is_file() or not all(self.config[n].is_dir() for n in names[1:]):
            raise ScienceError("Configured existing runtime is unavailable")
        self.folder = _directory(receipt_dir)
        self.timeout_seconds = timeout_seconds
        self.stack, self.session = None, None

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        c = self.config
        environment = {"HOME": str(c["sci_home"]), "SCI_HOME": str(c["sci_home"]),
            "SCI_RUNTIME_DIR": str(c["runtime_dir"]), "TMPDIR": str(c["runtime_dir"]),
            "PATH": str(c["server_python"].parent) + os.pathsep + os.defpath,
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        self.stack = AsyncExitStack()
        try:
            error_log = self.stack.enter_context(open(os.devnull, "w"))
            params = StdioServerParameters(command=str(c["server_python"]),
                args=["-m", "plugins.boltzmann.boltzmann_mcp_server"],
                cwd=str(c["source_path"]), env=environment)
            read, write = await self.stack.enter_async_context(stdio_client(params, errlog=error_log))
            self.session = await self.stack.enter_async_context(ClientSession(read, write,
                read_timeout_seconds=self.timeout_seconds))
            await self.session.initialize()
            advertised = await self.session.list_tools()
            if not TOOLS <= {tool.name for tool in advertised.tools}:
                raise ScienceError("Required public tools unavailable")
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def call(self, tool, arguments):
        if tool not in TOOLS or self.session is None:
            raise ScienceError("Unapproved tool or inactive transport")
        _encode(arguments)
        result = await self.session.call_tool(tool, arguments=arguments)
        raw = result.model_dump(mode="json")
        _write(self.folder / ("mcp-raw-" + uuid4().hex + ".json"), {"tool": tool, "response": raw})
        return decode_result(raw)

    async def __aexit__(self, *args):
        await self.stack.aclose()


def _request(value):
    if (not isinstance(value, dict) or set(value) != {"job_name", "experiment_data"}
            or value["job_name"] not in ("retrosynthesis", "forward_reaction")):
        raise ScienceError("Unapproved scientific request")
    data = value["experiment_data"]
    if not isinstance(data, dict) or set(data) != {"target_mol"}:
        raise ScienceError("Unsupported scientific parameters")
    target = data["target_mol"]
    if not isinstance(target, str) or not target or len(target) > 4096 or any(s.isspace() for s in target):
        raise ScienceError("A previously validated bounded molecular input is required")
    return json.loads(_encode(value))


def _save(folder, current, **changes):
    value = {**current, **changes, "updated_at": _now()}
    _write(folder / "state.json", value)
    return value


async def _call(caller, folder, tool, arguments):
    response = await caller(tool, arguments)
    filename = "raw-" + uuid4().hex + ".json"
    _write(folder / filename, {"tool": tool, "arguments": arguments, "response": response, "recorded_at": _now()})
    if not isinstance(response, dict):
        raise ScienceError("Response is not an object")
    return response, filename


def _identity(response, state):
    for name, expected in (("job_name", state["job_name"]), ("doc_id", state["doc_id"]),
                           ("docId", state["doc_id"])):
        if name in response and response[name] != expected:
            raise ScienceError("Response identity mismatch")


async def _download(caller, folder, state, response):
    state = _save(folder, state, operation="download", artifact_refresh_status="fetching", artifact_issue=None)
    refs = response.get("data", {}).get("downloadlink") if isinstance(response.get("data"), dict) else None
    if refs in (None, []):
        status = state["artifact_status"] if state["artifacts"] else "missing"
        return _save(folder, state, artifact_status=status, artifact_refresh_status="missing")
    if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(ref, str) for ref in refs):
        raise ScienceError("Malformed download references")
    artifacts = []
    for reference in refs:
        destination = _directory(folder / ("download-" + uuid4().hex))
        downloaded, raw_file = await _call(caller, folder, "boltzmann_download", {
            "download_url": reference, "output_folder": str(destination)})
        paths = downloaded.get("output_paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
            raise ScienceError("No bounded artifact paths returned")
        for item in paths:
            if not isinstance(item, str) or ".." in Path(item).parts:
                raise ScienceError("Invalid artifact path")
            path = Path(item) if Path(item).is_absolute() else destination / item
            if not path.is_relative_to(destination) or path.resolve() != path or not path.is_file():
                raise ScienceError("Artifact path escapes output directory or uses links")
            content = _read(path)
            path.chmod(0o600)
            artifacts.append({"path": str(path), "sha256": sha256(content).hexdigest(),
                              "size_bytes": len(content), "download_response": raw_file})
    return _save(folder, state, artifact_status="available", artifacts=artifacts,
                 artifact_refresh_status="available", artifact_issue=None)


async def _observe(caller, folder, state, download, *, wait_for_terminal=False):
    state = _save(folder, state, operation="observe", observation_status="observing", observation_issue=None)
    while True:
        response, raw_file = await _call(caller, folder, "boltzmann_status", {
            "job_name": state["job_name"], "doc_id": state["doc_id"], "output_folder": str(folder)})
        state = _save(folder, state, latest_status_response=raw_file)
        _identity(response, state)
        status = response.get("status")
        if not wait_for_terminal or response.get("error") or response.get("cancelled"):
            break
        if status not in ("pending", "running", "accepted", "queued", "processing") and not (
            status == "unknown" and response.get("timeout") is True
        ):
            break
        await asyncio.sleep(2)
    observed = "completed" if status in ("completed", "success", "100%completed") else status
    if observed not in ("completed", "failed"):
        raise ScienceError("Status did not establish terminal state")
    if state["state"] in ("completed", "failed") and state["state"] != observed:
        return _save(folder, state, observation_status="conflict",
                     observation_issue="terminal_status_conflict", latest_observed_state=observed)
    state = _save(folder, state, state=observed, reason=None,
                  status_response=state.get("status_response", raw_file),
                  observation_status="confirmed", latest_observed_state=observed)
    if observed == "failed":
        return state
    if not download:
        return state
    return await _download(caller, folder, state, response)


def _prepare(folder, request, action, doc_id):
    digest = sha256(_encode(request)).hexdigest()
    if action == "submit":
        if (folder / "state.json").exists():
            raise ScienceError("Existing job state permits observation only")
        state = {"request_sha256": digest, "job_name": request["job_name"],
            "target": request["experiment_data"]["target_mol"], "doc_id": None,
            "state": "submitting", "reason": None, "started_at": _now(),
            "artifact_status": "not_requested", "artifacts": []}
        try:
            _write(folder / "claim.json", {"request": request, "started_at": _now()}, exclusive=True)
        except FileExistsError as exc:
            raise ScienceError("This run was already admitted; explicit observation only") from exc
        return _save(folder, state)
    if (folder / "state.json").exists():
        state = json.loads(_read(folder / "state.json"))
        if state["request_sha256"] != digest or doc_id is not None and doc_id != state["doc_id"]:
            raise ScienceError("Observation request or receipt changed")
        doc_id = state["doc_id"]
    else:
        state = {"request_sha256": digest, "job_name": request["job_name"],
            "target": request["experiment_data"]["target_mol"], "doc_id": doc_id,
            "state": "observing", "reason": None, "started_at": _now(),
            "artifact_status": "not_requested", "artifacts": []}
    if not isinstance(doc_id, str) or not doc_id or len(doc_id) > 512:
        raise ScienceError("Explicit observation requires an accepted receipt")
    outcome = state["state"] if state["state"] in ("completed", "failed") else "observing"
    return _save(folder, state, state=outcome, observation_status="observing", observation_issue=None)


def _record_failure(folder, latest, error):
    """Observation or retrieval failure cannot erase a confirmed remote outcome."""
    known_terminal = latest["state"] in ("completed", "failed")
    uncertain = "submission_unknown" if latest["doc_id"] is None else "observation_unknown"
    issue = type(error).__name__
    if isinstance(error, asyncio.CancelledError):
        issue = "local_observation_cancelled" if known_terminal else "local_cancelled_remote_state_unknown"
    changes = {"state": latest["state"] if known_terminal else uncertain,
               "reason": latest.get("reason") if known_terminal else issue}
    if latest.get("operation") == "download":
        status = latest["artifact_status"] if latest["artifacts"] else "unavailable"
        changes = {**changes, "artifact_status": status,
                   "artifact_refresh_status": "unavailable", "artifact_issue": issue}
    else:
        changes = {**changes, "observation_status": "unknown", "observation_issue": issue}
    return _save(folder, latest, **changes)


async def run_job(caller, request, run_dir, *, action="submit", doc_id=None,
                  timeout_seconds=1800, download=True):
    """Perform one durable submission, or explicitly observe an accepted job.

    Caller manages total run budgets and sequential admission across run dirs.
    A timeout leaves a known receipt resumable; an uncertain submission cannot
    be retried in this directory. ``status`` never downloads or submits.
    Confirmed completed/failed outcomes survive later observation failures.
    Contradictory terminal observations are recorded separately as conflicts;
    explicit investigation is required before revising the established outcome.
    """
    request = _request(request)
    if action not in ("submit", "observe", "status") or not callable(caller):
        raise ScienceError("Invalid action or caller")
    if action == "submit" and doc_id is not None:
        raise ScienceError("An existing receipt permits observation only")
    if timeout_seconds is not None and (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ScienceError("A finite positive deadline is required")
    folder = _directory(run_dir)
    lock = os.open(folder / "run.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScienceError("This run is already observed by another process") from exc
        state = _prepare(folder, request, action, doc_id)
        try:
            async with asyncio.timeout(timeout_seconds):
                if action == "submit":
                    response, raw_file = await _call(caller, folder, "boltzmann_submit", request)
                    receipt = response.get("doc_id")
                    if not isinstance(receipt, str) or not receipt or len(receipt) > 512:
                        raise ScienceError("Submission lacks accepted receipt")
                    _identity(response, {**state, "doc_id": receipt})
                    state = _save(folder, state, doc_id=receipt, state="observing", submit_response=raw_file)
                return await _observe(caller, folder, state, download and action != "status",
                                      wait_for_terminal=timeout_seconds is None)
        except BaseException as exc:
            if not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise
            latest = json.loads(_read(folder / "state.json"))
            result = _record_failure(folder, latest, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return result
    finally:
        os.close(lock)
