"""Sourced precursor-material arithmetic with explicit, comparable assumptions.

Quotes/measurements are trusted backend inputs, never model assertions. A proxy
is a declared scenario; an evidence-supported estimate needs measured reaction
parameters for every step. Neither establishes chemistry or manufacturing cost.
All quoted pack prices are prorated; pack rounding/minimum orders are excluded.
The shared synthesis-graph policy rejects unchanged ancestors embedded in
precursor mixtures, including explicit hydrate/component dissociation; cost
evaluation does not strip salts or change molecular identities to price them.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import re

from rdkit import Chem
from rdkit.Chem import Descriptors

from .graphs import canonical_smiles, check_component_ancestry, graph_key


MODES = ("inventory_catalog_price_proxy", "evidence_supported_precursor_cost")
EXCLUSIONS = (
    "Only listed terminal precursor materials; excludes unlisted reagents, "
    "catalysts, solvents, labor, energy, purification, waste, shipping and taxes.",
    "Linear consumption pricing; excludes purchasing whole packs and minimum orders.",
    "Quote and reaction-parameter applicability must be reviewed at the requested scale.",
)


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("A bounded decimal number is required")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal") from exc
    if (not result.is_finite() or result < 0 or positive and result == 0
            or len(result.as_tuple().digits) > 64
            or result != 0 and abs(result.adjusted()) > 50):
        raise ValueError("Number is nonfinite, negative or exceeds numeric bounds")
    return result


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(label + " must be nonempty bounded text")
    return value


def _molecule(value):
    return canonical_smiles(value)


def _string(value):
    if value == 0:
        return "0"
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PriceQuote:
    molecule: str
    currency: str
    amount_per_mol: Decimal
    source: str
    retrieved_at: str

    def __post_init__(self):
        if _molecule(self.molecule) != self.molecule:
            raise ValueError("Quote molecule must be canonical")
        if not isinstance(self.amount_per_mol, Decimal):
            raise ValueError("Normalized quote amount must be a Decimal")
        _number(self.amount_per_mol)
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("Currency must be an uppercase three-letter code")
        _text(self.source, "Quote source")
        try:
            moment = datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError("Quote retrieval time must include timezone")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("Invalid quote retrieval timestamp") from exc

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Quote must be an object")
        required = {"molecule", "currency", "amount", "quantity", "unit", "source", "retrieved_at"}
        if not required <= value.keys() or value.keys() - required - {"package_quantity", "package_unit"}:
            raise ValueError("Unexpected or missing quote fields")
        molecule = _molecule(value["molecule"])
        amount, quantity = _number(value["amount"]), _number(value["quantity"], positive=True)
        unit = value["unit"]
        if unit == "package":
            quantity *= _number(value.get("package_quantity"), positive=True)
            unit = value.get("package_unit")
        elif "package_quantity" in value or "package_unit" in value:
            raise ValueError("Package fields require package units")
        factors = {"mol": Decimal(1), "mmol": Decimal("0.001")}
        if unit in ("g", "mg"):
            mass = quantity * (Decimal("0.001") if unit == "mg" else Decimal(1))
            mw = Decimal(str(Descriptors.MolWt(Chem.MolFromSmiles(molecule))))
            quantity = mass / mw
        elif unit in factors:
            quantity *= factors[unit]
        else:
            raise ValueError("Unsupported quote quantity unit")
        return cls(molecule, value["currency"], amount / quantity,
                   value["source"], value["retrieved_at"])


def reaction_key(reaction):
    return _hash((_molecule(reaction["product"]), sorted(_molecule(s) for s in reaction["reactants"])))


def _graph(route):
    if not isinstance(route, dict) or not isinstance(route.get("reactions"), list):
        raise ValueError("A reaction graph is required")
    if not 1 <= len(route["reactions"]) <= 200:
        raise ValueError("Route reaction count outside supported bounds")
    indexed = {}
    for reaction in route["reactions"]:
        product = _molecule(reaction["product"])
        raw = reaction["reactants"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
            raise ValueError("Every reaction needs bounded reactants")
        reactants = tuple(_molecule(s) for s in raw)
        if product in indexed or product in reactants:
            raise ValueError("Duplicate producer or self reaction")
        indexed = {**indexed, product: {"product": product, "reactants": list(reactants)}}
    roots = set(indexed) - {s for r in indexed.values() for s in r["reactants"]}
    target = _molecule(route["target"]) if "target" in route else next(iter(roots)) if len(roots) == 1 else None
    if target is None or target not in indexed or roots != {target}:
        raise ValueError("Route has no unique exact target root")
    active, visited, postorder = set(), set(), []

    def visit(molecule):
        if molecule in active:
            raise ValueError("Route cycle")
        if molecule in visited:
            return
        active.add(molecule)
        if molecule in indexed:
            for precursor in indexed[molecule]["reactants"]:
                visit(precursor)
        active.remove(molecule)
        visited.add(molecule)
        postorder.append(molecule)

    visit(target)
    if not set(indexed) <= visited:
        raise ValueError("Disconnected reactions")
    check_component_ancestry({product: reaction['reactants'] for product, reaction in indexed.items()})
    key = graph_key({"reactions": list(indexed.values())})
    return target, indexed, tuple(reversed(postorder)), key


def _quotes(values, currency):
    if not isinstance(values, (tuple, list)) or len(values) > 10000:
        raise ValueError("A bounded quote list is required")
    result = {}
    for raw in values:
        quote = raw if isinstance(raw, PriceQuote) else PriceQuote.from_mapping(raw)
        if quote.currency != currency:
            continue
        previous = result.get(quote.molecule)
        if previous and previous.amount_per_mol != quote.amount_per_mol:
            raise ValueError("Conflicting prices need an explicit backend quote selection")
        result = {**result, quote.molecule: quote}
    return result


def _parameters(reaction, settings, mode):
    key = reaction_key(reaction)
    parameters = settings.get("reaction_parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("Reaction parameters must be an object")
    params = parameters.get(key, {})
    if not isinstance(params, dict):
        raise ValueError("Reaction parameters must be an object")
    missing = []
    if mode == "evidence_supported_precursor_cost":
        if params.get("evidence_kind") != "measured" or not params.get("source"):
            return None, {}, ["measured_parameters:" + key]
        _text(params["source"], "Measurement source")
    default_yield = settings.get("default_yield") if mode == "inventory_catalog_price_proxy" else None
    raw_yield = params.get("yield_fraction", default_yield)
    yield_fraction = _number(raw_yield, positive=True) if raw_yield is not None else None
    if yield_fraction is None:
        missing.append("yield:" + key)
    elif yield_fraction > 1:
        raise ValueError("Yield fraction cannot exceed one")
    equivalents = params.get("equivalents", {})
    if not isinstance(equivalents, dict):
        raise ValueError("Equivalents must map molecules to molar equivalents")
    normalized = {_molecule(molecule): _number(value, positive=True) for molecule, value in equivalents.items()}
    default_eq = settings.get("default_equivalents") if mode == "inventory_catalog_price_proxy" else None
    values = {}
    for molecule in reaction["reactants"]:
        raw = normalized.get(molecule, default_eq)
        if raw is None:
            missing.append("equivalents:" + key + ":" + molecule)
        else:
            values = {**values, molecule: _number(raw, positive=True)}
    return yield_fraction, values, missing


def evaluate_route(route, quotes, assumptions):
    """Return a complete estimate for the declared scope, or unknown.

    ``default_yield``/``default_equivalents`` are explicit proxy assumptions.
    Evidence mode requires backend-curated ``reaction_parameters`` for every
    canonical reaction key, including measured yield, equivalents and source.
    Repeated reactants consume repeated equivalents. No silent price selection,
    currency conversion, unit inference or partial-sum reporting is performed.
    """
    if not isinstance(assumptions, dict):
        raise ValueError("Assumptions must be an object")
    target, indexed, order, key = _graph(route)
    mode, currency = assumptions.get("mode"), assumptions.get("currency")
    if mode is not None and mode not in MODES:
        raise ValueError("Unknown cost basis")
    if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise ValueError("Invalid currency")
    raw_target = assumptions.get("target_mol")
    target_mol = _number(raw_target, positive=True) if raw_target is not None else None
    indexed_quotes = _quotes(quotes, currency)
    leaves = tuple(sorted(set(order) - set(indexed)))
    missing = [name for name, value in (("mode", mode), ("currency", currency), ("target_mol", target_mol)) if value is None]
    missing.extend("quote:" + molecule for molecule in leaves if molecule not in indexed_quotes)
    params = {}
    for product, reaction in indexed.items():
        yld, eq, absent = _parameters(reaction, assumptions, mode)
        params = {**params, product: (yld, eq)}
        missing.extend(absent)
    demands, amount = {}, None
    if not missing:
        with localcontext() as context:
            context.prec = 64
            demands = {target: target_mol}
            for molecule in order:
                if molecule not in indexed:
                    continue
                yld, equivalents = params[molecule]
                for precursor, count in Counter(indexed[molecule]["reactants"]).items():
                    demand = demands[molecule] / yld * equivalents[precursor] * count
                    demands = {**demands, precursor: demands.get(precursor, Decimal(0)) + demand}
            amount = sum((demands[m] * indexed_quotes[m].amount_per_mol for m in leaves), Decimal(0))
    return {"graph_key": key, "target": target, "status": "unknown" if missing else "known",
            "basis": mode, "currency": currency, "target_mol": _string(target_mol) if target_mol is not None else None,
            "amount": _string(amount) if amount is not None else None,
            "leaf_demands_mol": {m: _string(demands[m]) for m in leaves if m in demands},
            "missing": sorted(set(missing)), "exclusions": list(EXCLUSIONS),
            "assumptions": json.loads(json.dumps(assumptions, default=str)),
            "quote_sources": sorted({indexed_quotes[m].source for m in leaves if m in indexed_quotes}),
            "quote_evidence": [{"molecule": m, "currency": indexed_quotes[m].currency,
                "amount_per_mol": _string(indexed_quotes[m].amount_per_mol),
                "source": indexed_quotes[m].source, "retrieved_at": indexed_quotes[m].retrieved_at}
                for m in leaves if m in indexed_quotes],
            "chemical_feasibility_established": False}


def compare_routes(baseline_routes, candidate_routes, quotes, assumptions):
    """Compare distinct candidates against the cheapest fully valued baseline.

    All routes use the same request, currency and assumptions. Unknowns remain
    in the returned collections and never establish savings. Reductions in a
    proxy remain proxy reductions; even measured input costs exclude operations.
    """
    baseline = [evaluate_route(route, quotes, assumptions) for route in baseline_routes]
    candidates = [evaluate_route(route, quotes, assumptions) for route in candidate_routes]
    if len({value["target"] for value in (*baseline, *candidates)}) > 1:
        raise ValueError("Cost comparison requires the same exact target")
    known = [value for value in baseline if value["status"] == "known"]
    best = min(known, key=lambda value: Decimal(value["amount"])) if known else None
    baseline_keys = {value["graph_key"] for value in baseline}
    improvements = []
    if best is not None and Decimal(best["amount"]) > 0:
        for candidate in candidates:
            if candidate["status"] != "known" or candidate["graph_key"] in baseline_keys:
                continue
            before, after = Decimal(best["amount"]), Decimal(candidate["amount"])
            if after < before:
                improvements.append({"graph_key": candidate["graph_key"], "baseline_graph_key": best["graph_key"],
                    "basis": candidate["basis"], "reduction_amount": _string(before - after),
                    "reduction_percent": _string((before - after) / before * Decimal(100))})
    return {"baseline": baseline, "candidates": candidates, "best_baseline": best,
            "improvements": improvements, "demonstrated_total_synthesis_cost_reduction": False,
            "unknown_baseline_count": len(baseline) - len(known),
            "comparison_scope": "Cheapest fully valued baseline under the same declared assumptions; unknown routes may be cheaper.",
            "interpretation": "Comparable precursor-material estimate or explicit scenario only; chemistry unassessed."}
