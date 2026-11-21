"""Additional diversity lens; never modifies the validated reaction graphs."""
import json
from hashlib import sha256
from rdkit import Chem
from .graphs import canonical_smiles

def organic_route_key(route):
    def organic(value):
        return any(atom.GetAtomicNum()==6 for atom in Chem.MolFromSmiles(value).GetAtoms())
    reactions=[]
    for reaction in route['reactions']:
        product=canonical_smiles(reaction['product'])
        reactants=[canonical_smiles(value) for value in reaction['reactants']]
        if organic(product):
            reactions.append((product,tuple(sorted(value for value in reactants if organic(value)))))
    return sha256(json.dumps(sorted(reactions),separators=(',',':')).encode()).hexdigest()

def compare_diversity(baseline,candidates):
    before={organic_route_key(route) for route in baseline}
    after={organic_route_key(route) for route in candidates}
    return {'baseline_organic_sequences':len(before),
            'additional_organic_sequences':len(after-before),
            'scope':'Carbon-containing products and precursors only, preserving stereochemistry and multiplicity. A diversity diagnostic, not chemical validation; full graphs remain unchanged.'}
