from copy import deepcopy
from hashlib import sha256
import unittest

from retro_specialists.native_catalog import extract_catalog_observations


def payload(cost=None, smiles='CCO', links=None):
    leaf={'smile':smiles,'cost':cost if cost is not None else [],
          'building_block_path':links or ['https://example.org/a','https://example.org/b']}
    return {'top0':{'path':{'smile':'CC=O','path_cost':262,'total_cost':3.4,
                           'children':[{'smile':'CC=O','reference':'True','children':[leaf]}]},'idx':0}}


class NativeCatalogTests(unittest.TestCase):
    def extract(self, value):
        return extract_catalog_observations(value,'a'*64)

    def test_pack_normalization_keeps_currency_unknown_and_sources_unpaired(self):
        result=self.extract(payload([{'1G':7},{'5G':29}]))
        item=result['materials'][0]
        self.assertEqual([row['native_units_per_gram'] for row in item['offers']],['7','5.8'])
        self.assertIsNone(item['currency']);self.assertIsNone(item['observed_at'])
        self.assertIsNone(item['assay'])
        self.assertFalse(item['eligible_for_monetary_comparison'])
        self.assertEqual(len(item['source_urls']),2)
        self.assertNotIn('source_url',item['offers'][0])
        self.assertEqual(item['source_offer_mapping'],'unresolved_no_positional_mapping')

    def test_mass_units_and_scientific_numeric_labels(self):
        result=self.extract(payload([{'100MG':5},{'0.5KG':100},{'1e-2G':2}]))
        self.assertEqual([r['pack_grams'] for r in result['materials'][0]['offers']],['0.1','500','0.01'])

    def test_duplicate_routes_and_canonical_smiles_do_not_create_new_offers(self):
        obj=payload([{'1G':7}]);obj['top1']=deepcopy(obj['top0'])
        obj['top1']['path']['children'][0]['children'][0]['smile']='OCC'
        result=self.extract(obj);item=result['materials'][0]
        self.assertEqual(len(result['materials']),1);self.assertEqual(len(item['offers']),1)
        self.assertEqual(item['route_ids'],['top0','top1'])

    def test_alternative_prices_are_preserved_for_review(self):
        result=self.extract(payload([{'1G':7},{'1G':16}]))
        self.assertEqual(len(result['materials'][0]['offers']),2)
        self.assertTrue(result['materials'][0]['multiple_prices_for_pack'])

    def test_salts_isotopes_and_stereochemistry_are_not_merged(self):
        obj={}
        for i,s in enumerate(['CCO','CCO.[Na+]','[13CH3]CO','F[C@H](Cl)Br','F[C@@H](Cl)Br']):
            obj[f'top{i}']=payload([{'1G':7}],s)['top0']
        self.assertEqual(len(self.extract(obj)['materials']),5)

    def test_missing_prices_never_become_262_quotes(self):
        for cost in [[],None,'None','Not available']:
            obj=payload();obj['top0']['path']['children'][0]['children'][0]['cost']=cost
            result=self.extract(obj)
            self.assertEqual(result['priced_material_count'],0)
            self.assertEqual(result['materials'][0]['offers'],[])
        result=self.extract(payload([{'1G':262}]))
        self.assertTrue(result['materials'][0]['offers'][0]['ambiguous_legacy_fallback_value'])
        self.assertFalse(result['materials'][0]['eligible_for_monetary_comparison'])

    def test_invalid_offerings_are_rejected_individually(self):
        values=[{'0G':2},{'-1G':2},{'1ML':2},{'1G':-1},{'1G':True},{'1G':float('nan')},
                {'unknown':2},{'1G':'7'},{'1G':7}]
        result=self.extract(payload(values))
        self.assertEqual(len(result['materials'][0]['offers']),1)
        self.assertEqual(len(result['rejections']),8)

    def test_internal_prices_and_root_duplicates_are_not_leaf_quotes(self):
        obj=payload([{'1G':7}]);obj['top0']['path']['cost']=[{'1G':999}]
        obj['top0']['cost']=[{'1G':999}]
        result=self.extract(obj)
        self.assertEqual(result['materials'][0]['molecule'],'CCO')
        self.assertEqual(result['route_metrics'][0]['native_path_cost'],262)
        self.assertEqual(result['route_metrics'][0]['planner_total_cost'],3.4)
        self.assertEqual(len(result['materials'][0]['offers']),1)

    def test_input_not_mutated_and_sha_required(self):
        obj=payload([{'1G':7}]);before=deepcopy(obj);self.extract(obj);self.assertEqual(obj,before)
        for value in ['','Z'*64,None]:
            with self.assertRaises(ValueError):extract_catalog_observations(obj,value)

    def test_unsafe_urls_not_promoted_to_source_links(self):
        obj=payload([{'1G':7}],links=['https://example.org/a','file:///etc/passwd',
                    'https://user:password@example.org/','http://127.0.0.1/'])
        self.assertEqual(self.extract(obj)['materials'][0]['source_urls'],['https://example.org/a'])

    def test_malformed_or_cyclic_tree_rejected(self):
        for obj in [[],{'top0':{}},{'top0':{'path':[]}}]:
            with self.assertRaises(ValueError):self.extract(obj)
        node={'smile':'CCO'};node['children']=[node]
        with self.assertRaises(ValueError):self.extract({'top0':{'path':node}})
