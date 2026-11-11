"""diff 단위, 민감 정보 제외와 독립적인 위험 후보 계산."""
import fnmatch
import ast
import re
from functools import lru_cache
from pathlib import PurePosixPath

SANITIZATION_POLICY = 'redact-v4'

REDACTED = '[민감 정보 제외]'
# 값은 전부 가리되, 문자열 리터럴을 직접 대입한 줄이었다는 사실은 남긴다.
REDACTED_LITERAL = '[민감 정보 제외: 직접 쓴 문자열 값]'

# 모델 출력에서는 값 자체가 확인되는 토큰·개인 키만 가린다.
SECRET_VALUE = re.compile(
    r'-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)'
    r'|(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk_(?:live|test)_[A-Za-z0-9]+'
    r'|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]+|AIza[A-Za-z0-9_-]{30,})')
SECRET_ASSIGNMENT = re.compile(r"(?im:\b(?P<head>(?:api[_-]?key|[a-z0-9_]*(?:secret|password|token|private[_-]?key)[a-z0-9_]*|authorization)[\"']?\s*[=:])(?P<value>[^\n]*))")

# 이름과 값이 모두 정해진 사용량 설정만 예외로 둔다. 비밀값처럼 보이는 다른 대입은 계속 가린다.
SAFE_COUNT_ASSIGNMENT = re.compile(r"""(?i)^(?:max_tokens|token_count|total_tokens|input_tokens|output_tokens|prompt_tokens|completion_tokens)["']?\s*[:=]\s*(?P<value>[0-9]{1,10}|true|false)\s*[,;]?\s*$""")

# A value that is one whole quoted string. f-strings and concatenations do not match.
LITERAL_VALUE = re.compile(r"""\s*[bBrRuU]{0,2}(?P<quote>["'])(?P<body>[^"'\\\n]+)(?P=quote)\s*[,;]?\s*""")
# Quoted text that only refers to a value defined elsewhere (shell, template, CI placeholders).
REFERENCE_BODY = re.compile(r'\$\{|\{\{|#\{|%\(|^\$|^<[^>]*>$|^%\w+%$|^\s*$')
# The name is kept only when nothing but a declaration prefix precedes it on the line.
# Anywhere else the matched "name" may be the tail of a value (e.g. a base64 run ending in '=').
NAME_PREFIX = re.compile(r"""[+\- ]?\s*(?:-\s+)?(?:(?:export|const|let|var|final|static|public|private|val)\s+)*(?:(?:self|this)\.)?["']?""")


EXCLUDED = ('.env', '.env.*', '*.pem', '*.key', '*credentials*', 'id_rsa*', 'id_ed25519*', '*.p12', '*.pfx')


def sanitize(text, assignments=True):
    cleaned = SECRET_VALUE.sub(REDACTED, text or '')
    if not assignments:
        return cleaned
    def redact(match):
        safe = SAFE_COUNT_ASSIGNMENT.fullmatch(match.group())
        if safe and (not safe['value'].isdigit() or int(safe['value']) <= 2147483647):
            return match.group()
        if match['value'].strip() == REDACTED_LITERAL:
            # Already cleaned: cleaning twice must not erase the name again.
            return match.group()
        literal = LITERAL_VALUE.fullmatch(match['value'])
        if not literal or REFERENCE_BODY.search(literal['body']):
            return REDACTED
        line_start = match.string.rfind('\n', 0, match.start()) + 1
        if len(match['head']) <= 80 and NAME_PREFIX.fullmatch(match.string, line_start, match.start()):
            return match['head'] + ' ' + REDACTED_LITERAL
        return REDACTED_LITERAL
    return SECRET_ASSIGNMENT.sub(redact, cleaned)


def secret_path(path):
    return any(fnmatch.fnmatch(part.lower(), pat) for part in PurePosixPath(path).parts for pat in EXCLUDED)


def is_test(path):
    return bool(re.search(r'(^|/)(tests?|__tests__)(/|$)|(^|/)(test_[^/]+|[^/]+[._](test|spec)\.[^/]+)$|_test\.', path, re.I))


def changed_spans(text):
    """헤더를 제외한 변경 내용의 원문 오프셋을 돌려준다."""
    offset = 0
    in_hunk = False
    for line in text.splitlines(keepends=True):
        if line.startswith('@@ '):
            in_hunk = True
        elif line.startswith(('+', '-')):
            if in_hunk or not line.startswith(('+++ ', '--- ')):
                yield offset + 1, offset + len(line.rstrip('\r\n'))
        offset += len(line)


def changed_lines(text):
    """문맥과 헤더를 제외하되 hunk 안의 헤더 모양 변경 줄은 보존한다."""
    return '\n'.join(text[start:end] for start, end in changed_spans(text))


def changed_secrets(text):
    """개인 키 전체의 가림 범위와 변경 줄이 겹치는지도 검사한다."""
    spans = list(changed_spans(text))
    if any(start < match.end() and match.start() < end
           for match in SECRET_VALUE.finditer(text) for start, end in spans):
        return True
    changed = changed_lines(text)
    return sanitize(changed) != changed


def payment_candidate(parts, code):
    return any(re.fullmatch(r'checkout|(?:payments?|refunds?|charges?|billing|stripe)(?:_.+)?', part)
               for part in parts) or bool(re.search(
        r'\b(?:stripe|braintree|paypal|adyen|razorpay)(?:\.[a-z_]\w*)+\s*\(', code))


def schema_candidate(parts, path, name, code):
    return (any(part in ('migration', 'migrations', 'migrate') for part in parts)
            or 'alembic/versions/' in path.lower()
            or name == 'schema.prisma' or path.lower().endswith('db/schema.rb')
            or bool(re.search(r'\b(?:(?:create|alter|drop)\s+table|(?:add|drop)\s+column|create\s+(?:unique\s+)?index)\b', code)))


SQL_LITERAL = re.compile(r"--[^\n]*|/\*[\s\S]*?(?:\*/|\Z)|'(?:''|[^'])*(?:'|\Z)|\$(?P<tag>\w*)\$[\s\S]*?(?:\$(?P=tag)\$|\Z)")
DDL = re.compile(r'(?im)^\s*(?:(?:create|alter|drop)\s+table|(?:add|drop)\s+column|create\s+(?:unique\s+)?index)\b')


def changed_ddl(text):
    for hunk in re.split(r'(?m)^@@ .*$', text):
        for sign in ('+', '-'):
            lines = [line for line in hunk.splitlines() if line.startswith((' ', sign))]
            source = '\n'.join(line[1:] for line in lines)
            masked = SQL_LITERAL.sub(lambda match: re.sub(r'[^\n]', ' ', match.group()), source)
            if any(line.startswith(sign) and DDL.search(code)
                   for line, code in zip(lines, masked.split('\n'))):
                return True
    return False


def config_candidate(parts, path, name):
    return (any(part in ('config', 'configs', 'settings') for part in parts)
            or name.endswith(('.toml', '.ini', '.conf', '.tf')) or name.startswith('.env')
            or name in ('compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml',
                        'dockerfile', 'values.yaml', 'values.yml', 'tsconfig.json',
                        'application.yml', 'application.properties')
            or name.startswith('dockerfile.')
            or path.lower() in ('.github/dependabot.yml', '.github/dependabot.yaml'))


def auth_candidate(parts, name, code):
    auth_names = (r'auth|authn|authz|authenticate|authenticated|authentication|'
                  r'authorize|authorized|authorization|is_authenticated|'
                  r'check_permission|check_permissions|verify_token|login|logout|oauth|jwt')
    auth_identifier = r'(?:[a-z0-9]+_)*(?:auth|oauth|permission|permissions|login|jwt)(?:_[a-z0-9]+)*'
    auth_path = any(re.fullmatch(
        r'auth|authn|authz|authentication|authorization|authenticate|authenticated|'
        r'authorize|is_authenticated|check_permission|permissions?|sessions?|login|logout|oauth|acl'
        r'|(?:auth|oauth|jwt|permissions?|login)_.+', part) for part in parts)
    middleware_auth = ('middleware' in parts and bool(re.fullmatch(r'auth(?:_.+)?\.[^.]+', name)) or
                       bool(re.fullmatch(r'auth_?middleware\.[^.]+', name)))
    return auth_path or middleware_auth or bool(re.search(
            r'\b(?:' + auth_names + r')\s*(?:\(|\.\s*[a-z_]\w*\s*\()', code)) or bool(re.search(
                r'\b' + auth_identifier + r'\s*\(|@' + auth_identifier + r'\b', code))


# 흔한 모듈명은 식별자만으로 대응 테스트를 추정하지 않는다.
COMMON_STEMS = {'__init__', 'index', 'main', 'config', 'utils', 'run', 'app', 'common', 'helpers', 'settings'}


@lru_cache(maxsize=512)
def test_reference_patterns(stem):
    """여러 hunk에서 같은 모듈의 참조 정규식을 재사용한다."""
    name = re.escape(stem)
    imports = re.compile(
        r'^\s*(?:from\s+[\w.]*\b' + name + r'\s+import\b'
        r'|from\s+[\w.]+\s+import\s+\(?\s*(?:\w+\s*(?:as\s+\w+\s*)?,\s*)*'
        + name + r'(?=\s*(?:as\b|[,)#]|$))'
        r'|import\s+(?:[\w.]+\s*(?:as\s+\w+\s*)?,\s*)*'
        + r'(?:\w+\.)*' + name + r'(?=\s|\.|,|$))')
    test_name = re.compile(r'^\s*(?:async\s+)?def\s+test_(?:\w+_)?'
                           + name + r'(?:_\w*)?\s*\(')
    return imports, test_name


def test_references(stem, lines):
    imports, test_name = test_reference_patterns(stem)
    return (any(imports.search(line) for line in lines)
            or (len(stem) >= 4 and any(test_name.search(line) for line in lines)))


def added_test_lines(text):
    """새 쪽 문맥·추가 줄의 문자열과 주석을 가리고 추가 줄만 반환한다.

    들여쓰기와 괄호는 해석하지 않는다. hunk가 여러 줄 문자열 중간에서
    시작하면 문자열 내부인지 판별할 수 없다. 문자열 상태는 hunk마다 초기화한다.
    """
    for hunk in re.split(r'(?m)^@@ .*$', text)[1:]:
        quote = None
        for line in hunk.splitlines():
            if not line.startswith((' ', '+')):
                continue
            source = line[1:]
            masked = list(source)
            index = 0
            continued = False
            while index < len(source):
                if quote:
                    if source[index] == '\\':
                        end = min(index + 2, len(source))
                        continued = end == index + 1
                    elif source.startswith(quote, index):
                        end = index + len(quote)
                        quote = None
                    else:
                        end = index + 1
                elif source[index] == '#':
                    end = len(source)
                elif source[index] in ('"', "'"):
                    char = source[index]
                    quote = char * (3 if source.startswith(char * 3, index) else 1)
                    end = index + len(quote)
                else:
                    index += 1
                    continue
                masked[index:end] = ' ' * (end - index)
                index = end
            # Only escaped newlines can continue ordinary quoted strings.
            if quote and len(quote) == 1 and not continued:
                quote = None
            if line.startswith('+'):
                yield ''.join(masked)


DEFINITION = re.compile(
    r'\b(?:def|function|func|fn|fun)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[(<]'
    r'|\b(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|\w+\s*=>)')
CALL = re.compile(r'(?:(\.)\s*|(?<![\w$.]))([A-Za-z_]\w*)\s*\(')
# 여러 모듈에 같은 이름이 흔해 이름만으로 호출 대상을 특정할 수 없는 함수다.
COMMON_CALLS = frozenset(
    'main init setup teardown test run start stop open close read write load save dump send recv '
    'get set put post delete patch head add remove update create build make find list show print format '
    'parse render validate check handle process execute call apply reset clear copy merge filter sort '
    'next iter enter exit wrapper inner callback handler helper decorator'.split())
CALLS_CHANGED_LIMIT = 3
CALLEE_EXCERPT_BYTES = 240


def _definition_names(source):
    return [first or second for first, second in DEFINITION.findall(source)]


def definitions(text):
    """hunk에서 정의 줄이 보이는 이름과, 그중 변경 줄이 정의 범위에 있는 이름을 돌려준다.

    들여쓰기는 해석하지 않는다. 정의 줄 자체가 추가·삭제됐거나, 문맥·hunk 머리의 정의 뒤에
    다음 정의가 나오기 전 변경 줄이 있으면 그 정의가 바뀐 것으로 본다.
    """
    seen, changed, pending = set(), [], None
    for line in text.splitlines():
        if line.startswith('@@'):
            sign, source = ' ', line.partition('@@')[2].partition('@@')[2]
        elif line[:1] in ('+', '-', ' '):
            sign, source = line[0], line[1:]
        else:
            continue
        names = _definition_names(source)
        seen.update(names)
        if names and sign == ' ':
            pending = names[-1]
            continue
        if sign == ' ':
            continue
        for name in names or ([pending] if pending else []):
            if name not in changed:
                changed.append(name)
        pending = None
    return seen, changed


def _specific_call(name):
    return (len(name) >= 4 and not name.startswith(('__', 'test')) and name.lower() not in COMMON_CALLS)


def _multiword(name):
    return '_' in name.strip('_') or bool(re.search(r'[a-z0-9][A-Z]', name))


def _prose(path):
    return path.lower().endswith(('.md', '.rst', '.txt'))


def link_calls(units):
    """추가 줄이 호출하는 함수의 정의가 같은 PR의 다른 변경 단위에 있으면 그 단위를 가리킨다.

    이름이 정확히 같은 경우만 연결한다. 흔한 이름과 여러 파일에서 정의가 바뀐 이름은
    대상을 특정할 수 없어 연결하지 않는다. 수신 객체를 알 수 없는 obj.name() 호출은
    여러 단어로 된 이름만 연결한다. 테스트와 문서는 정의와 호출 어느 쪽으로도 쓰지 않는다:
    테스트의 가짜 메서드가 제품 코드의 호출 대상으로 잡히고, 테스트의 호출은 동작 전파가 아니다.
    중요도를 정하지 않는 문맥 신호다.
    """
    local, owners = {}, {}
    for unit in units:
        unit['calls_changed'] = []
        if (unit['excluded'] or unit['kind'] != 'hunk' or is_test(unit['file_path'])
                or _prose(unit['file_path'])):
            continue
        local[unit['change_id']], changed = definitions(unit['diff'])
        for name in changed:
            owners.setdefault(name, []).append(unit)
    targets = {name: found[0] for name, found in owners.items()
               if _specific_call(name) and len({unit['file_path'] for unit in found}) == 1}
    if not targets:
        return
    for unit in units:
        if unit['change_id'] not in local:
            continue
        names = []
        for line in added_test_lines(unit['diff']):
            for dotted, name in CALL.findall(line):
                target = targets.get(name)
                if (target is not None and target is not unit and name not in names
                        and name not in local[unit['change_id']]
                        and (not dotted or _multiword(name))):
                    names.append(name)
        for name in names[:CALLS_CHANGED_LIMIT]:
            target = targets[name]
            # Signs stay so the excerpt reads as before/after rather than two unrelated lines.
            changed = '\n'.join(line for line in target['diff'].splitlines() if line.startswith(('+', '-')))
            excerpt = changed.encode('utf-8')[:CALLEE_EXCERPT_BYTES].decode('utf-8', errors='ignore')
            unit['calls_changed'].append(dict(
                name=name, change_id=target['change_id'], file_path=target['file_path'],
                excerpt=excerpt, excerpt_truncated=excerpt != changed))


def signals(path, text, tests, test_diffs=None, *, test_lines=None):
    location = PurePosixPath(path.lower())
    # 경로 조각은 camelCase를 분리하고 확장자를 뗀 이름으로 통일한다.
    parts = tuple(re.sub(r'([a-z])([A-Z])', r'\1_\2', part).lower().split('.')[0]
                  for part in PurePosixPath(path).parts)
    name = location.name
    changed = changed_lines(text)
    # camelCase 호출도 동일한 식별자로 검사하되 일반 단어 언급은 근거로 쓰지 않는다.
    code = re.sub(r'([a-z])([A-Z])', r'\1_\2', changed).lower()
    rules = {
        'config': config_candidate(parts, path, name),
        'install_script': (name in ('setup.sh', 'install.sh', 'bootstrap.sh')
                           or (name.endswith('.sh') and (len(location.parts) == 1
                               or (location.parts[0] == 'scripts' and name.startswith(('install', 'setup')))))
                           or (name == 'setup.py' and (len(location.parts) == 1 or location.parts[0] == 'lib'))
                           or location.parts[0] == 'bin'),
        'ci_workflow': path.lower().startswith('.github/workflows/'),
        'dependency': bool(re.search(r'(^|/)(package(-lock)?\.json|(?:[^/]+[.-])?lock|requirements[^/]*|pyproject\.toml|go\.(?:mod|sum)|pnpm-lock\.yaml|gemfile|cargo\.toml)$', path.lower())),
    }
    location_hints = [flag for flag, matches in {
        'auth': auth_candidate(parts, name, ''),
        'payments': payment_candidate(parts, ''),
        'config': rules['config'],
        'secrets': 'secrets' in parts,
    }.items() if matches]
    schema_path = schema_candidate(parts, path, name, '')
    kind_signals = [flag for flag in ('install_script', 'ci_workflow', 'dependency') if rules[flag]]
    if schema_path:
        kind_signals.append('db_schema_migration')
    if secret_path(path):
        kind_signals.append('secrets')
    content_hits = [flag for flag, matches in {
        'auth': auth_candidate((), '', code),
        'payments': payment_candidate((), code),
        'db_schema_migration': schema_candidate((), '', '', code),
        'secrets': changed_secrets(text),
    }.items() if matches]
    hard_floors = []
    spans = list(changed_spans(text))
    if secret_path(path) or any(start < match.end() and match.start() < end
                               for match in SECRET_VALUE.finditer(text) for start, end in spans):
        hard_floors.append('secrets')
    # Only standalone SQL statements qualify; strings in source and tests are hints.
    if schema_path or (name.endswith('.sql') and not is_test(path) and changed_ddl(text)):
        hard_floors.append('db_schema_migration')
    test_gap = False
    if not is_test(path) and PurePosixPath(path).suffix in ('.py', '.js', '.ts', '.tsx', '.go', '.rs', '.java', '.rb', '.c', '.cpp'):
        stem = PurePosixPath(path).stem
        test_diffs = test_diffs if test_diffs is not None else {}
        if test_lines is None:
            test_lines = {t: tuple(added_test_lines(test_diffs.get(t, ''))) for t in tests}
        # 테스트 패키지 초기화 파일 자체는 대응 테스트의 근거가 아니다.
        if not any((PurePosixPath(t).stem != '__init__' and stem == re.sub(r'^(?:test_|spec_)|(?:[._](?:test|spec)|_test)$', '', PurePosixPath(t).stem))
                   or (stem.lower() not in COMMON_STEMS and test_references(stem, test_lines.get(t, ()))) for t in tests):
            test_gap = True
    return dict(location_hints=location_hints, kind_signals=kind_signals,
                content_hits=content_hits, hard_floors=hard_floors,
                test_gap=test_gap, mechanical_hints=[])


HASH_COMMENTS = ('#',)
SLASH_COMMENTS = ('//', '/*')
# Keyed by suffix because the same prefix is code elsewhere: '#' opens a Markdown
# heading or a C include, and '--' is an ordinary flag in a shell script.
COMMENT_PREFIXES = {
    **dict.fromkeys(('.py', '.rb', '.sh', '.yaml', '.yml', '.toml', '.tf'), HASH_COMMENTS),
    **dict.fromkeys(('.js', '.jsx', '.ts', '.tsx', '.java', '.kt', '.go', '.rs', '.c', '.h', '.cpp', '.cs', '.swift'),
                    SLASH_COMMENTS),
    '.php': HASH_COMMENTS + SLASH_COMMENTS,
    '.sql': ('--', '/*'),
}
# 주석 접두어로 시작하지만 실행에 영향을 주는 코드다. PHP 8 속성은 라우팅·권한 같은 동작을 바꾼다.
CODE_PREFIXES = {'.php': ('#[',)}


def comment_only(path, text):
    """변경 줄이 모두 빈 줄이거나 한 줄 전체가 주석일 때만 참이다.

    블록 주석 안의 ' * ' 줄은 *args 같은 코드와 구분할 수 없어 주석으로 보지 않는다.
    """
    suffix = PurePosixPath(path).suffix.lower()
    prefixes = COMMENT_PREFIXES.get(suffix)
    code = ('#!',) + CODE_PREFIXES.get(suffix, ())
    lines = [text[start:end].strip() for start, end in changed_spans(text)]
    if not prefixes or not lines:
        return False
    return all(not line or line == '*/' or _full_line_comment(line, prefixes, code) for line in lines)


def _full_line_comment(line, prefixes, code):
    if not line.startswith(prefixes) or line.startswith(code):
        return False
    if not line.startswith('/*'):
        return True
    # 블록 주석을 닫은 뒤에 남은 글자는 실행 코드일 수 있다.
    return not line.partition('*/')[2].strip()


SIGNAL_FIELDS = ('location_hints', 'kind_signals', 'content_hits', 'hard_floors', 'test_gap', 'mechanical_hints')


def call_context(unit, excerpt=True):
    """모델 입력에 싣는 교차 참조. 연결이 없으면 필드를 만들지 않는다."""
    calls = unit.get('calls_changed') or []
    if not excerpt:
        calls = [{key: call[key] for key in ('name', 'change_id', 'file_path')} for call in calls]
    return {'calls_changed': calls} if calls else {}


def candidate_view(signals):
    flags = {flag for key in SIGNAL_FIELDS if key not in ('test_gap', 'mechanical_hints') for flag in signals[key]}
    if signals['test_gap']:
        flags.add('logic_without_tests')
    return sorted(flags)


def candidates(path, text, tests, test_diffs=None, *, test_lines=None):
    return candidate_view(signals(path, text, tests, test_diffs, test_lines=test_lines))


def priority(unit):
    if unit.get('hard_floors'):
        return 0
    if unit.get('kind_signals') or unit.get('content_hits'):
        return 1
    if unit.get('mechanical_hints') and not unit.get('location_hints'):
        return 3
    return 2


def allocation_key(unit, focus=()):
    path = unit['file_path'].lower()
    supporting = is_test(path) or path.startswith(('docs/', 'doc/')) or (path.endswith(('.md', '.rst', '.txt'))
                and not any(unit.get(key) for key in ('kind_signals', 'hard_floors', 'content_hits')))
    return priority(unit), unit['change_id'] not in focus, supporting, int(unit['change_id'][1:])


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
    test_lines = {p: tuple(added_test_lines(sections.get(p, ''))) for p in tests}
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
            local = signals(path, text, tests, test_lines=test_lines)
            if old_path:
                previous = signals(old_path, '', tests, test_lines=test_lines)
                for key in ('location_hints', 'kind_signals', 'hard_floors'):
                    local[key] = sorted(set(local[key]) | set(previous[key]))
            if (not old and old_path and 'similarity index 100%' in body
                    and not re.search(r'(?m)^(?:old|new) mode ', body)):
                local['mechanical_hints'] = ['pure_rename']
            if old and comment_only(path, text):
                local['mechanical_hints'].append('comment_only')
            flags = candidate_view(local)
            cleaned = sanitize(text)
            redacted = not excluded and cleaned != text
            if excluded and 'secrets' not in flags:
                flags.append('secrets')
            units.append(dict(change_id=f'h{len(units)+1}', file_path=sanitize(path), kind='hunk' if old else 'synthetic',
                              old_range=old, new_range=new, binary='GIT binary patch' in body or 'Binary files ' in body,
                              diff='' if excluded else cleaned, raw_bytes=len(text.encode()) + max(0, overhead) * (index == 0), excluded=excluded,
                              exclusion_reason='민감 경로' if excluded else None,
                              redacted=redacted, redaction_reason='일부 민감 정보 가림' if redacted else None,
                              candidates=flags, **local))
    link_calls(units)
    return units, tests
