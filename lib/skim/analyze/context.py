"""정제된 변경 단위에서 PR 개요의 제한된 근거를 만든다."""
from collections import defaultdict

from .protocol import prompt
from .units import changed_lines


def _prefix(text, limit):
    return text.encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def overview_input(units, metadata, max_bytes=32 * 1024):
    """각 파일에서 한 단위씩 선택하며 제외·누락된 원문은 포함하지 않는다."""
    groups = defaultdict(list)
    excluded = omitted = 0
    for unit in units:
        if unit.get('excluded'):
            excluded += 1
        elif unit.get('omission_reason'):
            omitted += 1
        else:
            groups[unit['file_path']].append(unit)
    eligible = sum(len(group) for group in groups.values())
    data = {'metadata': {'title': _prefix(metadata.get('title') or '', 1024),
                         'body': _prefix(metadata.get('body') or '', 4096)},
            'samples': [], 'coverage': {'eligible_units': eligible, 'sampled_units': 0,
                                       'excluded_units': excluded, 'omitted_units': omitted,
                                       'redacted_units': sum(bool(unit.get('redacted')) for unit in units)}}
    if len(prompt('overview', data).encode('utf-8')) > max_bytes:
        raise ValueError('PR 개요 입력 한도가 너무 작습니다.')
    # 파일을 한 바퀴씩 순회해 큰 파일이 나머지 파일의 자리를 차지하지 않게 한다.
    for index in range(max((len(group) for group in groups.values()), default=0)):
        for path, group in groups.items():
            if index >= len(group):
                continue
            unit = group[index]
            changed = changed_lines(unit['diff'])
            excerpt = _prefix(changed or unit['diff'], 384)
            sample = {'change_id': unit['change_id'], 'file_path': path, 'excerpt': excerpt,
                      'excerpt_truncated': len(excerpt.encode('utf-8')) < len((changed or unit['diff']).encode('utf-8'))}
            data['samples'].append(sample)
            data['coverage']['sampled_units'] = len(data['samples'])
            if len(prompt('overview', data).encode('utf-8')) > max_bytes:
                data['samples'].pop()
                data['coverage']['sampled_units'] = len(data['samples'])
    return data


def synthesis_input(units, metadata, items, coverage, overview=None, uncertainties=(), max_bytes=180 * 1024):
    """합성 요청을 제한하면서 변경 ID와 파일 경로의 관계를 보존한다."""
    by_id = {unit['change_id']: unit for unit in units if not unit.get('excluded') and not unit.get('omission_reason')}
    data = {'metadata': {'title': _prefix(metadata.get('title') or '', 1024),
                         'body': _prefix(metadata.get('body') or '', 4096)},
            'items': [], 'uncertainties': [], 'coverage': dict(coverage, synthesis_included=0, synthesis_omitted=0,
                                                               synthesis_uncertainties_omitted=len(uncertainties))}
    if overview is not None:
        data['overview'] = overview
    if len(prompt('synthesis', data).encode('utf-8')) > max_bytes:
        raise ValueError('합성 입력의 기본 크기가 한도를 초과했습니다.')
    for item in items:
        unit = by_id.get(item['change_id'])
        if unit is None:
            raise ValueError('합성 입력의 변경 ID가 유효하지 않습니다.')
        path = _prefix(unit['file_path'], 512)
        summary = {'change_id': item['change_id'], 'file_path': path, 'file_path_truncated': path != unit['file_path'],
                   'importance': item['importance'], 'reason': _prefix(item['reason'], 300),
                   'risk_flags': item['risk_flags']}
        data['items'].append(summary)
        data['coverage']['synthesis_included'] = len(data['items'])
        data['coverage']['synthesis_omitted'] = len(items) - len(data['items'])
        if len(prompt('synthesis', data).encode('utf-8')) > max_bytes:
            data['items'].pop()
            data['coverage']['synthesis_included'] = len(data['items'])
            data['coverage']['synthesis_omitted'] = len(items) - len(data['items'])
    for uncertainty in uncertainties:
        if uncertainty['change_id'] not in by_id:
            raise ValueError('합성 불확실성의 변경 ID가 유효하지 않습니다.')
        data['uncertainties'].append({'change_id': uncertainty['change_id'], 'reason': _prefix(uncertainty['reason'], 300)})
        data['coverage']['synthesis_uncertainties_omitted'] = len(uncertainties) - len(data['uncertainties'])
        if len(prompt('synthesis', data).encode('utf-8')) > max_bytes:
            data['uncertainties'].pop()
            data['coverage']['synthesis_uncertainties_omitted'] = len(uncertainties) - len(data['uncertainties'])
    return data
