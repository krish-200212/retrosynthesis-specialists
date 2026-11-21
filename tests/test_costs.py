"""Controlled arithmetic fixtures; these reactions are not chemistry evidence."""
import copy
import json
import unittest
from decimal import Decimal

from retro_specialists.costs import (
    PriceQuote, compare_routes, evaluate_route, reaction_key,
)


def route(reactants=("CC", "O")):
    return {"target": "CCO", "reactions": [
        {"product": "CCO", "reactants": list(reactants)}]}


def quote(molecule, amount="10", quantity="1", unit="mol", **extra):
    return {"molecule": molecule, "amount": amount, "quantity": quantity,
            "unit": unit, "currency": "USD", "source": "fixture://quote",
            "retrieved_at": "2026-11-20T12:00:00+00:00", **extra}


def proxy(**extra):
    return {"mode": "inventory_catalog_price_proxy", "target_mol": "1",
            "currency": "USD", "default_yield": "1",
            "default_equivalents": "1", **extra}


class QuoteTests(unittest.TestCase):
    def test_mass_units_normalize(self):
        grams = PriceQuote.from_mapping(quote("O", "18.015", "18.015", "g"))
        milligrams = PriceQuote.from_mapping(quote("O", "18.015", "18015", "mg"))
        self.assertEqual(grams.amount_per_mol, milligrams.amount_per_mol)
        self.assertAlmostEqual(float(grams.amount_per_mol), 18.015, places=3)

    def test_moles_and_millimoles(self):
        self.assertEqual(PriceQuote.from_mapping(quote("O", "2", "100", "mmol")).amount_per_mol,
                         Decimal("20"))

    def test_package_needs_real_quantity(self):
        with self.assertRaises(ValueError):
            PriceQuote.from_mapping(quote("O", unit="package"))
        parsed = PriceQuote.from_mapping(quote("O", "4", "2", "package",
                     package_quantity="100", package_unit="mmol"))
        self.assertEqual(parsed.amount_per_mol, Decimal("20"))

    def test_nonfinite_negative_and_boolean_rejected(self):
        for value in ("NaN", "Infinity", "-1", True, "1e999999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PriceQuote.from_mapping(quote("O", amount=value))

    def test_zero_quantity_and_missing_provenance_rejected(self):
        for change in ({"quantity": "0"}, {"source": ""}, {"retrieved_at": "yesterday"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                PriceQuote.from_mapping(quote("O", **change))

    def test_zero_price_permitted_only_as_sourced_quote(self):
        self.assertEqual(PriceQuote.from_mapping(quote("O", "0")).amount_per_mol, Decimal("0"))

    def test_unsupported_unit_currency_smiles(self):
        for change in ({"unit": "litre"}, {"currency": "usd"}, {"molecule": "not_smiles"}):
            item = {**quote("O"), **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                PriceQuote.from_mapping(item)

    def test_atom_maps_do_not_create_different_quote_identity(self):
        self.assertEqual(PriceQuote.from_mapping(quote("[OH2:1]")).molecule, "O")

    def test_direct_quote_requires_decimal_amount(self):
        with self.assertRaises(ValueError):
            PriceQuote("O", "USD", "10", "fixture://quote", "2026-11-20T12:00:00Z")


class CostTests(unittest.TestCase):
    def test_simple_proxy(self):
        value = evaluate_route(route(), [quote("CC"), quote("O", "4")], proxy())
        self.assertEqual(value["amount"], "14")
        self.assertEqual(value["status"], "known")
        self.assertEqual(value["basis"], "inventory_catalog_price_proxy")
        self.assertFalse(value["chemical_feasibility_established"])

    def test_duplicate_precursor_multiplicity(self):
        value = evaluate_route(route(("C", "C", "O")),
                               [quote("C", "3"), quote("O", "4")], proxy())
        self.assertEqual(value["amount"], "10")
        self.assertEqual(value["leaf_demands_mol"]["C"], "2")

    def test_shared_intermediate_accumulates_all_consumers(self):
        graph = {"target": "CCCO", "reactions": [
            {"product": "CCCO", "reactants": ["CCO", "CCN"]},
            {"product": "CCO", "reactants": ["CO"]},
            {"product": "CCN", "reactants": ["CO"]},
            {"product": "CO", "reactants": ["C", "O"]}]}
        value = evaluate_route(graph, [quote("C", "3"), quote("O", "4")], proxy())
        self.assertEqual(value["amount"], "14")
        self.assertEqual(value["leaf_demands_mol"], {"C": "2", "O": "2"})

    def test_yield_and_equivalent_scenario(self):
        value = evaluate_route(route(), [quote("CC"), quote("O", "4")],
                               proxy(default_yield="0.5", default_equivalents="2"))
        self.assertEqual(value["amount"], "56")

    def test_missing_price_never_returns_subtotal(self):
        value = evaluate_route(route(), [quote("CC")], proxy())
        self.assertIsNone(value["amount"])
        self.assertIn("quote:O", value["missing"])

    def test_missing_assumptions_are_unknown(self):
        settings = {"mode": "inventory_catalog_price_proxy", "currency": "USD"}
        value = evaluate_route(route(), [quote("CC"), quote("O")], settings)
        self.assertIsNone(value["amount"])
        self.assertIn("target_mol", value["missing"])

    def test_no_implicit_scenario(self):
        value = evaluate_route(route(), [quote("CC"), quote("O")], {})
        self.assertEqual(value["status"], "unknown")
        self.assertIsNone(value["amount"])

    def test_mixed_currency_not_converted(self):
        value = evaluate_route(route(), [quote("CC"), quote("O", currency="EUR")], proxy())
        self.assertIsNone(value["amount"])
        self.assertIn("quote:O", value["missing"])

    def test_conflicting_quotes_not_silently_selected(self):
        with self.assertRaises(ValueError):
            evaluate_route(route(), [quote("CC"), quote("CC", "11"), quote("O")], proxy())

    def test_evidence_mode_requires_measured_parameters(self):
        value = evaluate_route(route(), [quote("CC"), quote("O")],
                               proxy(mode="evidence_supported_precursor_cost"))
        self.assertIsNone(value["amount"])
        self.assertTrue(any("parameters" in item for item in value["missing"]))

    def test_evidence_mode_with_measured_parameters(self):
        item = route()
        params = {reaction_key(item["reactions"][0]): {
            "yield_fraction": "0.5", "equivalents": {"CC": "1", "O": "2"},
            "source": "fixture://measurement", "evidence_kind": "measured"}}
        value = evaluate_route(item, [quote("CC"), quote("O", "4")],
             {"mode": "evidence_supported_precursor_cost", "currency": "USD",
              "target_mol": "1", "reaction_parameters": params})
        self.assertEqual(value["amount"], "36")
        self.assertEqual(value["basis"], "evidence_supported_precursor_cost")

    def test_assumed_yield_cannot_be_called_measured(self):
        item = route()
        params = {reaction_key(item["reactions"][0]): {
            "yield_fraction": "0.5", "equivalents": {"CC": "1", "O": "2"},
            "source": "fixture://prediction", "evidence_kind": "predicted"}}
        value = evaluate_route(item, [quote("CC"), quote("O")],
            {"mode": "evidence_supported_precursor_cost", "currency": "USD",
             "target_mol": "1", "reaction_parameters": params})
        self.assertIsNone(value["amount"])

    def test_cycle_disconnected_duplicate_producer_rejected(self):
        malformed = [
            {"target": "CCO", "reactions": [{"product": "CCO", "reactants": ["CC"]},
                {"product": "CC", "reactants": ["CCO"]}]},
            {"target": "CCO", "reactions": [*route()["reactions"],
                {"product": "N", "reactants": ["C"]}]},
            {"target": "CCO", "reactions": route()["reactions"] * 2}]
        for item in malformed:
            with self.subTest(item=item), self.assertRaises(ValueError):
                evaluate_route(item, [], proxy())

    def test_inputs_not_mutated_json_safe(self):
        inputs = [route(), [quote("CC"), quote("O")], proxy()]
        before = copy.deepcopy(inputs)
        result = evaluate_route(*inputs)
        self.assertEqual(inputs, before)
        json.dumps(result, allow_nan=False)

    def test_invalid_yield_or_equivalents_rejected(self):
        for change in ({"default_yield": "1.1"}, {"default_yield": "0"},
                       {"default_equivalents": "-1"}, {"reaction_parameters": []}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluate_route(route(), [quote("CC"), quote("O")], proxy(**change))

    def test_quote_evidence_retains_date_and_normalized_basis(self):
        result = evaluate_route(route(), [quote("CC"), quote("O")], proxy())
        self.assertEqual(result["quote_evidence"][0]["retrieved_at"], "2026-11-20T12:00:00+00:00")
        self.assertEqual(result["quote_evidence"][0]["amount_per_mol"], "10")


class ComparisonTests(unittest.TestCase):
    def test_improvement_against_best_baseline(self):
        quotes = [quote("CC", "10"), quote("C", "3"), quote("O", "4"), quote("N", "20")]
        result = compare_routes([route(), route(("N", "O"))], [route(("C", "O"))], quotes, proxy())
        self.assertEqual(result["best_baseline"]["amount"], "14")
        self.assertEqual(result["improvements"][0]["reduction_percent"], "50")
        self.assertFalse(result["demonstrated_total_synthesis_cost_reduction"])

    def test_unknown_cost_does_not_claim_improvement(self):
        result = compare_routes([route()], [route(("C", "O"))], [quote("CC")], proxy())
        self.assertEqual(result["improvements"], [])
        self.assertEqual(len(result["candidates"]), 1)

    def test_identical_route_never_counts_as_new_improvement(self):
        result = compare_routes([route()], [route(("O", "CC"))],
                                [quote("CC"), quote("O")], proxy())
        self.assertEqual(result["improvements"], [])

    def test_target_mismatch_rejected(self):
        other = {"target": "CCN", "reactions": [{"product": "CCN", "reactants": ["C", "N"]}]}
        with self.assertRaises(ValueError):
            compare_routes([route()], [other], [], proxy())

    def test_no_baseline_no_claim(self):
        result = compare_routes([], [route()], [quote("CC"), quote("O")], proxy())
        self.assertIsNone(result["best_baseline"])
        self.assertEqual(result["improvements"], [])

    def test_unknown_baseline_is_disclosed(self):
        result = compare_routes([route(), route(("N",))], [route(("C",))],
                                [quote("CC"), quote("O"), quote("C", "1")], proxy())
        self.assertEqual(result["unknown_baseline_count"], 1)
        self.assertIn("fully valued", result["comparison_scope"])


if __name__ == "__main__":
    unittest.main()
