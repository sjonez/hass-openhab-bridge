"""Local Windows dev shim -- not part of the integration.

homeassistant.runner imports the POSIX-only fcntl module at import time, and
pytest-homeassistant-custom-component imports runner from a pytest plugin, so
the test session cannot even start on Windows without this.

fcntl is used in exactly one place (fcntl.flock, in the single-instance lock)
which unit tests never reach. Authoritative verification still happens on
Linux in CI; this only makes local iteration possible.
"""

import sys
import types

if sys.platform == "win32" and "fcntl" not in sys.modules:
    _fcntl = types.ModuleType("fcntl")
    _fcntl.LOCK_SH = 1
    _fcntl.LOCK_EX = 2
    _fcntl.LOCK_NB = 4
    _fcntl.LOCK_UN = 8

    def _unsupported(*args, **kwargs):
        raise OSError("fcntl is not available on Windows")

    _fcntl.flock = _unsupported
    _fcntl.fcntl = _unsupported
    _fcntl.ioctl = _unsupported
    _fcntl.lockf = _unsupported
    sys.modules["fcntl"] = _fcntl

if sys.platform == "win32" and "resource" not in sys.modules:
    # homeassistant.util.resource raises the file-descriptor soft limit at
    # startup. Report a limit that is already high enough so the helper
    # returns early instead of trying to change anything.
    _resource = types.ModuleType("resource")
    _resource.RLIMIT_NOFILE = 7
    _resource.RLIM_INFINITY = -1

    def _getrlimit(_which):
        return (65536, 65536)

    def _setrlimit(_which, _limits):
        return None

    _resource.getrlimit = _getrlimit
    _resource.setrlimit = _setrlimit
    sys.modules["resource"] = _resource
