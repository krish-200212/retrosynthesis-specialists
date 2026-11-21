"""Offline public-tool lifecycle checks; no remote chemistry jobs are submitted."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from retro_specialists.science import MCPTransport, ScienceError, TOOLS, decode_result, run_job


REQUEST = {"job_name": "retrosynthesis", "experiment_data": {"target_mol": "CCO"}}


class ScienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "run"
        self.calls = []

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "boltzmann_submit":
            self.assertTrue((self.folder / "claim.json").exists())
            return {"doc_id": "receipt-1", "job_name": "retrosynthesis"}
        if tool == "boltzmann_status":
            state = json.loads((self.folder / "state.json").read_text())
            self.assertEqual(state["doc_id"], "receipt-1")
            return {"status": "completed", "doc_id": "receipt-1", "data": {"downloadlink": ["https://fixture.invalid/result"]}}
        if tool == "boltzmann_download":
            path = Path(args["output_folder"]) / "routes.json"
            path.write_text('{"top0":{"path":{"missing":"None"},"idx":0}}')
            return {"output_paths": [str(path)]}
        raise AssertionError(tool)

    async def test_complete_preserves_raw_public_artifact(self):
        result = await run_job(self.call, REQUEST, self.folder)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifact_status"], "available")
        self.assertEqual(len(result["artifacts"]), 1)
        content = Path(result["artifacts"][0]["path"]).read_text()
        self.assertEqual(content, '{"top0":{"path":{"missing":"None"},"idx":0}}')
        self.assertEqual(len(list(self.folder.glob("raw-*.json"))), 3)

    async def test_repeat_submit_refuses_without_remote_call(self):
        await run_job(self.call, REQUEST, self.folder)
        before = len(self.calls)
        with self.assertRaises(ScienceError):
            await run_job(self.call, REQUEST, self.folder)
        self.assertEqual(len(self.calls), before)

    async def test_submission_error_is_unknown_and_not_retried(self):
        async def broken(tool, args):
            self.calls.append(tool)
            raise ConnectionError("private server diagnostic")
        result = await run_job(broken, REQUEST, self.folder)
        self.assertEqual(result["state"], "submission_unknown")
        self.assertNotIn("private server diagnostic", json.dumps(result))
        self.assertEqual(self.calls, ["boltzmann_submit"])

    async def test_invalid_submit_response_saved_before_validation(self):
        async def broken(tool, args):
            return {"unexpected": "receipt missing"}
        result = await run_job(broken, REQUEST, self.folder)
        self.assertEqual(result["state"], "submission_unknown")
        self.assertEqual(json.loads(next(self.folder.glob("raw-*.json")).read_text())["response"],
                         {"unexpected": "receipt missing"})

    async def test_status_error_retains_accepted_receipt(self):
        async def broken(tool, args):
            if tool == "boltzmann_submit":
                return {"doc_id": "receipt-1"}
            raise TimeoutError()
        result = await run_job(broken, REQUEST, self.folder)
        self.assertEqual(result["state"], "observation_unknown")
        self.assertEqual(result["doc_id"], "receipt-1")

    async def test_unrecognized_status_raw_saved(self):
        async def response(tool, args):
            return {"doc_id": "receipt-1"} if tool == "boltzmann_submit" else {"status": "mystery"}
        result = await run_job(response, REQUEST, self.folder)
        self.assertEqual(result["state"], "observation_unknown")
        self.assertEqual(len(list(self.folder.glob("raw-*.json"))), 2)

    async def test_explicit_observe_never_submits(self):
        async def observe(tool, args):
            self.assertEqual(tool, "boltzmann_status")
            self.assertEqual(args["doc_id"], "known-old-receipt")
            return {"status": "completed", "data": {"output": {"predictions": []}}}
        result = await run_job(observe, REQUEST, self.folder, action="status", doc_id="known-old-receipt")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifact_status"], "not_requested")
        with self.assertRaises(ScienceError):
            await run_job(observe, REQUEST, self.folder, action="submit")

    async def test_submit_with_existing_doc_id_refused(self):
        with self.assertRaises(ScienceError):
            await run_job(self.call, REQUEST, self.folder, doc_id="already-accepted")
        self.assertEqual(self.calls, [])

    async def test_mismatched_submission_identity_is_not_accepted(self):
        async def wrong(tool, args):
            return {"doc_id": "wrong-job", "job_name": "forward_reaction"}
        result = await run_job(wrong, REQUEST, self.folder)
        self.assertEqual(result["state"], "submission_unknown")
        self.assertIsNone(result["doc_id"])

    async def test_resume_existing_receipt(self):
        async def broken(tool, args):
            return {"doc_id": "receipt-1"} if tool == "boltzmann_submit" else {"status": "pending"}
        await run_job(broken, REQUEST, self.folder)
        result = await run_job(self.call, REQUEST, self.folder, action="observe")
        self.assertEqual(result["state"], "completed")
        self.assertFalse(any(tool == "boltzmann_submit" for tool, args in self.calls))

    async def test_resume_missing_or_different_receipt_refused(self):
        with self.assertRaises(ScienceError):
            await run_job(self.call, REQUEST, self.folder, action="observe")
        await run_job(self.call, REQUEST, self.folder)
        with self.assertRaises(ScienceError):
            await run_job(self.call, REQUEST, self.folder, action="observe", doc_id="different")

    async def test_timeout_and_cancellation_do_not_claim_remote_stop(self):
        async def hanging(tool, args):
            if tool == "boltzmann_submit":
                return {"doc_id": "receipt-1"}
            await asyncio.sleep(10)
        result = await run_job(hanging, REQUEST, self.folder, timeout_seconds=0.01)
        self.assertEqual(result["state"], "observation_unknown")
        other = self.folder.parent / "cancel"
        task = asyncio.create_task(run_job(hanging, REQUEST, other))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = json.loads((other / "state.json").read_text())
        self.assertEqual(state["state"], "observation_unknown")
        self.assertEqual(state["reason"], "local_cancelled_remote_state_unknown")

    async def test_remote_failure_only_from_explicit_status(self):
        async def failed(tool, args):
            return {"doc_id": "receipt-1"} if tool == "boltzmann_submit" else {"status": "failed"}
        result = await run_job(failed, REQUEST, self.folder)
        self.assertEqual(result["state"], "failed")

    async def test_completed_refresh_timeout_preserves_outcome_and_artifacts(self):
        previous = await run_job(self.call, REQUEST, self.folder)
        async def timeout(tool, args):
            self.assertEqual(tool, "boltzmann_status")
            await asyncio.sleep(10)
        result = await run_job(timeout, REQUEST, self.folder, action="observe", timeout_seconds=0.01)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifacts"], previous["artifacts"])
        self.assertEqual(result["artifact_status"], "available")
        self.assertEqual(result["status_response"], previous["status_response"])
        self.assertEqual(result["observation_status"], "unknown")
        self.assertEqual(result["observation_issue"], "TimeoutError")
        self.assertIsNone(result["reason"])

    async def test_completed_then_failed_status_records_conflict(self):
        previous = await run_job(self.call, REQUEST, self.folder)
        async def failed(tool, args):
            return {"status": "failed", "doc_id": "receipt-1"}
        result = await run_job(failed, REQUEST, self.folder, action="observe")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifacts"], previous["artifacts"])
        self.assertEqual(result["status_response"], previous["status_response"])
        self.assertEqual(result["observation_status"], "conflict")
        self.assertEqual(result["observation_issue"], "terminal_status_conflict")
        self.assertEqual(result["latest_observed_state"], "failed")
        saved = json.loads((self.folder / result["latest_status_response"]).read_text())
        self.assertEqual(saved["response"]["status"], "failed")

    async def test_failed_refresh_timeout_preserves_failed_outcome(self):
        async def failed(tool, args):
            return {"doc_id": "receipt-1"} if tool == "boltzmann_submit" else {"status": "failed"}
        previous = await run_job(failed, REQUEST, self.folder)
        async def timeout(tool, args):
            raise TimeoutError()
        result = await run_job(timeout, REQUEST, self.folder, action="observe")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["status_response"], previous["status_response"])
        self.assertEqual(result["observation_issue"], "TimeoutError")

    async def test_failed_then_completed_status_records_conflict(self):
        async def failed(tool, args):
            return {"doc_id": "receipt-1"} if tool == "boltzmann_submit" else {"status": "failed"}
        previous = await run_job(failed, REQUEST, self.folder)
        result = await run_job(self.call, REQUEST, self.folder, action="observe")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["observation_status"], "conflict")
        self.assertEqual(result["status_response"], previous["status_response"])
        self.assertFalse(any(tool == "boltzmann_download" for tool, args in self.calls))

    async def test_status_only_refresh_preserves_existing_artifact_evidence(self):
        previous = await run_job(self.call, REQUEST, self.folder)
        result = await run_job(self.call, REQUEST, self.folder, action="status")
        self.assertEqual(result["artifact_status"], "available")
        self.assertEqual(result["artifacts"], previous["artifacts"])

    async def test_download_refresh_failure_preserves_available_artifacts(self):
        previous = await run_job(self.call, REQUEST, self.folder)
        async def broken(tool, args):
            if tool == "boltzmann_download":
                raise TimeoutError()
            return await self.call(tool, args)
        result = await run_job(broken, REQUEST, self.folder, action="observe")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifact_status"], "available")
        self.assertEqual(result["artifacts"], previous["artifacts"])
        self.assertEqual(result["observation_status"], "confirmed")
        self.assertEqual(result["artifact_refresh_status"], "unavailable")
        self.assertEqual(result["artifact_issue"], "TimeoutError")

    async def test_refresh_without_download_links_preserves_available_artifacts(self):
        previous = await run_job(self.call, REQUEST, self.folder)
        async def missing(tool, args):
            return {"status": "completed", "doc_id": "receipt-1"}
        result = await run_job(missing, REQUEST, self.folder, action="observe")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifact_status"], "available")
        self.assertEqual(result["artifacts"], previous["artifacts"])
        self.assertEqual(result["artifact_refresh_status"], "missing")

    async def test_completed_observation_cancellation_preserves_terminal_state(self):
        previous = await run_job(self.call, REQUEST, self.folder)
        entered = asyncio.Event()
        async def pending(tool, args):
            entered.set()
            await asyncio.sleep(10)
        task = asyncio.create_task(run_job(pending, REQUEST, self.folder, action="observe"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = json.loads((self.folder / "state.json").read_text())
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifacts"], previous["artifacts"])
        self.assertEqual(result["observation_issue"], "local_observation_cancelled")

    async def test_download_failure_preserves_completed_state_and_raw(self):
        async def broken(tool, args):
            if tool == "boltzmann_download":
                return {"errors": ["file missing"]}
            return await self.call(tool, args)
        result = await run_job(broken, REQUEST, self.folder)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["artifact_status"], "unavailable")
        self.assertEqual(len(list(self.folder.glob("raw-*.json"))), 3)

    async def test_download_path_escape_rejected(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("secret unrelated file")
        async def broken(tool, args):
            if tool == "boltzmann_download":
                return {"output_paths": [str(outside)]}
            return await self.call(tool, args)
        result = await run_job(broken, REQUEST, self.folder)
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(result["artifact_status"], "unavailable")

    async def test_symlink_directory_rejected(self):
        self.folder.symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ScienceError):
            await run_job(self.call, REQUEST, self.folder)

    async def test_different_request_cannot_reuse_run(self):
        await run_job(self.call, REQUEST, self.folder)
        different = {"job_name": "retrosynthesis", "experiment_data": {"target_mol": "CCC"}}
        with self.assertRaises(ScienceError):
            await run_job(self.call, different, self.folder, action="observe")

    async def test_job_and_payload_allowlist(self):
        for request in ({**REQUEST, "job_name": "shell"}, {**REQUEST, "command": "bad"},
                        {"job_name": "retrosynthesis", "experiment_data": {"target_mol": "CCO", "extra": True}}):
            with self.subTest(request=request), self.assertRaises(ScienceError):
                await run_job(self.call, request, self.folder)


class DecodeTests(unittest.TestCase):
    def test_public_json_object(self):
        self.assertEqual(decode_result({"content": [{"type": "text", "text": '{"ok":true}'}]}), {"ok": True})

    def test_invalid_or_conflicting_json_rejected(self):
        values = [
            {"isError": True, "content": []},
            {"content": [{"type": "text", "text": '{"x":1,"x":2}'}]},
            {"content": [{"type": "text", "text": '{"x":NaN}'}]},
            {"content": [{"type": "image", "data": ""}]},
            {"content": [{"type": "text", "text": '{"x":1}'}], "structuredContent": {"x": 2}},
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ScienceError):
                decode_result(value)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = {"server_python": sys.executable, "source_path": str(self.folder),
                       "sci_home": str(self.folder), "runtime_dir": str(self.folder)}
        self.events = []
        self.raw = {"content": [{"type": "text", "text": '{"ok":true}'}]}
        outer = self

        class Session:
            def __init__(self, read, write, read_timeout_seconds):
                outer.events.append(("session_timeout", read_timeout_seconds))
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                outer.events.append("session_closed")
            async def initialize(self):
                outer.events.append("initialized")
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in TOOLS])
            async def call_tool(self, tool, arguments):
                outer.events.append((tool, arguments))
                return SimpleNamespace(model_dump=lambda **kwargs: outer.raw)

        @asynccontextmanager
        async def stdio(parameters, errlog):
            outer.events.append(("spawn", parameters))
            try:
                yield None, None
            finally:
                outer.events.append("process_closed")

        mcp = ModuleType("mcp")
        mcp.ClientSession = Session
        mcp.StdioServerParameters = lambda **kwargs: SimpleNamespace(**kwargs)
        client, stdio_module = ModuleType("mcp.client"), ModuleType("mcp.client.stdio")
        stdio_module.stdio_client = stdio
        self.modules = {"mcp": mcp, "mcp.client": client, "mcp.client.stdio": stdio_module}

    async def test_fixed_module_profile_environment_and_cleanup(self):
        with patch.dict(sys.modules, self.modules):
            async with MCPTransport(self.config, self.folder / "receipts", timeout_seconds=12) as transport:
                result = await transport.call("boltzmann_status", {"doc_id": "known"})
                self.assertEqual(result, {"ok": True})
                with self.assertRaises(ScienceError):
                    await transport.call("shell", {})
        spawned = next(event[1] for event in self.events if isinstance(event, tuple) and event[0] == "spawn")
        self.assertEqual(spawned.args, ["-m", "plugins.boltzmann.boltzmann_mcp_server"])
        self.assertEqual(spawned.env["SCI_HOME"], str(self.folder))
        self.assertNotIn("OPENAI_API_KEY", spawned.env)
        self.assertIn(("session_timeout", 12), self.events)
        self.assertEqual(self.events[-2:], ["session_closed", "process_closed"])

    async def test_raw_sdk_error_is_saved_before_decoding(self):
        self.raw = {"isError": True, "content": [{"type": "text", "text": "diagnostic"}]}
        with patch.dict(sys.modules, self.modules):
            async with MCPTransport(self.config, self.folder / "receipts") as transport:
                with self.assertRaises(ScienceError):
                    await transport.call("boltzmann_status", {"doc_id": "known"})
        saved = list((self.folder / "receipts").glob("mcp-raw-*.json"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0].read_text())["response"], self.raw)

    async def test_missing_public_tool_cleans_up_startup(self):
        async def missing(_):
            return SimpleNamespace(tools=[])
        self.modules["mcp"].ClientSession.list_tools = missing
        with patch.dict(sys.modules, self.modules):
            with self.assertRaises(ScienceError):
                async with MCPTransport(self.config, self.folder / "receipts"):
                    self.fail("Must not enter unavailable transport")
        self.assertEqual(self.events[-2:], ["session_closed", "process_closed"])


if __name__ == "__main__":
    unittest.main()
