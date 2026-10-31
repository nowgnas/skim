"""경로와 실제 변경 줄에 근거한 위험 후보 회귀 검사."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from skim.analyze.units import candidates, parse_diff


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
+password = user.password
+token = get_token()
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
