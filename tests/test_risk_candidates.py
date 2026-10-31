"""경로와 실제 변경 줄에 근거한 위험 후보 회귀 검사."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.analyze.units import candidates, changed_lines, parse_diff


class RiskCandidatesTests(unittest.TestCase):
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
            ('schema.sql', 'db_schema_migration'),
            ('prisma/schema.prisma', 'db_schema_migration'),
            ('db/schema.rb', 'db_schema_migration'),
            ('auth/login.py', 'auth'), ('middleware/auth_guard.py', 'auth'),
            ('.github/workflows/ci.yml', 'ci_workflow'),
            ('package.json', 'dependency'), ('.env', 'config'), ('.env', 'secrets'),
            ('settings.py', 'config'), ('config/app.json', 'config'),
            ('secrets/value.txt', 'secrets'), ('private.pem', 'secrets'),
            ('payments/client.py', 'payments')):
            with self.subTest(path=path, flag=flag):
                self.assertIn(flag, candidates(path, '', []))

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
            ('app.py', 'max_tokens=100'),
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
