import json
import unittest

from retro_specialists.contracts import ContractError, parse_response, response_schema


def route():
    return {"route_id": "r1", "name": "example", "reactions": [
        {"product": "CCO", "reactants": ["CC=O"]}],
        "rationale": "A hypothesis", "feasibility_risks": [],
        "evidence_ids": [], "self_assessment": "proposed"}


class ContractTests(unittest.TestCase):
    def parse(self, obj, **kwargs):
        return parse_response(json.dumps(obj), **kwargs)

    def test_valid_and_empty(self):
        obj = {"routes": [route()], "limitations": []}
        self.assertEqual(self.parse(obj), obj)
        self.assertEqual(self.parse({"routes": [], "limitations": []})["routes"], [])

    def test_schema_is_independent_per_request(self):
        first = response_schema(1)
        second = response_schema(20)
        first["properties"]["routes"]["maxItems"] = 99
        self.assertEqual(second["properties"]["routes"]["maxItems"], 20)
        self.assertEqual(response_schema(1)["properties"]["routes"]["maxItems"], 1)

    def test_bad_configuration(self):
        for count in [0, 21, True, 1.5, "2"]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                response_schema(count)

    def test_invalid_json_duplicate_keys_and_nonfinite(self):
        for raw in ['{', '{"routes":[],"routes":[],"limitations":[]}',
                    '{"routes":[],"limitations":[NaN]}', '[]']:
            with self.subTest(raw=raw), self.assertRaises(ContractError):
                parse_response(raw)

    def test_wrong_types_and_additional_fields(self):
        for obj in [{"routes": {}, "limitations": []},
                    {"routes": [], "limitations": [], "confidence": 1},
                    {"routes": [dict(route(), confidence=1)], "limitations": []},
                    {"routes": [dict(route(), evidence_ids="citation")], "limitations": []}]:
            with self.subTest(obj=obj), self.assertRaises(ContractError):
                self.parse(obj)

    def test_every_field_required(self):
        for key in route():
            bad = {k: v for k, v in route().items() if k != key}
            with self.subTest(key=key), self.assertRaises(ContractError):
                self.parse({"routes": [bad], "limitations": []})

    def test_empty_reactions_and_precursors(self):
        for reactions in [[], [{"product": "CCO", "reactants": []}]]:
            with self.subTest(reactions=reactions), self.assertRaises(ContractError):
                self.parse({"routes": [dict(route(), reactions=reactions)], "limitations": []})

    def test_output_and_requested_count_bounds(self):
        with self.assertRaises(ContractError):
            self.parse({"routes": [route(), route()], "limitations": []}, requested_count=1)
        with self.assertRaises(ContractError):
            parse_response(" " * 1_048_577)
        with self.assertRaises(ContractError):
            parse_response(b'{}')
        with self.assertRaises(ContractError):
            parse_response('\ud800')


if __name__ == "__main__":
    unittest.main()
