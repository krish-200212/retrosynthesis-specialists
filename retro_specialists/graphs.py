"""Topology and molecular-identity checks for untested route hypotheses.

Canonical identity removes atom-map labels only. Salts, isotopes, charge and
encoded stereochemistry are retained; no tautomer or protonation normalization
is performed. Graph checks, element-presence warnings and catalog membership
are not chemical validation.

Synthesis hypotheses exclude hydrate/component dissociation that merely removes
extra disconnected components from an otherwise unchanged whole ancestor. This
is a route-validity rule, not salt stripping: molecular IDs remain unchanged.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from collections.abc import Iterable
from collections import Counter

from rdkit import Chem, rdBase

from .contracts import (ContractError, MAX_SMILES_CHARS, parse_response,
                        positive_limit, response_schema)


def canonical_smiles(smiles: str) -> str:
    if (not isinstance(smiles, str) or not smiles or len(smiles) > MAX_SMILES_CHARS
            or any(char.isspace() for char in smiles)):
        raise ValueError("SMILES must be nonempty, bounded and contain no whitespace")
    settings = Chem.SmilesParserParams()
    settings.parseName = False
    settings.allowCXSMILES = False
    with rdBase.BlockLogs():
        molecule = Chem.MolFromSmiles(smiles, settings)
    if molecule is None or not molecule.GetNumAtoms():
        raise ValueError("Invalid or unsanitizable SMILES")
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() == 0:
            raise ValueError("Unresolved wildcard atom")
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)


def _reactions(route: dict) -> list[dict]:
    if not isinstance(route, dict) or not isinstance(route.get("reactions"), list):
        raise ValueError("Route needs an explicit reaction array")
    if not 1 <= len(route["reactions"]) <= 200:
        raise ValueError("Reaction count outside structural limit")
    result = []
    for reaction in route["reactions"]:
        if not isinstance(reaction, dict) or set(reaction) != {"product", "reactants"}:
            raise ValueError("Reaction must contain only product and reactants")
        precursors = reaction["reactants"]
        if not isinstance(precursors, list) or not 1 <= len(precursors) <= 32:
            raise ValueError("Reaction needs a bounded nonempty precursor list")
        result.append({"product": canonical_smiles(reaction["product"]),
                       "reactants": sorted(canonical_smiles(item) for item in precursors)})
    return result


def _key(reactions: list[dict]) -> str:
    edges = sorted((r["product"], tuple(r["reactants"])) for r in reactions)
    return sha256(json.dumps(edges, separators=(",", ":")).encode()).hexdigest()


def graph_key(route: dict) -> str:
    """Canonical connectivity identity; reactant multiplicity is significant."""
    return _key(_reactions(route))


def check_component_ancestry(producers: dict[str, list[str]]) -> None:
    """Reject a complete ancestor embedded unchanged in one precursor mixture.

    Callers first validate graph shape and canonical isomeric molecular IDs.
    Component multiplicities are retained. Sharing only a counterion, changing
    protonation/isotopes/stereo, or replacing one component is not containment.
    Every ancestor's reachable subgraph is checked independently so a shared
    node visited on another path cannot bypass this rule. Identity, stock and
    price keys are never rewritten. Explicit hydrate/component dissociation is
    excluded from this synthesis-hypothesis contract.
    """
    molecules = set(producers) | {value for values in producers.values() for value in values}
    components = {}
    for value in molecules:
        if '.' not in value:
            components[value] = Counter({value: 1})
        else:
            molecule = Chem.MolFromSmiles(value)
            components[value] = Counter(Chem.MolToSmiles(fragment, canonical=True, isomericSmiles=True)
                for fragment in Chem.GetMolFrags(molecule, asMols=True, sanitizeFrags=True))
    for ancestor, precursors in producers.items():
        pending, visited = list(precursors), set()
        while pending:
            precursor = pending.pop()
            if components[ancestor] <= components[precursor]:
                raise ValueError('Precursor contains unchanged whole product or ancestor components')
            if precursor not in visited:
                visited.add(precursor)
                pending.extend(producers.get(precursor, ()))


def _topology(reactions: list[dict], target: str) -> tuple[int, list[str]]:
    producers = {}
    for reaction in reactions:
        product = reaction["product"]
        if product in producers:
            raise ValueError("Multiple transformations produce the same intermediate")
        if product in reaction["reactants"]:
            raise ValueError("Self-reaction")
        producers[product] = reaction["reactants"]
    if target not in producers:
        raise ValueError("No reaction produces the exact target")
    active, visited, depths, leaves = set(), set(), {}, set()

    def visit(product):
        if product in active:
            raise ValueError("Cyclic route")
        if product in depths:
            return depths[product]
        if product not in producers:
            leaves.add(product)
            return 0
        active.add(product)
        depth = 1 + max(visit(precursor) for precursor in producers[product])
        active.remove(product)
        visited.add(product)
        depths[product] = depth
        return depth

    depth = visit(target)
    if visited != set(producers):
        raise ValueError("Disconnected reactions")
    check_component_ancestry(producers)
    return depth, sorted(leaves)


def _element_warnings(reactions: list[dict]) -> list[dict]:
    def elements(smiles):
        mol = Chem.MolFromSmiles(smiles)
        return {atom.GetSymbol() for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1}
    warnings = []
    for index, reaction in enumerate(reactions):
        available = set().union(*(elements(item) for item in reaction["reactants"]))
        missing = elements(reaction["product"]) - available
        if missing:
            warnings.append({"reaction_index": index, "missing_elements": sorted(missing),
                             "meaning": "No listed precursor contains this product element; chemistry unresolved"})
    return warnings


def route_graph(route: dict, target: str, max_depth: int = 5,
                max_total_reactions: int = 20) -> dict:
    positive_limit(max_depth, "max_depth", 100)
    positive_limit(max_total_reactions, "max_total_reactions", 200)
    canonical_target = canonical_smiles(target)
    reactions = _reactions(route)
    if len(reactions) > max_total_reactions:
        raise ValueError("Total reaction limit exceeded")
    depth, leaves = _topology(reactions, canonical_target)
    if depth > max_depth:
        raise ValueError("Longest-path depth limit exceeded")
    from .stereochemistry import validate_stereo_plan
    validate_stereo_plan({**route, 'reactions': reactions}, canonical_target)
    return {**deepcopy(route), "target": canonical_target, "reactions": reactions,
            "graph_key": _key(reactions), "leaves": leaves, "depth": depth,
            "total_reactions": len(reactions), "chemistry_status": "unassessed",
            "element_source_warnings": _element_warnings(reactions)}


_SELF_DISQUALIFICATION = re.compile(
    r"\b(?:this|the|my)\s+(?:proposed\s+)?route\s+(?:is|remains)\s+"
    r"(?:invalid|impossible|not\s+(?:viable|feasible|chemically\s+feasible))\b"
    r"|\b(?:should|must)\s+be\s+rejected\b"
    r"|\b(?:do\s+not\s+use\s+this\s+route|I\s+withdraw\s+this\s+route)\b",
    re.IGNORECASE)
_GLOBAL_DISQUALIFICATION = re.compile(
    r"\b(?:all|both)\s+(?:the\s+)?(?:proposed\s+)?routes\s+are\s+"
    r"(?:invalid|impossible|not\s+(?:viable|feasible))\b", re.IGNORECASE)


def _disqualification(route: dict, limitations: list[str]) -> str | None:
    if route.get("self_assessment") == "withdrawn":
        return "Model explicitly withdrew this proposal"
    text = "\n".join([route.get("rationale", ""), *route.get("feasibility_risks", [])])
    if _SELF_DISQUALIFICATION.search(text) or _GLOBAL_DISQUALIFICATION.search("\n".join(limitations)):
        return "Heuristic self-disqualification screen requires review; not a chemical verdict"
    return None


def validate_response(raw: str, target: str, requested_count: int = 5,
                      max_depth: int = 5, max_total_reactions: int = 20,
                      inventory: Iterable[str] = (), *, require_stereo_plan: bool = False,
                      require_origin_declarations: bool = False) -> dict:
    """Keep graph-valid hypotheses, separating stock and chemistry status.

    ``unique`` counts distinct structurally valid graphs, including withdrawn
    proposals. ``retained`` counts undisqualified distinct graph hypotheses;
    it is not a chemical acceptance count. ``rejected`` counts input proposals
    not retained, including duplicates. A malformed payload with no recoverable
    route array has proposed/rejected=0 and an explicit payload rejection.
    """
    response_schema(requested_count, max_total_reactions, require_stereo_plan=require_stereo_plan,
                    require_origin_declarations=require_origin_declarations)
    positive_limit(max_depth, "max_depth", 100)
    canonical_target = canonical_smiles(target)
    if isinstance(inventory, (str, bytes, dict)):
        raise ValueError("Inventory must be an iterable of exact molecular structures")
    stock = frozenset(canonical_smiles(item) for item in inventory)
    counts = {key: 0 for key in ("proposed", "schema_valid", "structurally_valid", "unique",
                                "stock_connected", "chemically_assessed", "retained", "rejected")}
    result = {"routes": [], "counts": counts, "rejections": [], "limitations": [],
              "retention_basis": "structurally_valid_hypotheses",
              "self_disqualification_screen": "Explicit withdrawal plus limited text heuristics; not chemical validation"}
    try:
        parsed = parse_response(raw, requested_count, max_total_reactions,
                                require_stereo_plan=require_stereo_plan,
                                require_origin_declarations=require_origin_declarations)
    except ContractError as exc:
        counts.update(proposed=exc.proposed, rejected=exc.proposed)
        result["rejections"].append({"stage": "schema", "reason": str(exc), "route_id": None})
        return result
    result["limitations"] = list(parsed["limitations"])
    counts["proposed"] = counts["schema_valid"] = len(parsed["routes"])
    unique, retained = set(), set()
    for index, route in enumerate(parsed["routes"]):
        try:
            graph = route_graph(route, canonical_target, max_depth, max_total_reactions)
        except ValueError as exc:
            result["rejections"].append({"index": index, "route_id": route["route_id"],
                                         "stage": "structure", "reason": str(exc)})
            continue
        counts["structurally_valid"] += 1
        unique.add(graph["graph_key"])
        reason = _disqualification(route, parsed["limitations"])
        stage = "self_disqualification"
        if reason is None and graph["graph_key"] in retained:
            reason, stage = "Duplicate canonical reaction connectivity", "deduplication"
        if reason:
            result["rejections"].append({"index": index, "route_id": route["route_id"],
                                         "stage": stage, "reason": reason})
            continue
        retained.add(graph["graph_key"])
        graph["stock_connected"] = set(graph["leaves"]).issubset(stock)
        graph["unknown_starting_materials"] = sorted(set(graph["leaves"]) - stock)
        result["routes"].append(graph)
    counts.update(unique=len(unique), retained=len(result["routes"]),
                  stock_connected=sum(item["stock_connected"] for item in result["routes"]))
    counts["rejected"] = counts["proposed"] - counts["retained"]
    return result
