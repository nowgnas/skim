"""diff 단위, 민감 정보 제외와 독립적인 위험 후보 계산."""
import fnmatch
import ast
import re
from pathlib import PurePosixPath

# 모델 출력에서는 값 자체가 확인되는 토큰·개인 키만 가린다.
SECRET_VALUE = re.compile(
    r'-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)'
    r'|(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk_(?:live|test)_[A-Za-z0-9]+'
    r'|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]+|AIza[A-Za-z0-9_-]{30,})')
SECRET_ASSIGNMENT = re.compile(r"(?im:\b(?:api[_-]?key|[a-z0-9_]*(?:secret|password|token|private[_-]?key)[a-z0-9_]*|authorization)[\"']?\s*[=:][^\n]*)")

EXCLUDED = ('.env', '.env.*', '*.pem', '*.key', '*credentials*', 'id_rsa*', 'id_ed25519*', '*.p12', '*.pfx')


def sanitize(text, assignments=True):
    cleaned = SECRET_VALUE.sub('[민감 정보 제외]', text or '')
    return SECRET_ASSIGNMENT.sub('[민감 정보 제외]', cleaned) if assignments else cleaned


def secret_path(path):
    return any(fnmatch.fnmatch(part.lower(), pat) for part in PurePosixPath(path).parts for pat in EXCLUDED)


def is_test(path):
    return bool(re.search(r'(^|/)(tests?|__tests__)(/|$)|(^|/)(test_[^/]+|[^/]+[._](test|spec)\.[^/]+)$|_test\.', path, re.I))


def changed_lines(text):
    """문맥·hunk 헤더·파일 헤더를 제외한 추가/삭제 줄만 반환한다."""
    return '\n'.join(line[1:] for line in text.splitlines()
                     if line.startswith(('+', '-')) and not line.startswith(('+++ ', '--- ')))


def candidates(path, text, tests):
    location = PurePosixPath(path.lower())
    parts = location.parts
    name = location.name
    changed = changed_lines(text)
    # camelCase 호출도 동일한 식별자로 검사하되 일반 단어 언급은 근거로 쓰지 않는다.
    code = re.sub(r'([a-z])([A-Z])', r'\1_\2', changed).lower()
    auth_names = (r'auth|authn|authz|authenticate|authenticated|authentication|'
                  r'authorize|authorized|authorization|is_authenticated|'
                  r'check_permission|check_permissions|verify_token|login|logout|oauth|jwt')
    auth_path = any(re.fullmatch(
        r'auth|authn|authz|authentication|authorization|authenticate|authenticated|'
        r'authorize|is_authenticated|check_permission|permissions?|sessions?|login|logout|oauth|acl',
        re.sub(r'([a-z])([A-Z])', r'\1_\2', part).lower().split('.')[0]) for part in PurePosixPath(path).parts)
    middleware_auth = ('middleware' in parts and name.startswith('auth') or
                       bool(re.fullmatch(r'auth[^/]*middleware\.[^.]+', name)))
    rules = {
        'auth': auth_path or middleware_auth or bool(re.search(
            r'\b(?:' + auth_names + r')\s*(?:\(|\.\s*[a-z_]\w*\s*\()', code)),
        'payments': any(part in ('payments', 'billing', 'checkout', 'stripe') or
                        part.startswith('stripe.') for part in parts) or bool(re.search(
                            r'\b(?:stripe|braintree|paypal|adyen|razorpay)(?:\.[a-z_]\w*)+\s*\(', code)),
        'db_schema_migration': any(part in ('migrations', 'migrate') for part in parts) or
            'alembic/versions/' in path.lower() or name.endswith('.sql') or
            path.lower().endswith(('prisma/schema.prisma', 'db/schema.rb')) or bool(re.search(
                r'\b(?:(?:create|alter|drop)\s+table|(?:add|drop)\s+column|create\s+(?:unique\s+)?index)\b', code)),
        'config': 'config' in parts or name.endswith(('.toml', '.ini', '.yaml', '.yml')) or
            name.startswith('.env') or name == 'settings.py',
        'ci_workflow': path.lower().startswith('.github/workflows/'),
        'dependency': bool(re.search(r'(^|/)(package(-lock)?\.json|(?:[^/]+[.-])?lock|requirements[^/]*|pyproject\.toml|go\.(?:mod|sum)|pnpm-lock\.yaml|gemfile|cargo\.toml)$', path.lower())),
        'secrets': secret_path(path) or 'secrets' in parts or bool(SECRET_VALUE.search(changed)) or
            bool(re.search(r"\b(?:[a-z0-9_]*(?:password|api_?key|secret|token)[a-z0-9_]*)[\"']?\s*[:=]\s*([\"'])[^\"'\n]+\1", code)),
    }
    flags = [flag for flag, matches in rules.items() if matches]
    if not is_test(path) and PurePosixPath(path).suffix in ('.py', '.js', '.ts', '.tsx', '.go', '.rs', '.java', '.rb', '.c', '.cpp'):
        stem = PurePosixPath(path).stem
        if not any(stem == re.sub(r'^(?:test_|spec_)|(?:[._](?:test|spec)|_test)$', '', PurePosixPath(t).stem) for t in tests):
            flags.append('logic_without_tests')
    return flags


def parse_diff(diff, files=()):
    # 파일 목록에 있지만 patch가 없는 변경도 합성 단위로 보존한다.
    sections = {}
    section_sizes = {}
    for block in re.split(r'(?m)^diff --git ', diff)[1:]:
        header, _, body = block.partition('\n')
        match = re.search(r'(?m)^\+\+\+ (.+)$', body)
        raw = match.group(1) if match and match.group(1) != '/dev/null' else None
        if raw is None:
            match = re.search(r'(?m)^--- (.+)$', body)
            raw = match.group(1) if match else None
        if raw is None:
            # rename/binary의 경우 git 헤더의 목적지 경로를 사용한다.
            rename = re.search(r'(?m)^rename to (.+)$', body)
            raw = rename.group(1) if rename else header.split(' b/', 1)[-1]
            if raw.startswith('\"a/'):
                quoted = re.findall(r'\"(?:[^\"\\]|\\.)*\"', header)
                raw = quoted[-1] if quoted else raw
        if raw.startswith('"'):
            try:
                decoded = ast.literal_eval(raw)
                try:
                    raw = decoded.encode('latin1').decode('utf-8')
                except UnicodeError:
                    raw = decoded
            except (ValueError, SyntaxError):
                pass
        path = raw[2:] if raw.startswith(('a/', 'b/')) else raw
        sections[path] = body
        section_sizes[path] = len(('diff --git ' + block).encode())
    paths = sorted(set(sections) | {f['filename'] for f in files})
    tests = [p for p in paths if is_test(p) and not secret_path(p)]
    units = []
    for path in paths:
        body = sections.get(path, '')
        matches = list(re.finditer(r'(?m)^@@ -(\d+(?:,\d+)?) \+(\d+(?:,\d+)?) @@[^\n]*', body))
        parts = [(body[m.start():matches[i+1].start() if i+1 < len(matches) else len(body)], m.group(1), m.group(2)) for i, m in enumerate(matches)] or [(body or '파일 목록만 제공됨', None, None)]
        old_path = next((f.get('previous_filename') for f in files if f['filename'] == path), None)
        rename_from = re.search(r'(?m)^rename from (.+)$', body)
        if old_path is None and rename_from:
            old_path = rename_from.group(1).strip('\"')
        overhead = section_sizes.get(path, 0) - sum(len(p[0].encode()) for p in parts)
        for index, (text, old, new) in enumerate(parts):
            excluded = secret_path(path) or bool(old_path and secret_path(old_path))
            flags = candidates(path, text, tests)
            cleaned = sanitize(text)
            redacted = not excluded and cleaned != text
            if excluded and 'secrets' not in flags:
                flags.append('secrets')
            units.append(dict(change_id=f'h{len(units)+1}', file_path=sanitize(path), kind='hunk' if old else 'synthetic',
                              old_range=old, new_range=new, binary='GIT binary patch' in body or 'Binary files ' in body,
                              diff='' if excluded else cleaned, raw_bytes=len(text.encode()) + max(0, overhead) * (index == 0), excluded=excluded,
                              exclusion_reason='민감 경로' if excluded else None,
                              redacted=redacted, redaction_reason='일부 민감 정보 가림' if redacted else None,
                              candidates=flags))
    return units, tests
