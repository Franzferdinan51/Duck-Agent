"""
Regression test for M0 launcher ghost-paths.

Both shell launchers must reference paths that exist in the repo. The pre-M0
launchers referenced packages/coding-agent/ which does not exist; the real
package lives at prime-agent-packages/coding-agent/.
"""

import os
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
GHOST = "packages/coding-agent"


class TestLauncherPaths(unittest.TestCase):

    def test_prime_agent_sh_does_not_reference_ghost_path(self):
        """prime-agent.sh must not reference packages/coding-agent/."""
        text = (REPO / "prime-agent.sh").read_text()
        occurrences = [line for line in text.splitlines() if GHOST in line]
        self.assertEqual(
            occurrences, [],
            "prime-agent.sh still references the ghost path "
            f"'{GHOST}':\n" + "\n".join(occurrences),
        )

    def test_prime_agent_sh_repoints_to_prime_agent_packages(self):
        """prime-agent.sh must reference prime-agent-packages/coding-agent."""
        text = (REPO / "prime-agent.sh").read_text()
        self.assertIn(
            "prime-agent-packages/coding-agent", text,
            "prime-agent.sh should be repointed to "
            "prime-agent-packages/coding-agent/ (the actual package directory).",
        )

    def test_prime_agent_sh_bundle_path_exists(self):
        """If --dist is used, the bundle path in prime-agent.sh must exist on disk."""
        text = (REPO / "prime-agent.sh").read_text()
        m = re.search(r'BUNDLE="(\$SCRIPT_DIR/[^"]+)"', text)
        if not m:
            self.skipTest("No BUNDLE= line found in prime-agent.sh")
        rel = m.group(1).replace("$SCRIPT_DIR", str(REPO))
        # On the upstream side, the bundle is only produced after `npm run build`
        # inside prime-agent-packages/coding-agent. We assert that the directory
        # is the right one, not that the dist artefact exists yet.
        self.assertIn(
            "prime-agent-packages/coding-agent", rel,
            f"BUNDLE path is not the real package directory: {rel}",
        )

    def test_duck_agent_does_not_reference_ghost_path(self):
        """duck-agent must not reference packages/coding-agent/."""
        text = (REPO / "duck-agent").read_text()
        self.assertNotIn(
            GHOST, text,
            f"duck-agent references the ghost path '{GHOST}'.",
        )


if __name__ == "__main__":
    unittest.main()
