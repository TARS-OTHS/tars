"""Auto-discovery registry. Scans src/ subdirs, imports modules, registers everything."""

import importlib
import logging
import pkgutil
from pathlib import Path

from src.core.base import Connector, LLMProvider, MemoryBackend, VaultBackend
from src.core.tools import get_all_tools
from src.core.skills import load_skills, get_all_skills

logger = logging.getLogger(__name__)


class Registry:
    """Central registry for all T.A.R.S modules. Auto-discovers on init."""

    def __init__(self):
        self.connectors: dict[str, Connector] = {}
        self.llm_providers: dict[str, type[LLMProvider]] = {}
        self.memory_backends: dict[str, type[MemoryBackend]] = {}
        self.vault_backends: dict[str, type[VaultBackend]] = {}

    def discover(self) -> None:
        """Auto-discover all modules from src/ subdirectories."""
        # Import all modules in these packages — @tool decorators register on import
        self._scan_package("src.tools")
        self._scan_package("src.connectors")
        self._scan_package("src.llm")
        self._scan_package("src.memory")
        self._scan_package("src.vault")
        self._scan_package("src.apis")

        # Discover connector/provider/backend classes from imported modules
        self._collect_subclasses()

        # Load skills from YAML
        load_skills("skills")

        tools = get_all_tools()
        skills = get_all_skills()
        logger.info(
            f"Registry: {len(self.connectors)} connectors, "
            f"{len(self.llm_providers)} LLM providers, "
            f"{len(self.memory_backends)} memory backends, "
            f"{len(self.vault_backends)} vault backends, "
            f"{len(tools)} tools, {len(skills)} skills"
        )

    def _scan_package(self, package_name: str) -> None:
        """Import all modules in a package."""
        try:
            package = importlib.import_module(package_name)
        except ImportError:
            logger.debug(f"Package {package_name} not found, skipping")
            return

        package_path = getattr(package, "__path__", None)
        if not package_path:
            return

        for importer, module_name, is_pkg in pkgutil.iter_modules(package_path):
            if module_name.startswith("_"):
                continue
            full_name = f"{package_name}.{module_name}"
            try:
                importlib.import_module(full_name)
                logger.debug(f"Imported {full_name}")
            except Exception as e:
                logger.error(f"Failed to import {full_name}: {e}")

    def _collect_subclasses(self) -> None:
        """Find all instantiated or defined subclasses of base interfaces."""
        for cls in Connector.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("connector", ""))
            self.connectors[name] = cls
            logger.debug(f"Registered connector: {name}")

        for cls in LLMProvider.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("provider", ""))
            self.llm_providers[name] = cls
            logger.debug(f"Registered LLM provider: {name}")

        for cls in MemoryBackend.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("backend", ""))
            self.memory_backends[name] = cls
            logger.debug(f"Registered memory backend: {name}")

        for cls in VaultBackend.__subclasses__():
            name = getattr(cls, "name", cls.__name__.lower().replace("backend", ""))
            self.vault_backends[name] = cls
            logger.debug(f"Registered vault backend: {name}")

    def create_connector(self, name: str, config: dict,
                         vault: VaultBackend | None = None) -> Connector:
        """Instantiate a connector by name."""
        cls = self.connectors.get(name)
        if not cls:
            raise ValueError(f"Unknown connector: {name}. Available: {list(self.connectors.keys())}")
        return cls(config=config, vault=vault)

    def create_llm_provider(self, name: str, config: dict) -> LLMProvider:
        """Instantiate an LLM provider by name."""
        cls = self.llm_providers.get(name)
        if not cls:
            raise ValueError(f"Unknown LLM provider: {name}. Available: {list(self.llm_providers.keys())}")
        return cls(config=config)

    def create_memory_backend(self, name: str, config: dict) -> MemoryBackend:
        """Instantiate a memory backend by name."""
        cls = self.memory_backends.get(name)
        if not cls:
            raise ValueError(f"Unknown memory backend: {name}. Available: {list(self.memory_backends.keys())}")
        return cls(config=config)
