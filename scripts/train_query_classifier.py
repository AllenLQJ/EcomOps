#!/usr/bin/env python3
"""Train and evaluate the self-authored toy query classifier (no new dependencies)."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ecomops.kb.classifier import LABELS, predict, refine, save_model, train
from ecomops.kb.service import classify_query


def read_rows(path):
    with Path(path).open(encoding='utf-8') as source:
        return [json.loads(line) for line in source if line.strip()]


def report(name, rows, predictions):
    matrix = Counter((row['label'], predicted) for row, predicted in zip(rows, predictions))
    accuracy = sum(matrix[label, label] for label in LABELS) / len(rows)
    print(f'\n{name} accuracy: {accuracy:.4f}')
    print('confusion matrix: rows=true, columns=predicted')
    print('            ' + ' '.join(f'{label:>9}' for label in LABELS))
    for label in LABELS:
        print(f'{label:12}' + ' '.join(f'{matrix[label, other]:9}' for other in LABELS))
    print('class         precision    recall')
    for label in LABELS:
        tp = matrix[label, label]
        precision = tp / max(1, sum(matrix[other, label] for other in LABELS))
        recall = tp / max(1, sum(matrix[label, other] for other in LABELS))
        print(f'{label:12} {precision:9.4f} {recall:9.4f}')
    return accuracy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--train', type=Path, default=ROOT / 'eval/kb_query_train.jsonl')
    parser.add_argument('--test', type=Path, default=ROOT / 'eval/kb_query_test.jsonl')
    parser.add_argument('--output', type=Path, default=ROOT / 'ecomops/kb/query_model.json')
    args = parser.parse_args()
    training, testing = read_rows(args.train), read_rows(args.test)
    if not testing or {r['query'] for r in training} & {r['query'] for r in testing}:
        raise ValueError('Test data must be nonempty and disjoint from training data')
    print('自编小样本，不具统计意义')
    print(f'train={len(training)}, test={len(testing)}')
    model = train(training)
    save_model(model, args.output)
    report('classifier', testing, [predict(model, r['query'])[0] for r in testing])
    rules = [classify_query(r['query']) for r in testing]
    report('rule only', testing, rules)
    report('rule + classifier', testing,
           [refine(rule, r['query'], model)[0] for rule, r in zip(rules, testing)])
    print(f'\nmodel saved: {args.output}')


if __name__ == '__main__':
    main()
