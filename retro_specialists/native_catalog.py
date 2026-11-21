"""Extract native pack-price observations without inventing quote provenance.

The public tool lacks currency, observation date, assay and offer-to-vendor
mapping. Numerical mass normalization preserves unknown native price units;
these observations deliberately are not PriceQuote objects or monetary inputs.
"""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .graphs import canonical_smiles

_PACK = re.compile(r'([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(MG|KG|G)', re.I)
_FACTORS = {'MG': Decimal('.001'), 'G': Decimal(1), 'KG': Decimal(1000)}


def _decimal(value):
    try:
        number=Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('Invalid pack number') from error
    if (not number.is_finite() or number<=0 or len(number.as_tuple().digits)>64
            or abs(number.adjusted())>50):
        raise ValueError('Pack numbers must be positive, finite and bounded')
    return number


def _number(value):
    if type(value) not in (float,int):
        raise ValueError('Native pack price must be a JSON number')
    return _decimal(value)


def _text(number):
    raw=format(number,'f')
    return raw.rstrip('0').rstrip('.') if '.' in raw else raw


def _urls(value):
    if not isinstance(value,list):return []
    if len(value)>1000:raise ValueError('Too many source links')
    result=set()
    for link in value:
        if not isinstance(link,str) or len(link)>4096:continue
        try:
            parsed=urlsplit(link)
            if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.hostname.lower()=='localhost'):continue
            try:
                if not ipaddress.ip_address(parsed.hostname).is_global:continue
            except ValueError:pass
            result.add(link)
        except ValueError:continue
    return sorted(result)


def _offer(label, price):
    if not isinstance(label,str) or len(label)>100:
        raise ValueError('Invalid pack label')
    match=_PACK.fullmatch(label.strip())
    if match is None:raise ValueError('Pack size must be explicit g, mg or kg')
    grams=_decimal(match[1])*_FACTORS[match[2].upper()]
    amount=_number(price)
    identity={'pack_grams':_text(grams),'native_pack_price':_text(amount)}
    return {**identity,'offer_id':sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
        'pack_label':label,'native_units_per_gram':_text(amount/grams),
        'ambiguous_legacy_fallback_value':amount==262,
        'normalization_basis':'Native price units / labeled gross grams; currency and active assay unknown.'}


def _material(molecule):
    return {'molecule':molecule,'currency':None,'observed_at':None,'assay':None,
        'offers':[],'source_urls':[],'route_ids':[],
        'source_offer_mapping':'unresolved_no_positional_mapping',
        'eligible_for_monetary_comparison':False,'multiple_prices_for_pack':False}


def _collect(node, route_id, materials, rejections):
    try:molecule=canonical_smiles(node.get('smile',node.get('smiles')))
    except ValueError:
        rejections.append({'route_id':route_id,'reason':'Invalid terminal molecular identity'});return
    material=materials.setdefault(molecule,_material(molecule))
    material['route_ids']=sorted(set(material['route_ids'])|{route_id})
    material['source_urls']=sorted(set(material['source_urls'])|set(_urls(node.get('building_block_path'))))
    costs=node.get('cost')
    if costs is None or costs in ('None','Not available'):return
    if not isinstance(costs,list):
        rejections.append({'route_id':route_id,'molecule':molecule,'reason':'Cost is not a pack array'});return
    if len(costs)>1000:raise ValueError('Too many pack offerings')
    for pack in costs:
        if not isinstance(pack,dict) or len(pack)!=1:
            rejections.append({'route_id':route_id,'molecule':molecule,'reason':'Pack requires one quantity/price pair'});continue
        label,price=next(iter(pack.items()))
        try:offer=_offer(label,price)
        except ValueError as error:
            rejections.append({'route_id':route_id,'molecule':molecule,'reason':str(error)});continue
        if offer['offer_id'] not in {row['offer_id'] for row in material['offers']}:
            material['offers'].append(offer)
    by_size={}
    for offer in material['offers']:
        by_size.setdefault(offer['pack_grams'],set()).add(offer['native_pack_price'])
    material['multiple_prices_for_pack']=any(len(prices)>1 for prices in by_size.values())


def extract_catalog_observations(public_output, source_sha256):
    """Read topN.path leaves only; retain every distinct normalized offering.

    The host supplies an artifact digest, preferably of verified downloaded bytes.
    Even one URL per offer would not establish a positional association; sources
    and offerings remain separate. No missing-price fallback is synthesized.
    """
    if not isinstance(source_sha256,str) or not re.fullmatch('[a-f0-9]{64}',source_sha256):
        raise ValueError('Exact source-artifact SHA-256 required')
    if not isinstance(public_output,dict) or len(public_output)>10000:
        raise ValueError('Bounded topN output object required')
    materials,rejections,metrics={ },[],[]
    visited=0
    for route_id,entry in public_output.items():
        if not isinstance(route_id,str) or not re.fullmatch(r'top\d+',route_id):continue
        if not isinstance(entry,dict) or not isinstance(entry.get('path'),dict):
            raise ValueError('Each topN entry requires an object path')
        root=entry['path']
        metrics.append({'route_id':route_id,'native_path_cost':root.get('path_cost'),
            'planner_total_cost':root.get('total_cost'),
            'path_cost_meaning':'Legacy aggregate of minimum pack prices, possibly including fallback values; not a scale-normalized synthesis cost.',
            'total_cost_meaning':'Planner/model-score cost; not monetary synthesis cost.'})
        pending=[(root,frozenset())]
        while pending:
            node,ancestors=pending.pop();visited+=1
            if visited>100000 or len(ancestors)>128:raise ValueError('Route tree exceeds traversal bound')
            if not isinstance(node,dict):raise ValueError('Route tree nodes must be objects')
            if id(node) in ancestors:raise ValueError('Cyclic input tree')
            children=node.get('children',[])
            if not isinstance(children,list):raise ValueError('Children must be an array')
            if children:
                next_ancestors=ancestors|{id(node)}
                pending.extend((child,next_ancestors) for child in reversed(children))
            elif node.get('reference') not in ('True',True):
                _collect(node,route_id,materials,rejections)
    values=[materials[key] for key in sorted(materials)]
    return {'format_version':1,'source_artifact_sha256':source_sha256,
        'source_hash_scope':'Host-supplied artifact digest; caller must verify exact downloaded bytes.',
        'route_count':len(metrics),'materials':values,'route_metrics':metrics,
        'priced_material_count':sum(bool(row['offers']) for row in values),
        'unpriced_material_count':sum(not row['offers'] for row in values),
        'rejections':rejections,'monetary_cost_reduction_established':False,
        'limitations':['Currency, retrieval date, assay and deployed catalog identity are not present in the public JSON.',
            'Supplier URLs and pack offers are not paired; never zip their positions to invent attribution.',
            'Value 262 may coincide with the legacy missing-price fallback and needs explicit source review.',
            'Per-gram values are native catalog units, not verified USD or EUR prices.',
            'Use verified source/offer attribution, currency, timestamp and scale assumptions before monetary conversion.']}
