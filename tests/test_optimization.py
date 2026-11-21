"""Policy fixtures test measured axes, never assert synthetic feasibility."""
from copy import deepcopy
import unittest

from retro_specialists.optimization import assess_pool, shortlist_tasks, select_results
from retro_specialists.graphs import graph_key
from retro_specialists.literature import curated_evidence
from tests.test_costs import quote, proxy


def route(reactants=('CC=O',), label='r', reactions=None, **extra):
    return {'route_id': label, 'reactions': reactions or [{'product':'CCO','reactants':list(reactants)}], **extra}


def evidence(*routes):
    return [curated_evidence(identifier='curated:fixture',title='Controlled fixture, not real chemistry',
        url='https://example.org/fixture',doi='10.1000/fixture',excerpt='Controlled testing data',
        reactions=[reaction for r in routes for reaction in r['reactions']],reviewer='fixture',locator='fixture')]


class OptimizationTests(unittest.TestCase):
    def test_assessment_matches_backend_corpus_not_claimed_route_citations(self):
        a=route(); original=deepcopy(a)
        x=assess_pool([a], 'CCO', evidence=evidence(a))['assessments'][0]
        self.assertEqual(x['evidence']['exact_supported_reactions'],1)
        self.assertFalse(x['evidence']['chemical_feasibility_established'])
        self.assertEqual(x['inventory']['status'],'unknown')
        self.assertIsNone(x['inventory']['connected'])
        self.assertEqual(a,original)

    def test_per_step_support_exposes_unsupported_step_and_element_warning(self):
        a=route(reactions=[{'product':'CCO','reactants':['CC=O']},{'product':'CC=O','reactants':['CC']}])
        x=assess_pool([a], 'CCO', evidence=evidence(route()))['assessments'][0]
        self.assertEqual(x['evidence']['unsupported_reactions'],[1])
        self.assertEqual(x['evidence']['reactions'][0]['status'],'exact_precedent')
        tasks=shortlist_tasks([x],limit=1)
        self.assertEqual(tasks['route_repair'][0]['reaction_index'],1)
        self.assertIn('missing_product_elements',tasks['route_repair'][0]['issue_codes'])

    def test_pool_rejects_invalid_and_deduplicates_without_losing_origins(self):
        a=route(origin='model');b=route(('O=CC',),origin='route_proposer')
        x=assess_pool([a,b,route(('CCO',))],'CCO')
        self.assertEqual(len(x['assessments']),1)
        self.assertEqual(set(x['assessments'][0]['origins']),{'model','route_proposer'})
        self.assertEqual(len(x['rejections']),1)

    def test_shortlist_bounded_diverse_and_cost_focus_is_known_priced_leaf(self):
        x=assess_pool([route(),route(('CC','O'),'other')],'CCO',quotes=[quote('CC=O','100'),quote('CC','2'),quote('O','1')],cost_assumptions=proxy())
        tasks=shortlist_tasks(x,limit=1)
        self.assertEqual(len(tasks['route_repair']),1)
        self.assertEqual(tasks['cost_reducer'][0]['focus_molecule'],'CC=O')
        self.assertEqual(tasks['cost_reducer'][0]['focus_cost'],'100')

    def test_unknown_cost_not_invented_zero_or_cheapness(self):
        x=assess_pool([route()],'CCO')
        self.assertEqual(x['assessments'][0]['cost']['status'],'unknown')
        tasks=shortlist_tasks(x)
        self.assertEqual(tasks['cost_reducer'],[])
        self.assertIn('No fully priced',tasks['limitations'][0])

    def test_explicit_parent_and_new_graph_required_for_acceptance(self):
        a=route(); b=route(('CC','O'),'new')
        x=select_results([a],[a],[b],'CCO',evidence=evidence(b))
        self.assertEqual(x['accepted_improvements'],[])
        self.assertEqual(x['revision_decisions'][0]['status'],'unlinked')
        b['parent_graph_key']=graph_key(a)
        x=select_results([a],[a],[b],'CCO',evidence=evidence(b))
        self.assertEqual(len(x['accepted_improvements']),1)
        self.assertEqual(x['accepted_improvements'][0]['improved_axes'],['exact_precedent_coverage'])
        self.assertFalse(x['chemical_superiority_established'])
        self.assertEqual(len(x['original_routes']),1)
        self.assertEqual(len(x['all_routes']),2)

    def test_unknown_inventory_is_not_completion_but_explicit_membership_is(self):
        a=route(); b=route(('CC','O'),'new',parent_graph_key=graph_key(a))
        x=select_results([a],[a],[b],'CCO')
        self.assertEqual(x['accepted_improvements'],[])
        x=select_results([a],[a],[b],'CCO',inventory=['CC','O'])
        self.assertIn('configured_inventory_completion',x['accepted_improvements'][0]['improved_axes'])

    def test_cost_reduction_uses_same_scope_and_rejects_evidence_regression(self):
        a=route(); b=route(('CC','O'),'new',parent_graph_key=graph_key(a))
        quotes=[quote('CC=O','100'),quote('CC','2'),quote('O','1')]
        x=select_results([a],[a],[b],'CCO',quotes=quotes,cost_assumptions=proxy())
        self.assertIn('precursor_cost_proxy',x['accepted_improvements'][0]['improved_axes'])
        self.assertFalse(x['total_synthesis_cost_reduction_established'])
        x=select_results([a],[a],[b],'CCO',quotes=quotes,cost_assumptions=proxy(),evidence=evidence(a))
        self.assertEqual(x['accepted_improvements'],[])
        self.assertIn('exact_precedent_regression',x['revision_decisions'][0]['tradeoffs'])

    def test_shorter_unsupported_route_not_automatically_an_improvement(self):
        a=route(reactions=[{'product':'CCO','reactants':['CC=O']},{'product':'CC=O','reactants':['CC','O']}])
        b=route(('CC','O'),'new',parent_graph_key=graph_key(a))
        x=select_results([a],[a],[b],'CCO')
        self.assertEqual(x['accepted_improvements'],[])

    def test_duplicate_revision_and_unknown_parent_remain_traceable(self):
        a=route(); duplicate={**a,'parent_graph_key':graph_key(a)}
        bad=route(('CC','O'),'new',parent_graph_key='f'*64)
        x=select_results([a],[a],[duplicate,bad],'CCO')
        self.assertEqual([r['status'] for r in x['revision_decisions']],['unchanged','unknown_parent'])
        self.assertEqual(len(x['selected_routes']),1)

    def test_supported_route_is_preferred_and_selection_keeps_original_pool(self):
        a=route(); b=route(('CC','O'),'new')
        x=select_results([a],[a,b],[],'CCO',evidence=evidence(b),limit=1)
        self.assertEqual(x['selected_routes'][0]['graph_key'],graph_key(b))
        self.assertEqual(len(x['all_routes']),2)
        self.assertEqual(x['accepted_improvements'],[])
        self.assertEqual(x['selection_policy'],'separate_axes_lexicographic_shortlist_not_feasibility_score')

    def test_inventory_generator_is_frozen_and_invalid_limits_rejected(self):
        a=route()
        x=assess_pool([a,route(('CC','O'),'second')],'CCO',inventory=iter(['CC=O','CC','O']))
        self.assertTrue(all(r['inventory']['connected'] for r in x['assessments']))
        for value in (0,True,101):
            with self.assertRaises(ValueError):shortlist_tasks(x,value)

class OptimizationHardeningTests(unittest.TestCase):
    def test_invalid_revision_metadata_does_not_discard_valid_original(self):
        a=route(); bad=route(('CCO',),'bad',parent_graph_key=graph_key(a))
        x=select_results([a],[a],[bad],'CCO')
        self.assertEqual(len(x['all_routes']),1)
        self.assertEqual(len(x['rejections']['revised']),1)

    def test_cheaper_than_parent_is_not_claimed_cheaper_than_entire_baseline(self):
        a=route(('CC=O',),'expensive');other=route(('C=C','O'),'cheaper')
        b=route(('CC','O'),'new',parent_graph_key=graph_key(a))
        quotes=[quote('CC=O','100'),quote('C=C','1'),quote('CC','2'),quote('O','1')]
        x=select_results([a,other],[a,other],[b],'CCO',quotes=quotes,cost_assumptions=proxy())
        improvement=x['accepted_improvements'][0]
        self.assertEqual(improvement['comparison_scope'],'explicit_parent_route_only')
        self.assertFalse(improvement['lower_than_best_fully_priced_baseline'])

    def test_unknown_baseline_routes_cannot_establish_best_overall_cost(self):
        a=route(('CC=O',),'expensive');unknown=route(('COC',),'unknown')
        b=route(('CC','O'),'new',parent_graph_key=graph_key(a))
        quotes=[quote('CC=O','100'),quote('CC','2'),quote('O','1')]
        x=select_results([a,unknown],[a,unknown],[b],'CCO',quotes=quotes,cost_assumptions=proxy())
        improvement=x['accepted_improvements'][0]
        self.assertTrue(improvement['lower_than_best_fully_priced_baseline'])
        self.assertEqual(improvement['unknown_baseline_cost_count'],1)
        self.assertFalse(improvement['lowest_cost_overall_established'])


class SelectionEligibilityTests(unittest.TestCase):
    def test_unlinked_revision_remains_exploratory_and_cannot_displace_original(self):
        a=route(); b=route(('CC','O'),'unlinked')
        x=select_results([a],[a],[b],'CCO',evidence=evidence(b),limit=1)
        self.assertEqual(len(x['all_routes']),2)
        self.assertEqual(x['revision_decisions'][0]['status'],'unlinked')
        self.assertEqual(x['selected_routes'][0]['graph_key'],graph_key(a))
        self.assertEqual(len(x['exploratory_revision_routes']),1)

    def test_higher_cost_shorter_revision_cannot_displace_known_parent(self):
        a=route(reactions=[{'product':'CCO','reactants':['CC=O']},
                          {'product':'CC=O','reactants':['CC','O']}])
        b=route(('C=C','O'),'shorter',parent_graph_key=graph_key(a))
        quotes=[quote('CC','1'),quote('O','1'),quote('C=C','100')]
        x=select_results([a],[a],[b],'CCO',quotes=quotes,cost_assumptions=proxy(),limit=1)
        self.assertIn('precursor_cost_increase',x['revision_decisions'][0]['tradeoffs'])
        self.assertEqual(x['accepted_improvements'],[])
        self.assertEqual(x['selected_routes'][0]['graph_key'],graph_key(a))
        self.assertEqual(len(x['all_routes']),2)

    def test_rejected_revision_already_in_combined_pool_retains_independent_eligibility(self):
        a=route(); b=route(('CC','O'),'independent')
        revision={**b,'parent_graph_key':'f'*64}
        x=select_results([a],[a,b],[revision],'CCO',evidence=evidence(b),limit=1)
        self.assertEqual(x['revision_decisions'][0]['status'],'unknown_parent')
        self.assertEqual(x['selected_routes'][0]['graph_key'],graph_key(b))
        self.assertEqual(x['exploratory_revision_routes'],[])

    def test_accepted_revision_becomes_final_eligible(self):
        a=route(); b=route(('CC','O'),'repair',parent_graph_key=graph_key(a))
        x=select_results([a],[a],[b],'CCO',evidence=evidence(b),limit=1)
        self.assertEqual(x['selected_routes'][0]['graph_key'],graph_key(b))
        self.assertEqual(len(x['eligible_routes']),2)
        self.assertEqual(x['exploratory_revision_routes'],[])

class ExistingAlternativeTests(unittest.TestCase):
    def test_existing_combined_alternative_is_reused_not_new_revision_gain(self):
        parent=route(); existing=route(('CC','O'),'proposer')
        revision={**existing,'parent_graph_key':graph_key(parent)}
        x=select_results([parent],[parent,existing],[revision],'CCO',evidence=evidence(existing),limit=1)
        self.assertEqual(x['accepted_improvements'],[])
        self.assertEqual(len(x['existing_alternatives']),1)
        decision=x['existing_alternatives'][0]
        self.assertEqual(decision['status'],'reused_existing_alternative')
        self.assertEqual(decision['improved_axes'],['exact_precedent_coverage'])
        self.assertEqual(decision['parent_graph_key'],graph_key(parent))
        self.assertFalse(decision['new_graph_contribution'])
        self.assertEqual(x['selected_routes'][0]['graph_key'],graph_key(existing))
        self.assertEqual(len(x['all_routes']),2)
        self.assertEqual(x['exploratory_revision_routes'],[])

    def test_novel_accepted_revision_is_new_graph_contribution(self):
        parent=route(); revision=route(('CC','O'),'repair',parent_graph_key=graph_key(parent))
        x=select_results([parent],[parent],[revision],'CCO',evidence=evidence(revision))
        self.assertEqual(len(x['accepted_improvements']),1)
        self.assertTrue(x['accepted_improvements'][0]['new_graph_contribution'])
        self.assertEqual(x['existing_alternatives'],[])
