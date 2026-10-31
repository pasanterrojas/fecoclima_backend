"""Host-only checkpoint regression (stdlib Python and Git; no Docker needed)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release_checkpoint as checkpoint


class SourceManifestTest(unittest.TestCase):
    def test_untracked_references_ignored_secrets_and_content_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            repositories = tuple(Path(directory) / name for name in ("backend", "frontend"))
            for repository in repositories:
                repository.mkdir()
                subprocess.run(["git", "init", "-q", str(repository)], check=True)
                (repository / ".gitignore").write_text(".env\n")
                (repository / ".env").write_text("SECRET=private-test-only")
                (repository / "reference.json").write_text('{"document":"client"}')
            with patch.object(checkpoint, "REPOSITORIES", repositories):
                checkpoint.seal()
                checkpoint.verify()
                for repository in repositories:
                    manifest = json.loads((repository / checkpoint.MANIFEST).read_text())
                    self.assertNotIn(".env", manifest["sha256"])
                    self.assertIn("reference.json", manifest["sha256"])
                subprocess.run(["git", "-C", str(repositories[1]), "add", "-f", ".env"], check=True)
                with self.assertRaisesRegex(SystemExit, "environment file"):
                    checkpoint.source_files(repositories[1])
                subprocess.run(["git", "-C", str(repositories[1]), "rm", "--cached", "-q", ".env"], check=True)
                (repositories[0] / "reference.json").write_text("changed")
                with self.assertRaisesRegex(SystemExit, "reference.json"):
                    checkpoint.verify()


if __name__ == "__main__":
    unittest.main()
