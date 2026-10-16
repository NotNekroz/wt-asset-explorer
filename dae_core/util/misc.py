import os
import sys
import math
from os import path
from ctypes import cdll
from typing import Iterable
# [WTX patch] the Qt file dialog helper is GUI-only; the backend must not need PyQt5.

# def getPath(relativePath:str):
# 	return path.abspath(path.join(path.dirname(__file__), relativePath))

def getParentDir(filePath:str, level:int = 1):
	if level <= 0:
		return filePath
	else:
		return getParentDir(path.dirname(filePath), level - 1)

if getattr(sys, "frozen", False):
	ROOT_FOLDER = path.dirname(sys.executable)
else:
	# [WTX patch] vendored layout is <root>/dae_core/util/misc.py -> root is 3 levels up
	ROOT_FOLDER = os.environ.get("WTX_ROOT") or getParentDir(path.abspath(__file__), 3)

RES_FOLDER = path.join(ROOT_FOLDER, "res")
LIB_FOLDER = path.join(ROOT_FOLDER, "lib")
UI_FOLDER = path.join(ROOT_FOLDER, "ui")

def getResPath(fileName:str):
	return path.join(RES_FOLDER, fileName)

def loadDLL(name:str):
	dllPath = path.join(LIB_FOLDER, name)

	if not path.exists(dllPath):
		return None
	else:
		try:
			return cdll.LoadLibrary(dllPath)
		except OSError: # [WTX patch] Windows-only DLLs: degrade gracefully elsewhere
			return None

def getUIPath(fileName:str):
	return path.join(UI_FOLDER, fileName)


def matrixMul(A, B):
	if len(A[0]) != len(B):
		raise ValueError("Matrix dimensions do not match")
	
	result = [[0] * len(B[0]) for _ in range(len(A))]
	
	for i in range(len(A)):
		for j in range(len(B[0])):
			for k in range(len(B)):
				result[i][j] += A[i][k] * B[k][j]
	
	return result

def vectorTransform(matrix, vector):
	result = [0] * 3

	for i in range(3):
		for j in range(3):
			result[i] += matrix[i][j] * vector[j]
	
	result[0] += matrix[0][3]
	result[1] += matrix[1][3]
	result[2] += matrix[2][3]

	return result

def matrixToEuler(matrix):
	r11, r12, r13 = matrix[0][:3]
	r21, r22, r23 = matrix[1][:3]

	yaw = math.atan2(r13, math.sqrt(r11**2 + r12**2))

	pitch = math.atan2(-r23, math.sqrt(r21**2 + r22**2))

	roll = math.atan2(r21, r22)

	return pitch, roll, yaw



def openFile(*args, **kwargs):
	# [WTX patch] removed: Qt dialogs live in the new GUI, not in the backend
	raise NotImplementedError("openFile is not available in the headless backend")


powerLabels = (
	" B",
	" KB",
	" MB"
)
powerLabelCnt = len(powerLabels)
power = 2**10

def formatBytes(size):
	n = 0

	while size > power and n < powerLabelCnt:
		size /= power
		n += 1
	
	return str(round(size)) + powerLabels[n]