import os
import json
import hashlib
from os import path
from util.settings import SETTINGS
from util.enums import SETTINGS_CACHE_SEARCH
from util.misc import ROOT_FOLDER
import util.log as log

CACHE_DIR = path.join(ROOT_FOLDER, ".cache")

class FolderCache:
	OPENABLE_EXTENSIONS = ('.grp', '.dxp.bin', '.ddsx')
	DESC_SUFFIX = "Desc.bin"

	@classmethod
	def isEnabled(cls) -> bool:
		try:
			return SETTINGS.getValue(SETTINGS_CACHE_SEARCH)
		except Exception:
			return True

	@classmethod
	def getCacheDir(cls) -> str:
		if not path.exists(CACHE_DIR):
			try:
				os.makedirs(CACHE_DIR, exist_ok=True)
			except Exception:
				pass
		return CACHE_DIR

	@classmethod
	def getCacheFilePath(cls, folderPath: str) -> str:
		norm = path.normpath(folderPath).replace("\\", "/").rstrip("/").lower()
		key = hashlib.sha256(norm.encode('utf-8')).hexdigest()[:16]
		return path.join(cls.getCacheDir(), f"{key}.json")

	@classmethod
	def hasCache(cls, folderPath: str) -> bool:
		cachePath = cls.getCacheFilePath(folderPath)
		return path.isfile(cachePath)

	@classmethod
	def loadTree(cls, folderPath: str):
		if not cls.isEnabled():
			return None
		cachePath = cls.getCacheFilePath(folderPath)
		if not path.isfile(cachePath):
			return None
		try:
			with open(cachePath, "r", encoding="utf-8") as f:
				data = json.load(f)
			if data.get("root") and path.exists(data["root"]):
				return data.get("tree")
		except Exception as e:
			log.log(f"Failed to load cache from {cachePath}: {e}")
		return None

	@classmethod
	def saveTree(cls, folderPath: str, tree: dict):
		if not cls.isEnabled():
			return
		cachePath = cls.getCacheFilePath(folderPath)
		cls.getCacheDir()
		try:
			data = {
				"root": folderPath.replace("\\", "/"),
				"tree": tree
			}
			with open(cachePath, "w", encoding="utf-8") as f:
				json.dump(data, f)
		except Exception as e:
			log.log(f"Failed to save cache to {cachePath}: {e}")

	@classmethod
	def scanDir(cls, dirPath: str) -> dict:
		norm_dir = dirPath.replace("\\", "/")
		entry = {"path": norm_dir, "files": [], "dirs": {}}
		try:
			with os.scandir(dirPath) as it:
				for item in it:
					if item.is_dir(follow_symlinks=False):
						sub = cls.scanDir(item.path)
						if sub["files"] or sub["dirs"]:
							entry["dirs"][item.name] = sub
					elif item.is_file(follow_symlinks=False):
						name = item.name
						item_path = item.path.replace("\\", "/")
						if name.endswith(cls.DESC_SUFFIX):
							entry["files"].append({
								"name": name,
								"path": item_path,
								"isDesc": True
							})
						else:
							for ext in cls.OPENABLE_EXTENSIONS:
								if name.endswith(ext):
									suffix = ext.lstrip(".")
									entry["files"].append({
										"name": name,
										"path": item_path,
										"isDesc": False,
										"suffix": suffix
									})
									break
		except Exception as e:
			log.log(f"Error scanning directory {dirPath}: {e}")
		return entry

	@classmethod
	def getOrBuildTree(cls, folderPath: str, forceRefresh: bool = False):
		if not forceRefresh:
			tree = cls.loadTree(folderPath)
			if tree is not None:
				return tree

		# Fast scan directory and build tree
		tree = cls.scanDir(folderPath)
		cls.saveTree(folderPath, tree)
		return tree

	@classmethod
	def clearAllCache(cls):
		cacheDir = cls.getCacheDir()
		if path.exists(cacheDir):
			for fname in os.listdir(cacheDir):
				if fname.endswith(".json"):
					try:
						os.remove(path.join(cacheDir, fname))
					except Exception:
						pass

	@classmethod
	def clearCacheForPaths(cls, paths: list):
		for p in paths:
			if path.isdir(p):
				cacheFile = cls.getCacheFilePath(p)
				if path.isfile(cacheFile):
					try:
						os.remove(cacheFile)
					except Exception:
						pass
