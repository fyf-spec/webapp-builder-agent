"""Bounded per-task inference controls and explicit, retrievable context windowing."""
from contextvars import ContextVar
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
import re

CURRENT = ContextVar('model_task_policy', default=None)


def enabled(name):
    value = os.getenv('ARC_B5_' + name.upper())
    if value is None:
        if name == 'override_reasoning': return False
        return os.getenv('ARC_AGENT_PROFILE', 'b4').lower() == 'b4'
    if value.lower() not in {'0','1','true','false'}:
        raise ValueError('ARC_B5_' + name.upper() + ' must be boolean')
    return value.lower() in {'1','true'}


def setting(name, default):
    value = int(os.getenv('ARC_B5_' + name, str(default)))
    if value < 1: raise ValueError('ARC_B5_' + name + ' must be positive')
    return value


class ContextBudgetExceeded(ValueError):
    pass


@contextmanager
def task_call(state, round_number, checked_failures):
    token = CURRENT.set({'state':state, 'round':round_number, 'checked_failures':checked_failures})
    try: yield
    finally: CURRENT.reset(token)


def role_for(payload):
    task = payload.get('task')
    if not isinstance(task, dict): return 'design'
    if task.get('phase') == 'joint_application': return 'application'
    if 'original_requirements' in task: return 'repair'
    if 'implementation_paths' in task or 'page_paths' in task: return 'edit'
    if 'coverage_steps' in task or 'test_paths' in task or 'test_path' in task: return 'test'
    return 'edit'


def inference_policy(payload, model):
    context = CURRENT.get() or {'state':{}, 'round':1, 'checked_failures':0}
    role = role_for(payload)
    if context['checked_failures'] and role == 'edit': role = 'repair'
    base = getattr(model, 'reasoning', 'low')
    mode = base
    explicit = bool(os.getenv('MODEL_REASONING')) and not enabled('override_reasoning')
    escalated = False
    if enabled('task_reasoning') and not explicit:
        mode = os.getenv('ARC_B5_' + role.upper() + '_REASONING', 'none' if role == 'edit' else 'low').lower()
        if mode not in {'none','low','high','max','provider','auto'}: raise ValueError('Invalid per-task reasoning mode')
        if role == 'repair' and context['checked_failures'] >= 2 and not context['state'].get('escalated'):
            if enabled('repair_escalation'):
                mode = 'high'; context['state']['escalated'] = True; escalated = True
    maximum = getattr(model, 'max_output_tokens', 320000)
    output = maximum
    if enabled('output_budget'):
        default = {'edit':48000,'application':48000,'test':48000,'design':32000,'repair':32000}[role]
        initial = setting(role.upper() + '_OUTPUT_TOKENS', default)
        # Expand after rejected rounds, bounded by the user's existing global ceiling.
        output = min(maximum, initial * (2 ** min(context['round'] - 1, 3)))
        output = min(output, setting('MAX_POLICY_OUTPUT_TOKENS',96000))
        if escalated:
            output = min(output, setting('ESCALATED_OUTPUT_TOKENS',32000))
    return {'role':role, 'reasoning':mode, 'output_limit':output,
            'escalated':escalated, 'explicit_reasoning_preserved':explicit}


def compact_repair_task(payload):
    result = deepcopy(payload)
    task = result.get('task', {})
    if 'original_requirements' not in task: return result
    for rid, member in task.get('requirements', {}).items():
        if member.get('original_requirement') == task['original_requirements'].get(rid):
            member.pop('original_requirement', None)
        for key, canonical in [('api_interfaces','backend_apis'),('ui_interfaces','page_subtrees')]:
            links = member.get(key)
            records = task.get(canonical, {})
            if isinstance(links, dict) and links and all(identity in records and
                    all(records[identity].get(k) == v for k,v in item.items()) for identity,item in links.items()):
                member.pop(key)
    return result


LAYOUT_RULES = '''INPUT BUDGET: a file with exists=true and content_omitted_for_budget=true
already exists. Do not create it. Its content is available through allowed read_file
line ranges; supplied symbols are lexical navigation hints, not semantic summaries.
Read relevant ranges together, not every omitted file in full. Retained excerpts keep
exact line numbers. has_more/next_start_line means more original text remains available.
Canonical requirements and business contracts have not been shortened. In repair tasks,
original_requirements/backend_apis/page_subtrees hold canonical records once; redundant
copies inside requirements members may be omitted. existing_design.elements=null with
elements_omitted_for_budget=true means existing UI elements remain in the advertised
context index: query ui_element/page records before editing them. Preserve every original obligation.'''


def serialized_size(instructions, payload):
    return len(json.dumps({'messages':[{'role':'system','content':instructions},
        {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
        'response_format':{'type':'json_object'}},ensure_ascii=False))


def budget_context(instructions, payload, retained_reads=None):
    result = compact_repair_task(payload) if enabled('context_budget') else deepcopy(payload)
    original_reads = retained_reads if retained_reads is not None else result.get('read_files', {})
    from infrastructure.shared.agent.efficiency import compact_context
    result = compact_context(result)
    before = serialized_size(instructions, payload)
    maximum = setting('MAX_REQUEST_CHARS', 196608)
    omitted=[]; paged=[]; design_windowed=False
    if enabled('context_budget') and serialized_size(instructions, result) > maximum:
        files = result.get('files', {})
        candidates = sorted(files, key=lambda p:(bool(files[p].get('writable')), -len(files[p].get('content') or '')))
        for path in candidates:
            record = files[path]
            content = record.get('content')
            if not isinstance(content,str) or len(content) < 1000: continue
            lines=content.splitlines(keepends=True)
            symbols=[{'line':i,'text':line.strip()[:180]} for i,line in enumerate(lines,1)
                     if re.match(r'\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const|let)\s+\w+',line)]
            record.update(content=None, exists=True, content_omitted_for_budget=True,
                          total_lines=len(lines), symbols=symbols[:40])
            # Exact dedup may have removed this excerpt while the full file was pinned.
            # Once the file is windowed the actual tool observation must be restored.
            if path in original_reads:
                result.setdefault('read_files', {})[path] = deepcopy(original_reads[path])
            omitted.append(path)
            if serialized_size(instructions,result)<=maximum: break
        if serialized_size(instructions,result)>maximum:
            for path,excerpts in result.get('read_files',{}).items():
                for excerpt in excerpts:
                    content=excerpt.get('content')
                    if not isinstance(content,str): continue
                    lines=content.splitlines(keepends=True)
                    limit=setting('EXCERPT_LINES',160)
                    if len(lines)<=limit: continue
                    excerpt['content']=''.join(lines[:limit])
                    excerpt['end_line']=excerpt['start_line']+limit-1
                    excerpt.update(has_more=True,next_start_line=excerpt['end_line']+1)
                    paged.append(path)
        if serialized_size(instructions,result)>maximum:
            design=result.get('existing_design')
            if isinstance(design,dict) and isinstance(design.get('elements'),dict) and {'page','ui_element'}.issubset(result.get('context_kinds',[])):
                design['existing_element_ids']=list(design['elements'])
                design['elements']=None
                design['elements_omitted_for_budget']=True
                design_windowed=True
        if serialized_size(instructions,result)>maximum:
            raise ContextBudgetExceeded('Complete canonical request exceeds context budget after retrievable file windowing; split this task, not its requirements')
    if enabled('cache_layout'):
        if isinstance(result.get('files'),dict):
            result['files']={p:result['files'][p] for p in sorted(result['files'],key=lambda p:(bool(result['files'][p].get('writable')),p))}
        order=['tools','file_access','files','context_records','task','read_files','context_candidates','context_result','feedback']
        result={k:result[k] for k in [*[k for k in order if k in result],*[k for k in result if k not in order]]}
    after=serialized_size(instructions,result)
    return result,{'before_chars':before,'after_chars':after,'budget_chars':maximum,
                  'omitted_files':omitted,'paged_reads':paged,
                  'design_windowed':design_windowed,
                  'canonical_task_sha256':hashlib.sha256(json.dumps(result.get('task',{}),sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
