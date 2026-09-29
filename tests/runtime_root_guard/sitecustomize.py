"""Install the real-runtime-root guard in Python subprocesses spawned by tests.

tests/conftest.py prepends this directory to PYTHONPATH, so every child Python
process that inherits it imports this module at startup. Outside a guarded
pytest session the environment carries no protected roots and this is a no-op.
"""

import real_runtime_root_guard

real_runtime_root_guard.install_from_environment()
