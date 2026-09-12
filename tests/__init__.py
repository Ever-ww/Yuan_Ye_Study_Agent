"""Project test package.

Some integration tests intentionally reuse deterministic runtime fakes from
other test modules.  Keeping ``tests`` as an explicit package makes those
imports independent of pytest's import mode and any third-party ``tests``
package installed in the environment.
"""
