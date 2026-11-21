"""Subprocess boundary tests; every provider here is an offline executable."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from retro_specialists.provider import ProviderConfig, Provider, generate, codex_command, codex_schema


SCHEMA = {"type": "object", "properties": {"routes": {"type": "array"}},
          "required": ["routes"], "additionalProperties": False}


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, code, **kwargs):
        path = self.root / ("fake_" + str(len(list(self.root.glob('fake_*')))) + ".py")
        path.write_text(code)
        return ProviderConfig(model="explicit-test-model", provider="external",
                              external_command=(sys.executable, str(path)), **kwargs)

    async def run_provider(self, config, name="call", request=None):
        return await generate(request or {"target": "C$(touch INJECTION)"},
                              "Return JSON only.", SCHEMA, config, self.root / name)

    async def test_valid_external_and_private_provenance(self):
        config = self.config("import json,sys; x=json.load(sys.stdin); print(json.dumps({'routes': [], 'echo': x['request']['target']}))")
        result = await self.run_provider(config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(json.loads(result["raw_response"])["echo"], "C$(touch INJECTION)")
        self.assertIsNone(result["cost"])
        self.assertIsNone(result["usage"])
        self.assertEqual(result["model"], "explicit-test-model")
        self.assertEqual(len(result["response_sha256"]), 64)
        for name in ("request.json", "instructions.txt", "schema.json", "response.txt", "receipt.json"):
            self.assertTrue((self.root / "call" / name).is_file())
            self.assertEqual((self.root / "call" / name).stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "call" / "INJECTION").exists())

    async def test_empty_response_is_failure_but_empty_routes_is_valid(self):
        result = await self.run_provider(self.config("pass"))
        self.assertEqual(result["status"], "provider_error")
        valid = await self.run_provider(self.config("print('{\"routes\":[]}')"), "second")
        self.assertEqual(valid["status"], "completed")

    async def test_invalid_json_is_preserved_for_backend_validation(self):
        result = await self.run_provider(self.config("print('bad json')"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["raw_response"].strip(), "bad json")

    async def test_failure_does_not_become_empty_success(self):
        result = await self.run_provider(self.config("import sys; print('failure',file=sys.stderr); sys.exit(7)"))
        self.assertEqual(result["status"], "provider_error")
        self.assertEqual(result["returncode"], 7)

    async def test_timeout_is_bounded(self):
        start = time.monotonic()
        result = await self.run_provider(self.config("import time; time.sleep(20)", timeout_seconds=.1))
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, 2)
        self.assertTrue(result["process_reaped"])

    async def test_cancellation_persists_and_reaps(self):
        task = asyncio.create_task(self.run_provider(self.config("import time; time.sleep(20)")))
        while not (self.root / "call" / "request.json").exists():
            await asyncio.sleep(.01)
        await asyncio.sleep(.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        receipt = json.loads((self.root / "call" / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "cancelled")
        self.assertTrue(receipt["process_reaped"])

    async def test_output_limit_applies_to_stdout_and_stderr(self):
        for stream in ("stdout", "stderr"):
            config = self.config(f"import sys; sys.{stream}.write('x'*200000)", max_output_bytes=1000)
            result = await self.run_provider(config, stream)
            self.assertEqual(result["status"], "output_limit")
            self.assertLessEqual((self.root / stream / f"{stream}.txt").stat().st_size, 1000)

    async def test_process_group_child_is_terminated(self):
        pidfile = self.root / "child.pid"
        code = ("import subprocess,sys,time,pathlib\n"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])\n"
                f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
                "time.sleep(20)\n")
        result = await self.run_provider(self.config(code, timeout_seconds=.2))
        self.assertEqual(result["status"], "timeout")
        pid = int(pidfile.read_text())
        status = Path(f"/proc/{pid}/stat")
        self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")

    async def test_concurrent_calls_overlap_and_have_distinct_workdirs(self):
        code = "import json,os,time; time.sleep(.2); print(json.dumps({'cwd':os.getcwd()}))"
        provider = Provider(self.config(code))
        start = time.monotonic()
        a, b = await asyncio.gather(*(
            provider.generate({}, "JSON", SCHEMA, self.root / name) for name in ("a", "b")))
        self.assertLess(time.monotonic() - start, .7)
        self.assertNotEqual(json.loads(a["raw_response"])["cwd"], json.loads(b["raw_response"])["cwd"])

    async def test_existing_receipt_cannot_be_overwritten(self):
        config = self.config("print('{}')")
        await self.run_provider(config)
        with self.assertRaises(FileExistsError):
            await self.run_provider(config)

    async def test_missing_executable_recorded(self):
        config = ProviderConfig(model="specified", provider="external", external_command=("/not/an/executable",))
        result = await self.run_provider(config)
        self.assertEqual(result["status"], "provider_error")
        self.assertIsNone(result["returncode"])

    async def test_codex_final_file_and_measured_usage(self):
        executable = self.root / "fake-codex"
        executable.write_text(f"#!{sys.executable}\n" +
            "import json,pathlib,sys\n"
            "x=json.load(sys.stdin)\n"
            "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{\"routes\":[]}')\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':3}}))\n")
        executable.chmod(0o700)
        result = await self.run_provider(ProviderConfig(model="chosen", executable=str(executable)))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 3})
        self.assertEqual(result["raw_response"], '{"routes":[]}')

    async def test_codex_missing_final_is_failure(self):
        executable = self.root / "fake-codex"
        executable.write_text(f"#!{sys.executable}\nprint('no final file')\n")
        executable.chmod(0o700)
        result = await self.run_provider(ProviderConfig(model="chosen", executable=str(executable)))
        self.assertEqual(result["status"], "provider_error")

    async def test_bad_inputs_fail_before_spawn(self):
        config = self.config("raise RuntimeError('must not run')", max_input_bytes=30)
        with self.assertRaises(ValueError):
            await self.run_provider(config)
        self.assertFalse((self.root / "call").exists())
        with self.assertRaises(ValueError):
            await generate([], "text", SCHEMA, config, self.root / "call")
        with self.assertRaises(ValueError):
            await generate({}, "", SCHEMA, config, self.root / "call")

    async def test_receipt_symlink_rejected(self):
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            await self.run_provider(self.config("print('{}')"), "alias")

    async def test_progress_logs_are_visible_while_provider_runs(self):
        config = self.config("import time; print('started',flush=True); time.sleep(20)")
        task = asyncio.create_task(self.run_provider(config))
        try:
            for _ in range(40):
                path = self.root / "call" / "stdout.txt"
                if path.exists() and path.read_text():
                    break
                await asyncio.sleep(.01)
            self.assertEqual(path.read_text(), "started\n")
            self.assertFalse(task.done())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_codex_schema_removes_only_unique_items_keyword(self):
        schema = {"type": "object", "properties": {
            "uniqueItems": {"type": "string"},
            "routes": {"type": "array", "uniqueItems": True, "maxItems": 3,
                       "items": {"type": "object", "properties": {
                           "evidence_ids": {"type": "array", "uniqueItems": True,
                                            "items": {"type": "string", "minLength": 1}}}}}},
            "examples": [{"uniqueItems": True}]}
        original = json.dumps(schema, sort_keys=True)
        effective = codex_schema(schema)
        self.assertEqual(json.dumps(schema, sort_keys=True), original)
        self.assertNotIn("uniqueItems", effective["properties"]["routes"])
        self.assertNotIn("uniqueItems", effective["properties"]["routes"]["items"]["properties"]["evidence_ids"])
        self.assertEqual(effective["properties"]["uniqueItems"], {"type": "string"})
        self.assertEqual(effective["examples"], [{"uniqueItems": True}])
        self.assertEqual(effective["properties"]["routes"]["maxItems"], 3)

    async def test_codex_uses_effective_schema_and_records_original(self):
        executable = self.root / "fake-codex"
        executable.write_text(f"#!{sys.executable}\n" +
            "import json,pathlib,sys\n"
            "schema=json.loads(pathlib.Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())\n"
            "assert 'uniqueItems' not in schema['properties']['routes']\n"
            "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{\"routes\":[]}')\n")
        executable.chmod(0o700)
        original = {**SCHEMA, "properties": {"routes": {"type": "array", "uniqueItems": True}}}
        result = await generate({}, "JSON", original, ProviderConfig(model="chosen", executable=str(executable)), self.root / "call")
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(result["schema_sha256"], result["effective_schema_sha256"])
        self.assertEqual(json.loads((self.root / "call/schema.json").read_text()), original)
        self.assertEqual(json.loads((self.root / "call/effective_provider_schema.json").read_text()), codex_schema(original))

    def test_reasoning_effort_is_explicit_and_bounded(self):
        default = codex_command(ProviderConfig(model="same-model"), self.root / "schema", self.root / "result")
        self.assertFalse(any("model_reasoning_effort" in item for item in default))
        for effort in ("low", "medium", "high"):
            command = codex_command(ProviderConfig(model="same-model", reasoning_effort=effort), self.root / "schema", self.root / "result")
            self.assertIn('model_reasoning_effort="' + effort + '"', command)
            self.assertEqual(command[command.index("--model") + 1], "same-model")
        for invalid in ("", "minimal", "max", True, 1):
            with self.assertRaises(ValueError):
                ProviderConfig(model="same-model", reasoning_effort=invalid)

    async def test_reasoning_effort_is_recorded_and_sent_to_external_adapter(self):
        config = self.config("import json,sys; print(json.dumps(json.load(sys.stdin)))", reasoning_effort="low")
        result = await self.run_provider(config)
        self.assertEqual(result["reasoning_effort"], "low")
        self.assertEqual(json.loads(result["raw_response"])["reasoning_effort"], "low")

    def test_required_model_and_configuration_bounds(self):
        for kwargs in ({"model": ""}, {"model": "x", "timeout_seconds": float('nan')},
                       {"model": "x", "provider": "unknown"},
                       {"model": "x", "provider": "external"},
                       {"model": "x", "max_output_bytes": 0}):
            with self.assertRaises(ValueError):
                ProviderConfig(**kwargs)

    def test_codex_command_locks_down_tools_and_config(self):
        command = codex_command(ProviderConfig(model="configured-model"), self.root / "schema.json", self.root / "response.txt")
        for arg in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config", "read-only"):
            self.assertIn(arg, command)
        for arg in ('web_search="disabled"', 'features.shell_tool=false', 'features.apps=false',
                    'features.plugins=false', 'mcp_servers={}', 'project_doc_max_bytes=0'):
            self.assertIn(arg, command)
        self.assertEqual(command[-1], "-")
        self.assertIn("configured-model", command)


if __name__ == "__main__":
    unittest.main()
