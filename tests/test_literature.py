import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from retro_specialists import literature as lit


def response(**overrides):
    row = {'id': '123', 'source': 'MED', 'title': 'A synthesis study',
           'doi': '10.1000/example', 'abstractText': '<p>Reported synthesis.</p>',
           'pubTypeList': {'pubType': ['Journal Article']}}
    return {'resultList': {'result': [{**row, **overrides}]}}


def curated(product='CCO', reactants=('CC=O',)):
    return lit.curated_evidence(
        identifier='curated:one', title='Checked primary study',
        url='https://doi.org/10.1000/example', doi='10.1000/example',
        excerpt='Explicit substrate evidence in the reviewed supporting information.',
        reactions=({'product': product, 'reactants': list(reactants)},),
        reviewer='test backend curator', locator='Supporting information, page 4',
    )


class LiteratureTests(unittest.IsolatedAsyncioTestCase):
    async def test_fulltext_adds_real_method_context_without_exact_proof(self):
        xml = b'<article><body><sec><title>Methods</title><p>Vanillyl alcohol was combined with hydrogen peroxide for oxidation to vanillin.</p></sec></body></article>'
        with patch.object(lit, '_fetch_json', return_value=response(pmcid='PMC123')), \
                patch.object(lit, '_fetch_fulltext', return_value=xml) as fulltext:
            rows = await lit.search_literature('vanillyl alcohol oxidation', include_fulltext=True)
        self.assertIn('hydrogen peroxide', rows[0]['excerpt'])
        self.assertEqual(fulltext.call_args.args[0], 'PMC123')
        self.assertEqual(rows[0]['retrieval_scope'], 'retrieved_context')
        self.assertEqual(rows[0]['matching_reactions'], [])

    async def test_failed_fulltext_keeps_explicit_abstract_fallback(self):
        with patch.object(lit, '_fetch_json', return_value=response(pmcid='PMC123')), \
                patch.object(lit, '_fetch_fulltext', side_effect=TimeoutError('slow')):
            rows = await lit.search_literature('oxidation', include_fulltext=True)
        self.assertIn('Abstract only', rows[0]['excerpt'])
        self.assertIn('Reported synthesis.', rows[0]['excerpt'])

    async def test_invalid_pmcid_never_fetched(self):
        with patch.object(lit, '_fetch_json', return_value=response(pmcid='https://localhost/')), \
                patch.object(lit, '_fetch_fulltext') as fulltext:
            rows = await lit.search_literature('oxidation', include_fulltext=True)
        fulltext.assert_not_called()
        self.assertEqual(len(rows), 1)

    async def test_context_budget_is_total_and_hashes_remain_valid(self):
        rows = response(abstractText='word ' * 1000)
        rows['resultList']['result'].append({**rows['resultList']['result'][0], 'id': '124'})
        with patch.object(lit, '_fetch_json', return_value=rows):
            records = await lit.search_literature('oxidation', context_chars=1000)
        self.assertLessEqual(sum(len(row['excerpt']) for row in records), 1000)
        self.assertEqual(len(lit.validate_evidence(records)), 2)

    async def test_search_encodes_query_and_provides_hashed_context(self):
        with patch.object(lit, '_fetch_json', return_value=response()) as fetch:
            rows = await lit.search_literature('aspirin synthesis & OR x', limit=2)
        self.assertEqual(len(rows), 1)
        self.assertIn('query=aspirin+synthesis+%26+OR+x', fetch.call_args.args[0])
        self.assertEqual(rows[0]['retrieval_scope'], 'retrieved_context')
        self.assertEqual(rows[0]['excerpt'], 'Reported synthesis.')
        self.assertEqual(len(rows[0]['sha256']), 64)
        self.assertEqual(lit.validate_evidence(rows), tuple(rows))

    async def test_empty_is_valid(self):
        with patch.object(lit, '_fetch_json', return_value={'resultList': {'result': []}}):
            self.assertEqual(await lit.search_literature('aspirin synthesis'), [])

    async def test_reviews_are_filtered(self):
        with patch.object(lit, '_fetch_json', return_value=response(pubTypeList={'pubType': ['Review']})):
            self.assertEqual(await lit.search_literature('aspirin synthesis'), [])

    async def test_missing_abstract_remains_context(self):
        with patch.object(lit, '_fetch_json', return_value=response(abstractText=None)):
            rows = await lit.search_literature('aspirin synthesis')
        self.assertEqual(rows[0]['excerpt'], '')
        self.assertEqual(rows[0]['retrieval_scope'], 'retrieved_context')

    async def test_no_doi_uses_derived_europepmc_url(self):
        with patch.object(lit, '_fetch_json', return_value=response(doi=None)):
            rows = await lit.search_literature('aspirin synthesis')
        self.assertEqual(rows[0]['url'], 'https://europepmc.org/article/MED/123')

    async def test_invalid_parameters(self):
        for kwargs in ({'query': ''}, {'query': 'x' * 1001}, {'query': 'x', 'limit': 0},
                       {'query': 'x', 'limit': True}, {'query': 'x', 'limit': 21},
                       {'query': 'x', 'timeout_seconds': 0}, {'query': 'x', 'timeout_seconds': 31}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await lit.search_literature(**kwargs)

    async def test_invalid_response_shape_rejected(self):
        for payload in ({}, [], {'resultList': {'result': 'oops'}}):
            with patch.object(lit, '_fetch_json', return_value=payload):
                with self.assertRaises(ValueError):
                    await lit.search_literature('aspirin synthesis')

    async def test_timeout_propagates(self):
        with patch.object(lit, '_fetch_json', side_effect=TimeoutError('timed out')):
            with self.assertRaises(TimeoutError):
                await lit.search_literature('aspirin synthesis')

    async def test_invalid_source_identifiers_are_skipped(self):
        with patch.object(lit, '_fetch_json', return_value=response(id='../../secret', source='file:')):
            self.assertEqual(await lit.search_literature('aspirin synthesis'), [])

    async def test_retrieved_context_does_not_establish_exact_match(self):
        with patch.object(lit, '_fetch_json', return_value=response()):
            rows = await lit.search_literature('aspirin synthesis')
        route = {'evidence_ids': [rows[0]['id']], 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']}]}
        result = lit.assess_citations(route, rows)
        self.assertEqual(result['status'], 'retrieved_context')
        self.assertEqual(result['exact_supported_reactions'], 0)
        self.assertFalse(result['chemically_validated'])


class EvidenceTests(unittest.TestCase):
    def test_exact_match_preserves_multiplicity_and_stereo(self):
        record = curated('OCC', ('O=CC',))
        route = {'evidence_ids': [record['id']], 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']}]}
        result = lit.assess_citations(route, [record])
        self.assertEqual(result['status'], 'exact_precedent')
        self.assertEqual(result['exact_supported_reactions'], 1)
        self.assertFalse(result['chemically_validated'])
        multiple = {**route, 'reactions': [{'product': 'CCO', 'reactants': ['CC=O', 'CC=O']}]}
        self.assertEqual(lit.assess_citations(multiple, [record])['exact_supported_reactions'], 0)

    def test_partial_exact_coverage_reported(self):
        record = curated()
        route = {'evidence_ids': [record['id']], 'reactions': [
            {'product': 'CCO', 'reactants': ['CC=O']},
            {'product': 'CC=O', 'reactants': ['CC']}]}
        result = lit.assess_citations(route, [record])
        self.assertEqual(result['status'], 'partial_exact_precedent')
        self.assertEqual(result['unsupported_reactions'], [1])

    def test_stereo_is_not_erased(self):
        record = curated('C[C@H](O)CC', ('CC(=O)CC',))
        route = {'evidence_ids': [record['id']], 'reactions': [
            {'product': 'C[C@@H](O)CC', 'reactants': ['CC(=O)CC']}]}
        self.assertEqual(lit.assess_citations(route, [record])['exact_supported_reactions'], 0)

    def test_unknown_citation_rejected(self):
        with self.assertRaises(ValueError):
            lit.assess_citations({'evidence_ids': ['invented'], 'reactions': []}, [])

    def test_no_citations_is_explicit(self):
        result = lit.assess_citations({'evidence_ids': [], 'reactions': [{'product': 'CCO', 'reactants': ['CC=O']}]}, [])
        self.assertEqual(result['status'], 'no_citations')

    def test_changed_record_or_duplicate_id_rejected(self):
        record = curated()
        changed = {**record, 'excerpt': 'Replaced by untrusted text'}
        for rows in ([changed], [record, record]):
            with self.assertRaises(ValueError):
                lit.validate_evidence(rows)

    def test_exact_record_requires_review_location(self):
        with self.assertRaises(ValueError):
            lit.curated_evidence(identifier='curated:x', title='x', url='https://example.com/paper',
                doi=None, excerpt='x', reactions=({'product': 'CCO', 'reactants': ['CC=O']},),
                reviewer='', locator='')

    def test_invalid_structures_or_untrusted_url_rejected(self):
        for product in ('not a molecule', '', 'CCO ethanol'):
            with self.assertRaises(ValueError):
                curated(product)
        record = curated()
        with self.assertRaises(ValueError):
            lit.curated_evidence(identifier='curated:x', title='x', url='file:///etc/passwd',
                doi=None, excerpt='x', reactions=({'product': 'CCO', 'reactants': ['CC=O']},),
                reviewer='curator', locator='page 1')

    def test_input_is_not_mutated(self):
        record = curated()
        original = deepcopy(record)
        lit.validate_evidence([record])
        self.assertEqual(record, original)


class FetchBoundaryTests(unittest.TestCase):
    url = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=vanillin'

    def connection(self, *, status=200, headers=None, chunks=None):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = status
        response.getheader.side_effect = lambda key, default=None: (headers or {}).get(key, default)
        response.read1.side_effect = [b'{"resultList":{"result":[]}}', b''] if chunks is None else chunks
        return connection

    def test_fixed_host_only(self):
        for url in ('file:///etc/passwd', 'https://localhost/',
                    'https://www.ebi.ac.uk/other', 'https://www.ebi.ac.uk.evil.com/europepmc/webservices/rest/search'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                lit._fetch_json(url, 10)

    def test_success_and_cleanup(self):
        connection = self.connection()
        with patch.object(lit.http.client, 'HTTPSConnection', return_value=connection):
            self.assertEqual(lit._fetch_json(self.url, 10), {'resultList': {'result': []}})
        connection.close.assert_called_once()
        connection.sock.settimeout.assert_called()

    def test_redirect_and_failures_are_not_followed(self):
        for status in (301, 302, 403, 429, 500):
            connection = self.connection(status=status)
            with patch.object(lit.http.client, 'HTTPSConnection', return_value=connection):
                with self.assertRaises(ValueError):
                    lit._fetch_json(self.url, 10)
            connection.close.assert_called_once()

    def test_compressed_or_oversized_body_rejected(self):
        for headers in ({'Content-Encoding': 'gzip'}, {'Content-Length': '1048577'}):
            connection = self.connection(headers=headers)
            with patch.object(lit.http.client, 'HTTPSConnection', return_value=connection):
                with self.assertRaises(ValueError):
                    lit._fetch_json(self.url, 10)
        connection = self.connection(chunks=[b'x' * 1048577])
        with patch.object(lit.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaises(ValueError):
                lit._fetch_json(self.url, 10)

    def test_deadline_and_malformed_json(self):
        connection = self.connection()
        with patch.object(lit.http.client, 'HTTPSConnection', return_value=connection), \
                patch.object(lit.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaises(TimeoutError):
                lit._fetch_json(self.url, 10)


class FulltextTests(unittest.TestCase):
    def test_standard_external_jats_doctype_is_not_fetched(self):
        xml = b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS//EN" "https://invalid.example/jats.dtd"><article><body><sec><title>Methods</title><p>Oxidation reagent was peroxide.</p></sec></body></article>'
        excerpt = lit._fulltext_excerpt(xml, 'oxidation', 1000)
        self.assertIn('peroxide', excerpt)

    def test_methods_rank_above_unrelated_introduction(self):
        xml = b'<article><body><sec><title>Introduction</title><p>Vanillin is useful.</p></sec><sec><title>Experimental methods</title><p>Vanillyl alcohol and hydrogen peroxide gave vanillin.</p></sec></body></article>'
        excerpt = lit._fulltext_excerpt(xml, 'vanillyl alcohol oxidation', 300)
        self.assertIn('hydrogen peroxide', excerpt)
        self.assertIn('Experimental methods', excerpt)

    def test_entities_malformed_xml_and_size_rejected(self):
        for raw in (b'<!DOCTYPE article [<!ENTITY x SYSTEM "file:///etc/passwd">]><article>&x;</article>',
                    b'<article>', b'x' * 2_000_001):
            with self.assertRaises(ValueError):
                lit._fulltext_excerpt(raw, 'oxidation', 1000)

    def test_invalid_id_and_redirect_rejected(self):
        with self.assertRaises(ValueError):
            lit._fetch_fulltext('../secret', 5)
        connection = MagicMock()
        connection.getresponse.return_value.status = 302
        with patch.object(lit.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaises(ValueError):
                lit._fetch_fulltext('PMC123', 5)

    def test_fulltext_contains_no_methods_is_not_silent_support(self):
        with self.assertRaises(ValueError):
            lit._fulltext_excerpt(b'<article><front><article-title>A paper</article-title></front></article>', 'oxidation', 1000)


if __name__ == '__main__':
    unittest.main()
