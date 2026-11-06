#!/usr/bin/env python3
#Copyright (C) 2016, 2017  Tyler Spivey
#See the license in COPYING.txt
import io
import sys
import speechd.client

def check_range(synth, value, low, high, name):
	if value >= low and value <= high:
		return True
	synth.speak('Bogus value for {0}: {1}.  Value must be in the range {2} to {3}'.format(name, value, low, high))
	return False

next_pitch = None

def handle_line(synth, line):
	"""Handle one protocol line. An empty line is ignored rather than raising."""
	if not line:
		return
	global next_pitch
	command, rest = line[0], line[1:]
	if command == u"P":
		try:
			# Multiplier 0.5 to 2.0 mapped onto speech-dispatcher's -100 to 100.
			next_pitch = int(max(-100, min(100, (float(rest) - 1.0) * 100)))
		except ValueError:
			next_pitch = None
		return
	pitch = globals().get('next_pitch')
	next_pitch = None
	if command in (u"s", u"l") and pitch is not None:
		try:
			synth.set_pitch(pitch)
		except Exception:
			pitch = None
	if command == u"s":
		synth.speak(rest)
	elif command == u"l":
		synth.char(rest)
	if pitch is not None and command in (u"s", u"l"):
		try:
			synth.set_pitch(0)
		except Exception:
			pass
	elif command == u"x":
		synth.cancel()
	elif command == u"r":
		try:
			rate = int(rest)
		except ValueError:
			return
		if check_range(synth, rate, 0, 100, 'rate'):
			synth.set_rate(-100 + rate * 2)
	elif command == u"v":
		try:
			volume = int(rest)
		except ValueError:
			return
		if check_range(synth, volume, 0, 100, 'volume'):
			synth.set_volume(-100 + volume * 2)
	elif command == u"V":
		# Speech Dispatcher takes a voice name, not an index. A bare number is
		# meaningless here, so ignore it rather than guessing.
		if rest.strip().isdigit():
			return
		try:
			synth.set_synthesis_voice(rest.strip())
		except Exception:
			pass

def notify(event):
	"""Tell TDSR something happened. One word per line on stdout."""
	try:
		sys.stdout.write(event + "\n")
		sys.stdout.flush()
	except (BrokenPipeError, ValueError):
		pass

def make_callback():
	def callback(callback_type):
		name = getattr(callback_type, 'name', str(callback_type))
		if 'END' in str(name).upper():
			notify("done")
		elif 'CANCEL' in str(name).upper():
			notify("cancelled")
	return callback

def main():
	input_stream = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
	synth = speechd.client.SSIPClient('tdsr')
	try:
		synth.set_notification_on('end')
		synth.set_notification_on('cancel')
		synth._conn.set_callback(make_callback())
	except Exception:
		# Older bindings may not support notifications; say all will fall back
		# to a timer on the TDSR side.
		pass
	try:
		line = input_stream.readline()
		while line:
			handle_line(synth, line.strip('\n'))
			line = input_stream.readline()
	finally:
		synth.close()

if __name__ == '__main__':
	main()
