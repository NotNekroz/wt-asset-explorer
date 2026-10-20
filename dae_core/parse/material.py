import sys
import os
from os import path, getcwd, mkdir, makedirs

sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

import util.log as log
from util.fileread import *
from util.terminable import Packed, Pack, SafeIter, SafeRange, SafeEnumerate, SafeReversed, Terminable
from util.decompression import zstdDecompress, oodleDecompress, zlibDecompress, lzmaDecompress
from util.enums import *
from util.assetcacher import AssetCacher
from struct import pack_into as packInto
from struct import pack, unpack
from ctypes import create_string_buffer
from PIL import Image
from PIL.ImageChops import invert

DDSX_HEADER_SIZE = 0x20

# [REMOVED in cleanup] DYNAMIC_CLASSES  (material.py lines 21-27 of the pre-cleanup file, module level, just after DDSX_HEADER_SIZE).
#   What it was: tuple of War Thunder shader class names that use dynamic camo painting:
#   "dynamic_masked_ship", "dynamic_painted_by_mask", "dynamic_masked", "dynamic_alpha_blend", "dynamic_camo".
#   Worked with: nothing any more. It was the class whitelist of the first camo-bake version (only these shader classes
#   were baked), later replaced by the substring test in CAMO_CLASS_KEYS / CAMO_CLASS_EXCLUDED / isCamoClass(), which
#   also covers tanks, ground vehicles and boats. No other file referenced it.

import subprocess
import tempfile
import shutil
from pathlib import Path

def _texconv_convert_dds_bytes(dds_bytes: bytes, output_format: str = "DDS"):
	try:
		from util.misc import ROOT_FOLDER as _WTX_ROOT # [WTX patch] do not depend on the CWD
		texconv_path = path.join(_WTX_ROOT, "texconv.exe")

		if not path.exists(texconv_path):
			print("[ERROR] texconv.exe not found!")
			return None

		with tempfile.TemporaryDirectory() as tmpdir:
			in_file = path.join(tmpdir, "input.dds")

			with open(in_file, "wb") as f:
				f.write(dds_bytes)

			result = subprocess.run(
				[
					texconv_path,
					"-y",
					"-ft", output_format.lower(),
					"-o", tmpdir,
					in_file
				],
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE
			)

			if result.returncode != 0:
				print("[texconv FAIL]")
				print(result.stderr.decode(errors="ignore"))
				return None

			out_file = path.join(tmpdir, f"input.{output_format.lower()}")

			if not path.exists(out_file):
				print("[texconv ERROR] Output file missing")
				return None

			with open(out_file, "rb") as f:
				return f.read()

	except Exception as e:
		print(f"[texconv EXCEPTION] {e}")
		return None

class DDSx(Packed):
	class Header:
		def __repr__(self):
			return f"<DDSxHeader {self.getFormattedData()}>"
		
		def getFormattedData(self):
			return f"{self.d3dformat} {self.w}x{self.h}"
		
		def __init__(self, stream):
			# Safely extract the raw 32 bytes regardless of stream type/wrapper
			if hasattr(stream, "read"):
				raw = stream.read(32)
			elif isinstance(stream, (bytes, bytearray)):
				raw = stream
			else:
				raw = bytes(stream)
			
			if len(raw) < 32:
				raise ValueError(f"DDSx header expected 32 bytes, got {len(raw)}")

			# Unpack the 32-byte header structure directly using struct.unpack
			(
				label_b,
				fmt_b,
				val_cMethod_flags,
				self.w,
				self.h,
				self.levels,
				self.hqPartLevels,
				self.depth,
				self.bitsPerPixel,
				lq_mq,
				dxt_uq,
				self.memSz,
				self.packedSz
			) = unpack("<4s4sIHHBBHHBBII", raw[:32])

			self.label = label_b.decode("latin1", errors="ignore")
			self.d3dformat = fmt_b.decode("latin1", errors="ignore")

			self.cMethod = (val_cMethod_flags >> 24) & 0xFF
			self.flags = val_cMethod_flags & 0xFFFFFF

			self.lQmip = (lq_mq >> 4) & 0xF
			self.mQmip = lq_mq & 0xF

			self.dxtShift = (dxt_uq >> 4) & 0xF
			self.uQmip = dxt_uq & 0xF

		def getBin(self):
			label_b = self.label.encode("latin1") if isinstance(self.label, str) else self.label
			fmt_b = self.d3dformat.encode("latin1") if isinstance(self.d3dformat, str) else self.d3dformat
			return pack("4s4sIHHBBHHBBII", 	label_b, 
											fmt_b, 
											self.cMethod * 0x1000000 + self.flags, 
											self.w, 
											self.h, 
											self.levels,
											self.hqPartLevels,
											self.depth,
											self.bitsPerPixel,
											self.lQmip * 0x10 + self.mQmip,
											self.dxtShift * 0x10 + self.uQmip,
											self.memSz,
											self.packedSz)

	__FLAGS = {
		"FLG_ADDRU_MASK"          : 0xf,
		"FLG_ADDRV_MASK"          : 0xf0,
		"FLG_ARRTEX"              : 0x200_000,
		"FLG_COMPR_MASK"          : 0xe0_000_000,
		"FLG_CONTIGUOUS_MIP"      : 0x100,
		"FLG_CUBTEX"              : 0x800,
		"FLG_GAMMA_EQ_1"          : 0x8_000,
		"FLG_GENMIP_BOX"          : 0x2_000,
		"FLG_GENMIP_KAIZER"       : 0x4_000,
		"FLG_GLES3_TC_FMT"        : 0x100_000,
		"FLG_HASBORDER"           : 0x400,
		"FLG_HOLD_SYSMEM_COPY"    : 0x10_000,
		"FLG_HQ_PART"             : 0x80_000,
		"FLG_NEED_PAIRED_BASETEX" : 0x20_000,
		"FLG_VOLTEX"              : 0x1_000,

		"FLG_REV_MIP_ORDER"       : 0x40_0, # 0

		"FLG_NONPACKED"           : 0x200,
		"FLG_ZSTD"                : 0x20_000_000,
		"FLG_7ZIP"                : 0x40_000_000, # LZMA
		"FLG_OODLE"               : 0x60_000_000,
		"FLG_ZLIB"                : 0x80_000_000
	}

	__DXT_FORMATS = (
		b"DXT1",
		b"DXT5",
		b"BC7 ",
		b"ATI1")

	__DDS_HEADER = (
		0x44, 0x44, 0x53, 0x20, 0x7C, 0x00, 0x00, 0x00,
		0x07, 0x10, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x20, 0x00, 0x00, 0x00,
		0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
	)

	__DDS_2_HEADER = (
		0x44, 0x44, 0x53, 0x20, 0x7C, 0x00, 0x00, 0x00,
		0x06, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x20, 0x00, 0x00, 0x00,
		0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x08, 0x10, 0x40, 0x00,
		# 0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x62, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00
	)

	__DX10_FORMATS = (
		b"BC7 ", 
		b"BC7 "
	)

	@classmethod
	@property
	def classIconName(self) -> str:
		return "asset_tex.bmp"
	
	@classmethod
	@property
	def classNiceName(cls) -> str:
		return "DDSx"
	
	@classmethod
	@property
	def fileExtension(cls) -> str:
		return "ddsx"
	
	def __repr__(self):
		return f"<DDsX {self.name}: {self.__header.getFormattedData()}>"

	def __init__(self, 
		  filePath:str, 
		  name:str = None, 
		  size:int = 0, 
		  dataOffset:int = DDSX_HEADER_SIZE, 
		  header:Header = None):
		super().__init__(filePath, name, size, dataOffset)

		self._setName(self.name.split('*')[0].split("$")[0])

		if dataOffset == DDSX_HEADER_SIZE:
			self.__singleFile = True

			if header is None:
				file = open(self.filePath, "rb")

				header = self.Header(BinFile(file.read(DDSX_HEADER_SIZE)))

				file.close()
		elif header is None:
			raise NotImplementedError(f"Missing header for packed DDSx (ofs={dataOffset})")
		else:
			self.__singleFile = False
		
		self.__header = header

		self._setSize(self.__header.packedSz)
		self._setValid()
	
	def __decompress__(self, data:bytes):
		cMethod = self.__header.cMethod

		log.log(f"Decompressing {self.__header.memSz}b using {cMethod=}")

		if cMethod == 0x20:
			data = zstdDecompress(data)
		elif cMethod == 0x60:
			data = oodleDecompress(data, maxOriginalSize = self.__header.memSz)
		elif cMethod == 0x80:
			data = zlibDecompress(data)
		elif cMethod == 0x40:
			data = lzmaDecompress(data)
		
		return data

	def getPixelCnt(self):
		return self.__header.w * self.__header.h
	
	def getMipSize(self, width:int, height:int, dxtVersion:bytes):
		dxtSize = max(1, (width + 3) // 4) * max(1, (height + 3) // 4)

		if dxtVersion == b"DXT1" or dxtVersion == b"ATI1":
			return dxtSize * 8
		elif dxtVersion == b"DXT5" or dxtVersion == b"BC7 ":
			return dxtSize * 16
		else:
			log.log(f"Unknown DXT version {dxtVersion}, using DXT1 scale", LOG_WARN)

			return dxtSize * 8

	def getData(self):
		return self.__decompress__(self.getBin().read())
	
	def save(self, output:str = getcwd()):
		self._save(output, self.__header.getBin() + self.getBin().read())

	def getDDS(self):
		log.log(f"Converting {self.name} to DDS")
		log.addLevel()

		d3dformat = self.__header.d3dformat
		# Ensure d3dformat is a bytes object
		if isinstance(d3dformat, str):
			d3dformat = d3dformat.encode("latin1", errors="ignore")

		w, h = self.__header.w, self.__header.h

		log.log(f"cMethod       =	{hex(self.__header.cMethod)}")
		log.log(f"D3D Format    =	{d3dformat}")
		log.log(f"Resolution    =	{w}x{h}")

		data = self.getData()

		if self.__header.flags & 0x40000:
			log.log("Found reversed mip order")
			
			pos = 0
			images = []

			for level in SafeRange(self, self.__header.levels - 1, -1, -1):
				width = w // (2 ** level)
				height = h // (2 ** level)

				size = self.getMipSize(width, height, d3dformat)
				images.append(data[pos:pos + size])
				pos += size

			data = bytearray()

			for image in SafeReversed(self, images):
				data.extend(image)
		
		if d3dformat in self.__DX10_FORMATS:
			log.log("Found DX10 texture")

			ddsData = create_string_buffer(0x94)

			packInto('148B', ddsData, 0, *self.__DDS_2_HEADER)
			packInto('I', ddsData, 0xc, h)
			packInto('I', ddsData, 0x10, w)
			packInto('I', ddsData, 0x14, self.__header.memSz)
			packInto('B', ddsData, 0x1c, self.__header.levels)
			packInto('4s', ddsData, 0x54, b"DX10")
		else:
			ddsData = create_string_buffer(0x80)

			packInto('128B', ddsData, 0, *self.__DDS_HEADER)
			packInto('I', ddsData, 0xc, h)
			packInto('I', ddsData, 0x10, w)
			packInto('I', ddsData, 0x14, self.__header.memSz)
			packInto('B', ddsData, 0x1c, self.__header.levels)
			packInto('4s', ddsData, 0x54, d3dformat)
		
		data = ddsData.raw + data

		log.log(f"Done: final size = {len(data)}")

		log.subLevel()

		return data
	
	@property
	def isSingleFile(self):
		return self.__singleFile
	
	def getParentName(self):
		if self.isSingleFile:
			return path.basename(path.dirname(self.filePath))
		else:
			fName = path.basename(self.filePath).split(".")[0]
			
			if fName.endswith("-hq"):
				fName = fName[:-3]
			
			if fName.startswith("hq_tex"):
				fName = fName[7:]
			
			return fName

	def exportDDS(self, output:str = getcwd()):
		fileName = f"{self.name}.dds"

		output = path.normpath(f"{output}\\{fileName}")

		log.log(f"Saving {fileName}")
		log.addLevel()

		binData = self.getDDS()

		file = open(output, "wb")

		file.write(binData)

		file.close()

		log.log(f"Wrote {len(binData)} bytes to {output}")
		log.subLevel()

class DDSxTexturePack2(Pack): # TODO: Add logs
	@classmethod
	@property
	def classIconName(self) -> str:
		return "folder_dxp.bmp"

	@classmethod
	@property
	def classNiceName(cls) -> str:
		return "DDSx Texture Pack"
	
	@classmethod
	@property
	def fileExtension(cls) -> str:
		return "dxp.bin"
	
	def __init__(self, filePath:str, name:str = None):
		super().__init__(filePath, name)

		self.__readFile__()
		self._setValid()

		self.__pulledAll = False
	
	def __readFile__(self):
		f = open(self.filePath, "rb")
		f.seek(0x8, 1)
		fileCnt = readInt(f)

		file = BinFile(f.read(readInt(f)))
		f.close()

		nameMapIndiciesOfs = readInt(file)
		nameMapIndiciesCnt = readInt(file)

		file.seek(8, 1)

		self.__ddsxHeadersOfs = readInt(file)
		if readInt(file) != fileCnt:
			raise Exception("ddsxHeader cnt != fileCnt")

		file.seek(8, 1)

		self.__ddsxRecordsOfs = readInt(file) + 0xC
		if readInt(file) != fileCnt:
			raise Exception("ddsxRecord cnt != fileCnt")

		file.seek(0x10, 1)

		self.__nameMap = readNameMap(file, nameMapIndiciesCnt, nameMapIndiciesOfs, 0x38, self, True)

		self.__files:list[DDSx] = []

		for i in SafeRange(self, fileCnt):
			ddsx = self.__readDDSx__(file, i)

			if ddsx is not None:
				self.__files.append(ddsx)
		
		file.close()
	
	def __readDDSx__(self, file:BinFile, id:int):
		name = self.__nameMap[id]

		log.log(f"Pulling {id}:{name} from {self.name}.dxp.bin")
		log.addLevel()

		file.seek(self.__ddsxHeadersOfs + id * DDSX_HEADER_SIZE, 0)

		header = DDSx.Header(BinFile(file.read(DDSX_HEADER_SIZE)))

		ddsx = None

		if header.packedSz == 0:
			log.log(f"Ignoring null sized DDSx")
		else:
			file.seek(self.__ddsxRecordsOfs + id * 0x18, 0)

			offset = readInt(file)
			
			ddsx = DDSx(self.filePath, name, header.memSz, offset, header)

		log.subLevel()

		return ddsx
	
	@staticmethod
	def peekNames(filePath:str) -> list[str]:
		"""[WTX patch] reads only the name table of a dxp.bin (no DDSx objects) - used to index textures quickly."""
		f = open(filePath, "rb")
		try:
			f.seek(0x8, 1)
			fileCnt = readInt(f)
			file = BinFile(f.read(readInt(f)))
		finally:
			f.close()

		nameMapIndiciesOfs = readInt(file)
		nameMapIndiciesCnt = readInt(file)

		file.seek(8, 1)
		readInt(file); readInt(file)
		file.seek(8, 1)
		readInt(file); readInt(file)
		file.seek(0x10, 1)

		return readNameMap(file, nameMapIndiciesCnt, nameMapIndiciesOfs, 0x38, None, True)

	def getDDSxById(self, id:int):
		assert id >= 0 and id <= len(self.__files)

		return self.__files[id]
	
	def getDDSxByName(self, name:str):
		for k, v in SafeRange(self, self.__nameMap):
			if v == name:
				return self.getDDSxById(k)
				
		return None
	
	def getPackedFiles(self) -> list[DDSx, False]:
		return self.__files

def ftm(tex:str): # format texture to material
	tex = tex.split("*")[0]
	splitted = tex.split("_")
	
	noTex = True

	for i in range(len(splitted) - 1, 0, -1):
		part = splitted[i]

		if part == "tex":
			if i + 1 < len(splitted) and len(splitted[i + 1]) <= 2:
				splitted.pop(i + 1)
			
			splitted.pop(i)
			noTex = False

			break
	
	if noTex and len(splitted[-1]) < 2:
		splitted.pop()
	
	return "_".join(splitted)

def addComp(list:list[str], comp:str):
	if comp is not None:
		list.append(ftm(comp))

def getBestTex(self:Terminable, ddsx:list[DDSx]):
	best = ddsx[0]
	
	for i in SafeRange(self, 1, len(ddsx)):
		cur = ddsx[i]

		if cur.getPixelCnt() > best.getPixelCnt():
			best = cur

	return best

class TexturePathDict:
	class Texture:
		def __repr__(self):
			return f"<{self.tex}:{self.texType}>"
		
		def __init__(self, tex:str, texType:int, texPath:str):
			self.__tex = tex
			self.__texType = texType
			self.__texPath = texPath

			if texType == TEXTURE_MASKED:
				self.__newName:str = self.tex + "m"
			else:
				self.__newName:str = None

		def setType(self, texType:int):
			self.__texType = texType

		@property
		def newName(self):
			return self.__newName
		
		@property
		def tex(self):
			return self.__tex
		
		@property
		def texType(self):
			return self.__texType
		
		@property
		def texPath(self):
			return self.__texPath
	
	def __init__(self, customPath:str = None):
		self.__d:dict[str, TexturePathDict.Texture] = {}
		
		if customPath is None:
			self.__customPath = "models"
		else:
			self.__customPath = f"models/{customPath}"

	def getTexPath(self, texName:str):
		cachedDDSx = AssetCacher.getCachedAsset(DDSx, texName)

		if cachedDDSx:
			cachedDDSx:DDSx = cachedDDSx[0]

			return f"{self.customPath}/{cachedDDSx.getParentName()}"
		else:
			return self.customPath
	
	def get(self, texName:str):
		return self.__d.get(texName)

	def append(self, 
			texName:str, 
			texType:int, 
			vmt:list[str],
			vmtKey:str):
		texName = MaterialData.getTexFileName(texName)
		texPath = self.getTexPath(texName)
		tex = TexturePathDict.Texture(texName, texType, texPath)

		if tex.newName:
			texName = tex.newName

		vmt.append(f'${vmtKey} "{texPath}/{texName}"')
		
		self.__d[texName] = tex

		return tex

	def getDict(self):
		return self.__d

	@property
	def customPath(self):
		return self.__customPath

class MaterialData(Terminable):
	def __repr__(self):
		return f"<MaterialData {self.getName()}>"
	
	def __init__(self):
		self.__name:str = None
		self.__params:dict[str, str] = None

		self.diff = (0, 0, 0, 1)
		self.amb = (0, 0, 0, 1)
		self.emis = (0, 0, 0, 1)
		self.spec = (0, 0, 0, 1)

		self.cls:str = None
		self.par:str = None

		self.__detailList:list[str] = None
		self.__detailNormalList:list[str, None] = None
		self.__textureSlots:list[str] = [None for _ in range(11)]
		self.textures:list[str] = []
	
	def isLayered(self):
		return self.cls.find("layered") != -1
	
	def isDynamic(self):
		return self.cls.find("dynamic") != -1
	
	def __generateName__(self):
		if len(self.textures) == 0:
			return self.cls
		
		components:list[str] = []
		
		if not self.isLayered():
			addComp(components, self.__textureSlots[0])
			addComp(components, self.__textureSlots[1])

		detailStart = 4

		if not self.isDynamic() or self.isLayered():
			detailStart = 3
		
		for i in range(detailStart, len(self.__textureSlots), 2):
			detail = self.__textureSlots[i]

			addComp(components, detail)

			if detail is None and i + 1 < len(self.__textureSlots):
				addComp(components, self.__textureSlots[i + 1])

		# A material whose slots give no name part (the F-16 canopy) used to get "" here: an empty "newmtl" / "usemtl" that
		# Blender cannot resolve (it falls back to a plain white opaque default) and that the texture prune dropped.
		return "_".join(components) or self.cls or "material"

	def checkModifiedName(self, tex:str, texturePaths:TexturePathDict):
		if texturePaths is None or texturePaths.get(tex + "m") is None:
			return tex
		else:
			return tex + "m"

	@classmethod
	def getTexFileName(cls, tex:str):
		return tex.split("*")[0]

	def addTexSlot(self, slotName:str, tex:str):
		slotIdx = int(slotName[1:])

		self.__textureSlots[slotIdx] = tex
		self.textures.append(tex)
	
	def getName(self):
		if self.__name is None:
			self.__name = self.__generateName__()
		
		return self.__name
	
	def setName(self, name:str):
		self.__name = name
	
	def getParams(self):
		if self.__params is None:
			params = {}
			
			paramL = self.par.split("\n")

			for param in SafeIter(self, paramL):
				splitParam = param.split("=")

				if len(splitParam) == 1:
					key = splitParam[0]
					val = ""
				else:
					key, val = splitParam

				params[key] = val

			self.__params = params

		return self.__params

	def getTextureSlots(self):
		return self.__textureSlots

	def convertTex(self, texture: str, data: bytes, texturePaths: TexturePathDict = None, forceConvert: bool = False,
				   texN: str = None):
		newName = None

		if texturePaths is not None and texturePaths.get(texture if texN is None else texN) is not None:
			tex = texturePaths.get(texture if texN is None else texN)

			texType = tex.texType

			if tex.newName is not None:
				newName = tex.newName
		else:
			texType = None

		# --- Try Pillow first ---
		if texType is not None or forceConvert:
			try:
				img = Image.open(BytesIO(data))

				if texType is not None and texType != TEXTURE_GENERIC:
					r, g, b, a = img.split()
					img.close()

					if texType == TEXTURE_MASKED:
						img = Image.merge("RGBA", (r, g, b, invert(a)))
					elif texType == TEXTURE_NORMAL:
						whiteChannel = Image.new("L", img.size, 255)
						img = Image.merge("RGBA", (a, g, whiteChannel, r))

				outBin = BytesIO()
				img.save(outBin, format="DDS")
				data = outBin.getvalue()
				outBin.close()
				img.close()

				return data, newName

			except Exception as e:
				print(f"[Pillow FAIL] {texture}: {e}")

		# --- Fallback to texconv ---
		print(f"[texconv FALLBACK] {texture}")

		texconv_data = _texconv_convert_dds_bytes(data, "DDS")

		if texconv_data is not None:
			return texconv_data, newName

		print(f"[SKIP] Failed to convert texture: {texture}")
		return data, newName
	
	_exportedTextures = set()  # (output file, forceConvert, texN) already written by this process

	def skipsCamoTexture(self, tex) -> bool:
		"""True for the camo texture of a material whose diffuse the skin replaces: it would only be extracted for a bake that no longer happens."""
		if not SKIN_OVERRIDES or not self.diffuse or skinOverride(self.diffuse) is None:
			return False

		base = str(tex).split("*")[0].lower()
		return "camo" in base and base != str(self.diffuse).split("*")[0].lower() and isCamoClass(self.cls)

	def _exportSkinTexture(self, name: str, src: str, outpath: str) -> bool:
		"""Write the skin's picture for texture `name` into <outpath>/<name>.dds. Colour: opaque (on camo materials the alpha is the
		paint mask, not transparency). Normal map (*_n): kept in the game's channel layout, the derived <n>_obj.dds / <n>_gltf.png of an
		earlier export are removed so the exporters rebuild them from this one."""
		key = name.lower()

		if key in _skinWritten:
			return True

		import numpy as np

		arr = _loadSkinRgba(src)
		makedirs(outpath, exist_ok=True)

		if key.endswith("_n"):
			for stale in (f"{name}_obj.dds", f"{name}_gltf.png", f"{name}_mr.png", f"{name}_rough.png", f"{name}_metal.png"):
				try:
					os.remove(path.join(outpath, stale))
				except OSError:
					pass

			# The exporters read a normal map in the layout of the game's own ones: X in alpha, Y in green. Say what the skin's file holds, so a
			# normal map saved in another layout (plain RGB, alpha all 255) is visible in the log instead of just "not working".
			rng = lambda c: f"{int(arr[..., c].min())}-{int(arr[..., c].max())}"
			print(f"[skin] normal map {path.basename(src)} ({arr.shape[1]}x{arr.shape[0]}): red {rng(0)}, green {rng(1)}, alpha {rng(3)}")

			if arr[..., 3].min() == arr[..., 3].max():
				print(f"[skin] WARNING {path.basename(src)}: the alpha channel is one flat value. The preview reads the normal's X from alpha (the layout "
				      "of the game's own normal maps), so this file will show flat / wrong in the preview.")
		elif isCamoClass(self.cls) or arr[..., 3].max() == 0:
			arr[..., 3] = 255

		Image.fromarray(np.ascontiguousarray(arr), "RGBA").save(path.join(outpath, f"{name}.dds"), format="DDS")

		if key.endswith("_n") and SKIN_GLTF[0]:
			# gltf_conv builds this PNG itself later but swallows every error; doing it here says why it failed.
			raw = path.join(outpath, f"{name}.dds")
			png = path.join(outpath, f"{name}_gltf.png")

			try:
				from parse.gltf_conv import make_gltf_normal
				make_gltf_normal(Path(raw))
			except Exception as e:
				print(f"[skin] WARNING {name}: GLB normal map failed: {type(e).__name__}: {e}")

			if path.exists(png):
				print(f"[skin] {name}_gltf.png built from the skin's normal map")
			else:
				print(f"[skin] WARNING {name}: no {name}_gltf.png was produced from {path.basename(src)}; the GLB will point at the raw .dds instead")
				try:
					Image.open(raw).load()
				except Exception as e:
					print(f"[skin]   the written {name}.dds cannot be read back: {type(e).__name__}: {e}")

		_skinWritten.add(key)
		SKIN_USED.add(key)
		log.log(f"Skin texture {path.basename(src)} -> {name}.dds")
		return True

	def exportTexture(self, texture:str, output:str = getcwd(), forceConvert:bool = False, texturePaths:TexturePathDict = None, texN:str = None):
		outpath = path.join(output, "textures")

		skinSrc = skinOverride(texture)

		if skinSrc is not None and texN in (None, str(texture).split("*")[0]):
			try:
				self._exportSkinTexture(str(texture).split("*")[0], skinSrc, outpath)
				return
			except Exception as e:
				print(f"[WARNING] Skin texture {path.basename(skinSrc)} not used ({type(e).__name__}: {e}); extracting the game's {texture} instead")

		# The same texture is requested by several materials / slots; converting + writing it again is pure waste.
		doneKey = (path.abspath(path.join(outpath, str(texture).split("*")[0] + ".dds")), forceConvert, texN)

		if doneKey in MaterialData._exportedTextures and path.exists(doneKey[0]):
			return

		MaterialData._exportedTextures.add(doneKey)

		if not path.exists(outpath):
			mkdir(outpath)
			
		name = texture.split("*")[0]

		ddsx:list[DDSx] = AssetCacher.getCachedAsset(DDSx, name)

		if not ddsx:
			log.log(f"[WARNING] Texture {name} not found in cache. Attempting fallback search...")
			# Safely handle cache lookup without relying on missing class methods
			try:
				# Try querying lowercase or generic variants directly from the cacher dictionary if exposed, 
				# otherwise fall back to checking if a texture pack can provide it.
				ddsx = AssetCacher.getCachedAsset(DDSx, name.lower())
			except Exception:
				pass
			
		if not ddsx:
			log.log(f"{name} not found anywhere in cache, skipping.")
			return
		
		data = getBestTex(self, ddsx).getDDS()
		data, texN = self.convertTex(texture, data, texturePaths, forceConvert, texN)

		if texN is not None:
			name = texN

		output = path.join(outpath, f"{name}.dds")

		file = open(output, "wb")
		file.write(data)
		file.close()

		log.log(f"Wrote {len(data)} bytes to {output}")
	
	def getDMF(self):
		buffer = BBytesIO()

		name = self.getName()
		
		buffer.writeString(name)
		buffer.writeString(self.cls)
		
		buffer.write(pack("ffff", *self.diff))
		buffer.write(pack("ffff", *self.amb))
		buffer.write(pack("ffff", *self.emis))
		buffer.write(pack("ffff", *self.spec))

		for tex in SafeIter(self, self.__textureSlots):
			buffer.writeString(tex)
		
		params = self.getParams()

		buffer.writeInt(len(params))

		for k in params:
			buffer.writeString(k)
			buffer.writeString(params[k])

		value = buffer.getvalue()

		buffer.close()

		return value

	def __eq__(self, other):
		return self.__textureSlots == other.__textureSlots

	def detail1IsDiffuse(self):
		return self.isLayered() and self.cls != "rendinst_simple_layered"

	@property
	def diffuse(self):
		if self.detail1IsDiffuse():
			self.__initDetailList__()

			if len(self.__detailList) >= 1:
				return self.__detailList[0]
			else:
				return None
		else:
			# Always return t0 as the primary diffuse/base detail map for the model
			return self.__textureSlots[0]
	@property
	def normal(self):
		if self.detail1IsDiffuse() and len(self.__detailNormalList) > 0:
			return self.__detailNormalList[0]
		else:
			return self.__textureSlots[2]
	
	@property
	def mask(self):
		return self.__textureSlots[1]
	
	def __initDetailList__(self):
		if self.__detailList is None:
			detailList = []
			detailNormalList = []

			if self.cls != "dynamic_painted_by_mask":
				detailStart = 4

				if not self.isDynamic() or self.isLayered():
					detailStart = 3
				
				for i in range(detailStart, len(self.__textureSlots), 2):
					tex = self.__textureSlots[i]

					if tex is not None:
						detailList.append(tex)

						tex = self.__textureSlots[i + 1]

						detailNormalList.append(tex)
			
			self.__detailNormalList = detailNormalList
			self.__detailList = detailList

	@property 
	def detail(self):
		self.__initDetailList__()

		if self.detail1IsDiffuse():
			return self.__detailList[1:]
		else:
			return self.__detailList
	
	@property 
	def detailNormal(self):
		self.__initDetailList__()

		if self.detail1IsDiffuse():
			return self.__detailNormalList[1:]
		else:
			return self.__detailNormalList

	def getVMTcomments(self):
		return [
			f"// {self.cls}",
			"",
			"// params",
			"\n\t".join(tuple(f"//\t{k}={v}" for k, v in SafeIter(self, self.getParams().items()))),
			"",
			"// textures by slot",
			"\n\t".join(tuple(f"//\t{k}={v}" for k, v in SafeEnumerate(self, self.getTextureSlots()))),
			""]

	def getVMT(self, texturePaths:TexturePathDict):
		params = self.getParams()
		getScale = lambda x: 1 / float(params.get(x)) if x in params else 1

		vmt = self.getVMTcomments()

		if self.diffuse is not None:
			texType = TEXTURE_GENERIC
			
			if self.cls.find("masked") != -1:
				texType = TEXTURE_MASKED
			
			diffuse = texturePaths.append(self.diffuse, texType, vmt, "basetexture")

			if self.detail1IsDiffuse():
				vmt.append(f"$basetexturetransform center .5 .5 scale {getScale('detail1_tile_u')} {getScale('detail1_tile_v')} rotate 0 translate 0 0")
		else:
			diffuse = None

		if self.normal is not None:
			texturePaths.append(self.normal, TEXTURE_NORMAL, vmt, "bumpmap")

		mask = self.mask
		detail = self.detail

		if len(detail) > 0 and (mask is None or self.isLayered()):
			if self.detail1IsDiffuse():
				tileParam = "detail2"
			else:
				tileParam = "detail1"
			
			mask = detail[0]
		else:
			tileParam = "mask"

		if mask is not None:
			texturePaths.append(mask, TEXTURE_GENERIC, vmt, "detail")

			vmt.append("$detailblendmode 4")
			vmt.append(f"$detailscale [{getScale(f'{tileParam}_tile_u')} {getScale(f'{tileParam}_tile_v')}]")

			if self.isLayered():
				vmt.append("$detailblendfactor 0.5")

			gamma = params.get(f"{tileParam}_gamma_end")

			if gamma is None:
				gamma = params.get(f"{tileParam}_gamma_start")

			if gamma is not None:
				gamma = float(gamma) * 2

				vmt.append(f"$detailtint {'{'} {gamma} {gamma} {gamma} {'}'}")

		if self.cls.find("glass") != -1:
			vmt.append("$translucent 1")
			vmt.append("$nocull 1")
		elif ("atest" in params and params["atest"] != "128") or self.cls.find("atest") != -1:
			vmt.append("$alphatest 1")
			vmt.append("$nocull 1")
		elif "two_sided" in params:
			vmt.append("$nocull 1")

		fullVMT = "\n".join(('"VertexLitGeneric"', "{", "\t" + "\n\t".join(vmt), "}"))

		return fullVMT

# [gltf] Decode War Thunder's packed normal map (X in alpha, Y in green) into a standard RGB tangent-space
# normal map. Written next to the original as <name>_gltf.png; the original .dds is NOT touched.
NORMAL_FLIP_G = False  # True if bumps look inverted in your viewer (DirectX vs OpenGL green channel)

def decodeNormalForGltf(ddsPath: str):
	try:
		import numpy as np
		dst = path.splitext(ddsPath)[0] + "_gltf.png"

		if not path.exists(ddsPath) or path.exists(dst):
			return dst if path.exists(dst) else None

		arr = readDdsRgba(ddsPath)

		if arr is None:
			img = Image.open(ddsPath).convert("RGBA")
			arr = np.asarray(img)
			img.close()

		# X is stored in alpha, Y in green; Z is rebuilt. float32 in-place maths keeps this fast on 4096x4096.
		nx = arr[..., 3].astype(np.float32)
		ny = arr[..., 1].astype(np.float32)
		nx *= 2.0 / 255.0
		nx -= 1.0
		ny *= 2.0 / 255.0
		ny -= 1.0

		if NORMAL_FLIP_G:
			np.negative(ny, out=ny)

		nz = 1.0 - nx * nx - ny * ny
		np.clip(nz, 0.0, 1.0, out=nz)
		np.sqrt(nz, out=nz)

		out = np.empty(arr.shape[:2] + (3,), dtype=np.uint8)
		out[..., 0] = (nx * 127.5 + 127.5 + 0.5).astype(np.uint8)
		out[..., 1] = (ny * 127.5 + 127.5 + 0.5).astype(np.uint8)
		out[..., 2] = (nz * 127.5 + 127.5 + 0.5).astype(np.uint8)

		Image.fromarray(out, "RGB").save(dst, compress_level=1)

		return dst
	except Exception as e:
		print(f"[WARNING] Could not decode normal map {ddsPath}: {e}")
		return None

# [obj] Same decode as decodeNormalForGltf, but written as an UNCOMPRESSED .dds (<n>_obj.dds) for the OBJ/MTL path.
# Blender's OBJ importer feeds map_Bump into a Normal Map node, which expects plain RGB tangent-space normals; the game's
# packed original (X in alpha, Y in green, red = other data) therefore shades wrongly. The original <n>.dds is NOT touched.
def decodeNormalForObj(ddsPath: str):
	try:
		import numpy as np
		dst = path.splitext(ddsPath)[0] + "_obj.dds"

		if not path.exists(ddsPath) or path.exists(dst):
			return dst if path.exists(dst) else None

		arr = readDdsRgba(ddsPath)

		if arr is None:
			img = Image.open(ddsPath).convert("RGBA")
			arr = np.asarray(img)
			img.close()

		nx = arr[..., 3].astype(np.float32)
		ny = arr[..., 1].astype(np.float32)
		nx *= 2.0 / 255.0
		nx -= 1.0
		ny *= 2.0 / 255.0
		ny -= 1.0

		if NORMAL_FLIP_G:
			np.negative(ny, out=ny)

		nz = 1.0 - nx * nx - ny * ny
		np.clip(nz, 0.0, 1.0, out=nz)
		np.sqrt(nz, out=nz)

		out = np.empty(arr.shape[:2] + (4,), dtype=np.uint8)
		out[..., 0] = (nx * 127.5 + 127.5 + 0.5).astype(np.uint8)
		out[..., 1] = (ny * 127.5 + 127.5 + 0.5).astype(np.uint8)
		out[..., 2] = (nz * 127.5 + 127.5 + 0.5).astype(np.uint8)
		out[..., 3] = 255

		tmp = dst + ".part"   # a half-written file must never be mistaken for a finished one
		Image.fromarray(out, "RGBA").save(tmp, format="DDS")
		os.replace(tmp, dst)

		return dst
	except Exception as e:
		print(f"[WARNING] Could not decode normal map for OBJ {ddsPath}: {e}")
		return None

# [obj] Gloss and metal of a War Thunder _n texture for the OBJ/MTL path. The _n carries them next to the normal: red = smoothness
# (bright = glossy), blue = metalness (War Thunder wiki: PBR texture compliance). Blender's OBJ importer reads map_Pr (roughness)
# and map_Pm (metallic) as greyscale pictures, so write <n>_rough.png (255 - red) and <n>_metal.png (blue). The original <n>.dds
# is NOT touched. Used for the game's own normal maps and for a custom skin's alike (the GLB does the same, gltf_conv.make_gltf_mr).
GLOSS_SMOOTH_SCALE = 0.78  # red (smoothness) is multiplied by this before it becomes roughness; lower = rougher / less shiny (1.0 = the raw value)
GLOSS_METAL_SCALE = 1.0    # blue (metalness) is multiplied by this; lower = less metal. 0 = no metal at all

def glossMetalMaps(arr, label: str = ""):
	"""The _n texture's RGBA array -> (roughness, metalness) as uint8 pictures, the one place both exporters take them from.
	Also logs the range of the two channels, so a too shiny / too dull result can be traced to the data."""
	import numpy as np
	red, blue = arr[..., 0], arr[..., 2]
	print(f"[gloss] {label}: smoothness (red) {int(red.min())}-{int(red.max())} mean {float(red.mean()):.0f}, "
	      f"metal (blue) {int(blue.min())}-{int(blue.max())} mean {float(blue.mean()):.0f}")
	smooth = np.clip(red.astype(np.float32) * GLOSS_SMOOTH_SCALE, 0, 255)
	metal = np.clip(blue.astype(np.float32) * GLOSS_METAL_SCALE, 0, 255)
	return (255.0 - smooth + 0.5).astype(np.uint8), (metal + 0.5).astype(np.uint8)

def decodeGlossMetalForObj(ddsPath: str):
	try:
		import numpy as np
		base = path.splitext(ddsPath)[0]
		rough, metal = base + "_rough.png", base + "_metal.png"

		if not path.exists(ddsPath):
			return None

		# Already built from this very file (or a newer one): keep it. A custom skin's stale pair is deleted when the skin is written.
		srcTime = path.getmtime(ddsPath)
		if all(path.exists(p) and path.getmtime(p) >= srcTime for p in (rough, metal)):
			return rough, metal

		arr = readDdsRgba(ddsPath)

		if arr is None:
			img = Image.open(ddsPath).convert("RGBA")
			arr = np.asarray(img)
			img.close()

		roughArr, metalArr = glossMetalMaps(arr, path.basename(base))
		Image.fromarray(np.ascontiguousarray(roughArr), "L").save(rough, compress_level=1)
		Image.fromarray(np.ascontiguousarray(metalArr), "L").save(metal, compress_level=1)

		return rough, metal
	except Exception as e:
		print(f"[WARNING] Could not build the gloss / metal maps of {ddsPath} for the OBJ: {e}")
		return None

def hasGlossMetal(normalName, cls) -> bool:
	"""Does a material get gloss / metal pictures from its normal map? Only a game-layout *_n texture holds them, and glass keeps its own look."""
	if not normalName or "glass" in str(cls or "").lower():
		return False

	name = str(normalName).split("*")[0].lower()
	if name.endswith(".dds"):
		name = name[:-4]

	return name.endswith("_n")

# [obj-glass] Look of the canopy / glass materials in the OBJ (the GLB has its own, see gltf_conv.glass_look).
OBJ_GLASS_TINT = (0.93, 0.96, 1.0)   # Kd when the material sets no colour of its own: almost clear, a hint of blue
OBJ_GLASS_ALPHA_SCALE = 0.6          # game opacity * this = d (lower = more see-through)
OBJ_GLASS_ALPHA_MIN = 0.04           # never fully invisible (coloured glass)
OBJ_GLASS_CLEAR_ALPHA = 0.025        # d of "clear" glass (canopy, light colour): the value that looks right in Blender
OBJ_GLASS_CLEAR_MIN = 0.55           # clear = darkest colour channel at least this; coloured lenses keep the scaled game opacity
OBJ_GLASS_NS = 900.0                 # shininess (Blender roughness ~ 1 - sqrt(Ns/1000))

# [multi-camo] ------------------------------------------------------------------------------------------------
# One diffuse (t0) can be painted with several different camos (e.g. the Verdun hull uses
# camo_flatgullgrey / camo_honedgrey / camo_red / camo_black_a26f over the SAME destroyer_..._c texture).
# So the baked result is stored once per (diffuse, camo) pair as "<diffuse>__<camo>.dds" and every
# material's map_Kd points at its own pair. The original <diffuse>.dds is never overwritten.
CAMO_CLASS_KEYS = ("tank", "ground", "ship", "boat", "dynamic_masked", "dynamic_painted", "dynamic_camo")
CAMO_CLASS_EXCLUDED = ("aircraft", "cockpit", "building")

# [skin preview] ------------------------------------------------------------------------------------------------
# While a custom-skin preview is exported, SKIN_OVERRIDES maps a game texture name (lower case) to the picture the skin
# supplies for it. exportTexture() then writes that picture instead of extracting the game's texture, and getCamoPair()
# reports no camo for an overridden diffuse (the skin already is the finished picture), so neither the OBJ nor the GLB
# bakes it and both point at the plain <texture>.dds. Textures the skin does not supply go the normal way.
SKIN_OVERRIDES = {}
SKIN_USED = set()          # overridden names actually written during this export
SKIN_GLTF = [False]        # GLB export: build each skin normal's <n>_gltf.png right away, so a failure is reported
_skinWritten = set()

def setSkinOverrides(mapping: dict, gltf: bool = False):
	SKIN_GLTF[0] = bool(gltf)
	SKIN_OVERRIDES.clear()
	SKIN_USED.clear()
	_skinWritten.clear()
	SKIN_OVERRIDES.update({str(k).lower(): str(v) for k, v in (mapping or {}).items()})

def clearSkinOverrides():
	SKIN_OVERRIDES.clear()
	_skinWritten.clear()

def skinOverride(name):
	if not SKIN_OVERRIDES or not name:
		return None
	return SKIN_OVERRIDES.get(str(name).split("*")[0].lower())

def _loadSkinRgba(src: str):
	"""A skin picture (.tga/.png/.dds/...) as an (h, w, 4) uint8 RGBA ndarray."""
	import numpy as np

	if str(src).lower().endswith(".dds"):
		tmp = tempfile.mkdtemp(prefix="wtskin_")

		try:
			arr = _loadDdsArray(str(src), tmp)
		finally:
			shutil.rmtree(tmp, ignore_errors=True)

		if arr is None:
			raise RuntimeError(f"could not decode {path.basename(src)}")

		return np.array(arr, dtype=np.uint8)

	with Image.open(src) as im:
		return np.array(im.convert("RGBA"), dtype=np.uint8)

def isCamoClass(cls) -> bool:
	cls = str(cls).lower()
	return any(k in cls for k in CAMO_CLASS_KEYS) and not any(x in cls for x in CAMO_CLASS_EXCLUDED)

def getCamoPair(material):
	"""(diffuse, camo) as clean texture names; camo is None when the material has no camo texture."""
	diffuse = material.diffuse

	if diffuse is None:
		return None, None

	diffuse = str(diffuse).split("*")[0]
	camo = None

	if skinOverride(diffuse) is not None:
		return diffuse, None      # the skin supplies the finished picture: no camo is baked into it

	if isCamoClass(material.cls):
		for idx, slot in enumerate(material.getTextureSlots()):
			if idx > 0 and slot and "camo" in str(slot).lower():
				camo = str(slot).split("*")[0]
				break

	return diffuse, camo

def bakedDiffuseName(diffuse: str, camo: str = None) -> str:
	return f"{diffuse}__{camo}" if camo else diffuse

def _findTexconv() -> str:
	try:
		from util.misc import ROOT_FOLDER
		candidate = path.join(ROOT_FOLDER, "texconv.exe")

		if path.exists(candidate):
			return candidate
	except Exception:
		pass

	return "texconv.exe"

def _loadDdsImage(ddsPath: str, tmpDir: str):
	"""Opens a DDS as RGBA, via Pillow first and texconv->PNG as fallback. Temp files stay in tmpDir."""
	try:
		img = Image.open(ddsPath)
		img.load()
		return img.convert("RGBA")
	except Exception:
		pass

	try:
		subprocess.run([_findTexconv(), "-y", "-ft", "png", "-o", tmpDir, ddsPath],
					   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		png = path.join(tmpDir, path.splitext(path.basename(ddsPath))[0] + ".png")

		if path.exists(png):
			img = Image.open(png)
			img.load()
			return img.convert("RGBA")
	except Exception as e:
		print(f"[WARNING] Could not read {ddsPath}: {e}")

	return None

def readDdsRgba(ddsPath: str):
	"""Fast reader for UNCOMPRESSED 24/32-bit DDS files (what this exporter writes) -> (h, w, 4) uint8 RGBA.
	Pillow decodes these very slowly on big textures (30s+ for 4096x4096). Returns None for anything else."""
	import numpy as np

	try:
		with open(ddsPath, "rb") as f:
			head = f.read(128)

			if len(head) < 128 or head[:4] != b"DDS ":
				return None

			h, w = unpack("<II", head[12:20])
			pfFlags, fourcc, bits, rm, gm, bm, am = unpack("<I4sIIIII", head[80:108])

			if fourcc == b"DX10" or not (pfFlags & 0x40) or bits not in (24, 32):
				return None  # compressed / DX10 / palettised -> let Pillow handle it

			bpp = bits // 8
			raw = f.read(w * h * bpp)

		if len(raw) < w * h * bpp:
			return None

		arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, bpp)

		def chan(mask):
			if mask == 0:
				return None
			idx = (mask.bit_length() - 1) // 8
			return arr[..., idx] if idx < bpp and mask in (0xFF << (8 * idx),) else False

		r, g, b, a = chan(rm), chan(gm), chan(bm), chan(am)

		if r is None or g is None or b is None or r is False or g is False or b is False or a is False:
			return None

		out = np.empty((h, w, 4), dtype=np.uint8)
		out[..., 0], out[..., 1], out[..., 2] = r, g, b
		out[..., 3] = 255 if a is None else a
		return out
	except Exception:
		return None

def _loadDdsArray(ddsPath: str, tmpDir: str):
	"""DDS -> (h, w, 4) uint8 RGBA ndarray: fast reader, then Pillow/texconv through _loadDdsImage."""
	import numpy as np

	arr = readDdsRgba(ddsPath)

	if arr is not None:
		return arr

	img = _loadDdsImage(ddsPath, tmpDir)
	return np.asarray(img) if img is not None else None

def bakeCamoPairs(texturesDir: str, pairs: dict):
	"""pairs = {bakedName: (diffuse, camo)}. Writes textures/<bakedName>.dds for every pair."""
	import numpy as np

	texconv = _findTexconv()
	hasTexconv = path.exists(texconv) or shutil.which(texconv) is not None

	with tempfile.TemporaryDirectory() as tmp:  # temp PNGs live here, never in textures/
		baseCache = {}  # diffuse -> (rgb ndarray, alpha ndarray) - the same diffuse is reused by every camo
		camoCache = {}  # camo -> rgb ndarray
		tileCache = {}  # (camo, w, h) -> tiled rgb ndarray

		for bakedName, (diffuse, camo) in pairs.items():
			baseDds = path.join(texturesDir, diffuse + ".dds")
			camoDds = path.join(texturesDir, camo + ".dds")

			if not path.exists(baseDds) or not path.exists(camoDds):
				print(f"[WARNING] Camo bake skipped for {bakedName}: missing {diffuse}.dds or {camo}.dds")
				continue

			if diffuse not in baseCache:
				arr = _loadDdsArray(baseDds, tmp)
				baseCache[diffuse] = None if arr is None else (arr[..., :3], arr[..., 3])

			if camo not in camoCache:
				arr = _loadDdsArray(camoDds, tmp)
				camoCache[camo] = None if arr is None else np.ascontiguousarray(arr[..., :3])

			base = baseCache[diffuse]
			camoArr = camoCache[camo]

			if base is None or camoArr is None:
				print(f"[WARNING] Camo bake skipped for {bakedName}: could not decode textures")
				continue

			baseRgb, alpha = base
			h, w = alpha.shape
			ch, cw = camoArr.shape[:2]

			# Tile the camo pattern across the base texture resolution (one np.tile, no per-tile paste loop)
			key = (camo, w, h)

			if key not in tileCache:
				reps = (-(-h // ch), -(-w // cw), 1)
				tileCache[key] = np.tile(camoArr, reps)[:h, :w]

			tiled = tileCache[key]

			# The diffuse's ALPHA is the paint mask (white = painted hull/superstructure, black = bare metal/details).
			# Painted areas take the camo colour, unpainted areas keep the base colour (rust, grime, waterline growth).
			a = alpha[..., None].astype(np.uint16)
			out = ((tiled.astype(np.uint16) * a + baseRgb.astype(np.uint16) * (255 - a) + 127) // 255).astype(np.uint8)
			composite = Image.fromarray(out, "RGB")

			outDds = path.join(texturesDir, bakedName + ".dds")

			if path.exists(outDds):
				try:
					os.remove(outDds)  # never keep a stale bake if the re-encode below fails
				except OSError:
					pass

			# Re-encode to DDS: texconv when it is really available, otherwise straight through Pillow
			if hasTexconv:
				try:
					outPng = path.join(tmp, bakedName + ".png")
					composite.save(outPng)
					subprocess.run([texconv, "-y", "-ft", "dds", "-o", texturesDir, outPng],
								   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
				except Exception:
					pass

			if not path.exists(outDds):
				composite.save(outDds, format="DDS")

			print(f"[INFO] Baked {bakedName}.dds")

class MaterialTemplateLibrary(Terminable):
	class Material(Terminable):
		def __repr__(self):
			return f"newmtl {self.material.getName()}\n{self.getFormattedParams()}"

		def __init__(self, material:MaterialData):
			self.material = material
			self.params = {}

			self.setParam("illum", 2)
			self.setParam("Ns", 32.0)

			matParams = material.getParams()

			if "glass" in str(getattr(material, "cls", "") or "").lower():
				# Canopy / glass: always write an explicit alpha (a missing "opacity" left it opaque white in Blender),
				# a light colour, and a shiny surface.
				try:
					opacity = float(matParams.get("opacity", 0.5))
				except (TypeError, ValueError):
					opacity = 0.5
				diff = tuple(getattr(material, "diff", None) or (0, 0, 0, 1))
				kd = diff[:3] if sum(diff[:3]) > 1e-6 else OBJ_GLASS_TINT   # coloured lenses (nav lights) keep their colour
				if min(kd) >= OBJ_GLASS_CLEAR_MIN:
					alpha = OBJ_GLASS_CLEAR_ALPHA
				else:
					alpha = max(opacity * OBJ_GLASS_ALPHA_SCALE, OBJ_GLASS_ALPHA_MIN)
				self.setParam("Kd", f"{kd[0]:.3f} {kd[1]:.3f} {kd[2]:.3f}")
				self.setParam("d", f"{alpha:.3f}")
				self.setParam("Tr", f"{1.0 - alpha:.3f}")
				self.setParam("Ns", OBJ_GLASS_NS)
			elif "opacity" in matParams:
				self.setParam("d", matParams["opacity"])

			# Each material points at ITS OWN baked diffuse: <diffuse>__<camo>.dds (or plain <diffuse>.dds, no camo)
			diffuse_name, camo_name = getCamoPair(material)

			if diffuse_name is not None:
				self.setParam("map_Kd", "textures/" + bakedDiffuseName(diffuse_name, camo_name) + ".dds")

			if material.normal is not None:
				norm_tex = str(material.normal).split("*")[0]
				# OBJ: the decoded RGB normal map <n>_obj.dds written by decodeNormalForObj(). Only the GLB uses
				# <n>_gltf.png, which gltf_conv.make_gltf_normal() builds itself.
				if norm_tex.lower().endswith(".dds"):
					norm_tex = norm_tex[:-4]
				self.setParam("map_bump", "textures/" + norm_tex + "_obj.dds")

				# Gloss (red) and metal (blue) of the same _n: Blender reads map_Pr / map_Pm. Written by decodeGlossMetalForObj().
				if hasGlossMetal(norm_tex, getattr(material, "cls", None)):
					self.setParam("map_Pr", "textures/" + norm_tex + "_rough.png")
					self.setParam("map_Pm", "textures/" + norm_tex + "_metal.png")
		
		def setParam(self, name, value):
			if value == None:
				return
			
			self.params[name] = value
		
		def getFormattedParams(self):
			formatted = ""

			for k in SafeIter(self, self.params):
				formatted += f"\t{k} {self.params[k]}\n"

			return formatted

		def __exportDiffuse__(self, outpath: str, forceConvert: bool):
			for slot in self.material.getTextureSlots():
				if slot:
					clean_name = str(slot).split("*")[0]
					if clean_name and not self.material.skipsCamoTexture(clean_name):
						try:
							self.material.exportTexture(
								clean_name, outpath, forceConvert=forceConvert
							)
						except Exception as e:
							print(f"[WARNING] Failed to export texture slot {clean_name}: {e}")
		
		def __exportNormal__(self, outpath:str, forceConvert:bool):
			self.material.exportTexture(self.material.normal, outpath, forceConvert = forceConvert)

		def exportTextures(self, outpath: str = getcwd(), forceConvert: bool = False):
			self.__exportDiffuse__(outpath, forceConvert)
			if self.material.normal is not None:
				self.__exportNormal__(outpath, forceConvert)

	def __init__(self, materials:list[MaterialData]):
		self.__mats = tuple(self.Material(material) for material in SafeIter(self, materials))
	
	def getMTL(self):
		mtl = ""

		for v in SafeIter(self, self.__mats):
			mtl += f"{v}"

		return mtl
	
	def exportTextures(self, outpath: str = getcwd(), forceConvert: bool = False):
		log.log("Exporting MTL textures")
		log.addLevel()

		# [REMOVED in cleanup] `has_dynamic_camo = False`  (material.py line 1354 of the pre-cleanup file, start of
		#   MaterialTemplateLibrary.exportTextures). Initial value of the per-model camo flag described in the note inside the loop below.

		for mat in SafeIter(self, self.__mats):
			mat: MaterialTemplateLibrary.Material

			log.log(f"Exporting textures from {mat.material.getName()}")
			log.addLevel()

			mat.exportTextures(outpath, forceConvert)

			# [REMOVED in cleanup] per-material "does this model use dynamic camo?" gate  (material.py lines 1364-1369 of the pre-cleanup
			#   file, inside MaterialTemplateLibrary.exportTextures, in the per-material loop right after mat.exportTextures()).
			#   What it did: lower-cased mat.material.cls and set `has_dynamic_camo = True` when the class contained one of
			#   "tank", "ground", "ship", "boat", "dynamic_masked", "dynamic_painted" and none of "aircraft", "cockpit", "building".
			#   Worked with: the `has_dynamic_camo` flag (below, removed too), which the original bake step used to decide whether to
			#   bake at all. The same lists now live in CAMO_CLASS_KEYS / CAMO_CLASS_EXCLUDED and isCamoClass(), and the bake
			#   step collects (diffuse, camo) pairs through getCamoPair(), so the flag was written but never read.

			log.subLevel()

		# OBJ normal maps: decode the packed <n>.dds into <n>_obj.dds (what map_bump points at). The GLB export builds
		# its own png (gltf_conv.make_gltf_normal) and never goes through this method.
		for mat in SafeIter(self, self.__mats):
			if mat.material.normal is not None:
				normName = str(mat.material.normal).split("*")[0]
				if normName.lower().endswith(".dds"):
					normName = normName[:-4]
				decodeNormalForObj(path.join(outpath, "textures", f"{normName}.dds"))

				if hasGlossMetal(normName, getattr(mat.material, "cls", None)):
					decodeGlossMetalForObj(path.join(outpath, "textures", f"{normName}.dds"))

		# Collect every distinct (diffuse, camo) pair used by the model's materials
		pairs = {}

		for mat in SafeIter(self, self.__mats):
			diffuse, camo = getCamoPair(mat.material)

			if diffuse and camo:
				pairs[bakedDiffuseName(diffuse, camo)] = (diffuse, camo)

		if not pairs:
			log.log("Model does not use dynamic camo; skipping camouflage baking step.")
			log.subLevel()
			return

		log.log(f"Baking {len(pairs)} diffuse/camo combination(s)")

		try:
			bakeCamoPairs(path.join(outpath, "textures"), pairs)
		except Exception as e:
			print(f"[WARNING] Automated baking routine encountered an issue: {e}")

		log.subLevel()

def computeMaterialNames(mats:list[MaterialData], parent:Terminable = None):
	log.log("Computing material names")
	log.addLevel()
	
	enumerateIt = (lambda x: SafeEnumerate(parent, x)) if parent is not None else (lambda x: enumerate(x))
	iterIt = (lambda x: SafeIter(parent, x)) if parent is not None else (lambda x: iter(x))
	
	for k, mat in enumerateIt(mats):
		mat:MaterialData

		log.log(f"Materials #{k}: {mat.getName()}")

		cnt = 1

		for mat2 in iterIt(mats):
			mat2:MaterialData
			
			if mat is mat2:
				continue
			
			if mat.getName() == mat2.getName():
				# MaterialData.__eq__ only compares the texture slots, so e.g. the canopy and the coloured lenses (all
				# "dynamic_glass_chrome", no textures) counted as the same material and shared one name; Blender then merges
				# the same-named materials into one. Equal means same slots AND same shader class, colour and parameters.
				if mat == mat2 and mat.cls == mat2.cls and mat.diff == mat2.diff and mat.par == mat2.par:
					continue
				else:
					mat2.setName(mat2.getName() + f"_{cnt}")
					cnt += 1

	log.subLevel()

if __name__ == "__main__":
	from gameres import GameResDesc

	dxp = DDSxTexturePack2("C:\\Program Files (x86)\\Steam\\steamapps\\common\\War Thunder\\content\\base\\res\\cars_ri.dxp.bin")
	
	for ddsx in dxp.getAllDDSx():
		if ddsx != False:
			AssetCacher.cacheAsset(ddsx)

	desc = GameResDesc("C:\\Program Files (x86)\\Steam\\steamapps\\common\\War Thunder\\content\\base\\res\\riDesc.bin")
	
	materials = desc.getModelMaterials("dodge_wf32")

	mtl = MaterialTemplateLibrary(materials)
	print(mtl.getMTL())
	mtl.exportTextures()