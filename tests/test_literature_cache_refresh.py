"""Exact DOI query caches must observe newly reviewed backend source context."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from retro_specialists.literature import (
    _cache_key, _make_record, _save_query_cache, cached_literature_search,
    curated_evidence,
)


class CacheRefreshTests(unittest.TestCase):
    def context(self, identifier='old', *, doi='10.1000/exact', hours=2, excerpt='Old source context'):
        return _make_record(id=identifier, title='Experimental synthesis',
            doi=doi, url='https://doi.org/' + doi, excerpt=excerpt,
            retrieved_at=(datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),
            source_kind='publication_metadata', retrieval_scope='retrieved_context',
            matching_reactions=[], reviewer='', locator='')

    def search(self, state, sources, query='DOI:10.1000/exact', limit=5, **settings):
        return cached_literature_search({'query': query, 'max_results': limit},
            {'state_dir': state, **settings}, lambda _: sources, lambda *_: None)

    def seed(self, state, records, query='DOI:10.1000/exact', limit=5):
        key, profile = _cache_key(query, limit, True, 6000)
        _save_query_cache(Path(state) / 'literature-cache' / (key + '.json'), profile, records)

    def test_new_backend_context_refreshes_fresh_query_cache_without_network(self):
        old = self.context()
        new = self.context('expanded', hours=1, excerpt='New four-step experimental sequence')
        with tempfile.TemporaryDirectory() as state:
            self.seed(state, [old])
            with patch('retro_specialists.literature.search_literature', AsyncMock()) as fetch:
                result = self.search(state, [old, new])
            fetch.assert_not_called()
        self.assertEqual([r['id'] for r in result['records']], ['expanded', 'old'])
        self.assertEqual(result['records'][1]['retrieved_at'], old['retrieved_at'])
        self.assertEqual(result['records'][0]['retrieved_at'], new['retrieved_at'])
        self.assertEqual(result['retrieval']['cache_match_basis'], 'exact_doi_reference')
        self.assertEqual(result['chemistry_status'], 'source_text_only')

    def test_new_same_id_context_replaces_old_and_result_limit_applies(self):
        old = self.context()
        new = self.context(hours=1, excerpt='Expanded experimental sequence')
        with tempfile.TemporaryDirectory() as state:
            self.seed(state, [old], limit=1)
            result = self.search(state, [new], limit=1)
        self.assertEqual(result['records'], [new])

    def test_curated_unrelated_future_and_expired_sources_cannot_refresh(self):
        old = self.context()
        curated = curated_evidence(identifier='curated', title='Reviewed experiment',
            url='https://doi.org/10.1000/exact', doi='10.1000/exact', excerpt='Assertion',
            reactions=[{'product': 'CCO', 'reactants': ['CC=O']}],
            reviewer='reviewer', locator='Scheme 1')
        sources = [curated, self.context('unrelated', doi='10.1000/other'),
                   self.context('future', hours=-1), self.context('expired', hours=200)]
        with tempfile.TemporaryDirectory() as state:
            self.seed(state, [old])
            result = self.search(state, sources)
        self.assertEqual(result['records'], [old])
        self.assertEqual(result['retrieval']['status'], 'query_cache_hit')

    def test_non_doi_query_keeps_query_cache_without_loading_backend(self):
        old = self.context()
        with tempfile.TemporaryDirectory() as state:
            self.seed(state, [old], query='experimental synthesis')
            result = cached_literature_search({'query': 'experimental synthesis'},
                {'state_dir': state}, lambda _: self.fail('unexpected backend load'), lambda *_: None)
        self.assertEqual(result['records'], [old])

    def test_expired_source_stays_network_failure_fallback(self):
        expired = self.context(hours=48)
        with tempfile.TemporaryDirectory() as state:
            with patch('retro_specialists.literature.search_literature', AsyncMock(side_effect=TimeoutError())):
                result = self.search(state, [expired])
        self.assertEqual(result['retrieval']['status'], 'cache_fallback_after_failure')
        self.assertEqual(result['records'], [expired])

    def test_invalid_backend_context_hash_rejected(self):
        old = self.context()
        with tempfile.TemporaryDirectory() as state:
            self.seed(state, [old])
            with self.assertRaises(ValueError):
                self.search(state, [{**old, 'excerpt': 'tampered'}])

    def test_refresh_preserves_context_budget_and_valid_integrity(self):
        old = self.context(excerpt='A' * 5000)
        new = self.context('expanded', hours=1, excerpt='B' * 5000)
        with tempfile.TemporaryDirectory() as state:
            self.seed(state, [old])
            first = self.search(state, [old, new])
            second = self.search(state, [old, new])
        self.assertLessEqual(sum(len(row['excerpt']) for row in first['records']), 6000)
        self.assertEqual(first['records'], second['records'])
        self.assertEqual(second['retrieval']['status'], 'query_cache_hit')

    def test_query_keywords_rank_relevant_context_before_newer_text(self):
        relevant = self.context('relevant', excerpt='Protecting-group acetylation experiment')
        newer = self.context('newer', hours=1)
        with tempfile.TemporaryDirectory() as state:
            result = self.search(state, [newer, relevant],
                                 query='DOI:10.1000/exact acetylation', limit=1)
        self.assertEqual(result['records'], [relevant])

    def test_same_identifier_cannot_change_source_identity_during_refresh(self):
        old = self.context()
        new = self.context(hours=1)
        changed = {**new, 'url': 'https://example.org/different-source'}
        changed = _make_record(**{key: value for key, value in changed.items() if key != 'sha256'})
        with tempfile.TemporaryDirectory() as state:
            self.seed(state, [old])
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.search(state, [changed])
