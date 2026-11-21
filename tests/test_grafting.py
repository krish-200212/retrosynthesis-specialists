from copy import deepcopy
import unittest

from retro_specialists.grafting import graft_route


def route(*steps, target=None, evidence_ids=None):
    value = {'route_id': 'input', 'reactions': [
        {'product': product, 'reactants': list(reactants)} for product, reactants in steps]}
    if target is not None:
        value['target'] = target
    if evidence_ids is not None:
        value['evidence_ids'] = evidence_ids
    return value


class GraftingTests(unittest.TestCase):
    def test_preserves_other_branches_and_input(self):
        parent = route(('CCO', ['CC=O', 'O']))
        expansion = route(('CC=O', ['CC']), target='CC=O')
        before = deepcopy((parent, expansion))
        result = graft_route(parent, expansion, 'CCO', 5, 20)
        self.assertEqual(result['leaves'], ['CC', 'O'])
        self.assertEqual(result['depth'], 2)
        self.assertEqual(result['total_reactions'], 2)
        self.assertEqual(result['expanded_leaf'], 'CC=O')
        self.assertEqual(result['origin'], 'intermediate_replanning')
        self.assertEqual((parent, expansion), before)
        self.assertFalse(result['chemical_feasibility_established'])

    def test_infers_only_unambiguous_expansion_target(self):
        parent = route(('CCO', ['CC=O', 'O']))
        result = graft_route(parent, route(('CC=O', ['CC'])), 'CCO', 5, 20)
        self.assertEqual(result['expanded_leaf'], 'CC=O')
        ambiguous = route(('CC=O', ['O']), ('O', ['N']))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            graft_route(parent, ambiguous, 'CCO', 5, 20)

    def test_shared_producer_coalesces_without_losing_multiplicity(self):
        parent = route(('CCO', ['CC=O', 'CCN']), ('CCN', ['C', 'N', 'C']))
        expansion = route(('CC=O', ['CCN']), ('CCN', ['C', 'C', 'N']), target='CC=O')
        result = graft_route(parent, expansion, 'CCO', 5, 20)
        self.assertEqual(result['total_reactions'], 3)
        self.assertEqual(result['depth'], 3)
        shared = next(r for r in result['reactions'] if r['product'] == 'CCN')
        self.assertEqual(shared['reactants'], ['C', 'C', 'N'])

    def test_conflicting_shared_producer_rejected(self):
        parent = route(('CCO', ['CC=O', 'CCN']), ('CCN', ['C']))
        expansion = route(('CC=O', ['CCN']), ('CCN', ['CC']), target='CC=O')
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            graft_route(parent, expansion, 'CCO', 5, 20)

    def test_ancestor_cycle_and_embedded_ancestor_rejected(self):
        parent = route(('CCO', ['CC=O']))
        for precursor in ['CCO', 'CCO.O']:
            with self.subTest(precursor=precursor), self.assertRaises(ValueError):
                graft_route(parent, route(('CC=O', [precursor]), target='CC=O'), 'CCO', 5, 20)

    def test_global_depth_and_total_limits(self):
        parent = route(('CCO', ['CC=O', 'O']))
        expansion = route(('CC=O', ['CC']), target='CC=O')
        for depth, total in [(1, 20), (5, 1)]:
            with self.subTest(depth=depth, total=total), self.assertRaises(ValueError):
                graft_route(parent, expansion, 'CCO', depth, total)

    def test_disconnected_expansion_rejected(self):
        parent = route(('CCO', ['CC=O']))
        bad = route(('CC=O', ['CC']), ('N', ['O']), target='CC=O')
        with self.assertRaisesRegex(ValueError, 'Disconnected'):
            graft_route(parent, bad, 'CCO', 5, 20)

    def test_target_must_be_terminal_leaf_with_exact_stereo(self):
        parent = route(('CCO', ['F[C@H](Cl)Br']))
        wrong = route(('F[C@@H](Cl)Br', ['FC(Cl)Br']), target='F[C@@H](Cl)Br')
        with self.assertRaisesRegex(ValueError, 'terminal leaf'):
            graft_route(parent, wrong, 'CCO', 5, 20)
        with self.assertRaises(ValueError):
            graft_route(route(('CCO', ['CC'])), route(('CCO', ['CC=O']), target='CCO'), 'CCO', 5, 20)

    def test_stable_key_and_source_evidence_union_only(self):
        parent = route(('CCO', ['CC=O', 'O']), evidence_ids=['source-a'])
        expansion = route(('CC=O', ['CC']), target='CC=O', evidence_ids=['source-b', 'source-a'])
        one = graft_route(parent, expansion, 'CCO', 5, 20)
        two = graft_route(parent, expansion, 'OCC', 5, 20)
        self.assertEqual(one['route_id'], two['route_id'])
        self.assertEqual(one['evidence_ids'], ['source-a', 'source-b'])
        self.assertEqual(one['graph_key'], two['graph_key'])

    def test_malformed_evidence_and_stale_parent_target_rejected(self):
        parent = route(('CCO', ['CC=O']), target='CC')
        expansion = route(('CC=O', ['CC']), target='CC=O')
        with self.assertRaisesRegex(ValueError, 'target'):
            graft_route(parent, expansion, 'CCO', 5, 20)
        parent.pop('target')
        expansion['evidence_ids'] = 'not-an-array'
        with self.assertRaises(ValueError):
            graft_route(parent, expansion, 'CCO', 5, 20)


if __name__ == '__main__':
    unittest.main()
