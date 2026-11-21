import json
import unittest

from retro_specialists.graphs import canonical_smiles, graph_key, route_graph, validate_response
from tests.test_contracts import route


def proposal(reactions, **kwargs):
    return dict(route(), reactions=[{"product": p, "reactants": r} for p, r in reactions], **kwargs)


class GraphTests(unittest.TestCase):
    def run_routes(self, routes, target="CCO", **kwargs):
        return validate_response(json.dumps({"routes": routes, "limitations": []}), target, **kwargs)

    def test_canonical_identity_keeps_stereo_isotopes_salts(self):
        self.assertEqual(canonical_smiles("OCC"), canonical_smiles("CCO"))
        self.assertNotEqual(canonical_smiles("F[C@H](Cl)Br"), canonical_smiles("F[C@@H](Cl)Br"))
        self.assertNotEqual(canonical_smiles("[13CH3]O"), canonical_smiles("CO"))
        self.assertNotEqual(canonical_smiles("CCO.[Na+]"), canonical_smiles("CCO"))
        self.assertEqual(canonical_smiles("[CH3:7][OH:8]"), canonical_smiles("CO"))

    def test_invalid_smiles_names_and_placeholders(self):
        for value in ["", "not_smiles", "*", "C(C)(C)(C)(C)C", "CC ethane", "CC\n", "R1", "C" * 4097]:
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                canonical_smiles(value)

    def test_valid_counts_and_inventory(self):
        out = self.run_routes([route()], inventory=["O=CC"])
        self.assertEqual(out["counts"], {"proposed": 1, "schema_valid": 1,
                         "structurally_valid": 1, "unique": 1, "stock_connected": 1,
                         "chemically_assessed": 0, "retained": 1, "rejected": 0})
        item = out["routes"][0]
        self.assertEqual(item["leaves"], ["CC=O"])
        self.assertEqual(item["chemistry_status"], "unassessed")
        self.assertEqual(item["depth"], 1)

    def test_unknown_stock_is_explicit_hypothesis(self):
        out = self.run_routes([route()])
        self.assertFalse(out["routes"][0]["stock_connected"])
        self.assertEqual(out["counts"]["retained"], 1)
        self.assertEqual(out["retention_basis"], "structurally_valid_hypotheses")

    def test_target_mismatch_and_stereomismatch(self):
        self.assertEqual(self.run_routes([route()], target="CCN")["counts"]["retained"], 0)
        r = proposal([("F[C@H](Cl)Br", ["FC(Cl)Br"])])
        out = self.run_routes([r], target="F[C@@H](Cl)Br")
        self.assertEqual(out["counts"]["retained"], 0)

    def test_self_reaction_and_long_cycle(self):
        for reactions in [[("CCO", ["OCC"])],
                          [("CCO", ["CC=O"]), ("CC=O", ["CC"]), ("CC", ["CCO"])]]:
            out = self.run_routes([proposal(reactions)])
            self.assertEqual(out["counts"]["structurally_valid"], 0)

    def test_disconnected_and_duplicate_producers(self):
        for reactions in [[("CCO", ["CC=O"]), ("CN", ["C"])],
                          [("CCO", ["CC=O"]), ("OCC", ["CC"])]]:
            self.assertEqual(self.run_routes([proposal(reactions)])["counts"]["retained"], 0)

    def test_depth_and_total_reactions_are_distinct(self):
        r = proposal([("CCO", ["CC", "CO"]), ("CC", ["C"]), ("CO", ["C", "O"])])
        result = route_graph(r, "CCO", 2, 3)
        self.assertEqual((result["depth"], result["total_reactions"]), (2, 3))
        with self.assertRaises(ValueError):
            route_graph(r, "CCO", 1, 3)
        with self.assertRaises(ValueError):
            route_graph(r, "CCO", 2, 2)

    def test_connectivity_dedup_ignores_names_order_and_alternate_smiles(self):
        a = proposal([("CCO", ["CC", "O"])])
        b = proposal([("OCC", ["O", "CC"])], name="different", route_id="r2")
        out = self.run_routes([a, b])
        self.assertEqual(out["counts"]["structurally_valid"], 2)
        self.assertEqual(out["counts"]["unique"], 1)
        self.assertEqual(out["counts"]["rejected"], 1)
        self.assertEqual(graph_key(a), graph_key(b))

    def test_reactant_multiplicity_retained(self):
        a = proposal([("CCO", ["C", "O"])])
        b = proposal([("CCO", ["C", "C", "O"])])
        self.assertNotEqual(graph_key(a), graph_key(b))

    def test_element_source_warning_is_not_chemical_validation(self):
        result = self.run_routes([proposal([("CCO", ["CC"])])])["routes"][0]
        self.assertEqual(result["element_source_warnings"][0]["missing_elements"], ["O"])
        self.assertEqual(result["chemistry_status"], "unassessed")

    def test_withdrawn_and_explicit_self_disqualification(self):
        for r in [dict(route(), self_assessment="withdrawn"),
                  dict(route(), rationale="This route is invalid and should be rejected.")]:
            out = self.run_routes([r])
            self.assertEqual(out["counts"]["retained"], 0)
            self.assertEqual(out["counts"]["structurally_valid"], 1)

    def test_uncertainty_is_not_self_disqualification(self):
        r = dict(route(), feasibility_risks=["Not experimentally validated; selectivity is uncertain."])
        self.assertEqual(self.run_routes([r])["counts"]["retained"], 1)

    def test_top_level_explicit_disqualification(self):
        raw = json.dumps({"routes": [route()], "limitations": ["All proposed routes are invalid."]})
        self.assertEqual(validate_response(raw, "CCO")["counts"]["retained"], 0)

    def test_schema_failure_rejects_whole_payload(self):
        out = self.run_routes([route(), dict(route(), surprise=True)])
        self.assertEqual(out["counts"]["proposed"], 2)
        self.assertEqual(out["counts"]["schema_valid"], 0)
        self.assertEqual(out["counts"]["rejected"], 2)
        self.assertTrue(out["rejections"])

    def test_empty_completion_and_malformed_json(self):
        self.assertEqual(self.run_routes([])["counts"]["retained"], 0)
        out = validate_response("{", "CCO")
        self.assertEqual(out["routes"], [])
        self.assertTrue(out["rejections"])

    def test_invalid_request_and_inventory_fail_closed(self):
        for kwargs in [{"requested_count": True}, {"max_depth": 0},
                       {"max_total_reactions": False}, {"inventory": ["garbage"]},
                       {"inventory": "CC=O"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_routes([route()], **kwargs)

    def test_direct_graph_helper_rejects_invalid_shapes(self):
        for value in [None, {}, {"reactions": []},
                      {"reactions": [{"product": "CCO", "reactants": ["CC"], "yield": 1}]},
                      {"reactions": [{"product": "CCO", "reactants": []}]}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                route_graph(value, "CCO")

    def test_shared_intermediate_is_acyclic_and_input_is_not_mutated(self):
        r = proposal([("CCO", ["CC", "CO"]), ("CC", ["C"]),
                      ("CO", ["C", "O"]), ("C", ["C=C"])])
        before = json.dumps(r, sort_keys=True)
        graph = route_graph(r, "CCO", 3, 4)
        self.assertEqual(graph["depth"], 3)
        self.assertEqual(graph["leaves"], ["C=C", "O"])
        graph["feasibility_risks"].append("modified output")
        self.assertEqual(json.dumps(r, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
