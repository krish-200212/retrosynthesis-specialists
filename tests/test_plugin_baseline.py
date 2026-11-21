"""Read-only historical cost arithmetic remains available outside active tools."""
from hashlib import sha256
import json
import sys
from pathlib import Path
import tempfile
import unittest
from retro_specialists.plugin import PrimitiveTools
from retro_specialists.backend_worker import _cost
from tests.test_plugin import Context

BASELINE = {'route_id': 'base', 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']}]}
OTHER = {'route_id': 'other', 'reactions': [{'product': 'CCO', 'reactants': ['CC']}]}

class BaselineTests(unittest.TestCase):
    def service(self, tmp, **config):
        return PrimitiveTools(Context(tmp, enabled=True, backend_python=sys.executable,
            backend_library_paths=[str(Path(__import__('rdkit').__file__).resolve().parent.parent)], **config))
    def test_unsourced_baseline_is_not_tool_improvement(self):
        with tempfile.TemporaryDirectory() as tmp:
            trusted = self.service(tmp)._configuration()[-1]
            result = {'result': _cost({'baseline_routes': [BASELINE], 'candidate_routes': [OTHER]}, trusted)}
            self.assertEqual(result['result']['baseline_provenance']['status'], 'unverified_user_supplied_baseline')
            self.assertFalse(result['result']['tool_baseline_verified'])
            self.assertEqual(result['result']['improvements'], [])
    def test_matching_configured_artifact_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'baseline.json'
            path.write_text(json.dumps({'source': 'trusted tool fixture', 'routes': [BASELINE]}))
            service = self.service(tmp, baseline_file=str(path), baseline_sha256=sha256(path.read_bytes()).hexdigest())
            result = {'result': _cost({'baseline_routes': [BASELINE], 'candidate_routes': [OTHER]}, service._configuration()[-1])}
            self.assertTrue(result['result']['tool_baseline_verified'], result)
    def test_model_changed_baseline_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'baseline.json'; path.write_text(json.dumps({'source': 'trusted tool fixture', 'routes': [BASELINE]}))
            service = self.service(tmp, baseline_file=str(path), baseline_sha256=sha256(path.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ValueError, 'differs'):
                _cost({'baseline_routes': [OTHER], 'candidate_routes': [OTHER]}, service._configuration()[-1])
    def test_changed_baseline_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'baseline.json'; path.write_text(json.dumps({'source': 'trusted tool fixture', 'routes': [BASELINE]}))
            service = self.service(tmp, baseline_file=str(path), baseline_sha256='a'*64)
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                _cost({'baseline_routes': [BASELINE], 'candidate_routes': [OTHER]}, service._configuration()[-1])

if __name__ == '__main__':
    unittest.main()
