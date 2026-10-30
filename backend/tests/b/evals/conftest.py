"""The evaluation packages live at the repository root, outside the backend package."""

import sys

from wg.settings import REPO_ROOT

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
