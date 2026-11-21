from copy import deepcopy
import unittest

from retro_specialists.baseline import parse_baseline, select_original


def native(smile, precursors=None, stock=False):
    result = {'smile': smile, 'reference': 'False', 'building_block': str(stock)}
    if precursors is not None:
        result['children'] = [{'smile': smile, 'reference': 'True',
                              'children': precursors, 'step_yield': 'N/A'}]
    return result


def typed(smiles, precursors=None, stock=False):
    result = {'smiles': smiles, 'type': 'mol', 'in_stock': stock}
    if precursors is not None:
        result['children'] = [{'type': 'reaction', 'children': precursors}]
    return result


def container(node):
    return {'top0': {'path': node, 'idx': 0, 'score': 'N/A'}}


class BaselineTests(unittest.TestCase):
    def test_native_and_typed_routes(self):
        for build in [native, typed]:
            with self.subTest(schema=build.__name__):
                result = parse_baseline(container(build('CCO', [build('CC=O', stock=True)])), 'OCC')
                self.assertEqual(len(result['routes']), 1)
                route = result['routes'][0]
                self.assertEqual(route['route_id'], 'top0')
                self.assertEqual(route['reactions'], [{'product': 'CCO', 'reactants': ['CC=O']}])
                self.assertTrue(route['stock_connected'])
                self.assertEqual(result['inventory_observed'], ['CC=O'])
                self.assertEqual(result['provenance']['inventory_scope'], 'source_local_observations_only')

    def test_unknown_leaf_is_structural_hypothesis_not_stock_connected(self):
        result = parse_baseline(container(native('CCO', [native('CC=O')])), 'CCO')
        self.assertEqual(len(result['routes']), 1)
        self.assertFalse(result['routes'][0]['stock_connected'])
        self.assertEqual(result['routes'][0]['unknown_starting_materials'], ['CC=O'])
        self.assertEqual(result['inventory_observed'], [])

    def test_public_export_preserves_all_values_without_aliasing(self):
        payload = container(native('CCO', [native('CC=O', stock=True)]))
        payload['top7'] = {'path': typed('CCO', [typed('CC', stock=True)]), 'idx': 19, 'score': None}
        before = deepcopy(payload)
        result = select_original(payload, ['top7'])
        self.assertEqual(result, {'top7': payload['top7']})
        result['top7']['path']['children'].clear()
        self.assertEqual(payload, before)

    def test_reference_wrapper_must_match_product_and_not_be_stock(self):
        for updates in [{'smile': 'CC'}, {'reference': 'False'}, {'building_block': 'True'}]:
            node = native('CCO', [native('CC=O', stock=True)])
            node['children'][0].update(updates)
            out = parse_baseline(container(node), 'CCO')
            self.assertFalse(out['routes'])
            self.assertTrue(out['rejections'])

    def test_mixed_schema_and_unsupported_flags_rejected(self):
        nodes = [native('CCO', [typed('CC=O', stock=True)]),
                 {**native('CCO', [native('CC=O')]), 'building_block': 'yes'},
                 {**typed('CCO', [typed('CC=O')]), 'in_stock': 'False'},
                 {**native('CCO', [native('CC=O')]), 'in_stock': True}]
        for node in nodes:
            with self.subTest(node=node):
                out = parse_baseline(container(node), 'CCO')
                self.assertFalse(out['routes'])
                self.assertTrue(out['rejections'])

    def test_bad_entry_does_not_erase_good_entries(self):
        payload = container(native('CCO', [native('CC=O', stock=True)]))
        payload['top1'] = {'path': native('CCN', [native('C', stock=True)]), 'idx': 1}
        out = parse_baseline(payload, 'CCO')
        self.assertEqual(len(out['routes']), 1)
        self.assertEqual(len(out['rejections']), 1)
        self.assertEqual(out['inventory_observed'], ['CC=O'])

    def test_invalid_idx_and_shape(self):
        for entry in [{'path': native('CCO'), 'idx': True}, {'path': native('CCO')}, 3]:
            out = parse_baseline({'top0': entry}, 'CCO')
            self.assertFalse(out['routes'])
            self.assertTrue(out['rejections'])
        with self.assertRaises(ValueError):
            parse_baseline({'unrecognized': {}}, 'CCO')

    def test_two_selected_reactions_rejected(self):
        node = native('CCO', [native('CC=O', stock=True)])
        node['children'].append(deepcopy(node['children'][0]))
        self.assertFalse(parse_baseline(container(node), 'CCO')['routes'])

    def test_repeated_shared_intermediate_same_edge_coalesces(self):
        shared = native('C', [native('C=C', stock=True)])
        root = native('CCO', [native('CC', [deepcopy(shared)]),
                              native('CO', [deepcopy(shared), native('O', stock=True)])])
        result = parse_baseline(container(root), 'CCO')
        self.assertEqual(len(result['routes']), 1)
        self.assertEqual(result['routes'][0]['total_reactions'], 4)
        self.assertEqual(result['routes'][0]['depth'], 3)

    def test_conflicting_shared_intermediate_rejected(self):
        root = native('CCO', [native('CC', [native('C', [native('C=C', stock=True)])]),
                              native('CO', [native('C', [native('C#C', stock=True)]), native('O', stock=True)])])
        self.assertFalse(parse_baseline(container(root), 'CCO')['routes'])

    def test_cycles_depth_and_total_limits(self):
        cyclic = native('CCO', [native('CC', [native('CCO', stock=True)])])
        self.assertFalse(parse_baseline(container(cyclic), 'CCO')['routes'])
        chain = native('CCO', [native('CC=O', [native('CC', stock=True)])])
        for kwargs in [{'max_depth': 1}, {'max_total_reactions': 1}]:
            self.assertFalse(parse_baseline(container(chain), 'CCO', **kwargs)['routes'])

    def test_encoded_stereo_target_and_salts_preserved(self):
        node = native('F[C@H](Cl)Br', [native('FC(Cl)Br', stock=True)])
        self.assertFalse(parse_baseline(container(node), 'F[C@@H](Cl)Br')['routes'])
        salt = native('CCO.[Na+]', [native('CC=O.[Na+]', stock=True)])
        self.assertEqual(len(parse_baseline(container(salt), 'CCO.[Na+]')['routes']), 1)

    def test_empty_payload_and_procurement_are_not_synthesis(self):
        self.assertEqual(parse_baseline({}, 'CCO')['routes'], [])
        result = parse_baseline(container(native('CCO', stock=True)), 'CCO')
        self.assertFalse(result['routes'])
        self.assertEqual(result['rejections'][0]['reason'], 'Procurement-only entry has no synthesis reaction')

    def test_select_rejects_unknown_or_malformed_ids(self):
        payload = container(native('CCO', [native('CC=O')]))
        for ids in [['top8'], 'top0', [0]]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                select_original(payload, ids)


if __name__ == '__main__':
    unittest.main()
