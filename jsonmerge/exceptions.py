class JSONMergeError(Exception):
	def __init__(self, message, value=None, strategy_name=None):
		self.message = message
		self.value = value
		self.strategy_name = strategy_name

	def __str__(self):
		r = self.message

		try:
			ref = self.value.ref
		except:
			ref = None

		if ref is not None:
			r = "%s: %s" % (r, ref)

		if self.strategy_name is not None:
			r = "'%s' merge strategy: %s" % (self.strategy_name, r)

		return r

class BaseInstanceError(JSONMergeError): pass
class HeadInstanceError(JSONMergeError): pass
class SchemaError(JSONMergeError): pass

class OrderingError(JSONMergeError):
	"""Raised when the 'ordering' option cannot be satisfied.

	This happens when a declared relation references an ID that does not
	occur in the merged array or when the relations contain a cycle.
	"""
	pass
