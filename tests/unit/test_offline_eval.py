"""Offline-only tests: no database fixture, HTTP endpoint or external LLM."""
from collections import Counter
import importlib.util
from pathlib import Path
import socket

import pytest

SPEC = importlib.util.spec_from_file_location('offline_eval', Path(__file__).resolve().parents[2] / 'scripts/offline_eval.py')
eval_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(eval_module)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('offline tests must not access external services')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def test_datasets():
    m = eval_module
    rows = m.load_jsonl('kb_classify')
    assert len(rows) >= 40
    assert all(isinstance(r['query'], str) and r['query'] for r in rows)
    counts = Counter(r['expected'] for r in rows)
    assert set(counts) == {'GENERAL', 'PRECISE', 'VAGUE', 'BROAD'}
    assert min(counts.values()) >= 10
    assert any('以及' in r['query'] for r in rows)
    assert any(sum(r['query'].count(c) for c in '?？') > 1 for r in rows)
    rows = m.load_jsonl('listing_cases')
    assert len(rows) >= 30
    assert {r['expected_blocking'] for r in rows} == {True, False}
    assert all(isinstance(r['text'], str) and isinstance(r['category'], str)
               and type(r['expected_blocking']) is bool for r in rows)
    assert any('不是最好的' in r['text'] and not r['expected_blocking'] for r in rows)
    assert any('没有第一' in r['text'] and not r['expected_blocking'] for r in rows)
    rows = m.load_jsonl('claim_routes')
    assert len(rows) >= 30
    counts = Counter(r['expected_route'] for r in rows)
    assert set(counts) == {'auto_reject', 'need_evidence', 'auto_approve', 'supervisor'}
    assert min(counts.values()) >= 4
    for r in rows:
        t = r['ticket']
        assert all(type(t[k]) is int and t[k] >= 0 for k in
                   ('days_since_delivery', 'item_payable_cents', 'max_refundable_cents'))
        assert isinstance(t['buyer_reason_text'], str)
        assert isinstance(t['evidence_urls'], list)
        assert all(isinstance(url, str) for url in t['evidence_urls'])
    assert {15, 16} <= {r['ticket']['days_since_delivery'] for r in rows}
    assert {5000, 5001} <= {r['ticket']['item_payable_cents'] for r in rows}
    assert {True, False} == {bool(r['ticket']['evidence_urls']) for r in rows}
    rows = m.load_jsonl('training_turns')
    assert len(rows) >= 30
    assert Counter(r['stage'] for r in rows) == dict.fromkeys(
        ('verify_order', 'empathy_listen', 'policy_explain', 'solution_negotiate', 'finished'), 6)
    assert all(isinstance(r['answer'], str) and type(r['expected_pass']) is bool for r in rows)
    for word in ('抱歉', '理解'):
        assert any(word in r['answer'] and r['stage'] == 'empathy_listen' and r['expected_pass'] for r in rows)
    rows = m.load_jsonl('kb_retrieval')
    assert all(isinstance(r['query'], str) and type(r['expect_hit']) is bool for r in rows)
    assert {True, False} == {r['expect_hit'] for r in rows}
    assert {'退款多久到账', '售后怎么申请', '运费怎么算'} <= {r['query'] for r in rows}


def test_confusion_matrix_and_accuracy():
    r = eval_module.result([{'y': 'A'}, {'y': 'A'}, {'y': 'B'}], ['A', 'B', 'B'], 'y', ['A', 'B'])
    assert r['matrix'] == {'A': {'A': 1, 'B': 1}, 'B': {'A': 0, 'B': 1}}
    assert r['accuracy'] == pytest.approx(2 / 3)
    assert r['errors'] == [{'sample': {'y': 'A'}, 'predicted': 'B'}]
    with pytest.raises(ValueError):
        eval_module.confusion_matrix([True], [], [False, True])


def test_precision_recall_f1():
    # TP=2, FP=1, FN=2, TN=1 => precision=2/3, recall=1/2, F1=4/7.
    r = eval_module.binary_metrics([True, True, False, True, True, False],
                                   [True, True, True, False, False, False])
    assert r == pytest.approx({'precision': 2 / 3, 'recall': 1 / 2, 'f1': 4 / 7})
    assert eval_module.binary_metrics([], []) == {'precision': 0, 'recall': 0, 'f1': 0}
    assert eval_module.binary_metrics([False], [False])['f1'] == 0


def test_local_evaluators_isolate_configured_llm(monkeypatch):
    from ecomops.core import llm
    monkeypatch.setattr(llm, 'configured', lambda: True)
    monkeypatch.setattr(llm, 'should_fail', lambda _: True)
    m = eval_module
    assert m.evaluate_classification(m.load_jsonl('kb_classify'))['n'] == 40
    assert m.evaluate_listing(m.load_jsonl('listing_cases'))['recall'] == 1
    assert m.evaluate_routes(m.load_jsonl('claim_routes'))['accuracy'] == 1
    assert all(r['accuracy'] == 1 for r in m.evaluate_training(m.load_jsonl('training_turns'))['stages'].values())
    assert llm.configured() is True
    assert llm.should_fail('evidence_track') is True


def test_out_and_skipped_retrieval(tmp_path, capsys):
    out = tmp_path / 'nested' / '报告.md'
    assert eval_module.main(['--out', str(out)]) == 0
    printed = capsys.readouterr().out
    assert out.read_text(encoding='utf-8') == printed
    assert printed.startswith('数据说明：')
    assert '不具统计意义，只用于发现回归和明显错误' in printed
    assert '| 检索命中率 | 12 | 未运行（需要 EcomRAG） |' in printed
    assert '运行失败' not in printed
    # 否定语境修复前这里是 11 条误判（precision 52%）；现在只剩 1 条已知误判：培训材料里引用违规词，规则无法判断语境
    assert '判错样本：1 条（最多展示 10 条）' in printed
    assert printed.count('- {"sample":') == 1 and '销量第一' in printed
    assert 'recall=100.00%' in printed and 'precision=92.31%' in printed
    assert eval_module.evaluate_retrieval([], None)['skipped'] == '未运行（需要 EcomRAG）'


def test_default_prints_without_writing(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('default must not write files')
    monkeypatch.setattr(Path, 'write_text', forbidden)
    assert eval_module.main([]) == 0
    assert '未运行（需要 EcomRAG）' in capsys.readouterr().out


def test_failure_is_not_a_gate(monkeypatch, tmp_path, capsys):
    def broken(_):
        raise RuntimeError('unavailable')
    monkeypatch.setattr(eval_module, 'evaluate_classification', broken)
    assert eval_module.main(['--out', str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert '运行失败：RuntimeError' in output
    assert '报告写入失败：IsADirectoryError' in output
    assert '| 三轨路由 | 33 | 准确率=100.00%' in output
