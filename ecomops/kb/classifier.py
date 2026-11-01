"""Small, dependency-free character multinomial Naive Bayes query classifier.

This module deliberately does not wire itself into the KB service. Unknown
features are ignored; an empty or entirely unseen query uses class priors.
"""
from collections import Counter
import json
import math
from pathlib import Path

LABELS = ("GENERAL", "PRECISE", "VAGUE", "BROAD")
DEFAULT_MODEL_PATH = Path(__file__).with_name("query_model.json")
_CACHE = {}
_DIGITS = 12


def _features(query):
    text = ''.join(c for c in query.casefold() if c.isalnum())
    return Counter([*text, *(text[i:i + 2] for i in range(len(text) - 1))])


def train(rows) -> dict:
    """Fit Laplace-smoothed priors and likelihoods, with stable accumulation."""
    counts = {label: Counter() for label in LABELS}
    documents = Counter()
    for row in rows:
        label, query = row['label'], row['query']
        if label not in LABELS or not isinstance(query, str) or not query.strip():
            raise ValueError('Each row needs a nonempty query and a supported label')
        documents[label] += 1
        counts[label].update(_features(query))
    if not documents:
        raise ValueError('Training data must not be empty')
    vocabulary = sorted(set().union(*(set(c) for c in counts.values())))
    if not vocabulary:
        raise ValueError('Training data has no character features')
    priors, likelihoods = {}, {}
    for label in LABELS:
        priors[label] = round(math.log((documents[label] + 1) / (sum(documents.values()) + len(LABELS))), _DIGITS)
        denominator = sum(counts[label].values()) + len(vocabulary)
        likelihoods[label] = {token: round(math.log((counts[label][token] + 1) / denominator), _DIGITS)
                              for token in vocabulary}
    return {'version': 1, 'features': 'character_1_2_gram_alnum_casefold',
            'log_prior': priors, 'log_likelihood': likelihoods}


def _serialize(model):
    # Fixed decimals also on disk, independent of Python's shortest float repr.
    def encode(value):
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError('Nonfinite model value')
            return format(value, f'.{_DIGITS}f')
        if isinstance(value, dict):
            return '{' + ','.join(json.dumps(k, ensure_ascii=False) + ':' + encode(value[k])
                                  for k in sorted(value)) + '}'
        return json.dumps(value, ensure_ascii=False)
    return encode(model) + '\n'


def save_model(model, path):
    Path(path).write_text(_serialize(model), encoding='utf-8')
    _CACHE.pop(str(Path(path).resolve()), None)


def _validate(model):
    if not isinstance(model, dict) or model.get('version') != 1:
        raise ValueError('Unsupported model')
    if model.get('features') != 'character_1_2_gram_alnum_casefold':
        raise ValueError('Unsupported features')
    for key in ('log_prior', 'log_likelihood'):
        if set(model[key]) != set(LABELS):
            raise ValueError('Incomplete labels')
    vocabulary = None
    for label in LABELS:
        values = model['log_likelihood'][label]
        if not isinstance(values, dict) or not values:
            raise ValueError('Empty vocabulary')
        if any(not isinstance(k, str) or not 1 <= len(k) <= 2 for k in values):
            raise ValueError('Invalid feature')
        if vocabulary is not None and set(values) != vocabulary:
            raise ValueError('Inconsistent vocabulary')
        vocabulary = set(values)
        for number in [model['log_prior'][label], *values.values()]:
            if type(number) not in (float, int) or not math.isfinite(number) or number > 0:
                raise ValueError('Invalid log probability')


def load_model(path=DEFAULT_MODEL_PATH) -> dict | None:
    """Cache by resolved path and file identity; missing/corrupt files fail closed."""
    try:
        path = Path(path).resolve()
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        key = str(path)
        if key in _CACHE and _CACHE[key][0] == signature:
            return _CACHE[key][1]
        model = json.loads(path.read_text(encoding='utf-8'))
        _validate(model)
        _CACHE[key] = (signature, model)
        return model
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None


def predict(model, query) -> tuple[str, dict[str, float]]:
    features = _features(query)
    scores = {}
    for label in LABELS:
        likelihood = model['log_likelihood'][label]
        scores[label] = model['log_prior'][label] + math.fsum(
            count * likelihood[token] for token, count in sorted(features.items()) if token in likelihood)
    maximum = max(scores.values())
    weights = {label: math.exp(scores[label] - maximum) for label in LABELS}
    total = math.fsum(weights.values())
    probabilities = {label: weights[label] / total for label in LABELS}
    return max(LABELS, key=probabilities.get), probabilities


def refine(rule_label: str, query: str, model=None, min_confidence: float = 0.6) -> tuple[str, str]:
    """Only replace the rule's VAGUE fallback when sufficiently confident."""
    if rule_label != 'VAGUE':
        return rule_label, 'rule'
    if model is None:
        model = load_model()
    if model is None:
        return 'VAGUE', 'rule'
    label, probabilities = predict(model, query)
    if probabilities[label] >= min_confidence:
        return label, 'classifier'
    return 'VAGUE', 'rule'
