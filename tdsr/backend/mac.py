#!/usr/bin/env python3
#Copyright (C) 2016, 2017  Tyler Spivey
#See the license in COPYING.txt
from Foundation import (
	NSObject, NSFileHandle, NSNotificationCenter,
	NSFileHandleReadCompletionNotification, NSFileHandleNotificationDataItem,
)
from AVFoundation import AVSpeechSynthesizer, AVSpeechUtterance, AVSpeechBoundaryImmediate, AVSpeechSynthesisVoice
from PyObjCTools import AppHelper
import html
import sys
import objc

rate = None
volume = None
voice = None
# Pitch for the next utterance only, set by a P line (1.0 is normal).
next_pitch = None
voices = AVSpeechSynthesisVoice.speechVoices()
avsynth = AVSpeechSynthesizer.new()

def notify(event):
	"""Tell TDSR something happened. One word per line on stdout."""
	try:
		sys.stdout.write(event + "\n")
		sys.stdout.flush()
	except (BrokenPipeError, ValueError):
		pass

class SpeechDelegate(NSObject):
	"""Reports when an utterance finishes so TDSR can drive say all."""
	def speechSynthesizer_didFinishSpeechUtterance_(self, synth, utterance):
		notify("done")

	def speechSynthesizer_didCancelSpeechUtterance_(self, synth, utterance):
		notify("cancelled")

delegate = SpeechDelegate.alloc().init()
avsynth.setDelegate_(delegate)

class FileObserver(NSObject):
	def initWithFileDescriptor_readCallback_errorCallback_(self,
	fileDescriptor, readCallback, errorCallback):
		self = self.init()
		self.readCallback = readCallback
		self.errorCallback = errorCallback
		self.fileHandle = NSFileHandle.alloc().initWithFileDescriptor_(
		fileDescriptor)
		self.nc = NSNotificationCenter.defaultCenter()
		self.nc.addObserver_selector_name_object_(
		self,
		'fileHandleReadCompleted:',
		NSFileHandleReadCompletionNotification,
		self.fileHandle)
		self.fileHandle.readInBackgroundAndNotify()
		return self

	def fileHandleReadCompleted_(self, aNotification):
		ui = aNotification.userInfo()
		newData = ui.objectForKey_(NSFileHandleNotificationDataItem)
		if newData is None:
			if self.errorCallback is not None:
				self.errorCallback(self)
			self.close()
		else:
			self.fileHandle.readInBackgroundAndNotify()
			if self.readCallback is not None:
				self.readCallback(self, bytes(newData))

	def close(self):
		self.nc.removeObserver_(self)
		if self.fileHandle is not None:
			self.fileHandle.closeFile()
			self.fileHandle = None
		# break cycles in case these functions are closed over
		# an instance of us
		self.readCallback = None
		self.errorCallback = None

	def __del__(self):
		# Without this, if a notification fires after we are GC'ed
		# then the app will crash because NSNotificationCenter
		# doesn't retain observers.  In this example, it doesn't
		# matter, but it's worth pointing out.
		self.close()

def find_voice(value):
	"""Resolve a voice index, identifier, or (partial) name to an AVSpeechSynthesisVoice."""
	value = value.strip()
	if not value:
		return None
	if value.isdigit():
		idx = int(value)
		return voices[idx] if idx < len(voices) else None
	for v in voices:
		if v.identifier() == value:
			return v
	lowered = value.lower()
	for v in voices:
		if v.name().lower() == lowered:
			return v
	for v in voices:
		if lowered in v.name().lower():
			return v
	return None

def list_voices():
	print("index\tname\tlanguage\tidentifier")
	for i, v in enumerate(voices):
		print("%d\t%s\t%s\t%s" % (i, v.name(), v.language(), v.identifier()))

def list_voices_tsv():
	"""Machine readable list for TDSR's voice menu: a language line, then one voice per line."""
	try:
		current = AVSpeechSynthesisVoice.currentLanguageCode() or ''
	except Exception:
		current = ''
	print("lang\t%s" % current)
	for v in voices:
		print("%s\t%s\t%s" % (v.identifier(), v.name(), v.language()))

def gotLine(observer, line):
	if not line:
		AppHelper.stopEventLoop()
		return
	line = line.strip(b'\n')
	for ln in line.split(b'\n'):
		handle_line(ln)



def handle_line(line):
	global rate, volume, voice, next_pitch
	line = line.decode('utf-8', 'replace')
	if not line:
		return
	if line[0] == u"s" or line[0] == "l":
		ln = line[1:].replace('[[', ' ')
		ln = ln.replace(u'\u23ce', ' ')
		# macOS 26 doesn't process text in <>, even though we're not telling it to speak SSML.
		ln = html.escape(ln, False)
		u = AVSpeechUtterance.alloc().initWithString_(ln)
		# With VoiceOver on, preferring assistive technology settings makes
		# VoiceOver's voice override the one set on the utterance, so a voice
		# chosen in TDSR was silently ignored. Only defer to VoiceOver when
		# no voice has been chosen.
		u.setPrefersAssistiveTechnologySettings_(voice is None)
		if rate is not None:
			u.setRate_(rate)
		if volume is not None:
			u.setVolume_(volume)
		if voice is not None:
			u.setVoice_(voice)
		if next_pitch is not None:
			u.setPitchMultiplier_(next_pitch)
			next_pitch = None
		avsynth.speakUtterance_(u)
	elif line[0] == "P":
		try:
			next_pitch = min(2.0, max(0.5, float(line[1:])))
		except ValueError:
			next_pitch = None
	elif line[0] == u"x":
		avsynth.stopSpeakingAtBoundary_(AVSpeechBoundaryImmediate)
	elif line[0] == u"r":
		try:
			rate = float(line[1:]) / 100
		except ValueError:
			pass
	elif line[0] == u"v":
		try:
			volume = int(line[1:]) / 100.0
		except ValueError:
			pass
	elif line[0] == "V":
		if not line[1:].strip():
			# An empty value means go back to the system / VoiceOver voice.
			voice = None
			return
		found = find_voice(line[1:])
		if found is not None:
			voice = found

def gotError(observer):
	print("error:")
	AppHelper.stopEventLoop()


def main():
	import sys
	if '--list-voices-tsv' in sys.argv:
		list_voices_tsv()
		return
	if '--list-voices' in sys.argv:
		list_voices()
		return
	observer = FileObserver.alloc().initWithFileDescriptor_readCallback_errorCallback_(
	sys.stdin.fileno(), gotLine, gotError)
	AppHelper.runConsoleEventLoop(installInterrupt=True)

if __name__ == '__main__':
	main()
