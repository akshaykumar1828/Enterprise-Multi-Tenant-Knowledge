"""Scheduled backups: names, retention, archive check, secrets, task scripts.

Real runs dump the application database (read-only pg_dump) into temporary folders,
which are deleted afterwards. No scheduled task is ever registered (only -WhatIf).
No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_backup_schedule -v
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tests.helpers import new_password  # noqa: I001  (loads .env for the database settings)

from src.rag import backup
from src.rag import backup_schedule as schedule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = backup.app_database()
SAFE_DB = DB.lower()


def touch(path: Path, content: bytes = b"old backup") -> Path:
    path.write_bytes(content)
    return path


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rag-backup-schedule-"))
        self.addCleanup(shutil.rmtree, self.dir, True)


class NamingTests(unittest.TestCase):
    def test_backup_name_is_timestamped_and_safe(self):
        name = schedule.backup_name("enterprise_rag", datetime(2026, 9, 30, 2, 30, 5))
        self.assertEqual(name, "enterprise_rag-20260930-023005.dump")
        self.assertTrue(schedule.backup_pattern("enterprise_rag").fullmatch(name))
        for database in ("../../etc", "C:\\Windows", "Enterprise RAG", "a/b\\c:d"):
            with self.subTest(database=database):
                name = schedule.backup_name(database, datetime(2026, 1, 2, 3, 4, 5))
                self.assertRegex(name, r"^[a-z0-9_]+-20260102-030405\.dump$")
                self.assertEqual(Path(name).name, name)  # never a path
        with self.assertRaises(backup.BackupError):
            schedule.backup_name("///", datetime.now())

    def test_pattern_accepts_only_exact_backup_names(self):
        pattern = schedule.backup_pattern("enterprise_rag")
        for name in ("enterprise_rag-20260930-023005.dump",):
            self.assertTrue(pattern.fullmatch(name))
        for name in ("enterprise_rag-20260930-023005.dump.invalid", "enterprise_rag-20260930-023005.dump.bak",
                     "enterprise_rag-latest.dump", "enterprise_rag-2026-09-30.dump", "other_db-20260930-023005.dump",
                     "xenterprise_rag-20260930-023005.dump", "enterprise_rag-20260930-023005.DUMP",
                     "enterprise_rag-20260930-023005.dump ", "notes.txt", "enterprise_rag_restore_verify_1234abcd"):
            with self.subTest(name=name):
                self.assertIsNone(pattern.fullmatch(name))


class RetentionTests(TempDirTestCase):
    def seed(self) -> tuple[list[Path], list[Path]]:
        backups = [touch(self.dir / f"{SAFE_DB}-2026090{day}-020000.dump") for day in range(1, 7)]
        unrelated = [
            touch(self.dir / "notes.txt"), touch(self.dir / "important.dump"),
            touch(self.dir / f"{SAFE_DB}-20200101-000000.dump.invalid"),
            touch(self.dir / f"{SAFE_DB}-20200101-000000.dump.bak"),
            touch(self.dir / f"{SAFE_DB}-latest.dump"),
            touch(self.dir / f"other_db-20200101-000000.dump"),
        ]
        folder = self.dir / f"{SAFE_DB}-20200101-000000.dump"  # a folder named like a backup
        folder.mkdir()
        nested = self.dir / "archive"
        nested.mkdir()
        unrelated += [folder, touch(nested / f"{SAFE_DB}-20200101-000000.dump")]  # not directly in the folder
        return backups, unrelated

    def test_keeps_the_newest_and_deletes_only_matching_backups(self):
        backups, unrelated = self.seed()
        result = schedule.apply_retention(self.dir, DB, keep=3, protect=backups[-1])
        self.assertEqual([p.name for p in result.removed], [p.name for p in backups[:3]])
        self.assertEqual([p.name for p in result.kept], [p.name for p in backups[3:]])
        for path in backups[3:] + unrelated:
            self.assertTrue(path.exists(), f"{path.name} must not be deleted")
        for path in backups[:3]:
            self.assertFalse(path.exists())

    def test_the_new_backup_is_never_deleted_even_if_it_sorts_oldest(self):
        backups, _ = self.seed()
        clock_skewed = touch(self.dir / f"{SAFE_DB}-20250101-000000.dump")  # e.g. the clock was wrong
        result = schedule.apply_retention(self.dir, DB, keep=2, protect=clock_skewed)
        self.assertTrue(clock_skewed.exists())
        self.assertIn(clock_skewed.name, [p.name for p in result.kept])  # names: Windows may spell the temp path two ways
        self.assertEqual({p.name for p in result.kept}, {clock_skewed.name, backups[-1].name, backups[-2].name})

    def test_keep_must_be_positive(self):
        backups, unrelated = self.seed()
        with self.assertRaises(ValueError):
            schedule.apply_retention(self.dir, DB, keep=0, protect=backups[-1])
        self.assertTrue(all(p.exists() for p in backups + unrelated))

    def test_unrelated_files_survive_many_runs(self):
        backups, unrelated = self.seed()
        for _ in range(3):
            schedule.apply_retention(self.dir, DB, keep=1, protect=backups[-1])
        self.assertEqual([p.name for p in schedule.matching_backups(self.dir, DB)], [backups[-1].name])
        self.assertTrue(all(p.exists() for p in unrelated))


class RunTests(TempDirTestCase):
    """Full runs of one scheduled backup (real pg_dump of the application database, read-only)."""

    def setUp(self):
        super().setUp()
        self.logs = self.dir / "logs"
        self.backups = self.dir / "backups"
        self.backups.mkdir()

    def log_text(self) -> str:
        return (self.logs / schedule.LOG_FILE).read_text(encoding="utf-8")

    def test_successful_run_creates_a_verified_dump_and_applies_retention(self):
        old = [touch(self.backups / f"{SAFE_DB}-2020010{day}-000000.dump") for day in range(1, 4)]
        unrelated = touch(self.backups / "keep-me.txt")
        self.assertEqual(schedule.run(self.backups, keep=2, log_dir=self.logs), 0)
        current = schedule.matching_backups(self.backups, DB)
        self.assertEqual(len(current), 2)
        newest = current[-1]
        self.assertNotIn(newest, old)
        self.assertEqual(set(backup.archive_tables(newest)), set(backup.APP_TABLES))  # a readable, complete archive
        self.assertEqual([p.exists() for p in old], [False, False, True])
        self.assertTrue(unrelated.exists())
        log = self.log_text()
        self.assertIn(f"OK backup {newest.name}", log)
        self.assertIn("OK retention keep=2: retained=2 removed=2", log)
        for name in backup.SECRET_SETTINGS:
            value = os.environ.get(name, "")
            if len(value) >= 4:
                self.assertNotIn(value, log)

    def test_corrupt_archive_is_rejected_and_nothing_is_pruned(self):
        old = [touch(self.backups / f"{SAFE_DB}-2020010{day}-000000.dump") for day in range(1, 4)]
        real_run = backup._run

        def broken_pg_dump(args, env=None):
            if Path(args[0]).stem == "pg_dump":  # write garbage instead of a dump; pg_restore --list stays real
                Path(next(a for a in args if a.startswith("--file=")).split("=", 1)[1]).write_bytes(b"not a dump")
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_run(args, env)

        with mock.patch.object(backup, "_run", side_effect=broken_pg_dump):
            code = schedule.run(self.backups, keep=1, log_dir=self.logs, now=datetime(2026, 9, 30, 2, 30, 0))
        self.assertEqual(code, 1)
        name = f"{SAFE_DB}-20260930-023000.dump"
        self.assertFalse((self.backups / name).exists())
        self.assertTrue((self.backups / f"{name}.invalid").exists())
        self.assertTrue(all(p.exists() for p in old))  # no retention after a failed backup
        self.assertEqual(len(schedule.matching_backups(self.backups, DB)), 3)
        self.assertIn("FAILED backup", self.log_text())
        self.assertNotIn("OK retention", self.log_text())

    def test_an_existing_file_is_never_overwritten_or_touched(self):
        when = datetime(2026, 9, 30, 2, 30, 0)
        existing = touch(self.backups / schedule.backup_name(DB, when), b"an earlier backup")
        self.assertEqual(schedule.run(self.backups, keep=1, log_dir=self.logs, now=when), 1)
        self.assertEqual(existing.read_bytes(), b"an earlier backup")
        self.assertFalse(existing.with_name(existing.name + ".invalid").exists())
        self.assertIn("already exists", self.log_text())

    def test_secrets_are_never_logged(self):
        fake = {"PGPASSWORD": "pg-" + new_password(), "APP_DB_PASSWORD": "app-" + new_password(),
                "JWT_SECRET_KEY": "jwt-" + new_password(), "GEMINI_API_KEY": "AIza" + new_password()}
        message = f"connection failed for {fake['PGPASSWORD']} {fake['JWT_SECRET_KEY']} password=hunter2 api_key: {fake['GEMINI_API_KEY']}"
        with mock.patch.dict(os.environ, fake), \
                mock.patch.object(backup, "create_backup", side_effect=backup.BackupError(message)), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(schedule.run(self.backups, keep=1, log_dir=self.logs), 1)
        output = self.log_text() + "\n".join(str(c.args[0]) for c in printed.call_args_list)
        for value in list(fake.values()) + ["hunter2"]:
            self.assertNotIn(value, output)
        self.assertIn("***", output)

    def test_powershell_wrapper_runs_a_backup(self):
        """deploy\\run_backup_task.ps1, as the scheduled task calls it (credentials from the environment here)."""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PROJECT_ROOT / "deploy" / "run_backup_task.ps1"),
             "-BackupDir", str(self.backups), "-LogDir", str(self.logs), "-Keep", "3",
             "-DbUser", os.environ.get("PGUSER", "postgres")],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(schedule.matching_backups(self.backups, DB)), 1)
        self.assertIn("OK backup", self.log_text())
        missing = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PROJECT_ROOT / "deploy" / "run_backup_task.ps1"),
             "-BackupDir", str(self.backups), "-LogDir", str(self.logs), "-PgPassFile", str(self.dir / "no-such-pgpass.conf")],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("password file not found", self.log_text())


class RegistrationScriptTests(TempDirTestCase):
    """register_backup_task.ps1 is only ever run with -WhatIf here: nothing is registered."""

    def whatif(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(PROJECT_ROOT / "deploy" / "register_backup_task.ps1"), "-WhatIf", *extra],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120)

    def registered_tasks(self) -> int:
        result = subprocess.run(["powershell", "-NoProfile", "-Command",
                                 "(Get-ScheduledTask -TaskPath '\\EnterpriseRAG\\' -ErrorAction SilentlyContinue | Measure-Object).Count"],
                                capture_output=True, text=True, timeout=120)
        return int(result.stdout.strip() or 0)

    def test_dry_run_shows_the_task_and_registers_nothing(self):
        before = self.registered_tasks()
        pgpass = touch(self.dir / "pgpass.conf", b"localhost:5432:enterprise_rag:postgres:not-a-real-password\n")
        result = self.whatif("-BackupDir", str(self.dir / "backups"), "-PgPassFile", str(pgpass))
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("\\EnterpriseRAG\\RAG Backup", out)
        self.assertIn("daily at 02:30", out)
        self.assertIn("newest 14 backups", out)
        self.assertIn("Checks: OK", out)
        self.assertIn("Dry run: nothing registered.", out)
        self.assertIn(f"-PgPassFile \"{pgpass}\"", out)  # only the path of the password file is passed
        for secret in ["not-a-real-password", "PGPASSWORD"] + [os.environ[n] for n in backup.SECRET_SETTINGS
                                                                if len(os.environ.get(n, "")) >= 4]:
            self.assertNotIn(secret, out)
        self.assertFalse((self.dir / "backups").exists())  # a dry run creates nothing
        self.assertEqual(self.registered_tasks(), before)

    def test_dry_run_reports_unsafe_or_missing_settings(self):
        result = self.whatif("-BackupDir", str(PROJECT_ROOT / "backups"), "-PgPassFile", str(self.dir / "missing.conf"),
                             "-Keep", "0", "-At", "25:99")
        self.assertEqual(result.returncode, 0, result.stderr)
        for problem in ("outside the project folder", "password file not found", "-Keep must be at least 1", "-At must be HH:mm"):
            self.assertIn(problem, result.stdout)
        self.assertNotIn("Checks: OK", result.stdout)

    def test_unregister_dry_run(self):
        result = self.whatif("-Unregister")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would stop and unregister: \\EnterpriseRAG\\RAG Backup", result.stdout)
        self.assertIn("backups in the backup folder are kept", result.stdout)


if __name__ == "__main__":
    unittest.main()
