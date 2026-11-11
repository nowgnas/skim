"""경로와 실제 변경 줄에 근거한 위험 후보 회귀 검사."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.analyze.units import added_test_lines, candidates, changed_lines, parse_diff


class RiskCandidatesTests(unittest.TestCase):
    def unit(self, path, diff='', old_path=None):
        files = [dict(filename=path, previous_filename=old_path)]
        patch = f'diff --git a/{path} b/{path}\n' + diff if diff else ''
        return parse_diff(patch, files)[0][0]

    def test_secrets_directory_is_only_a_location_hint(self):
        unit = self.unit('secrets/app.py')
        self.assertIn('secrets', unit['location_hints'])
        self.assertIn('secrets', unit['candidates'])
        self.assertEqual(unit['kind_signals'], [])
        self.assertEqual(unit['hard_floors'], [])

    def test_dependency_text_file_is_not_demoted_by_extension(self):
        from skim.analyze.units import allocation_key
        dependency = dict(self.unit('requirements.txt'), change_id='h1')
        source = dict(self.unit('app.py'), change_id='h2', content_hits=['auth'])
        self.assertLess(allocation_key(dependency), allocation_key(source))

    def test_signals_separate_location_content_and_floors(self):
        unit = self.unit('auth/login.py')
        self.assertEqual(unit['location_hints'], ['auth'])
        self.assertEqual(unit['content_hits'], [])
        self.assertEqual(unit['hard_floors'], [])
        unit = self.unit('app.py', '@@ -1 +1 @@\n-@login_required\n+pass')
        self.assertEqual(unit['content_hits'], ['auth'])
        self.assertEqual(unit['location_hints'], [])
        for text in ('+token: str', '+token = value', '+password: str'):
            unit = self.unit('app.py', '@@ -1 +1 @@\n' + text)
            self.assertIn('secrets', unit['content_hits'])
            self.assertEqual(unit['hard_floors'], [])
        unit = self.unit('app.py', '@@ -1 +1 @@\n+value = "ghp_example123"')
        self.assertIn('secrets', unit['hard_floors'])

    def test_schema_path_and_ddl_are_distinct(self):
        for path in ('migrations/0001_init.py', 'alembic/versions/1.py', 'schema.prisma', 'db/schema.rb'):
            unit = self.unit(path)
            self.assertIn('db_schema_migration', unit['kind_signals'])
            self.assertIn('db_schema_migration', unit['hard_floors'])
        for path, text in (('query.sql', '+SELECT * FROM users;'),
                           ('tests/test_db.py', '+sql = "CREATE TABLE users (id int)"'),
                           ('tests/schema.sql', '+CREATE TABLE users (id int);'),
                           ('app.py', '+sql = "CREATE TABLE users (id int)"')):
            unit = self.unit(path, '@@ -1 +1 @@\n' + text)
            self.assertEqual(unit['kind_signals'], [])
            self.assertEqual(unit['hard_floors'], [])
            if 'CREATE' in text:
                self.assertIn('db_schema_migration', unit['content_hits'])
        unit = self.unit('query.sql', '@@ -1 +1 @@\n+DROP TABLE users;')
        self.assertEqual(unit['hard_floors'], ['db_schema_migration'])

    def test_sql_comments_and_literals_are_content_only(self):
        for sql in ('/*\nDROP TABLE users;\n*/', '-- DROP TABLE users;',
                    "SELECT 'literal\nDROP TABLE users;\n';",
                    'SELECT $$literal\nDROP TABLE users;\n$$;',
                    'SELECT $tag$literal\nDROP TABLE users;\n$tag$;'):
            for sign in ('+', '-'):
                unit = self.unit('query.sql', '@@ -1 +1 @@\n SELECT 1;\n' +
                                 '\n'.join(sign + line for line in sql.splitlines()))
                self.assertIn('db_schema_migration', unit['content_hits'])
                self.assertEqual(unit['hard_floors'], [])
        for text in (' /* comment\n+DROP TABLE users;\n */',
                     " SELECT 'literal\n+DROP TABLE users;\n ';",
                     ' SELECT $tag$literal\n+DROP TABLE users;\n $tag$;'):
            self.assertEqual(self.unit('query.sql', '@@ -1 +1 @@\n' + text)['hard_floors'], [])
        for sign in ('+', '-'):
            unit = self.unit('query.sql', '@@ -1 +1 @@\n /* done */\n' + sign + 'DROP TABLE users;')
            self.assertEqual(unit['hard_floors'], ['db_schema_migration'])

    def test_old_paths_retain_location_kind_and_sensitive_exclusion(self):
        for old, key, flag in (('auth/login.py', 'location_hints', 'auth'),
                               ('payments/client.py', 'location_hints', 'payments'),
                               ('config/app.py', 'location_hints', 'config'),
                               ('.github/workflows/ci.yml', 'kind_signals', 'ci_workflow'),
                               ('migrations/1.py', 'hard_floors', 'db_schema_migration'),
                               ('.env', 'hard_floors', 'secrets')):
            unit = self.unit('plain.py', old_path=old)
            self.assertIn(flag, unit[key])
            if old == '.env':
                self.assertTrue(unit['excluded'])
                self.assertEqual(unit['diff'], '')

    def test_mechanical_hints_never_infer_from_synthetic_alone(self):
        self.assertEqual(self.unit('a.py')['mechanical_hints'], [])
        body = 'similarity index 100%\nrename from a.py\nrename to b.py\n'
        self.assertEqual(self.unit('b.py', body)['mechanical_hints'], ['pure_rename'])
        self.assertEqual(self.unit('b.py', body + 'old mode 100644\nnew mode 100755\n')['mechanical_hints'], [])

    def hunk(self, path, *changed):
        return self.unit(path, f'--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n context\n' + '\n'.join(changed) + '\n')

    def test_comment_only_hint_requires_every_changed_line_to_be_a_comment(self):
        for path, changed in (('app/order_core.py', ('-# prior note 1', '+# updated note 1')),
                              ('app/order_core.py', ('-    # prior', '+', '+    # updated')),
                              ('web/app.ts', ('-// prior', '+/* updated', '+*/')),
                              ('web/app.ts', ('-/* prior */', '+/* updated */  ')),
                              ('db/report.sql', ('--- prior', '+-- updated')),
                              ('src/OrderController.php', ('-# old note', '+# new note')),
                              ('config/app.toml', ('-#[old]', '+#[new]'))):
            with self.subTest(path=path, changed=changed):
                self.assertEqual(self.hunk(path, *changed)['mechanical_hints'], ['comment_only'])
        for path, changed in (('app/order_core.py', ('-    return order.subtotal', '+    return order.subtotal + order.delivery_fee')),
                              ('app/order_core.py', ('-# prior note', '+return total  # updated note')),
                              ('app/order_core.py', ('-# prior note', '+# updated note', '+total += fee')),
                              ('app/sync/worker.py', ('-            queue.ack(job)', '+        queue.ack(job)')),
                              ('app/call.py', ('-run(args)', '+run(', '+    *args)')),
                              ('web/app.ts', ('-/* note */ run(1);', '+/* note */ run(2);')),
                              ('app/query.sql', ('-/* note */ SELECT 1;', '+/* note */ SELECT 2;')),
                              ('web/app.ts', ('-/* a */ /* b */', '+/* a */ run(); /* b */')),
                              ('web/app.ts', ('-# prior', '+# updated')),
                              ('src/OrderController.php', ("-#[Route('/old')]", "+#[Route('/new')]")),
                              ('src/OrderController.php', ('-# old note', "+    #[Route('/new')]")),
                              ('README.md', ('-# Old title', '+# New title')),
                              ('lib/util.c', ('-#include <a.h>', '+#include <b.h>')),
                              ('tools/run.sh', ('-#!/bin/sh', '+#!/bin/bash')),
                              ('tools/run.sh', ('-    --dry-run \\', '+    --force \\'))):
            with self.subTest(path=path, changed=changed):
                self.assertEqual(self.hunk(path, *changed)['mechanical_hints'], [])

    def test_php_attribute_change_keeps_default_priority(self):
        from skim.analyze.units import priority
        attribute = self.hunk('src/OrderController.php', "-#[Route('/old')]", "+#[Route('/new')]")
        note = self.hunk('src/OrderController.php', '-# old note', '+# new note')
        self.assertEqual(attribute['mechanical_hints'], [])
        self.assertEqual(priority(attribute), priority(self.hunk('src/OrderController.php', '-run(1);', '+run(2);')))
        self.assertEqual(note['mechanical_hints'], ['comment_only'])
        self.assertGreater(priority(note), priority(attribute))

    def test_comment_only_hint_ignores_units_without_a_hunk(self):
        self.assertEqual(self.unit('tools/rotate_logs.sh', 'old mode 100644\nnew mode 100755\n')['mechanical_hints'], [])
        fixture = (Path(__file__).resolve().parents[1] / 'eval/skim-ko-6/diffs/fx-format-behavior.diff').read_text()
        self.assertEqual([u['mechanical_hints'] for u in parse_diff(fixture)[0]], [[], []])

    def test_comment_only_hint_keeps_other_signals(self):
        unit = self.hunk('db/migrations/001_orders.sql', '--- prior note', '+-- updated note')
        self.assertEqual(unit['mechanical_hints'], ['comment_only'])
        self.assertEqual(unit['hard_floors'], ['db_schema_migration'])
        from skim.analyze.units import priority
        self.assertEqual(priority(unit), 0)

    def test_comment_only_hunks_are_allocated_after_real_changes(self):
        from skim.analyze.protocol import chunk_units
        from skim.analyze.units import allocation_key
        fixture = (Path(__file__).resolve().parents[1] / 'eval/skim-ko-6/diffs/fx-multichunk.diff').read_text()
        units, _ = parse_diff(fixture)
        real = {path: [u['change_id'] for u in units if u['file_path'] == path and 'comment_only' not in u['mechanical_hints']]
                for path in ('app/order_core.py', 'app/order_routes.py', 'infra/deployment.yaml')}
        self.assertTrue(all(len(ids) == 1 for ids in real.values()), real)
        fillers = [u for u in units if u['file_path'] in ('app/order_core.py', 'app/order_routes.py')
                   and u['mechanical_hints'] == ['comment_only']]
        self.assertGreater(len(fillers), 100)
        ordered = sorted(units, key=allocation_key)
        first_filler = next(i for i, u in enumerate(ordered) if u['mechanical_hints'])
        self.assertTrue(all(not u['mechanical_hints'] for u in ordered[:first_filler]))
        self.assertTrue(all(u['mechanical_hints'] == ['comment_only'] for u in ordered[first_filler:]))
        # Without an overview there is no focus list, so packing alone has to keep these together.
        chunks = chunk_units(units, {})
        self.assertEqual(sum(len(c['units']) for c in chunks), len(units))
        first = [u['change_id'] for u in chunks[0]['units']]
        self.assertLessEqual({ids[0] for ids in real.values()}, set(first[:first_filler]))

    def test_usage_counts_remain_visible_but_secrets_are_redacted(self):
        from skim.analyze.units import sanitize
        for line in ('max_tokens = 100', 'token_count: 3', 'input_tokens = 2147483647'):
            self.assertEqual(sanitize(line), line)
        for line in ('token = "secret-value"', 'password = "abc"',
                     'max_tokens = "secret-value"', 'token_count = 2147483648',
                     'max_tokens = 100; password = "abc"'):
            cleaned = sanitize(line)
            self.assertIn('[민감 정보 제외', cleaned)
            self.assertNotIn('secret-value', cleaned)
            self.assertNotIn('abc', cleaned)

    def test_pr13_provider_schema(self):
        diff = '''diff --git a/lib/skim/analyze/codex.py b/lib/skim/analyze/codex.py
--- a/lib/skim/analyze/codex.py
+++ b/lib/skim/analyze/codex.py
@@ -1,4 +1,8 @@
+UNSUPPORTED_SCHEMA_KEYS = {'$schema', 'maxItems'}
+def provider_schema(schema):
+    return {k: v for k, v in schema.items() if k not in UNSUPPORTED_SCHEMA_KEYS}
 (Path(runtime) / 'auth.json').symlink_to(auth_path)
-schema_path.write_text(encode(schema))
+schema_path.write_text(encode(provider_schema(schema)))
'''
        units, _ = parse_diff(diff)
        self.assertNotIn('auth', units[0]['candidates'])
        self.assertNotIn('db_schema_migration', units[0]['candidates'])

    def test_positive_paths(self):
        for path, flag in (
            ('migrations/0001_init.py', 'db_schema_migration'),
            ('db/migrate/001.py', 'db_schema_migration'),
            ('alembic/versions/001.py', 'db_schema_migration'),
            ('prisma/schema.prisma', 'db_schema_migration'),
            ('db/schema.rb', 'db_schema_migration'),
            ('auth/login.py', 'auth'), ('middleware/auth_guard.py', 'auth'),
            ('.github/workflows/ci.yml', 'ci_workflow'),
            ('package.json', 'dependency'), ('.env', 'config'), ('.env', 'secrets'),
            ('settings.py', 'config'), ('config/app.json', 'config'),
            ('private.pem', 'secrets'),
            ('payments/client.py', 'payments')):
            with self.subTest(path=path, flag=flag):
                self.assertIn(flag, candidates(path, '', []))

    def test_install_script_paths(self):
        for path in ('setup.sh', 'install.sh', 'bootstrap.sh', 'release.sh',
                     'scripts/install_deps.sh', 'scripts/setup-dev.sh',
                     'scripts/tools/install-deps.sh', 'tools/bootstrap.sh',
                     'setup.py', 'lib/setup.py', 'lib/skim/setup.py',
                     'bin/skim', 'bin/tools/helper.py'):
            with self.subTest(path=path):
                self.assertIn('install_script', candidates(path, '', []))
        for path in ('lib/skim/setup_utils.py', 'docs/setup.md',
                     'scripts/release.sh', 'tools/install_deps.sh',
                     'src/setup.py', 'setup.sh.md', 'Makefile', 'binary/tool'):
            with self.subTest(path=path):
                self.assertNotIn('install_script', candidates(path, '', []))

    def test_changed_evidence_only(self):
        for line, flag in (
            ('ALTER TABLE users ADD COLUMN x', 'db_schema_migration'),
            ('CREATE INDEX idx ON users (x)', 'db_schema_migration'),
            ('def authenticate(user, password):', 'auth'),
            ('authorize(user)', 'auth'), ('checkPermission(user)', 'auth'),
            ('jwt.decode(value)', 'auth'),
            ('stripe.PaymentIntent.create(amount=100)', 'payments'),
            ('API_KEY = "literal"', 'secrets')):
            for sign in ('+', '-'):
                with self.subTest(line=line, sign=sign):
                    self.assertIn(flag, candidates('app.py', sign + line, []))
            self.assertNotIn(flag, candidates('app.py', ' ' + line, []))
            self.assertNotIn(flag, candidates('app.py', '@@ -1 +1 @@ ' + line, []))

    def test_bare_words_and_file_mentions(self):
        flags = candidates('lib/provider_schema.py', '''+schema = provider_schema(schema)
+config = settings
+message = "payment billing checkout refund charge"
+path = Path('auth.json')
+path = Path('schema.sql')
+++ b/auth/login.py
--- a/migrations/001.py
''', [])
        self.assertEqual(flags, ['logic_without_tests'])

    def test_redacted_context_is_not_secret_evidence(self):
        units, _ = parse_diff('''diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 PASSWORD = "fixture"
-old()
+new()
''')
        self.assertTrue(units[0]['redacted'])
        self.assertNotIn('secrets', units[0]['candidates'])

    def test_common_file_paths(self):
        for path, flag in (
            ('src/billing.py', 'payments'), ('payment.py', 'payments'),
            ('billingService.ts', 'payments'), ('config.py', 'config'),
            ('settings/production.py', 'config'),
            ('nginx.conf', 'config'), ('compose.yaml', 'config'),
            ('src/auth_service.py', 'auth'), ('oauth_client.py', 'auth'),
            ('src/authService.py', 'auth'), ('schema.prisma', 'db_schema_migration')):
            with self.subTest(path=path):
                self.assertIn(flag, candidates(path, '', []))
        for path in ('openapi.yaml', 'fixtures/example.yml', 'src/author.py'):
            with self.subTest(path=path):
                self.assertNotIn('config', candidates(path, '', []))
                self.assertNotIn('auth', candidates(path, '', []))

    def test_auth_decorator_removal_and_compound_calls(self):
        for line in ('@requires_auth', '@permission_required("x")',
                     '@login_required', '@jwt_required()', 'requires_auth(user)',
                     'permission_required("x")', 'loginRequired(user)'):
            for sign in ('+', '-'):
                with self.subTest(line=line, sign=sign):
                    self.assertIn('auth', candidates('app.py', sign + line, []))
            self.assertNotIn('auth', candidates('app.py', ' ' + line, []))
        for line in ('author()', 'authorship()', 'message = "auth"', 'auth', 'requires_auth'):
            self.assertNotIn('auth', candidates('app.py', '+' + line, []))

    def test_changed_literal_assignments(self):
        for line in ('export API_TOKEN=abcd1234', 'DB_PASSWORD=hunter2',
                     'password = "fixture"', 'token = "fixture"'):
            for sign in ('+', '-'):
                with self.subTest(line=line, sign=sign):
                    self.assertIn('secrets', candidates('compose.yml', sign + line, []))
            self.assertNotIn('secrets', candidates('compose.yml', ' ' + line, []))

    def test_private_key_body_replacement(self):
        for path in ('deploy/k.yaml', 'app.py'):
            for change in ('-MIIEold\n+MIIEnew', '+MIIEnew', '-MIIEold'):
                with self.subTest(path=path, change=change):
                    units, _ = parse_diff(f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1,3 +1,3 @@
 -----BEGIN RSA PRIVATE KEY-----
{change}
 -----END RSA PRIVATE KEY-----
""")
                    self.assertTrue(units[0]['redacted'])
                    self.assertIn('secrets', units[0]['candidates'])
                    self.assertNotIn('MIIE', units[0]['diff'])

    def test_private_key_context_only(self):
        text = """@@ -1,4 +1,4 @@
 -----BEGIN RSA PRIVATE KEY-----
 MIIEfixture
 -----END RSA PRIVATE KEY-----
-old()
+new()
"""
        self.assertNotIn('secrets', candidates('app.py', text, []))

    def test_sanitized_changed_lines_are_secrets(self):
        for path, line in (
            ('deploy/secret.yaml', '  password: hunter2'),
            ('app.py', 'DB_PASSWORD=changeme'),
            ('app.py', 'SECRET_KEY=Pa$$w0rd!'),
            ('app.py', 'Authorization: Bearer eyJabc.def.ghi'),
            ('app.py', 'csrf_token = generate_token()'),
            ('app.py', 'csrf_token = "fixture"'),
            ('app.py', 'token = get_token()'),
            ('app.py', 'password = user.password'),
            ('app.py', 'Authorization: Bearer ${TOKEN}')):
            for sign in ('+', '-'):
                with self.subTest(path=path, line=line, sign=sign):
                    units, _ = parse_diff(f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
{sign}{line}
""")
                    self.assertTrue(units[0]['redacted'])
                    self.assertIn('secrets', units[0]['candidates'])
                    self.assertIn('secrets', candidates(path, sign + line, []))

    def test_config_stems_and_payment_directories(self):
        for path in ('refund/handler.py', 'refunds/handler.py',
                     'charge/handler.py', 'charges/handler.py'):
            with self.subTest(path=path):
                self.assertIn('payments', candidates(path, '', []))
        for path in ('configs/app.json', 'configs.py', 'settings.py',
                     'config/app.json', 'config.py', 'settings/app.json',
                     'tsconfig.json', 'app/application.yml', 'application.properties'):
            with self.subTest(path=path):
                self.assertIn('config', candidates(path, '', []))
        for path in ('configurable.ts', 'configurable/app.ts', 'config_prod.py',
                     'settings_extra.py', 'configuration/app.json'):
            with self.subTest(path=path):
                self.assertNotIn('config', candidates(path, '', []))

    def test_review_path_boundaries_and_config(self):
        for path, flag in (
            ('db/migration/V1.py', 'db_schema_migration'),
            ('src/migration/AddUser.ts', 'db_schema_migration'),
            ('Dockerfile', 'config'), ('deploy/Dockerfile.prod', 'config'),
            ('infra/main.tf', 'config'), ('helm/app/values.yaml', 'config'),
            ('.github/dependabot.yml', 'config')):
            with self.subTest(path=path):
                self.assertIn(flag, candidates(path, '', []))
        for path, flag in (
            ('scripts/checkout_repo.sh', 'payments'),
            ('striped_table.py', 'payments'), ('middleware/author.py', 'auth')):
            with self.subTest(path=path):
                self.assertNotIn(flag, candidates(path, '', []))
        self.assertIn('auth', candidates('app.py', '+oauth_client(value)', []))

    def test_header_like_changes_inside_hunk(self):
        text = '--- a/app.sql\n+++ b/app.sql\n@@ -1 +1 @@\n--- removed SQL comment\n+++ added comment'
        self.assertEqual(changed_lines(text), '-- removed SQL comment\n++ added comment')
        self.assertIn('secrets', candidates('app.py', '@@ -1 +1 @@\n+++ API_TOKEN=abcd1234', []))

    def test_pr16_doctor_referenced_by_codex_test(self):
        for reference in ('+from skim.doctor import run\n+run(config)',
                          '+import doctor', '+import doctor\n+doctor.run(config)', '+from .doctor import run'):
            with self.subTest(reference=reference):
                diff = f"""diff --git a/lib/skim/doctor.py b/lib/skim/doctor.py
--- a/lib/skim/doctor.py
+++ b/lib/skim/doctor.py
@@ -1 +1 @@
-old(config)
+validate_model(config)
diff --git a/tests/test_codex.py b/tests/test_codex.py
--- a/tests/test_codex.py
+++ b/tests/test_codex.py
@@ -1 +1,2 @@
{reference}
"""
                units, tests = parse_diff(diff)
                self.assertEqual(tests, ['tests/test_codex.py'])
                self.assertEqual(units[0]['candidates'], [])

    def test_test_references_and_identifier_boundaries(self):
        path = 'tests/test_other.py'
        for stem in ('config', '__init__', 'run', 'main', 'index', 'utils', 'doctor'):
            for reference in ('+run(config)', '+def __init__(self):', '+rerun(config)',
                              ' from skim.' + stem + ' import check',
                              '-from skim.' + stem + ' import check',
                              '+# doctor.run()', '+value = "doctor"'):
                with self.subTest(stem=stem, reference=reference):
                    diff = '@@ -1 +1 @@\n' + reference
                    self.assertIn('logic_without_tests', candidates(
                        'lib/' + stem + '.py', '+change()', [path], {path: diff}))
            # 흔한 이름도 경로로 대응하는 테스트는 인정한다.
            self.assertNotIn('logic_without_tests', candidates(
                'lib/' + stem + '.py', '+change()', ['tests/test_' + stem + '.py']))
        for reference in ('+from skim.doctor import check', '+import doctor',
                          '+import doctor\n+doctor.run(config)', '+from .doctor import check'):
            self.assertNotIn('logic_without_tests', candidates('lib/doctor.py', '+check()',
                [path], {path: '@@ -1 +1 @@\n' + reference}))
        for stem in ('config', '__init__', 'run', 'main', 'index', 'utils'):
            self.assertIn('logic_without_tests', candidates('lib/' + stem + '.py', '+change()',
                [path], {path: '@@ -1 +1 @@\n+from skim.' + stem + ' import check'}))
        for header in ('--- a/doctor.py', 'rename from doctor.py', '+++ b/doctor.py'):
            self.assertIn('logic_without_tests', candidates('lib/doctor.py', '+check()',
                [path], {path: header + '\n@@ -1 +1 @@\n+other()'}))

    def test_exact_test_filename_only(self):
        for source, test in (
            ('lib/skim/__init__.py', 'tests/__init__.py'),
            ('lib/skim/web.py', 'tests/web/test_anything.py'),
            ('lib/run.py', 'tests/test_run_codex.py'),
        ):
            with self.subTest(source=source, test=test):
                self.assertIn('logic_without_tests', candidates(source, '+change()', [test]))

    def test_module_evidence_requires_import_in_same_test(self):
        path = 'tests/test_other.py'
        for reference in ('+self.db.execute()', '+db = connect(); db.close()',
                          '+db.close()', '+doctor.run()', '+def test_doctoral(self):',
                          '+value = f"{doctor.run()}"'):
            stem = 'db' if 'db' in reference else 'doctor'
            with self.subTest(reference=reference):
                self.assertIn('logic_without_tests', candidates(
                    'lib/' + stem + '.py', '+change()', [path],
                    {path: '@@ -1 +1 @@\n' + reference}))
        self.assertIn('logic_without_tests', candidates(
            'lib/db.py', '+change()', [path, 'tests/test_another.py'],
            {path: '@@ -1 +1 @@\n+db.close()',
             'tests/test_another.py': '@@ -1 +1 @@\n+self.db.execute()'}))

    def test_added_imports_and_test_function_tokens(self):
        path = 'tests/test_codex.py'
        for reference in ('+from skim import doctor', '+from skim import a, doctor',
                          '+import doctor', '+import skim.doctor', '+import skim.doctor as m',
                          '+import os, skim.doctor as m', '+from pkg.doctor import run',
                          '+from .doctor import run',
                          '+def test_doctor_invalid_model_blocks_cached_and_live_probe(self):',
                          '+def test_cached_doctor_probe(self):',
                          '+def test_doctor(self):'):
            with self.subTest(reference=reference):
                diff = ('diff --git a/lib/skim/doctor.py b/lib/skim/doctor.py\n'
                        '@@ -1 +1 @@\n+validate_model(config)\n'
                        'diff --git a/tests/test_codex.py b/tests/test_codex.py\n'
                        '@@ -1 +1 @@\n' + reference)
                units, _ = parse_diff(diff)
                self.assertNotIn('logic_without_tests', units[0]['candidates'])
        for stem in ('Config', 'RUN', 'Utils'):
            self.assertIn('logic_without_tests', candidates(
                'lib/' + stem + '.py', '+change()', [path],
                {path: '@@ -1 +1 @@\n+import ' + stem + '\n+def test_' + stem + '(self):'}))

    def test_partial_hunk_indentation_and_brackets_preserve_test_evidence(self):
        for context in ('        self.assertTrue(x)', ' )', ' ]'):
            for reference in ('    def test_doctor(self):',
                              '        from skim.doctor import run'):
                with self.subTest(context=context, reference=reference):
                    patch = ('@@ -10,2 +10,3 @@\n ' + context + '\n \n+'
                             + reference + '\n')
                    self.assertEqual(tuple(added_test_lines(patch)), (reference,))
                    self.assertNotIn('logic_without_tests', candidates(
                        'lib/skim/doctor.py', '+validate_model(config)',
                        ['tests/test_codex.py'], {'tests/test_codex.py': patch}))

    def test_scanner_quote_state_and_hunk_boundaries(self):
        patch = (
            '@@ -1,4 +1,5 @@\n'
            ' value = "escaped \\" # still a string"\n'
            '+real() # "\n'
            "-old = '''\n"
            '+import doctor\n'
            " text = '''\n"
            '+from skim.doctor import hidden\n'
            '@@ -30 +31 @@\n'
            '+from skim.doctor import run\n'
        )
        self.assertEqual(tuple(added_test_lines(patch)), (
            'real()    ', 'import doctor',
            ' ' * len('from skim.doctor import hidden'),
            'from skim.doctor import run'))

    def test_real_d16_diff_has_doctor_test_evidence(self):
        # /tmp/d16.txt: keep the original patch inline so this needs no external file.
        diff = (
            'diff --git a/lib/skim/analyze/codex.py b/lib/skim/analyze/codex.py\n'
            'index 316c484..ef94e3f 100644\n'
            '--- a/lib/skim/analyze/codex.py\n'
            '+++ b/lib/skim/analyze/codex.py\n'
            '@@ -277,8 +277,32 @@ def metadata(value):\n'
            '                 found = metadata(json.loads(line))\n'
            '                 if found:\n'
            '                     return found\n'
            "-        match = re.search(r'^\\s*model:\\s*(\\S+)\\s*$', stderr, re.MULTILINE)\n"
            '-        return match.group(1) if match and valid_model(match.group(1)) else None\n'
            '+        # stderr의 경고·응답 대신 Codex 배너 내부에서만 모델을 읽는다.\n'
            '+        in_banner = False\n'
            '+        opened = False\n'
            '+        model = None\n'
            '+        banner_lines = 0\n'
            '+        for line in stderr.splitlines():\n'
            '+            line = line.strip()\n'
            '+            if not in_banner:\n'
            "+                if re.fullmatch(r'(?:\\[[^\\]\\r\\n]+\\]\\s+)?OpenAI Codex(?:\\s+.*)?', line):\n"
            '+                    in_banner = True\n'
            '+                continue\n'
            '+            banner_lines += 1\n'
            '+            # 닫는 구분선과 정상 키-값 행을 확인한 배너만 신뢰한다.\n'
            '+            if banner_lines > 32:\n'
            '+                break\n'
            "+            if line == '--------':\n"
            '+                if opened or banner_lines > 1:\n'
            '+                    return model\n'
            '+                opened = True\n'
            '+                continue\n'
            "+            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9 _-]*:\\s*.*', line):\n"
            '+                break\n'
            "+            match = re.fullmatch(r'model:\\s*(\\S+)', line)\n"
            '+            if match and valid_model(match.group(1)):\n'
            '+                model = match.group(1)\n'
            '+        return None\n'
            ' \n'
            '     @staticmethod\n'
            '     def _check_events(stdout):\n'
            'diff --git a/lib/skim/doctor.py b/lib/skim/doctor.py\n'
            'index 4ba8bc3..67854cf 100644\n'
            '--- a/lib/skim/doctor.py\n'
            '+++ b/lib/skim/doctor.py\n'
            '@@ -42,8 +42,10 @@ def report(ok, name, detail):\n'
            "             report(False, label, 'Codex 분석 차단: 선행 검사 실패 또는 CLI 없음')\n"
            '             ready = False\n'
            '             continue\n'
            "-        if label == 'probe' and probe and not model_ok:\n"
            "-            report(False, label, '모델 설정을 수정한 뒤 skim doctor --probe를 실행하세요.')\n"
            "+        if label == 'probe' and not model_ok:\n"
            "+            detail = ('모델 설정 오류로 probe를 실행하지 않음.' if probe else\n"
            "+                      '모델 설정 오류로 캐시된 probe를 검증하지 않음.')\n"
            "+            report(False, label, detail + ' 모델 설정을 수정한 뒤 skim doctor --probe를 실행하세요.')\n"
            '             ready = False\n'
            '             continue\n'
            '         try:\n'
            'diff --git a/progress.md b/progress.md\n'
            'index 2cbecbd..f01d2a9 100644\n'
            '--- a/progress.md\n'
            '+++ b/progress.md\n'
            '@@ -135,3 +135,15 @@\n'
            ' - 모델은 Config.load에서 한 번 해석해 분석기에 전달하며 실행 중 설정 파일·환경 변경으로 분석이 조용히 중단되지 않게 했다. doctor는 설정 JSON·객체·권한·모델 오류를 model 실패로 표시하고 나머지 진단을 계속한다.\n'
            ' - 모델 식별자 검증, 감지 모델 고정 의도 주석, 실행 전 모델·버전 변경 차단과 probe 우선 안내를 보완했다. 웹의 실제 모델·CLI 버전 표시와 설정 손상 회귀를 검증했다.\n'
            ' - fake 기반 unittest 129개와 git diff --check가 통과했다. 실제 LLM·네트워크 호출, GitHub 쓰기와 커밋은 하지 않았다.\n'
            '+\n'
            '+## Codex 배너 감지·doctor 출력 수정 — 2026-10-31\n'
            '+\n'
            '+- stderr 모델 감지를 OpenAI Codex 헤더 이후부터 다음 -------- 구분선 이전으로 제한하고 JSON 이벤트 모델 우선순위를 유지했다.\n'
            '+- 모델 설정 오류 시 --probe 유무와 관계없이 probe를 실패로 표시하고 검사를 실행하지 않는다. 배너 경계·잘못된 모델·캐시가 있는 진단의 회귀 테스트를 추가했다.\n'
            '+- fake 기반 unittest 132개와 git diff --check가 통과했다. 실제 LLM·네트워크 호출, GitHub 쓰기와 커밋은 하지 않았다.\n'
            '+\n'
            '+## Codex 배너 경계 리뷰 수정 — 2026-10-31\n'
            '+\n'
            '+- 헤더 다음 여는 구분선과 닫는 구분선을 구별하고 timestamp 접두사를 허용했다. 닫는 구분선 확인, 키-값 행 검사와 32행 상한으로 배너 밖 모델 오탐을 제한했다. 일반 doctor의 모델 오류는 캐시된 probe 검증 생략으로 안내한다.\n'
            '+- 직전 배너 감지 수정은 실제 출력으로 검증하지 않았다. 이번 fixture도 리뷰의 0.165.0 형식을 재구성한 합성 자료이며 실제 stderr 캡처가 아니다. 실제 CLI 출력 대조는 미검증으로 남긴다.\n'
            '+- fake/fixture만으로 unittest 132개와 git diff --check가 통과했다. 실제 LLM·네트워크 호출, GitHub 쓰기와 커밋은 하지 않았다.\n'
            'diff --git a/tests/fixtures/codex-banner-0.165.0.README.md b/tests/fixtures/codex-banner-0.165.0.README.md\n'
            'new file mode 100644\n'
            'index 0000000..107f55d\n'
            '--- /dev/null\n'
            '+++ b/tests/fixtures/codex-banner-0.165.0.README.md\n'
            '@@ -0,0 +1,3 @@\n'
            '+# Codex 배너 회귀 자료\n'
            '+\n'
            '+실제 CLI 캡처가 아니라 리뷰에 제시된 0.165.0 배너 구조를 재구성한 합성 fixture다. 경로와 모델은 테스트용이며 비밀값은 포함하지 않는다. 실제 0.165.0 stderr 대조는 아직 수행하지 않았다.\n'
            'diff --git a/tests/fixtures/codex-banner-0.165.0.stderr b/tests/fixtures/codex-banner-0.165.0.stderr\n'
            'new file mode 100644\n'
            'index 0000000..a186fa9\n'
            '--- /dev/null\n'
            '+++ b/tests/fixtures/codex-banner-0.165.0.stderr\n'
            '@@ -0,0 +1,7 @@\n'
            '+OpenAI Codex v0.165.0 (research preview)\n'
            '+--------\n'
            '+workdir: /redacted/workdir\n'
            '+model: gpt-5.4\n'
            '+provider: openai\n'
            '+--------\n'
            '+model: outside-banner\n'
            'diff --git a/tests/test_codex.py b/tests/test_codex.py\n'
            'index d995fff..3ed8af9 100644\n'
            '--- a/tests/test_codex.py\n'
            '+++ b/tests/test_codex.py\n'
            '@@ -325,7 +325,7 @@ def test_probe_records_are_separate_per_model(self):\n'
            '         self.adapter.check_probe()\n'
            ' \n'
            '     def test_model_banner_cached_without_extra_execution(self):\n'
            "-        self.stderr = 'OpenAI Codex\\nmodel: gpt-5.4\\nprovider: openai\\n'\n"
            "+        self.stderr = (Path(__file__).parent / 'fixtures/codex-banner-0.165.0.stderr').read_text()\n"
            '         self.adapter.probe()\n'
            "         self.assertEqual(self.adapter.model, 'gpt-5.4')\n"
            "         self.assertEqual(json.loads(self.adapter.probe_path.read_text())['model'], 'gpt-5.4')\n"
            '@@ -338,8 +338,48 @@ def test_model_banner_cached_without_extra_execution(self):\n'
            '         command = self.calls[-1][0]\n'
            "         self.assertEqual(command[command.index('--model') + 1], 'gpt-5.4')\n"
            ' \n'
            '+    def test_model_banner_boundaries(self):\n'
            '+        cases = (\n'
            "+            ('model: stray\\n', None),\n"
            "+            ('model: stray\\nOpenAI Codex v0.165.0\\nmodel: actual\\n--------\\nmodel: later\\n', 'actual'),\n"
            "+            ('OpenAI Codex\\n--------\\nmodel: later\\n', None),\n"
            "+            ('OpenAI Codex\\nprovider: openai\\n--------\\nmodel: later\\n', None),\n"
            "+            ('warning: OpenAI Codex\\nmodel: stray\\n', None),\n"
            "+            ('OpenAI Codex\\nmodel: unknown\\n--------\\nmodel: later\\n', None),\n"
            "+            ('OpenAI Codex\\nmodel: two words\\n--------\\n', None),\n"
            "+            ('  OpenAI Codex v0.165.0  \\n  model: actual  \\n--------\\n', 'actual'),\n"
            "+            ('OpenAI Codex v0.165.0\\n--------\\nmodel: actual\\n--------\\nmodel: later\\n', 'actual'),\n"
            "+            ('[2026-10-31T00:00:00Z] OpenAI Codex v0.165.0\\n--------\\nmodel: actual\\n--------\\n', 'actual'),\n"
            "+            ('OpenAI Codex\\n--------\\nprovider: openai\\n--------\\nmodel: later\\n', None),\n"
            "+            ('OpenAI Codex\\n--------\\nmodel: actual\\nresponse text\\nmodel: later\\n--------\\n', None),\n"
            "+            ('OpenAI Codex\\nmodel: actual\\n', None),\n"
            "+            ('OpenAI Codex\\n--------\\n' + 'provider: openai\\n' * 32 + 'model: later\\n--------\\n', None),\n"
            '+        )\n'
            '+        for stderr, expected in cases:\n'
            '+            with self.subTest(stderr=stderr):\n'
            "+                self.assertEqual(CodexAdapter._output_model('', stderr), expected)\n"
            '+\n'
            '+    def test_doctor_invalid_model_blocks_cached_and_live_probe(self):\n'
            '+        self.adapter.probe()\n'
            "+        config = Config(self.root / 'config', self.adapter.data_dir)\n"
            '+        for probe in (False, True):\n'
            '+            with self.subTest(probe=probe):\n'
            '+                out = io.StringIO()\n'
            "+                with patch.object(Config, 'doctor_codex_model', side_effect=ValueError), \\\n"
            "+                        patch.object(self.adapter, 'check_probe') as check_probe, \\\n"
            "+                        patch.object(self.adapter, 'probe') as live_probe, \\\n"
            '+                        contextlib.redirect_stdout(out):\n'
            "+                    self.assertEqual(run(config, self.runner, lambda _: '/fake',\n"
            '+                                         self.adapter, probe=probe), 1)\n'
            "+                self.assertIn('실패: model', out.getvalue())\n"
            "+                self.assertIn('실패: probe', out.getvalue())\n"
            "+                self.assertNotIn('정상: probe', out.getvalue())\n"
            "+                self.assertIn('probe를 실행하지 않음' if probe else '캐시된 probe를 검증하지 않음', out.getvalue())\n"
            '+                check_probe.assert_not_called()\n'
            '+                live_probe.assert_not_called()\n'
            '+\n'
            '     def test_model_event_precedes_banner_and_unknown_fallback(self):\n'
            "-        self.stderr = 'model: banner-model\\n'\n"
            "+        self.stderr = 'OpenAI Codex v0.165.0\\nmodel: banner-model\\n--------\\n'\n"
            "         self.events = [{'type': 'thread.started', 'metadata': {'model': 'event-model'}}]\n"
            '         self.adapter.probe()\n'
            "         self.assertEqual(self.adapter.model, 'event-model')\n"
            '@@ -363,7 +403,7 @@ def test_doctor_reports_one_known_model_after_checks(self):\n'
            '             with self.subTest(configured=configured, probe=probe):\n'
            "                 config = Config(self.root / 'config', self.adapter.data_dir,\n"
            "                                 codex_model='configured' if configured else None)\n"
            "-                self.stderr = 'model: detected\\n'\n"
            "+                self.stderr = 'OpenAI Codex v0.165.0\\nmodel: detected\\n--------\\n'\n"
            '                 if not configured and not probe:\n'
            '                     self.adapter.probe()\n'
            '                 out = io.StringIO()\n'
        )
        units, _ = parse_diff(diff)
        doctor_units = [unit for unit in units if unit['file_path'] == 'lib/skim/doctor.py']
        self.assertTrue(doctor_units)
        for unit in doctor_units:
            self.assertNotIn('logic_without_tests', unit['candidates'])

    def test_fstring_tokens_are_not_test_evidence(self):
        lines = tuple(added_test_lines(
            '@@ -1 +1 @@\n+value = f"{doctor.run()}" # doctor'))
        self.assertEqual(len(lines), 1)
        self.assertNotIn('doctor', lines[0])

    def test_multiline_strings_are_not_test_evidence(self):
        for reference in ('    """\n    import doctor\n    """',
                          "SRC = '''\nfrom skim.doctor import run\n'''",
                          "SRC = '''\nfrom skim.doctor import run"):
            with self.subTest(reference=reference):
                diff = ('diff --git a/lib/skim/doctor.py b/lib/skim/doctor.py\n'
                        '@@ -1 +1 @@\n+validate_model(config)\n'
                        'diff --git a/tests/test_codex.py b/tests/test_codex.py\n'
                        '@@ -1 +1 @@\n' + '\n'.join('+' + line for line in reference.splitlines()))
                units, _ = parse_diff(diff)
                self.assertIn('logic_without_tests', units[0]['candidates'])

    def test_context_opened_multiline_strings_are_not_test_evidence(self):
        for quote in ('"""', "'''"):
            with self.subTest(quote=quote):
                diff = ('diff --git a/lib/skim/doctor.py b/lib/skim/doctor.py\n'
                        '@@ -1 +1 @@\n+validate_model(config)\n'
                        'diff --git a/tests/test_codex.py b/tests/test_codex.py\n'
                        '@@ -1,2 +1,3 @@\n SRC = ' + quote + '\n'
                        '+from skim.doctor import run\n ' + quote + '\n')
                units, _ = parse_diff(diff)
                self.assertIn('logic_without_tests', units[0]['candidates'])

    def test_context_lines_are_not_emitted_as_test_evidence(self):
        lines = tuple(added_test_lines(
            '@@ -1,2 +1,3 @@\n import doctor\n-removed()\n+added()\n kept()\n'))
        self.assertEqual(lines, ('added()',))

    def test_short_stems_require_import_or_filename(self):
        path = 'tests/test_other.py'
        for stem in ('db', 'api', 'web'):
            with self.subTest(stem=stem):
                self.assertIn('logic_without_tests', candidates(
                    'lib/' + stem + '.py', '+change()', [path],
                    {path: '@@ -1 +1 @@\n+def test_' + stem + '(self):'}))
                self.assertNotIn('logic_without_tests', candidates(
                    'lib/' + stem + '.py', '+change()', [path],
                    {path: '@@ -1 +1 @@\n+import skim.' + stem}))
                self.assertNotIn('logic_without_tests', candidates(
                    'lib/' + stem + '.py', '+change()', ['tests/test_' + stem + '.py']))

    def test_hard_coded_literal_keeps_name_and_hides_value(self):
        from skim.analyze.units import REDACTED_LITERAL, sanitize
        value = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        cases = {
            f'+AWS_SECRET_ACCESS_KEY = "{value}"': '+AWS_SECRET_ACCESS_KEY = ' + REDACTED_LITERAL,
            f"  export DB_PASSWORD='{value}';": '  export DB_PASSWORD= ' + REDACTED_LITERAL,
            f'+    "api_key": "{value}",': '+    "api_key": ' + REDACTED_LITERAL,
            f'+  - password: "{value}"': '+  - password: ' + REDACTED_LITERAL,
            f'+self.token = b"{value}"': '+self.token = ' + REDACTED_LITERAL,
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(sanitize(line), expected)
        # A known token format inside the literal is replaced first and stays hidden.
        token = 'ghp_' + 'a' * 36
        cleaned = sanitize(f'+GITHUB_TOKEN = "{token}"')
        self.assertEqual(cleaned, '+GITHUB_TOKEN = ' + REDACTED_LITERAL)
        self.assertNotIn(token, cleaned)

    def test_non_literal_secret_assignments_keep_the_plain_marker(self):
        from skim.analyze.units import REDACTED, REDACTED_LITERAL, sanitize
        value = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        for line in ('+API_TOKEN = os.environ["API_TOKEN"]',
                     '+API_TOKEN = os.environ.get("API_TOKEN", "")',
                     '+password = config.db_password',
                     '+token = secrets.token_urlsafe(32)',
                     '+  password: "${DB_PASSWORD}"',
                     '+  token: "{{ vault_token }}"',
                     '+export API_TOKEN="$CI_TOKEN"',
                     '+api_key = "<your-key>"',
                     '+password = ""',
                     f'+token = f"{value}{{suffix}}"',
                     f'+token = "{value}" + salt',
                     f'+SECRET_KEY={value}',
                     f'+connect(password="{value}")',
                     '+def check(token: str, expected):',
                     f'+if password == "{value}":'):
            with self.subTest(line=line):
                cleaned = sanitize(line)
                self.assertIn(REDACTED, cleaned)
                self.assertNotIn(REDACTED_LITERAL, cleaned)
                self.assertNotIn(value, cleaned)

    def test_literal_marker_never_keeps_text_that_may_be_a_value(self):
        from skim.analyze.units import REDACTED_LITERAL, SECRET_ASSIGNMENT, SECRET_VALUE, sanitize
        value = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        # Here the matched "name" is not a declaration at the start of the line, so it is dropped too.
        for line in (f'+headers = {{"x": 1}}; token = "{value}"',
                     f'+url = "https://example.test/?access_token={value}"',
                     f'+blob = "x"; abcTOKENxyz= "{value}"',
                     '+' + 'x' * 90 + f'_token = "{value}"'):
            with self.subTest(line=line):
                cleaned = sanitize(line)
                self.assertNotIn(value, cleaned)
                self.assertNotIn('abcTOKENxyz', cleaned)
                self.assertNotIn('x' * 90, cleaned)
        diff = (f'+AWS_SECRET_ACCESS_KEY = "{value}"\n+aws_key = "AKIAABCDEFGHIJKLMNOP"\n'
                f'+password: \'{value}\'\n+token = "sk_live_{"a" * 24}"\n')
        cleaned = sanitize(diff)
        self.assertNotIn(value, cleaned)
        self.assertIsNone(SECRET_VALUE.search(cleaned))
        # Whatever still looks like an assignment after cleaning holds only a marker.
        for match in SECRET_ASSIGNMENT.finditer(cleaned):
            self.assertEqual(match['value'].strip(), REDACTED_LITERAL)
        self.assertEqual(sanitize(cleaned), cleaned)

    def test_fx_secret_unit_is_redacted_and_names_the_literal(self):
        from skim.analyze.units import REDACTED_LITERAL
        value = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        unit = self.unit('lib/app/settings.py', '@@ -1,3 +1,4 @@\n import os\n'
                         f'+AWS_SECRET_ACCESS_KEY = "{value}"\n REGION = os.environ.get("REGION", "ap-northeast-2")\n')
        self.assertTrue(unit['redacted'])
        self.assertIn('+AWS_SECRET_ACCESS_KEY = ' + REDACTED_LITERAL, unit['diff'])
        self.assertNotIn(value, unit['diff'])
        self.assertIn('secrets', unit['content_hits'])
        self.assertEqual(unit['hard_floors'], [])


ORDER_DIFF = '''diff --git a/app/order_core.py b/app/order_core.py
--- a/app/order_core.py
+++ b/app/order_core.py
@@ -1,2 +1,2 @@
 def calculate_order(order):
-    return order.subtotal
+    return order.subtotal + order.delivery_fee
diff --git a/app/order_routes.py b/app/order_routes.py
--- a/app/order_routes.py
+++ b/app/order_routes.py
@@ -196,2 +196,2 @@
 def order_total(order):
-    return order.subtotal
+    return calculate_order(order)
'''


def hunk(path, body, start=1):
    return (f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n'
            f'@@ -{start},2 +{start},2 @@\n' + body)


class CallReferenceTests(unittest.TestCase):
    def calls(self, diff):
        return {unit['file_path']: [(call['name'], call['change_id']) for call in unit['calls_changed']]
                for unit in parse_diff(diff)[0]}

    def test_added_call_points_at_the_changed_definition(self):
        units, _ = parse_diff(ORDER_DIFF)
        core, routes = units
        self.assertEqual(core['calls_changed'], [])
        self.assertEqual(routes['calls_changed'], [dict(
            name='calculate_order', change_id='h1', file_path='app/order_core.py',
            excerpt='-    return order.subtotal\n+    return order.subtotal + order.delivery_fee',
            excerpt_truncated=False)])
        # The signal is context only: it adds no candidate, floor, or other rule signal.
        for key in ('location_hints', 'kind_signals', 'content_hits', 'hard_floors', 'mechanical_hints'):
            self.assertEqual(routes[key], [])
        self.assertEqual(routes['candidates'], ['logic_without_tests'])

    def test_definition_forms(self):
        from skim.analyze.units import definitions
        cases = {
            '@@ -1 +1 @@ def from_header(a):\n-    x\n+    y\n': ['from_header'],
            '@@ -1 +1 @@\n+async def added_def(a):\n+    pass\n': ['added_def'],
            '@@ -1 +1 @@\n function jsFunc(a) {\n-  a\n+  b\n': ['jsFunc'],
            '@@ -1 +1 @@\n+const arrowFunc = async (a) => a\n': ['arrowFunc'],
            '@@ -1 +1 @@\n func (s *Server) GoMethod(a int) {\n+  b()\n': ['GoMethod'],
            # The change sits under the second definition; the first is untouched context.
            '@@ -1 +1 @@\n def untouched(a):\n     pass\n def touched(a):\n-    x\n+    y\n': ['touched'],
            '@@ -1 +1 @@\n+value = 1\n def later(a):\n     pass\n': [],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(definitions(text)[1], expected)

    def test_common_short_and_ambiguous_names_are_not_linked(self):
        for name in ('get', 'run', 'validate', 'handler', '__init__', 'test_total', 'abc'):
            with self.subTest(name=name):
                diff = (hunk('lib/core.py', f' def {name}(x):\n-    return 1\n+    return 2\n')
                        + hunk('lib/use.py', f' def use(x):\n-    return 1\n+    return {name}(x)\n'))
                self.assertEqual(self.calls(diff)['lib/use.py'], [])
        # The same name changes in two files, so the call target is unknown.
        diff = (hunk('lib/a.py', ' def compute_total(x):\n-    return 1\n+    return 2\n')
                + hunk('lib/b.py', ' def compute_total(x):\n-    return 1\n+    return 3\n')
                + hunk('lib/use.py', ' def use(x):\n-    return 1\n+    return compute_total(x)\n'))
        self.assertEqual(self.calls(diff)['lib/use.py'], [])

    def test_only_added_code_calls_to_changed_definitions_are_linked(self):
        core = hunk('lib/core.py', ' def compute_total(x):\n-    return 1\n+    return 2\n')
        for body in (' def use(x):\n-    return compute_total(x)\n+    return 1\n',
                     ' def use(x):\n     compute_total(x)\n+    return 1\n',
                     ' def use(x):\n+    # compute_total(x) is called elsewhere\n',
                     ' def use(x):\n+    log("compute_total(x)")\n',
                     ' def use(x):\n+    return compute_total\n',
                     ' def use(x):\n+    return other_total(x)\n',
                     # A local definition with the same name shadows the other file.
                     ' def compute_total(x):\n+    return compute_total(x - 1)\n'):
            with self.subTest(body=body):
                self.assertEqual(self.calls(core + hunk('lib/use.py', body))['lib/use.py'], [])
        # Unchanged definitions that only appear as context are not call targets.
        context_only = hunk('lib/core.py', '+LIMIT = 3\n def compute_total(x):\n     return 1\n')
        caller = hunk('lib/use.py', ' def use(x):\n+    return compute_total(x)\n')
        self.assertEqual(self.calls(context_only + caller)['lib/use.py'], [])

    def test_attribute_calls_are_not_linked(self):
        core = hunk('lib/core.py', ' def submit_order(x):\n-    return 1\n+    return 2\n def serve(x):\n-    a\n+    b\n')
        caller = hunk('lib/use.py', ' def use(x):\n+    checkout.submit_order(x)\n+    server.serve(x)\n+    $client->submit_order(x)\n+    Client::submit_order(x)\n+    client?.submit_order(x)\n')
        self.assertEqual(self.calls(core + caller)['lib/use.py'], [])
        bare = hunk('lib/use.py', ' def use(x):\n+    serve(x)\n')
        self.assertEqual(self.calls(core + bare)['lib/use.py'], [('serve', 'h1')])

    def test_tests_docs_and_excluded_files_are_not_linked(self):
        core = hunk('lib/core.py', ' def compute_total(x):\n-    return 1\n+    return 2\n')
        call = ' def use(x):\n+    return compute_total(x)\n'
        for path in ('tests/test_core.py', 'docs/guide.md', '.env'):
            with self.subTest(path=path):
                self.assertEqual(self.calls(core + hunk(path, call))[path], [])
        # A fake defined in a test is not the target of a product call.
        fake = hunk('tests/test_core.py', ' def compute_total(x):\n-    return 1\n+    return 2\n')
        self.assertEqual(self.calls(fake + hunk('lib/use.py', call))['lib/use.py'], [])

    def test_link_count_and_excerpt_are_capped(self):
        names = ['first_step', 'second_step', 'third_step', 'fourth_step']
        long_line = '+    return ' + ' + '.join(['value'] * 80) + '\n'
        core = ''.join(hunk(f'lib/core{index}.py', f' def {name}(x):\n-    return 1\n' + long_line)
                       for index, name in enumerate(names))
        caller = hunk('lib/use.py', ' def use(x):\n' + ''.join(f'+    {name}(x)\n' for name in names))
        linked = [unit for unit in parse_diff(core + caller)[0] if unit['file_path'] == 'lib/use.py'][0]['calls_changed']
        self.assertEqual([call['name'] for call in linked], names[:3])
        for call in linked:
            self.assertLessEqual(len(call['excerpt'].encode()), 240)
            self.assertTrue(call['excerpt_truncated'])

    def test_excerpt_of_the_target_is_sanitized(self):
        value = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        core = hunk('lib/core.py', f' def load_client(x):\n-    return 1\n+    api_token = "{value}"\n')
        caller = hunk('lib/use.py', ' def use(x):\n+    return load_client(x)\n')
        linked = parse_diff(core + caller)[0][1]['calls_changed']
        self.assertEqual([call['name'] for call in linked], ['load_client'])
        self.assertNotIn(value, linked[0]['excerpt'])


    def test_comments_and_duplicate_definitions_are_not_targets(self):
        caller = hunk('lib/use.py', ' def use(x):\n+    return compute_total(x)\n')
        for body in (
            '+# def compute_total(x):\n+value = 2\n',
            '+// function compute_total(x) {\n+value = 2\n',
            ' /*\n function compute_total(x) {\n-    return 1;\n+    return 2;\n */\n',
            ' class A:\n     def compute_total(x):\n-        return 1\n+        return 2\n'
            ' class B:\n     def compute_total(x):\n-        return 1\n+        return 3\n',
        ):
            with self.subTest(body=body):
                self.assertEqual(self.calls(hunk('lib/core.py', body) + caller)['lib/use.py'], [])
        core = (hunk('lib/core.py', ' def compute_total(x):\n-    return 1\n+    return 2\n')
                + '@@ -20,2 +20,2 @@\n def compute_total(x):\n-    return 1\n+    return 3\n')
        self.assertEqual(self.calls(core + caller)['lib/use.py'], [])

    def test_excerpt_contains_only_target_function_changes(self):
        core = hunk('lib/core.py',
                    ' def charge_card(x):\n-    charge(1)\n+    charge(999)\n'
                    ' def display_name(x):\n-    return x.old_name\n+    return x.new_name\n'
                    ' def other_work(x):\n-    erase(1)\n+    erase(2)\n')
        caller = hunk('lib/use.py', ' def use(x):\n+    return display_name(x)\n')
        call = parse_diff(core + caller)[0][-1]['calls_changed'][0]
        self.assertEqual(call['excerpt'], '-    return x.old_name\n+    return x.new_name')
        self.assertFalse(call['excerpt_truncated'])

    def test_excerpt_handles_header_and_replaced_definition(self):
        caller = hunk('lib/use.py', ' def use(x):\n+    return compute_total(x)\n')
        for body in (
            '@@ -1,2 +1,2 @@ def compute_total(x):\n-    return 1\n+    return 2\n',
            '@@ -1,2 +1,2 @@\n-def compute_total(old):\n-    return 1\n'
            '+def compute_total(new):\n+    return 2\n',
        ):
            core = 'diff --git a/lib/core.py b/lib/core.py\n--- a/lib/core.py\n+++ b/lib/core.py\n' + body
            call = parse_diff(core + caller)[0][-1]['calls_changed'][0]
            self.assertEqual(call['excerpt'], '\n'.join(
                line for line in body.splitlines() if line.startswith(('+', '-'))))
        unknown = hunk('lib/core.py', '-    return 1\n+    return 2\n')
        self.assertEqual(self.calls(unknown + caller)['lib/use.py'], [])

    def test_calls_survive_wire_overview_and_synthesis(self):
        from skim.analyze.protocol import wire, SYSTEM
        from skim.analyze.context import overview_input, synthesis_input
        units, _ = parse_diff(ORDER_DIFF)
        expected = units[1]['calls_changed']
        self.assertEqual(wire(units[1])['calls_changed'], expected)
        overview = overview_input(units, {})
        sample = next(row for row in overview['samples'] if row['change_id'] == 'h2')
        self.assertEqual(sample['calls_changed'], expected)
        items = [dict(change_id=u['change_id'], importance='mid', reason='검토',
                      risk_flags=[], behavior='changed') for u in units]
        synthesis = synthesis_input(units, {}, items, {})
        row = next(row for row in synthesis['items'] if row['change_id'] == 'h2')
        self.assertEqual(row['calls_changed'], [
            {key: call[key] for key in ('name', 'change_id', 'file_path')} for call in expected])
        self.assertIn('이름이 같은 정의가 같은 PR에서 바뀐 호출 후보', SYSTEM)
        self.assertIn('calls_changed가 있다는 사실만으로 high나 위험 플래그를 주지 마라', SYSTEM)


class SecretStringContextRegressionTests(unittest.TestCase):
    def test_multiline_string_names_and_values_are_redacted(self):
        from skim.analyze.units import sanitize, REDACTED
        openers = ('"' * 3, "'" * 3, '`', '<<EOF', '<<-EOF', "<<'EOF'",
                   '|', '>', '|-', '>2', '"continued' + chr(92), "'continued" + chr(92))
        for opener in openers:
            for value in ('"hidden"', '[민감 정보 제외: 직접 쓴 문자열 값]'):
                with self.subTest(opener=opener, value=value):
                    source = '+payload: ' + opener + '\n+  opaqueTOKENfragment=' + value + '\n'
                    outputs = (sanitize(source), parse_diff(hunk('lib/config.py', source))[0][0]['diff'])
                    for cleaned in outputs:
                        self.assertNotIn('opaqueTOKENfragment', cleaned)
                        self.assertNotIn('hidden', cleaned)
                        self.assertIn('+  ' + REDACTED, cleaned)

    def test_fx_secret_fixture_keeps_literal_name(self):
        path = Path(__file__).resolve().parents[1] / 'eval/skim-ko-6/diffs/fx-secret.diff'
        source = path.read_text()
        units, _ = parse_diff(source)
        cleaned = '\n'.join(unit['diff'] for unit in units)
        self.assertIn('AWS_SECRET_ACCESS_KEY = [민감 정보 제외: 직접 쓴 문자열 값]', cleaned)
        for line in source.splitlines():
            if line.startswith('+AWS_SECRET_ACCESS_KEY'):
                self.assertNotIn(line.split('"')[1], cleaned)

    def test_spoofed_marker_does_not_bypass_name_checks(self):
        from skim.analyze.units import sanitize, REDACTED_LITERAL
        for prefix in ('+blob = "prefix ', '+blob = "x"; ', '+' + 'x' * 90):
            cleaned = sanitize(prefix + 'opaqueTOKENfragment=' + REDACTED_LITERAL)
            self.assertNotIn('opaqueTOKENfragment', cleaned)
