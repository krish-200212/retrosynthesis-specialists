"""Standalone target inspection uses the actual isolated backend entry point."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class BackendStereoInspectionTests(unittest.TestCase):
    def inspect_target(self, target):
        result = subprocess.run(
            [sys.executable, '-m', 'retro_specialists.backend_worker'],
            input=json.dumps({'action': 'inspect_target_stereochemistry',
                              'arguments': {'target': target},
                              'trusted': {'library_paths': []}}),
            text=True, capture_output=True, timeout=10,
            cwd=Path(__file__).resolve().parents[1], check=True)
        return json.loads(result.stdout)

    def test_explicit_stereo_has_backend_numbered_configuration(self):
        response = self.inspect_target('C[C@H](O)c1ccccc1')
        self.assertEqual(response['status'], 'completed')
        requirements = response['result']
        self.assertEqual(requirements['mode'], 'specified')
        self.assertEqual(len(requirements['specified_elements']), 1)
        self.assertRegex(requirements['specified_elements'][0]['id'], r'^atom:\d+$')
        self.assertIn(requirements['specified_elements'][0]['configuration'], ('R', 'S'))
        self.assertFalse(requirements['chemical_feasibility_established'])

    def test_nonstereo_target_and_invalid_shape(self):
        response = self.inspect_target('CCO')
        self.assertEqual(response['status'], 'completed')
        self.assertEqual(response['result']['mode'], 'none')
        for value in (None, [], {'smiles': 'CCO'}, 'not a molecule'):
            with self.subTest(value=value):
                self.assertEqual(self.inspect_target(value)['status'], 'backend_error')
