"""Bounded literature retrieval and backend-owned reaction evidence.

Search hits are context, never reaction validation. Only operator-curated records
can assert that a reviewed source contains an exact reaction. Even those records
do not establish that a newly proposed route will work experimentally.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from html import unescape
import http.client
import json
import math
import os
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlsplit, quote
from uuid import uuid4

from rdkit import Chem


_HOST = 'www.ebi.ac.uk'
_PATH = '/europepmc/webservices/rest/search'
_MAX_BODY = 1_048_576
_FIELDS = frozenset(('id', 'title', 'url', 'doi', 'excerpt', 'retrieved_at',
    'sha256', 'source_kind', 'retrieval_scope', 'matching_reactions', 'reviewer', 'locator'))


def _text(value, label, maximum=2000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f'{label} must be bounded text')
    return value


def _hash(record):
    payload = {key: value for key, value in record.items() if key != 'sha256'}
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _canonical(smiles):
    _text(smiles, 'SMILES', 4096)
    parser = Chem.SmilesParserParams()
    parser.parseName = False
    molecule = Chem.MolFromSmiles(smiles, parser)
    if molecule is None:
        raise ValueError('Invalid evidence SMILES')
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)


def _reaction(value):
    if not isinstance(value, dict) or set(value) != {'product', 'reactants'}:
        raise ValueError('Evidence reaction requires product and reactants')
    reactants = value['reactants']
    if not isinstance(reactants, list) or not 1 <= len(reactants) <= 32:
        raise ValueError('Evidence precursors must be nonempty and bounded')
    return {'product': _canonical(value['product']),
            'reactants': sorted(_canonical(item) for item in reactants)}


def _signature(value):
    reaction = _reaction(value)
    return reaction['product'], tuple(reaction['reactants'])


def _url(value):
    _text(value, 'Source URL', 4096)
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Evidence requires a public HTTPS source URL')
    return value


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _make_record(**fields):
    record = {**fields, 'sha256': ''}
    return {**record, 'sha256': _hash(record)}


def curated_evidence(*, identifier, title, url, doi, excerpt, reactions,
                     reviewer, locator, retrieved_at=None):
    """Create an explicitly backend-curated record; never call on LLM assertions.

    The operator must have reviewed the specified source location and verified
    the encoded substrates. This constructor records that attestation; it cannot
    establish that the attestation is true. The record belongs in trusted config.
    """
    _text(reviewer, 'Evidence reviewer', 300)
    _text(locator, 'Source location', 1000)
    if not isinstance(reactions, (list, tuple)) or not 1 <= len(reactions) <= 100:
        raise ValueError('Curated evidence requires bounded explicit reactions')
    record = _make_record(id=identifier, title=title, url=_url(url), doi=doi,
        excerpt=excerpt, retrieved_at=retrieved_at or _timestamp(),
        source_kind='curated_primary_source', retrieval_scope='curated_exact_reactions',
        matching_reactions=[_reaction(reaction) for reaction in reactions],
        reviewer=reviewer, locator=locator)
    return validate_evidence([record])[0]


def validate_evidence(records):
    """Validate integrity/shape, not the truth or trustworthiness of assertions.

    Evidence must be supplied by the backend. A hash is an integrity check, not a
    signature: accepting model-supplied records here would violate this boundary.
    """
    if not isinstance(records, (list, tuple)) or len(records) > 100:
        raise ValueError('Evidence collection must be bounded')
    seen, result = set(), []
    for record in records:
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise ValueError('Unexpected evidence fields')
        _text(record['id'], 'Evidence identifier', 300)
        if record['id'] in seen:
            raise ValueError('Duplicate evidence identifier')
        seen.add(record['id'])
        _text(record['title'], 'Evidence title', 2000)
        _text(record['excerpt'], 'Evidence excerpt', 12000, empty=True)
        _url(record['url'])
        if record['doi'] is not None:
            _text(record['doi'], 'DOI', 300)
            if not re.fullmatch(r'10\.\d{4,9}/[^\s]+', record['doi']):
                raise ValueError('Malformed DOI')
        stamp = datetime.fromisoformat(_text(record['retrieved_at'], 'Timestamp', 100))
        if stamp.tzinfo is None:
            raise ValueError('Evidence timestamp needs a timezone')
        if record['retrieval_scope'] == 'curated_exact_reactions':
            if record['source_kind'] != 'curated_primary_source':
                raise ValueError('Exact assertions require curated provenance')
            _text(record['reviewer'], 'Evidence reviewer', 300)
            _text(record['locator'], 'Source location', 1000)
            values = record['matching_reactions']
            if not isinstance(values, list) or not 1 <= len(values) <= 100:
                raise ValueError('Missing exact reaction assertions')
            if [_reaction(item) for item in values] != values:
                raise ValueError('Evidence reactions must be canonical')
        elif record['retrieval_scope'] == 'retrieved_context':
            if (record['source_kind'] != 'publication_metadata'
                    or record['matching_reactions'] != [] or record['reviewer'] != '' or record['locator'] != ''):
                raise ValueError('Retrieved context cannot assert reaction validation')
        else:
            raise ValueError('Unsupported evidence scope')
        if not isinstance(record['sha256'], str) or record['sha256'] != _hash(record):
            raise ValueError('Evidence content hash mismatch')
        result.append(json.loads(json.dumps(record)))
    return tuple(result)


def _fetch_bytes(url, timeout_seconds, maximum=_MAX_BODY):
    """Fetch pinned Europe PMC search/full-text paths without redirects."""
    parsed = urlsplit(url)
    fulltext = re.fullmatch(r'/europepmc/webservices/rest/PMC[0-9]+/fullTextXML', parsed.path)
    if parsed.scheme != 'https' or parsed.netloc != _HOST or (parsed.path != _PATH and not fulltext):
        raise ValueError('Unapproved literature endpoint')
    deadline = time.monotonic() + timeout_seconds
    connection = http.client.HTTPSConnection(_HOST, timeout=timeout_seconds)
    try:
        connection.request('GET', parsed.path + '?' + parsed.query,
            headers={'Accept': 'application/json', 'User-Agent': 'retro-specialists/0.3 research'})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Literature retrieval deadline reached')
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f'Literature service returned HTTP {response.status}')
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Compressed literature responses are not accepted')
        declared = response.getheader('Content-Length')
        if declared is not None and int(declared) > maximum:
            raise ValueError('Literature response exceeds byte limit')
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Literature retrieval deadline reached')
            socket = connection.sock
            if socket is None:
                socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if socket is not None:
                socket.settimeout(remaining)
            chunk = response.read1(min(8192, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise ValueError('Literature response exceeds byte limit')
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        connection.close()


def _fetch_json(url, timeout_seconds):
    if urlsplit(url).path != _PATH:
        raise ValueError('Unapproved literature search endpoint')
    return json.loads(_fetch_bytes(url, timeout_seconds).decode('utf-8'))


def _fetch_fulltext(pmcid, timeout_seconds):
    if not isinstance(pmcid, str) or not re.fullmatch(r'PMC[0-9]{1,12}', pmcid):
        raise ValueError('Full text requires a server-supplied PMC identifier')
    url = 'https://' + _HOST + '/europepmc/webservices/rest/' + pmcid + '/fullTextXML'
    return _fetch_bytes(url, timeout_seconds, maximum=2_000_000)


def _fulltext_excerpt(raw, query, maximum=6000):
    """Extract source prose, never inferred reaction graphs or reagent identities."""
    if not isinstance(raw, bytes) or len(raw) > 2_000_000 or b'\x00' in raw:
        raise ValueError('Full text must be bounded UTF-8 XML')
    text = raw.decode('utf-8')
    if re.search(r'<!\s*ENTITY|<!\s*DOCTYPE[^>]*\[', text, re.I):
        raise ValueError('XML entity declarations and internal subsets are forbidden')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError('Malformed primary-source XML') from error
    tag = lambda element: element.tag.rsplit('}', 1)[-1]
    body = next((item for item in root.iter() if tag(item) == 'body'), None)
    if body is None:
        raise ValueError('No primary-source body available')
    terms = set(re.findall(r'[a-z]{4,}', query.lower())) - {
        'study', 'synthesis', 'production', 'chemical', 'with', 'from', 'using', 'title', 'molecules'}
    ranked, seen = [], set()
    for section in (item for item in body.iter() if tag(item) == 'sec'):
        title_node = next((item for item in section if tag(item) == 'title'), None)
        heading = ' '.join(title_node.itertext()) if title_node is not None else 'Article body'
        if re.search(r'references|acknowledg|funding|conflict', heading, re.I):
            continue
        section_score = 8 if re.search(r'method|experiment|procedure', heading, re.I) else 3 if re.search(r'result', heading, re.I) else 0
        for paragraph in (item for item in section.iter() if tag(item) == 'p'):
            prose = ' '.join(' '.join(paragraph.itertext()).split())
            if not prose or prose in seen:
                continue
            seen.add(prose)
            score = section_score + sum(term in prose.lower() for term in terms)
            score += 2 * bool(re.search(r'reagent|oxidant|peroxide|reaction|catalyst', prose, re.I))
            if score:
                ranked.append((score, len(ranked), heading, prose[:2000]))
    if not ranked:
        raise ValueError('No relevant primary-source paragraphs available')
    selected = sorted(ranked, key=lambda row: (-row[0], row[1]))[:8]
    passage = '\n\n'.join('[' + heading + '] ' + prose for _, _, heading, prose in selected)
    return ('Full-text source passages; chemistry remains unassessed.\n' + passage)[:maximum]


def bounded_context(records, maximum=6000):
    """Bound total excerpt characters without changing original retrieval times."""
    values = validate_evidence(records)
    if type(maximum) is not int or not 500 <= maximum <= 12000:
        raise ValueError('Context budget must be 500 to 12000 characters')
    allowance = maximum // max(1, len(values))
    result = []
    for record in values:
        clipped = record['excerpt'][:allowance]
        if len(record['excerpt']) > allowance:
            clipped = clipped[:max(0, allowance - 24)] + ' [context truncated]'
        value = {**record, 'excerpt': clipped}
        result.append({**value, 'sha256': _hash(value)})
    return result


def _cache_key(query, limit, fulltext, context_chars):
    profile = {'query': query.strip(), 'limit': limit, 'fulltext': fulltext,
               'context_chars': context_chars, 'version': 1}
    return sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest(), profile


def _load_query_cache(path, profile):
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 1_048_576:
        raise ValueError('Invalid literature query cache file')
    payload = json.loads(path.read_text())
    if (not isinstance(payload, dict) or set(payload) != {'profile', 'stored_at', 'records', 'sha256'}
            or payload['profile'] != profile or payload['sha256'] != _hash(payload)):
        raise ValueError('Literature query cache integrity mismatch')
    validate_evidence(payload['records'])
    if any(row['retrieval_scope'] != 'retrieved_context' for row in payload['records']):
        raise ValueError('Literature cache cannot expose curated assessments')
    stamp = datetime.fromisoformat(payload['stored_at'])
    if stamp.tzinfo is None:
        raise ValueError('Literature cache timestamp needs a timezone')
    return payload


def _save_query_cache(path, profile, records):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {'profile': profile, 'stored_at': _timestamp(), 'records': records}
    payload = {**payload, 'sha256': _hash(payload)}
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(payload, stream, sort_keys=True, allow_nan=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    files = sorted(path.parent.glob('[a-f0-9]' * 64 + '.json'), key=lambda value: value.stat().st_mtime_ns)
    for expired in files[:-100]:
        expired.unlink(missing_ok=True)


def _age_seconds(stamp):
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        raise ValueError('Literature retrieval time needs a timezone')
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _doi_query(query):
    match = re.fullmatch(r'(?i)(?:DOI\s*:\s*|https://(?:dx\.)?doi.org/)?["\']?(10\.\d{4,9}/[^\s"\']+)["\']?(?:\s+([a-z0-9_ .-]+))?', query.strip())
    if match is None or (match.group(2) and re.search(r'\b(?:AND|OR|NOT)\b', match.group(2), re.I)):
        return None
    return match.group(1).lower()


def _cached_result(records, status, profile, *, error=None):
    retrieval = {'status': status, 'network_attempted': status in ('network', 'cache_fallback_after_failure'),
                 'query': profile['query'], 'fulltext_requested': profile['fulltext'],
                 'context_character_limit': profile['context_chars'],
                 'original_retrieved_at': {row['id']: row['retrieved_at'] for row in records}}
    if status in ('source_cache_hit', 'cache_refreshed_from_backend'):
        retrieval = {**retrieval, 'cache_match_basis': 'exact_doi_reference',
                     'query_scope': 'Matched the named DOI; additional query keywords were not independently searched.'}
    if error is not None:
        retrieval = {**retrieval, 'network_error_type': type(error).__name__,
                     'warning': 'Using previously retrieved source text after a network failure; timestamps are unchanged.'}
    return {'records': records, 'chemistry_status': 'source_text_only', 'retrieval': retrieval}


def cached_literature_search(args, trusted, load_records, persist_records):
    """Reuse bounded trusted context caches; no cache entry can assert chemistry."""
    query, limit = args['query'], args.get('max_results', 5)
    _text(query, 'Literature query', 1000)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Literature result limit must be 1 to 20')
    fulltext = trusted.get('literature_include_fulltext', True)
    maximum = trusted.get('literature_context_chars', 6000)
    ttl = trusted.get('literature_cache_seconds', 86400)
    if type(fulltext) is not bool or type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 <= ttl <= 604800:
        raise ValueError('Invalid literature cache configuration')
    bounded_context([], maximum)
    key, profile = _cache_key(query, limit, fulltext, maximum)
    path = Path(trusted['state_dir']) / 'literature-cache' / (key + '.json')
    cached = _load_query_cache(path, profile)
    fallback = None
    query_fresh = False
    if cached is not None and 0 <= _age_seconds(cached['stored_at']) <= 604800:
        fallback = cached['records']
        query_fresh = _age_seconds(cached['stored_at']) <= ttl
    doi = _doi_query(query)
    if doi:
        available = [row for row in validate_evidence(load_records(trusted['state_dir']))
                     if row['retrieval_scope'] == 'retrieved_context' and isinstance(row['doi'], str)
                     and row['doi'].lower() == doi and 0 <= _age_seconds(row['retrieved_at']) <= 604800]
        terms = set(re.findall(r'[a-z]{4,}', query.lower())) - {'https', 'with', 'from'}
        rank = lambda row: (sum(term in (row['title'] + ' ' + row['excerpt']).lower() for term in terms),
                            datetime.fromisoformat(row['retrieved_at']), len(row['excerpt']), row['id'])
        available.sort(key=rank, reverse=True)
        recent = [row for row in available if _age_seconds(row['retrieved_at']) <= ttl]
        if recent:
            # Refresh exact-source context before trusting a still-fresh query
            # snapshot. Keep timestamps and scopes; this adds no chemical proof.
            previous = [row for row in (fallback or []) if row['doi'] is not None
                        and row['doi'].lower() == doi and 0 <= _age_seconds(row['retrieved_at']) <= ttl]
            combined = {}
            for row in sorted(previous + recent, key=lambda row: (
                    datetime.fromisoformat(row['retrieved_at']), len(row['excerpt']))):
                existing = combined.get(row['id'])
                if existing and (existing['doi'], existing['url']) != (row['doi'], row['url']):
                    raise ValueError('Conflicting literature source identity')
                combined[row['id']] = row
            records = bounded_context(sorted(combined.values(), key=rank, reverse=True)[:limit], maximum)
            if query_fresh and records == fallback:
                return _cached_result(fallback, 'query_cache_hit', profile)
            _save_query_cache(path, profile, records)
            status = 'cache_refreshed_from_backend' if query_fresh else 'source_cache_hit'
            return _cached_result(records, status, profile)
        if available and fallback is None:
            fallback = bounded_context(available[:limit], maximum)
    if query_fresh:
        return _cached_result(fallback, 'query_cache_hit', profile)
    try:
        records = asyncio.run(search_literature(query, limit=limit,
            timeout_seconds=trusted.get('literature_timeout_seconds', 30),
            include_fulltext=fulltext, context_chars=maximum,
            fulltext_timeout_seconds=trusted.get('literature_fulltext_timeout_seconds', 8)))
    except (OSError, ValueError) as error:
        if fallback is None:
            raise
        return _cached_result(fallback, 'cache_fallback_after_failure', profile, error=error)
    records = list(validate_evidence(records))
    if any(row['retrieval_scope'] != 'retrieved_context' for row in records):
        raise ValueError('Literature retrieval cannot expose curated assessments to generation')
    records = bounded_context(records, maximum)
    persist_records(trusted['state_dir'], records)
    _save_query_cache(path, profile, records)
    return _cached_result(records, 'network', profile)


def _record_from_hit(hit):
    if not isinstance(hit, dict):
        return None
    source, identifier = hit.get('source'), hit.get('id')
    if (not isinstance(source, str) or not re.fullmatch('[A-Z]{2,8}', source)
            or not isinstance(identifier, str) or not re.fullmatch('[A-Za-z0-9._-]{1,100}', identifier)):
        return None
    types = hit.get('pubTypeList', {})
    types = types.get('pubType', []) if isinstance(types, dict) else []
    if any('review' in str(value).lower() or 'editorial' in str(value).lower() for value in types):
        return None
    title = hit.get('title')
    if not isinstance(title, str) or not title.strip():
        return None
    doi = hit.get('doi')
    if not isinstance(doi, str) or not re.fullmatch(r'10\.\d{4,9}/[^\s]{1,280}', doi):
        doi = None
    abstract = hit.get('abstractText') or ''
    if not isinstance(abstract, str):
        abstract = ''
    excerpt = unescape(re.sub(r'<[^>]*>', ' ', abstract))
    excerpt = ' '.join(excerpt.split())[:12000]
    url = ('https://doi.org/' + quote(doi, safe='/') if doi else
           'https://europepmc.org/article/' + source + '/' + identifier)
    return _make_record(id='epmc:' + source + ':' + identifier, title=title[:2000],
        url=url, doi=doi, excerpt=excerpt, retrieved_at=_timestamp(),
        source_kind='publication_metadata', retrieval_scope='retrieved_context',
        matching_reactions=[], reviewer='', locator='')


async def search_literature(query, limit=5, timeout_seconds=10, *, include_fulltext=False,
                            context_chars=6000, fulltext_timeout_seconds=8):
    """Search publication metadata; errors remain visible, empty hits are valid.

    Query syntax belongs to Europe PMC. Molecular/user text is URL-encoded data,
    never a command or a URL. No article links from responses are fetched.
    """
    _text(query, 'Literature query', 1000)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Literature result limit must be 1 to 20')
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30):
        raise ValueError('Literature timeout must be positive and at most 30 seconds')
    if type(include_fulltext) is not bool:
        raise ValueError('Full-text selection must be boolean')
    if type(fulltext_timeout_seconds) not in (int, float) or not 0 < fulltext_timeout_seconds <= 15:
        raise ValueError('Full-text timeout must be positive and at most 15 seconds')
    bounded_context([], context_chars)
    deadline = time.monotonic() + timeout_seconds
    url = 'https://' + _HOST + _PATH + '?' + urlencode({
        'query': query, 'format': 'json', 'resultType': 'core', 'pageSize': limit})
    payload = await asyncio.to_thread(_fetch_json, url, timeout_seconds)
    container = payload.get('resultList') if isinstance(payload, dict) else None
    hits = container.get('result') if isinstance(container, dict) else None
    if not isinstance(hits, list):
        raise ValueError('Malformed literature search response')
    records, seen = [], set()
    for hit in hits[:limit]:
        record = _record_from_hit(hit)
        if record is not None and record['id'] not in seen:
            pmcid = hit.get('pmcid')
            if include_fulltext and isinstance(pmcid, str) and re.fullmatch(r'PMC[0-9]{1,12}', pmcid):
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= .1:
                        raise TimeoutError('No full-text time remains')
                    raw = await asyncio.to_thread(_fetch_fulltext, pmcid, min(remaining, fulltext_timeout_seconds))
                    excerpt = _fulltext_excerpt(raw, query + ' ' + record['title'], context_chars)
                    excerpt = 'Source: https://europepmc.org/articles/' + pmcid + '\n' + excerpt
                except (OSError, ValueError) as error:
                    excerpt = 'Abstract only; full-text retrieval unavailable (' + type(error).__name__ + ').\n' + record['excerpt']
                record = {**record, 'excerpt': excerpt[:12000]}
                record = {**record, 'sha256': _hash(record)}
            records.append(record)
            seen.add(record['id'])
    return bounded_context(records, context_chars)


def assess_citations(route, records):
    """Bind IDs to backend evidence and count exact reaction-level coverage."""
    evidence = {record['id']: record for record in validate_evidence(records)}
    identifiers = route.get('evidence_ids') if isinstance(route, dict) else None
    reactions = route.get('reactions') if isinstance(route, dict) else None
    if (not isinstance(identifiers, list) or len(identifiers) > 100
            or any(not isinstance(value, str) for value in identifiers)
            or len(set(identifiers)) != len(identifiers) or not isinstance(reactions, list)):
        raise ValueError('Malformed route citations')
    if any(identifier not in evidence for identifier in identifiers):
        raise ValueError('Route cited an unknown evidence identifier')
    cited = [evidence[identifier] for identifier in identifiers]
    exact = {_signature(reaction) for record in cited
             if record['retrieval_scope'] == 'curated_exact_reactions'
             for reaction in record['matching_reactions']}
    supported = [index for index, reaction in enumerate(reactions) if _signature(reaction) in exact]
    status = ('no_citations' if not cited else 'exact_precedent' if reactions and len(supported) == len(reactions)
              else 'partial_exact_precedent' if supported else 'retrieved_context')
    return {'status': status, 'cited_evidence_ids': list(identifiers),
            'exact_supported_reactions': len(supported), 'total_reactions': len(reactions),
            'unsupported_reactions': [index for index in range(len(reactions)) if index not in supported],
            'chemically_validated': False,
            'limitation': 'Source retrieval and exact precedent do not establish experimental route feasibility.'}
