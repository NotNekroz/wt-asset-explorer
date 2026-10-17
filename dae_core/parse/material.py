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

DYNAMIC_CLASSES = (
    "dynamic_masked_ship",
    "dynamic_painted_by_mask",
    "dynamic_masked",
    "dynamic_alpha_blend",
    "dynamic_camo",
)

import subprocess
import tempfile

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

		return "_".join(components)

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
	
	def exportTexture(self, texture:str, output:str = getcwd(), forceConvert:bool = False, texturePaths:TexturePathDict = None, texN:str = None):
		outpath = path.join(output, "textures")

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

			if "opacity" in matParams:
				self.setParam("d", matParams["opacity"])

			# Inspect texture slots directly to find diffuse (t0) and camo (t1 / camo textures)
			slots = material.getTextureSlots()
			diffuse_tex = material.diffuse
			camo_tex = None

			for idx, slot in enumerate(slots):
				if slot:
					slot_str = str(slot).lower()
					if idx > 0 and ("camo" in slot_str or idx == 1):
						camo_tex = slot
						break

			# Fallback to matParams if needed
			if not camo_tex:
				for param_key in ("camo_tex", "t1", "t0", "t2"):
					val = matParams.get(param_key)
					if val:
						camo_tex = val
						break

			# Point directly to the standard diffuse filename (e.g., leclerc_s2_body_c.dds), which will be overwritten by the baked version
			final_tex_name = diffuse_tex

			if final_tex_name is not None:
				clean_tex = str(final_tex_name).split("*")[0]
				if not clean_tex.endswith(".dds"):
					clean_tex += ".dds"
				self.setParam("map_Kd", "textures/" + clean_tex)

			if material.normal is not None:
				norm_tex = str(material.normal).split("*")[0]
				if not norm_tex.endswith(".dds"):
					norm_tex += ".dds"
				self.setParam("map_bump", "textures/" + norm_tex)
		
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
					if clean_name:
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

		has_dynamic_camo = False

		for mat in SafeIter(self, self.__mats):
			mat: MaterialTemplateLibrary.Material

			log.log(f"Exporting textures from {mat.material.getName()}")
			log.addLevel()

			mat.exportTextures(outpath, forceConvert)

			# Gate execution: Enable dynamic camo baking for ground vehicles, tanks, and ships
			cls_name = str(mat.material.cls).lower()
			if any(k in cls_name for k in ("tank", "ground", "ship", "boat", "dynamic_masked", "dynamic_painted")):
				if not any(excluded in cls_name for excluded in ("aircraft", "cockpit", "building")):
					has_dynamic_camo = True

			log.subLevel()

		# Skip dynamic camo baking if model does not use dynamic camo
		if not has_dynamic_camo:
			log.log("Model does not use dynamic camo; skipping camouflage baking step.")
			log.subLevel()
			return

		# Build a mapping of diffuse texture to its paired camo texture from material parameters
		diffuse_to_camo = {}
		for mat in SafeIter(self, self.__mats):
			params = mat.material.getParams()
			t0 = params.get("t0") or mat.material.diffuse
			t1 = params.get("t1") or params.get("camo_tex")
			if not t1:
				for s in mat.material.getTextureSlots()[1:]:
					if s and "camo" in str(s).lower():
						t1 = s
						break
			if t0 and t1:
				t0_clean = str(t0).split("*")[0].lower()
				t1_clean = str(t1).split("*")[0].lower()
				if not t0_clean.endswith(".dds"):
					t0_clean += ".dds"
				if not t1_clean.endswith(".dds"):
					t1_clean += ".dds"
				diffuse_to_camo[t0_clean] = t1_clean

		textures_dir = path.join(outpath, "textures")
		if path.exists(textures_dir):
			try:
				camo_files = [f for f in os.listdir(textures_dir) if "camo" in f.lower() and f.lower().endswith('.dds')]
				
				if camo_files:
					from PIL import Image, ImageChops
					import subprocess
					from util.misc import ROOT_FOLDER
					
					texconv_path = path.join(ROOT_FOLDER, "texconv.exe")
					if not path.exists(texconv_path):
						texconv_path = "texconv.exe"

					# Target base diffuse textures that have camo definitions, or all _c.dds if no explicit mapping
					if diffuse_to_camo:
						base_textures = [f for f in os.listdir(textures_dir) if f.lower() in diffuse_to_camo]
					else:
						base_textures = [f for f in os.listdir(textures_dir) if f.endswith('_c.dds') and "camo" not in f.lower()]

					for base_f in base_textures:
						base_dds = path.join(textures_dir, base_f)
						
						# Convert base DDS to PNG for Pillow manipulation
						subprocess.run([texconv_path, "-y", "-ft", "png", "-o", textures_dir, base_dds], stdout=subprocess.DEVNULL)
						base_png = path.splitext(base_dds)[0] + ".png"
						if not path.exists(base_png):
							continue

						# Pick the matching camo texture from material mapping, by name match, or default to the first extracted camo file
						matched_camo = None
						if diffuse_to_camo and base_f.lower() in diffuse_to_camo:
							target_camo = diffuse_to_camo[base_f.lower()]
							for c_file in camo_files:
								if c_file.lower() == target_camo:
									matched_camo = c_file
									break

						if not matched_camo:
							matched_camo = camo_files[0]
							for c_file in camo_files:
								c_stem = path.splitext(c_file)[0].lower()
								# If a specific material camo exists for this texture, pair them
								if c_stem in base_f.lower():
									matched_camo = c_file
									break

						camo_dds = path.join(textures_dir, matched_camo)
						subprocess.run([texconv_path, "-y", "-ft", "png", "-o", textures_dir, camo_dds], stdout=subprocess.DEVNULL)
						camo_png = path.splitext(camo_dds)[0] + ".png"

						if path.exists(camo_png):
							base_img = Image.open(base_png).convert("RGBA")
							camo_img = Image.open(camo_png).convert("RGB")

							r, g, b, a = base_img.split()
							base_rgb = Image.merge("RGB", (r, g, b))

							w, h = base_img.size
							cw, ch = camo_img.size
							
							# Tile the micro-detail camo pattern across the base texture resolution
							tiled_camo = Image.new("RGB", (w, h))
							for x in range(0, w, cw):
								for y in range(0, h, ch):
									tiled_camo.paste(camo_img, (x, y))

							# Blend camo layer into the base diffuse texture
							try:
								composite = ImageChops.soft_light(base_rgb, tiled_camo)
							except Exception:
								composite = Image.composite(tiled_camo, base_rgb, a)

							composite.save(base_png)
							
							# Re-encode composite PNG back to DDS format
							subprocess.run([texconv_path, "-y", "-ft", "dds", "-o", textures_dir, base_png], stdout=subprocess.DEVNULL)

							base_img.close()
							camo_img.close()
							composite.close()
							
							if path.exists(base_png):
								os.remove(base_png)
							if path.exists(camo_png):
								os.remove(camo_png)

					print("[INFO] Camouflage baking completed successfully.")
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
				if mat == mat2:
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