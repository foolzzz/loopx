"""Install the real-runtime-root guard in Python subprocesses spawned by tests.

The guard's pytest plugin prepends this directory to PYTHONPATH, so a child
Python process that inherits it imports this module at startup in place of any
other ``sitecustomize``. After installing the guard, this module loads the
``sitecustomize`` that would have run without this directory, if one exists.
The guard stays off unless the environment names the session's report file.
"""

import importlib.machinery
import importlib.util
import os
import sys

import real_runtime_root_guard

real_runtime_root_guard.install_from_environment()


def _load_shadowed_sitecustomize() -> None:
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    search_path = [entry for entry in sys.path if os.path.realpath(entry or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", search_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["sitecustomize"] = module
    spec.loader.exec_module(module)


_load_shadowed_sitecustomize()
