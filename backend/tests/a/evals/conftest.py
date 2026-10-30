import sys

from wg.settings import REPO_ROOT

# The runner lives in the repository's top-level `evals` package, outside the backend project.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
