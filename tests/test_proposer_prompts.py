"""Active role and target-aware instruction policy, independent of model output."""
import unittest

from retro_specialists.prompts import ROLES, instructions, planning_guidance
from retro_specialists.stereochemistry import target_stereo_requirements


class ProposerInstructionTests(unittest.TestCase):
    def test_only_proposer_can_receive_active_instructions(self):
        self.assertEqual(ROLES, ('route_proposer',))
        self.assertTrue(instructions('route_proposer'))
        for role in ('route_repair', 'cost_reducer', 'unknown'):
            with self.assertRaises(ValueError):
                instructions(role)

    def test_general_and_unspecified_targets_have_distinct_policy(self):
        general = planning_guidance(target_stereo_requirements('CC=O'))
        unspecified = planning_guidance(target_stereo_requirements('CC(O)c1ccccc1'))
        self.assertIn('not_applicable', general)
        self.assertIn('not_specified', unspecified)
        self.assertNotEqual(general, unspecified)
        self.assertIn('complete', general.lower())

    def test_opposite_targets_receive_their_actual_host_configuration(self):
        for smiles, configuration in [('C[C@H](O)c1ccccc1', 'S'),
                                       ('C[C@@H](O)c1ccccc1', 'R')]:
            requirements = target_stereo_requirements(smiles)
            before = repr(requirements)
            guidance = planning_guidance(requirements)
            self.assertIn('atom:2', guidance)
            self.assertIn(configuration, guidance)
            self.assertEqual(repr(requirements), before)

    def test_missing_or_unknown_requirement_mode_fails_closed(self):
        for requirements in ({}, {'mode': 'pretend_supported'}):
            with self.assertRaises(ValueError):
                planning_guidance(requirements)


class OriginInstructionTests(unittest.TestCase):
    def test_origin_is_inspected_and_bound_independently(self):
        text = instructions('route_proposer')
        for token in ('inspect_target_stereochemistry', 'origin_element', 'origin_configuration'):
            self.assertIn(token, text)
        self.assertIn('every distinct', text)
        self.assertIn('independently', text)
