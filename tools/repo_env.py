# -*- coding: utf-8 -*-
"""Shared paths for host-side tools and scripts (Python 3, never shipped).

    import repo_env
    repo_env.game_dir()          # WOT_082_DIR from .env / environment, or None
    repo_env.install_mod_imports()  # makes `gui.mods.offhangar...` import from src/
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
MOD_DIR = os.path.join(SRC, 'offhangar')


def load_env(path=os.path.join(ROOT, '.env')):
	"""KEY=VALUE lines from .env into os.environ; real env vars win."""
	if not os.path.isfile(path):
		return
	with open(path, encoding='utf-8-sig') as fh:
		for line in fh:
			line = line.strip()
			if line and not line.startswith('#') and '=' in line:
				key, value = line.split('=', 1)
				os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def game_dir():
	"""The WoT 0.8.2 client folder (WOT_082_DIR), or None when unset/invalid."""
	load_env()
	path = os.environ.get('WOT_082_DIR', '')
	return path if path and os.path.isfile(os.path.join(path, 'WorldOfTanks.exe')) else None


def install_mod_imports():
	"""Let tools import the mod as the game does (`gui.mods.offhangar.*`) from src/."""
	for name, path in (('gui', SRC), ('gui.mods', SRC)):
		if name not in sys.modules:
			module = types.ModuleType(name)
			module.__path__ = [path]
			sys.modules[name] = module
	sys.modules['gui'].mods = sys.modules['gui.mods']
