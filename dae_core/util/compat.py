import builtins

_orig_classmethod = builtins.classmethod

class _ClassMethodPatch(_orig_classmethod):
	def __init__(self, f):
		super().__init__(f)
		self._f = f

	def __get__(self, inst, cls=None):
		if cls is None:
			cls = type(inst)
		if isinstance(self._f, property):
			return self._f.fget(cls)
		return super().__get__(inst, cls)

# In Python 3.11+ chaining @classmethod and @property was deprecated,
# and in Python 3.13+ it returns a bound method instead of evaluating the property.
# Apply this patch so class-level property evaluation remains compatible.
try:
	_test_prop = _orig_classmethod(property(lambda cls: "ok")).__get__(None, object)
	if not isinstance(_test_prop, str):
		builtins.classmethod = _ClassMethodPatch
except Exception:
	builtins.classmethod = _ClassMethodPatch
