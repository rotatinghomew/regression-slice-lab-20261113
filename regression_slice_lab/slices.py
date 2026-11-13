import math


def wilson(success, total, z_value=1.959963984540054):
    if total < 0 or success < 0 or success > total:
        raise ValueError('成功数与总数不一致')
    if not total:
        return None
    probability = success / total
    denominator = 1 + z_value ** 2 / total
    center = (probability + z_value ** 2 / (2 * total)) / denominator
    half = z_value * math.sqrt(probability * (1 - probability) / total + z_value ** 2 / (4 * total ** 2)) / denominator
    return [max(0, center - half), min(1, center + half)]


def manual_comparison(rows, predicate, cutoff):
    selected = [row for row in rows if all(row.get(key) == value for key, value in predicate.items())]
    versions = sorted({row['firmware'] for row in rows})
    result = {}
    for version in versions:
        group = [row for row in selected if row['firmware'] == version]
        settled = [row for row in group if row.get('completed') is not None and row['outcome_known_at'] <= cutoff]
        success = sum(row['completed'] for row in settled)
        result[version] = {'attempts': len(group), 'settled': len(settled), 'pending': len(group) - len(settled),
                           'users': len({row['user_id'] for row in settled}), 'completed': success,
                           'descriptive_interval': wilson(success, len(settled))}
    return result


def inspect(document):
    identities = [row['row_id'] for row in document['rows']]
    if len(identities) != len(set(identities)):
        raise ValueError('任务观测编号重复')
    return {'manual_slices': [{'where': predicate, 'observed': manual_comparison(document['rows'], predicate, document['cutoff'])}
                             for predicate in document['manual_slices']]}
