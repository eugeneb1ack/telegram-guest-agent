import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class HermesProfileSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "app"
        self.bin = self.tmp / "bin"
        self.profile = self.tmp / "hermes-profile"
        self.log = self.tmp / "hermes.log"
        self.app.mkdir()
        self.bin.mkdir()
        for name in ("setup-hermes-profile.sh", "init-env.sh", ".env.example"):
            shutil.copy2(ROOT / name, self.app / name)

        self._write_executable(
            "hermes",
            r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_HERMES_LOG"
if [[ "${1:-}" == "profile" && "${2:-}" == "show" ]]; then
  [[ -f "$FAKE_PROFILE_DIR/config.yaml" ]]
  exit
fi
if [[ "${1:-}" == "profile" && "${2:-}" == "create" ]]; then
  mkdir -p "$FAKE_PROFILE_DIR/cache/images"
  printf 'model:\n  default: fake-model\n' > "$FAKE_PROFILE_DIR/config.yaml"
  printf '# fake profile secrets\n' > "$FAKE_PROFILE_DIR/.env"
  chmod 600 "$FAKE_PROFILE_DIR/.env"
  exit
fi
if [[ "${1:-}" == "-p" ]]; then
  shift 2
fi
if [[ "${1:-}" == "config" && "${2:-}" == "path" ]]; then
  printf '%s\n' "$FAKE_PROFILE_DIR/config.yaml"
  exit
fi
if [[ "${1:-}" == "config" && "${2:-}" == "env-path" ]]; then
  printf '%s\n' "$FAKE_PROFILE_DIR/.env"
  exit
fi
if [[ "${1:-}" == "config" && "${2:-}" == "get" && "${3:-}" == "model.default" ]]; then
  printf 'fake-model\n'
  exit
fi
if [[ "${1:-}" == "config" && ("${2:-}" == "set" || "${2:-}" == "check") ]]; then
  exit
fi
if [[ "${1:-}" == "gateway" && ("${2:-}" == "install" || "${2:-}" == "restart") ]]; then
  exit
fi
echo "unexpected fake hermes invocation: $*" >&2
exit 2
''',
        )
        self._write_executable(
            "openssl",
            "#!/usr/bin/env bash\nprintf '%064d\\n' 0\n",
        )
        self._write_executable("curl", "#!/usr/bin/env bash\nexit 0\n")

    def _write_executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "GUEST_AGENT_APP_DIR": str(self.app),
            "FAKE_PROFILE_DIR": str(self.profile),
            "FAKE_HERMES_LOG": str(self.log),
        }
        return subprocess.run(
            [str(self.app / "setup-hermes-profile.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    @staticmethod
    def _env_values(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def test_creates_full_tool_profile_and_writes_secret_only_to_env_files(self):
        result = self._run(
            "--profile",
            "telegram-guest-agent",
            "--clone-from",
            "default",
            "--port",
            "8765",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        project_env = self._env_values(self.app / ".env")
        profile_env = self._env_values(self.profile / ".env")
        key = profile_env["API_SERVER_KEY"]
        self.assertEqual(len(key), 64)
        self.assertEqual(project_env["HERMES_API_KEY"], key)
        self.assertEqual(project_env["HERMES_MODEL"], "fake-model")
        self.assertEqual(
            project_env["HERMES_API_URL"],
            "http://host.docker.internal:8765/v1/chat/completions",
        )
        self.assertEqual(
            project_env["GUEST_HARNESS_MEDIA_DIR"],
            str(self.profile.resolve() / "cache" / "images"),
        )
        self.assertNotIn(key, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE((self.app / ".env").stat().st_mode), 0o600)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("profile create telegram-guest-agent", log)
        self.assertIn('config set platform_toolsets.api_server ["hermes-cli"]', log)
        self.assertIn("config set memory.memory_enabled false", log)
        self.assertIn("config set memory.user_profile_enabled false", log)
        self.assertIn("config set memory.provider ", log)
        self.assertIn("gateway install --force --start-now", log)

    def test_existing_profile_requires_reuse_and_preserves_key(self):
        first = self._run("--no-start")
        self.assertEqual(first.returncode, 0, first.stderr)
        original_key = self._env_values(self.profile / ".env")["API_SERVER_KEY"]

        refused = self._run("--no-start")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--reuse", refused.stderr)

        reused = self._run("--reuse", "--no-start")
        self.assertEqual(reused.returncode, 0, reused.stderr)
        self.assertEqual(
            self._env_values(self.profile / ".env")["API_SERVER_KEY"],
            original_key,
        )

        restarted = self._run("--reuse")
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertIn(
            "gateway restart",
            self.log.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
