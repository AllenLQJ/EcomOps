"""Small, developer-labelled regression evaluation; no database or implicit network calls."""
import argparse
import asyncio
from contextlib import contextmanager
import json
import math
from pathlib import Path
import statistics
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA = ROOT / 'eval'
DATASETS = {'问题分类': 'kb_classify', '合规检查器': 'listing_cases', '三轨路由': 'claim_routes',
            '对练阶段推进': 'training_turns', '检索命中率': 'kb_retrieval'}


def load_jsonl(name):
    path = DATA / f'{name}.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def confusion_matrix(expected, predicted, labels):
    if len(expected) != len(predicted):
        raise ValueError('expected/predicted length mismatch')
    matrix = {a: {b: 0 for b in labels} for a in labels}
    for a, b in zip(expected, predicted):
        matrix[a][b] += 1
    return matrix


def binary_metrics(expected, predicted):
    matrix = confusion_matrix(expected, predicted, (False, True))
    tp, fp, fn = matrix[True][True], matrix[False][True], matrix[True][False]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {'precision': precision, 'recall': recall,
            'f1': 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def result(rows, predicted, key, labels):
    expected = [r[key] for r in rows]
    matrix = confusion_matrix(expected, predicted, labels)
    return {'n': len(rows), 'accuracy': sum(a == b for a, b in zip(expected, predicted)) / len(rows) if rows else 0.0,
            'matrix': matrix, 'errors': [{'sample': r, 'predicted': p} for r, p in zip(rows, predicted) if r[key] != p]}


@contextmanager
def local_rules():
    # Scope the overrides and restore them even on failure. Never invoke configured LLMs.
    from ecomops.core import llm
    with patch.object(llm, 'configured', return_value=False), patch.object(llm, 'should_fail', return_value=False):
        yield


def evaluate_classification(rows):
    from ecomops.kb.service import classify_query
    return result(rows, [classify_query(r['query']) for r in rows], 'expected',
                  ('GENERAL', 'PRECISE', 'VAGUE', 'BROAD'))


def evaluate_listing(rows):
    from ecomops.listing.checkers import check, classify_results, extract_structure
    predicted = []
    with local_rules():
        for row in rows:
            doc = extract_structure(row['text'])
            checks = [check(name, doc, row['category']) for name in
                      ('extreme_words', 'required_params', 'medical_claims', 'cert_extract', 'readability')]
            predicted.append(classify_results(checks)['verdict'] == 'REJECTED_BLOCKING')
    return result(rows, predicted, 'expected_blocking', (False, True)) | binary_metrics(
        [r['expected_blocking'] for r in rows], predicted)


def evaluate_routes(rows):
    from ecomops.claims.tracks import run_three, route

    async def run():
        return [route(r['ticket'], await run_three(r['ticket']), 5000)[0] for r in rows]

    with local_rules():
        predicted = asyncio.run(run())
    return result(rows, predicted, 'expected_route', ('auto_reject', 'need_evidence', 'auto_approve', 'supervisor'))


def evaluate_training(rows):
    from ecomops.training.service import STAGES, evaluate_answer
    predicted = [evaluate_answer(r['stage'], r['answer']) for r in rows]
    output = result(rows, predicted, 'expected_pass', (False, True))
    output['stages'] = {stage: result([r for r in rows if r['stage'] == stage],
                                    [p for r, p in zip(rows, predicted) if r['stage'] == stage],
                                    'expected_pass', (False, True)) for stage in STAGES}
    return output


def evaluate_retrieval(rows, rag_url=None, rag_token=None):
    if not rag_url:
        return {'n': len(rows), 'skipped': '未运行（需要 EcomRAG）'}
    import httpx
    predicted, distributions, failures = [], [], []
    headers = {'X-Internal-Token': rag_token} if rag_token else {}
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        for index, row in enumerate(rows, 1):
            try:
                response = client.post(rag_url.rstrip('/') + '/api/v1/rag/search',
                                       json={'query': row['query'], 'top_k': 8}, headers=headers)
                response.raise_for_status()
                scores = []
                for hit in response.json()['hits']:
                    score = hit['score']
                    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
                        raise ValueError('invalid score')
                    scores.append(float(score))
                predicted.append(max(scores, default=0) >= .75)
                distributions.append((index, row['query'], min(scores) if scores else None,
                                      statistics.median(scores) if scores else None, max(scores) if scores else None))
            except Exception as exc:
                # Do not turn transport/response errors into correct negative predictions or expose credentials.
                failures.append({'sample': row, 'error': type(exc).__name__})
                predicted.append(None)
    valid = [(r, p) for r, p in zip(rows, predicted) if p is not None]
    output = result([r for r, _ in valid], [p for _, p in valid], 'expect_hit', (False, True))
    matrix = output['matrix']
    inside = sum(matrix[True].values())
    outside = sum(matrix[False].values())
    output.update(n=len(rows), evaluated=len(valid), failures=failures, distributions=distributions,
                  inside_recall=matrix[True][True] / inside if inside else None,
                  outside_fpr=matrix[False][True] / outside if outside else None)
    return output


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', '<br>')


def percent(value):
    return f'{value:.2%}' if value is not None else 'N/A'


def render_report(datasets, results):
    counts = '、'.join(f'{name} {len(datasets[name])} 条' for name in DATASETS)
    lines = ['数据说明：标注集由开发者自己编写、规模很小（' + counts + '），**不具统计意义，只用于发现回归和明显错误**。',
             '', '合规仅评测本地规则及三层判定；三轨固定默认 15 天、5000 分门槛，关闭 LLM 与故障注入。对练评测阶段通过判定，finished 不再推进。检索阈值为 score ≥ 0.75；空命中按未命中，分布记 N/A；请求失败单列且不计入准确率分母。',
             '', '| 项目 | 样本数 | 指标 |', '|---|---:|---|']
    for name, r in results.items():
        if 'skipped' in r:
            metric = r['skipped']
        elif 'error' in r:
            metric = '运行失败：' + r['error']
        elif name == '合规检查器':
            metric = '；'.join(f'{k}={percent(r[k])}' for k in ('precision', 'recall', 'f1'))
        elif name == '对练阶段推进':
            metric = '；'.join(f'{s}({v["n"]})={percent(v["accuracy"])}' for s, v in r['stages'].items())
        else:
            metric = '准确率=' + (percent(r['accuracy']) if r.get('evaluated', r['n']) else 'N/A')
            if name == '检索命中率':
                metric += f'；库内召回={percent(r["inside_recall"])}；库外误判率={percent(r["outside_fpr"])}；有效={r["evaluated"]}；请求失败={len(r["failures"])}'
        lines.append(f'| {name} | {r["n"]} | {metric} |')
    for name, r in results.items():
        if 'matrix' not in r:
            continue
        lines.extend(['', f'### {name}（{r["n"]} 条）'])
        if name in ('问题分类', '三轨路由'):
            labels = list(r['matrix'])
            lines += ['', '混淆矩阵（行=标注，列=预测）：', '', '| 标注 / 预测 | ' + ' | '.join(labels) + ' |',
                      '|---|' + '---:|' * len(labels)]
            lines += ['| ' + str(a) + ' | ' + ' | '.join(str(r['matrix'][a][b]) for b in labels) + ' |' for a in labels]
        lines += ['', f'判错样本：{len(r["errors"])} 条（最多展示 10 条）。']
        for error in r['errors'][:10]:
            lines.append('- ' + cell(json.dumps(error, ensure_ascii=False)))
        for failure in r.get('failures', [])[:10]:
            lines.append('- 请求失败：' + cell(json.dumps(failure, ensure_ascii=False)))
        if 'distributions' in r:
            lines += ['', '| 序号 | 查询 | min | median | max |', '|---:|---|---:|---:|---:|']
            lines += [f'| {i} | {cell(q)} | ' + ' | '.join('N/A' if s is None else f'{s:.4f}' for s in scores) + ' |'
                      for i, q, *scores in r['distributions']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rag-url')
    parser.add_argument('--rag-token')
    parser.add_argument('--out', type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    datasets, results = {}, {}
    functions = (evaluate_classification, evaluate_listing, evaluate_routes, evaluate_training,
                 lambda rows: evaluate_retrieval(rows, args.rag_url, args.rag_token))
    for (name, filename), evaluate in zip(DATASETS.items(), functions):
        datasets[name] = []
        try:
            datasets[name] = load_jsonl(filename)
            results[name] = evaluate(datasets[name])
        except Exception as exc:
            results[name] = {'n': len(datasets[name]), 'error': type(exc).__name__}
    report = render_report(datasets, results)
    print(report, end='')
    if args.out:
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report, encoding='utf-8')
        except Exception as exc:
            print(f'报告写入失败：{type(exc).__name__}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
