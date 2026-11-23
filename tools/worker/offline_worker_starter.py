# -*- coding: utf-8 -*-
"""Windowless starter for a second, offline_battle.py "authority worker"
copy of World of Tanks 0.8.2, run alongside the player's own visible client.

PROTOTYPE. Written from this repo's Linux dev container, where there is no
Windows to actually run it against - it has NOT been run against a real
0.8.2 client yet. Treat every claim below as "should work per documented
WinAPI behaviour and the one confirmed real-world precedent", not as
verified. The person testing this on their own machine is the first real
test; expect to iterate.

--- Why this exists, in one paragraph ---

offline_battle.py already computes bot AI, physics and collision for every
vehicle on the visible client's own CPU frame, every tick, for all ~30 bots
at once - that is the actual FPS cost the earlier PERF-log investigation
was aimed at. colorfulmeans/wot-0.9.22-offline-battles (a credited fork of
pengw0048's original LAN-battle idea, adapted for the newer client) solves
the equivalent problem by moving that same computation into a SECOND, mostly
invisible copy of the game client ("simulation worker"), so the player's own
visible client only renders and sends its own input - never bot or physics
authority. This script is the Windows-side half of porting that idea to our
0.8.2 client: it starts that second copy without a visible window, using the
exact WinAPI trick documented in their own offline_worker_starter.c (see
this repo's tools/vendor comment below) - NOT a client patch, NOT a
rendering-subsystem hack, just running an unmodified WoT.exe on a desktop
object nobody ever switches to.

--- The actual trick (verified against colorfulmeans' own source comment,
not guessed) ---

  "#1513 hard-codes ShowWindow(SW_SHOW) for its main HWND, so STARTUPINFO's
  wShowWindow cannot suppress the first frame. Starting it on a private,
  never-switched desktop keeps that HWND and every child window off the
  player's desktop without patching or copying the client."

Windows supports several "desktop" objects per login session (distinct from
virtual desktops / Task View - this is the older, lower-level object that
Explorer's normal desktop is itself an instance of). Only the one that has
been SwitchDesktop()'d to is what the person actually sees on their monitor.
CreateDesktop() makes a new one that is never switched to, and a process
started with that desktop named in its STARTUPINFO.lpDesktop runs entirely
on it - it still opens a real HWND and the GPU still renders real frames,
they simply never reach the visible monitor. This is NOT the same as no
rendering happening: the worker copy still costs real GPU time. What it
removes is the cost on the VISIBLE client's own CPU frame budget of computing
bot AI/physics/collision for every other vehicle - which is the actual
bottleneck the PERF log points at, not GPU fill rate.

--- What this script does NOT do (be honest about the gap) ---

- It does not yet talk to offline_battle.py's own bot/physics loop at all.
  The mod-side half (making a second process actually BEHAVE as a
  bot/physics authority with no human at the wheel, and making the VISIBLE
  client become a pure replica of it) is separate, tracked work - see
  WORKER_MODE_NOTES.md next to this file once it exists. This script only
  proves (once run on a real machine) that a second WoT.exe copy can be
  started invisibly at all on THIS machine's Windows build.
- It has not been tested. ctypes signatures below are typed from the
  documented WinAPI prototypes; a wrong argtypes/restype on a 64-bit
  Python can produce silent corruption rather than a clean error, so this
  is exactly the kind of code that must be run under a debugger or with
  verbose logging the first several times, never trusted blind.
- v4.1 (2026-09-28) added job-object cleanup (see start_worker below), ported
  from colorfulmeans' own starter.c after the user supplied their real
  offline_worker_starter.c (1669 lines - the full file, not just the header
  comment this script was originally written from). Still NOT tested against
  a real client - this repo's dev container has no Windows to run it on.
  Still missing: crash-dump wiring, ready-marker handshake with the mod.
  Deliberately deferred until the basic desktop trick + job-object cleanup
  are both confirmed to work at all on the person's own machine - no point
  wiring a handshake protocol for a launch mechanism not yet confirmed to
  attach a window (or avoid attaching one) the way expected on their
  Windows build/driver combination.

--- How to actually try this ---

    python offline_worker_starter.py "C:\\Games\\WorldOfTanks\\WorldOfTanks.exe"

Watch Task Manager: a second WorldOfTanks.exe process should appear and
stay running, with NO new window appearing on the screen (not minimized -
literally not there; Alt-Tab will not show it either, since Alt-Tab is
itself scoped to the current desktop). If a window DOES flash on screen
even briefly, the desktop switch happened too late relative to window
creation, or CreateDesktop itself failed and the code silently fell back -
check the printed diagnostics, do not assume success from "a process
exists".
"""

import ctypes
import ctypes.wintypes as wintypes
import functools
import os
import subprocess
import sys
import time

print = functools.partial(print, flush=True)


# --- WinAPI constants (documented values, not guessed) ---------------------
GENERIC_ALL = 0x10000000
DESKTOP_CREATEWINDOW = 0x0002
DESKTOP_ENUMERATE = 0x0040
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_READOBJECTS = 0x0001
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_CREATEMENU = 0x0004
DESKTOP_HOOKCONTROL = 0x0008
DESKTOP_JOURNALRECORD = 0x0010
DESKTOP_JOURNALPLAYBACK = 0x0020
STANDARD_RIGHTS_REQUIRED = 0x000F0000
DESKTOP_ALL_ACCESS = (
	STANDARD_RIGHTS_REQUIRED | DESKTOP_READOBJECTS | DESKTOP_CREATEWINDOW |
	DESKTOP_CREATEMENU | DESKTOP_HOOKCONTROL | DESKTOP_JOURNALRECORD |
	DESKTOP_JOURNALPLAYBACK | DESKTOP_ENUMERATE | DESKTOP_WRITEOBJECTS |
	DESKTOP_SWITCHDESKTOP)

STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NEW_CONSOLE = 0x00000010
CREATE_SUSPENDED = 0x00000004

# --- Job-object constants, for automatic cleanup if this launcher dies -----
# v4.1: ported from colorfulmeans' own offline_worker_starter.c
# (configure_kill_job / CreateJobObjectW / AssignProcessToJobObject /
# ResumeThread sequence, read from their real source, not guessed). A job
# object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE means Windows itself kills
# every process assigned to the job the moment the job's LAST handle closes -
# including if this launcher process crashes or is killed, since a process's
# open handles are closed by the OS automatically when it exits. Without
# this, a worker started with the desktop trick alone survives its own
# launcher's death as a permanently orphaned, invisible WoT.exe - exactly the
# gap this script's docstring used to call out as deliberately deferred.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
	_fields_ = [
		('PerProcessUserTimeLimit', ctypes.c_int64),
		('PerJobUserTimeLimit', ctypes.c_int64),
		('LimitFlags', wintypes.DWORD),
		('MinimumWorkingSetSize', ctypes.c_size_t),
		('MaximumWorkingSetSize', ctypes.c_size_t),
		('ActiveProcessLimit', wintypes.DWORD),
		('Affinity', ctypes.c_size_t),
		('PriorityClass', wintypes.DWORD),
		('SchedulingClass', wintypes.DWORD),
	]


class IO_COUNTERS(ctypes.Structure):
	_fields_ = [
		('ReadOperationCount', ctypes.c_uint64),
		('WriteOperationCount', ctypes.c_uint64),
		('OtherOperationCount', ctypes.c_uint64),
		('ReadTransferCount', ctypes.c_uint64),
		('WriteTransferCount', ctypes.c_uint64),
		('OtherTransferCount', ctypes.c_uint64),
	]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
	_fields_ = [
		('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
		('IoInfo', IO_COUNTERS),
		('ProcessMemoryLimit', ctypes.c_size_t),
		('JobMemoryLimit', ctypes.c_size_t),
		('PeakProcessMemoryUsed', ctypes.c_size_t),
		('PeakJobMemoryUsed', ctypes.c_size_t),
	]


JobObjectExtendedLimitInformation = 9  # documented enum value, not guessed

WORKER_MODE_ENV = 'OFFHANGAR_WORKER_MODE'
WORKER_MODE_VALUE = 'authority_worker'
WORKER_DESKTOP_ENV = 'OFFHANGAR_WORKER_DESKTOP'


class STARTUPINFOW(ctypes.Structure):
	_fields_ = [
		('cb', wintypes.DWORD),
		('lpReserved', wintypes.LPWSTR),
		('lpDesktop', wintypes.LPWSTR),
		('lpTitle', wintypes.LPWSTR),
		('dwX', wintypes.DWORD),
		('dwY', wintypes.DWORD),
		('dwXSize', wintypes.DWORD),
		('dwYSize', wintypes.DWORD),
		('dwXCountChars', wintypes.DWORD),
		('dwYCountChars', wintypes.DWORD),
		('dwFillAttribute', wintypes.DWORD),
		('dwFlags', wintypes.DWORD),
		('wShowWindow', wintypes.WORD),
		('cbReserved2', wintypes.WORD),
		('lpReserved2', ctypes.POINTER(ctypes.c_byte)),
		('hStdInput', wintypes.HANDLE),
		('hStdOutput', wintypes.HANDLE),
		('hStdError', wintypes.HANDLE),
	]


class PROCESS_INFORMATION(ctypes.Structure):
	_fields_ = [
		('hProcess', wintypes.HANDLE),
		('hThread', wintypes.HANDLE),
		('dwProcessId', wintypes.DWORD),
		('dwThreadId', wintypes.DWORD),
	]


def _load_user32_kernel32():
	if os.name != 'nt':
		raise RuntimeError(
			'This starter uses Win32 desktop objects (CreateDesktopW) and '
			'only makes sense on Windows - refusing to run elsewhere.')
	user32 = ctypes.windll.user32
	kernel32 = ctypes.windll.kernel32
	user32.CreateDesktopW.restype = wintypes.HANDLE
	user32.CreateDesktopW.argtypes = (
		wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
		wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
	user32.CloseDesktop.restype = wintypes.BOOL
	user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
	kernel32.CreateProcessW.restype = wintypes.BOOL
	kernel32.CreateProcessW.argtypes = (
		wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
		wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
		ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION))
	kernel32.CreateJobObjectW.restype = wintypes.HANDLE
	kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
	kernel32.SetInformationJobObject.restype = wintypes.BOOL
	kernel32.SetInformationJobObject.argtypes = (
		wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
	kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
	kernel32.AssignProcessToJobObject.argtypes = (
		wintypes.HANDLE, wintypes.HANDLE)
	kernel32.ResumeThread.restype = wintypes.DWORD
	kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
	kernel32.TerminateProcess.restype = wintypes.BOOL
	kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
	kernel32.CloseHandle.restype = wintypes.BOOL
	kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
	return user32, kernel32


def create_kill_on_close_job(kernel32):
	"""A job object that kills every assigned process the instant its last
	handle closes - including this launcher's own handle closing because the
	launcher itself crashed or was killed (the OS closes a dead process's
	open handles for it). Ported from colorfulmeans' configure_kill_job() -
	same JOBOBJECT_EXTENDED_LIMIT_INFORMATION + LimitFlags value, read from
	their real source, not guessed from the WinAPI docs alone.
	"""
	job = kernel32.CreateJobObjectW(None, None)
	if not job:
		raise ctypes.WinError(ctypes.get_last_error())
	info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
	info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	ok = kernel32.SetInformationJobObject(
		job, JobObjectExtendedLimitInformation,
		ctypes.byref(info), ctypes.sizeof(info))
	if not ok:
		error = ctypes.get_last_error()
		kernel32.CloseHandle(job)
		raise ctypes.WinError(error)
	return job


def create_hidden_desktop(user32, name):
	"""Create a desktop object nobody has switched to.

	Named uniquely per starter run (caller passes a name including this
	process's own pid) so two worker launches never collide on the same
	desktop object - colorfulmeans' own starter does the same
	("OfflineLanWorker_<pid>").
	"""
	handle = user32.CreateDesktopW(name, None, None, 0, DESKTOP_ALL_ACCESS, None)
	if not handle:
		error = ctypes.get_last_error()
		raise ctypes.WinError(error)
	return handle


WORKER_PROFILE_SUBDIR = os.path.join('offhangar_user', 'worker_appdata')


def _worker_window_size(game_dir):
	"""`worker_window_size` ("WxH") from the mod config, default 1024x768."""
	default = (1024, 768)
	try:
		import json
		path = os.path.join(game_dir, 'offhangar_user', 'config.json')
		with open(path, 'r') as fh:
			cfg = json.load(fh)
		raw = str(cfg.get('worker_window_size', '') or '').strip().lower()
		if not raw or raw in ('off', 'none', 'auto'):
			return default
		parts = raw.replace('*', 'x').split('x', 1)
		w = int(parts[0])
		h = int(parts[1])
		if 160 <= w <= 1920 and 120 <= h <= 1200:
			return (w, h)
		print('worker_window_size %r out of range, using %dx%d' % (raw, default[0], default[1]))
	except Exception:
		pass
	return default


STOCK_ENGINE_CONFIG = os.path.join('res', 'engine_config.xml')
OVERLAY_ENGINE_CONFIG = os.path.join('res_mods', '0.8.2', 'engine_config.xml')
WORKER_PREFS_NAME = 'preferences_offhangar_worker.xml'
PLAYER_PREFS_NAME = 'preferences.xml'
PROFILE_SUBDIR = os.path.join('Wargaming.net', 'WorldOfTanks')


def _import_packed_xml():
	"""The Packed XML codec, ported verbatim from the 0.9.22 reference archive
	(`tools/packed_xml.py`, 202 lines) - it reads and writes the binary BigWorld
	Packed XML format that `res/engine_config.xml` uses."""
	try:
		import packed_xml
		return packed_xml
	except Exception:
		pass
	try:
		sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # tools/
		import packed_xml
		return packed_xml
	except Exception:
		return None


def _profile_dir():
	"""Directory where the 0.8.2 client keeps preferences.xml."""
	base = os.environ.get('APPDATA', '') or ''
	if not base:
		return None
	return os.path.join(base, PROFILE_SUBDIR)


def _preferences_child(root):
	matches = [(i, v) for i, (name, v) in enumerate(root.children)
		if name == b'preferences']
	if len(matches) != 1:
		raise ValueError('engine_config.xml must contain exactly one preferences setting')
	return matches[0]


def seed_worker_prefs_file(game_dir):
	"""Write the worker's OWN preferences file next to the player's one.

	A copy of the player's preferences.xml with only the video keys we care about
	changed (windowedWidth/windowedHeight from config `worker_window_size`,
	waitVSync forced false), so the worker never inherits the player's resolution
	and never writes into the player's file. preferences.xml is plain text, so
	this is a string edit; the engine_config overlay below is what makes the
	worker actually READ this file.
	"""
	try:
		pdir = _profile_dir()
		if not pdir or not os.path.isdir(pdir):
			print('Player profile dir not found (%r) - cannot seed a worker profile' % (pdir,))
			return None
		src = os.path.join(pdir, PLAYER_PREFS_NAME)
		dst = os.path.join(pdir, WORKER_PREFS_NAME)
		w, h = _worker_window_size(game_dir)
		if not os.path.isfile(src):
			print('No player %s at %r - the engine will create worker defaults' % (PLAYER_PREFS_NAME, src))
			return dst
		import re
		with open(src, 'r') as fh:
			text = fh.read()
		text = re.sub(r'(<windowedWidth>\s*)[-\d.]+(\s*</windowedWidth>)',
			lambda m: '%s%d%s' % (m.group(1), w, m.group(2)), text)
		text = re.sub(r'(<windowedHeight>\s*)[-\d.]+(\s*</windowedHeight>)',
			lambda m: '%s%d%s' % (m.group(1), h, m.group(2)), text)
		text = re.sub(r'(<waitVSync>\s*)\w+(\s*</waitVSync>)',
			lambda m: '%sfalse%s' % (m.group(1), m.group(2)), text)
		tmp = dst + '.tmp'
		with open(tmp, 'w') as fh:
			fh.write(text)
		try:
			os.replace(tmp, dst)
		except Exception:
			if os.path.exists(dst):
				os.remove(dst)
			os.rename(tmp, dst)
		print('Worker profile: %s (windowed %dx%d, waitVSync false; copied from %s)'
			% (dst, w, h, src))
		return dst
	except Exception as exc:
		print('Worker profile preparation failed (%r)' % (exc,))
		return None


OVERLAY_MARKER = OVERLAY_ENGINE_CONFIG + '.offhangar.json'


def _overlay_marker_path(game_dir):
	return os.path.join(game_dir, OVERLAY_MARKER)


def _overlay_is_ours(game_dir):
	"""True only when WE wrote the overlay: a sidecar marker file names it.

	Sniffing the file's contents is no longer enough - the CLIENT variant keeps the
	stock `preferences.xml` name and differs from stock only in the soundMgr channel
	counts, so "is this ours" cannot be inferred from the preferences value.
	"""
	return os.path.isfile(_overlay_marker_path(game_dir))


def _write_overlay_marker(game_dir, prefs_name, sound_channels, virtual_channels):
	try:
		import json
		with open(_overlay_marker_path(game_dir), 'w') as fh:
			json.dump({'kind': 'offhangar_engine_config_overlay', 'version': 1,
				'preferences': prefs_name,
				'softwareChannels': int(sound_channels or 0),
				'virtualChannels': int(virtual_channels or 0)}, fh)
	except Exception as exc:
		print('Overlay marker write failed (%r)' % (exc,))


def _sound_channels_from_config(game_dir):
	"""(softwareChannels, virtualChannels) from the mod config; 0 = leave stock.

	Stock `res/engine_config.xml` carries soundMgr/softwareChannels=64,
	virtualChannels=512, ADPCMCodecs=64. Sixty-four real channels is small for this
	mod: 30 bots x 2 motion loops is already 58, before gunfire, ricochets,
	explosions and the player's own engine - and the owner reported losing ALL the
	other sounds while bot sounds were enabled. Raising it is exactly what the
	overlay is for: change one value, keep every other engine setting from stock.
	"""
	try:
		import json
		with open(os.path.join(game_dir, 'offhangar_user', 'config.json'), 'r') as fh:
			cfg = json.load(fh)
		soft = int(cfg.get('sound_software_channels', 0) or 0)
		virt = int(cfg.get('sound_virtual_channels', 0) or 0)
	except Exception:
		return (0, 0)
	if soft < 0 or soft > 1024:
		soft = 0
	if virt < 0 or virt > 4096:
		virt = 0
	return (soft, virt)


def _build_overlay_data(stock_root, packed_xml, prefs_name, sound_channels,
		virtual_channels, prefs_index):
	"""A clone of the stock root with ONLY the requested keys changed."""
	import copy
	result = copy.deepcopy(stock_root)
	result.children[prefs_index] = (b'preferences', packed_xml.PackedValue(
		packed_xml.TYPE_STRING, prefs_name.encode('utf-8')))
	if sound_channels or virtual_channels:
		for i, (name, value) in enumerate(result.children):
			if name != b'soundMgr' or value.value_type != packed_xml.TYPE_ELEMENT:
				continue
			kids = list(value.value.children)
			for j, (kname, _kval) in enumerate(kids):
				if kname == b'softwareChannels' and sound_channels:
					kids[j] = (kname, packed_xml.PackedValue(
						packed_xml.TYPE_INTEGER, int(sound_channels)))
				elif kname == b'virtualChannels' and virtual_channels:
					kids[j] = (kname, packed_xml.PackedValue(
						packed_xml.TYPE_INTEGER, int(virtual_channels)))
			# A TYPE_ELEMENT PackedValue holds a PackedElement, whose own `value`
			# is one level deeper again - passing value.value here (a PackedElement)
			# made write_packed_xml fail with 'PackedElement' object has no
			# attribute 'value_type'.
			result.children[i] = (name, packed_xml.PackedValue(
				packed_xml.TYPE_ELEMENT, packed_xml.PackedElement(
					value=value.value.value, children=kids)))
			break
	return packed_xml.write_packed_xml(result)


def install_worker_engine_config(game_dir, prefs_name=None, sound_channels=0,
		virtual_channels=0):
	"""Point the client's `preferences` at the worker's own file, without touching
	any stock file. Ported from the 0.9.22 reference
	(`launcher/preferences_overlay.py`), whose docstring states the trick: the
	client reads `res/engine_config.xml` through BigWorld's resource search path,
	so a complete Packed XML clone under `res_mods` can change ONLY the
	preferences location while retaining every other engine setting. Our
	`paths.xml` lists `./res_mods/0.8.2` first, so the clone wins.

	Our stock value matches exactly what their `_stock_root()` requires:
	`preferences` is TYPE_STRING b'preferences.xml' (verified by parsing
	res/engine_config.xml - 18 root children, renderer/soundMgr/entities/... all
	kept untouched by the deep copy). An unknown existing overlay is a hard
	conflict and is left alone. Returns True when the overlay is in place.
	"""
	packed_xml = _import_packed_xml()
	if packed_xml is None:
		print('packed_xml codec unavailable - worker keeps the shared preferences')
		return False
	stock = os.path.join(game_dir, STOCK_ENGINE_CONFIG)
	overlay = os.path.join(game_dir, OVERLAY_ENGINE_CONFIG)
	if not os.path.isfile(stock):
		print('Stock %s is missing - not installing an overlay' % STOCK_ENGINE_CONFIG)
		return False
	try:
		import copy
		with open(stock, 'rb') as fh:
			stock_root = packed_xml.read_packed_xml(fh.read())
		index, pref = _preferences_child(stock_root)
		if pref.value_type != packed_xml.TYPE_STRING or pref.value != b'preferences.xml':
			print('Unexpected stock preferences setting (%r) - not installing an overlay'
				% (pref.value,))
			return False
		if prefs_name is None:
			prefs_name = WORKER_PREFS_NAME
		data = _build_overlay_data(stock_root, packed_xml, prefs_name,
			sound_channels, virtual_channels, index)
		if os.path.lexists(overlay) and not _overlay_is_ours(game_dir):
			print('CONFLICT: %s exists and has no offhangar marker - leaving it alone'
				% OVERLAY_ENGINE_CONFIG)
			return False
		outdir = os.path.dirname(overlay)
		if outdir and not os.path.isdir(outdir):
			os.makedirs(outdir)
		tmp = overlay + '.tmp'
		with open(tmp, 'wb') as fh:
			fh.write(data)
		try:
			os.replace(tmp, overlay)
		except Exception:
			if os.path.exists(overlay):
				os.remove(overlay)
			os.rename(tmp, overlay)
		_write_overlay_marker(game_dir, prefs_name, sound_channels, virtual_channels)
		print('engine_config overlay installed: %s (preferences -> %s, softwareChannels=%s, virtualChannels=%s)'
			% (overlay, prefs_name, sound_channels or 'stock', virtual_channels or 'stock'))
		return True
	except Exception as exc:
		print('Overlay install failed (%r) - worker keeps the shared preferences' % (exc,))
		return False


def remove_worker_engine_config(game_dir):
	"""Remove OUR overlay (either variant) plus its marker.

	Ownership is decided by the sidecar marker, NOT by sniffing the file: the
	client variant keeps the stock `preferences.xml` name and differs from stock
	only in the soundMgr channel counts, so the preferences value can no longer
	tell ours from a foreign file. Anything without our marker is left alone and
	reported, because deleting a foreign res_mods file is not ours to do.
	"""
	overlay = os.path.join(game_dir, OVERLAY_ENGINE_CONFIG)
	marker = _overlay_marker_path(game_dir)
	if not os.path.lexists(overlay):
		if os.path.lexists(marker):
			try:
				os.remove(marker)
			except Exception:
				pass
		return False
	if not os.path.isfile(marker):
		print('Overlay at %s has no offhangar marker - NOT removing it' % OVERLAY_ENGINE_CONFIG)
		return False
	try:
		os.remove(overlay)
	except Exception as exc:
		print('Overlay removal failed (%r)' % (exc,))
		return False
	try:
		os.remove(marker)
	except Exception:
		pass
	print('engine_config overlay removed (client uses stock res/engine_config.xml)')
	return True


def prepare_worker_profile(game_dir):
	"""SUPERSEDED by seed_worker_prefs_file() + install_worker_engine_config().

	Kept only as the record of a DISPROVEN approach: it handed the worker child a
	private %APPDATA%, and the measurement showed the engine does not take the
	preferences path from the environment at all - WORKER_VIDEO logged
	`APPDATA=D:\\Games\\wot\\offhangar_user\\worker_appdata` while
	`resolution=(1920, 1009)`, i.e. still the player's settings, because the path
	comes from res/engine_config.xml.

	Give the worker its OWN %APPDATA% so it never inherits the player's video
	settings - and never writes into them either.

	Why this exists, measured rather than assumed: both processes read the SAME
	%APPDATA%/Wargaming.net/WorldOfTanks/preferences.xml. On 04.10 15:24 the
	player's client saved its own new settings (windowedWidth 1920,
	windowedHeight 1009, waitVSync true - the file went from 12331 bytes / mtime
	02.10 19:01 to 12332 / 04.10 15:24), and the worker, started afterwards, came
	up rendering about six times more pixels with vsync on. Its frame went from
	36.02 ms to 79.59 ms and fps med from 25.9 to 12.5; because the worker
	publishes bot states at its own frame rate, the client's `bot_updates` fell
	from 20.4/s to 11.6/s and `max_bot_gap` med rose from 105 to 156 ms. In other
	words a settings change in the VISIBLE client made the whole battle stutter
	twice as much. The worker cannot be resized at runtime instead:
	`BigWorld.resizeWindow` was tried with 320x240 and with 640x480 (the smallest
	mode in `BigWorld.listVideoModesAllMonitors()`) and the engine ignored both -
	six attempts each, no exception, readback unchanged.

	The preferences file is plain XML text, so a private copy with our own values
	is a simple string edit. The copy is rewritten on EVERY launch, so the worker
	cannot drift. With a private profile the worker may also write its own file
	freely (its `BigWorld.savePreferences` is only stubbed while
	`worker_graphics_low` is on) without ever touching the player's settings.

	Returns the directory to pass as APPDATA to the worker child, or None when
	nothing could be prepared - in which case the caller leaves the environment
	alone and the worker keeps using the shared profile (previous behaviour).
	"""
	try:
		root = os.path.join(game_dir, WORKER_PROFILE_SUBDIR)
		profile = os.path.join(root, 'Wargaming.net', 'WorldOfTanks')
		if not os.path.isdir(profile):
			os.makedirs(profile)
		w, h = _worker_window_size(game_dir)
		src = os.path.join(os.environ.get('APPDATA', '') or '',
			'Wargaming.net', 'WorldOfTanks', 'preferences.xml')
		dst = os.path.join(profile, 'preferences.xml')
		if src and os.path.isfile(src):
			import re
			with open(src, 'r') as fh:
				text = fh.read()
			text = re.sub(r'(<windowedWidth>\s*)[-\d.]+(\s*</windowedWidth>)',
				lambda m: '%s%d%s' % (m.group(1), w, m.group(2)), text)
			text = re.sub(r'(<windowedHeight>\s*)[-\d.]+(\s*</windowedHeight>)',
				lambda m: '%s%d%s' % (m.group(1), h, m.group(2)), text)
			text = re.sub(r'(<waitVSync>\s*)\w+(\s*</waitVSync>)',
				lambda m: '%sfalse%s' % (m.group(1), m.group(2)), text)
			with open(dst, 'w') as fh:
				fh.write(text)
			print('Worker profile: %s (windowed %dx%d, waitVSync false; copied from %s)'
				% (dst, w, h, src))
		else:
			print('Worker profile: no source preferences.xml at %r - the worker will'
				' create its own defaults there' % (src,))
		return root
	except Exception as exc:
		print('Worker profile preparation failed (%r) - keeping the shared APPDATA'
			% (exc,))
		return None


def build_environment_block(extra):
	"""A CreateProcessW environment block: current environment plus extra,
	NUL-separated, double-NUL-terminated, as CREATE_UNICODE_ENVIRONMENT
	expects. Passing None instead would inherit this process's environment
	verbatim, which is usually fine too, but the worker needs
	OFFHANGAR_WORKER_MODE set, which the launcher's own environment does not
	have (that would make the launcher's OWN process look like a worker to
	anything reading os.environ, which is not what we want).

	Returns a ctypes unicode buffer, NOT a plain str/c_wchar_p - a first
	pass through this used ctypes.c_wchar_p(block) to hand the block to
	CreateProcessW, which is a real, confirmed bug: c_wchar_p stops at the
	FIRST embedded NUL, so CreateProcessW would have seen only the first
	"KEY=value" pair and silently lost the rest of the caller's own
	environment (PATH included) for the child process. create_unicode_buffer
	preserves the embedded NULs; the caller must keep this buffer alive
	(assigned to a variable) until after CreateProcessW returns, since
	ctypes does not keep an extra reference for you once it is cast to
	c_void_p.
	"""
	merged = dict(os.environ)
	merged.update(extra)
	parts = [u'%s=%s' % (key, value) for key, value in merged.items()]
	block = u'\0'.join(parts) + u'\0\0'
	return ctypes.create_unicode_buffer(block, len(block))


def start_worker(exe_path, extra_args=(), window_station_name='WinSta0', extra_env=None):
	"""Launch exe_path on a freshly-created, never-switched desktop, inside
	a kill-on-close job object.

	Returns (job_handle, desktop_handle, process_handle, pid). The caller
	owns all three handles - CloseHandle/CloseDesktop them when the worker
	process is torn down deliberately. If this launcher process itself dies
	first (crash, kill, closed console) without an explicit teardown, the
	job object's own KILL_ON_JOB_CLOSE flag makes Windows kill the worker
	automatically the instant this process's job handle is closed by the OS
	as part of normal process exit - so an orphaned invisible worker is no
	longer possible even on an ungraceful launcher exit. Ported from
	colorfulmeans' offline_worker_starter.c CREATE_SUSPENDED ->
	AssignProcessToJobObject -> ResumeThread sequence: the process must be
	suspended and assigned to the job BEFORE it (or anything it spawns) runs
	even one instruction, or there is a race window where it could act (or
	crash) before the job's protection applies to it.
	"""
	user32, kernel32 = _load_user32_kernel32()
	pid = os.getpid()
	desktop_name = u'OffhangarWorker_%d' % pid
	desktop = create_hidden_desktop(user32, desktop_name)
	full_desktop_name = u'%s\\%s' % (window_station_name, desktop_name)

	job = create_kill_on_close_job(kernel32)

	# Kept alive as a local for the whole function body (not just until the
	# CreateProcessW call below) - see build_environment_block's own
	# docstring on why a c_wchar_p cast here would be a silent data-loss bug.
	_env_extra = {
		WORKER_MODE_ENV: WORKER_MODE_VALUE,
		WORKER_DESKTOP_ENV: full_desktop_name,
	}
	if extra_env:
		_env_extra.update(extra_env)
	env_buffer = build_environment_block(_env_extra)

	startup_info = STARTUPINFOW()
	startup_info.cb = ctypes.sizeof(STARTUPINFOW)
	startup_info.lpDesktop = full_desktop_name
	# The private desktop already keeps the worker window 100% invisible on the user's screen.
	# We must use SW_SHOWNORMAL (1) instead of SW_HIDE, because SW_HIDE signals DirectX 9
	# that the window is hidden, causing D3D9 to throttle Present() and BigWorld to sleep 250ms!
	SW_SHOWNORMAL = 1
	startup_info.dwFlags = STARTF_USESHOWWINDOW
	startup_info.wShowWindow = SW_SHOWNORMAL

	process_info = PROCESS_INFORMATION()

	command_line = u'"%s"' % exe_path
	for arg in extra_args:
		command_line += u' %s' % arg

	# lpApplicationName=None + a quoted exe in lpCommandLine matches how the
	# real client is normally launched (argv[0] resolution, working
	# directory handling for its own relative resource paths) - passing
	# lpApplicationName directly has bitten other launchers on paths with
	# spaces interacting oddly with argument parsing.
	command_line_buffer = ctypes.create_unicode_buffer(command_line)
	working_dir = os.path.dirname(exe_path) or None

	# CREATE_SUSPENDED: the process exists (and already sits on the hidden
	# desktop - lpDesktop takes effect at CreateProcessW time, not at
	# ResumeThread time) but its main thread has not run a single
	# instruction yet. This is the window in which AssignProcessToJobObject
	# below must happen, before ResumeThread lets anything actually execute.
	ok = kernel32.CreateProcessW(
		None, command_line_buffer, None, None, False,
		CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED,
		ctypes.cast(env_buffer, ctypes.c_void_p), working_dir,
		ctypes.byref(startup_info), ctypes.byref(process_info))
	if not ok:
		error = ctypes.get_last_error()
		kernel32.CloseHandle(job)
		user32.CloseDesktop(desktop)
		raise ctypes.WinError(error)

	if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
		error = ctypes.get_last_error()
		# The process was created suspended and never resumed - it has not
		# executed anything yet, so terminating it here is clean, not a
		# forceful kill of running game logic.
		kernel32.TerminateProcess(process_info.hProcess, 1)
		kernel32.CloseHandle(process_info.hThread)
		kernel32.CloseHandle(process_info.hProcess)
		kernel32.CloseHandle(job)
		user32.CloseDesktop(desktop)
		raise ctypes.WinError(error)

	# Pin worker process to dedicated P-cores (cores 4..7) at NORMAL priority,
	# leaving cores 0..3 dedicated to the visible player client.
	NORMAL_PRIORITY_CLASS = 0x00000020
	kernel32.SetPriorityClass(process_info.hProcess, NORMAL_PRIORITY_CLASS)
	try:
		import multiprocessing
		if multiprocessing.cpu_count() >= 8:
			kernel32.SetProcessAffinityMask(process_info.hProcess, 0x00F0)
	except Exception:
		pass

	# Explicitly disable Windows 11 EcoQoS (Power Throttling) on the worker process.
	# Offscreen background processes on Windows 11 are automatically forced into Efficiency Mode,
	# pinning them to 800MHz E-cores unless Power Throttling is explicitly turned off.
	try:
		class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
			_fields_ = [
				('Version', wintypes.DWORD),
				('ControlMask', wintypes.DWORD),
				('StateMask', wintypes.DWORD),
			]
		throttle_state = PROCESS_POWER_THROTTLING_STATE()
		throttle_state.Version = 1  # PROCESS_POWER_THROTTLING_CURRENT_VERSION
		throttle_state.ControlMask = 0x1 | 0x4  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION
		throttle_state.StateMask = 0  # Disable power throttling
		kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
		kernel32.SetProcessInformation.restype = wintypes.BOOL
		kernel32.SetProcessInformation(process_info.hProcess, 4, ctypes.byref(throttle_state), ctypes.sizeof(throttle_state))
	except Exception:
		pass

	# Patch wot_client_mutex -> wot_worker_mutex in worker process memory so it does
	# not trigger the client's single-instance check when the visible player client launches.
	try:
		MUTEX_NAME_VA = 0x00f2563c
		worker_mutex_name = u'wot_worker_mutex\0'.encode('utf-16le')
		old_protect = wintypes.DWORD()
		if kernel32.VirtualProtectEx(process_info.hProcess, ctypes.c_void_p(MUTEX_NAME_VA), len(worker_mutex_name), 0x04, ctypes.byref(old_protect)):
			bytes_written = ctypes.c_size_t()
			kernel32.WriteProcessMemory(process_info.hProcess, ctypes.c_void_p(MUTEX_NAME_VA), worker_mutex_name, len(worker_mutex_name), ctypes.byref(bytes_written))
			kernel32.VirtualProtectEx(process_info.hProcess, ctypes.c_void_p(MUTEX_NAME_VA), len(worker_mutex_name), old_protect.value, ctypes.byref(old_protect))
	except Exception:
		pass

	if kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
		error = ctypes.get_last_error()
		kernel32.TerminateProcess(process_info.hProcess, 1)
		kernel32.CloseHandle(process_info.hThread)
		kernel32.CloseHandle(process_info.hProcess)
		kernel32.CloseHandle(job)
		user32.CloseDesktop(desktop)
		raise ctypes.WinError(error)

	# The thread handle itself is not needed past this point - only
	# hProcess is used for any later wait/terminate.
	kernel32.CloseHandle(process_info.hThread)

	return job, desktop, process_info.hProcess, process_info.dwProcessId


def check_port_listening(host='127.0.0.1', port=28782):
	import socket
	s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	s.settimeout(0.5)
	try:
		s.connect((host, port))
		s.close()
		return True
	except Exception:
		return False


def ensure_lan_server(game_dir):
	if check_port_listening('127.0.0.1', 28782):
		print('[OFFLINE WORKER] LAN battle server is already running on 127.0.0.1:28782.')
		return None
	server_candidates = [
		os.path.join(game_dir, '_internal', 'servers', '0.8.2', 'lan_battle_server.py'),
		r'D:\Games\wot\_internal\servers\0.8.2\lan_battle_server.py',
	]
	server_script = None
	for cand in server_candidates:
		if os.path.isfile(cand):
			server_script = cand
			break
	if not server_script:
		print('[OFFLINE WORKER] WARNING: lan_battle_server.py not found!')
		return None
	print('[OFFLINE WORKER] Launching LAN battle server on 127.0.0.1:28782 (logging to lan_battle_server.log)...')
	log_path = os.path.join(game_dir, 'lan_battle_server.log')
	try:
		log_file = open(log_path, 'a', 1)
	except Exception:
		log_file = None
	server_env = dict(os.environ)
	server_env['WOT_OFFLINE_NAVGRAPH_DIR'] = os.path.join(
		game_dir, 'res_mods', '0.8.2', 'scripts', 'client', 'gui', 'mods', 'offhangar', 'navgraphs')
	proc = subprocess.Popen([sys.executable, server_script, '--port', '28782'], cwd=game_dir, env=server_env,
		stdout=log_file or subprocess.PIPE, stderr=log_file or subprocess.STDOUT)
	for _ in range(30):
		time.sleep(0.2)
		if check_port_listening('127.0.0.1', 28782):
			print('[OFFLINE WORKER] LAN battle server is up and listening on port 28782!')
			return proc
	print('[OFFLINE WORKER] Warning: timeout waiting for LAN server port 28782')
	return proc


def get_running_wot_pids(exclude_pid=None):
	pids = []
	try:
		kernel32 = ctypes.windll.kernel32
		TH32CS_SNAPPROCESS = 0x00000002
		class PROCESSENTRY32(ctypes.Structure):
			_fields_ = [
				('dwSize', wintypes.DWORD),
				('cntUsage', wintypes.DWORD),
				('th32ProcessID', wintypes.DWORD),
				('th32DefaultHeapID', ctypes.c_size_t),
				('th32ModuleID', wintypes.DWORD),
				('cntThreads', wintypes.DWORD),
				('th32ParentProcessID', wintypes.DWORD),
				('pcPriClassBase', ctypes.c_long),
				('dwFlags', wintypes.DWORD),
				('szExeFile', ctypes.c_char * 260)
			]
		hSnap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
		if hSnap and hSnap != -1:
			pe = PROCESSENTRY32()
			pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
			if kernel32.Process32First(hSnap, ctypes.byref(pe)):
				while True:
					name = pe.szExeFile.decode('ascii', 'ignore').lower()
					if 'worldoftanks' in name and pe.th32ProcessID != exclude_pid:
						pids.append(pe.th32ProcessID)
					if not kernel32.Process32Next(hSnap, ctypes.byref(pe)):
						break
			kernel32.CloseHandle(hSnap)
	except Exception:
		pass
	return pids


def launch_player_client(exe_path):
	print('[OFFLINE WORKER] Launching visible player client...')
	_game_dir = os.path.dirname(os.path.abspath(exe_path))
	# The overlay that points `preferences` at the worker's own file must be gone
	# BEFORE this process starts, otherwise the visible client would come up on the
	# worker's profile (small window, vsync off) and - worse - save the player's
	# own settings into it. When the config asks for larger sound channel counts we
	# swap in a CLIENT overlay instead: same stock `preferences.xml` name, only
	# soundMgr/softwareChannels (+ virtualChannels) raised.
	try:
		_soft, _virt = _sound_channels_from_config(_game_dir)
		if _soft or _virt:
			install_worker_engine_config(_game_dir, prefs_name=PLAYER_PREFS_NAME,
				sound_channels=_soft, virtual_channels=_virt)
		else:
			remove_worker_engine_config(_game_dir)
	except Exception as exc:
		print('[OFFLINE WORKER] overlay switch failed (%r)' % (exc,))
		try:
			remove_worker_engine_config(_game_dir)
		except Exception:
			pass
	env = dict(os.environ)
	env.pop('OFFHANGAR_WORKER_MODE', None)
	env['OFFHANGAR_NETWORK_MODE'] = '1'
	return subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path), env=env)


def main(argv):
	exe_path = None
	extra_args = ()
	auto_launch_client = ('--client' in argv or '-c' in argv)
	filtered_argv = [a for a in argv[1:] if a not in ('--client', '-c')]

	if filtered_argv and os.path.isfile(filtered_argv[0]):
		exe_path = filtered_argv[0]
		extra_args = filtered_argv[1:]
	else:
		this_dir = os.path.dirname(os.path.abspath(__file__))
		candidates = [
			os.path.normpath(os.path.join(this_dir, '..', '..', '..', 'WorldOfTanks.exe')),
			os.path.normpath(os.path.join(this_dir, '..', '..', 'WorldOfTanks.exe')),
			os.path.normpath(os.path.join(this_dir, 'WorldOfTanks.exe')),
			os.path.normpath(os.path.join(os.getcwd(), 'WorldOfTanks.exe')),
			r'D:\Games\wot\WorldOfTanks.exe',
		]
		for c in candidates:
			if os.path.isfile(c):
				exe_path = c
				break

	if not exe_path or not os.path.isfile(exe_path):
		sys.stderr.write(
			'usage: offline_worker_starter.py [path-to-WorldOfTanks.exe] [--client]\n'
			'Could not automatically find WorldOfTanks.exe.\n')
		return 2

	game_dir = os.path.dirname(os.path.abspath(exe_path))
	ready_markers = [
		os.path.join(game_dir, 'offline-worker.ready'),
		os.path.join(game_dir, 'offhangar_user', 'offline-worker.ready'),
	]
	for rm in ready_markers:
		try:
			if os.path.exists(rm):
				os.remove(rm)
		except Exception:
			pass

	# Step 1: Ensure LAN Battle Server is running
	server_proc = ensure_lan_server(game_dir)

	# Step 2: Start worker on hidden desktop
	print('Starting authority worker copy of %s on a hidden desktop...' % exe_path)
	seed_worker_prefs_file(game_dir)
	# The worker gets the STOCK sound channel counts on purpose: it is muted
	# end to end (FMOD master volume forced to 0, enableArenaSounds(False),
	# loadSounds/applyPreferences skipped), so raising its channel pool only buys
	# mixing work for sounds nobody hears. Measured cost of that mistake: with
	# softwareChannels=128 in BOTH overlays the worker process went from 51.4% of
	# one core to 115.0% while its python callback actually IMPROVED (21.16 ->
	# 19.62 ms), i.e. the growth was entirely outside the callback. Only the
	# visible client gets the configured counts (see launch_player_client).
	install_worker_engine_config(game_dir)
	job, desktop, process_handle, pid = start_worker(exe_path, extra_args)
	print('Started simulation worker. pid=%d' % pid)
	print('The worker process runs on a private desktop without a visible window.')
	print('')
	print('Waiting for worker to connect to LAN server and enter waiting room...')

	worker_ready = False
	player_proc = None
	wait_start = time.time()
	try:
		while True:
			if not worker_ready:
				for rm in ready_markers:
					if os.path.exists(rm):
						worker_ready = True
						elapsed = time.time() - wait_start
						print('===================================================================')
						print('[OFFLINE WORKER] Simulation worker is READY! (connected in %.1fs)' % elapsed)
						print('[OFFLINE WORKER] Authority session active. Bot simulation is offloaded.')
						if auto_launch_client:
							player_proc = launch_player_client(exe_path)
						else:
							print('You can now launch your visible player client (WorldOfTanks.exe)!')
							print('Hint: pass --client to auto-launch the player client.')
						print('===================================================================')
						break
				if not worker_ready and time.time() - wait_start > 15.0:
					print('===================================================================')
					print('[OFFLINE WORKER] Simulation worker initialized. Launching player client...')
					print('===================================================================')
					worker_ready = True
					if auto_launch_client:
						player_proc = launch_player_client(exe_path)
					else:
						print('You can now launch your visible player client (WorldOfTanks.exe)!')
			else:
				# Once worker is ready and player client has launched, track player client lifecycle
				if player_proc is not None:
					ret = player_proc.poll()
					if ret is not None:
						print('[OFFLINE WORKER] Player client closed (exit code %s). Shutting down worker and server...' % ret)
						break
				else:
					# Fallback: if player client was launched manually, detect and track running client PIDs
					running_pids = get_running_wot_pids(exclude_pid=pid)
					if not hasattr(main, '_tracked_pids'):
						main._tracked_pids = set()
					if running_pids:
						main._tracked_pids.update(running_pids)
					elif getattr(main, '_tracked_pids', None):
						print('[OFFLINE WORKER] Player client closed. Shutting down worker and server...')
						break
			time.sleep(0.5 if not worker_ready else 1.0)
	except KeyboardInterrupt:
		print('Stopping - terminating worker and server...')
	finally:
		if player_proc is not None and player_proc.poll() is None:
			try:
				player_proc.terminate()
			except Exception:
				pass
		kernel32 = ctypes.windll.kernel32
		user32 = ctypes.windll.user32
		kernel32.TerminateProcess(process_handle, 0)
		kernel32.CloseHandle(process_handle)
		kernel32.CloseHandle(job)
		user32.CloseDesktop(desktop)
		if server_proc is not None:
			try:
				server_proc.terminate()
				for _ in range(20):
					if server_proc.poll() is not None:
						break
					time.sleep(0.1)
				if server_proc.poll() is None:
					server_proc.kill()
			except Exception:
				pass
		for rm in ready_markers:
			try:
				if os.path.exists(rm):
					os.remove(rm)
			except Exception:
				pass
	# Last line of defence: never leave the overlay behind, or the next client
	# launch would come up on the worker's profile.
	try:
		remove_worker_engine_config(game_dir)
	except Exception:
		pass
	return 0


if __name__ == '__main__':
	sys.exit(main(sys.argv))
