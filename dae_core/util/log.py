import sys
from os import path

sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

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

def incrLevel(level:int = 0):
	global curLevel
	global levelStr

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

	return levelStr

def log(message:str, type:int = 0): # TODO: add multithreading support
	if MUTED:
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
