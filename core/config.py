"""
River Vector - Core Configuration
Handles system configuration loading and validation.
"""

import json
import logging
import os
from typing import Any, Dict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_UNITS_DIR = os.path.join(os.path.dirname(__file__), "..", "units")
_DEFAULT_PROFILE = os.path.join(_UNITS_DIR, "voyager.json")


class Config:
    def __init__(self, config_path: str = _DEFAULT_PROFILE):
        self.config_path = config_path
        self.data: Dict[str, Any] = {}
        self.load_config()

    def load_config(self):
        """Loads configuration from the specified JSON file."""
        if not os.path.exists(self.config_path):
            logger.error(f"Configuration file not found at {self.config_path}")
            # Fallback to default values or raise exception in strict mode
            self.data = self._get_defaults()
            return

        try:
            with open(self.config_path, 'r') as f:
                self.data = json.load(f)
            logger.info(f"Configuration loaded from {self.config_path}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse configuration file: {e}")
            self.data = self._get_defaults()
        except Exception as e:
            logger.error(f"An unexpected error occurred while loading config: {e}")
            self.data = self._get_defaults()

    def _get_defaults(self) -> Dict[str, Any]:
        """Returns default configuration values."""
        return {
            "unit_name": "Default Voyager",
            "unit_id": "VOY-000",
            "hardware": {
                "cameras": 5,
                "clutch_type": "7-speed manual",
                "pico_enabled": True
            },
            "safety": {
                "estop_enabled": True,
                "watchdog_timeout": 1.0
            }
        }

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieves a configuration value by key."""
        keys = key.split('.')
        value = self.data
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
