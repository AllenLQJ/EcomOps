"""Customer training workflow; the session table is the query projection."""
import json
import re
from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ecomops.core import llm, rag
from ecomops.core.auth import Identity
from ecomops.core.errors import ApiError
from ecomops.training import service


class State(TypedDict, total=False):
    session_id: str
    tenant_id: str
    user_id: str
    message: str
    data: dict
    accepted: bool
    advanced: bool
    policy_mismatch: bool
    reply: str
    fallback_used: bool


def connection(config):
    return config['configurable']['connection']


async def load_context(state: State, config):
    identity = Identity(tenant_id=state['tenant_id'], user_id=state['user_id'], role='cs_agent')
    data = await service.load(connection(config), state['session_id'], identity, lock=True)
    if data['status'] != 'ACTIVE' or data['stage'] not in service.KEYWORDS:
        raise ApiError(422, 'INVALID_STAGE', 'training session has ended or has an invalid stage')
    return {'data': data, 'reply': '', 'advanced': False, 'policy_mismatch': False, 'fallback_used': False}


def check_stage(state):
    return {}


def quantities(value):
    return {(Decimal(number), unit) for number, unit in
            re.findall(r'(\d+(?:\.\d+)?)\s*(天|元|%|％)', value.replace('％', '%'))}


def clause_text(value):
    """Read clause content, without treating numeric retrieval metadata as policy."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(clause_text(item) for item in value)
    if isinstance(value, dict):
        return '\n'.join(clause_text(v) for k, v in value.items()
                         if k not in ('score', 'chunk_id', 'metadata'))
    return ''


async def evaluate_answer(state):
    data = dict(state['data'])
    answer, stage = state['message'], data['stage']
    checked = mismatch = False
    if stage == 'policy_explain':
        topics = [word for word in service.KEYWORDS[stage] if word in answer]
        query = ' '.join(topics) or json.dumps(data['scenario_seed'], ensure_ascii=False)
        hits = await rag.search(query, tenant_id=state['tenant_id'], top_k=3)
        checked = hits is not None
        mismatch = checked and bool(quantities(answer) - quantities(clause_text(hits)))
    entry = dict(role='user', content=answer, stage=stage)
    if stage == 'policy_explain':
        entry.update(policy_checked=checked, policy_mismatch=mismatch)
    data['messages'] = [*data['messages'], entry]
    return {'data': data, 'accepted': service.evaluate_answer(stage, answer) and not mismatch,
            'policy_mismatch': mismatch}


def advance_or_keep(state):
    data = dict(state['data'])
    advanced = state['accepted'] or data['follow_up_count'] >= 2
    if advanced:
        data['stage'] = service.STAGES[service.STAGES.index(data['stage']) + 1]
        data['follow_up_count'] = 0
    else:
        data['follow_up_count'] += 1
    return {'data': data, 'advanced': advanced}


async def generate_response(state):
    data = state['data']
    reply = service.buyer_reply(data['scenario_seed'], data['stage'], data['follow_up_count'])
    challenge = '你引用的天数、金额或比例与条款不一致，请核对条款后解释。' if state['policy_mismatch'] else ''
    fallback = True
    if llm.configured():
        system = (f'你是挑剔的电商买家，当前阶段：{data["stage"]}。只输出买家回复，不评分、不改变阶段。'
                  f'{llm.DATA_NOTICE}\n场景种子：{llm.untrusted("scenario_seed", json.dumps(data["scenario_seed"], ensure_ascii=False))}'
                  f'\n本次要求：{challenge or "针对当前阶段追问"}；同一争议点最多追问两次。')
        try:
            result = await llm.complete(system, llm.untrusted('agent_reply', state['message']))
            if isinstance(result, str) and result.strip():
                reply, fallback = result.strip(), False
        except llm.LlmError:
            pass
    if challenge:
        reply += challenge
    return {'reply': reply, 'fallback_used': fallback}


def generate_report(state):
    data = dict(state['data'])
    data['report'] = service.make_report(data['messages'])
    return {'data': data}


async def save_report(state):
    data = dict(state['data'])
    data['status'] = 'FINISHED'
    # The report and final reply are persisted atomically by save_memory.
    response = await generate_response(state)
    return {'data': data, **response}


async def save_memory(state, config):
    data = dict(state['data'])
    data['messages'] = [*data['messages'], dict(role='assistant', content=state['reply'], stage=data['stage'])]
    if len(data['messages']) % 20 == 0:
        data['summary'] = service.summarize(data['messages'])
    await service.save(connection(config), data)
    return {'data': data}


def build_graph(checkpointer=None):
    graph = StateGraph(State)
    for node in (load_context, check_stage, evaluate_answer, advance_or_keep,
                 generate_response, generate_report, save_report, save_memory):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, 'load_context')
    graph.add_edge('load_context', 'check_stage')
    graph.add_conditional_edges('check_stage', lambda s: 'generate_report' if s['data']['stage'] == 'finished' else 'evaluate_answer')
    graph.add_edge('evaluate_answer', 'advance_or_keep')
    graph.add_conditional_edges('advance_or_keep', lambda s: 'generate_report' if s['data']['stage'] == 'finished' else 'generate_response')
    graph.add_edge('generate_report', 'save_report')
    graph.add_edge('save_report', 'save_memory')
    graph.add_edge('generate_response', 'save_memory')
    graph.add_edge('save_memory', END)
    return graph.compile(checkpointer=checkpointer)


_default_graph = None


def default_graph():
    global _default_graph
    if _default_graph is None:
        _default_graph = build_graph()
    return _default_graph
