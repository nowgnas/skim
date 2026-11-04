"""로컬 설정과 읽기 전용 PR 수집 CLI."""
from contextlib import ExitStack, nullcontext
import argparse
import re
import sqlite3
import sys
from . import __version__, maintenance
from .config import Config
from .db import SCOPES, analysis_allowed, analysis_scope, connect
from .hosts import hostname, registered


OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"


def repository(value):
    if not re.fullmatch(OWNER_PATTERN + r'/[A-Za-z0-9_.-]+', value):
        raise argparse.ArgumentTypeError('owner/repo 형식이 필요합니다')
    owner, name = value.split('/')
    if name in ('.', '..'):
        raise argparse.ArgumentTypeError('잘못된 저장소 이름입니다')
    return owner, name


def parser():
    root = argparse.ArgumentParser(prog='skim', description='로컬 PR 검토 도우미')
    root.add_argument('--version', action='version', version=f'skim {__version__}')
    commands = root.add_subparsers(dest='command', required=True)
    repo = commands.add_parser('repo', help='추적 저장소 관리')
    actions = repo.add_subparsers(dest='action', required=True)
    add = actions.add_parser('add')
    add.add_argument('repository', type=repository)
    add.add_argument('--host', type=hostname, default='github.com')
    add.add_argument('--source', choices=('owned', 'managed', 'responsible'), default='responsible')
    listing = actions.add_parser('list')
    listing.add_argument('--candidates', action='store_true', help='소유·관리 권한으로 발견한 미등록 후보')
    remove = actions.add_parser('remove')
    remove.add_argument('repository', type=repository)
    remove.add_argument('--host', type=hostname, default='github.com')
    analysis = commands.add_parser('analysis', help='모델 서비스 코드 전송 허용 관리')
    policies = analysis.add_subparsers(dest='action', required=True)
    scope = policies.add_parser('scope', help='호스트 분석 범위: none(안 함), all(모든 저장소), selected(선택한 저장소만)')
    scope.add_argument('scope', choices=SCOPES)
    scope.add_argument('--host', type=hostname, default='github.com')
    for action, text in (('allow', '선택한 저장소에 추가'), ('deny', '선택에서 제외'), ('reset', 'deny와 같음')):
        p = policies.add_parser(action, help=text)
        p.add_argument('--host', type=hostname, default='github.com')
        p.add_argument('repository', type=repository)
    commands.add_parser('collect', help='gh 로그인으로 PR 읽기 전용 수집')
    commands.add_parser('open', help='실행 중인 웹 서버를 브라우저에서 열기')
    app = commands.add_parser('app', help='macOS 앱과 로그인 시 자동 시작 설정')
    app_actions = app.add_mutually_exclusive_group()
    app_actions.add_argument('--uninstall', action='store_true')
    app_actions.add_argument('--restart', action='store_true', help='서버를 재시작하고 브라우저 열기 (진행 중인 작업이 중단됩니다)')
    uninstall = commands.add_parser('uninstall', help='skim 프로그램 제거 (사용자 데이터는 기본 보존)')
    uninstall.add_argument('--yes', action='store_true', help='확인 질문 없이 제거 (비대화형 실행에 필요)')
    uninstall.add_argument('--purge-data', action='store_true', help='설정, DB, 캐시, Codex 전용 홈도 삭제')
    update = commands.add_parser('update', help='새 정식 릴리스 확인 후 적용')
    update.add_argument('--check', action='store_true', help='사용 가능한 정식 릴리스만 확인')
    update.add_argument('--yes', action='store_true', help='적용 확인 질문 없이 진행')
    analyze = commands.add_parser('analyze', help='허용한 저장소의 최신 스냅샷 분석')
    analyze.add_argument('--snapshot', type=int)
    analyze.add_argument('--include-drafts', action='store_true', help='draft도 수동 분석')
    analyze.add_argument('--automatic', action='store_true', help='주기 실행용: 자동 분석 일일 호출 제한 적용')
    web = commands.add_parser('web', help='로컬 PR 대시보드')
    web.add_argument('--port', type=int, default=0)
    web.add_argument('--no-browser', action='store_true')
    web.add_argument('--offline', action='store_true', help='수집·분석 없이 저장된 데이터만 표시')
    doctor = commands.add_parser('doctor', help='인증·격리·로컬 권한 점검')
    doctor.add_argument('--probe', action='store_true', help='코드 없이 실제 모델 격리 검사 실행')
    codex_home = commands.add_parser('codex-home', help='분석 전용 인증 링크 관리')
    codex_init = codex_home.add_subparsers(dest='action', required=True).add_parser('init')
    codex_init.add_argument('--reset', action='store_true', help='전용 홈 항목을 제거하고 인증 링크 재생성')
    return root


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv or ['web'])
    barrier = ExitStack()
    try:
        if args.command == 'update':
            from .update_apply import run
            return run(check=args.check, assume_yes=args.yes)
        if args.command == 'uninstall':
            from .uninstall import run
            return run(purge_data=args.purge_data, yes=args.yes)
        if args.command == 'doctor':
            from .doctor import run, paths
            return run(paths(), probe=args.probe)
        config = Config.load()
        if args.command != 'open':
            # 업데이트 전환 중에는 새 명령을 막고, 이미 실행 중인 명령은 끝날 때까지 업데이트가 기다린다.
            barrier.enter_context(maintenance.guard(config.data_dir))
        if args.command == 'app':
            from .macos_bundle import install_app, uninstall_app
            return uninstall_app(config) if args.uninstall else install_app(config, restart=True) if args.restart else install_app(config)
        if args.command == 'open':
            from .web import open_running
            return open_running(config)
        if args.command == 'codex-home':
            from .analyze.codex import CodexAdapter
            CodexAdapter(data_dir=config.data_dir).init_home(reset=args.reset)
            print('분석 전용 Codex 인증 링크를 만들었습니다.')
            return 0
        if args.command == 'web':
            if not 0 <= args.port <= 65535:
                raise ValueError('포트는 0~65535 범위여야 합니다')
            from .web import serve
            return serve(config, args.port, not args.no_browser, args.offline)
        conn = connect(config)
        try:
            if args.command == 'analyze':
                from .analyze import Analyzer
                results = Analyzer(conn, config).run(args.snapshot, args.include_drafts, budgeted=args.automatic)
                for result in results:
                    print(f"스냅샷 {result['snapshot_id']}: {result['status']}")
                    if result.get('error'):
                        print(result['error'], file=sys.stderr)
                if not results:
                    print('분석 가능한 허용 스냅샷이 없습니다.')
                return int(any(r['status'] in ('failed', 'blocked', 'deferred') for r in results))
            from .collect import Collector
            if args.command == 'collect':
                count = Collector(conn, config).collect()
                print(f'PR {count}개를 수집했습니다.')
                return 0
            collector = Collector(conn, config)
            removing = args.command == 'repo' and args.action == 'remove'
            with collector.lock() if removing else nullcontext():
                with conn:
                    if hasattr(args, 'host'):
                        args.host = registered(conn, args.host)
                    if args.command == 'analysis' and (args.action == 'allow' or getattr(args, 'scope', 'none') != 'none'):
                        print('허용된 코드가 선택한 LLM CLI의 모델 서비스로 전송될 수 있습니다.', flush=True)
                    if args.command == 'repo':
                        if args.action == 'list':
                            print('호스트\t저장소\t출처\t활성\t분석 범위\t선택됨\t분석 허용')
                            condition = "tracked=0 AND source IN ('owned','managed')" if args.candidates else 'tracked=1'
                            for row in conn.execute('SELECT * FROM repositories WHERE ' + condition + ' ORDER BY owner,name'):
                                scope = analysis_scope(conn, row['host'])
                                # 선택은 범위가 selected일 때만 의미가 있으므로 다른 범위에서는 '-'로 보인다.
                                chosen = int(row['analysis_policy'] == 'allow') if scope == 'selected' else '-'
                                print(f"{row['host']}\t{row['owner']}/{row['name']}\t{row['source']}\t{row['enabled']}\t"
                                      f"{scope}\t{chosen}\t{int(analysis_allowed(conn, row['id']))}")
                        elif args.action == 'add':
                            from .repos import add_repository
                            add_repository(conn, args.repository, args.source, args.host)
                            print('저장소를 등록했습니다. 분석은 별도 허용이 필요합니다.')
                        else:
                            from .repos import remove_repository
                            remove_repository(conn, args.repository, args.host)
                            print('저장소 추적을 해제했습니다.')
                    elif args.action == 'scope':
                        conn.execute('UPDATE github_hosts SET analysis_scope=? WHERE host=?', (args.scope, args.host))
                        if args.scope != 'selected':
                            conn.execute("UPDATE repositories SET analysis_policy='inherit' WHERE host=?", (args.host,))
                        print(f'{args.host} 분석 범위를 {args.scope}로 변경했습니다.')
                    else:
                        current = analysis_scope(conn, args.host)
                        if current == 'all':
                            raise ValueError('모든 저장소가 허용된 호스트입니다. 개별로 정하려면 analysis scope selected를 먼저 실행하세요')
                        result = conn.execute("UPDATE repositories SET analysis_policy=? WHERE host=? AND owner=? AND name=?",
                                              ('allow' if args.action == 'allow' else 'inherit', args.host, *args.repository))
                        if not result.rowcount:
                            raise ValueError('등록되지 않은 저장소입니다')
                        if args.action == 'allow' and current == 'none':
                            # 저장소를 고르는 것은 '선택한 저장소만' 범위를 켜는 것과 같다.
                            conn.execute("UPDATE github_hosts SET analysis_scope='selected' WHERE host=?", (args.host,))
                        print('선택한 저장소 목록을 변경했습니다.')
                if removing:
                    collector.clean_cache()
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error, maintenance.UpdateInProgress) as exc:
        print(f'오류: {exc}', file=sys.stderr)
        return 1
    finally:
        barrier.close()
    return 0
