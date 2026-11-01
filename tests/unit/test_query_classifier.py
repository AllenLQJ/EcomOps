"""Offline tests: no DB fixtures, clients, or network calls."""
import json
import math
from pathlib import Path

import pytest

from ecomops.kb import classifier
from ecomops.kb.classifier import LABELS, load_model, predict, refine, save_model, train
from ecomops.kb.service import classify_query

ROOT = Path(__file__).resolve().parents[2]


def rows(name):
    return [json.loads(line) for line in (ROOT / 'eval' / name).read_text(encoding='utf-8').splitlines()]


@pytest.fixture(scope='module')
def tiny_model():
    return train([{'query': query, 'label': label} for label, query in zip(
        LABELS, ('你好朋友', '发票怎么开', '东西怪怪的', '发货时间以及发票费用'))])


def test_training_determinism_and_committed_artifact(tmp_path):
    data = rows('kb_query_train.jsonl')
    first, second = train(data), train(list(reversed(data)))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    a, b = tmp_path / 'a.json', tmp_path / 'b.json'
    save_model(first, a)
    save_model(second, b)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() == classifier.DEFAULT_MODEL_PATH.read_bytes()


@pytest.mark.parametrize('query', ['发票怎么开', '你们这个怎么回事', '', '☃☃', '发' * 5000])
def test_probabilities(tiny_model, query):
    label, probabilities = predict(tiny_model, query)
    assert label in LABELS
    assert set(probabilities) == set(LABELS)
    assert all(math.isfinite(p) and 0 <= p <= 1 for p in probabilities.values())
    assert sum(probabilities.values()) == pytest.approx(1.0)


def test_laplace_likelihoods_and_unseen_features():
    model = train([{'query': '甲甲', 'label': 'GENERAL'}])
    # Features are 甲 twice and 甲甲 once; vocabulary size=2, total count=3.
    assert math.exp(model['log_likelihood']['GENERAL']['甲']) == pytest.approx(3 / 5)
    assert math.exp(model['log_likelihood']['GENERAL']['甲甲']) == pytest.approx(2 / 5)
    assert math.exp(model['log_prior']['GENERAL']) == pytest.approx(2 / 5)
    assert predict(model, '☃')[1]['GENERAL'] == pytest.approx(2 / 5)


@pytest.mark.parametrize('label', ['GENERAL', 'BROAD', 'PRECISE'])
def test_strong_rule_never_loads_or_predicts(monkeypatch, label):
    def forbidden(*args, **kwargs):
        pytest.fail('Nonfallback rules must bypass the classifier')
    monkeypatch.setattr(classifier, 'load_model', forbidden)
    monkeypatch.setattr(classifier, 'predict', forbidden)
    assert refine(label, '商品异常') == (label, 'rule')
    assert refine(label, '商品异常', model={}) == (label, 'rule')


def test_vague_high_confidence(tiny_model):
    label, probabilities = predict(tiny_model, '发票怎么开发票怎么开')
    assert label == 'PRECISE' and probabilities[label] >= 0.6
    assert refine('VAGUE', '发票怎么开发票怎么开', tiny_model) == ('PRECISE', 'classifier')


def test_vague_low_confidence(tiny_model):
    assert max(predict(tiny_model, '')[1].values()) == pytest.approx(0.25)
    assert refine('VAGUE', '', tiny_model) == ('VAGUE', 'rule')


def test_confidence_boundary_and_vague_prediction(monkeypatch):
    monkeypatch.setattr(classifier, 'predict', lambda *_: ('PRECISE', {'PRECISE': 0.6}))
    assert refine('VAGUE', 'q', model={}) == ('PRECISE', 'classifier')
    assert refine('VAGUE', 'q', model={}, min_confidence=0.61) == ('VAGUE', 'rule')
    monkeypatch.setattr(classifier, 'predict', lambda *_: ('VAGUE', {'VAGUE': 0.9}))
    assert refine('VAGUE', 'q', model={}) == ('VAGUE', 'classifier')


def test_missing_model_falls_back(monkeypatch, tmp_path):
    absent = tmp_path / 'absent.json'
    assert load_model(absent) is None
    monkeypatch.setattr(classifier, 'load_model', lambda: load_model(absent))
    assert refine('VAGUE', '发票怎么开') == ('VAGUE', 'rule')


@pytest.mark.parametrize('content', ['not json', 'null', '[]', '{}', '{"version":1}',
                                     '{"version":2}', '\ufeff{}'])
def test_corrupt_model(tmp_path, content):
    path = tmp_path / 'bad.json'
    path.write_text(content, encoding='utf-8')
    assert load_model(path) is None


def test_cache_and_invalidated_files(tmp_path, tiny_model):
    path = tmp_path / 'model.json'
    save_model(tiny_model, path)
    first = load_model(path)
    assert first is not None and load_model(path) is first
    path.write_text('{broken', encoding='utf-8')
    assert load_model(path) is None
    save_model(tiny_model, path)
    assert load_model(path) == tiny_model
    path.unlink()
    assert load_model(path) is None


@pytest.mark.parametrize('damage', ['nonfinite', 'missing_label', 'different_vocabulary'])
def test_semantically_corrupt_model(tmp_path, tiny_model, damage):
    model = json.loads(json.dumps(tiny_model))
    if damage == 'nonfinite':
        model['log_prior']['GENERAL'] = float('nan')
    elif damage == 'missing_label':
        del model['log_likelihood']['BROAD']
    else:
        model['log_likelihood']['VAGUE'].pop(next(iter(model['log_likelihood']['VAGUE'])))
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(model), encoding='utf-8')
    assert load_model(path) is None


def test_data_sizes_labels_balance_and_disjointness():
    training, testing = rows('kb_query_train.jsonl'), rows('kb_query_test.jsonl')
    assert len(training) >= 320 and len(testing) >= 80
    for data in (training, testing):
        assert len({r['query'] for r in data}) == len(data)
        assert all(set(r) == {'query', 'label'} and r['query'].strip() for r in data)
        counts = [sum(r['label'] == label for r in data) for label in LABELS]
        assert sum(counts) == len(data)
        assert max(counts) / min(counts) <= 1.3
    assert not {r['query'] for r in training} & {r['query'] for r in testing}


def test_committed_model_test_accuracy():
    model = load_model()
    assert model is not None
    data = rows('kb_query_test.jsonl')
    correct = sum(refine(classify_query(r['query']), r['query'], model)[0] == r['label'] for r in data)
    # Measured on the self-authored 80-example test set: 0.6625 (53/80).
    # This is a toy regression threshold, not a population accuracy estimate.
    assert correct / len(data) >= 0.6625 - 0.05


def test_existing_rule_correct_samples_remain_correct():
    data = rows('kb_classify.jsonl')
    assert len(data) == 40
    model = load_model()
    assert model is not None
    for row in data:
        rule = classify_query(row['query'])
        if rule == row['expected']:
            assert refine(rule, row['query'], model)[0] == row['expected'], row['query']


@pytest.mark.parametrize('data', [[], [{'query': '', 'label': 'GENERAL'}],
                                  [{'query': 'x', 'label': 'WRONG'}],
                                  [{'query': '☃', 'label': 'GENERAL'}]])
def test_invalid_training_data(data):
    with pytest.raises(ValueError):
        train(data)
