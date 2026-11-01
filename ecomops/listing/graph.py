"""商品文案审查工作流；外部副作用仅发生于节点。"""
import asyncio
import json
from typing import TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from ecomops.core.db import get_engine
from ecomops.core.errors import ApiError
from ecomops.core.storage import get_store
from ecomops.listing import checkers


class State(TypedDict, total=False):
    review_id: str
    tenant_id: str
    user_id: str
    declared_category: str
    pdf_bytes: bytes
    pdf_object_path: str
    extracted_text: str
    document: dict
    structured: dict
    checks: list
    report: dict


async def upload_store(state: State):
    path = await asyncio.to_thread(lambda: get_store().put(f"{state['review_id']}.pdf", state['pdf_bytes'], 'application/pdf'))
    return {'pdf_object_path': path}


async def download_pdf(state: State):
    try:
        data = await asyncio.to_thread(lambda: get_store().get(state['pdf_object_path']))
        if not data:
            raise ValueError('empty object')
    except Exception:
        raise ApiError(400, 'UNSUPPORTED_FILE', '无法读取 PDF') from None
    return {'pdf_bytes': data}


async def extract_text(state: State):
    try:
        value = await asyncio.to_thread(checkers.extract_pdf, state['pdf_bytes'])
    except (RuntimeError, ValueError):
        raise ApiError(400, 'UNSUPPORTED_FILE', '无法读取 PDF') from None
    return {'extracted_text': value}


def extract_structured(state: State):
    value = state['extracted_text']
    return {'structured': checkers.extract_structured(value), 'document': checkers.extract_structure(value)}


async def run_compliance_checks(state: State):
    results = await asyncio.gather(*(checkers.check_with_context(n, state['document'], state['declared_category'], state['structured'])
                                    for n in ('extreme_words', 'required_params', 'medical_claims', 'cert_extract', 'readability')))
    return {'checks': results}


def scenario_seed(report: dict, structured: dict, category: str):
    disputes = [{'point': i['message'], 'kind': kind, 'detail': dict(i)}
                for key, kind in (('blocking_violations', 'blocking'), ('manual_verify_items', 'manual'))
                for i in report[key]][:5]
    title = structured['title']
    opening = ('请解释一下：' + '；'.join(d['point'] for d in disputes) + '，有什么依据？'
               if disputes else '这款商品的参数、宣传效果和资质有什么依据？请提供可核验的说明。')
    return {'product_title': title, 'declared_category': category, 'buyer_persona': '挑剔买家',
            'disputes': disputes, 'opening_message': opening}


def diagnose_and_classify(state: State):
    report = checkers.classify_results(state['checks'])
    if not state['extracted_text'].strip():
        report['manual_verify_items'].append({'checker': 'text_extract', 'message': 'PDF 无可提取文本，请人工核实或提供可检索 PDF'})
        if report['verdict'] != 'REJECTED_BLOCKING':
            report['verdict'] = 'PENDING_MANUAL_VERIFY'
    report.update(review_id=state['review_id'], declared_category=state['declared_category'],
                  pdf_object_path=state['pdf_object_path'],
                  extracted_fields={k: v for k, v in state['document'].items() if k != 'text'})
    report['scenario_seed'] = scenario_seed(report, state['structured'], state['declared_category'])
    return {'report': report}


async def save_results(state: State):
    report = state['report']
    async with get_engine().begin() as conn:
        await conn.execute(text('''INSERT INTO listing_reviews
            (review_id, tenant_id, created_by, declared_category, pdf_object_path, verdict, report, fallback_used, scenario_seed)
            VALUES (:id, :tenant, :user, :category, :path, :verdict, CAST(:report AS jsonb), :fallback, CAST(:seed AS jsonb))
            ON CONFLICT (review_id) DO NOTHING'''),
            {'id': UUID(state['review_id']), 'tenant': state['tenant_id'], 'user': state['user_id'],
             'category': state['declared_category'], 'path': state['pdf_object_path'], 'verdict': report['verdict'],
             'report': json.dumps(report, ensure_ascii=False), 'fallback': report['fallback_used'],
             'seed': json.dumps(report['scenario_seed'], ensure_ascii=False)})
    return {}


def build_graph(checkpointer=None):
    graph = StateGraph(State)
    nodes = (upload_store, download_pdf, extract_text, extract_structured, run_compliance_checks,
             diagnose_and_classify, save_results)
    previous = START
    for node in nodes:
        graph.add_node(node.__name__, node)
        graph.add_edge(previous, node.__name__)
        previous = node.__name__
    graph.add_edge(previous, END)
    return graph.compile(checkpointer=checkpointer)
