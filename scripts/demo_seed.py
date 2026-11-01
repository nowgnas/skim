#!/usr/bin/env python3
"""가짜 PR만 생성한다. SKIM_HOME=/tmp/skim-demo python3 scripts/demo_seed.py

확인: SKIM_HOME=/tmp/skim-demo bin/skim web --offline
실제 GitHub·LLM에 접근하지 않으며 비어 있는 DB에서만 실행한다.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.config import Config, private_file
from skim.db import connect
from skim.analyze import Analyzer


class DemoModel:
    """모델 서비스 대신 결정적인 fixture 응답을 제공한다."""
    provider, model, cli_version = 'fixture', 'demo', '1'

    def verify(self):
        pass

    def generate(self, request, schema):
        value = json.loads(request.split('\n', 1)[1])
        if value['stage'] == 'synthesis':
            return json.dumps({'intent': '변경 목적과 검토 우선순위를 보여 주는 가짜 PR입니다.'})
        items = []
        for unit in value['data']['units']:
            path = unit['file_path']
            level = 'high' if 'auth' in path else 'low' if path.endswith('.md') else 'mid'
            items.append(dict(change_id=unit['change_id'], importance=level,
                              reason='인증 경계를 확인하세요.' if level == 'high' else '변경 동작과 테스트를 확인하세요.',
                              risk_flags=['auth'] if level == 'high' else []))
        return json.dumps(dict(items=items, uncertainties=[]))


def seed(config):
    with closing(connect(config)) as conn:
        if conn.execute('SELECT 1 FROM repositories LIMIT 1').fetchone():
            raise ValueError('비어 있는 SKIM_HOME을 사용하세요')
        with conn:
            repo = conn.execute("INSERT INTO repositories(owner,name,analysis_policy,tracked) VALUES('demo','checkout','allow',1)").lastrowid
            for number, title, paths in [
                (1, '결제 인증 경계 보완', ['auth/session.py', 'service.py', 'README.md', '.env']),
                (2, '일반 API 응답 정리', ['handler.py', 'tests/test_handler.py']),
                (3, '사용 설명 보완', ['docs/guide.md']),
                (4, '수집 완료 · 분석 대기', ['.github/workflows/check.yml']),
                (5, '이전 SHA 분석 결과', ['auth/access.py'])]:
                head = 'head' + str(number)
                pr = conn.execute('INSERT INTO pull_requests(repo_id,number,title,author,url,base_sha,head_sha,updated_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                                  (repo, number, title, 'demo-author', 'https://github.com/demo/checkout/pull/' + str(number), 'base', head)).lastrowid
                conn.execute('INSERT INTO pr_sources VALUES(?,?)', (pr, 'review_request' if number % 2 else 'repository'))
                diff = ''.join(f'diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n' for p in paths)
                digest = hashlib.sha256(diff.encode()).hexdigest()
                path = config.data_dir / 'cache' / (digest + '.diff')
                private_file(path)
                path.write_text(diff)
                snapshot = conn.execute('INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path,coverage) VALUES(?,?,?,?,?,?,?)',
                                       (pr, 'base', head, digest, digest, str(path), json.dumps(dict(status='collected', files=[dict(filename=p) for p in paths])))).lastrowid
                conn.commit()
                if number != 4:
                    Analyzer(conn, config, adapter=DemoModel()).run(snapshot)
                if number == 5:
                    conn.execute("UPDATE pull_requests SET head_sha='new-head' WHERE id=?", (pr,))
        with conn:
            conn.execute("INSERT INTO github_hosts VALUES('github.corp.example')")
            conn.execute("INSERT INTO repositories(host,owner,name,tracked) VALUES('github.corp.example','demo-org','intranet',1)")
            conn.execute("INSERT INTO repositories(owner,name,tracked) VALUES('demo-org','library',1)")
            conn.execute("INSERT INTO repositories(owner,name,tracked,analysis_policy) VALUES('demo-org','private',1,'deny')")
            conn.execute("INSERT INTO organization_policies VALUES('demo-org','allow')")
        print('가짜 PR 5개 생성 완료. 같은 SKIM_HOME으로 bin/skim web --offline을 실행하세요.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', help='데모 전용 SKIM_HOME')
    args = parser.parse_args()
    if args.home:
        os.environ['SKIM_HOME'] = args.home
    if not os.environ.get('SKIM_HOME'):
        parser.error('데모 전용 --home 또는 SKIM_HOME이 필요합니다')
    seed(Config.load())


if __name__ == '__main__':
    main()
