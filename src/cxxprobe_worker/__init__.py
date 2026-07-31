"""cxxprobe-worker — a job-execution daemon for cxxprobe.

The worker owns job execution, workspace management, artifact storage, and
monitoring. It owns *no* judging logic: no checkers, validators, generators,
or contest concepts live here. All of that is cxxprobe's, and the worker
reaches it through exactly one interface — the `cxxprobe judge` CLI.
"""

__version__ = "0.1.0"
