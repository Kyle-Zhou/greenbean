"""Tool-layer implementations.

v1 ships ``WorkingCopyTools``, which runs every read/grep/git operation
against a local clone (whatever the sync layer or CLI put on disk). Future
backends — a local-agent implementation against the developer's actual repo,
or a cached/precomputed variant — slot in by satisfying the same
``greenbean.core.tools.Tools`` protocol.
"""

from greenbean.tools.working_copy import WorkingCopyTools

__all__ = ["WorkingCopyTools"]
