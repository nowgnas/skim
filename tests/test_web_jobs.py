"""웹 백그라운드 작업은 가짜 HTTP·수집기·분석기·doctor로만 검증한다. 실제 gh·모델은 호출하지 않는다."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from test_web import WebTests
from skim import jobs
from skim.db import connect
from skim.jobs import Runner, INTERRUPTED
from skim.web_jobs import JOB_PATHS
from skim.web_settings import POST_PATHS, RepoLookup, WARNING


class JobTests(unittest.TestCase):
    setup_http = WebTests.setUp
    request = WebTests.request

    def setUp(self):
        self.setup_http()
        self.server.repo_lookup = RepoLookup(self.config, clock=lambda: self.now)
        # HTTP 처리 중 외부 실행이 일어나면 즉시 실패한다.
        guard = patch('subprocess.run', side_effect=AssertionError('외부 실행 금지'))
        guard.start()
        self.addCleanup(guard.stop)

    def post(self, path, **data):
        return self.request(path, 'POST', {'csrf': self.csrf, **data})

    def rows(self, kind=None):
        with closing(connect(self.config)) as conn:
            if kind:
                return conn.execute('SELECT * FROM jobs WHERE type=? ORDER BY id', (kind,)).fetchall()
            return conn.execute('SELECT * FROM jobs ORDER BY id').fetchall()

    def runner(self, analyzer=None, doctor=None, collect=False):
        conn, state = connect(self.config), connect(self.config)
        self.addCleanup(conn.close)
        self.addCleanup(state.close)
        runner = Runner(self.config, conn, state, self.server.repo_lookup, analyzer=analyzer, doctor=doctor)
        if not collect:
            runner.scheduler = SimpleNamespace(tick=lambda collector: False, last_ok=False)
        return runner

    def fake_analyzer(self, *results):
        analyzer = Mock()
        analyzer.return_value.run.side_effect = list(results)
        return analyzer

    def test_job_paths_are_post_only_mutations(self):
        self.assertTrue(JOB_PATHS <= POST_PATHS)
        for path in JOB_PATHS:
            self.assertEqual(self.request(path)[0], 405)
            self.assertEqual(self.request(path, 'POST', {'csrf': 'bad'})[0], 403)
            self.assertEqual(self.request(path, 'POST', {'csrf': self.csrf}, [('!Origin', 'http://127.0.0.1:9999')])[0], 403)
            self.assertEqual(self.request(path, 'POST', {'csrf': self.csrf}, authenticated=False)[0], 403)
        self.assertEqual(self.rows(), [])

    def test_web_codex_setup_and_probe_are_explicit_and_serialized(self):
        body = self.request('/status')[2]
        self.assertIn('분석 준비 초기화', body)
        self.assertIn('격리 검증 실행', body)
        self.assertIn('실제 모델', body)
        self.assertEqual(self.post('/jobs/doctor', action='init')[0], 303)
        self.assertEqual(self.post('/jobs/doctor', action='probe', confirm='yes')[0], 409)
        self.assertEqual(len(self.rows('doctor')), 1)
        with patch('skim.analyze.codex.CodexAdapter.init_home') as initialize:
            check = Mock(return_value=1)
            self.runner(doctor=check).step()
            initialize.assert_called_once_with(reset=False)
            self.assertFalse(check.call_args.kwargs['probe'])
        self.assertEqual(self.rows('doctor')[-1]['status'], 'failed')
        body = self.request('/status')[2]
        self.assertIn('분석 준비 상태 점검 실패', body)
        self.assertNotIn('초기화 완료', body)
        self.assertEqual(self.post('/jobs/doctor', action='probe', confirm='yes')[0], 303)
        doctor = Mock(return_value=0)
        self.runner(doctor=doctor).step()
        self.assertTrue(doctor.call_args.kwargs['probe'])
        self.assertEqual(self.rows('doctor')[-1]['status'], 'completed')
        self.assertEqual(self.post('/jobs/doctor', action='reset')[0], 400)
        self.server.offline = True
        self.assertEqual(self.post('/jobs/doctor', action='probe', confirm='yes')[0], 409)

    def test_probe_requires_explicit_paid_model_confirmation(self):
        for data in ({}, {'confirm': 'no'}):
            self.assertEqual(self.post('/jobs/doctor', action='probe', **data)[0], 400)
        self.assertEqual(self.rows(), [])
        self.assertIn('name="confirm" value="yes" required', self.request('/status')[2])

    def test_setup_preserves_existing_files_and_probe_failure_is_visible(self):
        source = Path(self.temp.name) / 'codex-source'
        source.mkdir()
        (source / 'auth.json').write_text('fake authentication fixture')
        with patch.dict(os.environ, CODEX_HOME=str(source)):
            self.post('/jobs/doctor', action='init')
            self.runner(doctor=Mock(return_value=0)).step()
            home = self.config.data_dir / 'codex-home'
            self.assertTrue((home / 'auth.json').is_symlink())
            self.assertEqual(self.rows('doctor')[-1]['status'], 'completed')
            self.assertIn('분석 준비 초기화 완료', self.request('/status')[2])
            (home / 'keep.txt').write_text('preserve')
            self.post('/jobs/doctor', action='init')
            self.runner().step()
            self.assertEqual(self.rows('doctor')[-1]['status'], 'failed')
            self.assertEqual((home / 'keep.txt').read_text(), 'preserve')
        self.post('/jobs/doctor', action='probe', confirm='yes')
        doctor = Mock(return_value=1)
        self.runner(doctor=doctor).step()
        self.assertTrue(doctor.call_args.kwargs['probe'])
        self.assertEqual(self.rows('doctor')[-1]['status'], 'failed')

    def test_analyze_validates_target_before_queueing(self):
        for repository, number in [('demo/..', '1'), ('a/b;sh', '1'), ('-a/b', '1'), ('demo/checkout', '0'),
                                   ('demo/checkout', '-1'), ('demo/checkout', '01'), ('demo/checkout', '1;rm'),
                                   ('demo/checkout', '1'*11), ('demo/checkout', ' 1')]:
            self.assertEqual(self.post('/jobs/analyze', repository=repository, number=number)[0], 400, (repository, number))
        self.assertEqual(self.post('/jobs/analyze', repository='demo/checkout', number='99')[0], 404)
        self.assertEqual(self.post('/jobs/analyze', repository='demo/checkout', number='1', extra='x')[0], 400)
        # 수집된 최신 SHA 스냅샷이 없으면 먼저 수집하도록 안내한다.
        code, _, body = self.post('/jobs/analyze', repository='demo/checkout', number='5')
        self.assertEqual(code, 409)
        self.assertIn('먼저 수집', body)
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE repositories SET analysis_policy='deny' WHERE owner='demo'")
        code, _, body = self.post('/jobs/analyze', repository='demo/checkout', number='1')
        self.assertEqual(code, 403)
        self.assertIn('허용되지 않았습니다', body)
        self.assertEqual(self.rows(), [])

    def test_analyze_queues_once_and_does_not_retarget(self):
        code, headers, _ = self.post('/jobs/analyze', repository='DEMO/Checkout', number='1')
        self.assertEqual((code, headers['Location']), (303, '/pr/1'))
        self.post('/jobs/analyze', repository='demo/checkout', number='1')
        self.assertEqual(self.post('/jobs/analyze', repository='demo/checkout', number='2')[1]['Location'], '/pr/2')
        rows = self.rows('analyze')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['target_id'], rows[0]['target'], rows[0]['input_sha'], rows[0]['status']),
                         (1, 'demo/checkout#1', 'head1', 'pending'))
        self.assertEqual(json.loads(rows[0]['params']), {'snapshot_id': 1})
        body = self.request('/pr/2')[2]
        self.assertIn('다른 분석 작업(demo/checkout#1)', body)
        self.assertIn('완료 후 다시 요청', body)
        self.assertIn('data-job-active="1"', body)
        self.assertIn('src="/static/jobs.js"', body)

    def test_detail_shows_warning_only_when_opted_in(self):
        body = self.request('/pr/1')[2]
        self.assertIn(WARNING, body)
        self.assertIn('action="/jobs/analyze"', body)
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE repositories SET analysis_policy='inherit' WHERE owner='demo'")
        body = self.request('/pr/1')[2]
        self.assertNotIn('action="/jobs/analyze"', body)
        self.assertIn('href="/repos?scope_host=github.com&amp;select_q=demo%2Fcheckout&amp;from_pr=1#analysis-scope"', body)
        self.assertIn('분석 허용 범위 열기', body)
        self.assertNotIn('action="/jobs/analyze"', self.request('/pr/5')[2])

    def test_runner_maps_analysis_outcomes_and_rechecks_policy(self):
        cases = [([{'status': 'completed'}], 'completed', None),
                 ([{'status': 'partial'}], 'completed', None),
                 ([{'status': 'deferred', 'error': '일일 CLI 호출 20회 소진'}], 'deferred', '20회'),
                 ([{'status': 'blocked', 'error': '유효한 probe 없음'}], 'blocked', 'probe'),
                 ([], 'failed', '대상이 없습니다')]
        for results, status, error in cases:
            with self.subTest(status=status, error=error):
                analyzer = self.fake_analyzer(results)
                self.post('/jobs/analyze', repository='demo/checkout', number='1')
                self.assertTrue(self.runner(analyzer).step())
                analyzer.return_value.run.assert_called_once_with(1, include_drafts=True)
                row = self.rows('analyze')[-1]
                self.assertEqual(row['status'], status)
                if error:
                    self.assertIn(error, row['last_error'])
                self.assertIsNotNone(row['finished_at'])
        # 등록 이후 동의를 끄면 실행 직전 재확인으로 모델을 호출하지 않는다.
        self.post('/jobs/analyze', repository='demo/checkout', number='1')
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE repositories SET analysis_policy='deny' WHERE owner='demo'")
        analyzer = self.fake_analyzer()
        self.runner(analyzer).step()
        analyzer.return_value.run.assert_not_called()
        self.assertIn('허용이 꺼져', self.rows('analyze')[-1]['last_error'])

    def test_batch_analysis_preserves_non_success_outcomes(self):
        cases = [([], 'completed'), ([{'status': 'partial'}], 'completed'),
                 ([{'status': 'deferred'}], 'deferred'),
                 ([{'status': 'completed'}, {'status': 'deferred'}], 'deferred'),
                 ([{'status': 'failed'}, {'status': 'deferred'}], 'failed'),
                 ([{'status': 'deferred'}, {'status': 'blocked'}], 'blocked')]
        for results, expected in cases:
            with self.subTest(results=results):
                with closing(connect(self.config)) as conn:
                    row, _ = jobs.enqueue(conn, 'analyze')
                self.runner(self.fake_analyzer(results)).step()
                data = json.loads(self.request('/jobs/' + str(row['id']))[2])
                self.assertEqual(data['status'], expected)
                self.assertFalse(data['active'])
                self.assertIsNotNone(data['finished_at'])
                self.assertEqual(data['status_label'], jobs.STATUS[expected])
                if expected == 'blocked':
                    self.assertIn('복구 안내', data['error'])
                if expected == 'deferred':
                    self.assertIn('예산', data['error'])

    def test_analysis_prompt_distinguishes_first_and_stale_failure(self):
        with closing(connect(self.config)) as conn, conn:
            conn.execute('DELETE FROM analyses WHERE snapshot_id IN (SELECT id FROM snapshots WHERE pr_id=1)')
        body = self.request('/pr/1')[2]
        self.assertIn('현재 스냅샷의 분석 결과가 없습니다', body)
        self.assertNotIn('다시 분석</button>', body)
        with closing(connect(self.config)) as conn, conn:
            snapshot = conn.execute('SELECT * FROM snapshots WHERE pr_id=1 ORDER BY id DESC LIMIT 1').fetchone()
            conn.execute('INSERT INTO analyses(snapshot_id,input_hash,provider,model,cli_version,prompt_version,schema_version,status) VALUES(?,?,?,?,?,?,?,?)',
                         (snapshot['id'], 'x', 'codex', 'model', 'v', 'p', 's', 'failed'))
            conn.execute('INSERT INTO snapshots(pr_id,base_sha,head_sha,diff_hash,input_hash,diff_path,coverage) VALUES(?,?,?,?,?,?,?)',
                         (1, snapshot['base_sha'], snapshot['head_sha'], 'new', 'new', snapshot['diff_path'], snapshot['coverage']))
        body = self.request('/pr/1')[2]
        self.assertIn('현재 스냅샷의 분석 결과가 없습니다', body)
        self.assertNotIn('다시 분석</button>', body)

    def test_status_polls_recent_and_older_active_jobs(self):
        with closing(connect(self.config)) as conn:
            active, _ = jobs.enqueue(conn, 'analyze')
            for _ in range(11):
                row, _ = jobs.enqueue(conn, 'doctor')
                jobs.finish(conn, row['id'], 'completed')
        body = self.request('/status')[2]
        self.assertIn(f'data-job-id="{active["id"]}" data-job-active="1"', body)
        self.post('/jobs/doctor')
        body = self.request('/status')[2]
        recent = body.split('<h2>최근 작업</h2>', 1)[1]
        self.assertIn('data-job-active="1"', recent)
        self.assertIn('data-job-active="0"', recent)
        self.assertIn('<script src="/static/jobs.js" defer></script>', body)
        self.assertNotIn('<script>', body)

    def test_status_sections_and_job_progress(self):
        with closing(connect(self.config)) as conn:
            row, _ = jobs.enqueue(conn, 'doctor')
            pending = self.request('/status')[2]
            self.assertIn('<div class="status-actions">', pending)
            self.assertIn('<section class="status-section"><h2>활성 작업</h2>', pending)
            self.assertIn('<section class="status-section"><h2>최근 작업</h2>', pending)
            self.assertIn('<details class="status-recovery"><summary>복구 안내</summary>', pending)
            self.assertIn('data-job-progress-label>대기 중</span>', pending)
            self.assertIn('aria-label="작업 진행" hidden', pending)
            jobs.claim(conn)
            running = self.request('/status')[2]
            self.assertIn('data-job-progress-label>작업 중</span>', running)
            self.assertIn('<progress aria-label="작업 진행"></progress>', running)
            jobs.progress(conn, row['id'], '점검', 2, 5)
            running = self.request('/status')[2]
            self.assertIn('aria-label="작업 진행" max="5" value="2"', running)
            jobs.finish(conn, row['id'], 'completed')
            self.assertNotIn('data-job-progress-label>작업 중</span>', self.request('/status')[2])
        script = self.request('/static/jobs.js', authenticated=False)[2]
        self.assertIn("job.status === 'running'", script)
        self.assertIn('progress.value = job.processed', script)
        self.assertIn("progress.removeAttribute('value')", script)

    def test_job_metadata_survives_polling(self):
        with closing(connect(self.config)) as conn, conn:
            row, _ = jobs.enqueue(conn, 'doctor')
        body = self.request('/status')[2]
        self.assertIn('<p class="meta" aria-live="polite" data-job-field="summary"><span>', body)
        payload = json.loads(self.request('/jobs/' + str(row['id']))[2])
        self.assertEqual(payload['summary'], ', '.join(payload['summary_parts']))
        self.assertNotIn('·', payload['summary'])
        script = self.request('/static/jobs.js', authenticated=False)[2]
        self.assertIn('field.replaceChildren', script)
        self.assertIn("document.createElement('span')", script)
        self.assertNotIn('·', script)

    def test_poll_script_retries_with_bounded_backoff(self):
        # 표준 라이브러리만으로 제공 스크립트의 재시도·종료 경로를 검사한다.
        script = self.request('/static/jobs.js', authenticated=False)[2]
        failure = script.split('.catch(() => {', 1)[1]
        self.assertIn('setTimeout(tick, retryDelay)', failure)
        self.assertIn('Math.min(retryDelay * 2, 30000)', failure)
        success = script.split('.then(job => {', 1)[1].split('.catch', 1)[0]
        self.assertIn('retryDelay = 2000', success)
        self.assertIn('if (job.active) setTimeout(tick, 2000)', success)
        self.assertNotIn('location.reload()', success)
        self.assertIn('resultText.textContent = text', success)
        self.assertNotIn('innerHTML', script)

    def test_runner_rejects_stale_snapshot_and_hides_raw_errors(self):
        self.post('/jobs/analyze', repository='demo/checkout', number='1')
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET head_sha='moved' WHERE id=1")
        analyzer = self.fake_analyzer()
        self.runner(analyzer).step()
        analyzer.return_value.run.assert_not_called()
        self.assertIn('새 스냅샷', self.rows('analyze')[-1]['last_error'])
        with closing(connect(self.config)) as conn, conn:
            conn.execute("UPDATE pull_requests SET head_sha='head1' WHERE id=1")
        self.post('/jobs/analyze', repository='demo/checkout', number='1')
        analyzer = Mock()
        analyzer.return_value.run.side_effect = RuntimeError('SECRET token=abc')
        self.runner(analyzer).step()
        row = self.rows('analyze')[-1]
        self.assertEqual(row['status'], 'failed')
        self.assertNotIn('SECRET', row['last_error'])
        self.assertNotIn('SECRET', self.request('/pr/1')[2])

    def test_analysis_progress_is_recorded(self):
        seen = []
        def run(snapshot_id, include_drafts):
            analyzer.return_value.progress('모델 분석', 1, 3)
            seen.extend((r['stage'], r['processed'], r['total']) for r in self.rows('analyze'))
            return [{'status': 'completed'}]
        analyzer = Mock()
        analyzer.return_value.run.side_effect = run
        self.post('/jobs/analyze', repository='demo/checkout', number='1')
        self.runner(analyzer).step()
        self.assertEqual(seen, [('모델 분석', 1, 3)])
        data = json.loads(self.request('/jobs/' + str(self.rows('analyze')[0]['id']))[2])
        self.assertEqual((data['status'], data['active'], data['total']), ('completed', False, 3))

    def test_doctor_job_never_probes_and_shows_guidance(self):
        doctor = Mock(side_effect=lambda config, probe, out: (out('실패: gh — gh auth login으로 인증을 확인하세요.'), 1)[1])
        self.assertEqual(self.post('/jobs/doctor')[1]['Location'], '/status')
        self.post('/jobs/doctor')
        self.assertEqual(len(self.rows('doctor')), 1)
        self.runner(doctor=doctor).step()
        self.assertFalse(doctor.call_args.kwargs['probe'])
        self.assertEqual(self.rows('doctor')[0]['status'], 'failed')
        body = self.request('/status')[2]
        for text in ['점검 실패 항목', 'gh auth login', 'skim codex-home init', 'skim doctor --probe', '상태 점검 #']:
            self.assertIn(text, body)
        self.assertIn('data-job-active="0"', body)

    def test_real_doctor_output_is_collected_without_stdout(self):
        from skim import doctor
        lines = []
        adapter = Mock(model='unknown', unknown_enabled=[])
        with patch('skim.doctor.print', create=True, side_effect=AssertionError('stdout 금지')):
            doctor.run(self.config, runner=Mock(return_value=Mock(returncode=0)), which=lambda name: None,
                       adapter=adapter, out=lines.append)
        self.assertTrue(any(line.startswith('실패: gh') for line in lines))
        adapter.probe.assert_not_called()

    def test_collect_request_merges_followup_and_queues_auto_analysis(self):
        self.assertEqual(self.post('/jobs/collect')[0], 303)
        self.post('/jobs/collect')
        self.assertEqual(len(self.rows('collect')), 1)
        runner = self.runner(self.fake_analyzer([{'status': 'completed'}]), collect=True)
        stages = []
        def collect():
            runner.collector._report('PR 수집', 1, 2)
            row = self.rows('collect')[0]
            stages.append((row['stage'], row['total']))
            # 실행 중 수동 요청은 후속 1회로 합친다.
            self.post('/jobs/collect')
            self.assertIn('완료 후 1회 다시 수집', self.request('/status')[2])
            return 0
        runner.collector.collect = collect
        self.assertTrue(runner.step())
        self.assertEqual(stages, [('PR 수집', 2)])
        collects = self.rows('collect')
        self.assertEqual([r['status'] for r in collects], ['completed', 'pending'])
        analyze = self.rows('analyze')
        self.assertEqual([(r['target_id'], r['status']) for r in analyze], [(0, 'completed')])
        runner.analyzer.return_value.run.assert_called_once_with()

    def test_auto_analysis_waits_for_pending_pr_analysis(self):
        # 수집 직전에 PR 단위 분석이 대기 중이어도 수집 후 자동 분석을 잃지 않는다.
        self.post('/jobs/analyze', repository='demo/checkout', number='1')
        self.post('/jobs/collect')
        runner = self.runner(self.fake_analyzer([{'status': 'completed'}], [{'status': 'completed'}]), collect=True)
        runner.collector.collect = lambda: 0
        self.assertTrue(runner.step())
        self.assertEqual([(r['target_id'] != 0, r['status']) for r in self.rows('analyze')], [(True, 'completed')])
        # 다음 단계는 수집 없이도 미뤄 둔 대상 0 분석을 등록·실행한다.
        runner.scheduler = SimpleNamespace(tick=lambda collector: False, last_ok=False)
        self.assertTrue(runner.step())
        self.assertEqual([(r['target_id'] != 0, r['status']) for r in self.rows('analyze')], [(True, 'completed'), (False, 'completed')])
        self.assertFalse(runner.step())
        self.assertEqual(len(self.rows('analyze')), 2)

    def test_collect_failure_keeps_retry_time_and_safe_error(self):
        self.post('/jobs/collect')
        runner = self.runner(self.fake_analyzer(), collect=True)
        runner.collector.collect = Mock(side_effect=RuntimeError('SECRET'))
        runner.step()
        row = self.rows('collect')[0]
        self.assertEqual(row['status'], 'pending')
        self.assertIsNotNone(row['next_run_at'])
        self.assertNotIn('SECRET', row['last_error'])
        self.assertIn('재시도 가능', self.request('/status')[2])
        self.assertEqual(self.rows('analyze'), [])

    def test_poll_endpoint_is_authenticated_read_only_json(self):
        self.post('/jobs/doctor')
        job = self.rows('doctor')[0]['id']
        code, headers, body = self.request(f'/jobs/{job}')
        self.assertEqual(code, 200)
        self.assertTrue(headers['Content-Type'].startswith('application/json'))
        data = json.loads(body)
        self.assertEqual((data['kind'], data['status'], data['active']), ('doctor', 'pending', True))
        self.assertIn('상태 점검 #', data['summary'])
        self.assertEqual(self.request(f'/jobs/{job}', authenticated=False)[0], 401)
        self.assertEqual(self.request(f'/jobs/{job}', headers=[('Origin', 'http://evil.test')])[0], 403)
        self.assertEqual(self.request(f'/jobs/{job}', headers=[('!Host', 'evil.test')])[0], 403)
        for path in ['/jobs/999', '/jobs/0', '/jobs/abc', '/jobs/1/x']:
            self.assertEqual(self.request(path)[0], 404, path)
        for path in ['/', '/status', '/pr/1', '/repos', f'/jobs/{job}']:
            self.request(path)
        self.assertEqual(len(self.rows()), 1)
        code, headers, script = self.request('/static/jobs.js', authenticated=False)
        self.assertEqual(code, 200)
        self.assertNotIn('innerHTML', script)
        self.assertIn('2000', script)

    def test_offline_never_queues_or_runs_external_commands(self):
        self.server.offline = True
        for path, data in [('/jobs/collect', {}), ('/jobs/doctor', {}),
                           ('/jobs/analyze', dict(repository='demo/checkout', number='1'))]:
            code, _, body = self.post(path, **data)
            self.assertEqual(code, 409)
            self.assertIn('오프라인', body)
        self.assertEqual(self.rows(), [])
        for path in ['/', '/status', '/pr/1']:
            body = self.request(path)[2]
            self.assertIn('오프라인', body)
            self.assertNotIn('action="/jobs/', body)

    def test_restart_recovers_interrupted_jobs_without_rerunning_model(self):
        self.post('/jobs/analyze', repository='demo/checkout', number='1')
        self.post('/jobs/doctor')
        with closing(connect(self.config)) as conn:
            jobs.claim(conn)
        analyzer = self.fake_analyzer()
        doctor = Mock(return_value=0)
        runner = self.runner(analyzer, doctor)
        rows = {r['type']: r for r in self.rows()}
        self.assertEqual((rows['analyze']['status'], rows['analyze']['last_error']), ('failed', INTERRUPTED))
        self.assertEqual(rows['doctor']['status'], 'pending')
        runner.step()
        analyzer.return_value.run.assert_not_called()
        self.assertEqual(self.rows('doctor')[0]['status'], 'completed')

    def test_database_enforces_one_active_job_per_kind(self):
        with closing(connect(self.config)) as conn:
            conn.execute("INSERT INTO jobs(type,target_id) VALUES('analyze',1)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO jobs(type,target_id) VALUES('analyze',2)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO jobs(type,target_id) VALUES('shell',0)")
            with self.assertRaises(ValueError):
                jobs.enqueue(conn, 'collect')

    def test_migration_v4_preserves_all_job_fields_and_adds_blocked(self):
        import os
        import tempfile
        from skim.config import Config
        from skim.db import MIGRATIONS
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, SKIM_HOME=home):
            config = Config.load()
            with patch('skim.db.MIGRATIONS', MIGRATIONS[:4]):
                with closing(connect(config)) as conn:
                    row, _ = jobs.enqueue(conn, 'analyze', 7, '테스트 대상', 'sha', {'snapshot_id': 9})
                    jobs.claim(conn)
                    jobs.progress(conn, row['id'], '분석 중', 1, 2)
                    before = dict(jobs.get(conn, row['id']))
            with closing(connect(config)) as conn:
                self.assertEqual(dict(jobs.get(conn, row['id'])), before)
                jobs.finish(conn, row['id'], 'blocked', error='복구 안내')
                self.assertEqual(jobs.describe(jobs.get(conn, row['id']))['status_label'], '격리')
                jobs.enqueue(conn, 'analyze')
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO jobs(type,target_id) VALUES('analyze',8)")

    def test_migration_v3_preserves_jobs_and_closes_duplicate_active(self):
        import os
        import tempfile
        from skim.config import Config
        from skim.db import MIGRATIONS
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, SKIM_HOME=home):
            config = Config.load()
            with patch('skim.db.MIGRATIONS', MIGRATIONS[:3]):
                with closing(connect(config)) as conn, conn:
                    conn.execute("INSERT INTO jobs(type,target_id,status,last_error,retry_not_before) VALUES('collect',0,'pending','요청 제한','2026-11-01T00:00:00+00:00')")
                    conn.execute("INSERT INTO jobs(type,target_id,input_hash) VALUES('analyze',1,'a')")
                    conn.execute("INSERT INTO jobs(type,target_id,input_hash) VALUES('analyze',2,'b')")
            with closing(connect(config)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS))
                rows = conn.execute('SELECT type,status,last_error,retry_not_before FROM jobs ORDER BY id').fetchall()
                self.assertEqual([tuple(r) for r in rows][0], ('collect', 'pending', '요청 제한', '2026-11-01T00:00:00+00:00'))
                self.assertEqual([r['status'] for r in rows[1:]], ['pending', 'cancelled'])


del WebTests
