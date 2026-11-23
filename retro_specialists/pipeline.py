"""Staged parallel retrosynthesis, targeted revision and evidence-bound selection.

Public model outputs are never rewritten. This controller returns an internal
assessment artifact; graph validity does not establish successful chemistry.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
import math
from pathlib import Path
import time
from .contracts import positive_limit, response_schema
from .graphs import canonical_smiles, route_graph, validate_response
from .literature import validate_evidence
from .prompts import PROMPT_VERSION, instructions


@dataclass(frozen=True)
class PipelineConfig:
    requested_count: int = 2
    max_depth: int = 5
    max_total_reactions: int = 20
    proposal_timeout_seconds: float | None = None
    optimization_timeout_seconds: float | None = None
    search_timeout_seconds: float | None = None
    shortlist_limit: int = 3
    selection_limit: int = 3
    max_intermediate_expansions: int = 0
    until_search_finishes: bool = False

    def __post_init__(self):
        if type(self.until_search_finishes) is not bool:
            raise ValueError('until_search_finishes must be a boolean')
        for name, maximum in [('requested_count',20),('max_depth',30),('max_total_reactions',100),('shortlist_limit',10),('selection_limit',20)]:
            positive_limit(getattr(self,name),name,maximum)
        if type(self.max_intermediate_expansions) is not int or not 0<=self.max_intermediate_expansions<=3:
            raise ValueError('max_intermediate_expansions must be0..3')
        for name in ('proposal_timeout_seconds','optimization_timeout_seconds','search_timeout_seconds'):
            value=getattr(self,name)
            if value is None:
                continue
            if type(value) not in (float,int) or not math.isfinite(value) or not 0<value<=7200:
                raise ValueError(name+' must be finite, positive and at most7200')


def _save(path,value):
    raw=json.dumps(value,sort_keys=True,indent=2,allow_nan=False).encode()
    if len(raw)>32_000_000:raise ValueError('Pipeline artifact exceeds byte bound')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_bytes(raw);path.chmod(0o600)


def _merge(routes):
    merged={}
    for route in routes:
        key=route['graph_key']
        if key not in merged:merged[key]=deepcopy(route)
    return list(merged.values())


def _round_delay(status, added):
    return 1.0 if added else (5.0 if status == 'completed' else 15.0)


async def _collect_rounds(model_task, run_round, folder):
    """Collect only proposals verified before the model branch ended."""
    started = time.monotonic()
    rounds, accepted = [], {}
    completed = proposed = duplicates = 0
    active = None
    try:
        while not model_task.done():
            number = len(rounds) + 1
            previous = [route['reactions'] for route in list(accepted.values())[-12:]]
            active = asyncio.create_task(run_round('route_proposer', number, previous))
            await asyncio.wait((model_task, active), return_when=asyncio.FIRST_COMPLETED)
            cutoff = model_task.result()['ended_monotonic'] if model_task.done() else math.inf
            if not active.done() or active.result()['ended_monotonic'] > cutoff:
                rounds.append({'round': number, 'status': 'stopped_at_search_end'})
                break
            batch = active.result()
            active = None
            added = 0
            if batch['status'] == 'completed':
                completed += 1
                proposed += batch['counts']['proposed']
                for route in batch['routes']:
                    if route['graph_key'] in accepted:
                        duplicates += 1
                    else:
                        accepted[route['graph_key']] = route
                        added += 1
            rounds.append({'round': number, 'status': batch['status'], 'new_routes': added})
            if not model_task.done():
                await asyncio.wait((model_task,), timeout=_round_delay(batch['status'], added))
    finally:
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
    failed = any(row['status'] in ('failed', 'timed_out') for row in rounds)
    result = {'status': 'completed' if completed else ('failed' if failed else 'cancelled'),
              'routes': list(accepted.values()), 'rounds': rounds, 'stop_reason': 'model_branch_finished',
              'counts': {'proposed': proposed, 'retained': len(accepted), 'duplicate_verified': duplicates,
                         'rejected': proposed-len(accepted)},
              'started_monotonic': started, 'ended_monotonic': time.monotonic()}
    _save(folder/'route_proposer'/'result.json', result)
    return result


async def run_pipeline(target,name,search,specialist,receipt_dir,*,config=PipelineConfig(),
                       inventory=None,evidence=(),quotes=(),cost_assumptions=None,evidence_loader=None,intermediate_search=None):
    """Search and specialist are cancellable async callables supplied by host code.

    search()->route list or {'routes': list,...}. specialist(role,request,folder)
    ->{'status':'completed','raw_response': strict proposal JSON,...}. Deployment
    adapters own subprocess termination and must respond to asyncio cancellation.
    """
    from .optimization import assess_pool,select_results
    if not isinstance(config,PipelineConfig):raise ValueError('PipelineConfig required')
    if not callable(search) or not callable(specialist):raise ValueError('Async search and specialist callables required')
    if evidence_loader is not None and not callable(evidence_loader):raise ValueError('Evidence loader must be callable')
    if intermediate_search is not None and not callable(intermediate_search):raise ValueError('Intermediate search must be callable')
    if isinstance(inventory,(str,bytes)):raise ValueError('Inventory must be a molecular sequence, not text')
    target=canonical_smiles(target)
    if not isinstance(name,str) or not name.strip() or len(name)>300:raise ValueError('Bounded target name required')
    folder=Path(receipt_dir);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    stock=None if inventory is None else tuple(canonical_smiles(s) for s in inventory)
    evidence=validate_evidence(tuple(evidence));quotes=tuple(deepcopy(tuple(quotes)))
    def current_evidence():
        if evidence_loader is None:return evidence
        loaded=validate_evidence(tuple(evidence_loader()))
        merged={r['id']:r for r in evidence}
        for record in loaded:
            previous=merged.get(record['id'])
            if previous:
                if any(previous[field]!=record[field] for field in ('doi','url')):
                    raise ValueError('Conflicting evidence source identity')
                if previous['retrieval_scope']=='curated_exact_reactions':
                    if record['retrieval_scope']=='retrieved_context':
                        continue  # Source excerpts cannot downgrade backend reaction attestations.
                    assertions=lambda value:{k:v for k,v in value.items() if k not in ('sha256','retrieved_at')}
                    if assertions(previous)!=assertions(record):
                        raise ValueError('Conflicting curated evidence record')
                elif record['retrieval_scope']=='retrieved_context' and previous['retrieved_at']>=record['retrieved_at']:
                    continue
            merged[record['id']]=record
        return validate_evidence(tuple(merged.values()))
    assumptions=deepcopy(cost_assumptions or {});started=time.monotonic()
    context=[{k:r[k] for k in ('id','title','url','doi','excerpt','retrieved_at','source_kind')} for r in evidence]
    _save(folder/'configuration.json',{'target':target,'name':name,'configuration':asdict(config),'prompt_version':PROMPT_VERSION})
    async def model():
        begin=time.monotonic()
        try:
            async with asyncio.timeout(config.search_timeout_seconds):value=await search()
            if isinstance(value,dict):
                metadata={key:deepcopy(item) for key,item in value.items() if key not in ('public_output','routes')}
                if 'public_output' in value:
                    public_path=folder/'model-public-output.json'
                    # Compact encoding keeps a valid 20MB service artifact within the sidecar bound.
                    raw=json.dumps(value['public_output'],ensure_ascii=True,allow_nan=False,separators=(',',':')).encode()
                    if len(raw)>32_000_000:raise ValueError('Public model output exceeds sidecar byte bound')
                    public_path.write_bytes(raw);public_path.chmod(0o600)
                    metadata['public_output_artifact']={'path':public_path.name,'sha256':sha256(raw).hexdigest(),'bytes':len(raw),'meaning':'Unchanged public JSON values; serialized once in a private sidecar.'}
                metadata['source_route_count']=len(value.get('routes',[])) if isinstance(value.get('routes'),list) else None
            else:
                metadata={'source_route_count':len(value) if isinstance(value,list) else None}
            if isinstance(value,dict) and value.get('status','completed')!='completed':
                result={'status':value.get('status') if value.get('status') in ('failed','timed_out','cancelled') else 'failed','routes':[],'original_response':metadata}
                result={**result,'started_monotonic':begin,'ended_monotonic':time.monotonic()}
                _save(folder/'model-result.json',result);return result
            values=value.get('routes') if isinstance(value,dict) else value
            if not isinstance(values,list) or len(values)>10000:raise ValueError('Bounded model route list required')
            routes=[];rejections=[]
            for index,row in enumerate(values):
                try:routes.append({**deepcopy(row),**route_graph(row,target,config.max_depth,config.max_total_reactions),'origin':'model'})
                except (ValueError,TypeError,KeyError) as error:rejections.append({'index':index,'reason':str(error)[:500]})
            result={'status':'completed','routes':_merge(routes),'rejections':rejections,'original_response':metadata}
        except asyncio.CancelledError:raise
        except Exception as error:result={'status':'timed_out' if isinstance(error,TimeoutError) else 'failed','routes':[],'error_type':type(error).__name__}
        result={**result,'started_monotonic':begin,'ended_monotonic':time.monotonic()}
        _save(folder/'model-result.json',result);return result
    async def agent(role, round_number=None, previous=()):
        if role != 'route_proposer':
            raise ValueError('Only the independent proposer is active')
        from .stereochemistry import target_stereo_requirements
        begin=time.monotonic();part=folder/role
        if round_number is not None:
            part=part/f'round-{round_number:04d}'
        request={'role':role,'identity':{'name':name,'smiles':target},'requested_route_count':config.requested_count,
            'project_specification':{'max_depth':config.max_depth,'max_total_reactions':config.max_total_reactions},
            'stereochemical_requirements':target_stereo_requirements(target),
            'literature_context':deepcopy(context),'starting_material_library':({'status':'unknown','meaning':'Inventory not supplied; membership and availability unknown.'} if stock is None else {'status':'configured_membership_set','molecules':list(stock),'meaning':'Configured membership set; catalog membership does not establish chemical feasibility or supplier availability.'})}
        request['model_branch']='Independent; model-search results unavailable'
        if round_number is not None:
            request.update(round_number=round_number, previous_verified_reactions=list(previous))
        _save(part/'request.json',request)
        schema=response_schema(config.requested_count,config.max_total_reactions,require_stereo_plan=True,require_origin_declarations=True)
        _save(part/'schema.json',schema);_save(part/'instructions.json',{'version':PROMPT_VERSION,'text':instructions(role)})
        response=None
        try:
            budget=config.proposal_timeout_seconds if role=='route_proposer' else config.optimization_timeout_seconds
            async with asyncio.timeout(budget):
                response=await specialist(role,deepcopy(request),part)
                if isinstance(response,dict) and response.get('status')=='timed_out':
                    raise TimeoutError('Native proposer deadline exceeded')
                if not isinstance(response,dict) or response.get('status')!='completed':raise ValueError('Specialist did not complete')
                raw=response['raw_response']
                _save(part/'provider-response.json',response)
                checked=validate_response(raw,target,requested_count=config.requested_count,max_depth=config.max_depth,max_total_reactions=config.max_total_reactions,inventory=stock or (),require_stereo_plan=True,require_origin_declarations=True)
                known={r['id'] for r in current_evidence()};accepted=[];rejected=[]
                for route in checked['routes']:
                    if set(route.get('evidence_ids',()))-known:rejected.append({'route_id':route['route_id'],'reason':'Unknown evidence IDs'})
                    else:
                        from .stereochemistry import assess_stereo_evidence
                        stereo=assess_stereo_evidence(route,current_evidence())
                        if stereo['issues']:
                            rejected.append({'route_id':route['route_id'],'reason':'Unbound stereochemical source claims','issues':stereo['issues']})
                        else:accepted.append({**route,'origin':role,'stereochemical_assessment':stereo})
                result={'status':'completed','routes':accepted,'validation':checked,'counts':{**checked['counts'],'retained':len(accepted),'rejected':checked['counts']['proposed']-len(accepted)},'evidence_rejections':rejected}
        except asyncio.CancelledError:raise
        except Exception as error:result={'status':'timed_out' if isinstance(error,TimeoutError) else 'failed','routes':[],'error_type':type(error).__name__}
        result={**result,'started_monotonic':begin,'ended_monotonic':time.monotonic(),'request_sha256':sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()}
        _save(part/'result.json',result);return result
    model_task=asyncio.create_task(model())
    proposal_task=asyncio.create_task(_collect_rounds(model_task,agent,folder) if config.until_search_finishes else agent('route_proposer'))
    tasks=[model_task,proposal_task]
    try:
        model_result,proposal=await asyncio.gather(*tasks)
        baseline=model_result['routes'];combined=_merge([*baseline,*proposal['routes']])
        assessment=assess_pool(combined,target,inventory=stock,evidence=current_evidence(),quotes=quotes,cost_assumptions=assumptions)
        focused={'route_repair': [], 'cost_reducer': [],
                 'limitations': ['Repair and cost-reducer agents are retired; no optimization agents are dispatched.']}
        _save(folder/'combined-assessment.json',assessment);_save(folder/'focused-tasks.json',focused)
        revision_start=time.monotonic()
        now=time.monotonic()
        repair={'status':'retired','routes':[],'started_monotonic':now,'ended_monotonic':now}
        cost=dict(repair)
        for role, branch in (('route_repair',repair),('cost_reducer',cost)):
            _save(folder/role/'result.json',branch)
        revisions=[]
        associations=[]
        scoped={}
        for association in associations:
            scoped.setdefault(association['graph_key'],set()).add(association['parent_graph_key'])
        parents={key:next(iter(values)) for key,values in scoped.items() if len(values)==1}
        ambiguous=sorted(key for key,values in scoped.items() if len(values)>1)
        expansion_records=[]
        expansion_status='disabled' if not config.max_intermediate_expansions else 'skipped_unknown_or_empty_inventory' if not stock else 'skipped_no_callback' if intermediate_search is None else 'completed'
        if config.max_intermediate_expansions and stock and intermediate_search is not None:
            from .grafting import graft_route
            pending=[(r,leaf) for r in combined for leaf in r['leaves'] if leaf not in stock]
            seen=set()
            for parent,leaf in pending:
                if leaf in seen:continue
                if len(seen)>=config.max_intermediate_expansions:break
                seen.add(leaf);begin=time.monotonic()
                try:
                    async with asyncio.timeout(config.search_timeout_seconds):value=await intermediate_search(leaf)
                    if isinstance(value,dict) and value.get('status','completed')!='completed':
                        raise ValueError('Intermediate model search did not complete')
                    expansions=value.get('routes') if isinstance(value,dict) else value
                    if not isinstance(expansions,list) or len(expansions)>10000:raise ValueError('Bounded expansion route list required')
                    grafted=[];rejected=[]
                    for expansion in expansions:
                        try:
                            graph=graft_route(parent,{**expansion,'target':leaf},target,config.max_depth,config.max_total_reactions)
                            grafted.append(graph)
                            associations.append({'role':'intermediate_replanning','graph_key':graph['graph_key'],'parent_graph_key':parent['graph_key']})
                            scoped.setdefault(graph['graph_key'],set()).add(parent['graph_key'])
                            if len(scoped[graph['graph_key']])==1:parents[graph['graph_key']]=parent['graph_key']
                            else:
                                parents.pop(graph['graph_key'],None)
                                ambiguous=sorted(set(ambiguous)|{graph['graph_key']})
                        except (ValueError,TypeError,KeyError) as error:rejected.append(str(error)[:500])
                    revisions=_merge([*revisions,*grafted])
                    record={'status':'completed','leaf':leaf,'parent_graph_key':parent['graph_key'],'grafted_routes':grafted,'rejections':rejected,'original_response':value}
                except asyncio.CancelledError:raise
                except Exception as error:
                    record={'status':'timed_out' if isinstance(error,TimeoutError) else 'failed','leaf':leaf,'parent_graph_key':parent['graph_key'],'error_type':type(error).__name__}
                expansion_records.append({**record,'started_monotonic':begin,'ended_monotonic':time.monotonic()})
        intermediate={'status':expansion_status,'attempts':expansion_records}
        _save(folder/'intermediate-replanning.json',intermediate)
        # Explicit None also blocks a graft's declared-parent fallback when linkage is ambiguous.
        parents={**parents,**{key:None for key in ambiguous}}
        final=select_results(baseline,combined,revisions,target,inventory=stock,evidence=current_evidence(),quotes=quotes,cost_assumptions=assumptions,limit=config.selection_limit,revision_parents=parents)
    except BaseException:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        raise
    ended=time.monotonic();original={r['graph_key'] for r in baseline}
    output={'target':target,'configuration':asdict(config),'model_branch':model_result,'proposer':proposal,
        'active_specialists':['route_proposer'],
        'optimization_branches':{'route_repair':repair,'cost_reducer':cost},'intermediate_replanning':intermediate,'baseline_count':len(original),
        'combined_routes':combined,'revised_routes':revisions,'final':final,'focused_tasks':focused,
        'revision_parent_associations':associations,'ambiguous_revision_graph_keys':ambiguous,
        'additional_unique_graphs':len({r['graph_key'] for r in [*combined,*revisions]}-original),
        'original_model_routes_preserved':all(any(x['graph_key']==r['graph_key'] for x in combined) for r in baseline),
        'timing':{'total_seconds':ended-started,'search_proposer_overlap_seconds':max(0,min(model_result['ended_monotonic'],proposal['ended_monotonic'])-max(model_result['started_monotonic'],proposal['started_monotonic'])),
                  'postcollection_optimization_seconds':ended-revision_start,'additional_delay_after_model_seconds':max(0,ended-model_result['ended_monotonic']),
                  'first_model_result_seconds':model_result['ended_monotonic']-started if model_result['status']=='completed' else None},
        'chemical_feasibility_established':False,'public_model_output_unchanged':True}
    _save(folder/'result.json',output);return output
