"""네트워크를 사용하지 않는 초기 CLI."""
import argparse
import re
import sqlite3
import sys
from .config import Config
from .db import analysis_allowed, connect


def repository(value):
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+', value):
        raise argparse.ArgumentTypeError('owner/repo 형식이 필요합니다')
    owner, name = value.split('/')
    if name in ('.', '..'):
        raise argparse.ArgumentTypeError('잘못된 저장소 이름입니다')
    return owner, name


def parser():
    root = argparse.ArgumentParser(prog='skim', description='로컬 PR 검토 도우미')
    commands = root.add_subparsers(dest='command', required=True)
    repo = commands.add_parser('repo', help='추적 저장소 관리')
    actions = repo.add_subparsers(dest='action', required=True)
    add = actions.add_parser('add')
    add.add_argument('repository', type=repository)
    add.add_argument('--source', choices=('owned', 'managed', 'responsible'), default='responsible')
    actions.add_parser('list')
    remove = actions.add_parser('remove')
    remove.add_argument('repository', type=repository)
    analysis = commands.add_parser('analysis', help='모델 서비스 코드 전송 허용 관리')
    policies = analysis.add_subparsers(dest='action', required=True)
    for action in ('allow', 'deny', 'reset'):
        p = policies.add_parser(action)
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument('repository', type=repository, nargs='?')
        group.add_argument('--org')
    for command in ('collect', 'analyze', 'web', 'doctor'):
        commands.add_parser(command, help='후속 PR에서 구현 예정')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command in ('collect', 'analyze', 'web', 'doctor'):
        print(f'{args.command}: 아직 구현되지 않았습니다.', file=sys.stderr)
        return 2
    try:
        config = Config.load()
        conn = connect(config)
        try:
            with conn:
                if args.command == 'repo':
                    if args.action == 'list':
                        print('저장소\t출처\t활성\t분석 정책\t분석 허용')
                        for row in conn.execute('SELECT * FROM repositories ORDER BY owner,name'):
                            print(f"{row['owner']}/{row['name']}\t{row['source']}\t{row['enabled']}\t"
                                  f"{row['analysis_policy']}\t{int(analysis_allowed(conn, row['id']))}")
                    elif args.action == 'add':
                        conn.execute('INSERT INTO repositories(owner,name,source) VALUES(?,?,?) '
                                     'ON CONFLICT(owner,name) DO UPDATE SET source=excluded.source,enabled=1',
                                     (*args.repository, args.source))
                        print('저장소를 등록했습니다. 분석은 별도 허용이 필요합니다.')
                    else:
                        result = conn.execute('DELETE FROM repositories WHERE owner=? AND name=?', args.repository)
                        if not result.rowcount:
                            raise ValueError('등록되지 않은 저장소입니다')
                        print('저장소를 삭제했습니다.')
                elif args.org is not None:
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', args.org):
                        raise ValueError('잘못된 조직 이름입니다')
                    if args.action == 'reset':
                        conn.execute('DELETE FROM organization_policies WHERE owner=?', (args.org,))
                    else:
                        conn.execute('INSERT INTO organization_policies VALUES(?,?) '
                                     'ON CONFLICT(owner) DO UPDATE SET policy=excluded.policy', (args.org, args.action))
                    print('조직 분석 정책을 변경했습니다.')
                else:
                    policy = 'inherit' if args.action == 'reset' else args.action
                    result = conn.execute('UPDATE repositories SET analysis_policy=? WHERE owner=? AND name=?',
                                          (policy, *args.repository))
                    if not result.rowcount:
                        raise ValueError('등록되지 않은 저장소입니다')
                    print('저장소 분석 정책을 변경했습니다.')
                if args.command == 'analysis' and args.action == 'allow':
                    print('허용된 코드가 선택한 LLM CLI의 모델 서비스로 전송될 수 있습니다.')
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f'오류: {exc}', file=sys.stderr)
        return 1
    return 0
