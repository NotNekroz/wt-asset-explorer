import sys
from os import path

sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

import contextlib
import threading
from time import time
from util.enums import *
from threading import currentThread

startTime = time()

def getTime():
	return "%08.4f" % (time() - startTime)

curLevel = 0
levelStr = ""

# [WTX patch] lets a GUI mute noisy bulk operations and mirror log lines into a console widget
MUTED = False
sink = None

def setMuted(value:bool):
	global MUTED
	MUTED = bool(value)

def setSink(fn):
	global sink
	sink = fn

# [WTX patch] per-thread state for the export worker threads. A thread that calls setWorkerThread() gets its own indent level
# and (by default) logs nothing, so concurrent workers can neither corrupt the shared indent counter (it raises when it goes
# negative) nor write into the GUI console from a non-GUI thread. Every other thread keeps using the module globals.
_tls = threading.local()

def setWorkerThread(muted:bool = True):
	_tls.worker = True
	_tls.level = 0
	_tls.levelStr = ""
	_tls.muted = bool(muted)

@contextlib.contextmanager
def muted():
	"""Mute the log for the CURRENT thread only (setMuted() mutes every thread)."""
	before = getattr(_tls, "muted", False)
	_tls.muted = True
	try:
		yield
	finally:
		_tls.muted = before

def incrLevel(level:int = 0):
	global curLevel
	global levelStr

	if getattr(_tls, "worker", False):
		_tls.level += level

		if _tls.level < 0:
			raise Exception(f"Impossible level {_tls.level}")

		_tls.levelStr = "    " * _tls.level

		return

	curLevel += level
	levelStr = ""

	if curLevel < 0:
		raise Exception(f"Impossible level {curLevel}")

	for i in range(curLevel):
		levelStr += "    "

def addLevel(level:int = 1):
	incrLevel(level)

def subLevel(level:int = 1):
	incrLevel(-level)


def getLevelStr():
	global levelStr

	if getattr(_tls, "worker", False):
		return _tls.levelStr

	return levelStr

def log(message:str, type:int = 0): # TODO: add multithreading support
	if MUTED or getattr(_tls, "muted", False):
		return

	if sink is not None:
		tag = "[D]" if type == LOG_DEBUG else "[E]" if type == LOG_ERROR else "[W]"
		sink(f"{getTime()} {tag} | {getLevelStr()}{message}", type)

		return

	print(f"{getTime()} [{currentThread().ident}] ", end = "")

	if type == LOG_DEBUG: # could use a switch statement here
		print("[D]", end = "")
	elif type == LOG_ERROR:
		print("[E]", end = "")
	else:
		print("[W]", end = "")
	
	print(f" | {getLevelStr()}{message}")

	pass
