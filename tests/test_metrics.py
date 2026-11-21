import unittest
from retro_specialists.metrics import organic_route_key,compare_diversity
class MetricsTests(unittest.TestCase):
 def test_reagent_variation_is_not_new_organic_sequence(self):
  a={'reactions':[{'product':'CC=O','reactants':['CCO','O=O']}]}
  b={'reactions':[{'product':'CC=O','reactants':['CCO','OO']}]}
  self.assertEqual(organic_route_key(a),organic_route_key(b))
  self.assertEqual(compare_diversity([a],[b])['additional_organic_sequences'],0)
 def test_changed_organic_precursor_is_new(self):
  a={'reactions':[{'product':'CC=O','reactants':['CCO']}]}
  b={'reactions':[{'product':'CC=O','reactants':['CC(Cl)Cl','O']}]}
  self.assertEqual(compare_diversity([a],[b])['additional_organic_sequences'],1)
 def test_stereo_and_multiplicity_preserved(self):
  a={'reactions':[{'product':'CC=O','reactants':['C[C@H](O)Cl']}]}
  b={'reactions':[{'product':'CC=O','reactants':['C[C@@H](O)Cl']}]}
  self.assertNotEqual(organic_route_key(a),organic_route_key(b))
  b={'reactions':[{'product':'CC=O','reactants':['C[C@H](O)Cl','C[C@H](O)Cl']}]}
  self.assertNotEqual(organic_route_key(a),organic_route_key(b))
