#!/usr/bin/env python3
#tdsr, a terminal screen reader
#Copyright (C) 2016, 2017  Tyler Spivey
#See the license in COPYING.txt
import importlib
import sys
import os
import select
import fcntl
import struct
import termios
import codecs
import pyte
from importlib.resources import as_file, files
from wcwidth import wcwidth
from io import StringIO, SEEK_END
import subprocess
import time
import configparser
import re
import argparse
import shlex
import logging
import platform
import signal
import copy
import collections
import base64
import binascii
import tdsr
import tdsr.ai
import tdsr.backend

logger = logging.getLogger("tdsr")
logger.addHandler(logging.NullHandler())

DEFAULT_CONFIG = files(tdsr).joinpath('tdsr.cfg.dist')
HOME_CONFIG = os.path.expanduser('~/.tdsr.cfg')
# Replaced in main() by choose_config_file(); everything else reads this.
CONFIG_FILE = HOME_CONFIG
# Name of the config kept inside a source checkout, so a development copy run
# with uv run or bin/tdsr never shares settings with an installed copy.
PROJECT_CONFIG_NAME = 'tdsr.cfg'
# Settings for any copy run from its own folder. Kept in the home folder, not
# the project folder, so unpacking a new version over or beside the old one
# never loses them, and they can never end up in a bundle.
DEV_CONFIG = os.path.expanduser('~/.tdsr-dev.cfg')
DEV_KEY_FILE = '~/.tdsr-dev.key'
# Where the AI key lives unless api_key_file says otherwise; see default_key_file().
HOME_KEY_FILE = '~/.tdsr.key'
PROJECT_KEY_NAME = 'tdsr.key'
# How long output must be quiet before a cursor read is spoken.
CURSOR_TIMEOUT = 0.02
# Never wait longer than this for output to settle, so continuously
# redrawing programs (progress bars, clocks) still get spoken.
CURSOR_SETTLE_MAX = 0.25
DEFAULT_SCROLLBACK_LINES = 1000
# A program can emit a huge amount of text with no newline; don't let the
# pending-speech buffer grow without bound.
MAX_SPEECH_BUFFER = 8192
# Fallback prompt pattern used by prompt jumping when the user has not set
# one. Matches common shell prompt endings: $ % # > followed by a space.
DEFAULT_PROMPT_RE = r'[$%#>]\s'
# Stripped from the front of a prompt line when reading command history.
# Default removes a whole user@host prompt, directory included, up to and
# including the prompt character, so arrowing through history reads only the
# command. Matches zsh "user@host dir % ", bash "user@host:dir$ " and
# "[user@host dir]$ ". \x25 is a percent sign: configparser treats a literal
# percent as interpolation, so it cannot appear in a value as-is.
DEFAULT_PROMPT_STRIP_RE = r'^[^\s@]+@[^\s$\x25#]+(?:\s+[^$\x25#]*?)?[$\x25#]\s'
# Earlier defaults, upgraded automatically because they were written into
# ~/.tdsr.cfg on first run and would otherwise never change.
LEGACY_PROMPT_STRIP_RES = (r'^\S+@\S+\s+',)
# Punctuation verbosity levels, least to most spoken.
PUNCTUATION_LEVELS = ('none', 'some', 'most', 'all')
# Codepoints spoken at the 'some' level: the ones that change meaning.
PUNCTUATION_SOME = '.,?!:;'
# Added at 'most': structural characters common in code and paths.
PUNCTUATION_MOST = PUNCTUATION_SOME + '()[]{}<>"\'`/\\|@#$%^&*_~+=-'
# How long the child must be quiet before a command counts as finished.
COMMAND_DONE_TIMEOUT = 0.75
# A run of this many spaces or more is treated as a gap between columns
# rather than as ordinary spacing inside a sentence.
DEFAULT_COLUMN_GAP = 3
# Inserted between columns when reading a line. A comma is spoken as a short
# pause by every synthesiser, which is what is wanted here; the marker is
# added after punctuation processing so it is never read out as "comma".
DEFAULT_COLUMN_MARKER = ','
# If the speech server never reports an utterance finished, say all falls back
# to this many seconds per line so it cannot stall.
SAY_ALL_FALLBACK = 6.0
# Codepoint ranges (hex) skipped when speaking lines, words and output.
# Box drawing, block elements, geometric shapes, powerline glyphs.
DEFAULT_IGNORE_RANGES = '2500-257F,2580-259F,25A0-25FF,E0B0-E0BF'
REPEAT_KEY_TIMEOUT = 0.5
# A redraw of the character just typed, arriving this soon after it was echoed,
# is the shell repainting the line rather than new output.
ECHO_REPEAT_WINDOW = 0.4
# How capitals are announced: say "cap", raise the pitch, both, or nothing.
CAPITAL_MODES = ('cap', 'pitch', 'both', 'off')
# Pitch multipliers sent to the speech server (1.0 is normal).
DEFAULT_CAP_PITCH = 1.4
DEFAULT_DELETED_PITCH = 0.7
# Set in the child's environment so a TDSR launched from inside TDSR (for
# example from .zshrc) knows not to start a second copy.
NESTED_ENV = 'TDSR_ACTIVE'
# Copy file: how long to wait for the shell to start sending the file, and the
# most base64 text accepted (about 15 MB of file).
COPY_FILE_WAIT = 15.0
MAX_CLIP_BASE64 = 20 * 1024 * 1024
PHONETICS = {x[0]: x for x in [
		'alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot',
		'golf', 'hotel', 'india', 'juliet', 'kilo', 'lima', 'mike',
		'november', 'oscar', 'papa', 'quebec', 'romeo', 'sierra',
		'tango', 'uniform', 'victor', 'wiskey', 'x ray', 'yankee',
		'zulu']
}

class State:
	def __init__(self):
		self.revy = 0
		self.revx = 0
		self.delayed_functions = []
		self.pending_read = None
		self.pending_deadline = 0.0
		self.pending_limit = 0.0
		self.silence = False
		self.tempsilence = False
		# Copy file: until when a clipboard sequence from the shell is
		# expected, the path asked for, and a path remembered from nano.
		self.clip_expect_until = 0.0
		self.clip_path = None
		self.clip_nano_path = None
		self.key_handlers = []
		self.config = configparser.ConfigParser()
		self.config['speech'] = {'process_symbols': 'false', 'key_echo': True, 'cursor_tracking': True,
		"line_pause": True, 'repeated_symbols': 'false', 'repeated_symbols_values': '-=!#',
		'ignore_symbols': 'true', 'ignore_symbols_ranges': DEFAULT_IGNORE_RANGES,
		'cursor_settle_max': str(CURSOR_SETTLE_MAX),
		'punctuation_level': 'some', 'announce_indent': 'false',
		'indent_unit': '1', 'announce_bell': 'true',
		'announce_command_done': 'false', 'logical_lines': 'true',
		'selection_mode': 'stream', 'strip_prompt': 'true',
		'announce_columns': 'true', 'column_gap': str(DEFAULT_COLUMN_GAP),
		'column_marker': DEFAULT_COLUMN_MARKER,
		'prompt_strip_pattern': DEFAULT_PROMPT_STRIP_RE,
		'shift_arrow_selection': 'false', 'stop_at_last_line': 'true',
		'capitals': 'cap', 'cap_pitch': str(DEFAULT_CAP_PITCH),
		'lower_pitch_deleted': 'true', 'deleted_pitch': str(DEFAULT_DELETED_PITCH),
		'scrollback_lines': str(DEFAULT_SCROLLBACK_LINES)}
		self.config['ai'] = {'enabled': 'false', 'model': tdsr.ai.DEFAULT_MODEL,
			'endpoint': tdsr.ai.DEFAULT_ENDPOINT,
			'max_tokens': str(tdsr.ai.DEFAULT_MAX_TOKENS), 'effort': '',
			'provider': 'auto', 'auth_header': '', 'ca_bundle': '', 'api_key_file': '',
			'verify_tls': 'true', 'timeout': str(tdsr.ai.REQUEST_TIMEOUT),
			'run_on_accept': 'true',
			'context_lines': str(tdsr.ai.DEFAULT_CONTEXT_LINES),
			'context_chars': str(tdsr.ai.DEFAULT_CONTEXT_CHARS),
			'context_mode': 'screen', 'redact': 'true',
			'profile_file': tdsr.ai.PROFILE_FILE, 'profile': '',
			'announce_usage': 'false',
			'history_chars': str(tdsr.ai.DEFAULT_HISTORY_CHARS)}
		self.config['symbols'] = {}
		self.config['plugins'] = {}
		self.symbols_re = None
		self.ignore_re = None
		self.punctuation_re = None
		self.history = collections.deque(maxlen=DEFAULT_SCROLLBACK_LINES)
		self.copy_x = None
		self.copy_y = None
		self.last_drawn = ''
		self.last_utterance = ''
		self.find_text = ''
		self.prompt_re = None
		self.prompt_strip_re = None
		self.ai_answer = []
		self.ai_command = None
		self.say_all = None
		self.speech_done = True
		self.last_output_time = 0.0
		self.command_running = False
		self.pass_next = False
		self.last_drawn_x = 0
		self.last_drawn_y = 0
		self.delaying_output = False
		self.loaded = None
		self.config_problem = None
		self.echoed = None

	def snapshot(self):
		return {(sec, key): val for sec in self.config.sections()
			for key, val in self.config.items(sec, raw=True)}

	def mark_loaded(self):
		"""Remember the settings as read, so saving writes only what changed."""
		self.loaded = self.snapshot()

	def save_config(self):
		"""Write settings changed in this session, keeping everything else.

		The file on disk is re-read first and only values that differ from what
		was loaded are written into it. Edits made by hand while TDSR runs, and
		settings this version does not know about, therefore survive. Defaults
		are never written for settings the user has not changed, so a future
		version's better default still applies.
		"""
		loaded = getattr(self, 'loaded', None) or {}
		current = self.snapshot()
		disk = configparser.ConfigParser(interpolation=None)
		readable = True
		if os.path.exists(CONFIG_FILE):
			try:
				disk.read(CONFIG_FILE)
			except configparser.Error as e:
				readable = False
				logger.error("Could not re-read %s before saving: %s", CONFIG_FILE, e)
				backup_unreadable_config(CONFIG_FILE)
		if not readable:
			disk = configparser.ConfigParser(interpolation=None)
			changed = current.items()
		else:
			changed = [(k, v) for k, v in current.items() if loaded.get(k) != v]
		for (sec, key), val in changed:
			if not disk.has_section(sec):
				disk.add_section(sec)
			disk.set(sec, key, val)
		for (sec, key) in set(loaded) - set(current):
			if disk.has_section(sec):
				disk.remove_option(sec, key)
		for sec in self.config.sections():
			if not disk.has_section(sec):
				disk.add_section(sec)
		try:
			with open(CONFIG_FILE, 'w') as fp:
				disk.write(fp)
		except OSError as e:
			logger.error("Could not write %s: %s", CONFIG_FILE, e)
			say_ui("Could not save configuration")
			return
		self.loaded = current

	def get_bool(self, option, fallback):
		"""getboolean() that survives a hand-edited config holding junk."""
		try:
			return self.config.getboolean('speech', option, fallback=fallback)
		except ValueError:
			logger.error("Invalid boolean for %s, using %r", option, fallback)
			return fallback

	def get_number(self, option, fallback, cast=int):
		"""Numeric config read that survives junk."""
		raw = self.config.get('speech', option, fallback=None)
		if raw is None:
			return fallback
		try:
			return cast(raw)
		except (TypeError, ValueError):
			logger.error("Invalid value for %s: %r, using %r", option, raw, fallback)
			return fallback

	def build_symbols_re(self):
		candidates = []
		for symbol in self.config['symbols']:
			try:
				symbol = int(symbol)
			except ValueError:
				logger.error("Ignoring non-numeric symbol key: %r", symbol)
				continue
			if symbol == 32:
				continue
			try:
				candidates.append(re.escape(chr(symbol)))
			except ValueError:
				logger.error("Ignoring out of range symbol: %r", symbol)
		if not candidates:
			return None
		return re.compile('|'.join(candidates))

	def punctuation_level(self):
		level = self.config.get('speech', 'punctuation_level', fallback='some')
		level = level.strip().lower()
		if level not in PUNCTUATION_LEVELS:
			logger.error("Unknown punctuation_level %r, using 'some'", level)
			return 'some'
		return level

	def build_punctuation_re(self):
		"""Regexp matching punctuation that should be spoken by name.

		At 'all' this is None and the caller falls back to the symbols table,
		which is the original process_symbols behaviour.
		"""
		level = self.punctuation_level()
		if level == 'none':
			return None
		if level == 'some':
			chars = PUNCTUATION_SOME
		elif level == 'most':
			chars = PUNCTUATION_MOST
		else:
			return None
		usable = [c for c in chars if str(ord(c)) in self.config['symbols']]
		if not usable:
			return None
		return re.compile('[%s]' % re.escape("".join(usable)))

	def build_ignore_re(self):
		"""Regexp matching runs of characters which should not be spoken."""
		if not self.get_bool('ignore_symbols', True):
			return None
		spec = self.config.get('speech', 'ignore_symbols_ranges', fallback=DEFAULT_IGNORE_RANGES)
		parts = []
		for item in spec.split(','):
			item = item.strip()
			if not item:
				continue
			try:
				if '-' in item:
					low, high = item.split('-', 1)
					parts.append('%s-%s' % (re.escape(chr(int(low, 16))), re.escape(chr(int(high, 16)))))
				else:
					parts.append(re.escape(chr(int(item, 16))))
			except ValueError:
				logger.error("Invalid ignore_symbols_ranges entry: %r", item)
		if not parts:
			return None
		return re.compile('[%s]+' % "".join(parts))

class KeyHandler:
	PASSTHROUGH = 1
	REMOVE = 2
	def __init__(self, keymap, fd=None):
		self.keymap = keymap
		self.fd = fd
		self.last_key = None
		self.last_key_time = 0.0

	def add(self, key, handler):
		if key not in self.keymap:
			self.keymap[key] = handler

	def process(self, data):
		key_time = time.monotonic()
		key_delta = key_time - self.last_key_time
		self.last_key_time = key_time
		repeat = data + data
		if data not in self.keymap:
			self.last_key = data
			return self.handle_unknown_key(data)
		elif self.last_key == data and repeat in self.keymap and key_delta <= REPEAT_KEY_TIMEOUT:
			result = self.keymap[repeat]()
			self.last_key = data
			return result
		else:
			result = self.keymap[data]()
			if result == KeyHandler.PASSTHROUGH:
				write_all(self.fd, data)
			self.last_key = data
			return result

	def handle_unknown_key(self, data):
		write_all(self.fd, data)

class ConfigHandler(KeyHandler):
	def __init__(self):
		self.keymap = {
			b'r': self.set_rate,
			b'v': self.set_volume,
			b'V': self.set_voice,
			b'p': self.set_process_symbols,
			b'd': self.set_delay,
			b'e': self.set_echo,
			b'c': self.set_cursor_tracking,
			b'l': self.set_line_pause,
			b's': self.set_repeated_symbols,
			b'i': self.set_ignore_symbols,
			b'n': self.set_indent,
			b'u': self.set_punctuation,
			b'b': self.set_bell,
			b'k': self.set_command_done,
			b'w': self.set_logical_lines,
			b'm': self.set_selection_mode,
			b'o': self.set_columns,
			b'a': self.set_capitals,
			b'D': self.set_deleted_pitch,
			b'?': self.help,
			b'h': self.help,
			b'C': self.browse_commands,
			b'A': self.set_ai_context,
			b'F': self.choose_profile,
			b'U': self.usage,
		}
		super().__init__(self.keymap)

	def set_rate(self):
		say_ui("Rate")
		state.key_handlers.append(BufferHandler(on_accept=self.set_rate2))

	def set_rate2(self, val):
		try:
			val = int(val)
		except ValueError:
			say_ui("Invalid value")
			return
		synth.set_rate(val)
		state.config['speech']['rate'] = str(val)
		state.save_config()
		say_ui("Confirmed")

	def set_volume(self):
		say_ui("volume")
		state.key_handlers.append(BufferHandler(on_accept=self.set_volume2))

	def set_volume2(self, val):
		try:
			val = int(val)
		except ValueError:
			say_ui("Invalid value")
			return
		synth.set_volume(val)
		state.config['speech']['volume'] = str(val)
		state.save_config()
		say_ui("Confirmed")

	def set_voice(self):
		voices, language = list_system_voices()
		if voices:
			state.key_handlers.append(VoiceMenuHandler(voices, language))
			return
		# No voice list (Linux, or a custom speech server): type a name instead.
		say_ui("voice")
		state.key_handlers.append(BufferHandler(on_accept=self.set_voice2))

	def set_voice2(self, val):
		"""Accepts a voice index, a voice name, or a voice identifier."""
		val = val.strip()
		if not val:
			say_ui("Invalid value")
			return
		synth.set_voice(val)
		state.config['speech']['voice'] = val
		state.config['speech'].pop('voice_idx', None)
		state.save_config()
		say_ui("Confirmed")


	def set_process_symbols(self):
		current = state.get_bool('process_symbols', False)
		current = not current
		state.config['speech']['process_symbols'] = str(current)
		state.save_config()
		say_ui("process symbols on" if current else "process symbols off")

	def set_echo(self):
		current = state.get_bool('key_echo', True)
		current = not current
		state.config['speech']['key_echo'] = str(current)
		state.save_config()
		say_ui("character echo on" if current else "character echo off")

	def set_cursor_tracking(self):
		current = state.get_bool('cursor_tracking', True)
		current = not current
		state.config['speech']['cursor_tracking'] = str(current)
		state.save_config()
		say_ui("cursor tracking on" if current else "cursor tracking off")

	def set_line_pause(self):
		current = state.get_bool('line_pause', True)
		current = not current
		state.config['speech']['line_pause'] = str(current)
		state.save_config()
		say_ui("line pause on" if current else "line pause off")

	def set_repeated_symbols(self):
		current = state.get_bool('repeated_symbols', False)
		current = not current
		state.config['speech']['repeated_symbols'] = str(current)
		state.save_config()
		say_ui("repeated symbols on" if current else "repeated symbols off")

	def set_ignore_symbols(self):
		current = state.get_bool('ignore_symbols', True)
		current = not current
		state.config['speech']['ignore_symbols'] = str(current)
		state.save_config()
		state.ignore_re = state.build_ignore_re()
		say_ui("ignore symbols on" if current else "ignore symbols off")

	def set_indent(self):
		toggle_indent()

	def set_punctuation(self):
		cycle_punctuation()

	def set_bell(self):
		current = not state.get_bool('announce_bell', True)
		state.config['speech']['announce_bell'] = str(current)
		state.save_config()
		say_ui("bell announcement on" if current else "bell announcement off")

	def set_command_done(self):
		current = not state.get_bool('announce_command_done', False)
		state.config['speech']['announce_command_done'] = str(current)
		state.save_config()
		say_ui("command done announcement on" if current else "command done announcement off")

	def set_logical_lines(self):
		current = not state.get_bool('logical_lines', True)
		state.config['speech']['logical_lines'] = str(current)
		state.save_config()
		say_ui("logical lines on" if current else "logical lines off")

	def set_selection_mode(self):
		current = selection_mode()
		nxt = 'lines' if current == 'stream' else 'stream'
		state.config['speech']['selection_mode'] = nxt
		state.save_config()
		say_ui("selection by whole lines" if nxt == 'lines' else "selection by character")

	CONFIG_HELP = [
		"r, set rate",
		"v, set volume",
		"shift v, choose a voice",
		"d, set cursor delay",
		"p, toggle symbol processing",
		"e, toggle character echo",
		"c, toggle cursor tracking",
		"l, toggle pausing at newlines",
		"s, toggle repeated symbols",
		"i, toggle ignored symbols",
		"n, toggle indentation announcement",
		"u, cycle punctuation level",
		"b, toggle bell announcement",
		"k, toggle command finished announcement",
		"w, toggle wrapped line reading",
		"m, toggle selection mode",
		"o, toggle column breaks",
		"a, cycle how capitals are announced",
		"shift d, toggle lower pitch for deleted characters",
		"shift c, browse all commands and their keys",
		"shift a, cycle what the AI assistant is shown",
		"shift f, choose an AI instruction profile",
		"shift u, report AI token usage",
		"question mark, this help",
		"enter, exit and save",
	]

	def set_ai_context(self):
		modes = ('screen', 'scrollback', 'selection')
		current = ai_context_mode()
		nxt = modes[(modes.index(current) + 1) % len(modes)]
		state.config['ai']['context_mode'] = nxt
		state.save_config()
		if nxt == 'screen':
			say_ui("AI context: visible screen")
		elif nxt == 'scrollback':
			say_ui("AI context: screen and scrollback")
		else:
			say_ui("AI context: current selection")

	def choose_profile(self):
		choose_ai_profile()

	def usage(self):
		ai_usage_report()

	def help(self):
		state.key_handlers.append(MenuHandler(self.CONFIG_HELP, "Config options"))

	def browse_commands(self):
		state.key_handlers.append(MenuHandler(command_list(), "Commands"))

	def set_columns(self):
		current = not state.get_bool('announce_columns', True)
		state.config['speech']['announce_columns'] = str(current)
		state.save_config()
		say_ui("column breaks on" if current else "column breaks off")

	def set_capitals(self):
		nxt = CAPITAL_MODES[(CAPITAL_MODES.index(capitals_mode()) + 1) % len(CAPITAL_MODES)]
		state.config['speech']['capitals'] = nxt
		state.save_config()
		say_ui({'cap': "capitals: say cap", 'pitch': "capitals: higher pitch",
			'both': "capitals: say cap and higher pitch", 'off': "capitals: off"}[nxt])

	def set_deleted_pitch(self):
		current = not state.get_bool('lower_pitch_deleted', True)
		state.config['speech']['lower_pitch_deleted'] = str(current)
		state.save_config()
		say_ui("lower pitch for deleted characters on" if current
			else "lower pitch for deleted characters off")

	def set_delay(self):
		say_ui("Cursor delay")
		state.key_handlers.append(BufferHandler(on_accept=self.set_delay2))

	def set_delay2(self, val):
		global CURSOR_TIMEOUT
		try:
			val = int(val)
		except ValueError:
			say_ui("Invalid value")
			return
		val/=1000
		CURSOR_TIMEOUT = val
		state.config['speech']['cursor_delay'] = str(val)
		state.save_config()
		say_ui("Confirmed")

	def handle_unknown_key(self, data):
		if data == b'\r' or data == b'\n':
			say_ui("exit")
			return KeyHandler.REMOVE

def list_system_voices():
	"""Voices from the macOS backend as (identifier, name, language), plus the system language.

	Returns ([], '') when the list is unavailable, so the caller can fall back to
	typing a voice name.
	"""
	if not mac_backend:
		return [], ''
	try:
		with as_file(files(tdsr.backend).joinpath('mac.py')) as mac_path:
			out = subprocess.run([sys.executable, str(mac_path), '--list-voices-tsv'],
				stdin=subprocess.DEVNULL, capture_output=True, timeout=15).stdout
	except (OSError, subprocess.SubprocessError) as e:
		logger.error("Could not list voices: %s", e)
		return [], ''
	return parse_voice_list(out.decode('utf-8', 'replace'))

def parse_voice_list(text):
	"""Parse --list-voices-tsv output, putting voices in the system language first."""
	language = ''
	voices = []
	for line in text.splitlines():
		parts = line.split('\t')
		if len(parts) == 2 and parts[0] == 'lang':
			language = parts[1].strip()
		elif len(parts) == 3 and parts[0]:
			voices.append((parts[0], parts[1], parts[2]))
	prefix = language.split('-')[0].lower()
	def key(v):
		ident, name, lang = v
		mine = bool(prefix) and lang.lower().split('-')[0] == prefix
		return (0 if mine else 1, lang.lower() if not mine else '', name.lower())
	voices.sort(key=key)
	return voices, language

class VoiceMenuHandler(KeyHandler):
	"""Arrow through the system voices, hearing each one in its own voice.

	The first item is the system voice, which follows VoiceOver's settings.
	Enter keeps the voice you are on; escape puts the previous one back.
	A letter jumps to the next voice whose name starts with it.
	"""
	def __init__(self, voices, language=''):
		self.options = [('', 'System voice', '')] + list(voices)
		self.original = state.config.get('speech', 'voice', fallback='')
		self.index = 0
		for i, (ident, _name, _lang) in enumerate(self.options):
			if ident and ident == self.original:
				self.index = i
				break
		keymap = {
			b'\x1b[A': self.previous,
			b'\x1b[B': self.next,
			b'\x1bOA': self.previous,
			b'\x1bOB': self.next,
			b'\r': self.accept,
			b'\n': self.accept,
		}
		super().__init__(keymap)
		say_ui("Voices. %d options. Up and down to listen, a letter to jump, "
			"enter to choose, escape to cancel." % len(self.options))
		self.speak_current(preview=False)

	def describe(self, option):
		ident, name, lang = option
		if not ident:
			return "System voice, follows VoiceOver"
		return "%s, %s" % (name, lang) if lang else name

	def speak_current(self, preview=True):
		option = self.options[self.index]
		if preview:
			# Switch first so the name is heard in the voice itself.
			synth.set_voice(option[0])
		say_ui(self.describe(option))

	def move(self, step):
		new = self.index + step
		if new < 0:
			say_ui("start")
			return
		if new >= len(self.options):
			say_ui("end")
			return
		self.index = new
		self.speak_current()

	def next(self):
		self.move(1)

	def previous(self):
		self.move(-1)

	def jump(self, letter):
		count = len(self.options)
		for step in range(1, count + 1):
			i = (self.index + step) % count
			if self.options[i][1].lower().startswith(letter):
				self.index = i
				self.speak_current()
				return
		say_ui("no voice starting with %s" % letter)

	def accept(self):
		ident, name, _lang = self.options[self.index]
		synth.set_voice(ident)
		if ident:
			state.config['speech']['voice'] = ident
		else:
			state.config['speech'].pop('voice', None)
		state.config['speech'].pop('voice_idx', None)
		state.save_config()
		say_ui("Voice set to %s" % (name if ident else "system voice"))
		return KeyHandler.REMOVE

	def handle_unknown_key(self, data):
		if data == b'\x1b':
			synth.set_voice(self.original)
			say_ui("cancelled")
			return KeyHandler.REMOVE
		text = data.decode('utf-8', 'replace')
		if len(text) == 1 and text.isalnum():
			self.jump(text.lower())

class MenuHandler(KeyHandler):
	"""Speaks a list of items, one per keypress, and exits on escape."""
	def __init__(self, items, title):
		self.items = items
		self.index = -1
		keymap = {
			b'\x1b[A': self.previous,
			b'\x1b[B': self.next,
			b'\x1bOA': self.previous,
			b'\x1bOB': self.next,
			b'k': self.previous,
			b'j': self.next,
		}
		super().__init__(keymap)
		say_ui("%s. %d items. Up and down to browse, escape to exit."
			% (title, len(items)))

	def speak_current(self):
		if not self.items:
			say_ui("empty")
			return
		say_ui(self.items[self.index])

	def next(self):
		if self.index >= len(self.items) - 1:
			self.index = len(self.items) - 1
			say_ui("end")
			return
		self.index += 1
		self.speak_current()

	def previous(self):
		if self.index <= 0:
			self.index = 0
			say_ui("start")
			return
		self.index -= 1
		self.speak_current()

	def handle_unknown_key(self, data):
		if data in (b'\x1b', b'q', b'\r', b'\n'):
			say_ui("exit")
			return KeyHandler.REMOVE

def key_spec_for(name):
	"""How a command is currently bound, in spoken form."""
	bindings = dict(DEFAULT_KEYS)
	if state.config.has_section('keys'):
		for cmd, spec in state.config.items('keys'):
			if cmd in COMMANDS:
				bindings[cmd] = spec
	spec = bindings.get(name, '')
	if not spec or spec.lower() in ('none', 'off'):
		return "unbound"
	return speak_key_spec(spec)

# Spoken names for keys that a synthesiser would otherwise skip or misread.
# Fixed here rather than taken from [symbols] so help wording never changes
# with the user's punctuation settings.
KEY_NAMES = {
	'/': 'slash', '?': 'question mark', ',': 'comma', '.': 'dot', '>': 'greater than',
	'<': 'less than', ';': 'semicolon', ':': 'colon', "'": 'apostrophe', '"': 'quote',
	'[': 'left bracket', ']': 'right bracket', '{': 'left brace', '}': 'right brace',
	'\\': 'backslash', '|': 'bar', '-': 'dash', '=': 'equals', '+': 'plus', '`': 'grave',
	'~': 'tilde', '!': 'exclamation', '@': 'at', '#': 'number sign', '$': 'dollar',
	'%': 'percent', '^': 'caret', '&': 'and', '*': 'star', '(': 'left paren', ')': 'right paren',
	'_': 'underscore', ' ': 'space',
}

def speak_key_spec(spec):
	"""Turn 'alt+shift+u' into 'alt shift U' and 'alt+/' into 'alt slash'.

	Single letters are spoken in capitals because synthesisers read a lone
	lowercase letter as a word or drop it ("alt a" becomes "alta"); capitals
	are read as letter names. Symbols are named, since most synthesisers say
	nothing at all for a bare slash or question mark.
	"""
	presses = []
	for chunk in spec.split():
		# The key itself may be '+', so split off modifiers from the left only.
		parts = []
		rest = chunk
		while True:
			head, sep, tail = rest.partition('+')
			if sep and tail and head.lower() in ('alt', 'meta', 'shift'):
				parts.append('alt' if head.lower() == 'meta' else head.lower())
				rest = tail
			else:
				break
		key = rest
		if key.lower().startswith('\\x'):
			word = "byte " + key[2:]
		elif len(key) == 1 and key.isalpha():
			word = key.upper()
		else:
			word = KEY_NAMES.get(key, key)
		parts.append(word)
		presses.append(" ".join(parts))
	if len(presses) == 2 and presses[0] == presses[1]:
		return presses[0] + " twice"
	return ", then ".join(presses)

def command_list():
	"""Every command with its current key, for browsing."""
	items = []
	for name in sorted(COMMANDS):
		items.append("%s, %s" % (name.replace('_', ' '), key_spec_for(name)))
	return items

def browse_commands():
	state.key_handlers.append(MenuHandler(command_list(), "Commands"))

class CopyHandler(KeyHandler):
	def __init__(self):
		keymap = {
			b'l': self.copy_line,
			b's': self.copy_screen,
			b'a': self.copy_history,
		}
		super().__init__(keymap)

	def copy_line(self):
		"""Copy the line under the review cursor, following wrapping."""
		top, bottom = logical_line_bounds(state.revy)
		try:
			if top == bottom:
				copy_text(state.revy, 0, state.revy, screen.columns - 1)
			else:
				# A wrapped line is one line to the user, so join the rows.
				parts = [line_text(y).rstrip() if y == bottom else line_text(y)
					for y in range(top, bottom + 1)]
				copy_to_clip("".join(parts))
		except Exception as e:
			logger.error("Copy failed: %s", e)
			say_ui("Failed")
		else:
			say_ui("line")
		return KeyHandler.REMOVE

	def copy_screen(self):
		try:
			copy_text(0, 0, screen.lines - 1, screen.columns - 1)
		except Exception as e:
			logger.error("Copy failed: %s", e)
			say_ui("Failed")
		else:
			say_ui("screen")
		return KeyHandler.REMOVE

	def copy_history(self):
		"""Copy the scrollback and the screen together."""
		try:
			copy_text(min_y(), 0, screen.lines - 1, screen.columns - 1)
		except Exception as e:
			logger.error("Copy failed: %s", e)
			say_ui("Failed")
		else:
			say_ui("all")
		return KeyHandler.REMOVE

	def handle_unknown_key(self, data):
		say_ui("unknown key")
		return KeyHandler.REMOVE

class TextReview:
	"""Review a line of text by character, word or whole, with the same keys
	as screen review: alt m , . for characters, alt j k l for words, alt u i o
	for the whole line, alt shift m and alt > for the ends. Left and right
	arrows move by character, up and down read the whole line, home and end
	jump to the ends.

	Subclasses keep the text in self.text and the cursor in self.pos, which
	sits between characters (0 is before the first, len(text) after the last).
	"""
	REVIEW_KEYS = {
		b'\x1b[D': 'char_left', b'\x1bOD': 'char_left', b'\x1bm': 'char_left',
		b'\x1b[C': 'char_right', b'\x1bOC': 'char_right', b'\x1b.': 'char_right',
		b'\x1b,': 'char_here',
		b'\x1bj': 'word_left', b'\x1bb': 'word_left', b'\x1b[1;3D': 'word_left',
		b'\x1bl': 'word_right', b'\x1bf': 'word_right', b'\x1b[1;3C': 'word_right',
		b'\x1bk': 'word_here',
		b'\x1b[A': 'say_all', b'\x1bOA': 'say_all', b'\x1b[B': 'say_all', b'\x1bOB': 'say_all',
		b'\x1bu': 'say_all', b'\x1bi': 'say_all', b'\x1bo': 'say_all',
		b'\x1bM': 'to_start', b'\x1b[H': 'to_start', b'\x1bOH': 'to_start', b'\x01': 'to_start',
		b'\x1b>': 'to_end', b'\x1b[F': 'to_end', b'\x1bOF': 'to_end', b'\x05': 'to_end',
	}

	def review(self, data):
		"""Handle a review key. Returns True if it was one."""
		name = self.REVIEW_KEYS.get(data)
		if name is None:
			return False
		getattr(self, 'review_' + name)()
		return True

	def _char_at(self, i):
		say_character(self.text[i])

	def review_char_left(self):
		if self.pos <= 0:
			say_ui("start")
			return
		self.pos -= 1
		self._char_at(self.pos)

	def review_char_right(self):
		if self.pos >= len(self.text):
			say_ui("end")
			return
		self.pos += 1
		if self.pos >= len(self.text):
			say_ui("end")
		else:
			self._char_at(self.pos)

	def review_char_here(self):
		if self.pos >= len(self.text):
			say_ui("end" if self.text else "blank")
		else:
			self._char_at(self.pos)

	def _word_bounds(self, i):
		t = self.text
		start = i
		while start > 0 and not t[start - 1].isspace():
			start -= 1
		end = i
		while end < len(t) and not t[end].isspace():
			end += 1
		return start, end

	def _say_word_at(self, i):
		if i >= len(self.text) or self.text[i].isspace():
			say_ui("space" if i < len(self.text) else "end")
			return
		start, end = self._word_bounds(i)
		word = self.text[start:end]
		if len(word) == 1:
			say_character(word)
		else:
			say(word)

	def review_word_left(self):
		t = self.text
		i = self.pos
		while i > 0 and not t[i - 1].isspace():
			i -= 1
		if i == 0:
			say_ui("start")
			self.pos = 0
			self._say_word_at(0)
			return
		while i > 0 and t[i - 1].isspace():
			i -= 1
		while i > 0 and not t[i - 1].isspace():
			i -= 1
		self.pos = i
		self._say_word_at(i)

	def review_word_right(self):
		t = self.text
		i = self.pos
		while i < len(t) and not t[i].isspace():
			i += 1
		while i < len(t) and t[i].isspace():
			i += 1
		if i >= len(t):
			say_ui("end")
			self.pos = len(t)
			return
		self.pos = i
		self._say_word_at(i)

	def review_word_here(self):
		i = self.pos
		if i >= len(self.text) and i > 0:
			i -= 1
		self._say_word_at(i)

	def review_say_all(self):
		if self.text.strip():
			say(self.text)
		else:
			say_ui("blank")

	def review_to_start(self):
		self.pos = 0
		say_ui("start")

	def review_to_end(self):
		self.pos = len(self.text)
		say_ui("end")

class BufferHandler(TextReview, KeyHandler):
	"""Typed input such as a question for the assistant.

	Typing inserts at the cursor. Review keys and arrows move through what has
	been typed. Backspace deletes before the cursor, delete removes the
	character at it. A paste from the clipboard arrives in one piece and is
	added whole, without the newline in it sending the question early.
	"""
	PASTE_START = b'\x1b[200~'
	PASTE_END = b'\x1b[201~'

	def __init__(self, on_accept=None, on_tab=None):
		self.on_accept = on_accept
		self.on_tab = on_tab
		self.pos = 0
		self.pasting = None
		super().__init__(keymap={})
		self.text = ''

	@property
	def buffer(self):
		return self.text

	@buffer.setter
	def buffer(self, value):
		self.text = value
		self.pos = len(value)

	def insert(self, text):
		self.text = self.text[:self.pos] + text + self.text[self.pos:]
		self.pos += len(text)

	def finish_paste(self, raw):
		text = raw.decode('utf-8', 'replace').strip('\r\n')
		text = re.sub(r'\s*[\r\n]+\s*', ' ', text)
		text = ''.join(c for c in text if c.isprintable())
		if not text:
			say_ui("nothing pasted")
			return
		self.insert(text)
		say_ui("pasted %d character%s" % (len(text), "" if len(text) == 1 else "s"))

	def process(self, data):
		# Bracketed paste can arrive across several reads; collect until it ends.
		if self.pasting is not None or data.startswith(self.PASTE_START):
			if self.pasting is None:
				data = data[len(self.PASTE_START):]
				self.pasting = b''
			self.pasting += data
			if self.PASTE_END in self.pasting:
				raw, _sep, rest = self.pasting.partition(self.PASTE_END)
				self.pasting = None
				self.finish_paste(raw)
				if rest:
					return self.process(rest)
			return
		if data == b'\r' or data == b'\n':
			if self.on_accept is not None:
				self.on_accept(self.text)
			return KeyHandler.REMOVE
		if data == b'\x1b':
			say_ui("cancelled")
			return KeyHandler.REMOVE
		if data == b'\t' and self.on_tab is not None:
			self.on_tab()
			return
		if data == b'\x15':
			# Control u clears what has been typed, as in a shell.
			self.text = ''
			self.pos = 0
			say_ui("cleared")
			return
		if data in (b'\x7f', b'\x08'):
			if self.pos > 0:
				say_character(self.text[self.pos - 1], deleted=True)
				self.text = self.text[:self.pos - 1] + self.text[self.pos:]
				self.pos -= 1
			else:
				say_ui("blank" if not self.text else "start")
			return
		if data == b'\x1b[3~':
			if self.pos < len(self.text):
				say_character(self.text[self.pos], deleted=True)
				self.text = self.text[:self.pos] + self.text[self.pos + 1:]
			else:
				say_ui("end")
			return
		if self.review(data):
			return
		if data.startswith(b'\x1b'):
			return          # other special keys are not text
		# errors='replace': a split multibyte key must not take TDSR down.
		text = data.decode('utf-8', 'replace')
		if len(text) > 1 and ('\r' in text or '\n' in text or len(text) > 4):
			# Without bracketed paste a paste still arrives as one chunk.
			self.finish_paste(data)
			return
		text = ''.join(c for c in text if c.isprintable())
		if not text:
			return
		self.insert(text)
		if len(text) == 1 and state.get_bool('key_echo', True):
			say_character(text)

class Synth:
	def __init__(self, speech_server):
		self.pipe = None
		self.speech_server = speech_server
		self.rate = None
		self.volume = None
		self.voice = None

	def start(self):
		try:
			self.pipe = subprocess.Popen(self.speech_server, stdin=subprocess.PIPE,
				stdout=subprocess.PIPE)
		except OSError as e:
			self.pipe = None
			logger.error("Could not start speech server %r: %s", self.speech_server, e)
			return

		if self.rate is not None:
			self.set_rate(self.rate)
		if self.volume is not None:
			self.set_volume(self.volume)
		if self.voice is not None:
			self.set_voice(self.voice)

	def send(self, text):
		text = text.encode('utf-8')
		try:
			if self.pipe is None or self.pipe.stdin is None:
				raise BrokenPipeError
			self.pipe.stdin.write(text)
			self.pipe.stdin.flush()
		except (BrokenPipeError, ValueError, OSError):
			# ValueError: writing to a pipe that has already been closed.
			try:
				self.start()
				if self.pipe is None or self.pipe.stdin is None:
					return
				self.pipe.stdin.write(text)
				self.pipe.stdin.flush()
			except (BrokenPipeError, ValueError, OSError):
				pass # We'll try to restart next time something is sent

	def set_rate(self, rate):
		self.rate = rate
		self.send('r%d\n' % rate)

	def set_volume(self, volume):
		self.volume = volume
		self.send('v%d\n' % volume)

	def set_voice(self, voice):
		self.voice = voice
		self.send('V%s\n' % voice)

	def fileno(self):
		"""The speech server's stdout, or None. Used to watch for 'done'."""
		if self.pipe is None or self.pipe.stdout is None:
			return None
		try:
			return self.pipe.stdout.fileno()
		except (ValueError, OSError):
			return None

	def read_events(self):
		"""Drain notifications from the speech server. Returns a list of lines."""
		fd = self.fileno()
		if fd is None:
			return []
		try:
			data = os.read(fd, 4096)
		except (OSError, ValueError):
			return []
		if not data:
			return []
		return data.decode('utf-8', 'replace').split()

	def close(self):
		if self.pipe is None:
			return
		try:
			if self.pipe.stdin is not None:
				self.pipe.stdin.close()
		except (BrokenPipeError, OSError):
			pass
		try:
			self.pipe.wait(timeout=2)
		except subprocess.TimeoutExpired:
			self.pipe.kill()

state = State()
speech_buffer = StringIO()
ai_client = None
child_fd = None
# True when speech goes through the bundled macOS backend, which can list voices.
mac_backend = False
lastkey = ""
def main(term_params):
	global CURSOR_TIMEOUT, CURSOR_SETTLE_MAX, signal_pipe, CONFIG_FILE
	parser = argparse.ArgumentParser()
	parser.add_argument('-s', '--speech-server', action='store', help='speech server command to run')
	parser.add_argument('--debug', action='store_true')
	parser.add_argument('--list-voices', action='store_true',
		help='list the available system voices and exit (macOS only)')
	parser.add_argument('--config', action='store', metavar='PATH',
		help='settings file to use instead of the usual one')
	parser.add_argument('--test-ai', action='store_true',
		help='send one test question using the ai settings, print the result and exit')
	parser.add_argument('program', action='store', nargs='*')
	args = parser.parse_args()
	if args.list_voices:
		termios.tcsetattr(0, termios.TCSADRAIN, term_params)
		if platform.system() != 'Darwin':
			print("--list-voices is only supported on macOS.")
			return
		with as_file(files(tdsr.backend).joinpath('mac.py')) as mac_path:
			subprocess.call([sys.executable, str(mac_path), '--list-voices'])
		return
	if args.test_ai:
		termios.tcsetattr(0, termios.TCSADRAIN, term_params)
		test_ai(args.config)
		return
	global mac_backend
	mac_backend = args.speech_server is None and platform.system() == 'Darwin'
	if args.speech_server is None:
		if platform.system() == 'Darwin':
			speech_server = [sys.executable, files(tdsr.backend).joinpath('mac.py')]
		else:
			# Works on Linux, hopefully other places too:
			speech_server = files(tdsr.backend).joinpath('speechdispatcher.py')
	else:
		speech_server = shlex.split(args.speech_server)
	if args.debug:
		logger.setLevel(logging.DEBUG)
		formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
		fh = logging.FileHandler('tdsr.log')
		fh.setFormatter(formatter)
		logger.addHandler(fh)
		logger.debug("TDSR started")
	if os.environ.get(NESTED_ENV):
		# Already inside TDSR, usually because a shell startup file launches it
		# and the shell TDSR starts reads that file again. A second copy would
		# only double the speech, so step aside quietly and let the shell carry on.
		logger.debug("%s is set; not starting a nested TDSR", NESTED_ENV)
		return
	rows, cols = get_terminal_size(0)
	global synth, screen
	synth = Synth(speech_server)
	synth.start()
	CONFIG_FILE, config_source = choose_config_file(args.config)
	logger.debug("Using config %s (%s)", CONFIG_FILE, config_source)
	seed_config_file(CONFIG_FILE, config_source)
	load_config_file(CONFIG_FILE)
	state.symbols_re = state.build_symbols_re()
	state.ignore_re = state.build_ignore_re()
	state.prompt_re = None
	state.prompt_strip_re = None
	state.punctuation_re = state.build_punctuation_re()
	scrollback = state.get_number('scrollback_lines', DEFAULT_SCROLLBACK_LINES)
	state.history = collections.deque(state.history, maxlen=max(0, scrollback))
	rate = state.get_number('rate', None)
	if rate is not None:
		synth.set_rate(rate)
	volume = state.get_number('volume', None)
	if volume is not None:
		synth.set_volume(volume)
	if 'voice' in state.config['speech']:
		synth.set_voice(state.config['speech']['voice'])
	elif 'voice_idx' in state.config['speech']:
		# Legacy setting: a bare index into the system voice list.
		synth.set_voice(state.config['speech']['voice_idx'])
	CURSOR_TIMEOUT = max(0.0, state.get_number('cursor_delay', CURSOR_TIMEOUT, float))
	CURSOR_SETTLE_MAX = max(CURSOR_TIMEOUT,
		state.get_number('cursor_settle_max', CURSOR_SETTLE_MAX, float))
	global ai_client, ai_pipe, child_fd
	ai_pipe = os.pipe()
	ai_client = tdsr.ai.AIClient(notify_fd=ai_pipe[1])
	if state.config.getboolean('ai', 'enabled', fallback=False):
		configure_ai(ai_client)
		if not ai_client.available():
			logger.error("AI enabled but no API key found (looked in %s)", ai_key_file())
		apply_saved_profile()
	pid, fd = os.forkpty()
	if pid == 0:
		handle_child(args)
	screen = MyScreen(cols, rows)
	pyte.Stream.csi['S'] = 'scroll_up'
	pyte.Stream.csi['T'] = 'scroll_down'
	stream = pyte.Stream()
	stream.attach(screen)
	screen.define_charset('B', '(')
	decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
	global keymap
	keymap = build_keymap()
	default_key_handler = KeyHandler(keymap, fd)
	state.key_handlers.append(default_key_handler)
	termios.tcsetattr(fd, termios.TCSADRAIN, term_params)
	plugins = dict(state.config.items('plugins'))
	for plugin, shortcut in plugins.items():
		default_key_handler.add(f'\x1b{shortcut}'.encode(), handle_plugin(plugin))

	global child_fd
	child_fd = fd
	resize_terminal(fd)
	signal_pipe = os.pipe()
	signal.signal(signal.SIGWINCH, handle_sigwinch)
	greeting = "tdsr, presented by Lighthouse of San Francisco"
	if state.config_problem:
		greeting += (". Your settings file could not be read. A copy was kept as "
			+ os.path.basename(state.config_problem))
	elif config_source == 'project':
		greeting += ". Using development settings"
	elif config_source != 'home':
		greeting += ". Using settings from " + os.path.basename(CONFIG_FILE)
	say_ui(greeting)
	while True:
		watch = [sys.stdin, fd, signal_pipe[0], ai_pipe[0]]
		synth_fd = synth.fileno()
		if synth_fd is not None:
			watch.append(synth_fd)
		r, w, e = select.select(watch, [], [], time_until_next_delayed())
		if synth_fd is not None and synth_fd in r:
			for event in synth.read_events():
				if event == 'done':
					state.speech_done = True
					say_all_continue()
		if ai_pipe[0] in r:
			os.read(ai_pipe[0], 1)
			handle_ai_result()
		if signal_pipe[0] in r:
			os.read(signal_pipe[0], 1)
			old_x, old_y = screen.cursor.x, screen.cursor.y
			rows, cols = resize_terminal(fd)
			screen.resize(rows, cols)
			state.revx = min(state.revx, cols - 1)
			if state.revy >= 0:
				state.revy = min(state.revy, rows - 1)
			screen.cursor.x = min(old_x, cols - 1)
			screen.cursor.y = min(old_y, rows - 1)
		if sys.stdin in r:
			bytes = os.read(0, 4096)
			process_input(bytes, fd)
		if fd in r:
			try:
				bytes = read_all(fd)
			except (EOFError, OSError):
				sb()
				synth.close()
				sys.exit(0)
			state.last_output_time = time.monotonic()
			state.command_running = True
			bytes, clip_events = clip_filter.feed(bytes)
			decoded_bytes = decoder.decode(bytes)
			x, y = screen.cursor.x, screen.cursor.y
			stream.feed(decoded_bytes)
			for event in clip_events:
				handle_clip_event(event)
			defer_cursor_read()
			if state.get_bool('cursor_tracking', True) and (screen.cursor.x != x or screen.cursor.y != y):
				state.revx, state.revy = screen.cursor.x, screen.cursor.y
			if not state.silence and not state.tempsilence:
				if speech_buffer.tell() > 0 and not state.delaying_output:
					schedule(0.005, read_buffer_scheduled)
			write_all(1, bytes)
		run_scheduled()
		check_command_done()
		say_all_watchdog()

def source_checkout_root():
	"""The repository folder when running from a source checkout, else None.

	A checkout has pyproject.toml beside the tdsr package. An installed copy
	(uv tool install, pip) lives in site-packages and has no such file.
	"""
	root = os.path.dirname(os.path.dirname(os.path.abspath(tdsr.__file__)))
	if os.path.isfile(os.path.join(root, 'pyproject.toml')):
		return root
	return None

def choose_config_file(explicit=None):
	"""Where settings are read from and saved to, and why.

	Order: --config, then TDSR_CONFIG, then ~/.tdsr-dev.cfg for a copy run
	from its own folder, then ~/.tdsr.cfg. Returns (path, source).
	"""
	if explicit:
		return os.path.abspath(os.path.expanduser(explicit)), 'option'
	env = os.environ.get('TDSR_CONFIG')
	if env:
		return os.path.abspath(os.path.expanduser(env)), 'environment'
	root = source_checkout_root()
	if root is not None:
		return DEV_CONFIG, 'project'
	return HOME_CONFIG, 'home'

def backup_unreadable_config(path):
	"""Keep a copy of a settings file that could not be read, before anything
	writes over it. Returns the backup path, or None."""
	backup = "%s.unreadable-%s" % (path, time.strftime('%Y%m%d-%H%M%S'))
	try:
		with open(path, 'rb') as src, open(backup, 'wb') as dst:
			dst.write(src.read())
	except OSError as e:
		logger.error("Could not back up %s: %s", path, e)
		return None
	logger.error("Kept the unreadable settings as %s", backup)
	state.config_problem = backup
	return backup

def load_config_file(path):
	"""Read settings, keeping a copy if the file cannot be read."""
	try:
		state.config.read(path)
	except configparser.Error as e:
		logger.error("Could not parse %s: %s", path, e)
		backup_unreadable_config(path)
	state.mark_loaded()

def seed_config_file(path, source):
	"""Create a missing config file.

	A new project config starts as a copy of ~/.tdsr.cfg when there is one, so
	the development copy keeps your rate and voice. ~/.tdsr.cfg itself is only
	read, never changed. Anything else starts from the shipped defaults.
	"""
	if os.path.exists(path):
		return
	if source == 'project':
		# Earlier versions kept settings inside the project folder. Carry the
		# newest of those over, text and all, rather than starting again.
		old = os.path.join(source_checkout_root() or '', PROJECT_CONFIG_NAME)
		if os.path.isfile(old):
			try:
				with open(old, 'rb') as src, open(path, 'wb') as dst:
					dst.write(src.read())
				logger.info("Moved settings from %s to %s", old, path)
				return
			except OSError as e:
				logger.error("Could not copy %s: %s", old, e)
	if source != 'home' and os.path.exists(HOME_CONFIG):
		try:
			state.config.read(HOME_CONFIG)
		except configparser.Error as e:
			logger.error("Could not read %s to copy it: %s", HOME_CONFIG, e)
	else:
		with as_file(DEFAULT_CONFIG) as default_config_path:
			state.config.read(default_config_path)
	state.save_config()

def check_command_done():
	"""Announce when a long running command stops producing output."""
	if not state.command_running:
		return
	if not state.get_bool('announce_command_done', False):
		state.command_running = False
		return
	if time.monotonic() - state.last_output_time < COMMAND_DONE_TIMEOUT:
		return
	state.command_running = False
	if state.say_all is None and not state.silence:
		say_ui("done")

def read_buffer_scheduled():
	state.delaying_output = False
	sb()

def write_all(fd, data):
	"""os.write() may write only part of the buffer; keep going until it's all out."""
	while data:
		try:
			written = os.write(fd, data)
		except InterruptedError:
			continue
		if not written:
			break
		data = data[written:]

def read_all(fd):
	bytes = os.read(fd, 4096)
	if bytes == b'':
		raise EOFError
	while has_more(fd):
		data = os.read(fd, 4096)
		if data == b'':
			raise EOFError
		bytes += data
	return bytes

def handle_child(args):
	# Lets a TDSR started from inside this one notice and not start again.
	os.environ[NESTED_ENV] = '1'
	if args.program:
			program = shlex.split(" ".join(args.program))
	else:
		program = [os.environ["SHELL"]]
	os.execv(program[0], program)

def process_input(bytes, fd):
	global lastkey
	# Busybox ash asks the terminal to send the cursor position, ignore it
	if re.match(br'\033\[\d+;\d+R', bytes):
		write_all(fd, bytes)
		return
	# Ignore OSC and DA
	if re.search(br'\x1b\][^\x07\x1b]*(\x07|\x1b\\)', bytes) or re.search(br'\x1b\[\?[0-9;]+c', bytes):
		write_all(fd, bytes)
		return
	if state.pass_next:
		state.pass_next = False
		write_all(fd, bytes)
		return
	# Any keypress stops a say all in progress.
	stop_say_all()
	lastkey = ""
	silence()
	state.delayed_functions = []
	cancel_cursor_read()
	state.tempsilence = False
	res = state.key_handlers[-1].process(bytes)
	if res == KeyHandler.REMOVE:
		state.key_handlers = state.key_handlers[:-1]
	lastkey = bytes.decode('utf-8', 'replace')

def handle_backspace():
	x = screen.cursor.x
	if x > 0:
		say_character(screen.buffer[screen.cursor.y][screen.cursor.x - 1].data, deleted=True)
	return KeyHandler.PASSTHROUGH

def handle_delete():
	say_character(screen.buffer[screen.cursor.y][screen.cursor.x].data, deleted=True)
	return KeyHandler.PASSTHROUGH

def handle_plugin(plugin_name):
	try:
		mod = importlib.import_module(f"plugins.{plugin_name}")
	except Exception as e:
		logger.error("Error loading plugin %s: %s", plugin_name, e)
		# Bind a handler that reports the failure instead of raising later.
		def broken():
			say_ui(f"Error loading plugin {plugin_name}")
		return broken

	try:
		prompt_matcher = re.compile(state.config.get('speech', 'prompt', fallback='.*'))
	except re.error as e:
		logger.error("Invalid prompt regex: %s", e)
		prompt_matcher = re.compile('.*')
	command = state.config.get('commands', plugin_name, fallback=None)
	command_matcher = None
	if command:
		try:
			command_matcher = re.compile(command)
		except re.error as e:
			logger.error("Invalid command regex for %s: %s", plugin_name, e)
	check_command = command_matcher is not None

	def handle():
		lines = []
		for i in reversed(range(len(screen.buffer))):
			line = "".join(screen.buffer[i][x].data for x in range(screen.columns)).strip()
			lines.append(line)
			if prompt_matcher.search(line) and check_command and command_matcher.search(line):
				break
		try:
			lines_to_read = mod.parse_output(lines)
			for line in lines_to_read:
				say(line)
		except Exception as e:
			logger.error("Plugin %s failed: %s", plugin_name, e)
			say_ui(f"Plugin {plugin_name} failed")

	return handle

def history_line(y):
	"""Text of a scrollback line. y is negative; -1 is the most recently scrolled off line."""
	idx = len(state.history) + y
	if idx < 0 or idx >= len(state.history):
		return ''
	return state.history[idx]

def line_text(y):
	"""Text of a review line. Negative y reads from scrollback, otherwise from the screen."""
	if y < 0:
		return history_line(y)
	return "".join(screen.buffer[y][x].data for x in range(screen.columns))

def cell(y, x):
	"""A single review cell. Negative y reads from scrollback, otherwise from the screen."""
	if y < 0:
		line = history_line(y)
		return line[x] if x < len(line) else ' '
	return screen.buffer[y][x].data

def clamp_review():
	"""Force the review cursor back into range.

	Scrollback can shrink underneath the cursor when the config is re-read or
	the deque reaches its limit, which would otherwise let the cursor address
	a line that no longer exists.
	"""
	low = min_y()
	if state.revy < low:
		state.revy = low
	elif state.revy > screen.lines - 1:
		state.revy = screen.lines - 1
	if state.revx < 0:
		state.revx = 0
	elif state.revx > screen.columns - 1:
		state.revx = screen.columns - 1

def last_content_y():
	"""Last screen row worth navigating to.

	The screen is always a full rectangle, so a shell sitting halfway down
	leaves blank rows below it. Treat the last row with text as the bottom,
	but never above the real cursor, which in a full screen editor can
	legitimately sit on a blank line below the text.
	"""
	if not state.get_bool('stop_at_last_line', True):
		return screen.lines - 1
	last = 0
	for y in range(screen.lines - 1, -1, -1):
		if line_text(y).strip():
			last = y
			break
	try:
		cursor_y = screen.cursor.y
	except AttributeError:
		cursor_y = 0
	return max(last, cursor_y)

def min_y():
	"""Topmost review line; negative when there is scrollback."""
	return -len(state.history)

def column_gap_width():
	"""How many spaces count as a column boundary."""
	width = state.get_number('column_gap', DEFAULT_COLUMN_GAP)
	return max(2, width)

def columns_enabled():
	return state.get_bool('announce_columns', True)

def split_columns(text, gap=None):
	"""Break a line into its columns.

	Returns a list of segments with the padding removed. A line with no wide
	gaps comes back as a single segment, so callers can treat one column and
	many columns the same way.
	"""
	if gap is None:
		gap = column_gap_width()
	parts = re.split(r' {%d,}' % gap, text.strip())
	return [p.strip() for p in parts if p.strip()]

def gap_between(y, x1, x2):
	"""Longest run of spaces strictly between two columns on a line."""
	low, high = (x1, x2) if x1 <= x2 else (x2, x1)
	run = best = 0
	for x in range(low + 1, high):
		if cell(y, x) == ' ':
			run += 1
			best = max(best, run)
		else:
			run = 0
	return best

def crossed_column(y, x1, x2):
	"""True when moving between two columns stepped over a column gap."""
	if not columns_enabled():
		return False
	return gap_between(y, x1, x2) >= column_gap_width()

def say_columns(text):
	"""Speak a line, pausing at each column gap.

	Each column is processed for speech on its own, then joined with the
	marker and spoken as one utterance. Doing it in that order matters: the
	marker is added after punctuation processing, so it stays a pause instead
	of being read out as "comma", and the whole line remains a single
	utterance so say all still gets exactly one completion event per line.
	"""
	if not columns_enabled():
		return say(text)
	segments = split_columns(text)
	if len(segments) < 2:
		return say(text)
	marker = state.config.get('speech', 'column_marker',
		fallback=DEFAULT_COLUMN_MARKER)
	spoken = [process_for_speech(seg) for seg in segments]
	spoken = [seg for seg in spoken if seg]
	if not spoken:
		return say(text)
	return say_ui((marker + " ").join(spoken))

def sayline(y):
	line = line_text(y).strip()
	if line == u'':
		say_ui(u'blank')
		return

	new_line = replace_duplicate_characters_with_count(line)

	say_columns(new_line)


def replace_duplicate_characters_with_count(line):
	symbols = state.config.get('speech', 'repeated_symbols_values', fallback='-=!#')
	matcher = re.compile(fr'([{symbols}])\1*')
	new_line = line
	if state.get_bool('repeated_symbols', False):
		results = matcher.finditer(line)
		for r in results:
			match = r.group()
			if len(match) > 1:
				new_line = new_line.replace(match, f"{len(match)} {r.groups()[0]}", 1)

	return new_line


def prevline():
	was_on_screen = state.revy >= 0
	state.revy -= 1
	if state.revy < min_y():
		say_ui("top")
		state.revy = min_y()
	elif was_on_screen and state.revy < 0:
		say_ui("history")
	sayline(state.revy)

def nextline():
	was_in_history = state.revy < 0
	bottom = last_content_y()
	if state.revy >= bottom:
		say_ui("bottom")
		state.revy = max(state.revy, bottom)
		sayline(state.revy)
		return
	state.revy += 1
	if was_in_history and state.revy == 0:
		say_ui("screen")
	sayline(state.revy)

def prevchar():
	state.revx -= 1
	if state.revx < 0:
		say_ui("left")
		state.revx = 0
	skip_to_previous_char()
	saychar(state.revy, state.revx)

def saychar(y, x, phonetically=False):
	char = cell(y, x)
	lchar = char.lower()
	if phonetically and lchar in PHONETICS:
		synth.send('s%s\n' % PHONETICS[lchar])
	else:
		say_character(char)

def nextchar():
	state.revx += max(1, wcwidth(cell(state.revy, state.revx)) or 1)
	if state.revx > screen.columns - 1:
		say_ui("right")
		state.revx = screen.columns - 1
		skip_to_previous_char()
	saychar(state.revy, state.revx)

def skip_to_previous_char():
	while state.revx > 0 and cell(state.revy, state.revx) == '':
		state.revx -= 1

def topOfScreen():
	state.revy = 0
	sayline(state.revy)

def topOfHistory():
	"""Oldest line held, scrollback included."""
	state.revy = min_y()
	state.revx = 0
	if state.revy < 0:
		say_ui("history")
	sayline(state.revy)

def bottomOfBuffer():
	"""Last line with content, leaving scrollback behind."""
	was_in_history = state.revy < 0
	state.revy = last_content_y()
	state.revx = 0
	if was_in_history:
		say_ui("screen")
	sayline(state.revy)

def bottomOfScreen():
	state.revy = last_content_y()
	sayline(state.revy)

def startOfLine():
	state.revx = 0
	saychar(state.revy, state.revx)

def endOfLine():
	state.revx = screen.columns - 1
	saychar(state.revy, state.revx)

class MyScreen(pyte.Screen):

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.saved_cursor = None
		self.saved_buffer = None

	def index(self):
		"""Capture the line about to scroll off the top into scrollback, then scroll."""
		top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
		# Only capture full-screen scrolling on the normal (non alternate) screen.
		if self.cursor.y == bottom and top == 0 and self.saved_buffer is None and state.history.maxlen:
			line = "".join(self.buffer[top][x].data for x in range(self.columns))
			state.history.append(line.rstrip())
			if state.revy < 0:
				# Keep the review cursor pointing at the same text as history shifts.
				state.revy -= 1
				if state.revy < min_y():
					state.revy = min_y()
		super().index()

	def set_margins(self, top=None, bottom=None, private=None):
		if top == 0 and bottom is None:
			top = None
		super().set_margins(top=top, bottom=bottom)

	def draw(self, text):
		for c in text:
			self.draw2(c)

	def draw2(self, text):
		global lastkey
		logger.debug("Drawing %r", text)
		add_to_buffer = True
		if text == lastkey:
			add_to_buffer = False
			if len(text) == 1 and state.get_bool('key_echo', True):
				say_character(text)
			state.echoed = (text, time.monotonic())
		elif state.echoed and text == state.echoed[0] \
				and time.monotonic() - state.echoed[1] < ECHO_REPEAT_WINDOW:
			# The same character drawn a second time: the line was repainted.
			add_to_buffer = False
			state.echoed = None
		lastkey = ""
		if screen.cursor.y == state.last_drawn_y and screen.cursor.x - state.last_drawn_x > 1:
			speech_buffer.write(' ')
		super(MyScreen, self).draw(text)
		if add_to_buffer and not state.silence and not state.tempsilence:
			if speech_buffer.tell() < MAX_SPEECH_BUFFER:
				speech_buffer.write(text)
			state.last_drawn = text
			state.last_drawn_x = screen.cursor.x
			state.last_drawn_y = screen.cursor.y

	def bell(self):
		if state.get_bool('announce_bell', True):
			say_ui("bell")
		try:
			super().bell()
		except AttributeError:
			pass

	def tab(self):
		if not state.silence:
			speech_buffer.write(' ')
		super().tab()

	def linefeed(self):
		if state.get_bool('line_pause', True):
			sb()
		else:
			speech_buffer.seek(0, SEEK_END)
			speech_buffer.write(' ')
		super(MyScreen, self).linefeed()

	def backspace(self):
		if self.cursor.x > 0 and speech_buffer.tell() > 0:
			speech_buffer.seek(speech_buffer.tell() - 1)
		super(MyScreen, self).backspace()

	def report_device_status(self, *args, **kwargs):
		if "private" in kwargs:
			del kwargs["private"]
		return super().report_device_status(*args, **kwargs)

	def reset_mode(self, *modes, **kwargs):
		if 3 in modes:
			modes = list(modes)
			modes.remove(3)
		if 1049 in modes and self.saved_cursor is not None:
			self.cursor = self.saved_cursor
			self.buffer = self.saved_buffer
			self.saved_cursor = None
			self.saved_buffer = None
			self.dirty.update(range(self.lines))
		super().reset_mode(*modes, **kwargs)

	def set_mode(self, *modes, **kwargs):
		if 3 in modes:
			modes = list(modes)
			modes.remove(3)
		if 1049 in modes:
			self.saved_cursor = copy.copy(self.cursor)
			self.saved_buffer = copy.deepcopy(self.buffer)
			self.dirty.update(range(self.lines))
			self.cursor_position()
			self.buffer.clear()
		super().set_mode(*modes, **kwargs)

	def erase_in_display(self, how=0, private=False):
		if how == 3:
			return
		super().erase_in_display(how, private=private)

	def select_graphic_rendition(self, *attrs, **kwargs):
		if 'private' in kwargs:
			return
		return super().select_graphic_rendition(*attrs)

	def cursor_up(self, count=None):
		super().cursor_up(count=count)
		speech_buffer.write(' ')

	def scroll_down(self,	count):
		if count == 0:
			count = 1
		top,	bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
		y = self.cursor.y
		self.cursor.y = top
		for i in range(count):
			self.reverse_index()
		self.cursor.y = y

	def scroll_up(self, count):
		if count == 0:
			count = 1
		top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
		y = self.cursor.y
		self.cursor.y = bottom
		for i in range(count):
			self.index()
		self.cursor.y = y

	def cursor_position(self, line=None, column=None):
		if (speech_buffer.tell() > 0 and line == state.last_drawn_y
				and column == state.last_drawn_x + 1
				and getattr(state, 'last_drawn', '')
				and ord(state.last_drawn) > 127):
			speech_buffer.seek(speech_buffer.tell() - 1)
			c = speech_buffer.read(1)
			if c == ' ':
				speech_buffer.truncate()
				speech_buffer.seek(speech_buffer.tell() - 1)
		if column and speech_buffer.tell() > 0 and line > state.last_drawn_y and column == 1:
			speech_buffer.write(' ')
		super().cursor_position(line, column)

	def cursor_to_line(self, line=None):
		super().cursor_to_line(line)
		speech_buffer.write(' ')

def all_lines():
	"""Every reviewable line, oldest scrollback first, as (y, text) pairs."""
	out = []
	for y in range(min_y(), screen.lines):
		out.append((y, line_text(y)))
	return out

def repeat_last():
	"""Say the previous utterance again."""
	if not state.last_utterance:
		say_ui("nothing to repeat")
		return
	last = state.last_utterance
	synth.send("s" + last + "\n")
	state.last_utterance = last

def spell_last():
	"""Spell the previous utterance out character by character."""
	if not state.last_utterance:
		say_ui("nothing to repeat")
		return
	last = state.last_utterance
	say(' '.join(last), force_process_symbols=True)
	state.last_utterance = last

def where_am_i():
	"""Describe where the review cursor is sitting."""
	clamp_review()
	if state.revy < 0:
		depth = -state.revy
		say_ui("history line %d of %d, column %d" % (
			len(state.history) - depth + 1, len(state.history), state.revx + 1))
	else:
		say_ui("screen line %d of %d, column %d" % (
			state.revy + 1, screen.lines, state.revx + 1))

def get_prompt_strip_re():
	"""Pattern removed from the front of a prompt line, or None."""
	if not state.get_bool('strip_prompt', True):
		return None
	if state.prompt_strip_re is not None:
		return state.prompt_strip_re
	try:
		pattern = state.config.get('speech', 'prompt_strip_pattern',
			fallback=DEFAULT_PROMPT_STRIP_RE)
	except configparser.Error as e:
		# A literal percent sign in the value trips configparser interpolation.
		logger.error("Unreadable prompt_strip_pattern (write %% as \\x25): %s", e)
		pattern = DEFAULT_PROMPT_STRIP_RE
	if pattern.strip() in LEGACY_PROMPT_STRIP_RES:
		pattern = DEFAULT_PROMPT_STRIP_RE
	try:
		state.prompt_strip_re = re.compile(pattern)
	except re.error as e:
		logger.error("Invalid prompt_strip_pattern %r: %s", pattern, e)
		state.prompt_strip_re = re.compile(DEFAULT_PROMPT_STRIP_RE)
	return state.prompt_strip_re

def strip_prompt_prefix(text):
	"""Drop the noisy prefix of a prompt line, keeping the command.

	Only applies when something is left afterwards, so pressing enter on an
	empty prompt still reads the whole thing and you can tell where you are.
	"""
	matcher = get_prompt_strip_re()
	if matcher is None or not text:
		return text
	# Full screen programs (nano, vim, less) use the alternate screen; their
	# lines are file contents, never prompts, so leave them alone.
	if getattr(screen, 'saved_buffer', None) is not None:
		return text
	# Only strip a line that actually looks like a prompt.
	prompt = get_prompt_re().search(text)
	if not prompt:
		return text
	# An empty prompt with no command is read in full, so pressing enter or
	# clearing the line still tells you where you are.
	if not text[prompt.end():].strip():
		return text
	stripped = matcher.sub('', text, count=1)
	if stripped == text or not stripped.strip():
		return text
	return stripped

def get_prompt_re():
	"""Compiled prompt pattern, falling back if the configured one is unusable."""
	if state.prompt_re is not None:
		return state.prompt_re
	pattern = state.config.get('speech', 'prompt', fallback=None)
	# '.*' is the shipped default and matches every line, which is useless for
	# jumping, so treat it as unset.
	if not pattern or pattern == '.*':
		pattern = DEFAULT_PROMPT_RE
	try:
		state.prompt_re = re.compile(pattern)
	except re.error as e:
		logger.error("Invalid prompt regex %r: %s", pattern, e)
		state.prompt_re = re.compile(DEFAULT_PROMPT_RE)
	return state.prompt_re

def move_to_line(y, announce_history=True):
	"""Put the review cursor on a line and read it."""
	was_in_history = state.revy < 0
	state.revy = y
	state.revx = 0
	if announce_history:
		if y < 0 and not was_in_history:
			say_ui("history")
		elif y >= 0 and was_in_history:
			say_ui("screen")
	sayline(y)

def prev_prompt():
	"""Move to the previous shell prompt."""
	matcher = get_prompt_re()
	clamp_review()
	for y in range(state.revy - 1, min_y() - 1, -1):
		if matcher.search(line_text(y)):
			move_to_line(y)
			return
	say_ui("no previous prompt")

def next_prompt():
	"""Move to the next shell prompt."""
	matcher = get_prompt_re()
	clamp_review()
	for y in range(state.revy + 1, screen.lines):
		if matcher.search(line_text(y)):
			move_to_line(y)
			return
	say_ui("no next prompt")

def find():
	say_ui("find")
	state.key_handlers.append(BufferHandler(on_accept=find2))

def find2(text):
	text = text.strip()
	if not text:
		say_ui("cancelled")
		return
	state.find_text = text
	find_next(announce_wrap=False)

def find_in_line(text, y, start_x, backwards=False):
	"""Column of a match on one line, or None."""
	line = line_text(y).lower()
	needle = text.lower()
	if backwards:
		end = start_x if start_x is not None else len(line)
		idx = line.rfind(needle, 0, max(0, end))
	else:
		start = start_x if start_x is not None else 0
		idx = line.find(needle, start)
	return None if idx == -1 else idx

def find_next(announce_wrap=True):
	"""Next match at or after the review cursor, wrapping once."""
	clamp_review()
	if not state.find_text:
		say_ui("no search text")
		return
	text = state.find_text
	top, bottom = min_y(), screen.lines - 1
	# Rest of the current line first, then each following line.
	idx = find_in_line(text, state.revy, state.revx + 1)
	if idx is not None:
		state.revx = idx
		sayline(state.revy)
		return
	for y in range(state.revy + 1, bottom + 1):
		idx = find_in_line(text, y, 0)
		if idx is not None:
			state.revy, state.revx = y, idx
			sayline(y)
			return
	for y in range(top, state.revy + 1):
		idx = find_in_line(text, y, 0)
		if idx is not None:
			if announce_wrap:
				say_ui("wrapped")
			state.revy, state.revx = y, idx
			sayline(y)
			return
	say_ui("not found")

def find_previous():
	"""Previous match before the review cursor, wrapping once."""
	clamp_review()
	if not state.find_text:
		say_ui("no search text")
		return
	text = state.find_text
	top, bottom = min_y(), screen.lines - 1
	idx = find_in_line(text, state.revy, state.revx, backwards=True)
	if idx is not None:
		state.revx = idx
		sayline(state.revy)
		return
	for y in range(state.revy - 1, top - 1, -1):
		idx = find_in_line(text, y, None, backwards=True)
		if idx is not None:
			state.revy, state.revx = y, idx
			sayline(y)
			return
	for y in range(bottom, state.revy - 1, -1):
		idx = find_in_line(text, y, None, backwards=True)
		if idx is not None:
			say_ui("wrapped")
			state.revy, state.revx = y, idx
			sayline(y)
			return
	say_ui("not found")

class SayAll:
	"""Reads consecutive lines, waiting for each to finish being spoken."""
	def __init__(self, y):
		self.y = y
		self.done = False
		self.spoke_at = 0.0

	def speak_current(self):
		"""Speak the current line. Returns False when there is nothing left."""
		while self.y <= screen.lines - 1:
			text = line_text(self.y).strip()
			if text:
				prefix = (indent_prefix(self.y)
					if state.get_bool('announce_indent', False) else '')
				state.revy = self.y
				state.revx = 0
				state.speech_done = False
				self.spoke_at = time.monotonic()
				if prefix:
					spoke = say_with_prefix(prefix, text)
				else:
					spoke = say_columns(text)
				self.y += 1
				if spoke:
					return True
				# Nothing audible came out of that line, so no completion event
				# is coming. Move on rather than waiting for the watchdog.
				state.speech_done = True
				continue
			self.y += 1
		self.done = True
		return False

def start_say_all():
	"""Read from the review cursor to the bottom of the screen."""
	stop_say_all(announce=False)
	clamp_review()
	state.say_all = SayAll(state.revy)
	if not state.say_all.speak_current():
		state.say_all = None
		say_ui("nothing to read")

def stop_say_all(announce=True):
	if state.say_all is None:
		return False
	state.say_all = None
	if announce:
		silence()
	return True

def say_all_watchdog():
	"""Advance say all if the speech server never reported 'done'.

	Some speech-dispatcher builds do not deliver end notifications. Without
	this, say all would stop after the first line on those systems.
	"""
	if state.say_all is None:
		return
	if state.speech_done:
		return
	if time.monotonic() - state.say_all.spoke_at < SAY_ALL_FALLBACK:
		return
	logger.debug("Say all watchdog fired; no done event from the speech server")
	say_all_continue()

def say_all_continue():
	"""Called when the speech server reports an utterance finished."""
	if state.say_all is None:
		return
	if not state.say_all.speak_current():
		state.say_all = None

def indent_prefix(y):
	"""Spoken description of a line's leading whitespace, or ''."""
	line = line_text(y)
	stripped = line.lstrip(' \t')
	if not stripped:
		return ''
	indent = line[:len(line) - len(stripped)]
	if not indent:
		return ''
	columns = 0
	for ch in indent:
		columns += 8 - (columns % 8) if ch == '\t' else 1
	unit = state.get_number('indent_unit', 1)
	if unit < 1:
		unit = 1
	level = columns // unit
	if level < 1:
		return ''
	return "indent %d" % level

def say_line_with_indent(y):
	"""sayline() plus an indentation announcement when enabled."""
	if state.get_bool('announce_indent', False):
		prefix = indent_prefix(y)
		if prefix:
			say_with_prefix(prefix, line_text(y))
			return
	sayline(y)

def logical_line_bounds(y):
	"""First and last screen row of the wrapped line containing y.

	A row is a continuation of the one above when the row above fills the
	full width of the screen, which is how terminals wrap.
	"""
	if y < 0 or not state.get_bool('logical_lines', True):
		return y, y
	top = y
	while top > 0 and row_is_full(top - 1):
		top -= 1
	bottom = y
	while bottom < screen.lines - 1 and row_is_full(bottom):
		bottom += 1
	return top, bottom

def row_is_full(y):
	"""True when a screen row has text in its final column."""
	if y < 0 or y > screen.lines - 1:
		return False
	return cell(y, screen.columns - 1) not in ('', ' ')

def say_logical_line():
	"""Read the whole wrapped line the review cursor is on."""
	top, bottom = logical_line_bounds(state.revy)
	if top == bottom:
		say_line_with_indent(state.revy)
		return
	parts = [line_text(y).rstrip() if y == bottom else line_text(y)
		for y in range(top, bottom + 1)]
	text = "".join(parts).strip()
	say(text if text else "blank")

def highlighted_regions(y):
	"""Runs of reverse-video text on a line, as a list of strings."""
	if y < 0:
		return []   # scrollback keeps text only, not attributes
	runs = []
	current = []
	for x in range(screen.columns):
		try:
			ch = screen.buffer[y][x]
		except Exception:
			break
		if getattr(ch, 'reverse', False):
			current.append(ch.data)
		elif current:
			runs.append("".join(current).strip())
			current = []
	if current:
		runs.append("".join(current).strip())
	return [r for r in runs if r]

def say_highlighted():
	"""Speak reverse-video text, which is usually the selected item."""
	found = []
	for y in range(0, screen.lines):
		for run in highlighted_regions(y):
			found.append((y, run))
	if not found:
		say_ui("no highlighted text")
		return
	if len(found) == 1:
		y, text = found[0]
		state.revy, state.revx = y, 0
		say(text)
		return
	runs = ", ".join(process_for_speech(t) for _y, t in found)
	say_ui("%d highlighted, %s" % (len(found), runs))

def extend_selection(move):
	"""Move the review cursor, starting a selection if one isn't running."""
	if state.copy_x is None:
		clamp_review()
		state.copy_y = state.revy
		state.copy_x = 0 if selection_mode() == 'lines' else state.revx
	move()

def shift_arrow_up():
	if not state.get_bool('shift_arrow_selection', False):
		return KeyHandler.PASSTHROUGH
	extend_selection(prevline)

def shift_arrow_down():
	if not state.get_bool('shift_arrow_selection', False):
		return KeyHandler.PASSTHROUGH
	extend_selection(nextline)

def shift_arrow_left():
	if not state.get_bool('shift_arrow_selection', False):
		return KeyHandler.PASSTHROUGH
	extend_selection(prevchar)

def shift_arrow_right():
	if not state.get_bool('shift_arrow_selection', False):
		return KeyHandler.PASSTHROUGH
	extend_selection(nextchar)

def ai_enabled():
	"""True when the assistant is switched on and has a key."""
	if not state.config.getboolean('ai', 'enabled', fallback=False):
		return False
	return ai_client is not None and ai_client.available()

def ai_bool(option, fallback):
	try:
		return state.config.getboolean('ai', option, fallback=fallback)
	except ValueError:
		logger.error("Invalid ai.%s, using %r", option, fallback)
		return fallback

def configure_ai(client):
	"""Apply the ai section of the settings to a client."""
	max_tokens = ai_number('max_tokens', tdsr.ai.DEFAULT_MAX_TOKENS)
	model = state.config.get('ai', 'model', fallback=tdsr.ai.DEFAULT_MODEL)
	model, max_tokens, upgraded = tdsr.ai.upgrade_model_settings(model, max_tokens)
	if upgraded:
		logger.info("Upgraded retired default AI model to %s", model)
		state.config['ai']['model'] = model
		state.config['ai']['max_tokens'] = str(max_tokens)
		state.save_config()
	client.configure(
		api_key=ai_api_key(),
		endpoint=state.config.get('ai', 'endpoint', fallback=tdsr.ai.DEFAULT_ENDPOINT),
		model=model,
		max_tokens=max_tokens,
		effort=state.config.get('ai', 'effort', fallback=''),
		provider=state.config.get('ai', 'provider', fallback='auto'),
		auth_header=state.config.get('ai', 'auth_header', fallback=''),
		ca_bundle=state.config.get('ai', 'ca_bundle', fallback=''),
		verify_tls=ai_bool('verify_tls', True),
		timeout=ai_number('timeout', tdsr.ai.REQUEST_TIMEOUT))
	logger.debug("AI provider %s at %s", client.provider, client.endpoint)

def test_ai(explicit_config=None, out=print):
	"""Check the AI settings from an ordinary shell, printing what happens.

	Runs without starting the screen reader, so the result stays on screen to
	be reviewed with VoiceOver or TDSR afterwards.
	"""
	global CONFIG_FILE
	CONFIG_FILE, source = choose_config_file(explicit_config)
	out("Settings file: %s" % CONFIG_FILE)
	if not os.path.exists(CONFIG_FILE):
		out("That file does not exist yet. Start TDSR once to create it.")
		return False
	try:
		state.config.read(CONFIG_FILE)
	except configparser.Error as e:
		out("The settings file could not be read: %s" % e)
		return False
	state.mark_loaded()
	if not ai_bool('enabled', False):
		out("Note: enabled is not True in the ai section, so alt slash will not "
			"work yet. Testing the other settings anyway.")
	client = tdsr.ai.AIClient()
	configure_ai(client)
	out("Provider: %s" % client.provider)
	out("Address: %s" % client.endpoint)
	out("Model: %s" % client.model)
	if not client.available():
		out("No key found. Put it in %s" % ai_key_file())
		out("Looked in: %s" % ", ".join(ai_key_candidates()))
		return False
	found = next((p for p in ai_key_candidates() if tdsr.ai.read_key_file(p)), None)
	out("Key found in %s." % found if found else "Key found in the environment or settings.")
	if client.verify_tls is False:
		out("Warning: certificate checking is off.")
	elif client.ca_bundle:
		out("Certificates: %s" % client.ca_bundle)
	out("Sending a test question...")
	ok, message = client.test()
	out(("Success. Reply: " if ok else "Failed. ") + message)
	return ok

def default_key_file():
	"""Key file kept beside the settings file in use.

	~/.tdsr.cfg pairs with ~/.tdsr.key; a folder copy's tdsr.cfg pairs with
	tdsr.key in the same folder. Keeping the key out of the settings file
	means settings can be shared or uploaded without leaking it.
	"""
	if os.path.abspath(CONFIG_FILE) == os.path.abspath(HOME_CONFIG):
		return os.path.expanduser(HOME_KEY_FILE)
	if os.path.abspath(CONFIG_FILE) == os.path.abspath(DEV_CONFIG):
		return os.path.expanduser(DEV_KEY_FILE)
	return os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), PROJECT_KEY_NAME)

def ai_key_file():
	"""Where the key is expected. A relative api_key_file is taken from the
	settings file's folder, not from wherever TDSR happened to be started."""
	path = state.config.get('ai', 'api_key_file', fallback='').strip()
	if not path:
		return default_key_file()
	path = os.path.expanduser(path)
	if not os.path.isabs(path):
		path = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), path)
	return path

def ai_key_candidates():
	"""Key files to try, in order: the expected one, then ~/.tdsr.key so a
	fresh folder copy still works before its own key file exists."""
	paths = [ai_key_file(), default_key_file()]
	root = source_checkout_root()
	if root:
		paths.append(os.path.join(root, PROJECT_KEY_NAME))
	paths.append(os.path.expanduser(HOME_KEY_FILE))
	unique = []
	for p in paths:
		if os.path.abspath(p) not in [os.path.abspath(u) for u in unique]:
			unique.append(p)
	return unique

def ai_api_key():
	"""API key: environment, then the key file, then api_key in settings."""
	key = os.environ.get('TDSR_AI_API_KEY') or os.environ.get('ANTHROPIC_API_KEY')
	if key:
		return key.strip()
	for path in ai_key_candidates():
		key = tdsr.ai.read_key_file(path)
		if key:
			return key
	return state.config.get('ai', 'api_key', fallback='').strip()

def ai_number(option, fallback):
	"""Read a number from the ai config section, tolerating junk."""
	raw = state.config.get('ai', option, fallback=None)
	if raw is None:
		return fallback
	try:
		return int(raw)
	except (ValueError, TypeError):
		logger.error("Invalid ai.%s: %r, using %r", option, raw, fallback)
		return fallback

def ai_context_mode():
	mode = state.config.get('ai', 'context_mode', fallback='screen')
	mode = mode.strip().lower()
	if mode not in ('screen', 'scrollback', 'selection'):
		logger.error("Unknown ai.context_mode %r, using 'screen'", mode)
		return 'screen'
	return mode

def ai_context_lines():
	"""Candidate lines for the model, according to the context mode."""
	mode = ai_context_mode()
	if mode == 'selection':
		if state.copy_x is None:
			return None, "no selection"
		start_y = min(state.copy_y, state.revy)
		end_y = max(state.copy_y, state.revy)
		lines = [line_text(y).rstrip() for y in range(start_y, end_y + 1)]
		return lines, None
	if mode == 'scrollback':
		first = min_y()
	else:
		first = 0
	lines = [line_text(y).rstrip() for y in range(first, screen.lines)]
	# Trailing blank rows carry nothing; drop them so the model sees the end.
	while lines and not lines[-1].strip():
		lines.pop()
	return lines, None

def ai_context():
	"""Text to send, plus a short spoken description of it.

	Returns (text, description, error). The description is spoken before a
	request so it is always clear what left the machine.
	"""
	lines, problem = ai_context_lines()
	if problem:
		return None, None, problem
	if not lines:
		return None, None, "nothing on screen to send"
	max_lines = max(1, ai_number('context_lines', tdsr.ai.DEFAULT_CONTEXT_LINES))
	max_chars = max(200, ai_number('context_chars', tdsr.ai.DEFAULT_CONTEXT_CHARS))
	text, kept, dropped = tdsr.ai.apply_budget(lines, max_lines, max_chars)
	if not text.strip():
		return None, None, "nothing on screen to send"
	if state.config.getboolean('ai', 'redact', fallback=True):
		text = tdsr.ai.redact(text)
	if dropped:
		# Tell the model, so it does not answer about text it cannot see.
		text = ("[%d earlier lines not included]\n" % dropped) + text
	mode = ai_context_mode()
	description = "sending %d line%s" % (kept, "" if kept == 1 else "s")
	if mode == 'selection':
		description += " from the selection"
	elif mode == 'scrollback':
		description += " including scrollback"
	if dropped:
		description += ", %d trimmed" % dropped
	return text, description, None

def describe_ai_context():
	"""Say what a question would send, in full, without sending anything."""
	text, description, problem = ai_context()
	if problem:
		say_ui(problem)
		return
	chars = len(text)
	tokens = tdsr.ai.estimate_tokens(text)
	profile = ai_client.profile_name() if ai_client else None
	extra = ai_history_extra(text)
	if extra:
		pairs, included, dropped, hchars = extra
		description += ", " + describe_history(included, dropped, tdsr.ai.history_words(pairs))
		chars += hchars
		tokens = tdsr.ai.estimate_tokens_from_chars(chars)
	elif ai_client is not None:
		description += ", history off"
	parts = [description,
		"about %d words, %d characters, roughly %d tokens"
		% (tdsr.ai.words(text) + (tdsr.ai.history_words(extra[0]) if extra else 0),
		   chars, tokens)]
	if profile:
		parts.append("profile %s" % profile)
	else:
		parts.append("no profile")
	say_ui(". ".join(parts))

def cancel_ai():
	"""Give up on a request that is taking too long."""
	if ai_client is None or not ai_client.abandon():
		say_ui("nothing to cancel")
		return
	say_ui("cancelled")

def ai_usage_report():
	"""Say what has been used so far, using real counts from the API."""
	if ai_client is None:
		say_ui("AI is not configured")
		return
	say_ui("%s. %s" % (ai_client.last_usage_summary(), ai_client.session_summary()))

def ask_ai():
	"""Ask a question about what is on the screen."""
	if not ai_enabled():
		if state.config.getboolean('ai', 'enabled', fallback=False):
			say_ui("AI is on but no key was found. Put it in " + ai_key_file())
		else:
			say_ui("AI is not configured. See the readme.")
		return
	if ai_client.busy:
		say_ui("Still working on the previous question.")
		return
	say_ui("Ask, history on" if getattr(ai_client, 'history_on', False) else "Ask")
	state.key_handlers.append(BufferHandler(on_accept=ask_ai2, on_tab=toggle_ai_history))

def ask_ai2(question):
	question = question.strip()
	if not question:
		say_ui("cancelled")
		return
	context, description, problem = ai_context()
	if problem:
		say_ui(problem)
		return
	extra = ai_history_extra(context)
	history = None
	if extra:
		pairs, included, dropped, _chars = extra
		history = (pairs, dropped)
		description += ", %s, about %d words in all" % (
			describe_history(included, dropped, None),
			tdsr.ai.words(context) + tdsr.ai.history_words(pairs) + tdsr.ai.words(question))
	ok = ai_client.ask(question, context, history) if history else ai_client.ask(question, context)
	if not ok:
		say_ui("Still working on the previous question.")
		return
	say_ui("Thinking, %s" % description)

def ai_history_extra(context):
	"""History that would go with a question, or None when off or empty."""
	if ai_client is None or not getattr(ai_client, 'history_on', False):
		return None
	ai_client.history_chars = max(0, ai_number('history_chars', tdsr.ai.DEFAULT_HISTORY_CHARS))
	plan = ai_client.history_plan(context or "")
	if not plan[1] and not plan[2]:
		return None
	return plan

def describe_history(included, dropped, word_count):
	parts = "plus %d earlier exchange%s" % (included, "" if included == 1 else "s")
	if word_count is not None:
		parts += ", about %d words" % word_count
	if dropped:
		parts += ", %d oldest left out" % dropped
	return parts

def toggle_ai_history():
	"""Switch sending the chat history with questions, for this window only."""
	if ai_client is None or not ai_enabled():
		say_ui("AI is not configured")
		return
	ai_client.history_on = not ai_client.history_on
	if not ai_client.history:
		say_ui("history on, nothing saved yet" if ai_client.history_on else "history off")
		return
	context, _description, problem = ai_context()
	if problem:
		context = ""
	was_on = ai_client.history_on
	ai_client.history_on = True
	extra = ai_history_extra(context)
	ai_client.history_on = was_on
	if not extra:
		say_ui("history on" if was_on else "history off")
		return
	pairs, included, dropped, _chars = extra
	count = tdsr.ai.history_words(pairs)
	if was_on:
		say_ui("history on, adds %d earlier exchange%s, about %d words%s"
			% (included, "" if included == 1 else "s", count,
			   ", %d oldest left out" % dropped if dropped else ""))
	else:
		say_ui("history off, about %d fewer words" % count)

def clear_ai_history():
	"""Forget the saved conversation without closing the window."""
	if ai_client is None:
		say_ui("AI is not configured")
		return
	count = ai_client.clear_history()
	say_ui("cleared %d exchange%s" % (count, "" if count == 1 else "s") if count
		else "no history to clear")

def split_for_review(text):
	"""Break a reply into reviewable chunks.

	A model reply is often one long paragraph, which review_ai_answer would
	otherwise present as a single unreadable item. Split on sentences when
	there are no line breaks to work with.
	"""
	lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
	chunks = []
	for line in lines:
		if len(line) <= 200:
			chunks.append(line)
			continue
		parts = re.split(r'(?<=[.!?])\s+', line)
		buf = ''
		for part in parts:
			if buf and len(buf) + len(part) > 200:
				chunks.append(buf.strip())
				buf = part
			else:
				buf = (buf + ' ' + part).strip()
		if buf:
			chunks.append(buf.strip())
	return chunks

def handle_ai_result():
	"""Called from the main loop when a reply has arrived."""
	result = ai_client.take_result()
	if result is None:
		return
	# A reply must not talk over a continuous read.
	stop_say_all(announce=False)
	status, payload = result
	if status == 'error':
		state.ai_answer = [payload]
		say_ui(payload)
		return
	prose, command = tdsr.ai.extract_command(payload)
	prose = tdsr.ai.speakable(prose)
	state.ai_answer = split_for_review(prose) or [prose]
	if command:
		state.ai_answer.append("Suggested command: " + command)
	if prose:
		say_ui(prose)
	if state.config.getboolean('ai', 'announce_usage', fallback=False):
		say_ui(ai_client.last_usage_summary())
	if command:
		propose_command(command)

def propose_command(command):
	"""Offer a command for the user to accept or reject. Never runs it."""
	if tdsr.ai.is_dangerous(command):
		state.ai_command = None
		say_ui("Refusing a destructive command: " + command)
		return
	state.ai_command = command
	if len(state.key_handlers) > 1:
		# Something modal is already open, and stacking a confirmation on top
		# of it would be confusing and easy to accept by accident. Hold the
		# command instead; alt shift k offers it when the user is ready.
		say_ui("A command is ready. Press the pending command key to hear it.")
		return
	state.key_handlers.append(ConfirmHandler(command))

def pending_command():
	"""Offer a command that was held back while something else was open."""
	if not state.ai_command:
		say_ui("no pending command")
		return
	if len(state.key_handlers) > 1:
		say_ui("Finish what is open first")
		return
	state.key_handlers.append(ConfirmHandler(state.ai_command))

class ConfirmHandler(TextReview, KeyHandler):
	"""Asks before anything is typed into the terminal.

	This is the only path by which the assistant can reach the child process,
	and it always requires a keypress from the user.
	"""
	def __init__(self, command):
		self.command = command
		keymap = {
			b'y': self.accept,
			b'Y': self.accept,
			b'n': self.reject,
			b'N': self.reject,
			b's': self.spell,
			b'r': self.repeat,
			b'e': self.edit,
			b'E': self.edit,
		}
		super().__init__(keymap)
		self.text = command
		self.pos = 0
		self.runs = ai_runs_on_accept()
		say_ui("Command: %s. Press y to %s, e to type it for editing, n to discard, "
			"s to spell, r to repeat, or arrows to review it."
			% (command, "run it" if self.runs else "type it"))

	def type_command(self, run):
		state.ai_command = None
		data = self.command.encode('utf-8') + (b'\r' if run else b'')
		try:
			write_all(child_fd, data)
		except Exception as e:
			logger.error("Could not type command: %s", e)
			say_ui("Failed to type the command")
			return KeyHandler.REMOVE
		if not run:
			say_ui("Typed. Press enter to run it.")
		return KeyHandler.REMOVE

	def accept(self):
		"""Run the command, or only type it if run_on_accept is off."""
		return self.type_command(self.runs)

	def edit(self):
		"""Type the command without running it, so it can be changed first."""
		return self.type_command(False)

	def reject(self):
		state.ai_command = None
		say_ui("discarded")
		return KeyHandler.REMOVE

	def spell(self):
		say(' '.join(self.command), force_process_symbols=True)

	def repeat(self):
		say_ui("Command: %s" % self.command)

	def handle_unknown_key(self, data):
		if self.review(data):
			return
		if data in (b'\x1b', b'\r', b'\n'):
			state.ai_command = None
			say_ui("discarded")
			return KeyHandler.REMOVE
		say_ui("Press y, e, n, s or r")

def ai_runs_on_accept():
	try:
		return state.config.getboolean('ai', 'run_on_accept', fallback=True)
	except ValueError:
		return True

def review_ai_answer():
	"""Browse the last reply line by line, since speech is ephemeral."""
	if not state.ai_answer:
		say_ui("no answer to review")
		return
	state.key_handlers.append(MenuHandler(list(state.ai_answer), "Answer"))

def ai_profile_path():
	return os.path.expanduser(
		state.config.get('ai', 'profile_file', fallback=tdsr.ai.PROFILE_FILE))

def load_ai_profiles():
	return tdsr.ai.load_profiles(ai_profile_path())

def apply_saved_profile():
	"""Restore the profile named in the config, if it still exists."""
	wanted = state.config.get('ai', 'profile', fallback='').strip()
	if not wanted or ai_client is None:
		return
	for name, instructions in load_ai_profiles():
		if name.lower() == wanted.lower():
			ai_client.set_profile((name, instructions))
			return
	logger.error("Saved AI profile %r not found", wanted)

def choose_ai_profile():
	"""Pick the instruction set sent with questions."""
	if ai_client is None:
		say_ui("AI is not configured")
		return
	profiles = load_ai_profiles()
	if not profiles:
		path = ai_profile_path()
		say_ui("No profiles found. Create %s, or press c to write an example file."
			% path)
		state.key_handlers.append(CreateProfilesHandler())
		return
	state.key_handlers.append(ProfileHandler(profiles))

class ProfileHandler(KeyHandler):
	"""Arrow through the profiles and choose one."""
	def __init__(self, profiles):
		# None is a real choice: it means send no extra instructions.
		self.options = [(None, None)] + profiles
		current = ai_client.profile_name()
		self.index = 0
		for i, (name, _body) in enumerate(self.options):
			if name == current:
				self.index = i
				break
		keymap = {
			b'\x1b[A': self.previous,
			b'\x1b[B': self.next,
			b'\x1bOA': self.previous,
			b'\x1bOB': self.next,
			b'\r': self.accept,
			b'\n': self.accept,
			b'r': self.read_current,
		}
		super().__init__(keymap)
		say_ui("Choose a profile. %d options. Up and down to browse, "
			"r to read it, enter to choose, escape to cancel." % len(self.options))
		self.speak_current()

	def speak_current(self):
		name, _body = self.options[self.index]
		say_ui(name if name else "no profile")

	def read_current(self):
		name, body = self.options[self.index]
		if not name:
			say_ui("no extra instructions are sent")
			return
		say_ui(body)

	def next(self):
		if self.index >= len(self.options) - 1:
			say_ui("end")
			return
		self.index += 1
		self.speak_current()

	def previous(self):
		if self.index <= 0:
			say_ui("start")
			return
		self.index -= 1
		self.speak_current()

	def accept(self):
		name, body = self.options[self.index]
		if name:
			ai_client.set_profile((name, body))
			state.config['ai']['profile'] = name
			capped, dropped = tdsr.ai.cap_profile(body)
			tokens = tdsr.ai.estimate_tokens(capped)
			message = ("Profile %s, roughly %d extra tokens per question"
				% (name, tokens))
			if dropped:
				message += ", %d characters trimmed as too long" % dropped
			say_ui(message)
		else:
			ai_client.set_profile(None)
			state.config['ai']['profile'] = ''
			say_ui("No profile")
		state.save_config()
		return KeyHandler.REMOVE

	def handle_unknown_key(self, data):
		if data in (b'\x1b', b'q'):
			say_ui("cancelled")
			return KeyHandler.REMOVE

class CreateProfilesHandler(KeyHandler):
	"""Offers to write an example profiles file."""
	def __init__(self):
		super().__init__({b'c': self.create, b'C': self.create})

	def create(self):
		path = ai_profile_path()
		if os.path.exists(path):
			say_ui("That file already exists")
			return KeyHandler.REMOVE
		try:
			with open(path, 'w', encoding='utf-8') as fp:
				fp.write(tdsr.ai.EXAMPLE_PROFILES)
		except OSError as e:
			logger.error("Could not write %s: %s", path, e)
			say_ui("Could not write the file")
		else:
			say_ui("Wrote example profiles to %s. Edit it, then choose one." % path)
		return KeyHandler.REMOVE

	def handle_unknown_key(self, data):
		return KeyHandler.REMOVE

def pass_next_key():
	"""Send the next keypress straight to the program, bypassing TDSR."""
	state.pass_next = True
	say_ui("pass next")

def sb():
	data = speech_buffer.getvalue()
	speech_buffer.truncate(0)
	speech_buffer.seek(0)
	if data == u'':
		return
	if not state.tempsilence:
		stripped = data.strip()
		if len(stripped) == 1:
			# One character of output: speak it like typing does, so a capital
			# is "cap A" everywhere rather than sometimes "capital A".
			say_character(stripped)
			return
		new_line = replace_duplicate_characters_with_count(data)
		say(new_line)

def filter_ignored(data):
	"""Replace runs of ignored characters with a single space and collapse the gaps."""
	if state.ignore_re is None or not state.ignore_re.search(data):
		return data
	data = state.ignore_re.sub(' ', data)
	return re.sub(r'  +', ' ', data).strip()

def say(data, force_process_symbols=False, ui=False):
	"""Speak text.

	ui=True marks TDSR's own announcements, which are already plain English
	and must not have punctuation names substituted into them.
	"""
	data = data.strip()
	data = filter_ignored(data)
	if data == '':
		return False
	def replace_symbols(m):
		return " %s " % state.config['symbols'][str(ord(m.group(0)))]
	if not ui:
		if force_process_symbols or state.get_bool('process_symbols', False):
			if state.symbols_re is not None:
				data = state.symbols_re.sub(replace_symbols, data)
				# Spelling deliberately keeps its spacing, so no collapse here.
		elif state.punctuation_re is not None:
			data = state.punctuation_re.sub(replace_symbols, data)
			data = re.sub(r'  +', ' ', data).strip()
	state.last_utterance = data
	synth.send("s" + data + "\n")
	return True

def say_ui(data):
	"""Speak one of TDSR's own messages."""
	return say(data, ui=True)

def process_for_speech(data):
	"""Apply ignore filtering and punctuation naming, without speaking."""
	data = filter_ignored(data.strip())
	if data == '':
		return ''
	def replace_symbols(m):
		return " %s " % state.config['symbols'][str(ord(m.group(0)))]
	if state.get_bool('process_symbols', False):
		if state.symbols_re is not None:
			data = state.symbols_re.sub(replace_symbols, data)
	elif state.punctuation_re is not None:
		data = state.punctuation_re.sub(replace_symbols, data)
	return re.sub(r'  +', ' ', data).strip()

def say_with_prefix(prefix, body):
	"""Speak a TDSR prefix followed by screen content, as one utterance."""
	body = process_for_speech(body)
	return say_ui(prefix + (", " + body if body else ""))

def silence():
	synth.send('x\n')

def capitals_mode():
	mode = state.config.get('speech', 'capitals', fallback='cap').strip().lower()
	if mode not in CAPITAL_MODES:
		logger.error("Unknown capitals setting %r, using 'cap'", mode)
		return 'cap'
	return mode

def pitch_setting(option, fallback):
	value = state.get_number(option, fallback, float)
	return min(2.0, max(0.5, value))

WHITESPACE_NAMES = {' ': 'space', '\t': 'tab', '\u00a0': 'space', '\u3000': 'space'}

def say_character(ch, deleted=False):
	"""Speak one character, the way VoiceOver does.

	Capitals get "cap" before them, a higher pitch, or both, per the capitals
	setting. A character being deleted is spoken at a lower pitch, so typing
	and erasing sound different. Pitch is a hint to the speech server for the
	next utterance only.
	"""
	if ch == '':
		ch = ' '
	is_capital = len(ch) == 1 and ch.isalpha() and ch.isupper() and ch.lower() != ch
	mode = capitals_mode() if is_capital else 'off'
	pitch = None
	if deleted and state.get_bool('lower_pitch_deleted', True):
		pitch = pitch_setting('deleted_pitch', DEFAULT_DELETED_PITCH)
	elif mode in ('pitch', 'both'):
		pitch = pitch_setting('cap_pitch', DEFAULT_CAP_PITCH)
	if pitch is not None and abs(pitch - 1.0) > 0.001:
		synth.send('P%.2f\n' % pitch)
	key = str(ord(ch))
	if key in state.config['symbols']:
		state.last_utterance = state.config['symbols'][key]
		synth.send('s%s\n' % state.config['symbols'][key])
	elif ch.isspace() or ch in WHITESPACE_NAMES:
		# Whitespace sent as a bare character is silent on some speech servers
		# (the line is stripped before it is spoken), so always name it, even
		# when the settings file has no name for it in its symbols section.
		name = WHITESPACE_NAMES.get(ch, 'space')
		state.last_utterance = name
		synth.send('s%s\n' % name)
	elif mode in ('cap', 'both'):
		state.last_utterance = ch
		synth.send('scap %s\n' % ch)
	else:
		state.last_utterance = ch
		synth.send('l%s\n' % ch)

def say_cursor_line():
	"""Read the line the real cursor is on, minus a noisy prompt prefix."""
	y = screen.cursor.y
	text = line_text(y).strip()
	if text == '':
		say_ui("blank")
		return
	say(strip_prompt_prefix(text))

def say_cursor_char(previous_y):
	"""Read the character under the cursor, announcing a line change.

	Moving left at the start of a line, or right at the end, wraps to the
	neighbouring line. The character there is usually blank, so without this
	it just says "space" and you cannot tell you have moved.
	"""
	y, x = screen.cursor.y, screen.cursor.x
	if in_full_screen_program():
		# Editors such as nano let the cursor sit just past the last
		# character, where the line break is. Say so there, the way VoiceOver
		# does, and read the plain character everywhere else, including the
		# first character of a line you have just wrapped onto. Reading the
		# whole line on every wrap made arrowing through a file confusing.
		if x >= len(line_text(y).rstrip()):
			say_ui("new line")
		else:
			saychar(y, x)
		return
	if y != previous_y:
		say_cursor_line()
		return
	saychar(y, x)

def in_full_screen_program():
	"""True while nano, vim, less or similar have the alternate screen up."""
	return getattr(screen, 'saved_buffer', None) is not None

def arrow_up():
	schedule_cursor_read(say_cursor_line)
	return KeyHandler.PASSTHROUGH

def arrow_down():
	schedule_cursor_read(say_cursor_line)
	return KeyHandler.PASSTHROUGH

def arrow_left():
	y = screen.cursor.y
	schedule_cursor_read(lambda: say_cursor_char(y))
	return KeyHandler.PASSTHROUGH

def arrow_right():
	y = screen.cursor.y
	schedule_cursor_read(lambda: say_cursor_char(y))
	return KeyHandler.PASSTHROUGH

def schedule(timeout, func, set_tempsilence=False):
	state.delayed_functions.append((time.monotonic() + timeout, func))
	if set_tempsilence:
		state.tempsilence = True

def schedule_cursor_read(func):
	"""Speak the cursor position once the program has stopped redrawing.

	The read fires CURSOR_TIMEOUT after output goes quiet rather than
	CURSOR_TIMEOUT after the keypress, so a slow redraw can't cause us to
	speak the previous contents of the screen. CURSOR_SETTLE_MAX caps the
	wait for programs that never stop drawing.
	"""
	now = time.monotonic()
	state.pending_read = func
	state.pending_limit = now + max(CURSOR_TIMEOUT, CURSOR_SETTLE_MAX)
	# Until the program says something there is nothing new to read, so wait
	# for the first output rather than speaking the old screen contents. If it
	# never answers, the screen is unchanged and reading late is still correct.
	state.pending_deadline = state.pending_limit
	state.tempsilence = True

def defer_cursor_read():
	"""Output arrived; push the pending read back to wait for quiet."""
	if state.pending_read is None:
		return
	state.pending_deadline = min(time.monotonic() + CURSOR_TIMEOUT, state.pending_limit)

def cancel_cursor_read():
	state.pending_read = None

def run_scheduled():
	curtime = time.monotonic()
	if state.pending_read is not None and curtime >= state.pending_deadline:
		func = state.pending_read
		state.pending_read = None
		func()
	if not state.delayed_functions:
		return
	to_remove = []
	for i in state.delayed_functions:
		t, f = i
		if curtime >= t:
			f()
			to_remove.append(i)
	for i in to_remove:
		state.delayed_functions.remove(i)

def time_until_next_delayed():
	deadlines = [t for t, _f in state.delayed_functions]
	if state.pending_read is not None:
		deadlines.append(state.pending_deadline)
	if state.say_all is not None and not state.speech_done:
		deadlines.append(state.say_all.spoke_at + SAY_ALL_FALLBACK)
	if state.command_running and state.get_bool('announce_command_done', False):
		deadlines.append(state.last_output_time + COMMAND_DONE_TIMEOUT)
	if not deadlines:
		return None
	# min(), not [0]: the list is appended to, never sorted.
	return max(0, min(deadlines) - time.monotonic())

def config():
	say_ui("config")
	state.key_handlers.append(ConfigHandler())

def copy_mode():
	say_ui("copy")
	state.key_handlers.append(CopyHandler())

def get_char():
	return cell(state.revy, state.revx)

def move_prevchar():
	if state.revx == 0:
		if state.revy <= min_y():
			return ''
		state.revy -= 1
		state.revx = screen.columns - 1
	else:
		state.revx -= 1

def move_nextchar():
	if state.revx == screen.columns - 1:
		if state.revy == screen.lines - 1:
			return ''
		state.revy += 1
		state.revx = 0
	else:
		state.revx += 1

def prevword():
	if state.revx == 0:
		say_ui("left")
		sayword()
		return
	start_x, start_y = state.revx, state.revy
	#Move over any existing word we might be in the middle of
	while state.revx > 0 and get_char() != ' ':
		move_prevchar()
	#Skip whitespace
	while state.revx > 0 and get_char() == ' ':
		move_prevchar()
	#Move to the beginning of the word we're now on
	while state.revx > 0 and get_char() != ' ' and cell(state.revy, state.revx - 1) != ' ':
		move_prevchar()
	announce_column_jump(start_y, start_x)
	sayword()

def announce_column_jump(start_y, start_x):
	"""Say "column" when a word jump has crossed a column boundary."""
	if state.revy != start_y:
		return          # a line change is already announced elsewhere
	if crossed_column(state.revy, start_x, state.revx):
		say_ui("column")

def sayword(spell=False):
	word = ""
	revx, revy = state.revx, state.revy
	while state.revx > 0 and get_char() != ' ' and cell(state.revy, state.revx - 1) != ' ':
		move_prevchar()
	if state.revx == 0 and get_char() == ' ':
		say_ui("space")
		return
	word += get_char()
	while state.revx < screen.columns - 1:
		move_nextchar()
		if get_char() == ' ':
			break
		word += get_char()
	if spell:
		say(' '.join(word), force_process_symbols=True)
	else:
		say(word)
	state.revx, state.revy = revx, revy

def nextword():
	revx, _revy = state.revx, state.revy
	start_x, start_y = state.revx, state.revy
	m = screen.columns - 1
	#Move over any existing word we might be in the middle of
	while state.revx < m and get_char() != ' ':
		move_nextchar()
	#Skip whitespace
	while state.revx < m and get_char() == ' ':
		move_nextchar()
	if state.revx == m and get_char() == ' ':
		say_ui("right")
		state.revx = revx
		sayword()
		return
	announce_column_jump(start_y, start_x)
	sayword()

def handle_silence():
	state.silence = not state.silence
	say_ui("quiet on" if state.silence else "quiet off")

def describe_position(y, x):
	"""Short spoken description of a review position."""
	if y < 0:
		line_no = len(state.history) + y + 1
		return "history line %d, column %d" % (line_no, x + 1)
	return "line %d, column %d" % (y + 1, x + 1)

def selection_mode():
	mode = state.config.get('speech', 'selection_mode', fallback='stream')
	mode = mode.strip().lower()
	if mode not in ('stream', 'lines'):
		logger.error("Unknown selection_mode %r, using 'stream'", mode)
		return 'stream'
	return mode

def describe_copied(text):
	"""Say how much was copied, so it is clear what was taken."""
	if not text:
		say_ui("copied nothing")
		return
	lines = text.split("\n")
	if len(lines) == 1:
		chars = len(lines[0])
		say_ui("copied %d character%s" % (chars, "" if chars == 1 else "s"))
	else:
		say_ui("copied %d lines" % len(lines))

def cancel_selection():
	"""Drop a selection that is in progress."""
	if state.copy_x is None:
		say_ui("no selection")
		return
	state.copy_x = None
	state.copy_y = None
	say_ui("selection cancelled")

def handle_clipboard():
	clamp_review()
	mode = selection_mode()
	if state.copy_x is None:
		state.copy_y = state.revy
		# In line mode the column is irrelevant, so don't mention it.
		if mode == 'lines':
			state.copy_x = 0
			say_ui("select from %s" % describe_position(state.revy, 0).split(", column")[0])
		else:
			state.copy_x = state.revx
			say_ui("select from %s" % describe_position(state.revy, state.revx))
		return
	start_y, start_x = state.copy_y, state.copy_x
	end_y, end_x = state.revy, state.revx
	if mode == 'lines':
		# Whole lines: from the start of the first to the end of the last.
		if start_y > end_y:
			start_y, end_y = end_y, start_y
		start_x, end_x = 0, screen.columns - 1
	try:
		text = copy_text(start_y, start_x, end_y, end_x)
	except Exception as e:
		logger.error("Copy failed: %s", e)
		say_ui("Failed")
	else:
		if mode == 'lines':
			where = describe_position(state.revy, 0).split(", column")[0]
		else:
			where = describe_position(state.revy, state.revx)
		say_ui("to %s" % where)
		describe_copied(text)
	state.copy_x = None
	state.copy_y = None

def copy_text(start_y, start_x, end_y, end_x):
	"""Copy a region to the clipboard.

	The two points are an ordered pair, not a rectangle: selecting from
	column 50 of one line to column 10 of a later line takes the tail of the
	first line, all of the middle lines, and the head of the last one.
	"""
	# Order by position, keeping each column with its own line. Swapping the
	# columns independently is what used to corrupt multi-line selections.
	if (start_y, start_x) > (end_y, end_x):
		start_y, start_x, end_y, end_x = end_y, end_x, start_y, start_x
	buf = []
	for y in range(start_y, end_y + 1):
		line = line_text(y)
		first = start_x if y == start_y else 0
		last = end_x if y == end_y else len(line) - 1
		chars = [line[x] if x < len(line) else ' ' for x in range(first, last + 1)]
		buf.append("".join(chars).rstrip())
	text = "\n".join(buf)
	copy_to_clip(text)
	return text

def copy_to_clip(data):
	"""Hand text (or bytes) to the system clipboard. Raises if that isn't possible."""
	if isinstance(data, str):
		data = data.encode('utf-8')
	proc_args = []
	if platform.system() == 'Darwin':
		proc_args = ['pbcopy']
	elif platform.system() == 'Linux':
		if os.getenv('XDG_SESSION_TYPE') == 'wayland':
			proc_args = ['wl-copy']
		else:
			proc_args = ['xclip', '-selection', 'clip']
	if not proc_args:
		raise OSError("No clipboard command for this platform")
	proc = subprocess.Popen(proc_args, stdin=subprocess.PIPE)
	try:
		proc.stdin.write(data)
		proc.stdin.close()
	finally:
		# Without reaping, every copy leaves a zombie behind for the life of
		# the session. wl-copy stays resident by design, so don't block on it.
		if proc_args[0] == 'wl-copy':
			proc.poll()
		else:
			try:
				proc.wait(timeout=2)
			except subprocess.TimeoutExpired:
				proc.kill()

def has_more(fd):
	r, w, e = select.select([fd], [], [], 0)
	if fd in r:
		return True
	return False

def get_terminal_size(fd):
	s = struct.pack('HHHH', 0, 0, 0, 0)
	rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(fd, termios.TIOCGWINSZ, s))
	return rows, cols

def resize_terminal(fd):
	s = struct.pack('HHHH', 0, 0, 0, 0)
	s = fcntl.ioctl(0, termios.TIOCGWINSZ, s)
	fcntl.ioctl(fd, termios.TIOCSWINSZ, s)
	rows, cols, _, _ = struct.unpack('hhhh', s)
	return rows, cols

def handle_sigwinch(*args):
	os.write(signal_pipe[1], b'w')

# Named commands. The [keys] section of the config maps these names to key
# sequences, so every binding below can be changed without editing this file.
def toggle_columns():
	current = not state.get_bool('announce_columns', True)
	state.config['speech']['announce_columns'] = str(current)
	state.save_config()
	say_ui("column breaks on" if current else "column breaks off")

def toggle_indent():
	current = not state.get_bool('announce_indent', False)
	state.config['speech']['announce_indent'] = str(current)
	state.save_config()
	say_ui("indent announcement on" if current else "indent announcement off")

def cycle_punctuation():
	level = state.punctuation_level()
	nxt = PUNCTUATION_LEVELS[(PUNCTUATION_LEVELS.index(level) + 1) % len(PUNCTUATION_LEVELS)]
	state.config['speech']['punctuation_level'] = nxt
	state.save_config()
	state.punctuation_re = state.build_punctuation_re()
	say_ui("punctuation %s" % nxt)

# ---------------------------------------------------------------------------
# Copy a whole file to the clipboard, from wherever the shell is.
#
# TDSR types a short command at the prompt that prints the file as base64
# inside an OSC 52 "set clipboard" sequence. TDSR sees all output, however
# many ssh or pct enter hops away the shell is, so it catches the sequence,
# decodes it and puts the file on this machine's clipboard. Nothing has to be
# installed or enabled on the far side beyond base64 and tr.

class Osc52Filter:
	"""Pull OSC 52 clipboard sequences out of program output.

	feed() returns the output with accepted sequences removed, so the base64
	is never drawn or spoken, plus a list of events:
	('copied', base64_bytes), ('unreadable',), ('interrupted',), ('too_large',).
	Sequences that are not accepted pass through untouched. A sequence can
	be split across reads, so partial state is kept between calls.
	"""
	START = b'\x1b]52;'
	B64 = frozenset(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\r\n')

	def __init__(self, accept=lambda: True, limit=MAX_CLIP_BASE64):
		self.accept = accept
		self.limit = limit
		self.pending = b''
		self.collecting = False
		self.payload = bytearray()
		self.overflow = False
		self.error = False

	def _finish(self, events):
		if self.error:
			events.append(('unreadable',))
		elif self.overflow:
			events.append(('too_large',))
		else:
			events.append(('copied', bytes(self.payload)))
		self._reset()

	def _reset(self):
		self.collecting = False
		self.payload = bytearray()
		self.overflow = False
		self.error = False

	def feed(self, data):
		buf = self.pending + data
		self.pending = b''
		out = bytearray()
		events = []
		i = 0
		n = len(buf)
		while i < n:
			if self.collecting:
				j = i
				while j < n and buf[j] in self.B64:
					j += 1
				if not self.overflow:
					self.payload += buf[i:j]
					if len(self.payload) > self.limit:
						self.overflow = True
						self.payload = bytearray()
				i = j
				if i >= n:
					break
				b = buf[i]
				if b == 0x07:
					self._finish(events)
					i += 1
				elif b == 0x1b:
					if i + 1 >= n:
						self.pending = buf[i:]
						break
					if buf[i + 1] == 0x5c:
						self._finish(events)
						i += 2
					else:
						events.append(('interrupted',))
						self._reset()
				elif b == 0x21 and not self.payload and not self.error:
					# "!" is the command's own signal that the file can't be read.
					self.error = True
					i += 1
				else:
					# Anything else means the sender stopped part way, for
					# example control c. Give up and let the rest through.
					events.append(('interrupted',))
					self._reset()
				continue
			k = buf.find(self.START, i)
			if k < 0:
				# Hold back a possible start of a sequence split across reads.
				keep = 0
				for size in range(min(len(self.START) - 1, n - i), 0, -1):
					if buf.endswith(self.START[:size]):
						keep = size
						break
				out += buf[i:n - keep]
				self.pending = buf[n - keep:] if keep else b''
				break
			out += buf[i:k]
			p = buf.find(b';', k + len(self.START), k + len(self.START) + 8)
			if p < 0:
				if n - k < len(self.START) + 8:
					self.pending = buf[k:]
					break
				out += buf[k:k + 1]
				i = k + 1
				continue
			if self.accept():
				self.collecting = True
				i = p + 1
			else:
				out += buf[k:p + 1]
				i = p + 1
		return bytes(out), events

def osc52_accepted():
	"""Whether to take a clipboard sequence from the program right now.

	By default only when TDSR has just asked for a file itself, so text
	that merely passes over the screen can't quietly replace the clipboard.
	"""
	mode = state.config['speech'].get('osc52_clipboard', 'expected').strip().lower()
	if mode == 'off':
		return False
	if mode == 'always':
		return True
	return time.monotonic() < state.clip_expect_until

clip_filter = Osc52Filter(accept=osc52_accepted)

NANO_TITLE_RE = re.compile(r'GNU nano\s+\S+\s+(.*?)\s*$')
NANO_FLAGS_RE = re.compile(r'\s{2,}(?:Modified|View|Restricted)\s*$')
EDITOR_COMMAND_RE = re.compile(
	r'(?:^|[\s;&|(])(?:sudo\s+(?:-\S+\s+)*)?(?:\S*/)?(nano|rnano|vim?|nvim|view|less|more|micro|pico)\s+(.+?)\s*$')

def nano_file_name():
	"""The file nano has open, read from its title bar.

	None when nano's title bar can't be found, '' for a buffer that has no
	file name yet. The title is usually the top row, but look at the second
	too in case something is drawn above it.
	"""
	for y in (0, 1):
		m = NANO_TITLE_RE.search(line_text(y))
		if not m:
			continue
		name = NANO_FLAGS_RE.sub('', m.group(1)).strip()
		if name in ('', 'New Buffer'):
			return ''
		return name
	return None

def saved_line(y):
	"""A row of the normal screen as it was before a full screen program started."""
	saved = getattr(screen, 'saved_buffer', None)
	if not saved or y not in saved:
		return ''
	row = saved[y]
	return ''.join(row[x].data if x in row else ' ' for x in range(screen.columns)).rstrip()

def launched_file_name():
	"""The file named on the command line that started the full screen program.

	The shell prompt and command are still on the saved normal screen, just
	above where the cursor was when the program took over. Works for nano,
	vim, less and similar, whatever their own screen shows.
	"""
	cursor = getattr(screen, 'saved_cursor', None)
	if cursor is None:
		return None
	for y in range(cursor.y, max(-1, cursor.y - 4), -1):
		m = EDITOR_COMMAND_RE.search(saved_line(y))
		if not m:
			continue
		try:
			words = shlex.split(m.group(2))
		except ValueError:
			words = m.group(2).split()
		files = [w for w in words if not w.startswith(('-', '+'))]
		if files:
			return files[0]
	return None

def shell_word(path):
	"""Quote a path for the shell, keeping a leading ~ working."""
	path = path.strip()
	if len(path) >= 2 and path[0] == path[-1] and path[0] in '\'"':
		path = path[1:-1]
	if path == '~':
		return '~'
	if path.startswith('~/'):
		return '~/' + shlex.quote(path[2:])
	return shlex.quote(path)

def copy_file_command(path):
	"""The command typed at the prompt. Works in bash, zsh and sh, on Linux and macOS.

	The leading space keeps it out of history where HISTCONTROL ignores
	spaces (Debian's default .bashrc does).
	"""
	f = shell_word(path)
	return (" [ -f %s ] && [ -r %s ] && base64 < %s | tr -d '\\n' | "
		"{ printf '\\033]52;c;'; cat; printf '\\033\\\\'; } "
		"|| printf '\\033]52;c;!\\033\\\\'" % (f, f, f))

def copy_file():
	if in_full_screen_program():
		name = nano_file_name()
		if not name:
			# Title bar missing or unreadable: use the command that opened it.
			name = launched_file_name() or name
		if name is None:
			logger.info("Copy file: no file name found. Top rows %r %r",
				line_text(0), line_text(1))
			say_ui("Couldn't find the file name. Exit, then press copy file and type the path.")
		elif name == '':
			say_ui("This nano buffer has no file name yet. Save it first.")
		elif name.startswith('...'):
			say_ui("The file name is too long to read from nano. Exit nano and type it instead.")
		else:
			state.clip_nano_path = name
			say_ui("Remembered %s. Save and exit, then press copy file again." % name)
		return
	handler = BufferHandler(on_accept=copy_file2)
	if state.clip_nano_path:
		handler.buffer = state.clip_nano_path
		say_ui("Copy file %s. Press enter, or control u to clear." % state.clip_nano_path)
		state.clip_nano_path = None
	else:
		say_ui("Copy file, type the path")
	state.key_handlers.append(handler)

def copy_file2(path):
	path = path.strip()
	if not path:
		say_ui("cancelled")
		return
	state.clip_path = path
	state.clip_expect_until = time.monotonic() + COPY_FILE_WAIT
	# Control e, control u: clear anything already typed at the prompt so the
	# command isn't appended to it.
	write_all(child_fd, b'\x05\x15' + copy_file_command(path).encode('utf-8') + b'\r')
	say_ui("copying")
	# Don't read out the command or the shell's reply; the result is spoken.
	state.tempsilence = True
	schedule(COPY_FILE_WAIT, copy_file_timeout)

def copy_file_timeout():
	if state.clip_expect_until and not clip_filter.collecting:
		state.clip_expect_until = 0.0
		state.tempsilence = False
		say_ui("No file came back. Check you were at a shell prompt, and review the screen.")

def count_lines(text):
	if not text:
		return 0
	return text.count('\n') + (0 if text.endswith('\n') else 1)

def handle_clip_event(event):
	"""Speak and act on what the filter caught."""
	name = os.path.basename(state.clip_path.rstrip('/')) if state.clip_path else ''
	state.clip_expect_until = 0.0
	state.clip_path = None
	state.tempsilence = False
	kind = event[0]
	if kind == 'unreadable':
		say_ui("Can't read %s. Check the path, and review the screen." % (name or "that file"))
		return
	if kind == 'interrupted':
		say_ui("Copy interrupted, nothing copied")
		return
	if kind == 'too_large':
		say_ui("File too large to copy, nothing copied")
		return
	try:
		data = base64.b64decode(re.sub(rb'\s+', b'', event[1]), validate=True)
	except (binascii.Error, ValueError):
		say_ui("Copy failed, the file arrived damaged")
		return
	if not data:
		say_ui("%s is empty, nothing copied" % (name or "The file"))
		return
	try:
		copy_to_clip(data)
	except Exception as e:
		logger.error("Copy failed: %s", e)
		say_ui("Failed")
		return
	try:
		text = data.decode('utf-8')
	except UnicodeDecodeError:
		say_ui("Copied %s, %d bytes, not plain text" % (name or "file", len(data)))
		return
	lines = count_lines(text)
	what = "%d line%s" % (lines, "" if lines == 1 else "s")
	say_ui("Copied %s from %s" % (what, name) if name else "Copied %s" % what)

COMMANDS = {
	'say_line': lambda: say_line_with_indent(state.revy),
	'prev_line': prevline,
	'next_line': nextline,
	'prev_word': prevword,
	'say_word': sayword,
	'spell_word': lambda: sayword(spell=True),
	'next_word': nextword,
	'prev_char': prevchar,
	'say_char': lambda: saychar(state.revy, state.revx),
	'say_char_phonetically': lambda: saychar(state.revy, state.revx, phonetically=True),
	'next_char': nextchar,
	'top_of_history': topOfHistory,
	'bottom_of_buffer': bottomOfBuffer,
	'top_of_screen': topOfScreen,
	'bottom_of_screen': bottomOfScreen,
	'start_of_line': startOfLine,
	'end_of_line': endOfLine,
	'config': config,
	'toggle_silence': handle_silence,
	'clipboard': handle_clipboard,
	'cancel_selection': cancel_selection,
	'copy_mode': copy_mode,
	'copy_file': copy_file,
	'silence': silence,
	'find': find,
	'find_next': find_next,
	'find_previous': find_previous,
	'prev_prompt': prev_prompt,
	'next_prompt': next_prompt,
	'repeat_last': repeat_last,
	'spell_last': spell_last,
	'where_am_i': where_am_i,
	'say_all': start_say_all,
	'say_highlighted': say_highlighted,
	'say_logical_line': say_logical_line,
	'pass_next_key': pass_next_key,
	'browse_commands': browse_commands,
	'ask_ai': ask_ai,
	'review_ai_answer': review_ai_answer,
	'describe_ai_context': describe_ai_context,
	'choose_ai_profile': choose_ai_profile,
	'ai_usage_report': ai_usage_report,
	'cancel_ai': cancel_ai,
	'toggle_ai_history': toggle_ai_history,
	'clear_ai_history': clear_ai_history,
	'pending_command': pending_command,
	'toggle_indent': toggle_indent,
	'toggle_columns': toggle_columns,
	'cycle_punctuation': cycle_punctuation,
}

# Default bindings, written in the same notation the config file accepts.
# A space separated pair means "press it twice quickly".
DEFAULT_KEYS = {
	'say_line': 'alt+i',
	'prev_line': 'alt+u',
	'next_line': 'alt+o',
	'prev_word': 'alt+j',
	'say_word': 'alt+k',
	'spell_word': 'alt+k alt+k',
	'next_word': 'alt+l',
	'prev_char': 'alt+m',
	'say_char': 'alt+,',
	'say_char_phonetically': 'alt+, alt+,',
	'next_char': 'alt+.',
	'top_of_history': 'alt+g',
	'bottom_of_buffer': 'alt+h',
	'top_of_screen': 'alt+shift+u',
	'bottom_of_screen': 'alt+shift+o',
	'start_of_line': 'alt+shift+m',
	'end_of_line': 'alt+>',
	'config': 'alt+c',
	'toggle_silence': 'alt+q',
	'clipboard': 'alt+r',
	'cancel_selection': 'alt+shift+r',
	'copy_mode': 'alt+v',
	'copy_file': 'alt+shift+v',
	'silence': 'alt+x',
	'find': 'alt+f',
	'find_next': 'alt+n',
	'find_previous': 'alt+shift+n',
	'prev_prompt': 'alt+p',
	'next_prompt': 'alt+shift+p',
	'repeat_last': 'alt+y',
	'spell_last': 'alt+shift+y',
	'where_am_i': 'alt+w',
	'say_all': 'alt+a',
	'say_highlighted': 'alt+s',
	'say_logical_line': 'alt+shift+i',
	'pass_next_key': 'alt+shift+a',
	'browse_commands': 'alt+shift+h',
	'ask_ai': 'alt+/',
	'review_ai_answer': 'alt+?',
	'describe_ai_context': 'alt+shift+d',
	'choose_ai_profile': 'alt+shift+f',
	'ai_usage_report': 'alt+shift+s',
	'cancel_ai': 'alt+shift+x',
	'toggle_ai_history': 'alt+shift+l',
	'clear_ai_history': 'none',
	'pending_command': 'alt+shift+k',
	'toggle_indent': 'alt+shift+t',
	'toggle_columns': 'none',
	'cycle_punctuation': 'alt+shift+c',
}

# Bindings that are not user-remappable because they shadow real terminal keys.
FIXED_KEYS = {
	b'\x08': handle_backspace,
	b'\x7f': handle_backspace,
	b'\x1b[3~': handle_delete,
	b'\x1b[A': arrow_up,
	b'\x1b[B': arrow_down,
	b'\x1b[C': arrow_right,
	b'\x1b[D': arrow_left,
	# Application cursor mode
	b'\x1bOA': arrow_up,
	b'\x1bOB': arrow_down,
	b'\x1bOC': arrow_right,
	b'\x1bOD': arrow_left,
	# top_of_screen pressed twice, for the Hungarian keyboard layout
	b'\x1b:': endOfLine,
	# Shift plus arrows. These pass through untouched unless
	# shift_arrow_selection is turned on.
	b'\x1b[1;2A': shift_arrow_up,
	b'\x1b[1;2B': shift_arrow_down,
	b'\x1b[1;2C': shift_arrow_right,
	b'\x1b[1;2D': shift_arrow_left,
}

def parse_key(spec):
	"""Turn a key description such as 'alt+shift+u' into the bytes it sends.

	Accepts alt+ (or meta+) and shift+ modifiers, single characters, and
	\\xNN escapes for raw bytes. Space separated parts mean a double press.
	Returns None if the spec cannot be understood.
	"""
	chunks = spec.split()
	if not chunks:
		return None
	out = b''
	for chunk in chunks:
		parts = chunk.lower().split('+')
		key = parts[-1]
		mods = parts[:-1]
		for mod in mods:
			if mod not in ('alt', 'meta', 'shift'):
				logger.error("Unknown modifier %r in key %r", mod, spec)
				return None
		if key.startswith('\\x'):
			try:
				key_bytes = bytes([int(key[2:], 16)])
			except ValueError:
				logger.error("Bad escape %r in key %r", key, spec)
				return None
		else:
			actual = chunk.split('+')[-1]
			if len(actual) != 1:
				logger.error("Key %r must be a single character", actual)
				return None
			# Case in the spec is ignored; shift must be written explicitly, so
			# 'alt+g' and 'ALT+G' are the same key and only 'alt+shift+g' differs.
			actual = actual.lower() if actual.isalpha() else actual
			if 'shift' in mods:
				actual = actual.upper()
			try:
				key_bytes = actual.encode('utf-8')
			except UnicodeEncodeError:
				return None
		if 'alt' in mods or 'meta' in mods:
			key_bytes = b'\x1b' + key_bytes
		out += key_bytes
	return out or None

def build_keymap():
	"""Default bindings, overridden by anything in the config [keys] section."""
	km = dict(FIXED_KEYS)
	bindings = dict(DEFAULT_KEYS)
	if state.config.has_section('keys'):
		for name, spec in state.config.items('keys'):
			if name not in COMMANDS:
				logger.error("Unknown command in [keys]: %s", name)
				continue
			bindings[name] = spec
	for name, spec in bindings.items():
		spec = spec.strip()
		if not spec or spec.lower() in ('none', 'off'):
			continue  # unbound on purpose
		keys = parse_key(spec)
		if keys is None:
			logger.error("Could not parse key %r for %s, using the default", spec, name)
			keys = parse_key(DEFAULT_KEYS.get(name, ''))
			if keys is None:
				continue
		if keys in km and km[keys] is not COMMANDS[name]:
			logger.error("Key %r for %s is already bound, ignoring", spec, name)
			continue
		km[keys] = COMMANDS[name]
	return km

keymap = {}
