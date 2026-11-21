"""Component identity guards: no salt stripping and no shared-counterion ban."""
from copy import deepcopy
import unittest

from retro_specialists.costs import evaluate_route
from retro_specialists.graphs import canonical_smiles, route_graph


def graph(target, reactions):
    return {'target': target, 'reactions': [{'product': product, 'reactants': precursors}
                                          for product, precursors in reactions]}


class ComponentAncestryTests(unittest.TestCase):
    def validators(self, value):
        return (lambda: route_graph(value, value['target'], 20, 40),
                lambda: evaluate_route(value, [], {}))

    def test_target_embedded_in_precursor_is_rejected_in_graph_and_cost(self):
        value = graph('CCO', [('CCO', ['CCO.O'])])
        for validate in self.validators(value):
            with self.subTest(validator=validate), self.assertRaises(ValueError):
                validate()

    def test_longer_ancestor_embedded_in_precursor_is_rejected(self):
        value = graph('CCO', [('CCO', ['CC=O.O']), ('CC=O.O', ['CCO.Cl'])])
        for validate in self.validators(value):
            with self.subTest(validator=validate), self.assertRaises(ValueError):
                validate()

    def test_shared_dag_cannot_hide_different_path_ancestor(self):
        # The D subgraph is visited through A before B; memoizing only D must
        # not hide B's unchanged components when D is later reached from B.
        value = graph('CCCCO', [('CCCCO', ['CCCO', 'CCN']),
                               ('CCCO', ['CO']), ('CO', ['CCN.O']), ('CCN', ['CO'])])
        for validate in self.validators(value):
            with self.subTest(validator=validate), self.assertRaises(ValueError):
                validate()

    def test_shared_counterion_is_allowed_and_identity_is_preserved(self):
        value = graph('CCO.[Na+]', [('CCO.[Na+]', ['CC=O.[Na+]'])])
        before = deepcopy(value)
        for validate in self.validators(value):
            validate()
        result = route_graph(value, value['target'])
        self.assertEqual(result['target'], canonical_smiles('CCO.[Na+]'))
        self.assertEqual(result['leaves'], [canonical_smiles('CC=O.[Na+]')])
        self.assertEqual(value, before)

    def test_counterion_exchange_does_not_remove_components(self):
        value = graph('CC[NH3+].[Cl-]', [('CC[NH3+].[Cl-]', ['CC[NH3+].[Br-]'])])
        for validate in self.validators(value):
            validate()
        self.assertNotEqual(canonical_smiles(value['target']), canonical_smiles(value['reactions'][0]['reactants'][0]))

    def test_component_multiplicity_is_required_for_containment(self):
        value = graph('CCO.CCO', [('CCO.CCO', ['CCO.O'])])
        for validate in self.validators(value):
            validate()
        contained = graph('CCO.CCO', [('CCO.CCO', ['CCO.CCO.O'])])
        for validate in self.validators(contained):
            with self.subTest(validator=validate), self.assertRaises(ValueError):
                validate()

    def test_changed_stereoisomer_or_isotope_is_not_unchanged_ancestor(self):
        for product, precursor in [('F[C@H](Cl)Br', 'F[C@@H](Cl)Br.O'),
                                    ('[13CH3]O', 'CO.O'), ('CCO', 'CC[O-].[Na+]')]:
            value = graph(product, [(product, [precursor])])
            for validate in self.validators(value):
                validate()

    def test_mapped_alias_hydrate_cannot_bypass_check(self):
        value = graph('CCO', [('CCO', ['[CH3:1][CH2:2][OH:3].O'])])
        for validate in self.validators(value):
            with self.subTest(validator=validate), self.assertRaises(ValueError):
                validate()


if __name__ == '__main__':
    unittest.main()
