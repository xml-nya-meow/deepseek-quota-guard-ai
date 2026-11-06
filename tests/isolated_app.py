"""Offline fixtures explicitly establish a temporary responsibility scope.

Production App never creates a scope implicitly. Existing GUI regressions use
this fixture rather than touching the Windows user's real registry. Dedicated
responsibility workflow tests instantiate the production App directly.
"""
from pathlib import Path

from quota_guard.core import GuardError
from quota_guard.ui import App as ProductApp


class App(ProductApp):
    def __init__(self, root, folder, home_folder=None):
        directory = Path(home_folder or folder) / '.offline-test-responsibility'
        super().__init__(root, folder, home_folder, responsibility_directory=directory)

    def load_folder(self):
        super().load_folder()
        registry = self.responsibility_gate.registry
        if (self.config is not None and not registry.directory.exists()
                and not registry.anchor_path.exists()):
            try:
                self.responsibility_gate.initialize([self.store])
            except GuardError:
                # Deliberately malformed/legacy recovery fixtures stay read-only;
                # they must not acquire a false clean migration declaration.
                pass
