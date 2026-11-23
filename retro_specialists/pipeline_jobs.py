"""Main-only asynchronous workflow jobs; no creation tools are granted to leaves.

The runner and deployment template are operator-owned pinned inputs. Molecular
arguments become JSON data only. A terminal failure never resubmits remote work.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import fcntl
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
from threading import Thread
from uuid import uuid4

MAX_BYTES = 32_000_000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write(path, value):
    raw=json.dumps(value,ensure_ascii=True,allow_nan=False).encode()
    if len(raw)>MAX_BYTES:raise ValueError('Job artifact exceeds bound')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())


def _file(path, maximum=MAX_BYTES):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('Absolute trusted file required')
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Symlink configuration or artifact rejected')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_size>maximum:
            raise ValueError('Bounded owned regular file required')
        raw=stream.read(maximum+1)
    if len(raw)>maximum:raise ValueError('File exceeds bound')
    return raw


def _json(path, maximum=MAX_BYTES):
    return json.loads(_file(path,maximum))


def _folder(state_dir):
    from .science import _directory
    root=_directory(Path(state_dir)/'workflow-jobs')
    return root


def _job_id(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('Invalid workflow job identifier')
    return value


def _process_identity(pid):
    try:
        raw=Path('/proc')/str(pid)/'stat'
        fields=raw.read_text().split(') ',1)[1].split()
        return None if fields[0]=='Z' else fields[19]
    except (OSError,IndexError,ValueError):return None


def _deployment(trusted):
    for key in ('pipeline_runner_file','pipeline_runner_sha256'):
        if not isinstance(trusted.get(key),str) or not trusted[key]:raise ValueError('Configure pipeline template and pinned runner before starting workflows')
    runner=Path(trusted['pipeline_runner_file']);raw=_file(runner,2_000_000)
    digest=trusted['pipeline_runner_sha256']
    if not re.fullmatch('[a-f0-9]{64}',digest) or sha256(raw).hexdigest()!=digest:raise ValueError('Pipeline runner digest mismatch')
    if 'portable_template' in trusted:
        template=trusted['portable_template']
    else:
        if not trusted.get('pipeline_config_file'):
            raise ValueError('Configure a pipeline template before starting workflows')
        template=_json(trusted['pipeline_config_file'],1_000_000)
    if not isinstance(template,dict):raise ValueError('Pipeline template must be an object')
    if (template.get('search_action','submit')!='submit' or
            any(key in template for key in ('existing_doc_id','recovered_proposal_from','baseline_file'))):
        raise ValueError('Main workflow admission requires a fresh submission template without resume receipts or proposal checkpoints')
    libraries=trusted.get('library_paths',[])
    if not isinstance(libraries,list) or any(not isinstance(v,str) or not Path(v).is_absolute() for v in libraries):raise ValueError('Invalid deployment library directories')
    return runner,template,libraries


def _replace_index(path,value):
    temporary=path.parent/('.index-'+uuid4().hex)
    try:
        _write(temporary,value)
        os.replace(temporary,path)
        descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def continuous_configuration(template):
    """Upgrade legacy wall-clock settings for the combined workflow before admission."""
    config = {key: value for key, value in template.items() if key != 'search_timeout_seconds'}
    native = {key: value for key, value in template['native'].items() if key != 'timeout_seconds'}
    deadlines = {'proposal_timeout_seconds', 'optimization_timeout_seconds', 'search_timeout_seconds'}
    pipeline = {key: value for key, value in template.get('pipeline', {}).items() if key not in deadlines}
    return {**config, 'native': native, 'pipeline': {**pipeline, 'until_search_finishes': True}}


def start_workflow(target,name,trusted):
    from .graphs import canonical_smiles
    target=canonical_smiles(target)
    if not isinstance(name,str) or not name.strip() or len(name)>300:raise ValueError('Target name must be bounded nonempty text')
    root=_folder(trusted['state_dir']);key=sha256(target.encode()).hexdigest()
    lock=os.open(root/('admission-'+key+'.lock'),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode & 0o077:
            raise ValueError('Admission lock must be an owner-only regular file')
        fcntl.flock(lock,fcntl.LOCK_EX)
        index=root/('admission-'+key+'.json')
        if index.exists():
            previous=_json(index,10000)
            if previous.get('target')!=target:raise ValueError('Admission target index mismatch')
            job_id=_job_id(previous.get('job_id'))
            request=_json(root/job_id/'request.json',1_000_000)
            if request.get('target')!=target:raise ValueError('Admission job target mismatch')
            observed=workflow_status(job_id,trusted)
            matched=observed.get('summary',{}).get('matched_tool_comparison')
            if observed['state']!='completed' or matched is False:
                return {'job_id':job_id,'state':observed['state'],'target':target,'reused_existing_job':True,
                    'matched_tool_comparison':matched,
                    'meaning':'Existing workflow keeps its original name and configuration; new settings were not applied. Poll or manually resume its existing scientific receipt; never automatically resubmit after failure or unknown observation.'}
        runner,template,libraries=_deployment(trusted)
        template=continuous_configuration(template)
        route=trusted.get('main_route')
        if not isinstance(route,dict) or any(not isinstance(route.get(key),str) or not route[key].strip() for key in ('model','provider')):
            raise ValueError("Main's active model and provider are required")
        native={**template['native'], 'model':route['model'], 'provider':route['provider']}
        job_id=uuid4().hex;folder=root/job_id;folder.mkdir(mode=0o700)
        config={**template,'native':native,'target':target,'name':name}
        if 'portable_science' in trusted:
            science=folder/'science.json'
            _write(science,trusted['portable_science'])
            config={**config,'science_config':str(science)}
        _write(folder/'config.json',config)
        _write(folder/'request.json',{'job_id':job_id,'target':target,'name':name,'started_at':_now(),
            'runner_file':str(runner),'runner_sha256':trusted['pipeline_runner_sha256'],
            'root':str(Path(__file__).resolve().parents[1]),'library_paths':libraries})
        # Publish the admission before launch. A lost start reply or interrupted
        # launcher therefore cannot automatically create a second remote job.
        _replace_index(index,{'job_id':job_id,'target':target})
        log=os.open(folder/'worker.log',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            process=subprocess.Popen([sys.executable,'-I',str(Path(__file__).resolve()),'--worker',str(folder)],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
        finally:os.close(log)
        identity=_process_identity(process.pid)
        Thread(target=process.wait,daemon=True).start()
        _write(folder/'process.json',{'pid':process.pid,'start_identity':identity})
        return {'job_id':job_id,'state':'running','target':target,'reused_existing_job':False,
            'next_action':'Poll retrosynthesis_workflow_status with this job_id. Repeated starts for this pending target reuse this job.'}
    finally:
        fcntl.flock(lock,fcntl.LOCK_UN)
        os.close(lock)


def _summary(result):
    final=result.get('final',{})
    selected=final.get('selected_routes',[])[:20]
    proposer=result.get('proposer',{})
    proposer=proposer if isinstance(proposer,dict) else {}
    proposals=proposer.get('routes',[])
    proposals=proposals[:20] if isinstance(proposals,list) else []
    stereo_requirements=None
    if result.get('target'):
        from .stereochemistry import target_stereo_requirements
        stereo_requirements=target_stereo_requirements(result['target'])
    model=result.get('model_branch',{})
    model_status=model.get('status')
    metadata=model.get('original_response',{})
    metadata=metadata if isinstance(metadata,dict) else {}
    matched=None if model_status is None else model_status=='completed'
    unavailable=model_status in ('failed','timed_out','cancelled')
    limitations=final.get('limitations',[])[:20]
    if unavailable:
        limitations=[('The tool result is unavailable or incomplete; available local route count is not '
            'the number returned by the remote tool. No matched tool comparison or added-route gain '
            'is established. Scoped assessments of independent hypotheses may remain.'),*limitations][:20]
    # The bounded route objects carry backend assessments, not raw transcripts.
    summary={'baseline_count':None if unavailable else result.get('baseline_count'),
        'additional_unique_graphs':None if unavailable else result.get('additional_unique_graphs'),
        'model_branch_status':model_status,'model_artifact_status':metadata.get('artifact_status'),
        'matched_tool_comparison':matched,
        'available_model_route_count':len(model['routes']) if isinstance(model.get('routes'),list) else None,
        'proposer_status':proposer.get('status'),
        'proposer_routes':proposals,'stereochemical_requirements':stereo_requirements,
        'proposer_metadata':{key:proposer[key] for key in
            ('counts','started_monotonic','ended_monotonic','request_sha256') if key in proposer},
        'optimization_branch_statuses':{role:branch.get('status') for role,branch in result.get('optimization_branches',{}).items()},
        'selected_routes':selected,'accepted_improvements':final.get('accepted_improvements',[])[:40],
        'existing_alternatives':final.get('existing_alternatives',[])[:40],
        'revision_decisions':final.get('revision_decisions',[])[:40],'timing':result.get('timing'),
        'original_model_routes_preserved':None if unavailable else result.get('original_model_routes_preserved'),
        'chemical_feasibility_established':False,'total_synthesis_cost_reduction_established':False,
        'limitations':limitations}
    if len(json.dumps(summary).encode())>1_500_000:raise ValueError('Workflow summary exceeds bound')
    return summary


def workflow_status(job_id,trusted):
    folder=_folder(trusted['state_dir'])/_job_id(job_id)
    if not folder.exists():return {'job_id':job_id,'state':'not_found'}
    if folder.is_symlink():raise ValueError('Symlink job rejected')
    request=_json(folder/'request.json',1_000_000)
    terminal=folder/'terminal.json'
    if terminal.exists():
        record=_json(terminal,10000)
        result={'job_id':job_id,'state':record['state'],'target':request['target'],'finished_at':record['finished_at']}
        if record['state']=='completed':result['summary']=_summary(_json(folder/'run/benchmark-result.json'))
        else:result['reason']='Workflow failed; inspect private job diagnostics before deciding whether to resume its existing scientific receipt.'
        return result
    process=folder/'process.json'
    if not process.exists():return {'job_id':job_id,'state':'starting','target':request['target']}
    record=_json(process,10000)
    identity=_process_identity(record['pid'])
    state='running' if identity is not None and identity==record['start_identity'] else 'observation_unknown'
    return {'job_id':job_id,'state':state,'target':request['target'],
        'meaning':'Local worker observation only; missing worker does not establish remote cancellation. Never automatically resubmit.'}


def _worker(folder):
    request=_json(folder/'request.json',1_000_000)
    sys.path[:0]=[request['root'],*request['library_paths']]
    state='failed';error=None
    try:
        runner=Path(request['runner_file'])
        if sha256(_file(runner,2_000_000)).hexdigest()!=request['runner_sha256']:raise ValueError('Runner changed after admission')
        # JSON config carries molecular text; argv contains only host-created paths.
        sys.argv=[str(runner),'--config',str(folder/'config.json'),'--output',str(folder/'run')]
        try:
            runpy.run_path(str(runner),run_name='__main__')
        except SystemExit as exc:
            if exc.code not in (None,0):raise ValueError('Workflow runner failed') from exc
        result=_json(folder/'run/benchmark-result.json')
        if not isinstance(result,dict):raise ValueError('Workflow result must be an object')
        state='completed'
    except Exception as exc:error=type(exc).__name__
    _write(folder/'terminal.json',{'state':state,'error_type':error,'finished_at':_now()})


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--worker':raise SystemExit('Internal workflow worker only')
    _worker(Path(sys.argv[2]))
