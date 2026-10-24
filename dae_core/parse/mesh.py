import sys
from os import path, getcwd

sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

import util.log as log
from struct import unpack
from util.fileread import *
from util.terminable import SafeRange, Terminable, FilePathable, SafeIter, SafeEnumerate
from util.enums import *
from parse.material import MaterialData
from abc import ABC, abstractmethod
import os as _os
import numpy as _np

# Vectorised vertex / index-buffer decoding. Same results as the per-item loops below (they stay as the fallback and are
# used for anything unusual). WTX_FAST_MATH=0 switches it off.
FAST_DECODE = _os.environ.get("WTX_FAST_MATH", "1") != "0"


class ShaderMesh(Terminable):
	class Elem:
		def __repr__(self):
			return f"<ShaderMesh::RElem e={self.ShaderElementPtr} mat={self.mat} vData={self.vData} vdOrderIndex={self.vdOrderIndex} startV={self.startV} numV={self.numV} startI={self.startI} numFace={self.numFace} baseVertex={self.baseVertex}>"
		
		def __init__(self, file:BinFile):
			self.ShaderElementPtr = readLong(file)
			self.mat = readLong(file)
			self.vData = readLong(file)

			self.vdOrderIndex = readInt(file)
			self.startV = readInt(file)
			self.numV = readInt(file)
			self.startI = readInt(file)
			self.numFace = readInt(file)
			self.baseVertex = readInt(file)

			log.log(self)

	def __repr__(self):
		return f"stage={self.stageEndElemIdx} maxMatPass={self._deprecatedMaxMatPass} resv={self._resv} cnt={self.cnt}"

	def __init__(self, file:BinFile):
		ofs = readInt(file)
		cnt = readInt(file)

		file.seek(8, 1)

		self.stageEndElemIdx = list(readShort(file) for _ in SafeRange(self, 8))
		
		self._deprecatedMaxMatPass = readEx(4, file, True)
		self._resv = readInt(file)

		if self._deprecatedMaxMatPass < 0: # ShaderMesh::patchData
			self._deprecatedMaxMatPass &= 0x7FFFFFFF
		elif self.stageEndElemIdx[2] != 0:
			old = self.stageEndElemIdx[2]

			self.stageEndElemIdx[0] = cnt
			self.stageEndElemIdx[1] = cnt
			self.stageEndElemIdx[2] = cnt
			self.stageEndElemIdx[3] = cnt

			cnt += old

			self.stageEndElemIdx[4] = cnt
			self.stageEndElemIdx[5] = cnt
			self.stageEndElemIdx[6] = cnt
			self.stageEndElemIdx[7] = cnt
		
		self.cnt = cnt

		log.log(self)
		log.addLevel()

		self.elems = tuple(self.Elem(file) for i in SafeRange(self, cnt))
		
		log.subLevel()

class InstShaderMeshResource(Terminable):
	def __init__(self, file:BinFile):
		sz = readInt(file)
		resv = readInt(file)

		assert resv == 0

		self.shaderMesh = self.setSubTask(ShaderMesh(file.readBlock(sz)))

		self.clearSubTask()
	

class MatVData(Terminable, FilePathable): # stores material and vertex data :D	
	def __init__(self, 
	      		file:BinFile, 
				name:str = None, 
				texCnt:int = 0, 
				matCnt:int = 0, 
				filePath:str = None,
				textures:list[str] = None,
				flag:int = MVD_NORMAL):
		FilePathable.__init__(self, filePath, name, file.getSize())

		self.__texCnt = texCnt
		self.__matCnt = matCnt
		self.__file = file
		self.__flag = flag
		self.__textures = textures

		self.__dataComputed = False

	def __repr__(self):
		return f"<MVD {self.name} texCnt={self.__texCnt} matCnt={self.__matCnt} computed={self.__dataComputed}>"

	def save(self, output:str = getcwd()):
		binData = self.__file.read()

		output = path.normpath(f"{output}\\{self.name}.mvd")

		file = open(output, "wb")

		file.write(binData)

		file.close()

		return output

	def computeData(self):
		if self.__dataComputed:
			return

		log.log("Computing MatVData")
		log.addLevel()

		file = self.__file

		file.seek(0, 0)

		matOfs = readInt(file)
		matCnt = readInt(file)

		self.__hasMaterials = matOfs != 0 and matCnt != 0 

		file.seek(8, 1)

		gvdOfs = readInt(file)
		gvdCnt = readInt(file)

		self.__computeMaterials__(matCnt, matOfs)
		self.__computeGVData__(gvdCnt, gvdOfs)

		log.subLevel()

		self.__dataComputed = True
	

	#-------------------------------------------------
	#
	#
	#	Material handling (v1 mvd only?)
	#
	#

	class Material:
		def __repr__(self):
			return f"<MVDMaterial cls={self.shaderClass}>"
		
		def __init__(self):
			self.shaderClass:str = None
			self.textures:tuple[int] = None # 16 elements
			self.data:BinBlock = None # 176 bytes big
			self.diff:tuple[float] = None # 16 elements
			self.amb:tuple[float] = None # 16 elements
			self.spec:tuple[float] = None # 16 elements
			self.emis:tuple[float] = None # 16 elements


	def __processMaterial__(self, ofs:int, idx:int):
		file = self.__file

		file.seek(ofs + idx * 0xA8, 0)

		log.log(f"Material {idx} @ {file.tell()}:")
		log.addLevel()

		diff = unpack("4f", file.read(0x10))
		amb = unpack("4f", file.read(0x10))
		emis = unpack("4f", file.read(0x10))
		spec = unpack("4f", file.read(0x10))

		unknown = readInt(file)

		log.log(f"unknown         = {hex(unknown)}")

		shaderOfs = readInt(file)
		
		file.seek(8, 1)

		textureIds = unpack("I" * 16, file.read(0x40))

		dataOfs = readInt(file)
		unknown2 = readInt(file)

		file.seek(8, 1)

		flags = readLong(file)

		log.log(f"flags           = {hex(flags)}")

		file.seek(shaderOfs, 0)

		shader = b""

		while True:
			b = file.read(1)

			if b == b"\x00":
				break
			
			shader += b

		shader = shader.decode("utf-8")

		log.subLevel()

		mat = MaterialData()
		mat.cls = shader

		# mat.data = BinBlock(file, dataOfs, 0xB0)
		mat.diff = diff
		mat.amb = amb
		mat.spec = spec
		mat.emis = emis
		mat.par = ""

		for slotId, texId in SafeEnumerate(self, textureIds):
			if slotId > 10:
				break

			if texId == 0xFFFFFFFF:
				continue

			if self.__textures is None:
				tex = str(texId)
			else:
				tex = self.__textures[texId]

			mat.addTexSlot(f"t{slotId}", tex)

		return mat

	def __computeMaterials__(self, cnt:int, ofs:int):
		log.log(f"Computing {cnt} materials @ {ofs}")
		log.addLevel()
		# skip if self.__textures is None?
		self.__materials = tuple(self.__processMaterial__(ofs, i) for i in SafeRange(self, cnt))

		log.subLevel()

	def getMaterials(self):
		return self.__materials

	#-------------------------------------------------
	#
	#
	#	Global vertex data handling
	#
	#

	class GlobalVertexData:
		def __init__(self, file:BinBlock, absOfs:int, idx:int, flag:int):
			self.__ofs = absOfs + idx * 0x20

			self.__idx = idx

			self.__vCnt = readInt(file)

			self.__vStride = readByte(file)
			self.__iPackedSz = readEx(3, file)

			self.__iSz = readInt(file)
			
			self.__flags = readShort(file)
			self.__bf_14 = readShort(file)

			self.__iCnt = readInt(file)

			self.__storageFormat = readInt(file) + flag * 10


			# 8 null-bytes remainder

			log.log(f"{self}")

		def __repr__(self):
			return f"<GVData {self.__idx} @ {self.__ofs}: vCnt={self.__vCnt} vStride={self.__vStride} iPackedSz={self.__iPackedSz} iSz={self.__iSz} flags={hex(self.__flags)} bf_14={hex(self.__bf_14)} iCnt={self.__iCnt} storageFormat={self.__storageFormat}>"
	
		def getStorageFormat(self):
			return self.__storageFormat

		def getVertexBlockSz(self):
			return self.__vCnt * self.__vStride
		
		def getFullVertexDataSz(self):
			if self.__iPackedSz == 0:
				return self.getVertexBlockSz() + self.__iSz
			else:
				return self.getVertexBlockSz() + self.__iPackedSz

		def getVertexCnt(self):
			return self.__vCnt

		def getVertexStride(self):
			return self.__vStride

		def getPackedIndicesSz(self):
			return self.__iPackedSz
		
		def getIndicesSz(self):
			return self.__iSz

		@property
		def idx(self):
			return self.__idx


		@property
		def flags(self):
			return self.__flags
		@property
		def bf_14(self):
			return self.__bf_14


	def __computeGVData__(self, cnt:int, ofs:int):
		file = self.__file
		
		file.seek(ofs, 0)

		log.log(f"Computing {cnt} global vertex data @ {ofs}")
		log.addLevel()

		self.__gvdata = tuple(self.GlobalVertexData(file.readBlock(0x20), ofs, i, self.__flag) for i in SafeRange(self, cnt))

		self.__vdStartOfs = ofs + cnt * 0x20

		log.subLevel()


	#-------------------------------------------------
	#
	#
	#	Vertex data decoding
	#
	#
	
	class VertexData(Terminable):
		class __decodeIndexSequence__(): # i haven't cleaned this up yet and i don't think i ever will, who cares anyway
			class __byteArrayGen__():
				def __init__(self, parent:Terminable, data:bytes, offset:int, size:int):
					self.parent = parent
					self.data = data
					self.offset = offset
					self.size = size
					self.done = False

				def __iter__(self):
					return self

				def __next__(self):
					if self.parent.shouldTerminate:
						raise StopIteration()
					else:
						return self.next()

				def next(self):
					if not self.done and self.offset < self.size:
						b = ord(self.data[self.offset:self.offset + 1])

						self.offset += 1
						self.done = (b & 0x80) == 0

						return b
					else:
						raise StopIteration()

			def __init__(self, data:bytes, size:int, mvd):
				self.data = data
				self.size = size
				self.offset = 0
				self.buffer = [0, 0]
				self.count = 0
				# self.lastCur = None
				self.fucking = False
				self.mvd:MatVData = mvd

			def __iter__(self):
				return self

			def __next__(self):
				return self.next()

			def decodeLebOld(self):
				a = bytearray(x for x in self.__byteArrayGen__(self.mvd, self.data, self.offset, self.size))
				sz = len(a)
				self.offset += sz

				decoded = 0

				for i in SafeRange(self.mvd, sz):
					decoded = decoded + ((a[i] & 0x7f) << (i * 7)) # leb128 decoding

				return decoded

			def decodeLeb(self):
				v14 = self.data[self.offset]
				self.offset += 1

				decoded = v14
				if ( v14 >= 0x80 ):
					decoded = v14 & 0x7F
					v16 = 7

					for _ in SafeRange(self.mvd, 4):
						v18 = self.data[self.offset]
						self.offset += 1
						v19 = (v18 & 0x7F) << v16
						v16 += 7
						decoded |= v19
						if ( v18 < 0x80 ):
							break
						
				return decoded

			def getIndex(self):
				decoded = self.decodeLeb()

				current = decoded & 1 # meshopt

				d = (decoded >> 2) ^ -((decoded & 2) != 0) # stolen from gaijin
				index = self.buffer[current] + d

				self.buffer[current] = index

				return index

			def getValidIndex(self):
				if self.offset >= self.size or self.mvd.shouldTerminate:
					raise StopIteration()

				return self.getIndex()

			def next(self):
				a = self.getValidIndex()

				if a == None:
					return self.next()

				b = self.getValidIndex()

				if b == None:
					return self.next()

				c = self.getValidIndex()

				if c == None:
					return self.next()

				return (a, c, b) # reversed order somehow fixes normals???

		def __repr__(self):
			return f"<VertexData {self.__gvData.idx} vCnt={self.__gvData.getVertexCnt()}>"

		def __init__(self, file:BinBlock, gvData):
			self.__verts:tuple[tuple(float, float, float)] = None
			self.__UVs:tuple[tuple(float, float)] = None
			self.__faces:tuple[tuple(int, int, int)] = None
			self.__unimplemented = False

			gvData:MatVData.GlobalVertexData = gvData

			self.__gvData = gvData

			vDataSz = gvData.getVertexBlockSz()

			log.addLevel()

			self.__processVertices__(file.readBlock(vDataSz))
			self.__processFaces__(file.readBlock(file.getSize() - vDataSz))

			log.subLevel()
		
		@property
		def globalVertexData(self):
			return self.__gvData
		
		class PARSECLASS(ABC):
			@abstractmethod
			def read(self, file:BinFile):
				...

		class VERTEX(PARSECLASS):
			...
		
		class UV(PARSECLASS):
			...

		class SHORT_VERTEX(VERTEX):
			def read(self, file:BinFile):
				return [readSignedShort(file) / 32768, readSignedShort(file) / 32768, readSignedShort(file) / 32768]

		class FLOAT_VERTEX(VERTEX):
			def read(self, file:BinFile):
				return list(unpack("fff", file.read(12)))

		class NO_UV(UV):
			def read(self, file:BinFile):
				return (0, 0)

		class SHORT_UV(UV):
			def read(self, file:BinFile):
				return (readSignedShort(file) / 4096, 1 + (-readSignedShort(file) / 4096))

		class FLOAT_UV(UV):
			def read(self, file:BinFile):
				uv = unpack("ff", file.read(8))

				return (uv[0], -uv[1])
		
		class DUAL_SHORT_UV(UV):
			def read(self, file:BinFile):
				uv0 = (readSignedShort(file) / 4096, 1 + (-readSignedShort(file) / 4096))
				uv1 = (readSignedShort(file) / 4096, 1 + (-readSignedShort(file) / 4096))
				return (uv0, uv1)
						
		class HALFFLOAT_UV(UV):
			def read(self, file:BinFile):
				uv = unpack("ee", file.read(4))

				return (uv[0], -uv[1])
		
		class PADDING(PARSECLASS):
			def __init__(self, sz:int):
				self.sz = sz
			
			def read(self, file:BinFile):
				file.seek(self.sz, 1)


		FORMATS:dict[int, dict[int, tuple[PARSECLASS]]] = {
			1:{
				12:(FLOAT_VERTEX(), NO_UV())
			  },
			2:{
				12:(SHORT_VERTEX(), PADDING(2), SHORT_UV()),
				16:(FLOAT_VERTEX(), SHORT_UV())
			  },
			3:{
				16:(SHORT_VERTEX(), PADDING(6), SHORT_UV()),
				20:(FLOAT_VERTEX(), PADDING(4), SHORT_UV()),
				24:(FLOAT_VERTEX(), PADDING(4), FLOAT_UV()),
			  },
			4:{
				20:(PADDING(4), SHORT_VERTEX(), PADDING(6), SHORT_UV()),
				# 24: # tree_02_dstr (wt)
				28:(FLOAT_VERTEX(), PADDING(4), FLOAT_UV(), PADDING(4)),
			  },
			5:{
				24:(PADDING(4), SHORT_VERTEX(), PADDING(6), DUAL_SHORT_UV()),
				28:(FLOAT_VERTEX(), PADDING(12), SHORT_UV()),
				36:(FLOAT_VERTEX(), PADDING(4), FLOAT_UV(), PADDING(12))
			  },
			6:{
				32:(FLOAT_VERTEX(), PADDING(4), SHORT_UV(), PADDING(12)) # UVs?
			  },
			
			13:{
				16:(SHORT_VERTEX(), PADDING(6), SHORT_UV()),
			  },
			15:{
				24:(PADDING(8), SHORT_VERTEX(), PADDING(6), SHORT_UV()),
			 	28:(FLOAT_VERTEX(), PADDING(8), SHORT_UV(), PADDING(4)),
				32:(FLOAT_VERTEX(), PADDING(20), NO_UV())
			  },
			16:{
				28:(PADDING(12), SHORT_VERTEX(), PADDING(6), SHORT_UV()),
			 	32:(PADDING(4), FLOAT_VERTEX(), PADDING(4), SHORT_UV(), PADDING(8)),
				36:(FLOAT_VERTEX(), PADDING(14), SHORT_UV(), PADDING(6))
			  },
			17:{
				32:(NO_UV(), PADDING(8), SHORT_VERTEX(), PADDING(18)),
				36:(FLOAT_VERTEX(), PADDING(8), SHORT_UV(), PADDING(12))
			  },
			25:{
				24:(SHORT_VERTEX(), PADDING(6), SHORT_UV(), PADDING(8)),
				28:(FLOAT_VERTEX(), PADDING(4), SHORT_UV(), PADDING(8)),
				32:(FLOAT_VERTEX(), PADDING(4), FLOAT_UV(), PADDING(8))
			  },
			26:{
				32:(PADDING(4), FLOAT_VERTEX(), PADDING(4), SHORT_UV(), PADDING(8))
			},
			27:{
				36:(FLOAT_VERTEX(), PADDING(4), SHORT_UV(), PADDING(16))
			},
		}

		def getParser(self, format:int, vStride:int):
			if format in self.FORMATS:
				return self.FORMATS[format].get(vStride)
			else:
				return None

		# ---- fast paths (numpy). Each returns None when it is not sure it can reproduce the loop exactly; the caller then runs
		# ---- the original loop. The names end in "__" so Python does not name-mangle them.
		def __fastVertices__(self, file:BinBlock, parser, vCnt:int):
			try:
				with _np.errstate(all = "ignore"):
					return self.__fastVerticesImpl__(file, parser, vCnt)
			except Exception:
				return None

		def __fastVerticesImpl__(self, file:BinBlock, parser, vCnt:int):
			VD = MatVData.VertexData

			if vCnt <= 0 or self.shouldTerminate:
				return None

			sizes = {VD.SHORT_VERTEX: 6, VD.FLOAT_VERTEX: 12, VD.NO_UV: 0, VD.SHORT_UV: 4, VD.FLOAT_UV: 8, VD.DUAL_SHORT_UV: 8, VD.HALFFLOAT_UV: 4}
			vert = uv = None
			ofs = 0

			for c in parser:
				t = type(c)

				if t is VD.PADDING:
					if type(c.sz) is not int or c.sz < 0:
						return None
					ofs += c.sz
				elif t is VD.SHORT_VERTEX or t is VD.FLOAT_VERTEX:
					if vert is not None:
						return None
					vert = (t, ofs)
					ofs += sizes[t]
				elif t in sizes:
					if uv is not None:
						return None
					uv = (t, ofs)
					ofs += sizes[t]
				else:
					return None

			if vert is None or uv is None:
				return None

			row = ofs
			need = vCnt * row

			if file.getSize() - file.tell() < need:
				return None

			start = file.getAbsOffset() + file.tell()
			raw = file.getData()[start:start + need]

			if len(raw) != need:
				return None

			rows = _np.frombuffer(raw, dtype = _np.uint8).reshape(vCnt, row)

			def field(dt, ofs, n):
				w = _np.dtype(dt).itemsize * n
				return _np.ascontiguousarray(rows[:, ofs:ofs + w]).view(dt).reshape(vCnt, n)

			if vert[0] is VD.SHORT_VERTEX:
				verts = (field("<i2", vert[1], 3).astype(_np.float64) / 32768).tolist()
			else:
				verts = field("<f4", vert[1], 3).astype(_np.float64).tolist()

			ut, uo = uv
			UVs1 = [None] * vCnt

			if ut is VD.NO_UV:
				UVs0 = [(0, 0)] * vCnt
			elif ut is VD.SHORT_UV:
				a = field("<i2", uo, 2).astype(_np.float64)
				UVs0 = list(zip((a[:, 0] / 4096).tolist(), (1 + (-a[:, 1] / 4096)).tolist()))
			elif ut is VD.DUAL_SHORT_UV:
				a = field("<i2", uo, 4).astype(_np.float64)
				UVs0 = list(zip((a[:, 0] / 4096).tolist(), (1 + (-a[:, 1] / 4096)).tolist()))
				UVs1 = list(zip((a[:, 2] / 4096).tolist(), (1 + (-a[:, 3] / 4096)).tolist()))
			else:
				a = field("<f4" if ut is VD.FLOAT_UV else "<f2", uo, 2).astype(_np.float64)
				UVs0 = list(zip(a[:, 0].tolist(), (-a[:, 1]).tolist()))

			file.seek(need, 1)

			return verts, UVs0, UVs1

		def __fastUnpackedFaces__(self, file:BinBlock, count:int):
			try:
				if count <= 0 or self.shouldTerminate or file.getSize() - file.tell() < count * 6:
					return None

				a = _np.frombuffer(file.read(count * 6), dtype = "<u2").reshape(count, 3)

				return tuple(map(tuple, a.tolist()))
			except Exception:
				return None

		def __fastPackedFaces__(self, data:bytes, size:int):
			try:
				if size <= 0 or len(data) != size or self.shouldTerminate:
					return None

				b = _np.frombuffer(data, dtype = _np.uint8)

				if b[-1] >= 0x80:      # an unfinished number at the end: the loop raises there, let it
					return None

				ends = _np.flatnonzero(b < 0x80)
				starts = _np.concatenate(([0], ends[:-1] + 1))
				lens = ends - starts + 1

				if lens.max() > 5:     # the loop stops a number after 5 bytes and carries on inside it; not reproduced here
					return None

				pos = _np.arange(len(b)) - _np.repeat(starts, lens)
				vals = _np.add.reduceat((b & 0x7f).astype(_np.int64) << (7 * pos), starts)

				n = (len(vals) // 3) * 3
				vals = vals[:n]       # a last incomplete triple is dropped, like the loop does

				stream = vals & 1
				delta = vals >> 2
				delta = _np.where((vals & 2) != 0, ~delta, delta)
				idx = _np.zeros(n, dtype = _np.int64)

				for s in (0, 1):
					m = stream == s
					idx[m] = _np.cumsum(delta[m])

				return tuple(map(tuple, idx.reshape(-1, 3)[:, (0, 2, 1)].tolist()))     # (a, c, b) like the loop
			except Exception:
				return None

		def __processVertices__(self, file:BinBlock):
				format = self.__gvData.getStorageFormat()
				vStride = self.__gvData.getVertexStride()
				vCnt = self.__gvData.getVertexCnt()

				verts:list[list[float, float, float]] = [None] * vCnt
				UVs0:list[list[float, float]] = [None] * vCnt
				UVs1:list[list[float, float]] = [None] * vCnt

				parser = self.getParser(format, vStride)

				self.__autoRaw = None

				if format == 17 and vStride == 32:
					# Skinned meshes of this layout (the nets of the M1128 and similar): where the position and the UV sit inside the 32 bytes
					# is not known for certain. Decode with the table's layout now (so nothing is lost if the check fails) and let
					# __autoLayout__ compare the candidates against the mesh's own triangles once the faces are decoded.
					self.__autoRaw = file.read(vCnt * vStride)
					file.seek(0, 0)

				if parser is None:
					raise Exception(f"Unimplemented storage format {format}-{vStride}")
				else:
					log.log(f"Processing {vCnt} vertices vFormat={format} vStride={vStride}")

					fast = self.__fastVertices__(file, parser, vCnt) if FAST_DECODE else None

					if fast is not None:
						verts, UVs0, UVs1 = fast

					for i in (SafeRange(self, vCnt) if fast is None else ()):
						for parseClass in SafeIter(self, parser):
							parseClass:MatVData.VertexData.PARSECLASS

							if isinstance(parseClass, MatVData.VertexData.PADDING):
								parseClass.read(file)
							elif isinstance(parseClass, MatVData.VertexData.VERTEX):
								verts[i] = parseClass.read(file)
							elif isinstance(parseClass, MatVData.VertexData.UV):
								res = parseClass.read(file)
								if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], tuple):
									UVs0[i] = res[0]
									UVs1[i] = res[1]
								else:
									# Only assign single streams to UVs0, leaving UVs1 unassigned 
									# to prevent fallback duplication from overriding the base unwrap
									UVs0[i] = res

				self.__verts = verts
				self.__UVs = UVs0
				self.__UVs1 = UVs1
			
		def __processFaces__(self, file:BinBlock):
			if self.__unimplemented:
				log.log("Unimplemented vertex format, skipping indice buffer", LOG_WARN)
				
				return
			
			log.log(f"Processing faces @ {self.__gvData.getVertexBlockSz()}")
			log.addLevel()

			pSz = self.__gvData.getPackedIndicesSz()
			sz = self.__gvData.getIndicesSz()

			if pSz == 0:
				log.log("Processing unpacked faces")

				# file.seek(1, 1)

				count = (sz // 6)  - sz % 6

				faces = self.__fastUnpackedFaces__(file, count) if FAST_DECODE else None

				if faces is None:
					faces = tuple(unpack("HHH", file.read(6)) for i in SafeRange(self, count))

			else:
				log.log("Processing packed faces")

				file.seek(1, 1)
				
				encodedSize = pSz - 0x5
				encoded = file.read(encodedSize)
				faces = self.__fastPackedFaces__(encoded, encodedSize) if FAST_DECODE else None

				if faces is None:
					faces = tuple(f for f in self.__decodeIndexSequence__(encoded, encodedSize, self))

			log.log(f"Processed {len(faces)} faces")
			log.subLevel()

			self.__faces = faces

			if self.__autoRaw is not None:
				try:
					self.__autoLayout__()
				except Exception as e:    # never lose the mesh over this check
					log.log(f"Vertex layout check skipped ({type(e).__name__}: {e}); using the table's layout", LOG_WARN)

			self.__autoRaw = None

		# Picks where the position and the UV sit in a 32-byte skinned vertex. Every candidate field (3 x int16, 3 x half float,
		# 3 x float at each possible byte) is scored by "mean triangle edge / size of the mesh": a correct position makes the triangles
		# short against the whole mesh, a wrong field (bone weights, packed normals ...) makes some edges span it. The lowest score wins;
		# the table's layout is kept only when it is about as good as the best (a coherent mesh scores around 0.01-0.03).
		def __autoLayout__(self):
			import numpy as np

			vCnt = self.__gvData.getVertexCnt()
			vStride = self.__gvData.getVertexStride()
			raw = self.__autoRaw

			if not raw or len(raw) < vCnt * vStride or not self.__faces:
				return

			bytesArr = np.frombuffer(raw, dtype=np.uint8, count=vCnt * vStride).reshape(vCnt, vStride)
			tris = np.asarray(self.__faces, dtype=np.int64).reshape(-1, 3)
			tris = tris[(tris >= 0).all(axis=1) & (tris < vCnt).all(axis=1)]

			if len(tris) == 0:
				return

			if len(tris) > 60000:
				tris = tris[:: -(-len(tris) // 60000)]

			def field(kind, byte, n):
				"""(vCnt, n) float32 read at `byte` as int16 / half float / float, or None when it is not a sane number field"""
				dt = {"i2": "<i2", "f2": "<f2", "f4": "<f4"}[kind]
				size = np.dtype(dt).itemsize

				if byte + size * n > vStride:
					return None

				F = np.ascontiguousarray(bytesArr[:, byte:byte + size * n]).view(dt).reshape(vCnt, n).astype(np.float32)

				if kind != "i2" and (not np.isfinite(F).all() or np.abs(F).max() > 1e4):
					return None

				return F

			def ratio(P, minSpan):
				span = P.max(axis=0) - P.min(axis=0)

				if (span < minSpan).any():
					return None

				edges = np.concatenate([np.linalg.norm(P[tris[:, a]] - P[tris[:, b]], axis=1) for a, b in ((0, 1), (1, 2), (2, 0))])
				diag = float(np.linalg.norm(span))

				return float(edges.mean() / diag) if diag > 0 else None

			candidates = []   # (ratio, kind, byte)
			for kind, step in (("i2", 2), ("f2", 2), ("f4", 4)):
				for byte in range(0, vStride, step):
					P = field(kind, byte, 3)

					if P is None:
						continue

					r = ratio(P, 1500 if kind == "i2" else 0.05)

					if r is not None:
						candidates.append((r, kind, byte))

			if not candidates:
				log.log("Vertex layout 17/32: no usable position field found; keeping the table's layout", LOG_WARN)
				return

			candidates.sort()

			# Some smooth 3-vectors in the vertex are NOT positions: bone weights (4 values adding up to 1) and normals (length 1)
			# vary gently over a net and so score better than the real position. Find those fields and rule them out.
			weightBlocks = []   # (first byte, end byte, what)
			for kind, size in (("i2", 2), ("f2", 2), ("f4", 4), ("u1", 1)):
				for byte in range(0, vStride - size * 4 + 1, 1 if kind == "u1" else 2):
					dt = {"i2": "<i2", "f2": "<f2", "f4": "<f4", "u1": "u1"}[kind]
					W = np.ascontiguousarray(bytesArr[:, byte:byte + size * 4]).view(dt).reshape(vCnt, 4).astype(np.float64)

					if not np.isfinite(W).all() or W.min() < 0:
						continue

					total = W.sum(axis=1) / {"i2": 32767.0, "f2": 1.0, "f4": 1.0, "u1": 255.0}[kind]

					if (np.abs(total - 1) < 0.03).mean() > 0.97 and W.max() > 0:
						weightBlocks.append((byte, byte + size * 4, f"bone weights {kind}x4 at bytes {byte}-{byte + size * 4 - 1}"))

			def whyNot(c):
				_r, kind, byte = c
				size = 4 if kind == "f4" else 2
				for a, b, what in weightBlocks:
					if byte < b and byte + size * 3 > a:
						return what

				P = field(kind, byte, 3)
				L = np.linalg.norm(P / 32767.0 if kind == "i2" else P, axis=1)

				if np.abs(L - 1).mean() < 0.03:
					return "unit-length vectors (normals)"

				return None

			ruledOut = [(c, whyNot(c)) for c in candidates]
			kept = [c for c, why in ruledOut if why is None]

			if kept:
				skipped = [(c, why) for c, why in ruledOut if why is not None][:4]
				if skipped:
					log.log("Vertex layout 17/32: ruled out " + ", ".join(f"{c[1]}@{c[2]} ({why})" for c, why in skipped))
				candidates = kept
			else:
				log.log("Vertex layout 17/32: every candidate looked like weights or normals; using the raw ranking", LOG_WARN)

			best = candidates[0]
			cur = next((c for c in candidates if c[1] == "i2" and c[2] == 8), None)

			# The 28-byte skinned vertex (storage format 16, which decodes correctly) keeps its position at byte 12; the 32-byte one
			# has 4 more bytes up front, which puts the position at byte 16. When that field is among the plausible ones, prefer it.
			family = next((c for c in candidates if c[1] == "i2" and c[2] == vStride - 16), None)

			log.log("Vertex layout 17/32: best position candidates (edge ratio, format, byte): "
					+ ", ".join(f"{c[0]:.3f} {c[1]}@{c[2]}" for c in candidates[:6])
					+ f"; table layout (i2@8): {'n/a' if cur is None else f'{cur[0]:.3f}'}")

			if family is not None and family[0] < 0.05 and family[0] <= best[0] * 2.5:
				chosen = family
				P = field(chosen[1], chosen[2], 3)
				P = P / 32768.0
				self.__verts = [list(map(float, p)) for p in P]
				log.log(f"Vertex layout 17/32: position at i2@{chosen[2]} (edge ratio {chosen[0]:.3f}, same place as the 28-byte layout plus 4); "
						f"value range x {P[:, 0].min():.3f}..{P[:, 0].max():.3f}, y {P[:, 1].min():.3f}..{P[:, 1].max():.3f}, z {P[:, 2].min():.3f}..{P[:, 2].max():.3f}")
			elif cur is not None and cur[0] <= best[0] * 1.5:
				chosen = cur
				log.log("Vertex layout 17/32: the table's position is as coherent as the best candidate; kept")
			else:
				chosen = best
				P = field(chosen[1], chosen[2], 3)
				if chosen[1] == "i2":
					P = P / 32768.0
				self.__verts = [list(map(float, p)) for p in P]
				log.log(f"Vertex layout 17/32: position moved to {chosen[1]}@{chosen[2]} (edge ratio {chosen[0]:.3f})")

			# UV: the table has none for this layout. A plausible texture-coordinate range, smooth over the triangles, clear of the position.
			posBytes = range(chosen[2], chosen[2] + (12 if chosen[1] == "f4" else 6))
			bestUV = None
			uvCandidates = {}
			for byte in range(0, vStride - 3, 2):
				if byte in posBytes or byte + 1 in posBytes or byte + 2 in posBytes or byte + 3 in posBytes:
					continue

				if any(byte < b and byte + 4 > a for a, b, _what in weightBlocks):    # bone weights are smooth too, they are not UVs
					continue

				UV = field("i2", byte, 2)

				if UV is None or UV.min() < -16384 or UV.max() > 65536:
					continue

				r = ratio(UV, 400)

				if UV.max() >= 32767 or UV.min() <= -32768:    # clamped values (normals / tangents), not texture coordinates
					continue

				if r is not None and r <= 0.12:
					uvCandidates[byte] = r

					if bestUV is None or r < bestUV[0]:
						bestUV = (r, byte)

			# Diagnostics only: every UV-like field found, with its raw range, and the raw bytes of the first vertices, so the real UV
			# field can be picked by comparing against the game's own UV layout.
			try:
				for _b in sorted(uvCandidates, key=uvCandidates.get)[:8]:
					_f = field("i2", _b, 2)
					log.log(f"Vertex layout 17/32: UV candidate byte {_b}: edge ratio {uvCandidates[_b]:.3f}, raw u {int(_f[:, 0].min())}..{int(_f[:, 0].max())}"
							f" v {int(_f[:, 1].min())}..{int(_f[:, 1].max())}")
				for _i in (0, vCnt // 2, vCnt - 1):
					log.log(f"Vertex layout 17/32: raw vertex {_i}: " + bytesArr[_i].tobytes().hex(" ", 2))
			except Exception as e:
				log.log(f"Vertex layout 17/32: candidate dump failed ({e})", LOG_WARN)

			# the other skinned layouts end with the UV (28-byte format at byte 24): prefer the last four bytes when they qualify
			lastUV = uvCandidates.get(vStride - 4)
			lastField = field("i2", vStride - 4, 2)
			# a field spanning 0..4096 on both axes is a UV set in the game's 12-bit atlas range: take it whatever its smoothness score
			atlas = lastField is not None and lastField.min() >= -64 and lastField.max() <= 4200 and (lastField.max(axis=0) - lastField.min(axis=0) >= 1000).all()

			if lastUV is not None and (atlas or (bestUV is not None and lastUV <= bestUV[0] * 2.5)):
				bestUV = (lastUV, vStride - 4)

			if bestUV is None:
				log.log("Vertex layout 17/32: no texture coordinates found; UVs stay empty", LOG_WARN)
				return

			r, byte = bestUV
			log.log(f"Vertex layout 17/32: UV at byte {byte} (edge ratio {r:.3f})")
			UV = field("i2", byte, 2)
			self.__UVs = [(float(u) / 4096, 1 - float(v) / 4096) for u, v in UV]

			# Diagnostics only (changes nothing): how the picture is laid on the surface. Per triangle, the 3D size of one UV unit along
			# its two stretch directions; a big gap between them (aniso) means stretched UVs, a different median means a different scale.
			try:
				P3 = np.asarray(self.__verts, dtype=np.float64).reshape(-1, 3)
				T2 = np.asarray(self.__UVs, dtype=np.float64).reshape(-1, 2)
				e1, e2 = P3[tris[:, 1]] - P3[tris[:, 0]], P3[tris[:, 2]] - P3[tris[:, 0]]
				u1, u2 = T2[tris[:, 1]] - T2[tris[:, 0]], T2[tris[:, 2]] - T2[tris[:, 0]]
				G3 = np.stack([np.stack([(e1 * e1).sum(1), (e1 * e2).sum(1)], 1), np.stack([(e1 * e2).sum(1), (e2 * e2).sum(1)], 1)], 1)
				Gu = np.stack([np.stack([(u1 * u1).sum(1), (u1 * u2).sum(1)], 1), np.stack([(u1 * u2).sum(1), (u2 * u2).sum(1)], 1)], 1)
				ok = np.abs(np.linalg.det(Gu)) > 1e-12
				ev = np.linalg.eigvals(np.linalg.solve(Gu[ok], G3[ok])).real
				ev = np.sqrt(np.clip(ev, 1e-18, None))
				smax, smin = ev.max(axis=1), ev.min(axis=1)
				log.log(f"Vertex layout 17/32: UV raw range u {int(UV[:, 0].min())}..{int(UV[:, 0].max())} v {int(UV[:, 1].min())}..{int(UV[:, 1].max())}"
						f" -> u {T2[:, 0].min():.3f}..{T2[:, 0].max():.3f} v {T2[:, 1].min():.3f}..{T2[:, 1].max():.3f}")
				log.log(f"Vertex layout 17/32: surface size of one UV unit: median {np.median(smax):.2f} / {np.median(smin):.2f} (units per UV 1.0), "
						f"aniso median {np.median(smax / smin):.2f}, 90th percentile {np.percentile(smax / smin, 90):.2f}")
			except Exception as e:
				log.log(f"Vertex layout 17/32: UV diagnostics failed ({e})", LOG_WARN)

		def getVertices(self):
			return self.__verts
		
		def getUVs(self):
			return self.__UVs
		
		def getFaces(self):
			return self.__faces

		def getObj(self):
			verts, UVs0, faces = self.__verts, self.__UVs, self.__faces

			if not verts:
				raise NotImplementedError("Can't get an obj out of an unimplemented model format")

			obj = ""

			log.log(f"Exporting {len(verts)} vertices")

			for v in SafeIter(self, verts):
				obj += f"v {v[0]:.4f} {v[1]:.4f} {v[2]:.4f}\n"
			
			log.log(f"Exporting {len(UVs0)} UVs (Channel 0)")

			for v in SafeIter(self, UVs0):
				obj += f"vt {v[0]:.4f} {v[1]:.4f}\n"

			# Omit UVs1 entirely so it doesn't corrupt the .obj texture coordinate indices

			log.log(f"Exporting {len(faces)} faces")
			
			for face in SafeIter(self, faces):
				f = ""
				for idx in SafeIter(self, face):
					idx += 1
					f += f" {idx}/{idx}"
				
				if f == "":
					continue

				obj += f"f{f}\n"
			
			return obj

	def getVDCount(self):
		return len(self.__gvdata)
	
	def getVertexDataByLOD(self, lodId:int):
		if not self.__dataComputed:
			self.computeData()
		
		vDataList:list[MatVData.VertexData] = []
		
		for k, gvData in SafeEnumerate(self, self.__gvdata):
			gvData:MatVData.GlobalVertexData

			vDlodId = (gvData.flags & 0xF000) >> 12

			if vDlodId == lodId:
				vDataList.append(self.getVertexData(k))
			
		return vDataList



	def getVertexDataOffset(self, idx:int):
		if not self.__dataComputed:
			self.computeData()
		
		ofs = self.__vdStartOfs

		for i in SafeRange(self, idx):
			gvData = self.__gvdata[i]
			ofs += gvData.getFullVertexDataSz()

		return ofs
	
	def getGlobalVertexData(self, idx):
		return self.__gvdata[idx]

	def getVertexData(self, idx:int):
		if not self.__dataComputed:
			self.computeData()
		
		vdCnt = len(self.__gvdata)

		if idx < 0 or idx > vdCnt:
			raise ValueError(f"Impossible vData ID {idx} (vdCnt = {vdCnt})")
		
		file = self.__file
		gvData = self.__gvdata[idx]

		file.seek(self.__vdStartOfs, 0)

		for i in SafeRange(self, 0, idx):
			file.seek(self.__gvdata[i].getFullVertexDataSz(), 1)
		
		sz = gvData.getFullVertexDataSz()

		log.log(f"Retrieving vertex data {idx} @ {file.tell()} sz={sz}")

		return self.VertexData(file.readBlock(sz), gvData)

	def quickExportVDataToObj(self, idx:int, suffix:str = "", outdir:str = None):
		log.log(f"Quick exporting {idx} as OBJ")
		log.addLevel()

		fileName = f"{self.name}_{idx}{suffix}.obj"

		if outdir is not None:
			fileName = path.join(outdir, fileName)

		obj = self.getVertexData(idx).getObj()

		file = open(fileName, "w")
		file.write(obj)
		file.close()

		log.subLevel()

		log.log(f"Wrote {len(obj)} to {fileName}")

	@property
	def hasMaterials(self):
		return self.__hasMaterials
