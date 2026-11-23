# -*- coding: utf-8 -*-
"""Assemble the res_mods/0.8.2 layout from src/, pack the release zip, optionally deploy.

    python scripts/build.py            # build/Offline_Battles_<ver>_res_mods.zip
    python scripts/build.py --deploy   # also copy into the client from WOT_082_DIR (.env)

Host-side (Python 3), never shipped. Same idea as the old replays-manager build.py.
"""
import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import repo_env

VERSION = '2.1_beta_1'
GAME_VERSION = '0.8.2'

# src path -> path inside res_mods/<GAME_VERSION>/
LAYOUT = (
	('src/bootstrap', 'scripts/client'),                    # CameraNode.py (mod loader), OfflineEntity.py, mods/
	('src/mods', 'scripts/client/gui/mods'),                # mod_offhangar.py entry point, mod_replserver.py
	('src/offhangar', 'scripts/client/gui/mods/offhangar'), # the package
	('src/gui', 'gui'),                                     # swf, icons
	('src/native/offh_math.dll', 'scripts/client/gui/mods/offhangar/native/offh_math.dll'),  # optional native math, beside the other binaries
)
# Host Python 3 only writes bytecode into __pycache__, so any .pyc left in src/ is a
# deliberate Python 2.6 binary (src/bootstrap: the engine imports CameraNode/OfflineEntity
# only as .pyc - without them no mod loads at all).
SKIP_DIRS = ('__pycache__',)

BUILD_DIR = os.path.join(repo_env.ROOT, 'build')
STAGE = os.path.join(BUILD_DIR, 'res_mods', GAME_VERSION)
PACKAGE = os.path.join(BUILD_DIR, 'Offline_Battles_%s_res_mods.zip' % VERSION)


def _files(src):
	if os.path.isfile(src):
		yield src, ''
		return
	for dir_path, dir_names, file_names in os.walk(src):
		dir_names[:] = [d for d in dir_names if d not in SKIP_DIRS]
		for name in file_names:
			path = os.path.join(dir_path, name)
			yield path, os.path.relpath(path, src)


BOOTSTRAP_PYC = ('CameraNode', 'OfflineEntity')  # engine imports these as .pyc only

# Runs inside Python 2.6: syntax-check every shipped .py, then refresh the bootstrap .pyc.
_PY26_SCRIPT = r'''
import os, sys, py_compile
src, bootstrap = sys.argv[1], sys.argv[2:]
bad = 0
for d, dirs, files in os.walk(src):
    dirs[:] = [x for x in dirs if x not in ('__pycache__', 'native')]
    for f in files:
        if f.endswith('.py'):
            p = os.path.join(d, f)
            try:
                compile(open(p, 'rU').read() + '\n', p, 'exec')
            except SyntaxError, e:
                bad += 1
                print 'PY26 SYNTAX %s:%s: %s' % (p, e.lineno, e.msg)
for name in bootstrap:
    p = os.path.join(src, 'bootstrap', name + '.py')
    py_compile.compile(p, p + 'c', 'scripts/client/%s.py' % name, True)
sys.exit(1 if bad else 0)
'''


def py26_exe():
	"""Portable Python 2.6 (PY26_EXE in .env, default .tools/python26/python.exe)."""
	exe = os.environ.get('PY26_EXE') or os.path.join(repo_env.ROOT, '.tools', 'python26', 'python.exe')
	return exe if os.path.isfile(exe) else None


def check_and_compile_py26():
	exe = py26_exe()
	if not exe:
		print('no Python 2.6 (PY26_EXE): skipped syntax check, shipping committed bootstrap .pyc')
		return
	import subprocess
	if subprocess.call([exe, '-c', _PY26_SCRIPT, repo_env.SRC] + list(BOOTSTRAP_PYC)):
		sys.exit('Python 2.6 syntax errors above - the client would fail to load these files')
	print('py2.6: src/ syntax ok, bootstrap .pyc rebuilt')


def stage():
	"""build/res_mods/<ver>/ = exactly what goes into the client."""
	shutil.rmtree(os.path.join(BUILD_DIR, 'res_mods'), ignore_errors=True)
	for src_rel, dst_rel in LAYOUT:
		for path, rel in _files(os.path.join(repo_env.ROOT, src_rel)):
			dst = os.path.join(STAGE, dst_rel, rel) if rel else os.path.join(STAGE, dst_rel)
			os.makedirs(os.path.dirname(dst), exist_ok=True)
			shutil.copy2(path, dst)


def pack():
	count = 0
	with zipfile.ZipFile(PACKAGE, 'w', zipfile.ZIP_DEFLATED) as package:
		for path, rel in _files(STAGE):
			package.write(path, 'res_mods/%s/%s' % (GAME_VERSION, rel.replace(os.sep, '/')))
			count += 1
	print('built %s (%d files)' % (PACKAGE, count))


def deploy():
	game = repo_env.game_dir()
	if not game:
		sys.exit('set WOT_082_DIR in .env to a WoT 0.8.2 client folder (with WorldOfTanks.exe)')
	target = os.path.join(game, 'res_mods', GAME_VERSION)
	for path, rel in _files(STAGE):
		dst = os.path.join(target, rel)
		os.makedirs(os.path.dirname(dst), exist_ok=True)
		shutil.copy2(path, dst)
	# Never delete .pyc here: the mod cleans its own package bytecode, and the bootstrap
	# .pyc files are the only form the engine loads.
	print('deployed into %s' % target)


if __name__ == '__main__':
	repo_env.load_env()
	check_and_compile_py26()
	stage()
	pack()
	if '--deploy' in sys.argv:
		deploy()
